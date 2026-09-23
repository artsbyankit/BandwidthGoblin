"""BandwidthGoblin: download ONE file over ALL your internet links
(LAN + USB-tethered phones) at the same time, with resume, work-stealing,
hot-plug and a live dashboard. Stdlib only."""
import http.client, ssl, sys, os, re, subprocess, urllib.parse, concurrent.futures, time, threading, socket, collections, json, random, hashlib, base64

# A closed stdout (pipes, pagers like `| head`, GUI shells) must never kill a
# run mid-download. Swap print for a version that swallows write errors.
import builtins as _b
_orig_print = _b.print
def _safe_print(*a, **kw):
    try:
        _orig_print(*a, **kw)
    except Exception:
        try:
            # stdout is gone (pipe closed under us). Point it at devnull so
            # later prints AND the interpreter-shutdown flush cannot raise
            # again (which made successful piped runs exit 120, build 21).
            sys.stdout = open(os.devnull, 'w')
        except Exception:
            pass
_b.print = _safe_print  # every print call site hardened with no per-site churn

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, 'w')
    except Exception:
        pass

# Output streams may use an ANSI code page (cp1252 on many Windows boxes) that
# cannot encode non-Latin glyphs. Reconfigure to error-replace so the 👺 logo
# banner prints as '?' on legacy consoles and shows for real on UTF-8
# terminals, instead of silently shredding all further output (build 40 bug:
# _safe_print switched stdout to devnull after the first unencodable print).
try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(errors='replace')
except Exception:
    pass
try:
    if sys.stderr is not None:
        sys.stderr.reconfigure(errors='replace')
except Exception:
    pass

VERSION = '2.0'
BUILD = 41
BETA = True  # when True, version strings carry the "(beta)" label (public status)
_VER_LABEL = ' (beta)' if BETA else ''

SOCK_TIMEOUT = 20   # real socket timeout (sec); enforced via sock.settimeout
STALL_SECS = 25     # abort a chunk Range if zero bytes arrive for this long

DSCP_TOS = 0xB8  # EF = highest user priority tag; routers/ISPs may ignore

class _ConnMixin:
    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_TOS)
        except Exception:
            pass

class _HTTPS(_ConnMixin, http.client.HTTPSConnection): pass
class _HTTP(_ConnMixin, http.client.HTTPConnection): pass

_CTX = ssl.create_default_context()

def _sleep_stop(sec):
    end = time.time() + sec
    while time.time() < end:
        if STOP:
            raise _Stop()
        time.sleep(min(0.2, max(0.0, end - time.time())))

def _throttle(n):
    """Aggregate speed cap across every worker (global token rate).
    Each call grants tokens earned since the last call, sleeps any shortfall,
    then re-anchors the clock so the slept interval is never double-credited.
    Raises _Stop on STOP so a limit never hangs a shutdown."""
    if not SPEEDLIMIT:
        return
    global THR_CAP, THR_LAST
    with THR_LOCK:
        now = time.time()
        if not THR_LAST:
            THR_LAST = now
        THR_CAP = min(SPEEDLIMIT, THR_CAP + (now - THR_LAST) * SPEEDLIMIT)
        THR_LAST = now
        if THR_CAP < n:
            _sleep_stop((n - THR_CAP) / SPEEDLIMIT)
            THR_LAST = time.time()     # sleep time cannot refill the bucket twice
            THR_CAP = 0.0
        else:
            THR_CAP -= n

def _toggle_pause():
    global PAUSED
    PAUSED = not PAUSED
    if PAUSED:
        log('PAUSED - press P/R to resume, ESC to stop')
    else:
        log('RESUMED - downloads continuing')

def _parse_ifconfig(out):
    ips, current = [], None
    for line in out.splitlines():
        if line and not line[0].isspace():
            current = line.split(':')[0].strip()
        m = re.search(r'\binet (\d+\.\d+\.\d+\.\d+)\b', line)
        if m and not m.group(1).startswith('127.'):
            ips.append((current or 'net', m.group(1)))
    return ips

def local_ips():
    ips = []
    try:
        if os.name == 'nt':
            out = subprocess.check_output('ipconfig', text=True, errors='ignore')
            current = None
            for line in out.splitlines():
                if 'adapter' in line and ':' in line: current = line.strip()
                m = re.search(r'IPv4 Address[.\s]+:\s*([\d.]+)', line)
                if m and not m.group(1).startswith('127.'):
                    ips.append((current or 'windows', m.group(1)))
        elif sys.platform == 'darwin':
            out = subprocess.check_output(['ifconfig'], text=True, errors='ignore')
            ips = _parse_ifconfig(out)
        else:  # linux and others
            try:
                out = subprocess.check_output(['ip', '-4', '-o', 'addr', 'show'],
                                              text=True, errors='ignore')
                for line in out.splitlines():
                    m = re.search(r'^\d+:\s+(\S+).*?\binet (\d+\.\d+\.\d+\.\d+)', line)
                    if m and not m.group(2).startswith('127.'):
                        ips.append((m.group(1), m.group(2)))
            except (FileNotFoundError, OSError, subprocess.CalledProcessError):
                out = subprocess.check_output(['ifconfig'], text=True, errors='ignore')
                ips = _parse_ifconfig(out)
    except Exception:
        pass
    # dedupe, drop loopback/link-local/APIPA
    seen, res = set(), []
    for name, ip in ips:
        if ip not in seen and not ip.startswith(('127.', '169.254.')):
            seen.add(ip); res.append((name, ip))
    return res

def _is_private(ip):
    try:
        p = ip.split('.')
        a = int(p[0]); b = int(p[1]) if len(p) > 1 else 0
        return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)
    except Exception:
        return False

def _same_subnet(a, b):
    try:
        return a.split('.')[:3] == b.split('.')[:3]
    except Exception:
        return False

# Neighbour MAC vendor OUIs -> device kind (best-effort, display only).
_DEVICE_OUIS = {
    # Apple (iPhone / iPad / Apple Watch - hotspot devices)
    '000393': 'iPhone', '000a27': 'iPhone', '000ebf': 'iPhone', '0017f2': 'iPhone',
    '001b63': 'iPhone', '001d4f': 'iPhone', '001f03': 'iPhone', '001ff3': 'iPhone',
    '002332': 'iPhone', '0023df': 'iPhone', '002500': 'iPhone', '0026bb': 'iPhone',
    '3c15c2': 'iPhone', '3c22fb': 'iPhone', '482ae3': 'iPhone', '64a2f9': 'iPhone',
    '705681': 'iPhone', '88532e': 'iPhone', '8c5877': 'iPhone', 'a45e60': 'iPhone',
    'a851ab': 'iPhone', 'acbc32': 'iPhone', 'b065bd': 'iPhone', 'bc52b7': 'iPhone',
    'c42c03': 'iPhone', 'cc08e0': 'iPhone', 'd023db': 'iPhone', 'd0e140': 'iPhone',
    'f09fc2': 'iPhone', 'f025b7': 'iPhone', 'f40f24': 'iPhone',
    # Samsung
    '0012fb': 'Android', '080028': 'Android', '343111': 'Android', '6068d3': 'Android',
    '746b82': 'Android', '80248c': 'Android', 'a0b4a5': 'Android', 'ac5f3e': 'Android',
    'b49eab': 'Android', 'c0517e': 'Android', 'c80aa9': 'Android', 'd021f9': 'Android',
    # Google / Pixel
    '001a11': 'Android', '3859f9': 'Android', '3c5ab4': 'Android', 'a47733': 'Android',
    'f4f5d8': 'Android', 'f81a67': 'Android',
    # Xiaomi / Redmi / Poco
    '0064fb': 'Android', '083e0c': 'Android', '0ca0ce': 'Android', '20f478': 'Android',
    '286c07': 'Android', '2cb96b': 'Android', '305cfd': 'Android', '34ce00': 'Android',
    '381325': 'Android', '449abb': 'Android', '48de5f': 'Android', '544f19': 'Android',
    '5820b1': 'Android', '64a5c3': 'Android', '74852d': 'Android', '7811dc': 'Android',
    '84df9d': 'Android', '90ab1c': 'Android', 'a0510b': 'Android', 'a487a8': 'Android',
    'ac2b93': 'Android', 'b03495': 'Android', 'c4e78a': 'Android', 'd8c468': 'Android',
    'e465c8': 'Android', 'f45c89': 'Android', 'f83923': 'Android',
    # Huawei / Honor
    '002324': 'Android', '04bd88': 'Android', '0c37dc': 'Android', '142d27': 'Android',
    '204c03': 'Android', '2c910f': 'Android', '305a3a': 'Android', '3c6314': 'Android',
    '4098ad': 'Android', '4439c4': 'Android', '549f35': 'Android', '58a235': 'Android',
    '682bcb': 'Android', '78f5fd': 'Android', '8425db': 'Android', '88c397': 'Android',
    '8c34fd': 'Android', 'a0d795': 'Android', 'b05cda': 'Android', 'cc8c75': 'Android',
    'dcd2fc': 'Android', 'e0191d': 'Android', 'f42e73': 'Android',
    # OnePlus
    '009ec8': 'Android', '68572d': 'Android', '74046b': 'Android', 'b43909': 'Android',
    'd43a2c': 'Android',
    # Routers / access points (LAN gateways)
    '006098': 'LAN', '14cc20': 'LAN', '30b5c2': 'LAN', '3c84a6': 'LAN', '50c7bf': 'LAN',
    '60898b': 'LAN', '64d1a3': 'LAN', '8c210a': 'LAN', '98dacc': 'LAN', 'a025b4': 'LAN',
    'a851ac': 'LAN', 'b0a72b': 'LAN', 'b8a375': 'LAN', 'bcd1f2': 'LAN', 'c02c7a': 'LAN',
    'c4e984': 'LAN', 'c8be19': 'LAN', 'd0254e': 'LAN', 'd85162': 'LAN', 'dc518c': 'LAN',
    'e05b4b': 'LAN', 'ec2af0': 'LAN', 'f4342b': 'LAN', 'fc3496': 'LAN', 'f80377': 'LAN',
    'f8d111': 'LAN',  # TP-Link
    '006018': 'LAN', '14dda9': 'LAN', '2c0dd0': 'LAN', '34c993': 'LAN', '48c0b9': 'LAN',
    '58d08e': 'LAN', '5c8ab8': 'LAN', '8c107b': 'LAN', '9466f3': 'LAN', '9c2a70': 'LAN',
    'ac9e17': 'LAN', 'd017c2': 'LAN', 'e8a1af': 'LAN',  # ASUS
    '0012bf': 'LAN', '0017c8': 'LAN', '00260c': 'LAN', '0050f1': 'LAN',  # Linksys
    '000199': 'LAN', '000e5c': 'LAN', '0015e9': 'LAN', '001e58': 'LAN',  # D-Link
    '00b014': 'LAN', '08ea40': 'LAN',  # Tenda
}

# Keywords looked for inside PTR hostnames of neighbours.
_PTR_KW = (
    ('iphone', 'iPhone'), ('ipad', 'iPhone'), ('apple', 'Apple'),
    ('macbook', 'Mac'), ('imac', 'Mac'), ('macpro', 'Mac'),
    ('android', 'Android'), ('samsung', 'Samsung'), ('galaxy', 'Samsung'),
    ('pixel', 'Pixel'), ('xiaomi', 'Xiaomi'), ('redmi', 'Xiaomi'), ('poco', 'Xiaomi'),
    ('huawei', 'Huawei'), ('honor', 'Honor'), ('oneplus', 'OnePlus'), ('oppo', 'Oppo'),
    ('vivo', 'Vivo'), ('realme', 'Realme'), ('nokia', 'Nokia'), ('motorola', 'Motorola'),
    ('moto', 'Motorola'), ('lenovo', 'Lenovo'), ('zte', 'ZTE'),
    ('tplink', 'TP-Link'), ('tp-link', 'TP-Link'), ('asus', 'ASUS'), ('netgear', 'NETGEAR'),
    ('linksys', 'Linksys'), ('dlink', 'D-Link'), ('d-link', 'D-Link'), ('tenda', 'Tenda'),
    ('router', 'LAN'), ('gateway', 'LAN'),
)

_LABEL_CACHE = {}          # ip -> 'Kind (ip)' final label (per run)
_LABEL_FINED = set()       # ips already enriched by the background warmer
_PTR_CACHE = {}            # neighbour ip -> lowercased first PTR label (per run)
_NEIGH_T = 0.0
_NEIGH_CACHE = {}
_NEIGH_LOCK = threading.Lock()
_PTR_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='lbptr')

def _neighbor_table():
    """{ neighbour_ip: mac-nodash-lower } for live on-link neighbours (cached 30s)."""
    global _NEIGH_T, _NEIGH_CACHE
    with _NEIGH_LOCK:
        now = time.time()
        if _NEIGH_T and now - _NEIGH_T < 30:
            return _NEIGH_CACHE
        tbl = {}
        try:
            if os.name == 'nt':
                out = subprocess.check_output('arp -a', text=True, errors='ignore')
                for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})', out):
                    tbl[m.group(1)] = m.group(2).replace('-', '').lower()
            elif sys.platform == 'darwin':
                out = subprocess.check_output(['arp', '-a'], text=True, errors='ignore')
                for m in re.finditer(r'\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})', out):
                    tbl[m.group(1)] = m.group(2).replace(':', '').lower()
            else:
                try:
                    out = subprocess.check_output(['ip', 'neigh'], text=True, errors='ignore')
                    for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)\s+.*?lladdr\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})', out):
                        tbl[m.group(1)] = m.group(2).replace(':', '').lower()
                except (FileNotFoundError, OSError, subprocess.CalledProcessError):
                    out = subprocess.check_output(['arp', '-n'], text=True, errors='ignore')
                    for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)\s+ether\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})', out):
                        tbl[m.group(1)] = m.group(2).replace(':', '').lower()
        except Exception:
            pass
        _NEIGH_T, _NEIGH_CACHE = now, tbl
        return tbl

def _ptr_of(ip):
    got = _PTR_CACHE.get(ip)
    if got is not None:
        return got
    def _lookup():
        try:
            return (socket.gethostbyaddr(ip)[0] or '').lower().split('.')[0]
        except Exception:
            return ''
    try:
        name = _PTR_POOL.submit(_lookup).result(timeout=1.5)
    except Exception:
        name = ''
    _PTR_CACHE[ip] = name
    return name

def _name_brand(host):
    h = (host or '').lower()
    for kw, kind in _PTR_KW:
        if kw in h:
            return kind
    return ''

def link_label(ip):
    """Friendly per-link label, e.g. 'iPhone (172.20.10.2)' / 'LAN (192.168.1.13)'.
    Display only - internal keys stay the raw ip. Never blocks on DNS:
    PTR names are warmed in the background by _warm_labels()."""
    if not ip:
        return 'default'
    lbl = _LABEL_CACHE.get(ip)
    if lbl:
        return lbl
    kind = ''
    try:
        for n_ip, mac in _neighbor_table().items():
            if _same_subnet(n_ip, ip):
                kind = _DEVICE_OUIS.get(mac[:6], '')
                if kind:
                    break
    except Exception:
        pass
    if not kind:
        if ip.startswith('172.20.10.'):
            kind = 'iPhone'          # Apple hotspot default subnet
        elif ip.startswith('169.254.'):
            kind = 'Link-local'
        elif _is_private(ip):
            kind = 'LAN'
    if not kind:
        kind = 'Link'
    lbl = f'{kind} ({ip})'
    _LABEL_CACHE[ip] = lbl
    return lbl

def _probe_links(ips, url):
    """Best-effort 'are these connections REALLY here?' check run before a
    combined download: open one bare TCP connect to the download host through
    each link's source IP. An adapter can hold an IP yet have no working route
    out (dead WiFi, dead hotspot, VPN ghost). Those fail fast here - so we can
    tell the user 'only 1 working connection' instead of pretending we are
    combining. Returns the subset of ips that reached the host; [] on parse
    errors keeps callers safe (they just skip probing)."""
    good = []
    try:
        u = urllib.parse.urlparse(url)
        host = u.hostname or ''
        port = u.port or (443 if u.scheme == 'https' else 80)
        if not host:
            return good
    except Exception:
        return good
    for ip in ips:
        try:
            with socket.create_connection((host, port), timeout=4.0,
                                          source_address=(ip, 0)):
                good.append(ip)
        except Exception as ex:
            log(f'link {link_label(ip)}: cannot reach {host}:{port} - dropped ({ex.__class__.__name__})')
    return good

def _warm_labels(ips):
    """Background: enrich labels from neighbour MACs / PTR names once traffic
    has populated the ARP table (best-effort; updates the dash as it goes)."""
    def _enrich():
        try:
            for _ in range(6):
                time.sleep(1.5)
                tbl = _neighbor_table()
                for ip in ips:
                    if not ip or ip in _LABEL_FINED:
                        continue
                    for n_ip, mac in tbl.items():
                        if not _same_subnet(n_ip, ip):
                            continue
                        kind = _DEVICE_OUIS.get(mac[:6], '') or _name_brand(_ptr_of(n_ip))
                        if not kind:
                            continue
                        _LABEL_CACHE[ip] = f'{kind} ({ip})'
                        _LABEL_FINED.add(ip)
                        break
                if all(not (i and i not in _LABEL_FINED) for i in ips):
                    break
        except Exception:
            pass
    try:
        threading.Thread(target=_enrich, daemon=True, name='lbwarm').start()
    except Exception:
        pass

def get_conn(url, ip):
    u = urllib.parse.urlparse(url)
    port = u.port or (443 if u.scheme == 'https' else 80)
    key = (threading.get_ident(), ip, u.hostname, port, u.scheme)
    with _CONN_LOCK:
        c = _CONN_CACHE.get(key)
    path = (u.path or '/') + ('?' + u.query if u.query else '')
    if c is not None:
        try:
            st = getattr(c, '_HTTPConnection__state', '_CS_IDLE')
            if getattr(c, 'sock', None) is None or st != '_CS_IDLE':
                try: c.close()
                except Exception: pass
                c = None
        except Exception:
            c = None
    if c is not None:
        _apply_sock_timeout(c)
        return c, path, True
    _pace_conn()
    conn, _ = open_conn(url, ip)
    with _CONN_LOCK:
        _CONN_CACHE[key] = conn
    return conn, path, False

def drop_conn(url, ip):
    try:
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == 'https' else 80)
        key = (threading.get_ident(), ip, u.hostname, port, u.scheme)
        with _CONN_LOCK:
            c = _CONN_CACHE.pop(key, None)
        if c is not None:
            try: c.close()
            except Exception: pass
    except Exception:
        pass

def _apply_sock_timeout(conn, secs=SOCK_TIMEOUT):
    try:
        conn.timeout = secs
    except Exception:
        pass
    try:
        s = getattr(conn, 'sock', None)
        if s is not None:
            s.settimeout(secs)
    except Exception:
        pass

def open_conn(url, local_ip=None):
    u = urllib.parse.urlparse(url)
    kw = {'source_address': (local_ip, 0)} if local_ip else {}
    conn = (_HTTPS(u.hostname, u.port or 443, context=_CTX, timeout=SOCK_TIMEOUT, **kw)
            if u.scheme == 'https' else
            _HTTP(u.hostname, u.port or 80, timeout=SOCK_TIMEOUT, **kw))
    _apply_sock_timeout(conn)
    return conn, (u.path or '/') + ('?' + u.query if u.query else '')

def redact(u):
    """Strip query strings (token/expiry params) from a URL for logs."""
    try:
        p = urllib.parse.urlparse(u or '')
        return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, '', '', ''))
    except Exception:
        return (u or '')[:48]

def _same_val(a, b):
    """ETag/Last-Modified comparison tolerant of quoted / weak forms."""
    def _norm(x):
        if not x: return ''
        x = str(x).strip()
        if x.startswith('W/'): x = x[2:]
        return x.strip('"').strip()
    na, nb = _norm(a), _norm(b)
    return bool(na) and na == nb

def head(url):
    for _ in range(4):
        conn, path = open_conn(url)
        conn.request('HEAD', path, headers={'User-Agent': UA, 'Accept': '*/*', 'Connection': 'keep-alive', **EXTRA_HEADERS})
        r = conn.getresponse(); r.read()
        if r.status in (301, 302, 303, 307, 308) and r.getheader('Location'):
            try: conn.close()
            except Exception: pass
            url = urllib.parse.urljoin(url, r.getheader('Location')); continue
        if r.status in (400, 401, 403, 404, 410):
            raise RuntimeError(f'HTTP {r.status} for {redact(url)}')
        if r.status >= 500:
            raise RuntimeError(f'HTTP {r.status} (server error) for {redact(url)}')
        try:
            size = int(r.getheader('Content-Length') or 0)
        except (ValueError, TypeError):
            size = 0   # malformed Content-Length - treat as unknown size
        ar = r.getheader('Accept-Ranges')
        meta = {'etag': r.getheader('ETag') or '',
                'lastmod': r.getheader('Last-Modified') or '',
                'ctype': (r.getheader('Content-Type') or '').split(';')[0].strip().lower(),
                'enc': (r.getheader('Content-Encoding') or '').strip().lower()}
        if not size and ar and 'bytes' in ar.lower():
            try:
                conn2, path2 = open_conn(url)
                conn2.request('GET', path2, headers={'Range': 'bytes=0-0', 'User-Agent': UA,
                                                    'Accept-Encoding': 'identity', **EXTRA_HEADERS})
                r2 = conn2.getresponse()
                cr = r2.getheader('Content-Range') or ''
                enc2 = (r2.getheader('Content-Encoding') or '').strip().lower()
                r2.read(); conn2.close()
                m = re.search(r'/(\d+)\s*$', cr)
                if m and enc2 in ('', 'identity'): size = int(m.group(1))
            except Exception:
                pass
        return url, size, ar, meta
    raise RuntimeError('too many redirects')

def range_probe(url, ip=None, size=0):
    """Prove the server honours a REAL mid-file Range before multi-link mode.
    A range starting at 0 can be special-cased by servers (raw.githubusercontent
    serves `bytes=0-N` as 206 but re-sends 200 full bodies for other offsets).
    We probe a range that never starts at 0 (size//2) to catch such liars.
    Returns (proved_206, total). 200-only servers must drop to single stream."""
    if size and size > 1:
        s0, e0 = max(1, size // 2), size - 1
    else:
        s0, e0 = 0, 262143
    conn, path = open_conn(url, ip)
    try:
        conn.request('GET', path, headers={'Range': f'bytes={s0}-{e0}', 'User-Agent': UA,
                                          'Accept': '*/*', 'Accept-Encoding': 'identity', **EXTRA_HEADERS})
        r = conn.getresponse()
        cr = r.getheader('Content-Range') or ''
        enc = (r.getheader('Content-Encoding') or '').strip().lower()
        r.read(1 << 20)
        if r.status == 206:
            m = re.match(rf'bytes\s+{s0}-(\d+)/(\d+|\*)$', cr.strip())
            if m and enc in ('', 'identity'):
                tot = 0 if m.group(2) == '*' else int(m.group(2))
                return True, tot
            raise RuntimeError(f'bad 206 probe: {cr!r}')
        if r.status == 200:
            cl = r.getheader('Content-Length') or '0'
            try:
                total = int(cl)
            except ValueError:
                total = 0
            return False, total if enc in ('', 'identity') else 0
        raise RuntimeError(f'range probe: HTTP {r.status}')
    finally:
        try: conn.close()
        except Exception: pass

LOGFILE = None
LOGLOCK = threading.Lock()
T0 = 0
RING = collections.deque(maxlen=8)
LINK_DONE = {}
LINK_LOCK = threading.Lock()
LINK_SPEED = {}
PERF_LOCK = threading.Lock()
G_HOST = ''
CHUNK_STATE = {}
CHUNK_LOCK = threading.Lock()
LINK_THREADS = {}
DASH_ON = False
DASH_MODE = 'off'
DASH_EMA = {}
SPARK = collections.deque(maxlen=90)
STOP = False
STOP_REASON = 'ESC'
RUN_DONE = threading.Event(); RUN_DONE.set()   # set => no helper threads should run
LAST_PROGRESS = 0.0                            # watchdog: last time bytes were written
WATCHDOG_SECS = 180                            # stall this long -> hard stop
V_ETAG = ''                                    # validator from head() used for If-Range
V_LM = ''
RANGE_VERIFIED = False                         # server proven to serve real 206 ranges
VALIDATOR_BROKEN = False                       # If-Range/ETag proven unstable -> stop sending If-Range (build 36)
EXPECT_SHA = ''                                # optional sha256 from BANDWIDTHGOBLIN_SHA256
OPEN_AFTER = None                              # None=open folder only in tty; True=always; False=never
EXTRA_HEADERS = {}                             # extra request headers (referer/cookie/custom)
PAUSED = False                                 # soft pause (P key): keep sockets, stall workers/steals
QUIET = False                                  # --quiet: stdout chatter suppressed, milestones kept
NO_LOG = False                                 # --no-log: skip per-run disk logs
SPEEDLIMIT = 0.0                               # --limit: aggregate bytes/sec cap (0 = off)
THR_LOCK = threading.Lock()
THR_CAP = 0.0
THR_LAST = 0.0
SPEEDTEST = 0.0                                # --speedtest: bytes to measure before stopping (0 = off)
HIST_PATH = None                               # lazy: history.jsonl next to perf.jsonl
REFRESH_LOCK = threading.Lock()
LAST_REFRESH_T = 0.0
REFRESH_NOOP = 0
RETRY_SINGLE = False                             # drop to single-stream when no link can multi-range
CHUNK_STEAL_N = {}                             # chunk -> times stolen this run (cap 8)
ESC_THREAD = None
CHUNK_RANGE = {}
CHUNK_POS = {}
CHUNK_OWNER = {}
CHUNK_END = {}
CHUNKS_DONE = 0
NCH = 0
LINK_JOINED = {}
STEAL_LOCK = threading.Lock()
CHUNK_STEALTIME = {}
STEAL_MIN = 8 << 20
G_LOCK = threading.Lock()
G_FUTS = []
G_RUNID = 0
G_STARTED = set()
_FH = None

class _Stop(Exception):
    pass

class _SpeedtestDone(Exception):
    """--speedtest reached its measurement cap - stop cleanly, report rates."""
    pass

class _Stolen(Exception):
    pass

class _Fatal(Exception):
    """Unrecoverable error - stop the whole run instead of corrupting the file."""
    pass

class _NoRange(Exception):
    """This link cannot serve real byte ranges (server sends 200 full bodies).
    Drop the link and re-queue its work - never corrupt, never brick the run."""
    pass

def _install_crash_hooks():
    def _hook(t, v, tb):
        try:
            import traceback
            txt = ''.join(traceback.format_exception(t, v, tb))
            try:
                if LOGFILE:
                    with open(LOGFILE, 'a', errors='replace') as _f: _f.write(txt + '\n')
            except Exception:
                pass
        finally:
            sys.__excepthook__(t, v, tb)
    sys.excepthook = _hook
    try:
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass
ORIG_URL = ''
FINAL_URL = ''
FINAL_LOCK = threading.Lock()
REDIR = {}
REDIR_LOCK = threading.Lock()
_CONN_CACHE = {}
_CONN_LOCK = threading.Lock()
REQ_TIMES = collections.deque()
REQ_LOCK = threading.Lock()
REQ_PER_SEC = 4

def _pace_conn():
    while True:
        if STOP:
            raise _Stop()
        with REQ_LOCK:
            now = time.time()
            while REQ_TIMES and REQ_TIMES[0] <= now - 1.0:
                REQ_TIMES.popleft()
            if len(REQ_TIMES) < REQ_PER_SEC:
                REQ_TIMES.append(now)
                return
            wait = REQ_TIMES[0] + 1.0 - now
        _sleep_stop(max(0.05, wait))
G429_N = 0
G429_COOLDOWN = 0.0
G429_LOCK = threading.Lock()

def _cooldown_wait():
    while True:
        if STOP:
            raise _Stop()
        u = G429_COOLDOWN
        if time.time() >= u:
            return
        time.sleep(0.2)
SOCK_REG = {}
SOCK_LOCK = threading.Lock()

def abort_sockets():
    with SOCK_LOCK:
        conns = list(SOCK_REG.values())
        SOCK_REG.clear()
    for c in conns:
        try:
            c.close()
        except Exception:
            pass

def _close_ip_socks(ip):
    """Close every socket registered to one link so its in-flight workers
    unblock immediately (used when a link is dropped mid-run)."""
    with SOCK_LOCK:
        victims = [c for t, c in list(SOCK_REG.items())
                   if isinstance(t, tuple) and t[0] == ip]
    for c in victims:
        try:
            c.close()
        except Exception:
            pass
IP_STATE = {}
IP_LOCK = threading.Lock()
PARK_FAILS = 6
PARK_SECS = 30
TOTAL_DONE = 0
TOTAL_DONE_LOCK = threading.Lock()
TOTAL_SIZE = 0
STAT429 = {}
STAT_LOCK = threading.Lock()
WORKER_CAP = {}
ACTIVE = {}
CHUNK_TRIES = {}
CHUNK_TRIES_LOCK = threading.Lock()

def log(msg):
    line = f'{time.strftime("%H:%M:%S")} {msg}'
    RING.append(line)
    if not DASH_ON:
        out_line = line
        if 'TOTAL:' in msg:
            out_line = re.sub(r'([\d.]+ MB/s)', lambda m: CYLW(m.group(1)), line)
        if not QUIET or any(k in msg for k in ('TOTAL:', 'all chunks done', 'resume saved',
                                                'DOWNLOAD FAILED', 'STOPPED', 'finalizing:',
                                                'auto-restarting', 'single-stream retry',
                                                'WATCHDOG', 'PAUSED', 'RESUMED', 'FAILED')):
            print(out_line, flush=True)
    if LOGFILE and not NO_LOG:
        with LOGLOCK:
            with open(LOGFILE, 'a', errors='replace') as lf: lf.write(_ANSI.sub('', line) + '\n')

def _prune_logs(d, keep=20):
    try:
        if not d or not os.path.isdir(d):
            return
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith('.log')),
                       key=os.path.getmtime, reverse=True)
        for f in files[keep:]:
            try: os.remove(f)
            except Exception: pass
    except Exception:
        pass

def get_final():
    with FINAL_LOCK:
        return FINAL_URL

def get_url_for(ip):
    with REDIR_LOCK:
        u = REDIR.get(ip)
    return u or get_final()

def ip_ok(ip):
    if ip is None: return True
    with IP_LOCK:
        st = IP_STATE.get(ip)
        if st and st.get('parked_until', 0) > time.time():
            return False
    return True

def ip_success(ip):
    if ip is None: return
    with IP_LOCK:
        IP_STATE[ip] = {'fails': 0, 'parked_until': 0}

def ip_failed(ip):
    if ip is None: return
    with IP_LOCK:
        st = IP_STATE.setdefault(ip, {'fails': 0, 'parked_until': 0})
        st['fails'] += 1
        if st['fails'] >= PARK_FAILS:
            try:
                present = any(x == ip for _, x in local_ips())
            except Exception:
                present = True
            cool = 60 if not present else PARK_SECS
            st['parked_until'] = time.time() + cool
            st['fails'] = 0
            log(f'link {ip} parked {cool}s ({"gone" if not present else "failing"})')

def _esc_listener():
    global STOP, STOP_REASON
    try:
        if os.name == 'nt':
            import msvcrt
            while not STOP and not RUN_DONE.is_set():
                time.sleep(0.05)
                try:
                    if msvcrt.kbhit():
                        c = msvcrt.getch()
                        if c == b'\x1b':
                            STOP = True
                            STOP_REASON = 'ESC'
                            return
                        if c in (b'p', b'P', b'r', b'R') and not RUN_DONE.is_set():
                            _toggle_pause()
                except Exception:
                    return
        else:
            import termios, tty, select
            if not sys.stdin.isatty():
                return
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while not STOP and not RUN_DONE.is_set():
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if r:
                        c = sys.stdin.read(1)
                        if c == '\x1b':
                            STOP = True
                            STOP_REASON = 'ESC'
                            return
                        if c.lower() in ('p', 'r') and not RUN_DONE.is_set():
                            _toggle_pause()
            finally:
                try: termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except Exception: pass
    except Exception:
        return

def follow_redirects(url, ip, max_hops=5):
    cur = url
    for _ in range(max_hops):
        conn, path = open_conn(cur, ip)
        conn.request('HEAD', path, headers={'User-Agent': UA, 'Accept': '*/*',
                                            'Accept-Encoding': 'identity', 'Connection': 'keep-alive', **EXTRA_HEADERS})
        r = conn.getresponse(); r.read()
        if r.status in (301, 302, 303, 307, 308) and r.getheader('Location'):
            cur = urllib.parse.urljoin(cur, r.getheader('Location'))
            try: conn.close()
            except Exception: pass
            continue
        try: conn.close()
        except Exception: pass
        return cur
    return cur

def refresh_final(force=False):
    global FINAL_URL, LAST_REFRESH_T, REFRESH_NOOP
    now = time.time()
    with REFRESH_LOCK:
        if not force and now - LAST_REFRESH_T < 5:
            REFRESH_NOOP += 1
            if REFRESH_NOOP <= 3:
                log(f'refresh throttled (x{REFRESH_NOOP})')
            return get_final()
        LAST_REFRESH_T = now
        REFRESH_NOOP = 0
    try:
        u, size0, ar0, meta0 = head(ORIG_URL)
    except Exception as ex:
        log(f'refresh failed: {ex}')
        return get_final()
    old = get_final()
    with FINAL_LOCK:
        if old != u:
            FINAL_URL = u
            log(f'refreshed url -> {redact(u)[:80]}')
            return u
        log('refresh: url unchanged')
        return u

def fetch_direct(idx, s, e, ip, out, tok):
    global TOTAL_DONE, G429_N, G429_COOLDOWN, LAST_PROGRESS, THR_LAST
    global VALIDATOR_BROKEN
    bounded = isinstance(e, int)
    total = (e - s + 1) if bounded else 0
    done = 0
    t0, last = time.time(), time.time()
    retries = 0
    _stalefresh = 0  # silent reconnects for server-closed keep-alives (build 36)
    # NB: os.open defaults to TEXT mode on Windows (translates \n -> \r\n
    # and corrupts binaries). O_BINARY is required. getattr for POSIX.
    _BIN = getattr(os, 'O_BINARY', 0)
    fd = os.open(out, os.O_RDWR | os.O_CREAT | _BIN)
    try:
        while (done < total) if bounded else True:
            if STOP:
                raise _Stop()
            _cooldown_wait()
            with CHUNK_LOCK:
                if bounded and CHUNK_OWNER.get(idx) != tok:
                    raise _Stolen()
            url = get_url_for(ip)
            try:
                conn, path, reused = get_conn(url, ip)
                _apply_sock_timeout(conn)
                if not reused:
                    _sleep_stop(random.uniform(0.05, 0.4))
                with SOCK_LOCK:
                    SOCK_REG[tok] = conn
                _ifrange = ''
                if bounded:
                    if RANGE_VERIFIED and not VALIDATOR_BROKEN:
                        if V_ETAG and not V_ETAG.startswith('W/'):
                            _ifrange = f"'{V_ETAG}'"
                        elif V_LM:
                            _ifrange = V_LM
                    _rh = {'Range': f'bytes={s+done}-{e}',
                           'User-Agent': UA, 'Accept': '*/*',
                           'Accept-Language': 'en-US,en;q=0.9',
                           'Accept-Encoding': 'identity',
                           'Connection': 'keep-alive',
                           **EXTRA_HEADERS}
                    if _ifrange:
                        _rh['If-Range'] = _ifrange
                else:
                    _rh = {'User-Agent': UA, 'Accept': '*/*',
                           'Accept-Language': 'en-US,en;q=0.9',
                           'Accept-Encoding': 'identity',
                           'Connection': 'keep-alive',
                           **EXTRA_HEADERS}
                try:
                    conn.request('GET', path, headers=_rh)
                    r = conn.getresponse()
                except (http.client.HTTPException, OSError, TimeoutError, ConnectionError) as ex:
                    # Server closed an idle keep-alive between chunks: the cached
                    # connection looks idle but answers nothing (BadStatusLine /
                    # RemoteDisconnected). That is a stale socket, not a failed
                    # transfer -> reconnect silently instead of burning the give-up
                    # counter and sleeping (build 36, found in real-world tests).
                    drop_conn(url, ip)
                    if reused and done == 0 and _stalefresh < 2 and \
                            isinstance(ex, (http.client.BadStatusLine, ConnectionResetError)):
                        _stalefresh += 1
                        log(f'chunk{idx} via {ip}: stale keep-alive on first request ({ex}) - fresh connection')
                        continue
                    raise
                if r.status in (301, 302, 303, 307, 308):
                    loc = r.getheader('Location'); r.read()
                    if not loc: raise RuntimeError(f'HTTP {r.status} no location')
                    final = follow_redirects(urllib.parse.urljoin(url, loc), ip)
                    drop_conn(url, ip)
                    with REDIR_LOCK: REDIR[ip] = final
                    log(f'chunk{idx} via {ip}: redirect -> {urllib.parse.urlparse(final).hostname}')
                    continue
                if r.status == 429:
                    ra = r.getheader('Retry-After')
                    base = int(ra) if (ra and ra.isdigit()) else 5
                    r.read()
                    with STAT_LOCK:
                        STAT429[ip] = STAT429.get(ip, 0) + 1
                    with G429_LOCK:
                        G429_N += 1
                        if G429_N > 0 and G429_N % 30 == 0:
                            G429_COOLDOWN = time.time() + 60
                            log('server throttling everything: 60s cooldown, calm down')
                    retries += 1
                    wait = min(base * (2 ** (retries - 1)), 60)
                    wait += random.uniform(0, min(wait * 0.25, 5))
                    log(f'chunk{idx} via {ip}: 429 throttle, sleep {wait:.1f}s r{retries}')
                    _sleep_stop(wait)
                    if retries > 8: raise RuntimeError(f'chunk{idx}: too many 429s, release for steal')
                    continue
                if r.status in (401, 403, 410):
                    log(f'chunk{idx} via {ip}: HTTP {r.status}, backing off')
                    r.read()
                    with STAT_LOCK:
                        STAT429[ip] = STAT429.get(ip, 0) + 1
                    retries += 1
                    wait = min(60, 2 ** min(retries, 6)) + random.uniform(0, 1)
                    log(f'chunk{idx} via {ip}: backoff {wait:.1f}s r{retries}')
                    refresh_final()
                    _sleep_stop(wait)
                    if retries > 5: raise RuntimeError(f'chunk{idx}: too many {r.status}s, release for steal')
                    continue
                if r.status == 412:
                    r.read()
                    raise _Fatal('server says file changed (412 Precondition Failed) - restart the download')
                if r.status not in (200, 206):
                    r.read()
                    raise RuntimeError(f'HTTP {r.status}')
                _enc = (r.getheader('Content-Encoding') or '').strip().lower()
                if _enc not in ('', 'identity'):
                    raise _Fatal(f'server sent Content-Encoding {_enc!r} - unsupported, aborting instead of corrupting')
                _ct = (r.getheader('Content-Type') or '').split(';')[0].strip().lower()
                if _ct in ('text/html', 'application/xhtml+xml') and os.path.splitext(out)[1].lower() not in ('.html', '.htm', '.xhtml', '.txt', '.csv', '.json', '.xml', '.log', '.svg'):
                    raise _Fatal(f'server returned a web page (Content-Type {_ct}), not the file - link expired or wrong')
                resp_skip = 0
                if r.status == 206:
                    cr = r.getheader('Content-Range') or ''
                    m = re.match(r'bytes\s+(\d+)-(\d+)/(\d+|\*)$', cr.strip())
                    if not m or int(m.group(1)) != s + done:
                        raise _Fatal(f'bad Content-Range {cr!r} (expected start {s+done}) - file changed on server')
                    if TOTAL_SIZE and m.group(3) != '*' and int(m.group(3)) != TOTAL_SIZE:
                        raise _Fatal(f'size changed: server says {m.group(3)}, expected {TOTAL_SIZE} - file changed, re-download')
                else:  # 200
                    if bounded and s + done > 0:
                        if _ifrange:
                            if not VALIDATOR_BROKEN:
                                log(f'chunk{idx} via {ip}: server replied 200 (If-Range mismatch) - disabling If-Range for this download')
                                VALIDATOR_BROKEN = True
                        elif RANGE_VERIFIED:
                            raise _NoRange(ip or 'default')
                        else:
                            raise _Fatal('server ignored Range and re-sent from byte 0 - cannot write mid-file')
                        drop_conn(url, ip)
                        continue
                    if _ifrange:
                        if not VALIDATOR_BROKEN:
                            log(f'chunk{idx} via {ip}: 200 on first byte (If-Range mismatch) - disabling If-Range for this download')
                            VALIDATOR_BROKEN = True
                        drop_conn(url, ip)
                        continue
                    resp_skip = done
                # NB: do NOT reset retries here - a fresh 206 with zero
                # follow-up bytes must count toward give-up, or a stalled
                # range loops forever (build 17 stall-test finding).
                last_recv = time.time()
                win_t, win_n = last_recv, 0
                resp0 = done
                while (done < total) if bounded else True:
                    if STOP:
                        raise _Stop()
                    if PAUSED:
                        _sleep_stop(0.3)
                        last_recv = time.time()   # pause is not a stall - don't trip the watchdog
                        with THR_LOCK:
                            THR_LAST = time.time()   # paused time must not earn throttle tokens
                        continue
                    with CHUNK_LOCK:
                        if bounded and CHUNK_OWNER.get(idx) != tok:
                            raise _Stolen()
                        lim = CHUNK_END.get(idx, e) if bounded else None
                    if bounded and s + done > lim:
                        break
                    now_c = time.time()
                    if now_c - last_recv > STALL_SECS:
                        drop_conn(url, ip)
                        raise RuntimeError(f'stalled {STALL_SECS}s with zero bytes, release for steal')
                    if now_c - win_t > STALL_SECS and win_n < 32 * 1024:
                        drop_conn(url, ip)
                        raise RuntimeError(f'trickle stall ({win_n}B in {STALL_SECS}s), release for steal')
                    try:
                        blk = r.read(64 * 1024)
                    except socket.timeout as ex:
                        drop_conn(url, ip)
                        raise RuntimeError(f'read timeout after {SOCK_TIMEOUT}s idle ({ex})')
                    if not blk:
                        if resp_skip and bounded:
                            raise _Fatal(f'server stream ended early after skipping {resp_skip}B')
                        break
                    if resp_skip:
                        if resp_skip >= len(blk):
                            resp_skip -= len(blk)
                            last_recv = time.time()
                            continue
                        blk = blk[resp_skip:]
                        resp_skip = 0
                    os.lseek(fd, s + done, os.SEEK_SET)
                    mv = memoryview(blk)
                    while mv:
                        n = os.write(fd, mv)
                        mv = mv[n:]
                    done += len(blk)
                    _throttle(len(blk))
                    LAST_PROGRESS = time.time()
                    last_recv = time.time()
                    retries = 0  # progress resets the give-up counter
                    win_n += len(blk)
                    if last_recv - win_t > STALL_SECS and win_n >= 32 * 1024:
                        win_t, win_n = last_recv, 0
                    with TOTAL_DONE_LOCK:
                        TOTAL_DONE += len(blk)
                    if SPEEDTEST and TOTAL_DONE >= SPEEDTEST:
                        raise _SpeedtestDone()
                    with G429_LOCK:
                        G429_N = 0
                    with LINK_LOCK:
                        LINK_DONE[ip] = LINK_DONE.get(ip, 0) + len(blk)
                    with CHUNK_LOCK:
                        CHUNK_POS[idx] = s + done
                    now = time.time()
                    if now - last >= 5:
                        el = now - t0
                        avg = done / el if el else 0
                        with LINK_LOCK:
                            old = LINK_SPEED.get(ip, 0.0)
                            LINK_SPEED[ip] = avg if not old else old * 0.7 + avg * 0.3
                        log(f'chunk{idx} via {ip}: {done/1e6:.1f}/{total/1e6:.1f}MB @ {done/el/1e6:.2f} MB/s')
                        last = now
                if not bounded:
                    if done == 0:
                        raise RuntimeError('server sent 0 bytes for the whole download - empty body')
                    break
                if done == resp0:
                    # 206 response with zero follow-up bytes and no error:
                    # count it or a premature-EOF range loops forever.
                    retries += 1
                    log(f'chunk{idx} via {ip}: empty response, retry {retries}')
                    if retries > 4: raise RuntimeError(f'chunk{idx} via {ip} gave up after {retries} empty responses, release for steal')
                    drop_conn(url, ip)   # wedged conn - always retry on a fresh one
                    _sleep_stop(1 + retries * 0.3)
            except (RuntimeError, OSError, http.client.HTTPException, TimeoutError, ConnectionError) as ex:
                drop_conn(url, ip)
                retries += 1
                log(f'chunk{idx} via {ip}: err {ex} retry {retries} resumed {done/1e6:.1f}/{total/1e6:.1f}MB')
                if retries % 3 == 0: refresh_final()
                if retries > 4: raise RuntimeError(f'chunk{idx} via {ip} gave up after {retries}, release for steal ({done/1e6:.1f}/{total/1e6:.1f}MB)')
                _sleep_stop(1 + retries * 0.3)
    finally:
        os.close(fd)
        with SOCK_LOCK:
            SOCK_REG.pop(tok, None)
    el = time.time() - t0
    if bounded and s + done <= e:
        with CHUNK_LOCK:
            mine = CHUNK_OWNER.get(idx) == tok
        if mine:
            # Still ours but the outer loop ended with an unfinished chunk.
            # Never assume theft: give up explicitly so the chunk re-queues
            # from its current offset instead of wedging state='ip' in limbo.
            raise RuntimeError(f'chunk{idx} via {ip} ended at {done}/{total} bytes with no error - releasing for steal')
        raise _Stolen()
    if not bounded:
        total = done
    avg = total / el if el else 0
    perf_write({'type': 'chunk', 'host': G_HOST, 'ip': ip, 'mb': round(total / 1e6, 1),
                'sec': round(el, 1), 'mbps': round(avg / 1e6, 2), 'outcome': 'done'})
    log(f'chunk{idx} via {ip} DONE: {total/1e6:.1f}MB in {el:.1f}s avg {total/el/1e6:.2f} MB/s')
    return idx

def watchdog():
    global STOP, STOP_REASON
    while True:
        time.sleep(5)
        if STOP or RUN_DONE.is_set():
            return
        now = time.time()
        with CHUNK_LOCK:
            if NCH > 0 and CHUNKS_DONE >= NCH:
                return
        if now - T0 > WATCHDOG_SECS and now - LAST_PROGRESS > WATCHDOG_SECS:
            STOP = True
            STOP_REASON = 'WATCHDOG'
            log(f'WATCHDOG: no progress for {int(now-LAST_PROGRESS)}s - forcing stop instead of hanging')
            abort_sockets()
            return

def monitor():
    last_t = time.time()
    while True:
        if STOP or RUN_DONE.is_set():
            break
        with CHUNK_LOCK:
            fin = NCH > 0 and CHUNKS_DONE >= NCH
        if fin:
            break
        time.sleep(0.5)
        now = time.time()
        if now - last_t < 5:
            continue
        last_t = now
        with TOTAL_DONE_LOCK:
            d = TOTAL_DONE
        el = now - T0
        tot = TOTAL_SIZE
        d = min(d, tot) if tot else d
        pct = f'{d/tot*100:.1f}%' if tot else ''
        log(f'TOTAL: {d/1e6:.1f}/{tot/1e6:.1f}MB {pct} @ {d/el/1e6:.2f} MB/s el {el:.0f}s')
        with CHUNK_LOCK:
            fin = NCH > 0 and CHUNKS_DONE >= NCH
        if fin: break

def simple_dash(out):
    ema = 0.0
    with TOTAL_DONE_LOCK: prev = TOTAL_DONE
    with LINK_LOCK: prev_links = dict(LINK_DONE)
    prev_t = time.time()
    try:
        while True:
            if STOP or RUN_DONE.is_set():
                break
            time.sleep(0.5)
            with TOTAL_DONE_LOCK: tot = TOTAL_DONE
            now = time.time(); dt = now - prev_t or 1e-6
            inst = (tot - prev) / dt
            ema = inst if ema == 0 else ema * 0.7 + inst * 0.3
            prev, prev_t = tot, now
            with LINK_LOCK: cur = dict(LINK_DONE)
            el = now - T0
            tsize = TOTAL_SIZE if TOTAL_SIZE else 0
            tshow = min(tot, tsize) if tsize else tot
            pct = f'{tshow/tsize*100:.1f}%' if tsize else ''
            with CHUNK_LOCK:
                ndone = sum(1 for v in CHUNK_STATE.values() if v == 'done')
            parts = []
            _lshort = {}
            for _n, _ip in enumerate(list(LINK_THREADS.keys())):
                _lshort[_ip] = f'L{_n+1}'
            for ip in list(LINK_THREADS.keys()):
                v = (cur.get(ip, 0) - prev_links.get(ip, 0)) / dt
                e = DASH_EMA.get(ip, 0.0)
                e = v if e == 0 else e * 0.7 + v * 0.3
                DASH_EMA[ip] = e
                parts.append(f'{_lshort[ip]}:{e/1e6:.1f}')
            prev_links = cur
            _savg = tot / el if el else 0
            line = (f'\r{tshow/1e6:.0f}/{tsize/1e6:.0f}MB {pct} {CINV(f" {ema/1e6:.2f}MB/s ")} '
                    f'[{"|".join(parts)}] {ndone}/{NCH} {int(el)//60:02d}:{int(el)%60:02d} avg {_savg/1e6:.2f}   ')
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except Exception:
                return
            with CHUNK_LOCK:
                fin = NCH > 0 and CHUNKS_DONE >= NCH
            if fin:
                break
    finally:
        try:
            sys.stdout.write('\n')
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stdout.write(f'downloaded at: {os.path.abspath(out)}\n')
            sys.stdout.flush()
        except Exception:
            pass

def ticker(out):
    pf = out + '.progress'
    mf = out + '.resume'
    while True:
        time.sleep(1)
        if STOP or RUN_DONE.is_set():
            break
        try:
            with TOTAL_DONE_LOCK:
                d = TOTAL_DONE
            with open(pf, 'w') as f: f.write(f'{d} {time.time()}')
        except Exception:
            pass
        if NCH > 1:
            try:
                with CHUNK_LOCK:
                    done_list = [i for i in range(NCH) if CHUNK_STATE.get(i) == 'done']
                tmp = mf + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump({'url': ORIG_URL, 'size': TOTAL_SIZE, 'chunks': NCH,
                               'done': done_list, 'etag': V_ETAG, 'lastmod': V_LM,
                               'build': BUILD}, f)
                os.replace(tmp, mf)
            except Exception:
                pass
        with CHUNK_LOCK:
            fin = NCH > 0 and CHUNKS_DONE >= NCH
        if fin:
            break

_ANSI = re.compile(r'\x1b\[[0-9;]*m')
_USE_C = os.environ.get('NO_COLOR') is None

def _tty():
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False

def _col(code, s):
    return f'\x1b[{code}m{s}\x1b[0m' if (_USE_C and _tty()) else s

def CYES(s): return _col('1;38;2;166;227;161', s)
def CNO(s): return _col('1;38;2;243;139;168', s)
def CYLW(s): return _col('1;38;2;249;226;175', s)
def CDIM(s): return _col('2', s)
def CINV(s): return _col('7;1', s)

def _cell(s, width):
    vis = _ANSI.sub('', s)
    if len(vis) > width:
        return vis[:width]
    return s + ' ' * (width - len(vis))

def dash(out, use, nchunks):
    if os.name == 'nt':
        try: os.system('')
        except Exception: pass
    C = {'rst': '\x1b[0m', 'bld': '\x1b[1m', 'dim': '\x1b[2m',
         'cyan': '\x1b[38;2;180;190;254m', 'grn': '\x1b[38;2;166;227;161m',
         'ylw': '\x1b[38;2;249;226;175m', 'red': '\x1b[38;2;243;139;168m',
         'sky': '\x1b[38;2;137;220;235m', 'peach': '\x1b[38;2;250;179;135m',
         'pink': '\x1b[38;2;245;194;231m', 'teal': '\x1b[38;2;148;226;213m',
         'inv': '\x1b[7m', 'box': '\x1b[38;2;203;166;247m'}
    if os.environ.get('NO_COLOR') is not None:
        C = {k: '' for k in C}
    uni = os.environ.get('MULTILINK_ASCII') != '1'
    if uni:
        try:
            '╭─╮│╰╯├┤█░●'.encode(sys.stdout.encoding or 'utf-8')
        except Exception:
            uni = False
    G = ({'tl': '╭', 'tr': '╮', 'bl': '╰', 'br': '╯', 'h': '─', 'v': '│',
          'lt': '├', 'rt': '┤', 'full': '█', 'empty': '░', 'dot': '●'}
         if uni else
         {'tl': '+', 'tr': '+', 'bl': '+', 'br': '+', 'h': '-', 'v': '|',
          'lt': '+', 'rt': '+', 'full': '#', 'empty': '-', 'dot': '*'})
    try:
        import shutil as _sh
        _tw = _sh.get_terminal_size().columns
    except Exception:
        _tw = 108
    W = min(max(_tw - 2, 50), 124)
    top = C['box'] + G['tl'] + G['h'] * W + G['tr'] + C['rst']
    mid = C['box'] + G['lt'] + G['h'] * W + G['rt'] + C['rst']
    bot = C['box'] + G['bl'] + G['h'] * W + G['br'] + C['rst']

    def row(s):
        return C['box'] + G['v'] + C['rst'] + ' ' + _cell(s, W - 2) + ' ' + C['box'] + G['v'] + C['rst']

    def meter(frac, w, col=None):
        f = max(0.0, min(1.0, frac))
        n = int(w * f)
        c = col or C['grn']
        return c + G['full'] * n + C['dim'] + G['empty'] * (w - n) + C['rst']

    ema_tot = 0.0
    with TOTAL_DONE_LOCK: prev_tot = TOTAL_DONE
    with LINK_LOCK: prev_links = dict(LINK_DONE)
    prev_t = time.time()
    name = os.path.basename(out)
    if len(name) > 34: name = name[:33] + '…'
    sys.stdout.write('\x1b[?25l')
    try:
        while True:
            time.sleep(0.2)
            if STOP or RUN_DONE.is_set():
                break
            with TOTAL_DONE_LOCK: tot = TOTAL_DONE
            if TOTAL_SIZE: tot = min(tot, TOTAL_SIZE)
            now = time.time(); dt = now - prev_t or 1e-6
            inst = (tot - prev_tot) / dt
            ema_tot = inst if ema_tot == 0 else ema_tot * 0.7 + inst * 0.3
            prev_tot, prev_t = tot, now
            SPARK.append(ema_tot)
            with LINK_LOCK: cur = dict(LINK_DONE)
            el = now - T0
            pct = tot / TOTAL_SIZE if TOTAL_SIZE else 0
            eta = (TOTAL_SIZE - tot) / ema_tot if ema_tot > 0 and TOTAL_SIZE else 0
            etas = f'{eta/60:.0f}m{eta%60:02.0f}s' if ema_tot > 0 else '--'
            _pc = pct * 100
            _pcc = C['red'] if _pc < 30 else (C['ylw'] if _pc < 70 else C['grn'])
            _LCMAP = {}
            _LCC = ['sky', 'peach', 'pink', 'teal', 'cyan', 'ylw']
            for _ni, _nip in enumerate(list(use)):
                _LCMAP[_nip] = _LCC[_ni % len(_LCC)]
            L = [top,
                 row(f"{C['bld']}{C['cyan']}BANDWIDTHGOBLIN v{VERSION} b{BUILD}{_VER_LABEL}{C['rst']}  {C['dim']}{name}{C['rst']}  {_pcc}{C['bld']}{_pc:5.1f}%{C['rst']}"),
                 row(f"{C['inv']}{C['bld']}  ▼ {ema_tot/1e6:6.2f} MB/s  {C['rst']}  {C['dim']}{tot/1e6:,.1f}/{TOTAL_SIZE/1e6:,.1f} MB  ETA {etas}  el {int(el)//60:02d}:{int(el)%60:02d}{C['rst']}"),
                 row(f"{meter(pct, W - 14, _pcc)} {_pcc}{C['bld']}{_pc:4.1f}%{C['rst']}"),
                 mid]
            mx = 1e-6
            spd = {}
            for ip in list(use):
                v = (cur.get(ip, 0) - prev_links.get(ip, 0)) / dt
                e = DASH_EMA.get(ip, 0.0)
                e = v if e == 0 else e * 0.7 + v * 0.3
                DASH_EMA[ip] = e
                spd[ip] = e
                mx = max(mx, e)
            for ip in list(use):
                parked = not ip_ok(ip)
                is_new = ip in LINK_JOINED and (time.time() - LINK_JOINED.get(ip, 0)) < 60
                if parked:
                    dot = C['red'] + G['dot'] + C['rst']
                    st = C['red'] + 'OFF' + C['rst']
                elif is_new:
                    dot = C['cyan'] + G['dot'] + C['rst']
                    st = C['cyan'] + 'NEW' + C['rst']
                else:
                    dot = C['grn'] + G['dot'] + C['rst']
                    st = C['grn'] + 'ON' + C['rst']
                _lc = C.get(_LCMAP.get(ip, 'cyan'), C['cyan'])
                _sv = spd[ip] / 1e6
                _sc = C['grn'] if _sv >= 2 else (C['ylw'] if _sv >= 0.5 else C['red'])
                _bw = max(6, min(16, W // 9))
                _lbl = link_label(ip)
                L.append(row(f"{dot} {_lc}{_lbl:<22}{C['rst']} {meter(spd[ip]/mx, _bw, _sc)} {C['bld']}{_sc}{_sv:6.2f}{C['rst']} MB/s {cur.get(ip,0)/1e6:8.1f}MB x{LINK_THREADS.get(ip,1)} {st}"))
            prev_links = cur
            with CHUNK_LOCK:
                states = [CHUNK_STATE.get(i) for i in range(nchunks)]
                ndone = sum(1 for s in states if s == 'done')
            plain = ''.join('#' if s == 'done' else ('>' if s not in (None, '.') else '.') for s in states)
            plain = plain[:max(0, W - 34)]
            cells = ''.join((C['grn'] + '#' + C['rst']) if c == '#' else ((C['ylw'] + '>' + C['rst']) if c == '>' else (C['dim'] + '.' + C['rst'])) for c in plain)
            _sw = int(el)
            _sws = f'{_sw//3600:02d}:{_sw%3600//60:02d}:{_sw%60:02d}'
            L.append(row(f"chunks {C['bld']}{ndone}/{nchunks}{C['rst']} {cells} {C['dim']}{_sws}{C['rst']}"))
            _sp = list(SPARK)
            if _sp:
                _mx = max(_sp) or 1e-6
                _gl = ' ▁▂▃▄▅▆▇█'
                _w = max(8, min(42, W - 34))
                _vals = _sp
                if len(_vals) > _w:
                    _step = len(_vals) / _w
                    _vals = [_vals[int(i * _step)] for i in range(_w)]
                _spark = ''.join(_gl[min(8, int(v / _mx * 8))] for v in _vals)
                _avg = tot / el if el else 0
                _avgc = C['grn'] if _avg >= 1e6 else (C['ylw'] if _avg >= 3e5 else C['red'])
                L.append(row(f"speed {C['dim']}{_spark}{C['rst']}  avg {_avgc}{C['bld']}{_avg/1e6:5.2f}{C['rst']} MB/s"))
            L.append(bot)
            for l in list(RING)[-4:]:
                L.append(C['dim'] + ' ' + l[-70:] + C['rst'])
            L.append(C['dim'] + ' [ESC] stop   ' + os.path.basename(out) + '.log' + C['rst'])
            try:
                import shutil as _sh
                try:
                    _tw = _sh.get_terminal_size().columns
                except Exception:
                    _tw = 120
                _fr = []
                for _ln in L:
                    _v = _ANSI.sub('', _ln)
                    _fr.append(_v[:max(0, _tw)] if len(_v) > _tw else _ln)
                sys.stdout.write('\x1b[H' + '\n'.join(_fr) + '\x1b[J')
                sys.stdout.flush()
            except Exception as ex:
                global DASH_ON
                DASH_ON = False
                try:
                    sys.stdout.write('\x1b[?25h\n')
                    sys.stdout.flush()
                except Exception:
                    pass
                log(f'dashboard disabled, plain logs: {ex}')
                break
            with CHUNK_LOCK:
                fin = NCH > 0 and CHUNKS_DONE >= NCH
            if fin:
                _dp = os.path.abspath(out)
                if len(_dp) > 54:
                    _dp = '...' + _dp[-51:]
                _dl = [top,
                       row(f"{C['grn']}{C['bld']}DONE{C['rst']}  {os.path.basename(out)}"),
                       row(f"downloaded at: {C['cyan']}{_dp}{C['rst']}"),
                       row(f"{tot/1e6:,.1f} MB in {int(el)//60}m{int(el)%60:02d}s  avg {tot/max(el,1e-6)/1e6:.2f} MB/s"),
                       bot]
                try:
                    sys.stdout.write('\x1b[H' + '\n'.join(_dl) + '\x1b[J')
                    sys.stdout.flush()
                    time.sleep(8)
                except Exception:
                    pass
                break
    finally:
        sys.stdout.write('\x1b[?25h\n')
        sys.stdout.flush()

def auto_tune():
    import time as _t
    last = {}
    while not STOP and not RUN_DONE.is_set():
        _t.sleep(20)
        if STOP or RUN_DONE.is_set():
            break
        if PAUSED:
            continue
        with STAT_LOCK:
            cur = dict(STAT429)
        with G_LOCK:
            caps = dict(WORKER_CAP)
        with LINK_LOCK:
            speeds = dict(LINK_SPEED)
        best_spd = max(speeds.values()) if speeds else 0
        for ip, cap in caps.items():
            w = cur.get(ip, 0) - last.get(ip, 0)
            if w == 0 and cap < 4:
                spd = speeds.get(ip, 0)
                if best_spd >= 1e6 and spd and spd < max(1e6, best_spd / 2):
                    log(f'auto-tune {(ip or "link")}: hold at {cap} (slow {spd/1e6:.2f} MB/s vs best {best_spd/1e6:.2f})')
                    continue
                with G_LOCK:
                    WORKER_CAP[ip] = cap + 1
                    LINK_THREADS[ip] = cap + 1
                log(f'auto-tune {(ip or "link")}: workers -> {cap + 1} (no throttling)')
            elif w > 15 and cap > 1:
                with G_LOCK:
                    WORKER_CAP[ip] = cap - 1
                    LINK_THREADS[ip] = cap - 1
                log(f'auto-tune {(ip or "link")}: workers -> {cap - 1} (throttled x{w})')
        last = cur
        with CHUNK_LOCK:
            if NCH > 0 and CHUNKS_DONE >= NCH:
                return

def _active_inc(ip):
    with G_LOCK:
        ACTIVE[ip] = ACTIVE.get(ip, 0) + 1

def _active_dec(ip):
    with G_LOCK:
        ACTIVE[ip] = max(0, ACTIVE.get(ip, 0) - 1)

def try_steal(out, ip, q):
    global CHUNKS_DONE
    if PAUSED:
        return False
    with STEAL_LOCK:
        now0 = time.time()
        old_tok = None
        _sn = 0
        with CHUNK_LOCK:
            best, best_rem = None, STEAL_MIN
            for idx, st in CHUNK_STATE.items():
                if st in (None, '.', 'done'):
                    continue
                if CHUNK_STEAL_N.get(idx, 0) >= 8:
                    continue
                if now0 - CHUNK_STEALTIME.get(idx, 0) < 15:
                    continue
                s, e = CHUNK_RANGE.get(idx, (0, 0))
                if not isinstance(e, int):
                    continue
                pos = max(CHUNK_POS.get(idx, s), s)
                if e - pos > best_rem:
                    best, best_rem = idx, e - pos
            if best is None:
                return False
            s, e = CHUNK_RANGE[best]
            pos = max(CHUNK_POS.get(best, s), s)
            if pos > e:
                return False
            old_tok = CHUNK_OWNER.get(best)
            tok = (ip, time.time())
            CHUNK_OWNER[best] = tok
            # The new owner runs pos..e in full. The old owner aborts via the
            # CHUNK_OWNER check, so it never sees this value again (build 21:
            # setting pos-1 here made EVERY steal break instantly on the
            # stealer's `s+done > lim` check and silently re-queue ~5s later).
            CHUNK_END[best] = e
            CHUNK_STATE[best] = ip
            CHUNK_STEALTIME[best] = now0
            _sn = CHUNK_STEAL_N.get(best, 0) + 1
            CHUNK_STEAL_N[best] = _sn
        # unblock the victim's stalled read so it notices the steal promptly
        if old_tok is not None:
            with SOCK_LOCK:
                vc = SOCK_REG.pop(old_tok, None)
            if vc is not None:
                try: vc.close()
                except Exception: pass
    _active_inc(ip)
    if _sn >= 8:
        log(f'chunk{best} stole 8x - giving it a quiet window (steal cap)')
    log(f'stealing chunk{best} remainder {(e-pos+1)/1e6:.1f}MB via {ip} (steal #{_sn})')
    try:
        fetch_direct(best, pos, e, ip, out, tok)
    except _Stop:
        _active_dec(ip)
        raise
    except _Stolen:
        _active_dec(ip)
        return True
    except _Fatal:
        _active_dec(ip)
        raise
    except _NoRange:
        _active_dec(ip)
        raise
    except Exception as ex:
        ip_failed(ip)
        _active_dec(ip)
        with CHUNK_LOCK:
            if CHUNK_STATE.get(best) == 'done':
                return True
            CHUNK_RANGE[best] = (pos, e)
            CHUNK_STATE[best] = '.'
        q.put((best, pos, e))
        log(f'steal of chunk{best} via {ip} failed ({ex}), re-queued remainder')
        return True
    ip_success(ip)
    _active_dec(ip)
    with CHUNK_LOCK:
        if CHUNK_STATE.get(best) != 'done':
            CHUNK_STATE[best] = 'done'
            CHUNKS_DONE += 1
    with CHUNK_TRIES_LOCK:
        CHUNK_TRIES.pop(best, None)
    CHUNK_STEAL_N.pop(best, None)
    log(f'steal of chunk{best} via {ip} delivered {(e-pos+1)/1e6:.1f}MB')
    return True

def membership(q, out, per_link, use, ex, rid):
    import time as _t
    while not STOP and not RUN_DONE.is_set() and G_RUNID == rid:
        _t.sleep(2)
        if STOP or RUN_DONE.is_set() or G_RUNID != rid:
            break
        if NCH <= 1:
            return
        with CHUNK_LOCK:
            if NCH > 0 and CHUNKS_DONE >= NCH and q.empty():
                return
        try:
            cur = {ip for _, ip in local_ips()}
        except Exception:
            continue
        with G_LOCK:
            have = set(u for u in use if u)
        for ip in sorted(cur - have):
            with IP_LOCK:
                _st0 = IP_STATE.get(ip) or {}
            if _st0.get('norange') and _st0.get('parked_until', 0) > time.time():
                continue
            try:
                _ok_new, _ = range_probe(get_final(), ip, TOTAL_SIZE)
            except Exception:
                _ok_new = False
            if not _ok_new:
                log(CNO(f'new link {ip} does not serve ranges - not joining multi-link'))
                continue
            use.append(ip)
            with G_LOCK:
                LINK_THREADS[ip] = per_link
            LINK_JOINED[ip] = time.time()
            with G_LOCK:
                if ip not in G_STARTED:
                    G_STARTED.add(ip)
                    for _ in range(per_link):
                        try:
                            G_FUTS.append(ex.submit(worker, out, ip, q))
                        except Exception as ex2:
                            log(f'could not start worker for new link {ip}: {ex2}')
                            break
            with IP_LOCK:
                st = IP_STATE.get(ip)
                if st:
                    st['fails'] = 0
                    st['parked_until'] = 0
                    st['gone'] = False
                    st['norange'] = False
            log(CYES(f'new link joined: {link_label(ip)}'))
        for ip in sorted(have - cur):
            with IP_LOCK:
                st = IP_STATE.setdefault(ip, {'fails': 0, 'parked_until': 0})
                st['parked_until'] = time.time() + 3600
                st['fails'] = 0
                st['gone'] = True
            orph = []
            with CHUNK_LOCK:
                for idx, stt in CHUNK_STATE.items():
                    if stt == ip:
                        s, e = CHUNK_RANGE.get(idx, (0, 0))
                        if isinstance(e, int):
                            CHUNK_OWNER[idx] = ('gone', time.time())
                            CHUNK_END[idx] = s - 1
                            CHUNK_STATE[idx] = '.'
                            orph.append((idx, s, e))
            for (idx, s, e) in orph:
                q.put((idx, s, e))
            log(CNO(f'link lost: {ip} - its chunks re-queued to healthy links'))
        for ip in sorted(have & cur):
            with IP_LOCK:
                st = IP_STATE.get(ip)
                if st and st.get('gone'):
                    st['gone'] = False
                    st['fails'] = 0
                    st['parked_until'] = 0
                    log(CYES(f'link back: {ip}'))

def worker(out, ip, q):
    global CHUNKS_DONE
    import queue as _q
    while True:
        if STOP or RUN_DONE.is_set():
            return
        if PAUSED:
            _sleep_stop(0.3)
            continue
        with G_LOCK:
            capped = ACTIVE.get(ip, 0) >= WORKER_CAP.get(ip, 99)
        if capped:
            time.sleep(2)
            continue
        if not ip_ok(ip):
            time.sleep(5)
            with CHUNK_LOCK:
                if NCH > 0 and CHUNKS_DONE >= NCH:
                    return
            continue
        try:
            idx, s, e = q.get_nowait()
        except _q.Empty:
            global STEAL_MIN
            try:
                with G_LOCK:
                    _tot_w = sum(LINK_THREADS.values())
                if q.qsize() <= max(1, _tot_w):
                    if STEAL_MIN != (1 << 20):
                        STEAL_MIN = 1 << 20
                        log('tail phase: steal threshold -> 1MB, slow tails get help')
            except Exception:
                pass
            try:
                if try_steal(out, ip, q):
                    continue
            except _Stop:
                raise
            except _Fatal:
                raise
            time.sleep(1)
            with CHUNK_LOCK:
                ndone = sum(1 for v in CHUNK_STATE.values() if v == 'done')
            if ndone >= NCH and q.empty():
                return
            continue
        tok = (ip, 'q', time.time())
        with CHUNK_LOCK:
            CHUNK_STATE[idx] = ip
            CHUNK_RANGE[idx] = (s, e)
            CHUNK_POS[idx] = s
            CHUNK_END[idx] = e
            CHUNK_OWNER[idx] = tok
        with G_LOCK:
            ACTIVE[ip] = ACTIVE.get(ip, 0) + 1
        try:
            fetch_direct(idx, s, e, ip, out, tok)
            with CHUNK_LOCK:
                _aud = (CHUNK_POS.get(idx, s) >= e + 1) if isinstance(e, int) else True
            if not _aud:
                raise RuntimeError(f'chunk{idx} ended before its byte range ({CHUNK_POS.get(idx, s)} < {e+1})')
            ip_success(ip)
            with CHUNK_LOCK:
                if CHUNK_STATE.get(idx) != 'done':
                    CHUNK_STATE[idx] = 'done'
                    CHUNKS_DONE += 1
            with CHUNK_TRIES_LOCK:
                CHUNK_TRIES.pop(idx, None)
        except _Stop:
            raise
        except _SpeedtestDone:
            # speedtest cap reached: a control-flow signal, never a transfer
            # failure. Let it reach the runner so the measurement stops
            # cleanly instead of churning (build 37, found in real-world
            # tests - the worker was logging it as 'failed ()' and re-queuing).
            raise
        except _Stolen:
            continue
        except _Fatal:
            raise
        except _NoRange:
            raise
        except Exception as ex:
            ip_failed(ip)
            nxt_s = s
            with CHUNK_LOCK:
                CHUNK_STATE[idx] = '.'
                # re-queue from ACTUAL progress so part-downloaded bytes are
                # kept, not re-fetched from byte 0 (build 21)
                nxt_s = max(CHUNK_POS.get(idx, s), s)
                CHUNK_RANGE[idx] = (nxt_s, e)
            with CHUNK_TRIES_LOCK:
                n = CHUNK_TRIES.get(idx, 0) + 1
                CHUNK_TRIES[idx] = n
            if n >= 12:
                log(f'chunk{idx} FAILED {n}x, giving up: {ex}')
                perf_write({'type': 'chunk', 'host': G_HOST, 'ip': ip,
                            'mb': round(((e - s + 1) / 1e6), 1) if isinstance(e, int) else 0,
                            'sec': 0, 'mbps': 0, 'outcome': 'failed', 'err': str(ex)[:120]})
                raise
            log(f'chunk{idx} via {ip} failed ({ex}), re-queue {n}x from {nxt_s} for steal by any link')
            q.put((idx, nxt_s, e))
        finally:
            _active_dec(ip)
            try: q.task_done()
            except ValueError: pass

def notify(title, msg):
    """Real desktop toast, fire-and-forget: the downloader must NEVER wait on an
    OS notification. Build 29: the old helper ran `powershell -File` *synchronously*
    with a Start-Sleep 7 inside, so every successful run hung ~7-9s after DONE.
    Now a detached `-EncodedCommand` powershell (no temp file, no blocking) shows
    a ~3s balloon and disappears."""
    try:
        if os.name == 'nt':
            t = (title or '').replace("'", "''")
            m = (msg or '').replace("'", "''")
            body = ("Add-Type -AssemblyName System.Windows.Forms\n"
                    "$n = New-Object System.Windows.Forms.NotifyIcon\n"
                    "$n.Icon = [System.Drawing.SystemIcons]::Information\n"
                    "$n.Visible = $true\n"
                    f"$n.ShowBalloonTip(4000, '{t}', '{m}', 'Info')\n"
                    "Start-Sleep 3\n"
                    "$n.Dispose()\n")
            _enc = base64.b64encode(body.encode('utf-16-le')).decode('ascii')
            subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-EncodedCommand', _enc],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=(getattr(subprocess, 'DETACHED_PROCESS', 0)
                                            | getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
        elif sys.platform == 'darwin':
            def _osa(s):
                return (s or '').replace('\\', '\\\\').replace('"', '\\"')
            subprocess.Popen(['osascript', '-e',
                              'display notification "' + _osa(msg) + '" with title "' + _osa(title) + '"'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(['notify-send', title or '', msg or ''],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def get_clipboard():
    t = ''
    try:
        if os.name == 'nt':
            t = subprocess.check_output(['powershell', '-NoProfile', '-Command', 'Get-Clipboard'],
                                        text=True, errors='ignore', timeout=10)
        elif sys.platform == 'darwin':
            t = subprocess.check_output(['pbpaste'], text=True, errors='ignore', timeout=10)
        else:
            for cmd in (['xclip', '-o', '-selection', 'clipboard'],
                        ['xclip', '-o'], ['xsel', '--clipboard', '--output']):
                try:
                    t = subprocess.check_output(cmd, text=True, errors='ignore', timeout=10)
                    if t:
                        break
                except Exception:
                    continue
    except Exception:
        t = ''
    return (t or '').strip()

def is_link(s):
    try:
        u = urllib.parse.urlparse(s or '')
        h = u.hostname or ''
        if u.scheme not in ('http', 'https') or not h:
            return False
        if '.' in h or h == 'localhost':
            return True
        # bare IPv4 (LAN testing against a local server)
        return re.match(r'^\d{1,3}(\.\d{1,3}){3}$', h) is not None
    except Exception:
        return False

_LINK_RE = re.compile(r'https?://[^\s\'"<>]+')

def extract_links(t):
    out = []
    for m in _LINK_RE.findall(t or ''):
        u = m.rstrip('.,;)]}')
        if is_link(u) and u not in out:
            out.append(u)
    return out

def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return '.'

def _perf_path():
    try:
        return os.path.join(_script_dir(), 'perf.jsonl')
    except Exception:
        return 'perf.jsonl'

def perf_write(rec):
    try:
        rec = dict(rec)
        rec['t'] = time.time()
        rec['build'] = BUILD
        with PERF_LOCK:
            p = _perf_path()
            if os.path.exists(p):
                try:
                    if os.path.getsize(p) > 2_000_000:
                        with open(p, 'r', errors='ignore') as f:
                            lines = f.readlines()
                        tmp = p + '.tmp'
                        with open(tmp, 'w') as f:
                            f.writelines(lines[-1000:])
                        os.replace(tmp, p)
                except Exception:
                    pass
            with open(p, 'a') as f:
                f.write(json.dumps(rec) + '\n')
    except Exception:
        pass

def seed_speeds():
    try:
        p = _perf_path()
        if not os.path.exists(p):
            return
        data = b''
        try:
            with open(p, 'rb') as f:
                f.seek(max(0, os.path.getsize(p) - 262144))
                data = f.read()
        except Exception:
            pass
        txt = data.decode('utf-8', errors='ignore')
        if data and txt and not txt.startswith('{'):
            nl = txt.find('\n')
            if nl >= 0:
                txt = txt[nl + 1:]
        agg = {}
        for line in txt.splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            ip = r.get('ip')
            mb = r.get('mb', 0)
            if not ip or not isinstance(mb, (int, float)) or mb <= 0:
                continue
            if r.get('type') == 'chunk' and r.get('outcome', 'done') == 'done' and r.get('mbps', 0) > 0:
                agg.setdefault(ip, []).append(r['mbps'])
        if not agg:
            return
        with LINK_LOCK:
            for ip, v in agg.items():
                LINK_SPEED[ip] = sum(v) / len(v)
        log('learned from past runs: ' + ', '.join(f'{ip}~{sum(v)/len(v):.1f}MB/s over {len(v)} chunks' for ip, v in agg.items()))
    except Exception:
        pass

def load_lastdir():
    try:
        with open(os.path.join(_script_dir(), '.lastdir')) as f:
            d = f.read().strip()
            if d and os.path.isdir(d):
                return d
    except Exception:
        pass
    return ''

def save_lastdir(d):
    try:
        if d and os.path.isdir(d):
            with open(os.path.join(_script_dir(), '.lastdir'), 'w') as f:
                f.write(d)
    except Exception:
        pass

def acquire_urls():
    while True:
        clip = get_clipboard()
        cl = extract_links(clip)
        if cl:
            first = cl[0] if len(cl[0]) <= 80 else cl[0][:80] + '...'
            extra = f' (+{len(cl)-1} more)' if len(cl) > 1 else ''
            v = ask(f'clipboard link:\n  {first}{extra}\n[Enter] use / [p] paste different / [q] quit: ')
            if v.lower() == 'q':
                return None
            if v.lower() in ('p', 'paste', 'n', 'no'):
                v2 = ask('paste link(s) (or q to quit): ')
                if (v2 or '').lower() == 'q' or not v2:
                    return None
                links = extract_links(v2)
                if links:
                    return links
                print(CNO('no link found - please copy link(s) and paste them.'))
                continue
            if extract_links(v):
                return extract_links(v)
            if v:
                print(CNO('that is not a link - please copy a link and paste it.'))
                continue
            return cl
        else:
            if clip:
                print(CYLW('clipboard has no link in it.'))
            v = ask('paste download link(s) (or q to quit): ')
            if (v or '').lower() == 'q' or not v:
                return None
            links = extract_links(v)
            if links:
                return links
            print(CNO('no link found - please copy a link (starts with http) and paste it.'))

def reset_state():
    global LOGFILE, T0, ORIG_URL, FINAL_URL, TOTAL_SIZE, TOTAL_DONE, DASH_ON, STOP, DASH_MODE
    global LINK_DONE, CHUNK_STATE, REDIR, IP_STATE, DASH_EMA, CHUNK_TRIES, G_RUNID, LINK_JOINED, G_FUTS, G_STARTED, CHUNK_STEALTIME, ACTIVE, WORKER_CAP, STAT429, STEAL_MIN, G429_N, G429_COOLDOWN
    global STOP_REASON, LAST_PROGRESS, V_ETAG, V_LM, RANGE_VERIFIED, EXPECT_SHA, REFRESH_NOOP, CHUNK_STEAL_N, RETRY_SINGLE
    global _NEIGH_T, PAUSED, LINK_SPEED
    global VALIDATOR_BROKEN
    # Build 30 audit fixes: a paused run must not leak into the next one (the
    # next download would start frozen forever - every worker stalls on
    # PAUSED), and history-learned speeds must not ghost across runs.
    PAUSED = False
    LINK_SPEED = {}
    STOP_REASON = 'ESC'; RUN_DONE.set(); LAST_PROGRESS = 0.0
    _LABEL_CACHE.clear(); _LABEL_FINED.clear(); _PTR_CACHE.clear(); _NEIGH_T = 0.0
    V_ETAG = ''; V_LM = ''; RANGE_VERIFIED = False; EXPECT_SHA = ''
    VALIDATOR_BROKEN = False
    REFRESH_NOOP = 0; CHUNK_STEAL_N = {}; RETRY_SINGLE = False
    LOGFILE = None; T0 = time.time(); ORIG_URL = ''; FINAL_URL = ''
    TOTAL_SIZE = 0; TOTAL_DONE = 0; DASH_ON = False; STOP = False
    DASH_MODE = 'off'
    LINK_DONE = {}; CHUNK_STATE = {}; REDIR = {}; IP_STATE = {}
    DASH_EMA = {}; CHUNK_TRIES = {}; LINK_JOINED = {}; CHUNK_STEALTIME = {}
    ACTIVE = {}; WORKER_CAP = {}; STAT429 = {}
    LINK_THREADS.clear()   # stale per-link thread counts must not leak into the next run
    G429_N = 0; G429_COOLDOWN = 0.0
    STEAL_MIN = 8 << 20
    try:
        with _CONN_LOCK:
            for _c in list(_CONN_CACHE.values()):
                try: _c.close()
                except Exception: pass
            _CONN_CACHE.clear()
    except Exception:
        pass
    G_FUTS = []; G_STARTED = set()
    G_RUNID += 1
    try: SPARK.clear()
    except Exception: pass

def ask(prompt, default=''):
    if _USE_C and _tty():
        def _paint(m):
            inner = m.group(1)
            inner = re.sub(r'(?i)\b(enter|y|yes)\b', lambda x: CYES(x.group(0)), inner)
            inner = re.sub(r'(?i)\b(n|no|q|quit)\b', lambda x: CNO(x.group(0)), inner)
            return '[' + inner + ']'
        prompt = re.sub(r'\[([^\]]*)\]', _paint, prompt)
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ''
    return v or default

def guess_name(url):
    try:
        p = urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]
        p = urllib.parse.unquote(p)
        if p and p.lower() not in ('videoplayback', 'download', 'get', 'file'):
            return p
    except Exception:
        pass
    return ''

def _load_perf():
    recs = []
    try:
        p = _perf_path()
        if os.path.exists(p):
            for line in open(p, errors='ignore'):
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return recs

def verify_file(out, size):
    try:
        got = os.path.getsize(out)
        if got <= 0:
            return False, 'zero-byte file'
        if size and got != size:
            return False, f'size mismatch got={got} expected={size}'
        with open(out, 'rb') as f:
            headb = f.read(512)
            ext = os.path.splitext(out)[1].lower()
            if ext not in ('.html', '.htm', '.xhtml', '.txt', '.csv', '.json', '.xml', '.log', '.svg') and b'<html' in headb[:256].lower():
                return False, 'looks like an HTML page, not the file'
            if EXPECT_SHA:
                h = hashlib.sha256()
                h.update(headb)
                while True:
                    b = f.read(1 << 20)
                    if not b: break
                    h.update(b)
                got_sha = h.hexdigest()
                if got_sha != EXPECT_SHA:
                    return False, f'sha256 mismatch (got {got_sha[:12]}...)'
        if ext == '.zip' and not headb.startswith(b'PK\x03\x04'):
            return False, 'not a zip (bad header)'
        if ext == '.exe' and not headb.startswith(b'MZ'):
            return False, 'not an exe (bad header)'
        if ext in ('.webm', '.mkv') and not headb.startswith(b'\x1a\x45\xdf\xa3'):
            return False, 'not a webm/mkv (bad EBML header)'
        if ext in ('.mp4', '.m4v', '.mov') and b'ftyp' not in headb[3:9]:
            return False, 'not an mp4 (no ftyp)'
        return True, 'size + header intact' + (' + sha256 verified' if EXPECT_SHA else '')
    except Exception as ex:
        return False, f'verify error: {ex}'

def print_report(host=''):
    recs = _load_perf()
    if host:
        recs = [r for r in recs if host.lower() in (r.get('host') or '').lower()]
    chunks = [r for r in recs if r.get('type') == 'chunk' and r.get('outcome', 'done') == 'done' and r.get('mbps', 0) > 0]
    if not chunks:
        print('no performance data yet - finish a download first.')
        return
    from collections import defaultdict
    by_ip = defaultdict(lambda: [0, 0.0, 0.0, 0])
    for r in chunks:
        a = by_ip[r.get('ip') or '?']
        a[0] += 1; a[1] += r.get('mb', 0); a[2] += r.get('sec', 0); a[3] = max(a[3], r.get('mbps', 0))
    print(f'--- per connection, per chunk ({len(chunks)} chunks) ---')
    for ip, (n, mb, sec, best) in sorted(by_ip.items()):
        print(f'  {ip:<16} chunks:{n:<4} MB:{mb:<10.1f} avg:{mb/max(sec,1e-6):5.2f} MB/s  best:{best:5.2f} MB/s')
    print('--- runs (newest last) ---')
    for r in [x for x in recs if x.get('type') == 'run'][-10:]:
        print(f'  {(r.get("host") or "?")[:40]:<42} {r.get("mb_total",0):.0f}MB in {r.get("sec",0):.0f}s = {r.get("mbps_avg",0):.2f} MB/s  links:{len(r.get("links",[]))}')
    print('--- tune advice ---')
    print('  throttled hosts (many 429s in logs): 16 1 | fast CDNs: 16 3 | default: 16 auto')

def print_eta(host, mb):
    try:
        mb = float(mb)
    except Exception:
        print('usage: bandwidthgoblin.py --eta <host-part> <MB>')
        return
    recs = [r for r in _load_perf() if r.get('type') == 'chunk' and r.get('outcome', 'done') == 'done'
            and host.lower() in (r.get('host') or '').lower() and r.get('mbps', 0) > 0]
    from collections import defaultdict
    by_ip = defaultdict(list)
    for r in recs:
        by_ip[r.get('ip') or '?'].append(r['mbps'])
    rates = {ip: sum(v) / len(v) for ip, v in by_ip.items() if len(v) >= 2}
    if not rates:
        print('no history for that host - downloading once will teach it.')
        return
    tot = sum(rates.values())
    print(f'--- min-time plan for {mb:.0f} MB on {host} ---')
    for ip, r8 in sorted(rates.items(), key=lambda x: -x[1]):
        share = r8 / tot
        print(f'  {ip or "?":<16} {r8:5.2f} MB/s  share {share*100:4.1f}%  (~{mb*share:.0f} MB, ETA {mb*share/max(r8,1e-6):.0f}s alone)')
    print(f'  combined {tot:.2f} MB/s -> ETA {mb/max(tot,1e-6):.0f}s ({mb/max(tot,1e-6)/60:.1f} min)')

def _human(n):
    try:
        n = float(n or 0)
    except Exception:
        return '0 B'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'

_VALUE_FLAGS = ('queue', 'out-dir', 'referer', 'cookie', 'sha', 'header', 'limit', 'links', 'jobs', 'verify')

def _parse_flags(tokens):
    """Split new-style --flags out of a token list.
    Returns ({flag: [values...] or bool}, remaining_positionals).
    Value flags accept both --k=v and --k v. --report/--eta/--version are
    handled by main() before this runs, so unknown --flags pass through."""
    flags = {}
    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith('--') and len(t) > 2:
            body = t[2:]
            k = body.split('=', 1)[0].lower()
            has_eq = '=' in body
            if k in _VALUE_FLAGS:
                if has_eq:
                    v = body.split('=', 1)[1]
                elif i + 1 < len(tokens) and not tokens[i + 1].startswith('--'):
                    i += 1
                    v = tokens[i]
                else:
                    v = ''
                flags.setdefault(k, []).append(v)
            elif k in ('open', 'no-open'):
                flags['open'] = (k == 'open')
            elif k in ('dry-run', 'info', 'history', 'resume', 'quiet', 'no-log', 'speedtest', 'selftest'):
                flags[k] = True
            else:
                out.append(t)
        else:
            out.append(t)
        i += 1
    return flags, out

def _read_queue(path):
    """One entry per line -> [(url, out_or_None), ...].
    Lines:  URL           -> output name guessed from the URL
            URL  OUT      -> explicit output name or path (rest of the line)
    Blank lines and #comments skipped; anything that isn't a link is skipped."""
    items = []
    try:
        with open(path, errors='ignore') as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                parts = s.split(None, 1)
                u = parts[0]
                if not is_link(u):
                    print(CDIM(f'  skipping non-link: {u[:60]}'))
                    continue
                items.append((u, parts[1].strip() if len(parts) > 1 and parts[1].strip() else None))
    except Exception as ex:
        print(CNO(f'cannot read queue {path}: {ex}'))
    return items

def _sidecar_sha(out):
    """Auto-discover sha256 from <file>.sha256 or SHA256SUMS next to the target."""
    dirn = os.path.dirname(os.path.abspath(out)) or '.'
    base = os.path.basename(out)
    for f in (os.path.join(dirn, base + '.sha256'), os.path.join(dirn, 'SHA256SUMS')):
        try:
            with open(f, errors='ignore') as fh:
                for line in fh:
                    m = re.search(r'\b([0-9a-fA-F]{64})\b', line)
                    if not m:
                        continue
                    if f.lower().endswith('sha256sums') and base.lower() not in line.lower():
                        continue
                    return m.group(1).lower()
        except Exception:
            pass
    return ''

def _probe_print(url):
    """--dry-run: report everything about a link without downloading it."""
    print(f'--- {redact(url)} ---')
    try:
        final = follow_redirects(url, None)
        u, size, ar, meta = head(url)
    except Exception as ex:
        print(CNO(f'unreachable: {ex}') + CYLW('\nTip: signed/token URLs expire - copy a fresh link.'))
        return
    if final and final != url:
        print(f'final     : {redact(final)}')
    print(f'size      : {_human(size)}' + ('' if size else ' (unknown)'))
    print(f'type      : {meta.get("ctype") or "?"}  encoding: {meta.get("enc") or "none"}')
    if ar and 'bytes' in (ar or '').lower():
        print('ranges    : ' + CYES('bytes - multi-chunk possible'))
        if size and size > 1:
            try:
                ok = range_probe(u, None, size)[0]
                print('206 probe : ' + (CYES('verified') if ok else CNO('server ignores ranges - single stream')))
            except Exception as ex:
                print('206 probe : ' + CNO(f'check failed ({ex})'))
    else:
        print('ranges    : none - single stream')
    h = urllib.parse.urlparse(u).hostname or ''
    recs = _load_perf()
    chunks = [r for r in recs if r.get('type') == 'chunk' and h.lower() in (r.get('host') or '').lower()
              and r.get('outcome', 'done') == 'done' and r.get('mbps', 0) > 0]
    if chunks:
        from collections import defaultdict
        by_ip = defaultdict(list)
        for r in chunks:
            by_ip[r.get('ip') or '?'].append(r['mbps'])
        learned = ', '.join(f'{ip}~{sum(v)/len(v):.1f}MB/s' for ip, v in by_ip.items())
        print(f'learned   : {learned}')
    else:
        print('learned   : (nothing yet - one download teaches this host)')

def _hist_path():
    global HIST_PATH
    if HIST_PATH is None:
        HIST_PATH = os.path.join(_script_dir(), 'history.jsonl')
    return HIST_PATH

def _hist_write(rec):
    try:
        rec = dict(rec)
        rec['t'] = time.strftime('%Y-%m-%d %H:%M:%S')
        with PERF_LOCK:
            with open(_hist_path(), 'a') as f:
                f.write(json.dumps(rec) + '\n')
    except Exception:
        pass

def _print_history(n=10):
    p = _hist_path()
    if not os.path.exists(p):
        print(CDIM('no history yet - completed downloads will be recorded as history.jsonl.'))
        return
    lines = []
    try:
        with open(p, errors='ignore') as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        lines.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    if not lines:
        print(CDIM('no history yet.'))
        return
    print(f'last {min(n, len(lines))} download(s):')
    for r in lines[-n:]:
        when = (r.get('t') or '')[:16]
        out = (r.get('out') or '?')[:42]
        mb = r.get('mb', 0)
        mbps = r.get('mbps', 0)
        tag = CYES('OK  ') if r.get('outcome', 'done') == 'done' else CNO('FAIL')
        host = r.get('host') or '?'
        print(f'  {tag} {when}  {out:<42} {mb:>7.1f} MB  {mbps:>5.2f} MB/s  {host}')

def _pick_resume():
    """Newest valid <out>.resume in the last-used folder + cwd -> (path, mtime, manifest)."""
    best = None
    for d in list(dict.fromkeys([load_lastdir() or '', '.'])):
        try:
            for fn in os.listdir(d):
                if not fn.endswith('.resume'):
                    continue
                p = os.path.join(d, fn)
                try:
                    m = json.load(open(p))
                    if not (m.get('url') and m.get('chunks') and is_link(m.get('url', ''))):
                        continue
                    mt = os.path.getmtime(p)
                    if best is None or mt > best[1]:
                        best = (p, mt, m)
                except Exception:
                    continue
        except Exception:
            continue
    return best

def _usage():
    return ('usage:\n'
            '  bandwidthgoblin.py                         interactive (paste a link after prompt)\n'
            '  bandwidthgoblin.py URL OUT [16 auto]       direct download\n'
            '  bandwidthgoblin.py --queue list.txt [--out-dir D:\\dl]\n'
            '                                      batch - one URL per line in the file\n'
            '  bandwidthgoblin.py URL --out-dir D:\\dl     save straight into a folder\n'
            '  bandwidthgoblin.py --dry-run URL ...       probe links: size, type, ranges, learned speed\n'
            '  bandwidthgoblin.py --sha <sha256> URL OUT  deep-verify against a hash\n'
            '  bandwidthgoblin.py --history               show the last 10 completed downloads\n'
            '  bandwidthgoblin.py --resume                continue the newest saved .resume download\n'
            '  bandwidthgoblin.py --speedtest URL         measure each link + combined speed (8 MB cap)\n'
            '  bandwidthgoblin.py --help                  this text\n\n'
            'flags (any position, also env):\n'
            '  --queue FILE   --out-dir DIR   --dry-run\n'
            '  --sha HEX (=BANDWIDTHGOBLIN_SHA256)  --referer URL (=BANDWIDTHGOBLIN_REFERER)\n'
            '  --cookie STR  (=BANDWIDTHGOBLIN_COOKIE)  --header "Name: value" (repeatable, =\n'
            '  BANDWIDTHGOBLIN_HEADERS "a: 1; b: 2")  --limit MB/s (aggregate cap, =BANDWIDTHGOBLIN_LIMIT)\n'
            '  --links 0,1   pick which interfaces to combine: indices or IPs\n'
            '                 (e.g. --links 0,1 = WiFi + LAN, =MULTILINK_IFACES)\n'
            '  --open / --no-open   --history   --resume   --speedtest\n'
            '  --quiet (milestones only)   --no-log (no disk logs)\n'
            '  --jobs N   download a --queue in parallel (1-8 files at once);\n'
            '             each queue line may be:  URL  or  URL  output-name.ext\n'
            '  --verify <manifest|dir>  check files against sha256sum checksums\n'
            '             (sha256sum-format file, or a dir with SHA256SUMS / *.sha256)\n'
            '  --selftest run the built-in offline QA (local ranged server, 2 links, sha)\n'
            '  env also: MULTILINK_IFACES=0,1  MULTILINK_NODASH=1  BANDWIDTHGOBLIN_SPEEDTEST_MB=8')

def _subrun(url, out, rest):
    """Download ONE queue entry in its own python process (used by --jobs > 1).
    Isolated process = zero shared-global risk between parallel runs; the child
    inherits this environment (BANDWIDTHGOBLIN_* vars, MULTILINK_IFACES, ...) and gets
    plain logs (no dashboard) so parallel output stays readable. Exit code is
    meaningful: 0 = file saved, 1 = hard failure."""
    args = [sys.executable, os.path.abspath(__file__), url, out] + list(rest)
    env = dict(os.environ)
    env['MULTILINK_NODASH'] = '1'
    try:
        p = subprocess.run(args, env=env)
        return p.returncode
    except Exception as ex:
        print(CNO(f'launching job for {os.path.basename(out)} failed: {ex}'), flush=True)
        return 1

def _batch_run(items, fl, eflags, jobs):
    """Run queue entries in parallel, each in its own process. Output names:
    explicit line OUT (absolute, or relative to --out-dir), else the guessed
    name, else download_<n>.bin; duplicates get _2/_3 suffixes."""
    od = (fl.get('out-dir') or [''])[0] or load_lastdir() or '.'
    try:
        os.makedirs(od, exist_ok=True)
        save_lastdir(od)
    except Exception:
        pass
    rest = ['16', 'auto'] + eflags
    used, plan = set(), []
    for i, (url, out) in enumerate(items, 1):
        if out and os.path.isabs(out):
            name = out
        elif out:
            name = os.path.join(od, out)
        else:
            nm = guess_name(url) or f'download_{i}.bin'
            name = os.path.join(od, nm)
        base = os.path.basename(name)
        if base in used:
            stem, ext = os.path.splitext(base)
            k = 2
            while f'{stem}_{k}{ext}' in used:
                k += 1
            base = f'{stem}_{k}{ext}'
            name = os.path.join(os.path.dirname(name) or od, base)
        used.add(os.path.basename(name))
        plan.append((url, name))
    bad = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_subrun, u, o, rest): o for u, o in plan}
        for f in concurrent.futures.as_completed(futs):
            o = futs[f]
            rc = f.result()
            print(f'[{os.path.basename(o)}] exit {rc}', flush=True)
            if rc != 0:
                bad.append(os.path.basename(o))
    print(f'batch: {len(plan) - len(bad)}/{len(plan)} ok'
          + (f', failed: {", ".join(bad)}' if bad else ''), flush=True)
    if bad:
        sys.exit(1)

def _read_manifest(path):
    """sha256sum-format manifest -> [(hex, filename), ...].
    Lines:  <64-hex>  <relative-path>.  Blank/# lines skipped; lines that do
    not look like checksums are ignored so sidecar/extra text stays harmless."""
    recs = []
    try:
        for line in open(path, errors='ignore'):
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            parts = s.split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 64 or not re.match(r'^[0-9a-fA-F]{64}$', parts[0]):
                continue
            recs.append((parts[0].lower(), parts[1].strip()))
    except Exception as ex:
        print(CNO(f'cannot read manifest {path}: {ex}'))
    return recs

def _verify_cmd(path):
    """--verify <manifest|dir>: check files against stored sha256 checksums.
    manifest = sha256sum-format text; dir = SHA256SUMS file, else every
    file.sha256 sidecar. Prints per-file PASS/FAIL. Exit 1 on any failure."""
    if os.path.isdir(path):
        base = path
        man = os.path.join(path, 'SHA256SUMS')
        recs = _read_manifest(man) if os.path.isfile(man) else []
        if not recs:
            for f in sorted(os.listdir(path)):
                if f.endswith('.sha256'):
                    try:
                        h = open(os.path.join(path, f), errors='ignore').read().strip().split()[0]
                        if re.match(r'^[0-9a-fA-F]{64}$', h):
                            recs.append((h.lower(), f[:-7]))
                    except Exception:
                        pass
    else:
        recs = _read_manifest(path)
        base = os.path.dirname(os.path.abspath(path))
    if not recs:
        print(CNO(f'verify: no checksums found in {path}'))
        return 1
    print(f'verifying {len(recs)} file(s) against {base}')
    bad = 0
    for hexd, name in recs:
        full = name if os.path.isabs(name) else os.path.join(base, name)
        if not os.path.isfile(full):
            print(f'FAIL {name}  (missing)')
            bad += 1
            continue
        h = hashlib.sha256()
        with open(full, 'rb') as f:
            for blk in iter(lambda: f.read(1 << 20), b''):
                h.update(blk)
        ok = h.hexdigest() == hexd
        print(f'{"PASS" if ok else "FAIL"} {name}  ' + ('OK' if ok else 'sha256 mismatch'))
        if not ok:
            bad += 1
    print(f'verify: {len(recs) - bad}/{len(recs)} ok')
    return 1 if bad else 0

def _selftest():
    """Built-in offline QA (--selftest): spins up a local ThreadingHTTPServer
    serving a random 4 MiB payload with real Byte-Range support, then runs a
    genuine multi-link download over two loopback links (127.0.0.2/127.0.0.3),
    with sha256 verification. Exercises ranges, chunking, combining, checking
    and cleanup end-to-end. Uses only stdlib; reaches no external network."""
    import http.server, tempfile
    data = os.urandom(4 * 1024 * 1024)
    dsha = hashlib.sha256(data).hexdigest()
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def _ok(self, code, n, extra=None):
            self.send_response(code)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(n))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', '"selftest"')
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
        def do_HEAD(self):
            self._ok(200, len(data))
        def do_GET(self):
            rng = self.headers.get('Range')
            if rng and rng.startswith('bytes='):
                a_s, b_s = rng[6:].split(',')[0].split('-', 1)
                a = int(a_s) if a_s else 0
                b = int(b_s) if b_s else len(data) - 1
                b = min(b, len(data) - 1)
                self._ok(206, b - a + 1, {'Content-Range': f'bytes {a}-{b}/{len(data)}'})
                try: self.wfile.write(data[a:b + 1])
                except Exception: pass
            else:
                self._ok(200, len(data))
                try: self.wfile.write(data)
                except Exception: pass
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _H)
    srv.handle_error = lambda *a: None
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = os.path.join(tempfile.gettempdir(), f'lb_selftest_{os.getpid()}.bin')
    try:
        for f in (out, out + '.resume', out + '.progress'):
            try: os.remove(f)
            except OSError: pass
    except Exception:
        pass
    global local_ips, notify, OPEN_AFTER, QUIET, NO_LOG
    _saved = (local_ips, os.environ.get('MULTILINK_IFACES'),
              os.environ.get('BANDWIDTHGOBLIN_SHA256'), notify, OPEN_AFTER, QUIET, NO_LOG)
    local_ips = lambda: [('selftest-a', '127.0.0.2'), ('selftest-b', '127.0.0.3')]
    os.environ.pop('MULTILINK_IFACES', None)
    os.environ['BANDWIDTHGOBLIN_SHA256'] = dsha
    notify = lambda *a, **kw: None
    OPEN_AFTER = False
    QUIET = True
    NO_LOG = True
    try:
        reset_state()
        res = run_one(f'http://127.0.0.1:{port}/selftest.bin', out, ['4', '2'])
    finally:
        local_ips = _saved[0]
        if _saved[1] is not None:
            os.environ['MULTILINK_IFACES'] = _saved[1]
        else:
            os.environ.pop('MULTILINK_IFACES', None)
        if _saved[2] is not None:
            os.environ['BANDWIDTHGOBLIN_SHA256'] = _saved[2]
        else:
            os.environ.pop('BANDWIDTHGOBLIN_SHA256', None)
        notify, OPEN_AFTER, QUIET, NO_LOG = _saved[3], _saved[4], _saved[5], _saved[6]
        srv.shutdown()
        srv.server_close()
    ok = False
    try:
        if res == 'ok' and os.path.isfile(out):
            ok = hashlib.sha256(open(out, 'rb').read()).hexdigest() == dsha
    except Exception:
        ok = False
    try:
        for f in (out, out + '.resume', out + '.progress'):
            if os.path.exists(f): os.remove(f)
    except Exception:
        pass
    print((CYES('selftest PASS: 4 MiB over 2 links, ranged, sha256-intact')
            if ok else CNO('selftest FAIL')), flush=True)
    return 0 if ok else 1

def main():
    global STOP, OPEN_AFTER, QUIET, NO_LOG
    _install_crash_hooks()
    raw = sys.argv[1:]
    _utility = any(a in raw for a in ('--verify', '--selftest', '--history', '--dry-run',
                                      '--info', '--speedtest', '--version', '-V'))
    if '--quiet' not in raw and not QUIET and not _utility:
        print(f'👺 BandwidthGoblin v{VERSION} build {BUILD}{_VER_LABEL} - one file, all your links.', flush=True)
    if raw and raw[0] in ('--version', '-V', 'version'):
        print(f'👺 BandwidthGoblin v{VERSION} build {BUILD}{_VER_LABEL}')
        return
    if raw and raw[0] in ('--report', '-r', 'report'):
        print_report(raw[1] if len(raw) > 1 else '')
        return
    if len(raw) >= 3 and raw[0] in ('--eta', 'eta'):
        print_eta(raw[1], raw[2])
        return
    if raw and raw[0] in ('--help', '-h', 'help'):
        print(_usage())
        return
    fl, args = _parse_flags(raw)
    eflags = []
    for _k in ('referer', 'cookie', 'sha', 'header', 'limit', 'links'):
        for _v in (fl.get(_k) or []):
            eflags.append(f'--{_k}={_v}')
    if 'open' in fl:
        OPEN_AFTER = bool(fl['open'])
    if fl.get('quiet'):
        QUIET = True
    if fl.get('no-log'):
        NO_LOG = True
    if fl.get('selftest'):
        sys.exit(_selftest())
    if fl.get('verify'):
        sys.exit(_verify_cmd((fl.get('verify') or [''])[0]))
    que = (fl.get('queue') or [''])[0]
    qitems = _read_queue(que) if que else []
    qurls = [u for u, _o in qitems]
    if que and not qitems:
        print(CNO(f'no links found in {que} (each line: URL, optionally followed by an output name).'))
        return
    if qitems:
        print(f'queue ({len(qitems)} entries): {redact(qitems[0][0])}' + (f' ... +{len(qitems)-1} more' if len(qitems) > 1 else ''))
    jobs = 1
    try:
        jobs = max(1, min(int((fl.get('jobs') or ['1'])[0]), 8))
    except (ValueError, TypeError):
        jobs = 1
    if jobs > 1 and qitems:
        print(CYLW(f'parallel jobs: {jobs} - each entry downloads in its own process'))
        _batch_run(qitems, fl, eflags, jobs)
        return
    if fl.get('history'):
        _print_history(10)
        return
    if fl.get('resume'):
        _rb = _pick_resume()
        if not _rb:
            print(CNO('no resume files found (looked in the last-used folder + cwd).'))
            return
        _rm = _rb[2]
        _rurl = _rm['url']
        _rout = _rb[0][:-len('.resume')]
        print(f'resuming {os.path.basename(_rout)} ({_rm["chunks"]} chunks)')
        reset_state()
        res = run_one(_rurl, _rout, [str(_rm['chunks']), '1'] + eflags)
        while res == 'retry-single':
            reset_state()
            res = run_one(_rurl, _rout, ['1', '1'] + eflags)
        return
    if fl.get('speedtest'):
        targets = [a for a in args if is_link(a)] or list(qurls or [])
        if not targets:
            print(CNO('--speedtest needs a link to measure against.') + CDIM('\n' + _usage()))
            return
        import tempfile
        os.environ['BANDWIDTHGOBLIN_SPEEDTEST_MB'] = os.environ.get('BANDWIDTHGOBLIN_SPEEDTEST_MB') or '8'
        for t in targets:
            reset_state()
            run_one(t, os.path.join(tempfile.gettempdir(), 'bg_speedtest.bin'), ['8', 'auto'] + eflags)
        return
    if fl.get('dry-run') or fl.get('info'):
        targets = [a for a in args if is_link(a)] or list(qurls or [])
        if not targets:
            print(CNO('--dry-run needs a link (or --queue with links).') + CDIM('\n' + _usage()))
            return
        for t in targets:
            _probe_print(t)
        return
    if len(args) >= 2 and is_link(args[0]) and not is_link(args[1]):
        # One-shot form: URL then output FILE. If the second token is itself a
        # link (e.g. two --out-dir URLs), that is a batch, not an output name.
        reset_state()
        res = run_one(args[0], args[1], args[2:] + eflags)
        while res == 'retry-single':
            reset_state()
            res = run_one(args[0], args[1], ['1', '1'] + eflags)
        # Build 32: meaningful exit code for one-shot CLI runs - scripts and
        # --jobs children rely on 0 = file saved, 1 = hard failure.
        sys.exit(0 if res != 'error' else 1)
        return
    pending = [a for a in args if not str(a).startswith('--')]
    _qout = {u: o for u, o in qitems if o}
    if qitems:
        pending = [u for u, _o in qitems] + pending
    headless = bool(que or fl.get('out-dir'))
    _dl_n = 0   # build 30: disambiguate files whose URL carries no filename
    _used = set()
    _hfails = 0
    while True:
        reset_state()
        if pending and is_link(pending[0]):
            url = pending.pop(0)
        elif pending:
            print(CNO(f'not a link: {pending.pop(0)[:60]} - copy a link and paste it.'))
            continue
        else:
            if headless:
                break
            _rb = _pick_resume()
            if _rb:
                _q = ask(f'resume available: {os.path.basename(_rb[0])[:-7]} (chunks {_rb[2].get("chunks")}) - [r] resume / [Enter] new: ')
                if _q.strip().lower().startswith('r'):
                    url = _rb[2]['url']
                    out = _rb[0][:-len('.resume')]
                    rest = [str(_rb[2].get('chunks', 16)), '1'] + eflags
                    try:
                        res = run_one(url, out, rest)
                    except Exception as ex:
                        STOP = True; RUN_DONE.set()
                        print(CNO(f'Crashed: {ex}'), flush=True)
                        res = None
                    if res == 'retry-single':
                        log('single-stream retry after multi-link failure')
                        reset_state()
                        run_one(url, out, ['1', '1'] + eflags)
                    continue
            urls = acquire_urls()
            if not urls:
                print(CDIM('bye.'))
                return
            url, pending = urls[0], urls[1:] + pending
        if headless:
            od = (fl.get('out-dir') or [''])[0] or load_lastdir() or '.'
            try:
                os.makedirs(od, exist_ok=True)
                save_lastdir(od)
            except Exception:
                pass
            _qo = _qout.get(url)
            if _qo and os.path.isabs(_qo):
                out = _qo
            else:
                _nm = _qo or guess_name(url)
                if not _nm:
                    # Two queue entries can resolve to the same 'download.bin'
                    # (token URLs like http://host/?t=1 have no path name) and
                    # the second would truncate the first's output. Number them.
                    _dl_n += 1
                    _nm = f'download_{_dl_n}.bin'
                out = os.path.join(od, _nm)
            _bn = os.path.basename(out)
            if _bn in _used:
                _stem, _ext = os.path.splitext(_bn)
                _k = 2
                while f'{_stem}_{_k}{_ext}' in _used:
                    _k += 1
                _bn = f'{_stem}_{_k}{_ext}'
                out = os.path.join(os.path.dirname(out) or od, _bn)
            _used.add(os.path.basename(out))
            rest = ['16', 'auto'] + eflags
            try:
                res = run_one(url, out, rest)
            except Exception as ex:
                STOP = True; RUN_DONE.set()
                print(CNO(f'Crashed: {ex}'), flush=True)
                res = None
            if res == 'retry-single':
                log('single-stream retry after multi-link failure')
                reset_state()
                res = run_one(url, out, ['1', '1'] + eflags)
            if res == 'error' or res is None:
                _hfails += 1
            if pending:
                continue
            break
        base = load_lastdir()
        sug = os.path.join(base or '.', guess_name(url) or 'download.bin')
        while True:
            out = ask(f'save where? [Enter = {sug}]: ')
            if not out:
                out = sug
                break
            if is_link(out):
                print(CNO('That is a download link, not a save spot - type a folder or filename.'))
                continue
            if re.match(r'^[A-Za-z]:[\\/]*$', out.strip()):
                print(CNO('That is a drive root (like D:\\) - Windows blocks saving there.')
                      + CYLW(' Type a folder or filename inside it, e.g. D:\\Downloads\\file.'))
                continue
            break
        if os.path.dirname(os.path.abspath(out)) not in ('', '.'):
            save_lastdir(os.path.dirname(os.path.abspath(out)))
        tune = ask('chunks + threads/link? [Enter = 16 auto]: ')
        rest = (tune.split()[:2] or ['16', 'auto']) + eflags
        try:
            res = run_one(url, out, rest)
        except Exception as ex:
            import traceback
            STOP = True
            RUN_DONE.set()
            txt = traceback.format_exc()
            try:
                if LOGFILE:
                    with open(LOGFILE, 'a', errors='replace') as _f: _f.write(txt + '\n')
            except Exception:
                pass
            print(CNO(f'Crashed: {ex}') + CDIM(f'\nFull details saved to: {LOGFILE}'), flush=True)
            res = None
        if res == 'retry-single':
            log('single-stream retry after multi-link failure')
            reset_state()
            run_one(url, out, ['1', '1'] + eflags)
        try:
            tty = bool(sys.stdin.isatty())
        except Exception:
            tty = False
        if not tty:
            return
        nxt = ask('another? [paste link / y / N]: ')
        if is_link(nxt):
            pending = [nxt]
            continue
        if nxt.lower() in ('y', 'yes'):
            continue
        return
    if _hfails:
        sys.exit(1)

def run_one(url, out, rest):
    global LOGFILE, T0, ORIG_URL, FINAL_URL, TOTAL_SIZE, TOTAL_DONE, DASH_ON, DASH_MODE, NCH, CHUNKS_DONE, G_HOST
    global V_ETAG, V_LM, RANGE_VERIFIED, EXPECT_SHA, LAST_PROGRESS, RETRY_SINGLE, EXTRA_HEADERS, OPEN_AFTER, STOP_REASON, STOP
    global VALIDATOR_BROKEN
    import queue as _q, shutil
    _flg, rest = _parse_flags(list(rest))
    EXTRA_HEADERS = {}
    if _flg.get('referer') or os.environ.get('BANDWIDTHGOBLIN_REFERER'):
        EXTRA_HEADERS['Referer'] = (_flg.get('referer') or [os.environ['BANDWIDTHGOBLIN_REFERER']])[0]
    if _flg.get('cookie') or os.environ.get('BANDWIDTHGOBLIN_COOKIE'):
        EXTRA_HEADERS['Cookie'] = (_flg.get('cookie') or [os.environ['BANDWIDTHGOBLIN_COOKIE']])[0]
    for _hv in _flg.get('header', []) + (os.environ.get('BANDWIDTHGOBLIN_HEADERS') or '').split(';'):
        if ':' in _hv:
            _hk, _hvv = _hv.split(':', 1)
            _hk = _hk.strip()
            if _hk:
                EXTRA_HEADERS[_hk.title()] = _hvv.strip()
    if 'open' in _flg:
        OPEN_AFTER = bool(_flg['open'])
    _sha_flag = (_flg.get('sha') or [''])[0].strip().lower()
    if _flg.get('links'):
        # --links 0,1 (or bare IPs) selects which interfaces to combine; feed
        # it through the same env channel MULTILINK_IFACES already uses so
        # every code path (run_one, membership hot-plug) sees one selection.
        os.environ['MULTILINK_IFACES'] = (_flg.get('links') or [''])[0].strip()
    if _sha_flag:
        if len(_sha_flag) != 64 or not re.fullmatch(r'[0-9a-f]+', _sha_flag):
            print(CNO(f'ignoring bad --sha value (need 64 hex chars): {_sha_flag[:16]}...'), flush=True)
            _sha_flag = ''
    try:
        NCHUNKS = int(rest[0]) if len(rest) > 0 else 16
    except (ValueError, IndexError):
        NCHUNKS = 16
    # 0/negative chunks would wedge the run; thousands of chunks would turn
    # the dashboard's per-chunk render into a CPU blow-up. Clamp hard.
    NCHUNKS = max(1, min(NCHUNKS, 256))
    AUTO = False
    try:
        _r1 = rest[1] if len(rest) > 1 else 'auto'
        if isinstance(_r1, str) and _r1.lower() == 'auto':
            AUTO = True
            TPL = 1
        else:
            TPL = int(_r1)
    except (ValueError, IndexError):
        TPL = 4
    # 0 threads/link means zero workers and a dead run - never allow it.
    TPL = max(1, min(TPL, 16))
    ORIG_URL = url
    EXPECT_SHA = _sha_flag or (os.environ.get('BANDWIDTHGOBLIN_SHA256') or '').strip().lower()
    if os.path.isdir(out) or (not os.path.splitext(out)[1] and not os.path.exists(out)):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, guess_name(ORIG_URL) or 'download.bin')
    LOGDIR = os.path.join(_script_dir(), 'logs')
    try:
        os.makedirs(LOGDIR, exist_ok=True)
    except Exception:
        LOGDIR = os.path.dirname(os.path.abspath(out)) or '.'
    _base = re.sub(r'[^\w\-. ]', '_', os.path.basename(out)) or 'download.bin'
    LOGFILE = os.path.join(LOGDIR, _base + '.log')
    _n = 1
    while os.path.exists(LOGFILE) and _n < 1000:
        _n += 1
        LOGFILE = os.path.join(LOGDIR, f'{_base}.{_n}.log')
    if NO_LOG:
        LOGFILE = None
    else:
        open(LOGFILE, 'w', errors='replace').write(f'BandwidthGoblin v{VERSION} build {BUILD}{_VER_LABEL} | start {time.ctime()} url={redact(url)}\n')
        log(f'log file: {LOGFILE}')
    if EXTRA_HEADERS:
        log('extra headers set: ' + ', '.join(sorted(EXTRA_HEADERS)))
    if EXPECT_SHA:
        log('sha256 verification enabled via --sha/env')
    if not EXPECT_SHA:
        EXPECT_SHA = _sidecar_sha(out)
        if EXPECT_SHA:
            log(f'sha256 verification enabled via sidecar (next to {os.path.basename(out)})')
    global SPEEDLIMIT, THR_CAP, THR_LAST, SPEEDTEST
    SPEEDLIMIT = 0.0; THR_CAP = 0.0; THR_LAST = 0.0; SPEEDTEST = 0.0
    if _flg.get('limit') or os.environ.get('BANDWIDTHGOBLIN_LIMIT'):
        try:
            SPEEDLIMIT = max(0.0, float((_flg.get('limit') or [os.environ['BANDWIDTHGOBLIN_LIMIT']])[0]) * 1e6)
        except (ValueError, TypeError):
            log('ignoring bad --limit (need a number in MB/s, e.g. --limit=5)')
        else:
            log(f'speed limit: {SPEEDLIMIT/1e6:.1f} MB/s')
    if os.environ.get('BANDWIDTHGOBLIN_SPEEDTEST_MB'):
        try:
            SPEEDTEST = max(0.0, float(os.environ['BANDWIDTHGOBLIN_SPEEDTEST_MB']) * 1e6)
            log(f'speedtest: measuring up to {SPEEDTEST/1e6:.0f} MB via real chunks')
        except (ValueError, TypeError):
            pass
    _prune_logs(LOGDIR)   # keep logs/ tidy even when runs fail, not just on success
    T0 = time.time()
    LAST_PROGRESS = time.time()
    TOTAL_DONE = 0
    global _FH
    try:
        import faulthandler
        try:
            faulthandler.disable()
            if _FH is not None:
                try: _FH.close()
                except Exception: pass
        except Exception:
            pass
        if LOGFILE:
            _FH = open(LOGFILE, 'a', errors='replace')
            faulthandler.enable(file=_FH)
    except Exception:
        pass
    ifaces = local_ips()
    log(f'BandwidthGoblin v{VERSION} build {BUILD}{_VER_LABEL}')
    log('interfaces found:')
    for i, (name, ip) in enumerate(ifaces): log(f'  [{i}] {link_label(ip)}  ({name})')
    use = [ip for _, ip in ifaces]
    sel = os.environ.get('MULTILINK_IFACES', '').strip()
    if sel and len(use) > 1:
        try:
            # Accept interface indices ("0,1") or the IPs themselves
            # ("192.168.1.20,172.20.10.2") - build 31 usability for selecting
            # exactly which links to combine (e.g. WiFi + LAN, drop the phone).
            _parts = [x.strip() for x in sel.split(',') if x.strip()]
            _idxs = []
            for _x in _parts:
                if re.match(r'^\d+$', _x):
                    _idxs.append(int(_x))
                elif re.match(r'^\d{1,3}(\.\d{1,3}){3}$', _x):
                    for _i, (_n, _ip) in enumerate(ifaces):
                        if _ip == _x:
                            _idxs.append(_i)
                            break
            if _idxs:
                use = [ifaces[i][1] for i in _idxs if 0 <= i < len(ifaces)]
        except Exception as ex:
            log(f'bad --links/MULTILINK_IFACES={sel!r}: {ex}, using all')
    log(f'auto-using {len(use)} link(s): {", ".join(link_label(u) for u in use)} (ESC stops)')
    _real = [u for u in use if u]
    if len(_real) >= 2:
        _subs = {u[:u.rfind('.')] for u in _real if '.' in u}
        if _subs and len(_subs) < len(_real):
            log('note: some links share a subnet - if they go through the same router/ISP, '
                'combining helps reliability more than raw speed')
    # Build 32: prove the connections before claiming a combined run. A link
    # that cannot reach the download host (dead hotspot/WiFi with a stale IP,
    # VPN ghost) is dropped now with a loud warning, never silently ignored.
    if len([u for u in use if u]) >= 2:
        _reach = _probe_links([u for u in use if u], url)
        _dead = [u for u in use if u and u not in _reach]
        if _dead:
            use = [u for u in use if u in _reach or u is None]
            log('unreachable links dropped: ' + ', '.join(link_label(d) for d in _dead))
        _n = len([u for u in use if u])
        if _n < 2:
            print(CYLW('only 1 working connection - this download will run single-stream '
                       '(combining needs 2+ live links; check the Wi-Fi/hotspot).'), flush=True)
            log('only 1 working connection: single-stream mode')
        elif _dead:
            print(CYES(f'connected links: {_n} working - combining active'), flush=True)
    _warm_labels(list(use))
    seed_speeds()
    try:
        url, size, ar, meta = head(url)
    except Exception as ex:
        print(CNO(f'Link is broken or unreachable: {ex}') + CYLW('\nTip: signed/token URLs expire - copy a fresh link and paste it.'), flush=True)
        log(f'link unreachable: {ex}')
        return 'error'
    V_ETAG = meta.get('etag', '')
    V_LM = meta.get('lastmod', '')
    if meta.get('ctype') in ('text/html', 'application/xhtml+xml') and not size and os.path.splitext(out)[1].lower() not in ('.html', '.htm', '.xhtml', '.txt', '.csv', '.json', '.xml', '.log', '.svg'):
        print(CNO('Link returns a web page, not a file - copy the direct download link.'), flush=True)
        log('link returns a web page - aborting')
        return 'error'
    FINAL_URL = url
    try:
        G_HOST = urllib.parse.urlparse(url).hostname or ''
    except Exception:
        G_HOST = ''
    TOTAL_SIZE = size
    try:
        tty = bool(sys.stdout.isatty())
    except Exception:
        tty = False
    if not tty or os.environ.get('MULTILINK_NODASH') == '1':
        DASH_MODE = 'off'
    elif os.name != 'nt' or os.environ.get('WT_SESSION') or os.environ.get('TERM_PROGRAM') or os.environ.get('MULTILINK_FORCE_DASH') == '1':
        DASH_MODE = 'full'
    else:
        DASH_MODE = 'simple'
    DASH_ON = DASH_MODE == 'full'
    log(f'display: {DASH_MODE} (tty={tty})')
    log(f'final url: {redact(url)}')
    log(f'size: {size/1e6:.1f} MB, ranges: {ar}, etag: {V_ETAG[:28] or "-"}, links: {len(use)}, chunks: {NCHUNKS}, threads/link: {TPL}')
    ar_ok = bool(ar and 'bytes' in ar.lower())
    if size and ar_ok and len(use) >= 2 and NCHUNKS > 1:
        _ptot, _ok_ips = 0, []
        for _ip in list(use):
            try:
                _a, _b = range_probe(url, _ip, size)
            except Exception as ex:
                log(f'range probe {_ip or "default"}: {ex} - dropping this link from multi-link')
                continue
            if _a:
                _ok_ips.append(_ip)
                if _b and not _ptot:
                    _ptot = _b
            else:
                log(f'range probe {_ip or "default"}: sends 200, ignoring Range - dropping from multi-link')
        if _ok_ips:
            RANGE_VERIFIED = True
            use = _ok_ips
            if _ptot:
                size = _ptot
                TOTAL_SIZE = size
                log(f'range verified on {len(use)} link(s), total {size/1e6:.1f} MB')
            else:
                log(f'range verified on {len(use)} link(s): {", ".join(link_label(u) for u in use)}')
        else:
            ar_ok = False
            log('range probe: no link serves real 206 - single-stream mode for safety')
    if not size or not ar_ok or len(use) < 2:
        log('no range support or single link: downloading normally via default route')
        use = [None]; size = size or 0
        NCHUNKS = 1; TPL = 1
        TOTAL_SIZE = size
    pardir = os.path.dirname(os.path.abspath(out))
    if pardir:
        os.makedirs(pardir, exist_ok=True)
    if size:
        try:
            free = shutil.disk_usage(pardir or '.').free
        except Exception:
            free = None
        if free is not None and free < size:
            msg = f'Not enough disk space: need {size/1e6:.0f} MB, free {free/1e6:.0f} MB at {pardir}. Free space or pick another drive.'
            print(msg, flush=True)
            log(msg)
            return 'error'
    manifest = out + '.resume'
    skip = set()
    have_resume = False
    if os.path.exists(manifest) and os.path.exists(out) and size:
        try:
            m = json.load(open(manifest))
            same_file = (m.get('url') == ORIG_URL and m.get('size') == size
                         and m.get('chunks') == NCHUNKS)
            if m.get('vok') is not False:
                # Validators proven unstable in the earlier run (see VALIDATOR_BROKEN):
                # comparing them again would just reject a perfectly resumable file,
                # so they are only enforced when the previous run trusted them.
                if m.get('etag') and V_ETAG and not _same_val(m.get('etag'), V_ETAG):
                    same_file = False
                    log('resume rejected: file changed on server (ETag mismatch)')
                elif m.get('lastmod') and V_LM and not _same_val(m.get('lastmod'), V_LM):
                    same_file = False
                    log('resume rejected: file changed on server (Last-Modified mismatch)')
            if same_file and os.path.getsize(out) == size:
                skip = set(m.get('done', []))
                have_resume = True
                log(f'resuming: skipping {len(skip)} done chunks (existing bytes kept)')
        except Exception as ex:
            log(f'ignoring resume file: {ex}')
            skip = set()
    try:
        if have_resume:
            with open(out, 'r+b') as f:
                pass
        else:
            with open(out, 'wb') as f:
                if size: f.truncate(size)
    except PermissionError:
        msg = (f'Cannot write to {out} (permission denied). Drive roots like D:\\ need admin - '
               f'use a folder, e.g. D:\\Downloads\\{os.path.basename(out)}.')
        print(msg, flush=True)
        log(msg)
        return 'error'
    except OSError as ex:
        msg = f'Cannot create {out}: {ex}'
        print(msg, flush=True)
        log(msg)
        return 'error'
    q = _q.Queue()
    # Tiny files (< 1 byte/chunk) would produce negative/empty ranges and wedge
    # the run forever; never slice a file into more chunks than it has bytes.
    if size and NCHUNKS > size:
        log(f'small file: using {size} chunk(s) instead of {NCHUNKS} (file is only {size} bytes)')
        NCHUNKS = size
    per_link = TPL if NCHUNKS > 1 else 1
    NCH = NCHUNKS
    CHUNKS_DONE = 0
    LINK_THREADS.update({ip: per_link for ip in use})
    WORKER_CAP.update({ip: per_link for ip in use})
    if AUTO:
        with LINK_LOCK:
            mem = dict(LINK_SPEED)
        for ip in use:
            s = mem.get(ip, 0)
            start_cap = 3 if s >= 3 else (2 if s >= 1 else 1)
            WORKER_CAP[ip] = start_cap
            LINK_THREADS[ip] = start_cap
        log('auto-tune start caps from history: ' + ', '.join(f'{ip or "?"}x{WORKER_CAP[ip]}' for ip in use))
    for i in range(NCHUNKS):
        s = size * i // NCHUNKS if size else 0
        e = (size * (i + 1) // NCHUNKS - 1) if size else ''
        if i in skip:
            with CHUNK_LOCK:
                CHUNK_STATE[i] = 'done'
                CHUNKS_DONE += 1
            with TOTAL_DONE_LOCK:
                TOTAL_DONE += (e - s + 1) if isinstance(e, int) else 0
            continue
        q.put((i, s, e))
        with CHUNK_LOCK:
            CHUNK_STATE[i] = '.'
        log(f'chunk{i}: bytes={s}-{e}')
    import threading as _t
    RUN_DONE.clear()
    mon = _t.Thread(target=monitor, daemon=True)
    mon.start()
    tick = _t.Thread(target=ticker, args=(out,), daemon=True)
    tick.start()
    global ESC_THREAD
    if ESC_THREAD is None or not ESC_THREAD.is_alive():
        ESC_THREAD = _t.Thread(target=_esc_listener, daemon=True)
        ESC_THREAD.start()
    wd = _t.Thread(target=watchdog, daemon=True)
    wd.start()
    if AUTO and NCHUNKS > 1:
        log('auto-tune on: workers scale 1-4 per link by throttling')
        at = _t.Thread(target=auto_tune, daemon=True)
        at.start()
    dd = None
    if DASH_MODE == 'full' and NCHUNKS > 1:
        dd = _t.Thread(target=dash, args=(out, use, NCHUNKS), daemon=True)
        dd.start()
    elif DASH_MODE == 'simple':
        dd = _t.Thread(target=simple_dash, args=(out,), daemon=True)
        dd.start()
    workers = []
    for ip in use:
        for _ in range(per_link):
            workers.append((out, ip))
    stopped = False
    failed = None
    import time as _tm
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        with G_LOCK:
            G_FUTS.clear()
            for o, ip in workers:
                G_FUTS.append(ex.submit(worker, o, ip, q))
                G_STARTED.add(ip)
            rid = G_RUNID
            mem = _t.Thread(target=membership, args=(q, out, per_link, use, ex, rid), daemon=True)
            mem.start()
        while True:
            try:
                if stopped or failed is not None:
                    # Build 38: raise the global STOP flag on EVERY stop path
                    # (speedtest cap, ESC/Ctrl+C, no-range restart, hard
                    # failure). Without it, an idle worker that lost the stop
                    # race keeps cycling at the worker tail and ThreadPoolExecutor
                    # shutdown(wait=True) joins it forever - the run hangs even
                    # though STOPPED/FAILED was printed.
                    STOP = True
                    if stopped:
                        abort_sockets()
                    with G_LOCK:
                        for f in G_FUTS: f.cancel()
                    break
                with G_LOCK:
                    fs = list(G_FUTS)
                for f in fs:
                    if not f.done():
                        continue
                    try:
                        f.result()
                    except _SpeedtestDone:
                        stopped = True
                        STOP_REASON = 'speedtest'
                        with G_LOCK:
                            for f in G_FUTS: f.cancel()
                    except _Stop:
                        stopped = True
                        log('STOPPED by user (ESC) - writing resume file')
                    except _NoRange as ex3:
                        if failed is None:
                            bad_ip = str(ex3)
                            log(CNO(f'link {bad_ip} cannot serve multi-byte ranges - dropping it, re-queuing its chunks'))
                            with G_LOCK:
                                _safe = [u for u in use if u]
                                if bad_ip in _safe:
                                    use.remove(bad_ip)
                            with IP_LOCK:
                                IP_STATE[bad_ip] = {'fails': 0, 'parked_until': time.time() + 600, 'gone': False, 'norange': True}
                            _close_ip_socks(bad_ip)
                            with CHUNK_LOCK:
                                for idx0, st0 in list(CHUNK_STATE.items()):
                                    if st0 == bad_ip:
                                        s0, e0 = CHUNK_RANGE.get(idx0, (0, 0))
                                        if not isinstance(e0, int):
                                            continue
                                        CHUNK_OWNER[idx0] = ('gone', time.time())
                                        CHUNK_END[idx0] = s0 - 1
                                        CHUNK_STATE[idx0] = '.'
                                        q.put((idx0, s0, e0))
                            if not [u for u in use if u]:
                                log('no link can multi-range - auto-restarting as single-stream')
                                RETRY_SINGLE = True
                                with G_LOCK:
                                    for f in G_FUTS: f.cancel()
                                stopped = True
                                abort_sockets()
                                break
                    except Exception as ex2:
                        if failed is None:
                            failed = ex2
                            s = str(ex2)
                            if 'web page' in s:
                                hint = 'the server returned a web page instead of the file - the link may be expired or wrong.'
                            elif 'Content-Range' in s or 'size changed' in s or '412' in s:
                                hint = 'the file changed on the server mid-download - restart it fresh.'
                            elif 'Content-Encoding' in s:
                                hint = 'the server compressed the download - unsupported, use a direct link.'
                            elif '404' in s:
                                hint = 'link is broken (404).'
                            elif '403' in s:
                                hint = 'link expired/forbidden (403) - copy a fresh link.'
                            else:
                                hint = 'a chunk kept failing - host may throttle or the link may be dead.'
                            print(CNO(f'Download failed: {s}') + CYLW(f'\n{hint}'), flush=True)
                            log(f'DOWNLOAD FAILED: {s}')
                with CHUNK_LOCK:
                    fin = NCH > 0 and CHUNKS_DONE >= NCH
                if fin and q.empty():
                    break
                with G_LOCK:
                    alive = any(not f.done() for f in G_FUTS)
                if not alive and not q.empty():
                    log('WARNING: queued work but no workers alive - saving resume instead of hanging')
                    break
                _tm.sleep(0.5)
            except KeyboardInterrupt:
                # Ctrl+C is the instinctive stop in a plain console and would
                # otherwise kill the process WITHOUT writing the .resume file
                # (all progress lost). Treat it exactly like ESC: stop workers,
                # close sockets, and let the normal shutdown save a resume.
                if not stopped:
                    stopped = True
                    STOP = True
                    STOP_REASON = 'CTRL+C'
                    log('STOPPED by Ctrl+C - writing resume file')
                    abort_sockets()
                break
        if RETRY_SINGLE:
            log('multi-link exhausted - preparing single-stream restart')
        elif failed is not None:
            log(f'FAILED total {time.time()-T0:.1f}s')
        elif stopped:
            log(f'STOPPED ({STOP_REASON}) total {time.time()-T0:.1f}s')
        else:
            log(f'all chunks done total {time.time()-T0:.1f}s')
    RUN_DONE.set()
    if dd:
        dd.join(timeout=10)
    if STOP_REASON == 'speedtest':
        el = time.time() - T0
        with LINK_LOCK:
            per = dict(LINK_DONE)
        print(CYES(f'SPEEDTEST done: {TOTAL_DONE/1e6:.1f} MB measured in {el:.1f}s'), flush=True)
        if per:
            for ip, b in sorted(per.items(), key=lambda kv: -kv[1]):
                print(f'  {ip or "default route"}: {b/1e6:>6.1f} MB  ({b/max(el,0.01)/1e6:.2f} MB/s)')
        print(f'  combined: {TOTAL_DONE/max(el,0.01)/1e6:.2f} MB/s')
        try:
            for _p in (out, out + '.resume', out + '.progress'):
                if os.path.exists(_p):
                    os.remove(_p)
        except Exception:
            pass
        return
    print(CYES('Finalizing file - verifying all chunks...'), flush=True)
    log('finalizing: verifying all chunks')
    done_list = [i for i in range(NCHUNKS) if CHUNK_STATE.get(i) == 'done']
    if stopped or len(done_list) < NCHUNKS:
        try:
            with open(manifest, 'w') as rf:
                json.dump({'url': ORIG_URL, 'size': size, 'chunks': NCHUNKS, 'done': done_list,
                           'etag': V_ETAG, 'lastmod': V_LM, 'build': BUILD,
                           'vok': not VALIDATOR_BROKEN}, rf)
            log(f'resume saved: {len(done_list)}/{NCHUNKS} chunks ({TOTAL_DONE/1e6:.1f}MB). rerun same link + same chunk count to continue.')
        except Exception as ex:
            log(f'resume save failed: {ex}')
        if RETRY_SINGLE:
            try:
                with open(out, 'wb'):
                    pass
                if os.path.exists(manifest): os.remove(manifest)
                pf = out + '.progress'
                if os.path.exists(pf): os.remove(pf)
            except Exception:
                pass
            log('auto-restarting as single-stream to finish the file')
            return 'retry-single'
        notify('BandwidthGoblin: download failed' if failed is not None else 'BandwidthGoblin: stopped',
               f'{os.path.basename(out)} - resume saved ({len(done_list)}/{NCHUNKS} chunks)')
        return
    got = os.path.getsize(out)
    log(f'saved: {out} ({got/1e6:.1f} MB) in {time.time()-T0:.1f}s')
    if TOTAL_SIZE and got != TOTAL_SIZE:
        log(f'WARNING: size mismatch got={got} expected={TOTAL_SIZE}')
    else:
        log('size verified OK')
    _ok, _why = verify_file(out, TOTAL_SIZE)
    log(f'intact check: {_why}')
    print((CYES('INTACT: ') if _ok else CNO('CORRUPT?: ')) + _why, flush=True)
    if not _ok:
        notify('BandwidthGoblin: verification FAILED', f'{os.path.basename(out)} - {_why}')
        try:
            if os.path.exists(manifest): os.remove(manifest)
            pf = out + '.progress'
            if os.path.exists(pf): os.remove(pf)
        except Exception: pass
        perf_write({'type': 'run', 'host': G_HOST, 'mb_total': round(got / 1e6, 1),
                    'sec': round(time.time() - T0, 1), 'mbps_avg': 0,
                    'links': sorted([u for u in use if u]), 'chunks': NCHUNKS,
                    'tpl': TPL, 'auto': AUTO, 'outcome': 'verify_failed', 'err': _why[:120]})
        _hist_write({'url': redact(ORIG_URL), 'host': G_HOST,
                     'out': os.path.basename(out), 'mb': round(got / 1e6, 1),
                     'sec': round(time.time() - T0, 1), 'mbps': 0,
                     'links': sorted([u for u in use if u]) or None,
                     'chunks': NCHUNKS, 'outcome': 'verify_failed', 'err': _why[:80]})
        return 'error'
    print(CYES(f'DONE: {out} ({got/1e6:.1f} MB) in {time.time()-T0:.1f}s'), flush=True)
    _el = time.time() - T0
    _eh, _rem = divmod(int(_el), 3600)
    _em, _es = divmod(_rem, 60)
    _ets = f'{_eh}h{_em:02d}m' if _eh else (f'{_em}m{_es:02d}s' if _em else f'{_es}s')
    with LINK_LOCK:
        _perlink = {ip: round(v / 1e6, 1) for ip, v in LINK_DONE.items()}
    if len([u for u in use if u]) > 1 and _perlink:
        # Build 31: multi-link runs show per-link totals on the finish line so
        # you can see exactly what each interface contributed.
        _parts = []
        for _ip in sorted(_perlink, key=lambda k: -(_perlink[k] or 0)):
            _lbl = link_label(_ip) if _ip else 'default route'
            _rr = (LINK_DONE.get(_ip, 0) or 0) / max(_el, 0.01) / 1e6
            _parts.append(f'{_lbl}: {_perlink[_ip] or 0:.1f}MB @ {_rr:.2f} MB/s')
        print('  links: ' + ' | '.join(_parts), flush=True)
    perf_write({'type': 'run', 'host': G_HOST, 'mb_total': round(got / 1e6, 1),
                'sec': round(_el, 1), 'mbps_avg': round(got / _el / 1e6, 2) if _el else 0,
                'links': sorted([u for u in use if u]), 'chunks': NCHUNKS,
                'tpl': TPL, 'auto': AUTO, 'per_link_mb': _perlink, 'outcome': 'done'})
    _hist_write({'url': redact(ORIG_URL), 'host': G_HOST or (urllib.parse.urlparse(ORIG_URL).hostname or ''),
                 'out': os.path.basename(out), 'mb': round(got / 1e6, 1),
                 'sec': round(_el, 1), 'mbps': round(got / max(_el, 0.01) / 1e6, 2),
                 'links': sorted([u for u in use if u]) or None,
                 'chunks': NCHUNKS, 'outcome': 'done'})
    _el = time.time() - T0
    _eh, _rem = divmod(int(_el), 3600)
    _em, _es = divmod(_rem, 60)
    _ets = f'{_eh}h{_em:02d}m' if _eh else (f'{_em}m{_es:02d}s' if _em else f'{_es}s')
    _prune_logs(LOGDIR)
    notify('BandwidthGoblin: download complete', f'{os.path.basename(out)} ({got/1e6:.1f} MB) in {_ets}')
    try:
        if OPEN_AFTER is not False and (OPEN_AFTER is True or _tty()):
            print('\a', end='', flush=True)
            d = os.path.dirname(os.path.abspath(out))
            if os.name == 'nt':
                os.startfile(d)
            elif sys.platform == 'darwin':
                subprocess.run(['open', d])
            else:
                subprocess.run(['xdg-open', d])
    except Exception:
        pass
    try:
        if os.path.exists(manifest): os.remove(manifest)
        pf = out + '.progress'
        if os.path.exists(pf): os.remove(pf)
    except Exception: pass
    return 'ok'

if __name__ == '__main__': main()
