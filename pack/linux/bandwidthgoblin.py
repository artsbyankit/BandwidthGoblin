"""BandwidthGoblin: download ONE file over ALL your internet links
(LAN + USB-tethered phones) at the same time, with resume, work-stealing,
hot-plug and a live dashboard. Stdlib only."""
import http.client, ssl, sys, os, re, subprocess, urllib.parse, concurrent.futures, time, threading, socket, collections, json, random, hashlib, base64, urllib.request, shutil, zipfile, platform, glob, atexit, errno, unicodedata, email.message

# A closed stdout (pipes, pagers like `| head`, GUI shells) must never kill a
# run mid-download. Swap print for a version that swallows write errors.
import queue
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
BUILD = 48
BETA = False  # when True, version strings carry the "(beta)" label (public status)
_VER_LABEL = ' (beta)' if BETA else ''

# Build 48: two views - a calm simple screen by default and a btop-style
# advanced view (A switches, the choice is remembered). Stop keeps part-finished
# chunks, one-connection downloads are chunked and resumable, existing files
# are never overwritten, and a pile of reliability fixes (see CHANGELOG).
#
# Build 45: YouTube downloads now get the full BandwidthGoblin treatment -
# the raw DASH streams are pulled straight through the multi-link engine
# (split over every connection, resumable, verified) and merged locally with
# ffmpeg, so YouTube uses all your networks like any other link. Same
# usability passes as build 44: paste a link, pick a quality, done. This
# build also adds self-explanatory UI: a one-time "how to drive this" intro
# at the first prompt, visible [P]/[R]/[ESC]/[H] key hints in the download
# dashboard (H = in-run help overlay), a 'help' command at any prompt, and
# engine/feature info lines when a YouTube download starts.
# Bug fixes: --limit / --links / --cookie / --referer / --sha now apply to
# YouTube split downloads too; leftover temp stream files are cleaned up on
# failure or stop; YouTube filenames sanitize Windows-forbidden trailing
# dots/spaces and reserved names (CON, NUL, ...); non-single requested
# download shapes fall back to yt-dlp safely.
#
# Build 44: first stable release. Adds YouTube downloads - paste any
# youtube.com / youtu.be link and BandwidthGoblin fetches the proven
# yt-dlp engine (one time, ~30-40 MB, cached in tools/) and downloads the
# video at the best quality the video offers, or at a user-picked height
# (2160/1440/1080/720/480) or as audio-only. The first YouTube download also
# fetches deno (YouTube's required JS runtime) and ffmpeg (to merge full
# quality video + audio into one file) - afterwards everything is offline.
# New flag:  bandwidthgoblin.py --yt URL [quality]
#
# Build 43: merged line (old LinkBoost build 22 -> BandwidthGoblin build 42).
# The new implementation is a strict superset of the old one, so nothing was
# merged back except one opt-out-friendly switch: loopback (127.0.0.x) and
# link-local (169.254.x) addresses stay dropped by default - they are not
# separate physical routes - but BANDWIDTHGOBLIN_KEEP_LOOPBACK=1 keeps them
# for LAN-loopback testing. Branding is BandwidthGoblin everywhere.

SOCK_TIMEOUT = 20   # real socket timeout (sec); enforced via sock.settimeout
STALL_SECS = 25     # abort a chunk Range if zero bytes arrive for this long
READ_BLOCK = 256 * 1024   # most bytes taken from a socket per read

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

def _set_paused(v):
    global PAUSED
    if PAUSED != v:
        PAUSED = v
        log('PAUSED - press P to continue, ESC to stop' if v else 'RESUMED - downloads continuing')

def _tui_msg(m):
    _TUI['msg'] = m
    _TUI['msg_until'] = time.time() + 4

def _set_mode(m):
    global UI_MODE
    UI_MODE = m
    try:
        with open(os.path.join(_script_dir(), '.uimode'), 'w') as f:
            f.write(m)
    except Exception:
        pass

def _load_mode():
    global UI_MODE
    try:
        with open(os.path.join(_script_dir(), '.uimode')) as f:
            if f.read().strip() == 'advanced':
                UI_MODE = 'advanced'
    except Exception:
        pass

def _request_stop(reason):
    global STOP, STOP_REASON, STOP_USER
    STOP_REASON = reason
    STOP_USER = True
    STOP = True

def _toggle_rest(ip):
    """[x]: rest one connection (its work moves to the others) or wake it again."""
    if ip is None:
        return
    if ip in BENCHED:
        BENCHED.discard(ip)
        _tui_msg(f'{link_label(ip)} is back in action')
        return
    with G_LOCK:
        others = [u for u in _TUI['use'] if u != ip and u not in BENCHED]
    if not others:
        _tui_msg('that is the last working connection - keeping it on')
        return
    BENCHED.add(ip)
    if CUR_Q is not None:
        _requeue_link(ip, CUR_Q)
    _close_ip_socks(ip)
    _tui_msg(f'{link_label(ip)} is resting - press x to wake it')

def _handle_key(k):
    """One key press. A failure here must never take the keyboard down with it."""
    try:
        _do_key(k)
    except Exception as ex:
        log(f'key {k!r} failed: {ex}')

def _do_key(k):
    global SPEEDLIMIT
    k = k.lower() if len(k) == 1 else k
    if k in ('esc', 'q'):
        _request_stop('ESC')
    elif k == 'ctrl-c':
        _request_stop('CTRL+C')
    elif k == 'p':
        _set_paused(not PAUSED)
    elif k == 'r':
        _set_paused(False)
    elif k in ('h', '?'):
        _TUI['help'] = not _TUI['help']
        _TUI['help_until'] = time.time() + 20
    elif k == 'a':
        _set_mode('simple' if UI_MODE == 'advanced' else 'advanced')
    elif k == '-':
        # Start from the live speed unless it is near zero (e.g. while paused),
        # and never go so low that the cap itself looks like a dead download.
        SPEEDLIMIT = max(100e3, (SPEEDLIMIT or (_TUI['speed'] if _TUI['speed'] > 100e3 else 8e6)) * 0.75)
        log(f'speed cap: {SPEEDLIMIT/1e6:.1f} MB/s')
    elif k in ('+', '='):
        if SPEEDLIMIT:
            SPEEDLIMIT = SPEEDLIMIT / 0.75
            log(f'speed cap: {SPEEDLIMIT/1e6:.1f} MB/s')
    elif k == '0':
        SPEEDLIMIT = 0.0
        log('speed cap removed')
    elif k == 'up':
        _TUI['sel'] = max(0, _TUI['sel'] - 1)
    elif k == 'down':
        _TUI['sel'] += 1
    elif k == 'x':
        _toggle_rest(_TUI['sel_ip'])
    elif k == 's':
        _TUI['sort'] = (_TUI['sort'] + 1) % 3
    elif k in ('1', '2', '3', '4'):
        _TUI['panels'] ^= {{'1': 'net', '2': 'links', '3': 'chunks', '4': 'log'}[k]}

def _parse_keys(text):
    """Terminal input -> key names. Arrow keys arrive as ESC [ A: they must never
    be mistaken for a lone ESC, which means stop."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == '\x1b':
            m = re.match(r'\x1b[\[O][0-9;]*[A-Za-z~]', text[i:])
            if m:
                out.extend({'A': ['up'], 'B': ['down']}.get(m.group(0)[-1], []))
                i += len(m.group(0))
                continue
            out.append('esc')
        elif c == '\x03':
            out.append('ctrl-c')
        else:
            out.append(c)
        i += 1
    return out

def _parse_ifconfig(out):
    ips, current = [], None
    for line in out.splitlines():
        if line and not line[0].isspace():
            current = line.split(':')[0].strip()
        m = re.search(r'\binet (\d+\.\d+\.\d+\.\d+)\b', line)
        if m and not m.group(1).startswith('127.'):
            ips.append((current or 'net', m.group(1)))
    return ips

def _parse_ipconfig(out):
    # Matches on "IPv4", not "IPv4 Address": the label is translated on
    # non-English Windows (IPv4-Adresse, Adresse IPv4, Direccion IPv4...).
    ips, current = [], None
    for line in out.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(':'):
            current = line.strip()
        m = re.search(r'IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)', line)
        if m and not m.group(1).startswith('127.'):
            ips.append((current or 'windows', m.group(1)))
    return ips

def local_ips():
    ips = []
    try:
        if os.name == 'nt':
            ips = _parse_ipconfig(subprocess.check_output(
                'ipconfig', text=True, errors='ignore',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
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
    # Dedupe. Loopback (127.0.0.x) and link-local/APIPA (169.254.x) are not
    # separate physical routes, so they are dropped by default - a loopback
    # cannot add bandwidth. Set BANDWIDTHGOBLIN_KEEP_LOOPBACK=1 to include
    # them anyway, e.g. for multi-link tests against a 127.0.0.x server.
    _keep_lb = (os.environ.get('BANDWIDTHGOBLIN_KEEP_LOOPBACK') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    seen, res = set(), []
    for name, ip in ips:
        if ip not in seen and (_keep_lb or not ip.startswith(('127.', '169.254.'))):
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

def _disposition_name(v):
    """Content-Disposition header -> safe file name ('' when it names none)."""
    try:
        m = email.message.Message()
        m['Content-Disposition'] = v or ''
        return _safe_filename(m.get_filename() or '')
    except Exception:
        return ''

def _get_probe(url):
    """HEAD was refused (S3-style signed links and many CDNs do that for GET-only
    URLs): learn the same facts from a one-byte GET. Returns (status, url, size,
    accept_ranges, meta)."""
    for _ in range(4):
        conn, path = open_conn(url)
        try:
            conn.request('GET', path, headers={'Range': 'bytes=0-0', 'User-Agent': UA, 'Accept': '*/*',
                                               'Accept-Encoding': 'identity', **EXTRA_HEADERS})
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308) and r.getheader('Location'):
                url = urllib.parse.urljoin(url, r.getheader('Location'))
                continue
            size, ar = 0, r.getheader('Accept-Ranges')
            if r.status == 206:
                m = re.search(r'/(\d+)\s*$', r.getheader('Content-Range') or '')
                size, ar = int(m.group(1)) if m else 0, 'bytes'
            elif r.status == 200:
                try:
                    size = int(r.getheader('Content-Length') or 0)
                except (ValueError, TypeError):
                    size = 0
            return r.status, url, size, ar, _resp_meta(r)
        finally:
            try: conn.close()
            except Exception: pass
    raise RuntimeError('too many redirects')

def _resp_meta(r):
    return {'etag': r.getheader('ETag') or '',
            'lastmod': r.getheader('Last-Modified') or '',
            'ctype': (r.getheader('Content-Type') or '').split(';')[0].strip().lower(),
            'enc': (r.getheader('Content-Encoding') or '').strip().lower(),
            'name': _disposition_name(r.getheader('Content-Disposition'))}

def head(url):
    # Build 47 harden: googlevideo/CDN signed URLs intermittently 403/404/410
    # from one edge while the SAME url works moments later (or after yt-dlp
    # re-extracts fresh signatures). A transient first-request rejection used
    # to abort the multi-link engine on the spot and silently downgrade the run
    # to single-connection yt-dlp (no dashboard). Retry with backoff instead.
    # Build 48: any non-2xx HEAD is only a refused HEAD - ask with a GET before
    # believing it (its Content-Length is the error page, not the file), and a
    # network blip on the first request gets the same retries.
    hops, fail, neterr = 0, 0, 0
    while True:
        conn, path = open_conn(url)
        try:
            try:
                conn.request('HEAD', path, headers={'User-Agent': UA, 'Accept': '*/*', 'Connection': 'keep-alive', **EXTRA_HEADERS})
                r = conn.getresponse(); r.read()
            except (OSError, http.client.HTTPException):
                neterr += 1
                if neterr >= 3:
                    raise
                time.sleep(0.5 * neterr)
                continue
            if r.status in (301, 302, 303, 307, 308) and r.getheader('Location'):
                hops += 1
                if hops >= 6:
                    raise RuntimeError('too many redirects')
                url = urllib.parse.urljoin(url, r.getheader('Location')); continue
            if not 200 <= r.status < 300:
                st, u2, size, ar, meta = _get_probe(url)
                if st in (200, 206):
                    return u2, size, ar, meta
                fail += 1
                if fail < 4:
                    time.sleep(0.4 * fail)
                    continue
                raise RuntimeError(f'HTTP {st} for {redact(url)} (after {fail} attempts)')
            try:
                size = int(r.getheader('Content-Length') or 0)
            except (ValueError, TypeError):
                size = 0   # malformed Content-Length - treat as unknown size
            ar = r.getheader('Accept-Ranges')
            meta = _resp_meta(r)
            if not size and ar and 'bytes' in ar.lower():
                try:
                    st, _u, s2, _a, m2 = _get_probe(url)
                    if st == 206 and m2.get('enc') in ('', 'identity'):
                        size = s2
                except Exception:
                    pass
            return url, size, ar, meta
        finally:
            try: conn.close()
            except Exception: pass

def range_probe(url, ip=None, size=0):
    """Prove the server honours a REAL mid-file Range before multi-link mode.
    A range starting at 0 can be special-cased by servers (raw.githubusercontent
    serves `bytes=0-N` as 206 but re-sends 200 full bodies for other offsets).
    We probe a range that never starts at 0 (size//2) to catch such liars.
    Returns (proved_206, total). 200-only servers must drop to single stream.
    Redirects are followed (bounded). Some CDNs - googlevideo in particular -
    answer a range probe from a second source IP with a 302 to that link's
    nearest node; dropping such links would silently switch YouTube downloads
    back to a single connection, so a redirect is re-probed instead."""
    if size and size > 1:
        s0, e0 = max(1, size // 2), size - 1
    else:
        s0, e0 = 0, 262143
    cur = url
    for _hop in range(5):
        conn, path = open_conn(cur, ip)
        try:
            conn.request('GET', path, headers={'Range': f'bytes={s0}-{e0}', 'User-Agent': UA,
                                              'Accept': '*/*', 'Accept-Encoding': 'identity', **EXTRA_HEADERS})
            r = conn.getresponse()
            cr = r.getheader('Content-Range') or ''
            enc = (r.getheader('Content-Encoding') or '').strip().lower()
            if r.status in (301, 302, 303, 307, 308):
                loc = r.getheader('Location') or ''
                r.read(1 << 16)
                if not loc or not (loc.startswith('http://') or loc.startswith('https://')):
                    raise RuntimeError(f'range probe: HTTP {r.status} (bad Location)')
                cur = urllib.parse.urljoin(cur, loc)
                continue
            r.read(1 << 20)
            if r.status == 206:
                m = re.match(rf'bytes\s+{s0}-(\d+)/(\d+|\*)$', cr.strip())
                if m and enc in ('', 'identity'):
                    tot = 0 if m.group(2) == '*' else int(m.group(2))
                    # The probe just walked this link's own redirect chain to
                    # its nearest CDN node - remember it so the first chunks
                    # skip the hop instead of re-walking it (build 47).
                    if cur != url:
                        with REDIR_LOCK: REDIR[ip] = cur
                    return True, tot
                raise RuntimeError(f'bad 206 probe: {cr!r}')
            if r.status == 200:
                cl = r.getheader('Content-Length') or '0'
                try:
                    total = int(cl)
                except ValueError:
                    total = 0
                if cur != url:
                    with REDIR_LOCK: REDIR[ip] = cur
                return False, total if enc in ('', 'identity') else 0
            raise RuntimeError(f'range probe: HTTP {r.status}')
        finally:
            try: conn.close()
            except Exception: pass
    raise RuntimeError('range probe: too many redirects')

LOGFILE = None
LOGLOCK = threading.Lock()
T0 = 0
RING = collections.deque(maxlen=60)
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
SPARK = collections.deque(maxlen=1200)
LINK_HIST = {}                                 # per-link speed history for the advanced view
BENCHED = set()                                # links the user rested with [x]
POST_DASH = []                                 # messages held back until the full-screen view closes
CUR_Q = None                                   # the running download's work queue
UI_MODE = 'simple'                             # 'simple' or 'advanced' ([A] switches, remembered in .uimode)
OVERWRITE = False                              # --overwrite: replace an existing file instead of keeping both
_RENAMED = {}                                  # requested path -> the name we actually saved under
_TUI = {'sel': 0, 'sel_ip': None, 'sort': 0, 'help': False, 'help_until': 0.0, 'msg': '', 'msg_until': 0.0,
        'panels': {'net', 'links', 'chunks', 'log'}, 'speed': 0.0, 'use': []}
STOP = False
STOP_REASON = 'ESC'
STOP_USER = False                            # human/watchdog stop requested (ESC/Ctrl+C/stall) - never auto-continue
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
_INTRO_SHOWN = False                           # one-time "how to drive this" intro at the first prompt
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

_BGQ = queue.SimpleQueue()
_BG_THREAD = None

def _bg_writer():
    """Does all log/stats file writing, off the download threads, so a slow or
    busy drive can never hold up a download. A failed write (antivirus, full disk)
    costs one log line here, never a thread."""
    while True:
        items = [_BGQ.get()]
        time.sleep(0.2)                     # let lines pile up: fewer file opens
        while not _BGQ.empty():
            items.append(_BGQ.get())
        texts = {}
        for it in items:
            if isinstance(it, tuple):
                texts.setdefault(it[0], []).append(it[1])
        for path, parts in texts.items():
            try:
                with open(path, 'a', errors='replace') as lf:
                    lf.write(''.join(parts))
            except Exception:
                pass
        for it in items:
            try:
                if isinstance(it, threading.Event):
                    it.set()
                elif callable(it):
                    it()
            except Exception:
                pass

def _bg_put(item):
    global _BG_THREAD
    if _BG_THREAD is None:
        with LOGLOCK:
            if _BG_THREAD is None:
                _BG_THREAD = threading.Thread(target=_bg_writer, daemon=True, name='bgwrite')
                _BG_THREAD.start()
    _BGQ.put(item)

def _bg_flush(timeout=3):
    """Wait for queued lines to reach the disk (called on the way out)."""
    if _BG_THREAD is not None:
        ev = threading.Event()
        _BGQ.put(ev)
        ev.wait(timeout)

atexit.register(_bg_flush)

def log(msg):
    line = f'{time.strftime("%H:%M:%S")} {msg}'
    RING.append(line)
    if not DASH_ON:
        out_line = line
        if 'TOTAL:' in msg:
            out_line = re.sub(r'([\d.]+ MB/s)', lambda m: CYLW(m.group(1)), line)
        if not (QUIET or (UI_MODE == 'simple' and _tty())) or any(k in msg for k in ('TOTAL:', 'all chunks done', 'resume saved',
                                                'DOWNLOAD FAILED', 'STOPPED', 'finalizing:',
                                                'auto-restarting', 'single-stream retry',
                                                'WATCHDOG', 'PAUSED', 'RESUMED', 'FAILED')):
            print(out_line, flush=True)
    if LOGFILE and not NO_LOG:
        _bg_put((LOGFILE, _ANSI.sub('', line) + '\n'))

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
    if ip in BENCHED: return False
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
    try:
        if os.name == 'nt':
            import msvcrt
            while not STOP and not RUN_DONE.is_set():
                time.sleep(0.05)
                while msvcrt.kbhit():
                    c = msvcrt.getwch()
                    if c in ('\x00', '\xe0'):
                        # Special keys come as two codes. The second looks like a
                        # letter (down arrow = 'P'), so it must be consumed here.
                        _handle_key({'H': 'up', 'P': 'down'}.get(msvcrt.getwch(), 'none'))
                    elif c == '\x1b':
                        _handle_key('esc')
                    elif c == '\x03':
                        _handle_key('ctrl-c')
                    else:
                        _handle_key(c)
            while msvcrt.kbhit():      # leftovers must not type into the next prompt
                msvcrt.getwch()
        else:
            import termios, tty, select
            if not sys.stdin.isatty():
                return
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                # cbreak, not raw: raw mode also kills Ctrl+C and output newlines.
                tty.setcbreak(fd)
                while not STOP and not RUN_DONE.is_set():
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if r:
                        data = os.read(fd, 64)
                        if data == b'\x1b' and select.select([fd], [], [], 0.03)[0]:
                            data += os.read(fd, 64)   # a split arrow key, not ESC
                        for k in _parse_keys(data.decode('utf-8', 'ignore')):
                            _handle_key(k)
            finally:
                try: termios.tcsetattr(fd, termios.TCSAFLUSH, old)
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

_FD_LOCAL = threading.local()

def _out_fd(out):
    """This worker's handle on the output file, kept for the whole run. Reopening
    it per chunk froze every writer for ~8s after the first chunks closed on Windows
    (measured; most likely antivirus rescanning the file on each close)."""
    c = getattr(_FD_LOCAL, 'fd', None)
    if c and c[0] == out:
        return c[1]
    _close_out_fd()
    # NB: os.open defaults to TEXT mode on Windows (translates \n -> \r\n
    # and corrupts binaries). O_BINARY is required. getattr for POSIX.
    fd = os.open(out, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0))
    _FD_LOCAL.fd = (out, fd)
    return fd

def _close_out_fd():
    c = getattr(_FD_LOCAL, 'fd', None)
    _FD_LOCAL.fd = None
    if c:
        try: os.close(c[1])
        except OSError: pass

def fetch_direct(idx, s, e, ip, out, tok):
    global TOTAL_DONE, G429_N, G429_COOLDOWN, LAST_PROGRESS, THR_LAST
    global VALIDATOR_BROKEN
    bounded = isinstance(e, int)
    total = (e - s + 1) if bounded else 0
    done = 0
    t0, last = time.time(), time.time()
    retries = 0
    _stalefresh = 0  # silent reconnects for server-closed keep-alives (build 36)
    fd = _out_fd(out)
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
                            _ifrange = V_ETAG
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
                    # An expired per-link node URL (googlevideo re-signs per
                    # request) makes REDIR[ip] stale: refresh_final() can only
                    # rescue us if the next attempt stops preferring that dead
                    # cached node (build 47 audit M1 - without this, a 403
                    # token deadline replayed against the same dead node until
                    # give-up, failing long downloads days before expiry).
                    with REDIR_LOCK:
                        REDIR.pop(ip, None)
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
                        LAST_PROGRESS = time.time()   # ...nor trip the stall watchdog either (audit M2: a >3min [P] pause used to hard-stop the run)
                        win_t, win_n = last_recv, 0   # ...nor look like a trickling server on the way out
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
                        # read1: take whatever has arrived. On fast links that is a big
                        # block (less Python per byte); on slow ones a small block now
                        # rather than a wait for 64 KB, so screen and stall checks stay live.
                        blk = r.read1(max(16384, min(READ_BLOCK, int(SPEEDLIMIT / 4))) if SPEEDLIMIT else READ_BLOCK)
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
                    try:
                        while mv:
                            n = os.write(fd, mv)
                            mv = mv[n:]
                    except OSError as ex:
                        if ex.errno in (errno.ENOSPC, errno.EDQUOT if hasattr(errno, 'EDQUOT') else errno.ENOSPC):
                            raise _Fatal('the disk is full')   # retrying cannot help
                        raise
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
    global STOP, STOP_REASON, STOP_USER, LAST_PROGRESS
    rid = G_RUNID
    while True:
        time.sleep(5)
        if STOP or RUN_DONE.is_set() or G_RUNID != rid:
            return
        now = time.time()
        if PAUSED:
            LAST_PROGRESS = now   # workers idling between chunks while paused are not a stall
            continue
        with CHUNK_LOCK:
            if NCH > 0 and CHUNKS_DONE >= NCH:
                return
        if now - T0 > WATCHDOG_SECS and now - LAST_PROGRESS > WATCHDOG_SECS:
            STOP = True
            STOP_REASON = 'WATCHDOG'
            STOP_USER = True
            log(f'WATCHDOG: no progress for {int(now-LAST_PROGRESS)}s - forcing stop instead of hanging')
            abort_sockets()
            return

def monitor():
    last_t = time.time()
    rid = G_RUNID
    while True:
        if STOP or RUN_DONE.is_set() or G_RUNID != rid:
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

def _chunk_positions():
    """Part-finished chunks -> {chunk: next byte needed}. Call with CHUNK_LOCK."""
    return {str(i): CHUNK_POS[i] for i in range(NCH)
            if CHUNK_STATE.get(i) != 'done' and i in CHUNK_POS}

def _write_manifest(out, vok=None):
    """Atomic .resume save. Deliberately no fsync of the download: on Windows the
    first flush of a pre-sized file zero-fills the gap and freezes every writer
    (6s per 4 GB measured). A crash of this program loses nothing; a power cut
    can leave a chunk marked done but unwritten, which --sha would catch."""
    with CHUNK_LOCK:
        done_list = [i for i in range(NCH) if CHUNK_STATE.get(i) == 'done']
        pos = _chunk_positions()
    rec = {'url': ORIG_URL, 'size': TOTAL_SIZE, 'chunks': NCH, 'done': done_list, 'pos': pos,
           'etag': V_ETAG, 'lastmod': V_LM, 'build': BUILD}
    if vok is not None:
        rec['vok'] = vok
    tmp = out + '.resume.tmp'
    with open(tmp, 'w') as f:
        json.dump(rec, f)
    os.replace(tmp, out + '.resume')
    return done_list

def ticker(out):
    pf = out + '.progress'
    rid = G_RUNID
    n = 0
    while True:
        time.sleep(1)
        # The run id check matters: without it a ticker from the previous file
        # could wake inside the next run and write ghost files for the old one.
        if STOP or RUN_DONE.is_set() or G_RUNID != rid:
            break
        try:
            with TOTAL_DONE_LOCK:
                d = TOTAL_DONE
            with open(pf, 'w') as f: f.write(f'{d} {time.time()}')
        except Exception:
            pass
        n += 1
        if TOTAL_SIZE and n % 5 == 0:
            try:
                _write_manifest(out)
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

def _spin(msg, gate):
    """Animated one-line loading indicator for the YouTube processing phases
    that run with NO dashboard (reading video info, ffmpeg merge, chunk
    verify). Terminal-only: silent when the output is piped or --quiet is on,
    and it clears its own line when done so the next status line prints
    cleanly instead of stacking garbage (build 47)."""
    if QUIET or not _tty():
        return
    def _run():
        i = 0
        t0 = time.time()
        try:
            while not gate() and not STOP:
                sys.stdout.write('\r\x1b[2K' + _col('1;38;2;180;190;254', '|/-\\'[i % 4])
                                 + ' ' + CDIM(msg) + CDIM(f'  {int(time.time() - t0)}s'))
                sys.stdout.flush()
                time.sleep(0.1)
                i += 1
            sys.stdout.write('\r\x1b[2K')
            sys.stdout.flush()
        except Exception:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t

def _yticks(msg, fn):
    """Run fn() while showing the loading animation. Returns fn()'s result."""
    done = threading.Event()
    t = _spin(msg, done.is_set)
    try:
        return fn()
    finally:
        done.set()
        if t:
            t.join(0.5)   # let it wipe its line before anything else prints

def _wcw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1

def _vis_len(s):
    return sum(_wcw(c) for c in _ANSI.sub('', s))

def _fit(s, width):
    """Cut or pad a coloured string to exactly `width` terminal cells. Colours
    survive the cut and double-width characters (CJK, emoji) count as 2."""
    if width <= 0:
        return ''
    out, used, pos, cut = [], 0, 0, False
    while pos < len(s):
        m = _ANSI.match(s, pos)
        if m:
            out.append(m.group(0))
            pos = m.end()
            continue
        w = _wcw(s[pos])
        if used + w > width:
            cut = True
            break
        out.append(s[pos])
        used += w
        pos += 1
    return ''.join(out) + ('\x1b[0m' if cut and '\x1b[' in s else '') + ' ' * (width - used)

def _rate(b):
    if b >= 1e9: return f'{b/1e9:.2f} GB/s'
    if b >= 1e6: return f'{b/1e6:.1f} MB/s'
    return f'{b/1e3:.0f} KB/s'

def _size(b):
    if b >= 1e9: return f'{b/1e9:.2f} GB'
    if b >= 1e6: return f'{b/1e6:.1f} MB'
    return f'{b/1e3:.0f} KB'

def _clock(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h: return f'{h}h {m:02d}m'
    if m: return f'{m}m {s:02d}s'
    return f'{s}s'

def _plain_label(lbl):
    """'Wi-Fi (192.168.1.5)' -> 'Wi-Fi': simple mode never shows addresses."""
    return re.sub(r'\s*\([\d.]+\)$', '', lbl or '') or 'Connection'

def _tui_style(color=None, unicode_ok=None, braille=None):
    """(colours, glyphs) for the current terminal. Falls back to plain ASCII on
    legacy code pages and to block graphs where the font has no braille."""
    if color is None:
        color = os.environ.get('NO_COLOR') is None
    if unicode_ok is None:
        unicode_ok = os.environ.get('MULTILINK_ASCII') != '1'
        if unicode_ok:
            try:
                '╭─╮│╰╯█░●▁⣿'.encode(sys.stdout.encoding or 'utf-8')
            except Exception:
                unicode_ok = False
    if braille is None:
        pick = (os.environ.get('BANDWIDTHGOBLIN_GRAPH') or '').lower()
        # Classic conhost + Consolas draws braille as empty boxes.
        braille = pick == 'braille' or (pick != 'block' and (os.name != 'nt' or bool(os.environ.get('WT_SESSION'))))
    C = {'rst': '\x1b[0m', 'bld': '\x1b[1m', 'dim': '\x1b[2m', 'inv': '\x1b[7m',
         'cyan': '\x1b[38;2;180;190;254m', 'grn': '\x1b[38;2;166;227;161m',
         'ylw': '\x1b[38;2;249;226;175m', 'red': '\x1b[38;2;243;139;168m',
         'sky': '\x1b[38;2;137;220;235m', 'peach': '\x1b[38;2;250;179;135m',
         'pink': '\x1b[38;2;245;194;231m', 'teal': '\x1b[38;2;148;226;213m',
         'box': '\x1b[38;2;137;140;170m', 'mauve': '\x1b[38;2;203;166;247m'}
    if not color:
        C = {k: '' for k in C}
    if unicode_ok:
        G = {'tl': '╭', 'tr': '╮', 'bl': '╰', 'br': '╯', 'h': '─', 'v': '│', 'full': '█', 'empty': '░',
             'dot': '●', 'sel': '›', 'done': '■', 'run': '▣', 'wait': '·', 'logo': '👺 ',
             'braille': bool(braille), 'blocks': ' ▁▂▃▄▅▆▇█', 'sep': ' · ', 'up': '↑↓'}
    else:
        G = {'tl': '+', 'tr': '+', 'bl': '+', 'br': '+', 'h': '-', 'v': '|', 'full': '#', 'empty': '-',
             'dot': '*', 'sel': '>', 'done': '#', 'run': '>', 'wait': '.', 'logo': '',
             'braille': False, 'blocks': None, 'sep': ' - ', 'up': 'up/dn'}
    return C, G

def _graph(vals, w, h, C, G):
    """btop-style history graph: h rows x w columns, newest on the right,
    coloured green -> yellow -> red towards the top."""
    if w <= 0 or h <= 0:
        return []
    per = 2 if G['braille'] else 1
    vals = list(vals)[-(w * per):]
    vals = [0.0] * (w * per - len(vals)) + vals
    top = max(max(vals), 1e-6)
    steps = 4 if G['braille'] else (8 if G['blocks'] else 1)
    lv = [(max(1, round(v / top * h * steps)) if v > 0 else 0) for v in vals]
    rows = []
    for r in range(h):
        floor = (h - 1 - r) * steps
        frac = (h - r) / h
        col = C['red'] if frac > 0.8 else (C['ylw'] if frac > 0.5 else C['grn'])
        cells = []
        for x in range(w):
            if G['braille']:
                bits, a, b = 0, min(steps, max(0, lv[x * 2] - floor)), min(steps, max(0, lv[x * 2 + 1] - floor))
                for i in range(a):
                    bits |= (0x40, 0x04, 0x02, 0x01)[i]
                for i in range(b):
                    bits |= (0x80, 0x20, 0x10, 0x08)[i]
                cells.append(chr(0x2800 + bits) if bits else ' ')
            elif G['blocks']:
                cells.append(G['blocks'][min(steps, max(0, lv[x] - floor))])
            else:
                cells.append('#' if lv[x] > floor else ' ')
        rows.append(col + ''.join(cells) + C['rst'])
    return rows

def _spark(vals, w, G):
    if w <= 0:
        return ''
    vals = list(vals)[-w:]
    top = max(vals + [1e-6])
    gl = G['blocks'] or ' .:-=+*#%@'
    n = len(gl) - 1
    return ' ' * (w - len(vals)) + ''.join(gl[min(n, int(v / top * n + (0.99 if v > 0 else 0)))] for v in vals)

def _meter(frac, w, col, C, G):
    if w <= 0:
        return ''
    n = int(w * max(0.0, min(1.0, frac)))
    return col + G['full'] * n + C['rst'] + C['dim'] + G['empty'] * (w - n) + C['rst']

_HELP_SIMPLE = ('P  pause or continue',
                'ESC  stop - your progress is saved, run the same link again to continue',
                'A  advanced view: live graph, each connection, speed limit',
                'H  close this help')
_HELP_ADV = ('p pause/continue   r continue   esc/q stop safely (progress is saved)',
             '-  cap the speed 25% lower   +  raise the cap   0  remove the cap',
             'up/down pick a connection   x rest or wake it   s sort connections',
             '1 graph   2 connections   3 chunks   4 log   a simple view   h close help')

def _frame_simple(S, W, H, C, G):
    pad = '  '
    w = max(4, min(W - 4, 72))
    size, tot = S['size'], S['tot']
    pct = tot / size if size else 0
    links = S['links']
    L = ['', pad + C['bld'] + C['mauve'] + G['logo'] + 'BandwidthGoblin' + C['rst'], '',
         pad + C['bld'] + S['name'] + C['rst'], '']
    if size:
        L.append(pad + _meter(pct, max(1, w - 6), C['grn'], C, G) + f" {C['bld']}{pct*100:3.0f}%{C['rst']}")
    else:
        L.append(pad + C['dim'] + 'size not known yet - the server did not say' + C['rst'])
    L.append('')
    of = f'{_size(tot)} of {_size(size)}' if size else f'{_size(tot)} so far'
    if S['paused']:
        L.append(pad + C['ylw'] + C['bld'] + 'Paused' + C['rst'] + G['sep'] + of + G['sep'] + 'press P to continue')
    else:
        left = 'finishing up...' if tot >= size else (f"about {_clock(S['eta'])} left" if S['eta'] else 'working out time left...')
        L.append(pad + C['bld'] + C['grn'] + _rate(S['speed']) + C['rst'] + G['sep'] + of + (G['sep'] + left if size else ''))
    live = [l for l in links if l['status'] != 'off']
    if live:
        names = list(dict.fromkeys(_plain_label(l['label']) for l in live))
        word = '1 connection' if len(live) == 1 else f'{len(live)} connections together'
        L.append(pad + C['dim'] + f"Using {word}: {', '.join(names)}" + C['rst'])
    if S['limit']:
        L.append(pad + C['dim'] + f"Speed capped at {_rate(S['limit'])}" + C['rst'])
    if S['msg']:
        L.append(pad + C['ylw'] + S['msg'] + C['rst'])
    L.append('')
    if S['help']:
        L.extend(pad + C['dim'] + h + C['rst'] for h in _HELP_SIMPLE)
        L.append('')
    keys = (('P', 'continue' if S['paused'] else 'pause'), ('ESC', 'stop & save'), ('A', 'advanced'), ('H', 'help'))
    L.append(pad + '   '.join(f"{C['bld']}{C['cyan']}{k}{C['rst']} {C['dim']}{v}{C['rst']}" for k, v in keys))
    return L

def _box(title, lines, w, h, C, G):
    """A titled panel, exactly w x h cells."""
    t = f"{G['h']} {title} " if w > len(title) + 6 else ''
    top = C['box'] + G['tl'] + t + G['h'] * max(0, w - 2 - len(t)) + G['tr'] + C['rst']
    bot = C['box'] + G['bl'] + G['h'] * max(0, w - 2) + G['br'] + C['rst']
    v = C['box'] + G['v'] + C['rst']
    body = [v + _fit(ln, w - 2) + v for ln in (list(lines) + [''] * h)[:max(0, h - 2)]]
    return [top] + body + [bot]

def _links_panel(S, w, h, C, G):
    rows = max(0, h - 3)
    room = max(0, w - 2 - 38)       # what the fixed columns leave over
    name = max(10, min(24, room))
    bar = max(0, min(16, room - name))
    spk = max(0, min(24, room - name - bar - 1))
    head = C['dim'] + f"    {'connection':<{name}} {'':<{bar}} {'speed':>10} {'total':>9} {'conn':>4} {'state':<5}" + C['rst']
    links = S['links']
    top = max(0, S['sel'] - rows + 1)
    fast = max([l['speed'] for l in links] + [1e-6])
    out = [head]
    for i, l in enumerate(links[top:top + rows], top):
        col = C.get(l.get('color'), C['cyan'])
        stc = {'on': C['grn'], 'new': C['cyan'], 'off': C['red'], 'rest': C['ylw']}.get(l['status'], C['dim'])
        mark = C['bld'] + G['sel'] + C['rst'] if i == S['sel'] else ' '
        lbl = _fit(l['label'], name)
        out.append(f"{mark} {stc}{G['dot']}{C['rst']} {col}{lbl}{C['rst']} "
                   f"{_meter(l['speed'] / fast, bar, col, C, G)} {C['bld']}{_rate(l['speed']):>10}{C['rst']} "
                   f"{_size(l['total']):>9} {l['conns']:>4} {stc}{l['status']:<5}{C['rst']}"
                   + (' ' + col + _spark(l['hist'], spk, G) + C['rst'] if spk > 3 else ''))
    if not links:
        out.append(C['dim'] + '   default connection' + C['rst'])
    return out

def _job_panel(S, w, h, C, G):
    size, tot = S['size'], S['tot']
    pct = tot / size if size else 0
    col = C['red'] if pct < .3 else (C['ylw'] if pct < .7 else C['grn'])
    iw = max(1, w - 2)
    n = len(S['chunks'])
    out = [_meter(pct, max(1, iw - 7), col, C, G) + f" {col}{C['bld']}{pct*100:5.1f}%{C['rst']}" if size
           else C['dim'] + 'size unknown' + C['rst'],
           f"{_size(tot)} / {_size(size) if size else '?'}",
           f"eta {C['bld']}{_clock(S['eta']) if S['eta'] else '--'}{C['rst']}  ran {_clock(S['elapsed'])}",
           f"avg {_rate(S['avg'])}  peak {_rate(S['peak'])}",
           f"cap {_rate(S['limit']) if S['limit'] else 'off'}  chunks {S['ndone']}/{n}"]
    rows = max(0, h - 2 - len(out))
    cells = iw * rows
    if rows and n:
        ch = S['chunks']
        if n > cells:
            # More chunks than cells: one cell = a group; done only if all are.
            ch = [(lambda g: 'done' if all(c == 'done' for c in g) else
                   next((c for c in g if isinstance(c, int)), 'wait'))(ch[i * n // cells:max(i * n // cells + 1, (i + 1) * n // cells)])
                  for i in range(cells)]
        palette = [C[k] for k in ('sky', 'peach', 'pink', 'teal', 'cyan', 'ylw')]
        for r in range(rows):
            seg = ch[r * iw:(r + 1) * iw]
            if not seg:
                break
            out.append(''.join(C['grn'] + G['done'] + C['rst'] if c == 'done'
                               else (C['dim'] + G['wait'] + C['rst'] if c == 'wait'
                                     else palette[c % len(palette)] + G['run'] + C['rst']) for c in seg))
    return out

def _frame_adv(S, W, H, C, G):
    state = C['ylw'] + C['bld'] + 'PAUSED' + C['rst'] if S['paused'] else C['grn'] + C['bld'] + 'RUNNING' + C['rst']
    head = (' ' + C['bld'] + C['mauve'] + 'BandwidthGoblin' + C['rst'] + C['dim'] + ' ' + S['ver'] + C['rst'] + '  ' + state
            + '  ' + C['bld'] + _rate(S['speed']) + C['rst'] + '  ' + C['dim'] + S['name'] + C['rst'])
    pairs = (('p', 'continue' if S['paused'] else 'pause'), ('esc', 'stop'), ('a', 'simple'), ('h', 'help'),
             ('+-', 'cap'), (G['up'], 'pick'), ('x', 'rest'), ('s', 'sort'), ('1-4', 'panels'))
    foot = ' ' + ' '.join(f"{C['bld']}{C['cyan']}{k}{C['rst']}{C['dim']} {v}{C['rst']}" for k, v in pairs)
    avail = H - 2
    P = S['panels']
    show_log = 'log' in P and avail >= 18
    show_net = 'net' in P and avail >= 10
    log_h = (7 if avail >= 30 else 5) if show_log else 0
    show_mid = 'links' in P or 'chunks' in P
    net_h = 0
    if show_net:
        net_h = avail - log_h if not show_mid else max(5, int((avail - log_h) * 0.38))
    mid_h = avail - net_h - log_h
    if not show_mid and not show_net:
        log_h, mid_h = (avail if show_log else 0), 0
    L = [head]
    if net_h:
        gh = net_h - 2
        title = f"1 net  {_rate(S['speed'])} now  top of graph {_rate(max(list(S['hist'])[-(W * 2):] + [0]))}"
        L += _box(title, _graph(S['hist'], W - 2, gh, C, G), W, net_h, C, G)
    if mid_h >= 3:
        if 'links' in P and 'chunks' in P:
            rw = max(24, min(W // 2, 38))
            lw = W - rw
            a = _box('2 links', _links_panel(S, lw, mid_h, C, G), lw, mid_h, C, G)
            b = _box('3 chunks', _job_panel(S, rw, mid_h, C, G), rw, mid_h, C, G)
            L += [x + y for x, y in zip(a, b)]
        elif 'links' in P:
            L += _box('2 links', _links_panel(S, W, mid_h, C, G), W, mid_h, C, G)
        elif 'chunks' in P:
            L += _box('3 chunks', _job_panel(S, W, mid_h, C, G), W, mid_h, C, G)
    if log_h:
        if S['help']:
            L += _box('help', _HELP_ADV, W, log_h, C, G)
        else:
            rows = list(S['log'])[-max(0, log_h - 2):]
            L += _box('4 log', [C['dim'] + ln + C['rst'] for ln in rows], W, log_h, C, G)
    elif S['help']:
        L[1:1 + len(_HELP_ADV)] = list(_HELP_ADV)
    if S['msg']:
        foot = ' ' + C['ylw'] + S['msg'] + C['rst']
    L = (L + [''] * H)[:H - 1] + [foot]
    return L

def _frame(S, W, H, C, G):
    """Pure renderer: engine snapshot -> exactly H lines, each at most W cells
    wide. Advanced falls back to simple when the window cannot hold its panels."""
    adv = S.get('mode') == 'advanced' and W >= 60 and H >= 14
    lines = _frame_adv(S, W, H, C, G) if adv else _frame_simple(S, W, H, C, G)
    if len(lines) > H:
        lines = lines[:H - 1] + lines[-1:] if H > 1 else lines[-1:]
    return [_fit(ln, W).rstrip(' ') for ln in lines] + [''] * (H - len(lines))

_SCREEN_ON = False

def _screen_restore():
    global _SCREEN_ON
    if _SCREEN_ON:
        _SCREEN_ON = False
        try:
            sys.stdout.write('\x1b[?7h\x1b[?25h\x1b[?1049l')
            sys.stdout.flush()
        except Exception:
            pass

atexit.register(_screen_restore)

def _say(msg):
    """Print a message the user must not miss: while the full-screen view is up
    it waits until that view closes, otherwise it would vanish with it."""
    if _SCREEN_ON:
        POST_DASH.append(msg)
    else:
        print(msg, flush=True)

def _flush_said():
    while POST_DASH:
        print(POST_DASH.pop(0), flush=True)

def dash(out, use, nchunks):
    """Live full-screen view. Samples the engine, hands a snapshot to the pure
    _frame() renderer, and keeps its own hands off the download."""
    global DASH_ON, _SCREEN_ON
    if os.name == 'nt':
        try: os.system('')
        except Exception: pass
    C, G = _tui_style()
    rid = G_RUNID
    ema_tot = 0.0
    peak = 0.0
    with TOTAL_DONE_LOCK: prev_tot = TOTAL_DONE
    start_tot = prev_tot
    with LINK_LOCK: prev_links = dict(LINK_DONE)
    prev_t = time.time()
    name = os.path.basename(out)
    colours = ['sky', 'peach', 'pink', 'teal', 'cyan', 'ylw']
    _TUI['use'] = use
    _SCREEN_ON = True
    try:
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J')
        while not STOP and not RUN_DONE.is_set() and G_RUNID == rid:
            time.sleep(0.25)
            with TOTAL_DONE_LOCK: tot = TOTAL_DONE
            if TOTAL_SIZE: tot = min(tot, TOTAL_SIZE)
            now = time.time(); dt = max(now - prev_t, 1e-6)
            inst = max(0.0, (tot - prev_tot) / dt)
            ema_tot = inst if ema_tot == 0 else ema_tot * 0.7 + inst * 0.3
            peak = max(peak, ema_tot)
            prev_tot, prev_t = tot, now
            SPARK.append(ema_tot)
            _TUI['speed'] = ema_tot
            with LINK_LOCK: cur = dict(LINK_DONE)
            with G_LOCK:
                ips = list(use)
                act = dict(ACTIVE)
            order = {ip: i for i, ip in enumerate(ips)}
            links = []
            for ip in ips:
                v = (cur.get(ip, 0) - prev_links.get(ip, 0)) / dt
                e = DASH_EMA.get(ip, 0.0)
                e = v if e == 0 else e * 0.7 + v * 0.3
                DASH_EMA[ip] = e
                LINK_HIST.setdefault(ip, collections.deque(maxlen=120)).append(e)
                st = 'rest' if ip in BENCHED else ('off' if not ip_ok(ip) else
                     ('new' if now - LINK_JOINED.get(ip, 0) < 60 else 'on'))
                links.append({'ip': ip, 'label': link_label(ip) if ip else 'Default connection', 'speed': e,
                              'total': cur.get(ip, 0), 'conns': act.get(ip, 0), 'status': st,
                              'hist': LINK_HIST[ip], 'color': colours[order[ip] % len(colours)]})
            prev_links = cur
            srt = _TUI['sort']
            if srt == 1: links.sort(key=lambda l: -l['speed'])
            elif srt == 2: links.sort(key=lambda l: -l['total'])
            _TUI['sel'] = max(0, min(_TUI['sel'], len(links) - 1))
            _TUI['sel_ip'] = links[_TUI['sel']]['ip'] if links else None
            with CHUNK_LOCK:
                states = [CHUNK_STATE.get(i) for i in range(nchunks)]
            chunks = ['done' if s == 'done' else ('wait' if s in (None, '.') else order.get(s, 0)) for s in states]
            el = now - T0
            if _TUI['help'] and now > _TUI['help_until']:
                _TUI['help'] = False
            S = {'name': name, 'tot': tot, 'size': TOTAL_SIZE, 'speed': ema_tot, 'peak': peak,
                 'avg': (tot - start_tot) / max(el, 1e-6),
                 'eta': (TOTAL_SIZE - tot) / ema_tot if ema_tot > 1000 and TOTAL_SIZE and (TOTAL_SIZE - tot) / ema_tot < 360000 else 0,
                 'elapsed': el, 'paused': PAUSED, 'limit': SPEEDLIMIT, 'ver': f'v{VERSION} b{BUILD}',
                 'links': links, 'chunks': chunks, 'ndone': chunks.count('done'), 'hist': SPARK,
                 'log': RING, 'help': _TUI['help'], 'panels': _TUI['panels'], 'sel': _TUI['sel'],
                 'sort': srt, 'msg': _TUI['msg'] if now < _TUI['msg_until'] else '', 'mode': UI_MODE}
            try:
                sz = shutil.get_terminal_size((100, 30))
                fr = _frame(S, sz.columns, sz.lines, C, G)
                sys.stdout.write('\x1b[?2026h' + ''.join(f'\x1b[{i};1H{ln}\x1b[K' for i, ln in enumerate(fr, 1)) + '\x1b[?2026l')
                sys.stdout.flush()
            except Exception as ex:
                DASH_ON = False
                log(f'dashboard disabled, plain logs: {ex}')
                break
    finally:
        _screen_restore()

def auto_tune():
    import time as _t
    last = {}
    rid = G_RUNID
    while not STOP and not RUN_DONE.is_set() and G_RUNID == rid:
        _t.sleep(20)
        if STOP or RUN_DONE.is_set() or G_RUNID != rid:
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
                log(f'auto-tune {(ip or "link")}: tuning cap {cap + 1} (throttle-free, 1 connection per link)')
            elif w > 15 and cap > 1:
                with G_LOCK:
                    WORKER_CAP[ip] = cap - 1
                    LINK_THREADS[ip] = cap - 1
                log(f'auto-tune {(ip or "link")}: tuning cap {cap - 1} (throttled x{w})')
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

def _requeue_link(ip, q):
    """Hand a dropped link's chunks back to the queue from where they got to. The
    bytes before that point are already saved and counted; restarting from the
    chunk's start would waste them and push the progress total past the real one."""
    orph = []
    with CHUNK_LOCK:
        for idx, stt in list(CHUNK_STATE.items()):
            if stt == ip:
                s, e = CHUNK_RANGE.get(idx, (0, 0))
                if isinstance(e, int):
                    CHUNK_OWNER[idx] = ('gone', time.time())
                    CHUNK_END[idx] = s - 1
                    CHUNK_STATE[idx] = '.'
                    orph.append((idx, max(CHUNK_POS.get(idx, s), s), e))
    for item in orph:
        q.put(item)

def membership(q, out, per_link, use, ex, rid):
    import time as _t
    while not STOP and not RUN_DONE.is_set() and G_RUNID == rid:
        # Every look spawns ipconfig/ip; 5s still catches a hot-plug promptly.
        for _ in range(10):
            _t.sleep(0.5)
            if STOP or RUN_DONE.is_set() or G_RUNID != rid:
                return
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
            # Every mutation of `use` must sit under G_LOCK: this thread runs
            # concurrently with run_one's `_NoRange` handler and the dashboard,
            # which both iterate `use` under G_LOCK (build 47 audit C1 - an
            # unlocked append racing a comprehension raised "list changed size
            # during iteration", crashing the run and losing the resume).
            with G_LOCK:
                use.append(ip)
                LINK_THREADS[ip] = per_link
            LINK_JOINED[ip] = time.time()
            if STOP or RUN_DONE.is_set():
                break   # executor is shutting down - spawning now would just error (build 47 polish)
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
            _requeue_link(ip, q)
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
    try:
        _worker_loop(out, ip, q)
    finally:
        _close_out_fd()

def _worker_loop(out, ip, q):
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
    rec = dict(rec)
    rec['t'] = time.time()
    rec['build'] = BUILD
    _bg_put(lambda: _perf_append(rec))

def _perf_append(rec):
    try:
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

def _mini_help():
    print(CYLW('\nwhat BandwidthGoblin can do (no guide needed):'), flush=True)
    print('  paste a link            - downloads over EVERY connection you have at once')
    print('  YouTube link            - auto-downloads (Enter = best, or type 1080/720/480/audio)')
    print('  [P]                     - pause / continue a download in progress')
    print('  [A]                     - switch between the simple and advanced view')
    print('  [H]                     - help right on the download screen')
    print('  [ESC]                   - stop safely (progress is saved - rerun to continue)')
    print('  --yt URL 1080           - one-line YouTube download')
    print('  --queue list.txt        - batch many links, one per line')
    print('  --limit 5               - cap the combined speed at 5 MB/s')
    print('  --links 0,1             - pick which connections to combine')
    print('  --dry-run URL           - inspect a link without downloading it')
    print('  --selftest              - quick self-check that everything works')
    print('  --help                  - the full option list\n', flush=True)

def _no_link_msg(v):
    if 'magnet:' in (v or '').lower() or (v or '').lower().rstrip('"\' ').endswith('.torrent'):
        return "torrents aren't supported yet - paste a direct download or YouTube link."
    return 'no link found - please copy a link (starts with http) and paste it.'

def acquire_urls():
    global _INTRO_SHOWN
    if not _INTRO_SHOWN:
        _INTRO_SHOWN = True
        print('  ' + CDIM("paste a link to download it over every connection you have. Type 'help' for more."))
    while True:
        clip = get_clipboard()
        cl = extract_links(clip)
        if cl:
            first = cl[0] if len(cl[0]) <= 80 else cl[0][:80] + '...'
            extra = f' (+{len(cl)-1} more)' if len(cl) > 1 else ''
            v = ask(f'clipboard link:\n  {first}{extra}\n[Enter] use / [p] paste different / [q] quit: ')
            if v.strip().lower() in ('help', 'h', 'menu'):
                _mini_help()
                continue
            if v.lower() == 'q':
                return None
            if v.lower() in ('p', 'paste', 'n', 'no'):
                v2 = ask('paste link(s) (or q to quit): ')
                if (v2 or '').strip().lower() in ('help', 'h', 'menu'):
                    _mini_help()
                    continue
                if (v2 or '').lower() == 'q' or not v2:
                    return None
                links = extract_links(v2)
                if links:
                    return links
                print(CNO(_no_link_msg(v2)))
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
            if (v or '').strip().lower() in ('help', 'h', 'menu'):
                _mini_help()
                continue
            if (v or '').lower() == 'q' or not v:
                return None
            links = extract_links(v)
            if links:
                return links
            print(CNO(_no_link_msg(v)))

def reset_state():
    global LOGFILE, T0, ORIG_URL, FINAL_URL, TOTAL_SIZE, TOTAL_DONE, DASH_ON, STOP, DASH_MODE
    global LINK_DONE, CHUNK_STATE, REDIR, IP_STATE, DASH_EMA, CHUNK_TRIES, G_RUNID, LINK_JOINED, G_FUTS, G_STARTED, CHUNK_STEALTIME, ACTIVE, WORKER_CAP, STAT429, STEAL_MIN, G429_N, G429_COOLDOWN
    global STOP_REASON, LAST_PROGRESS, V_ETAG, V_LM, RANGE_VERIFIED, EXPECT_SHA, REFRESH_NOOP, CHUNK_STEAL_N, RETRY_SINGLE
    global _NEIGH_T, PAUSED, LINK_SPEED, STOP_USER
    global VALIDATOR_BROKEN
    # Build 30 audit fixes: a paused run must not leak into the next one (the
    # next download would start frozen forever - every worker stalls on
    # PAUSED), and history-learned speeds must not ghost across runs.
    PAUSED = False
    LINK_SPEED = {}
    STOP_REASON = 'ESC'; STOP_USER = False; RUN_DONE.set(); LAST_PROGRESS = 0.0
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
    CHUNK_POS.clear(); CHUNK_RANGE.clear(); CHUNK_OWNER.clear(); CHUNK_END.clear()
    LINK_HIST.clear(); BENCHED.clear(); POST_DASH.clear()
    _TUI['sel'] = 0; _TUI['msg'] = ''; _TUI['help'] = False
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

def _safe_filename(name):
    """A server-supplied name -> a plain file name: never a path that escapes the
    save folder (a/../../x), never characters or device names Windows refuses."""
    name = re.split(r'[\\/]', name or '')[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name).strip().rstrip('. ')
    if name in ('', '.', '..'):
        return ''
    if re.match(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', name, re.I):
        name = '_' + name
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[:200 - len(ext)] + ext
    return name

def guess_name(url):
    try:
        p = urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]
        p = _safe_filename(urllib.parse.unquote(p))
        if p and p.lower() not in ('videoplayback', 'download', 'get', 'file'):
            return p
    except Exception:
        pass
    return ''

def suggest_name(url):
    """guess_name, plus asking the server when the URL itself names no file
    (/download?id=7): its Content-Disposition, or where the redirects end up."""
    n = guess_name(url)
    if n:
        return n
    try:
        final, _s, _a, meta = head(url)
        return meta.get('name') or guess_name(final)
    except Exception:
        return ''

def _clean_path(p):
    """Drag-and-drop pastes a path in quotes, and ~ means home everywhere."""
    return os.path.expanduser((p or '').strip().strip('"\''))

def _unused_path(out):
    stem, ext = os.path.splitext(out)
    k = 2
    while os.path.exists(f'{stem} ({k}){ext}'):
        k += 1
    return f'{stem} ({k}){ext}'

def _load_perf():
    _bg_flush()
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
    print('  all links maxed in one go: --priority high (4 connections on every link)')

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

_VALUE_FLAGS = ('queue', 'out-dir', 'referer', 'cookie', 'sha', 'header', 'limit', 'links', 'jobs', 'verify', 'yt', 'priority')

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
            elif k in ('dry-run', 'info', 'history', 'resume', 'quiet', 'no-log', 'speedtest', 'selftest', 'overwrite', 'advanced', 'simple'):
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
        with open(path, encoding='utf-8-sig', errors='replace') as f:   # Notepad adds a BOM
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
    rec = dict(rec)
    rec['t'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _bg_put(lambda: _hist_append(rec))

def _hist_append(rec):
    try:
        with PERF_LOCK:
            with open(_hist_path(), 'a') as f:
                f.write(json.dumps(rec) + '\n')
    except Exception:
        pass

def _print_history(n=10):
    _bg_flush()
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
                # YouTube stream parts resume through the video link, not by themselves
                if not fn.endswith('.resume') or fn.endswith('.tmp.resume'):
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
            '  bandwidthgoblin.py --yt URL [best|2160|1440|1080|720|480|audio]\n'
            '                                      YouTube: paste a youtube.com / youtu.be link\n'
            '                                      (interactive mode also auto-detects YouTube links;\n'
            '                                      streams split over ALL your connections + merged)\n'
            '  feedback / keys: type "help" at any prompt for the feature list; during a download\n'
            '                                      P pause | A advanced view | H help | ESC stop safely\n'
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
            '  --priority high   HIGH tier on EVERY link: 4 connections + start caps\n'
            '                 maxed (normal = 1 connection/link, =MULTILINK_PRIORITY=high).\n'
            '                 Helps hosts that don\'t throttle per source IP; YouTube\n'
            '                 throttles per IP, so link count matters most there.\n'
            '  --advanced / --simple   pick the download view (remembered; A switches live)\n'
            '  --overwrite   replace an existing file (default: keep it, save as "name (2).ext")\n'
            '  --open / --no-open   --history   --resume   --speedtest\n'
            '  --quiet (milestones only)   --no-log (no disk logs)\n'
            '  --jobs N   download a --queue in parallel (1-8 files at once);\n'
            '             each queue line may be:  URL  or  URL  output-name.ext\n'
            '  --verify <manifest|dir>  check files against sha256sum checksums\n'
            '             (sha256sum-format file, or a dir with SHA256SUMS / *.sha256)\n'
            '  --selftest run the built-in offline QA (local ranged server, 2 links, sha)\n'
            '  env also: MULTILINK_IFACES=0,1  MULTILINK_NODASH=1  BANDWIDTHGOBLIN_SPEEDTEST_MB=8\n'
            '  BANDWIDTHGOBLIN_KEEP_LOOPBACK=1 keeps loopback/link-local interfaces (usually\n'
            '          dropped - they are not separate physical routes; use for LAN tests only)')

# ---------------------------------------------------------------------------
# YouTube mode (build 44, split in build 45): paste any youtube.com / youtu.be
# link. BandwidthGoblin fetches the proven yt-dlp engine once (cached in
# tools/ next to this file), then downloads the video at best quality - or a
# user-picked height/audio. ffmpeg (if present on PATH or as tools/ffmpeg)
# enables full-quality video+audio merging; without it the best single-file
# format is used. Build 45: with ffmpeg available, the raw DASH streams are
# downloaded through BandwidthGoblin's own multi-link engine (split over every
# connection, resumable, verified) and merged locally - YouTube gets the same
# all-networks treatment as any other link. Streams run sequentially
# in-process to avoid global-state races; failures fall back to plain yt-dlp.
# ---------------------------------------------------------------------------

_YT_HOSTS = ('youtube.com', 'youtu.be')
_YT_PRESET_KEYS = ('2160', '1440', '1080', '720', '480')
# Build 47: prefer direct-HTTP formats for the multi-link split. The
# unpinned "best" can be a visionos HLS (m3u8) stream on some videos - which
# is a playlist of segments and can't be range-split, so the run silently
# falls to single-connection yt-dlp with no dashboard. Excluding m3u8 up
# front picks the best splittable (https) format; the old selector stays as
# the fallback for videos that only offer HLS.
_YT_FMT = {
    '2160': 'bv*[height<=2160][protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*[height<=2160]+ba/b',
    '1440': 'bv*[height<=1440][protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*[height<=1440]+ba/b',
    '1080': 'bv*[height<=1080][protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*[height<=1080]+ba/b',
    '720': 'bv*[height<=720][protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*[height<=720]+ba/b',
    '480': 'bv*[height<=480][protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*[height<=480]+ba/b',
}

def is_youtube(u):
    try:
        h = (urllib.parse.urlparse(u or '').hostname or '').lower()
        return any(h == x or h.endswith('.' + x) for x in _YT_HOSTS)
    except Exception:
        return False

def _yt_bin_path():
    try:
        return os.path.join(_script_dir(), 'tools',
                            'yt-dlp.exe' if os.name == 'nt' else 'yt-dlp')
    except Exception:
        return 'tools/yt-dlp.exe' if os.name == 'nt' else 'tools/yt-dlp'

def _yt_download_url():
    if os.name == 'nt':
        return 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe'
    if sys.platform == 'darwin':
        return 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos'
    return 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux'

def _yt_engine():
    """Make sure the yt-dlp engine exists (fetched once, ~30-40 MB, cached in
    tools/). A missing engine is the only time a YouTube download touches the
    network for anything other than the video itself."""
    if os.path.isfile(_yt_bin_path()) and os.path.getsize(_yt_bin_path()) > 1_000_000:
        return _yt_bin_path()
    try:
        os.makedirs(os.path.dirname(_yt_bin_path()), exist_ok=True)
    except Exception:
        pass
    tmp = _yt_bin_path() + '.part'
    print(f'👺 first YouTube download: fetching the downloader engine ({os.path.basename(_yt_bin_path())})...', flush=True)
    try:
        req = urllib.request.Request(_yt_download_url(), headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=90) as r, open(tmp, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            done = 0
            while True:
                b = r.read(1 << 16)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if total:
                    print(f'\r  {done / (1 << 20):.1f} / {total / (1 << 20):.1f} MB  ({100 * done // total}%)',
                          end='', flush=True)
        print()
        os.replace(tmp, _yt_bin_path())
        if os.name != 'nt':
            try:
                os.chmod(_yt_bin_path(), 0o755)
            except Exception:
                pass
        return _yt_bin_path()
    except Exception as ex:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        print(CNO(f'could not download the YouTube engine: {ex}') + CDIM(
            '\nFix: download yt-dlp yourself from https://github.com/yt-dlp/yt-dlp/releases\n'
            f'and save it as: {_yt_bin_path()}'))
        return None

def _yt_pick(url, headless, quality):
    """Return (format_string, is_audio, quality_label)."""
    q = (quality or '').strip().lower()
    if not q and not headless:
        q = (ask('quality? [Enter = best / 2160 / 1440 / 1080 / 720 / 480 / audio]: ') or '').strip().lower()
    if q in ('a', 'audio', 'mp3'):
        return 'ba/b', True, 'audio-only'
    if q in _YT_FMT:
        return _YT_FMT[q], False, f'up to {q}p'
    return ('bv*[protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*+ba/b', False, 'best available')

def _deno_bin():
    return os.path.join(_script_dir(), 'tools', 'deno.exe' if os.name == 'nt' else 'deno')

def _deno_download_url():
    arch = 'aarch64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x86_64'
    if os.name == 'nt':
        return f'https://github.com/denoland/deno/releases/latest/download/deno-{arch}-pc-windows-msvc.zip'
    if sys.platform == 'darwin':
        return f'https://github.com/denoland/deno/releases/latest/download/deno-{arch}-apple-darwin.zip'
    return f'https://github.com/denoland/deno/releases/latest/download/deno-{arch}-unknown-linux-gnu.zip'

def _deno_engine():
    """YouTube extracts need a JavaScript runtime nowadays (yt-dlp deprecation
    of JS-free extraction). deno is a single binary - fetched once into tools/."""
    b = _deno_bin()
    if os.path.isfile(b) and os.path.getsize(b) > 5_000_000:
        return b
    try:
        os.makedirs(os.path.dirname(b), exist_ok=True)
    except Exception:
        pass
    z = b + '.zip'
    print(f'👺 first YouTube download: fetching the JS runtime engine ({os.path.basename(b)}) - one time only...', flush=True)
    try:
        req = urllib.request.Request(_deno_download_url(), headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=120) as r, open(z, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f'\r  {done / (1 << 20):.1f} / {total / (1 << 20):.1f} MB  ({100 * done // total}%)',
                          end='', flush=True)
        print('  extracting...')
        with zipfile.ZipFile(z) as zf:
            hit = [n for n in zf.namelist() if n.endswith('deno.exe' if os.name == 'nt' else 'deno')]
            if not hit:
                raise RuntimeError('deno not found in archive')
            with zf.open(hit[0]) as src, open(b, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        try:
            os.remove(z)
        except Exception:
            pass
        if os.name != 'nt':
            try:
                os.chmod(b, 0o755)
            except Exception:
                pass
        return b
    except Exception as ex:
        try:
            if os.path.exists(z):
                os.remove(z)
        except Exception:
            pass
        print(CDIM(f'(JS runtime unavailable: {ex})'))
        return None

def _yt_ffmpeg():
    """Locate ffmpeg (needed to merge full-quality video + audio into one
    file). Prefers an existing install on PATH; otherwise fetches the official
    static Windows build (gyan.dev) into tools/ once. On macOS/Linux without a
    system ffmpeg it returns None (best single-file fallback is used)."""
    try:
        w = shutil.which('ffmpeg')
        if w:
            return w
    except Exception:
        pass
    cand = os.path.join(_script_dir(), 'tools', 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if os.path.isfile(cand) and os.path.getsize(cand) > 5_000_000:
        return cand
    if os.name != 'nt':
        return None  # mac/linux - point users at brew/apt instead of bundling
    try:
        os.makedirs(os.path.dirname(cand), exist_ok=True)
    except Exception:
        pass
    z = cand + '.zip'
    print('👺 first YouTube download: fetching ffmpeg (merges 1080p/4K video + audio) - one time only...', flush=True)
    try:
        req = urllib.request.Request('https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
                                     headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=120) as r, open(z, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f'\r  {done / (1 << 20):.1f} / {total / (1 << 20):.1f} MB  ({100 * done // total}%)',
                          end='', flush=True)
        print('  extracting ffmpeg...')
        with zipfile.ZipFile(z) as zf:
            hit = [n for n in zf.namelist() if n.endswith('/ffmpeg.exe')]
            if not hit:
                raise RuntimeError('ffmpeg.exe not found in archive')
            with zf.open(hit[0]) as src, open(cand, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        try:
            os.remove(z)
        except Exception:
            pass
        return cand
    except Exception as ex:
        try:
            if os.path.exists(z):
                os.remove(z)
        except Exception:
            pass
        print(CDIM(f'(ffmpeg unavailable: {ex})'))
        return None

def _yt_json(exe, fmt, url, deno):
    """Ask yt-dlp for the concrete media streams (simulate mode) so the
    multi-link engine can grab them directly. Returns (info_dict, None) or
    (None, reason)."""
    cmd = [exe, '--no-playlist', '-f', fmt, '-J', '--no-check-formats', url]
    if deno:
        cmd += ['--js-runtimes', f'deno:{deno}']
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=180)
    except Exception as ex:
        return None, str(ex)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout)[-240:]
    try:
        return json.loads(p.stdout), None
    except Exception:
        return None, 'yt-dlp JSON parse failed'

def _yt_subs(exe, url, od, deno, ff, info=None):
    """Best-effort: drop exactly ONE .srt next to the video - the video's
    original language (the uploader's manual track, which YouTube lists first)
    when one exists, else English; auto-generated captions only when there is
    no manual track. Override with MULTILINK_SUB_LANGS=hi.*. The info dict
    from the split path lets us skip the network call entirely when the video
    has no subtitles at all. Never fails the download."""
    if info is not None:
        man = list((info.get('subtitles') or {}).keys())
        auto = list((info.get('automatic_captions') or {}).keys())
        if not man and not auto:
            return False
        env = os.environ.get('MULTILINK_SUB_LANGS')
        if env:
            lang = env
        else:
            own = (info.get('language') or '').lower()
            base = own.split('-')[0] if own else ''
            if base and base in man:
                lang = base                      # exact code, manual track
            elif own and own in auto:
                lang = own
            elif base:
                m2 = [k for k in man if k and k.split('-')[0] == base]
                a2 = [k for k in auto if k and k.split('-')[0] == base]
                lang = (m2 or a2 or man or auto or ['en'])[0]   # manual first (audit M5)
            elif man:
                lang = man[0]
            else:
                lang = auto[0] if auto else 'en'
        wanted = lang
        use_auto = lang not in man       # manual track preferred over auto
    else:
        # Direct/fallback path: no info dict - let yt-dlp pick (manual first,
        # auto as fallback) and keep just one of whatever files land.
        env = os.environ.get('MULTILINK_SUB_LANGS')
        wanted = env or 'en.*'
        use_auto = True
    print(CDIM(f'  subtitles: checking for .srt ({wanted})...'), flush=True)
    cmd = [exe, '--no-playlist', '--skip-download',
           '--write-subs', '--sub-langs', wanted,
           '--sub-format', 'srt/best',
           '-o', os.path.join(od, '%(title)s [%(id)s].%(ext)s'), url]
    if use_auto:
        cmd += ['--write-auto-subs']
    if deno:
        cmd += ['--js-runtimes', f'deno:{deno}']
    if ff:
        cmd += ['--ffmpeg-location', ff, '--convert-subs', 'srt']
    t0 = time.time()
    try:
        subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=120)
    except Exception:
        return False
    new = sorted(f for f in glob.glob(os.path.join(od, '*.srt')) +
                 glob.glob(os.path.join(od, '*.vtt'))
                 if os.path.exists(f) and os.path.getmtime(f) >= t0 - 1)
    if len(new) > 1:
        # keep the manual track - auto-generated files carry a "-" in the name
        new.sort(key=lambda f: (os.path.basename(f).count('-'),
                                len(os.path.basename(f))))
        new = new[:1]
    for f in new:
        print(CDIM(f'  subtitles: {os.path.basename(f)}'), flush=True)
    if not new:
        print(CDIM(f'  no subtitles (wanted: {wanted})'), flush=True)
    return bool(new)

def _yt_split_download(info, od, ff, eflags):
    """Download the requested YouTube streams through BandwidthGoblin's own
    multi-link engine - chunk-split over every connection, resumable - then
    merge video + audio locally with ffmpeg. Returns (True, final_path) on
    success, or (None, reason) so the caller can fall back to plain yt-dlp."""
    try:
        rds = info.get('requested_downloads') or [{}]
        rd = rds[0] if len(rds) == 1 else {}
        streams = rd.get('requested_formats') or []
    except Exception:
        return None, 'no requested formats'
    if not streams:
        return None, 'no requested formats'
    good = []
    for s in streams:
        u = s.get('url') or ''
        proto = s.get('protocol') or ''
        if not (u.startswith('http://') or u.startswith('https://')):
            return None, f'non-HTTP stream ({proto})'
        if '.m3u8' in u.lower() or proto.startswith('m3u8'):
            return None, 'HLS stream (not range-splittable)'
        if not is_link(u):
            return None, 'stream URL not download-able'
        good.append((s.get('format_id') or '?', s.get('ext') or 'bin', u))
    title = str(info.get('title') or info.get('id') or 'video').strip()
    vid = str(info.get('id') or 'yt')
    safe = re.sub(r'[<>:"/\\|?*]', '_', title).strip().rstrip('. ')
    if not safe:
        safe = 'video'
    if safe.upper() in ('CON', 'PRN', 'AUX', 'NUL') or re.match(r'(COM|LPT)[1-9]$', safe, re.I):
        safe = '_' + safe
    ext = str(rd.get('ext') or '')
    if ext not in ('mp4', 'mkv', 'webm'):
        ext = 'mkv' if len(good) == 2 else 'mp4'
    final = os.path.join(od, f'{safe} [{vid}].{ext}')
    if os.path.isfile(final) and os.path.getsize(final) > 1000:
        print(CDIM(f'  {os.path.basename(final)} is already there - skipping.'))
        return True, final
    tmp = [os.path.join(od, f'.{vid}_s{i}.tmp') for i in range(len(good))]
    n = '1 stream' if len(good) == 1 else f'{len(good)} streams (video + audio)'
    print(f'👺 multi-link engine: splitting {n} over every connection you have...', flush=True)

    def _clean_tmp():
        for t in tmp:
            try:
                if os.path.exists(t):
                    os.remove(t)
                if os.path.exists(t + '.resume'):
                    os.remove(t + '.resume')
                if os.path.exists(t + '.progress'):
                    os.remove(t + '.progress')
            except Exception:
                pass

    try:
        for i, (fid, fext, u) in enumerate(good):
            if STOP:
                # Keep every partial + run_one's own .resume manifest on ANY
                # stop: the next run continues the streams. Build 47 audit M4:
                # _clean_tmp() used to delete the resume the instant run_one
                # saved it, then _yt_run promised "progress is preserved" - a lie.
                return None, 'stopped'
            print(CYLW(f'  stream {fid} ({fext}): all links, size-aware chunks...'), flush=True)
            reset_state()
            res = run_one(u, tmp[i], ['auto', 'auto'] + eflags)
            if res == 'retry-single':
                log('single-stream retry after multi-link failure (youtube stream)')
                reset_state()
                res = run_one(u, tmp[i], ['1', '1'] + eflags)
            if res == 'disk-space':
                return None, 'not enough disk space for this stream'
            if STOP and STOP_USER:
                return None, 'stopped'   # partials + .resume stay on disk for the next run
            if res == 'error' or res is None:
                _clean_tmp()
                return None, f'stream {fid} download failed'
            if not os.path.isfile(tmp[i]) or os.path.getsize(tmp[i]) < 100:
                _clean_tmp()
                return None, f'stream {fid} produced no file'
        if STOP:
            return None, 'stopped'   # both streams are complete - keep them; next run merges
        if len(good) == 1:
            os.replace(tmp[0], final)
            try:
                print(CYES(f'DONE: {os.path.basename(final)} ({os.path.getsize(final) / 1048576:.1f} MB)'), flush=True)
            except Exception:
                pass
            return True, final
        # two streams: merge with the bundled ffmpeg (copy, no re-encode)
        m = _yticks('merging video + audio (copy, no re-encode)',
                    lambda: subprocess.run([ff, '-y', '-i', tmp[0], '-i', tmp[1], '-c', 'copy', final],
                                           capture_output=True))
        if m.returncode != 0:
            final2 = os.path.join(od, f'{safe} [{vid}].mkv')
            m2 = _yticks('merging video + audio (copy, no re-encode)',
                         lambda: subprocess.run([ff, '-y', '-i', tmp[0], '-i', tmp[1], '-c', 'copy', final2],
                                                capture_output=True))
            if m2.returncode != 0:
                _clean_tmp()
                return None, 'ffmpeg merge failed'
            final = final2
        _clean_tmp()
        try:
            print(CYES(f'DONE: {os.path.basename(final)} ({os.path.getsize(final) / 1048576:.1f} MB), merged from {len(good)} streams'), flush=True)
        except Exception:
            pass
        return True, final
    except Exception as ex:
        _clean_tmp()
        return None, str(ex) or 'split failed'

def _yt_direct(exe, fmt, url, od, deno, ff, is_audio):
    """Plain yt-dlp download (used for audio-only and as the fallback when the
    multi-link split is not possible). Returns True on success."""
    outtmpl = os.path.join(od, '%(title)s [%(id)s].%(ext)s')
    cmd = [exe, '-f', fmt, '--no-playlist', '--newline', '-o', outtmpl, url]
    if deno:
        cmd += ['--js-runtimes', f'deno:{deno}']
    if ff:
        cmd += ['--ffmpeg-location', ff]
    if is_audio:
        if ff:
            cmd += ['-x', '--audio-format', 'mp3']
        else:
            print(CYLW('no ffmpeg - keeping the best audio stream as-is instead of converting to mp3.'))
    kind = 'audio' if is_audio else 'download'
    try:
        p = subprocess.run(cmd)
        if p.returncode != 0:
            print(CNO(f'YouTube {kind} failed.') + CDIM(
                '\nIf the error mentions "Sign in to confirm you are not a bot", the video is '
                'region/age-locked - try a different video, or watch it once in a browser first.'))
        elif not is_audio:
            _yt_subs(exe, url, od, deno, ff)
        return p.returncode == 0
    except Exception as ex:
        print(CNO(f'YouTube {kind} failed: {ex}'))
        return False

def _yt_nospace(got, od):
    """A save-drive shortfall cannot be solved by ANY engine - a single-
    connection yt-dlp run needs the same bytes and would just fail partway (or
    fill the disk). Explain and stop instead of silently downgrading to a
    no-dashboard, one-network download (build 47 harden)."""
    drv = os.path.splitdrive(os.path.abspath(od))[0] or 'the save drive'
    print(CNO(f'  cannot download: {got}.') + CYLW(
        f'\n  This video at this quality needs more free space than {drv} has,'
        f'\n  so the multi-link engine stopped before downloading anything.'
        f'\n  Free up space, save to another drive, or re-run with a smaller quality'
        f' (e.g. 1080 in the quality prompt).'), flush=True)
    return False

def _yt_run(url, headless, fl, quality=None, eflags=None):
    """Download a single YouTube link. Returns True on success. Build 45: when
    ffmpeg is available the raw streams are pulled through BandwidthGoblin's
    own multi-link engine (split over every connection, resumable, verified)
    and merged locally - YouTube gets the same all-networks treatment as any
    other link."""
    eflags = eflags or []
    if not is_youtube(url):
        return False
    exe = _yt_engine()
    if not exe:
        return False
    fmt, is_audio, qlabel = _yt_pick(url, headless, quality)
    if headless:
        od = (fl.get('out-dir') or [''])[0] or load_lastdir() or '.'
    else:
        sug = load_lastdir() or '.'
        v = ask(f'save folder? [Enter = {sug}]: ')
        od = _clean_path(v) or sug
        if not od:
            od = '.'
    try:
        os.makedirs(od, exist_ok=True)
        save_lastdir(od)
    except Exception:
        pass
    ff = _yt_ffmpeg()
    if not ff and (not is_audio and ('+' in fmt)):
        print(CYLW('no ffmpeg available - taking the best single-file format (often capped at ~720p).\n'
                   'On Windows the tool fetches ffmpeg automatically on the next run; on macOS/Linux\n'
                   'install it once with:  brew install ffmpeg   (or apt install ffmpeg)'))
        fmt = 'b'
    deno = _deno_engine()
    print(f'👺 YouTube download - {qlabel}' + CDIM(f'  |  {redact(url)}') + f'\n   saving to: {od}', flush=True)
    if is_audio:
        print(CDIM('   engines: yt-dlp + deno + ffmpeg (audio only - converted to mp3)'), flush=True)
    elif ff:
        print(CDIM('   engines: yt-dlp + deno + ffmpeg - streams split over all your connections,'), flush=True)
        print(CDIM('   [P] pause  [R] resume  [ESC] stop safely  all work during the multi-link download'), flush=True)
    else:
        print(CDIM('   engines: yt-dlp + deno (no ffmpeg - best single-file format, often ~720p)'), flush=True)
    if is_audio:
        return _yt_direct(exe, fmt, url, od, deno, ff, True)
    if ff:
        # Preferred: split the raw streams over every connection, then merge.
        info, jerr = _yticks('reading video info', lambda: _yt_json(exe, fmt, url, deno))
        if info:
            ok, got = _yt_split_download(info, od, ff, eflags)
            if ok:
                _yt_subs(exe, url, od, deno, ff, info)
                return True
            if 'not enough disk space' in got:
                return _yt_nospace(got, od)
            # Build 47 harden: a non-stop failure is usually a transient
            # reject (one googlevideo edge refused the signed URL while the
            # next one accepts it). Re-extract fresh signatures and give the
            # multi-link engine ONE more shot before dropping to plain
            # single-connection yt-dlp. Re-signed URLs routinely fix the 403.
            if got != 'stopped':
                info2, jerr2 = _yticks('reading video info (re-signing URLs)',
                                       lambda: _yt_json(exe, fmt, url, deno))
                if info2:
                    ok, got = _yt_split_download(info2, od, ff, eflags)
                    if ok:
                        _yt_subs(exe, url, od, deno, ff, info2)
                        return True
                else:
                    got = f'{got} (re-extract: {jerr2})'
            if 'not enough disk space' in got:
                return _yt_nospace(got, od)
            if got == 'stopped':
                print(CDIM('  stopped - your progress is preserved.'))
                return False
            _hint = (' This video only offers an HLS streaming playlist at that quality -'
                     ' not range-splittable, so it falls back to one connection (no dashboard).'
                     if 'HLS' in got else '')
            print(CDIM(f'  multi-link split not possible ({got}) - using the yt-dlp engine (single connection, no dashboard)...{_hint}'))
        else:
            print(CDIM(f'  multi-link split not possible ({jerr}) - using the yt-dlp engine (single connection, no dashboard)...'))
    else:
        print(CYLW('no ffmpeg - taking the best single-file format (often capped at ~720p).'))
    if STOP and STOP_USER:
        print(CDIM('  stopped - your progress is preserved.'))
        return False
    return _yt_direct(exe, fmt, url, od, deno, ff, False)

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
    rest = ['auto', 'auto'] + eflags
    used, plan = set(), []
    for i, (url, out) in enumerate(items, 1):
        if out and os.path.isabs(out):
            name = out
        elif out:
            name = os.path.join(od, _safe_filename(out) or f'download_{i}.bin')
        else:
            nm = suggest_name(url) or f'download_{i}.bin'
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
        for line in open(path, encoding='utf-8-sig', errors='replace'):
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
    global STOP, OPEN_AFTER, QUIET, NO_LOG, OVERWRITE
    _install_crash_hooks()
    _load_mode()
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
    for _k in ('referer', 'cookie', 'sha', 'header', 'limit', 'links', 'priority'):
        for _v in (fl.get(_k) or []):
            eflags.append(f'--{_k}={_v}')
    if fl.get('advanced') or fl.get('simple'):
        _set_mode('advanced' if fl.get('advanced') else 'simple')
    if fl.get('overwrite'):
        OVERWRITE = True
        eflags.append('--overwrite')
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
    if fl.get('yt'):
        tgt = (fl.get('yt') or [''])[0]
        u = tgt if is_link(tgt) else next((a for a in args if is_link(a)), '')
        qq = next((a for a in args if str(a).lower() in ('best', 'audio') + _YT_PRESET_KEYS), '')
        if not u or not is_youtube(u):
            print(CNO('--yt needs a YouTube URL, e.g.  bandwidthgoblin.py --yt https://youtu.be/XXXX 1080')
                  + CDIM('\n' + _usage()))
            sys.exit(1)
        sys.exit(0 if _yt_run(u, True, fl, qq or None, eflags) else 1)
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
        # Build 42: speedtest needs FINE chunks so every live link contributes
        # to the measurement. With '8' chunks a 140 MB file makes 17.5 MB
        # pieces: the first fast link's chunk satisfies the 8 MB cap before
        # the other links land a byte, so "combined" used to only ever show
        # one link. 256 chunks ~ 0.5 MB pieces - all links get sampled.
        for t in targets:
            reset_state()
            run_one(t, os.path.join(tempfile.gettempdir(), 'bg_speedtest.bin'), ['256', 'auto'] + eflags)
        return
    if fl.get('dry-run') or fl.get('info'):
        targets = [a for a in args if is_link(a)] or list(qurls or [])
        if not targets:
            print(CNO('--dry-run needs a link (or --queue with links).') + CDIM('\n' + _usage()))
            return
        for t in targets:
            _probe_print(t)
        return
    if len(args) >= 1 and is_link(args[0]) and is_youtube(args[0]):
        # YouTube one-shot:  YTURL | YTURL 1080 | YTURL D:\save-folder.
        # yt-dlp picks its own title-based filename, so the second token is a
        # quality (720/1080/.../audio/best) or a save folder, not a file name.
        qq = ''
        if len(args) >= 2 and args[1].lower() in ('best', 'audio') + _YT_PRESET_KEYS:
            qq = args[1].lower()
        elif len(args) >= 2 and not is_link(args[1]):
            try:
                os.makedirs(args[1], exist_ok=True)
                save_lastdir(args[1])
            except Exception:
                pass
        sys.exit(0 if _yt_run(args[0], True, fl, qq or None, eflags) else 1)
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
        sys.exit(0 if res == 'ok' else 1)
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
        if is_youtube(url):
            ok = _yt_run(url, headless, fl, None, eflags)
            if headless:
                if not ok:
                    _hfails += 1
                if pending:
                    continue
                break
            try:
                tty = bool(sys.stdin.isatty())
            except Exception:
                tty = False
            if not tty:
                return
            nxt = ask('another? [paste link / y / N]: ')
            if nxt.strip().lower() in ('help', 'h', 'menu'):
                _mini_help()
                continue
            if is_link(nxt):
                pending = [nxt]
                continue
            if nxt.lower() in ('y', 'yes'):
                continue
            return
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
                _nm = _safe_filename(_qo) if _qo else suggest_name(url)
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
            rest = ['auto', 'auto'] + eflags
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
            if res in ('error', 'disk-space') or res is None:
                _hfails += 1
            if pending:
                continue
            break
        base = load_lastdir()
        sug = os.path.join(base or '.', suggest_name(url) or 'download.bin')
        while True:
            out = _clean_path(ask(f'save where? [Enter = {sug}]: '))
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
        # Tuning is an expert question: the simple flow never asks it.
        tune = ask('chunks + threads/link? [Enter = auto auto]: ') if UI_MODE == 'advanced' else ''
        rest = (tune.split()[:2] or ['auto', 'auto']) + eflags
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
    global VALIDATOR_BROKEN, STOP_USER, CUR_Q
    import queue as _q
    _flg, rest = _parse_flags(list(rest))
    # Priority tier for EVERY link (build 47): normal = 1 connection per link,
    # high = 4 connections per link + higher start caps, so all links run at a
    # higher tier in one go. Explicit "chunks threads/link" args still win.
    _prio = (_flg.get('priority') or [os.environ.get('MULTILINK_PRIORITY', '')])[0].strip().lower()
    PRIO_HIGH = _prio in ('high', 'boost', 'max', 'turbo')
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
    # Chunk count: 'auto' (or no arg) = pick from the file size after we
    # learn it (~4 MB/chunk); an explicit number is always respected
    # (speedtest wants fine 256-chunk measurment, selftest uses 10).
    _want_auto_chunks = True
    try:
        _c0 = rest[0] if len(rest) > 0 else ''
        if isinstance(_c0, str) and _c0.lower() == 'auto':
            NCHUNKS = 16   # placeholder; the size-aware rule picks the real count
        else:
            NCHUNKS = int(_c0)
            _want_auto_chunks = False
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
            TPL = 4 if PRIO_HIGH else 1
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
        out = os.path.join(out, suggest_name(ORIG_URL) or 'download.bin')
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
    use = [ip for _, ip in ifaces] or [None]   # no adapters listed: use the default route
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
            print(CYLW('only 1 working connection right now - combining needs 2 or more '
                       '(check the Wi-Fi/hotspot).'), flush=True)
            log('only 1 working connection')
        elif _dead:
            print(CYES(f'connected links: {_n} working - combining active'), flush=True)
    with G_LOCK:
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
    else:
        # The full btop-style dashboard is the only UI. On a plain Windows
        # console make sure ANSI/VT rendering is enabled so colors and box
        # drawing actually display.
        DASH_MODE = 'full'
        if os.name == 'nt':
            try:
                import ctypes
                _k = ctypes.windll.kernel32
                _h = _k.GetStdHandle(-11)              # STD_OUTPUT_HANDLE
                _m = ctypes.c_uint32()
                if _k.GetConsoleMode(_h, ctypes.byref(_m)):
                    _k.SetConsoleMode(_h, _m.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            except Exception:
                pass
    DASH_ON = DASH_MODE == 'full'
    log(f'display: {DASH_MODE} (tty={tty})')
    log(f'final url: {redact(url)}')
    log(f'size: {size/1e6:.1f} MB, ranges: {ar}, etag: {V_ETAG[:28] or "-"}, links: {len(use)}, chunks: {NCHUNKS}, threads/link: {TPL}')
    ar_ok = bool(ar and 'bytes' in ar.lower())
    # One link gets chunks too: same speed, but it can resume and shows progress.
    if size and ar_ok and use and NCHUNKS > 1:
        _ptot, _ok_ips = 0, []

        def _probe_one(_ip):
            try:
                return _ip, range_probe(url, _ip, size)
            except Exception as ex:
                log(f'range probe {_ip or "default"}: {ex} - dropping this link from multi-link')
                return _ip, None

        # Probe every link in parallel - a googlevideo-style redirect chain is
        # walked per link, so serial probing costs one RTT*redirects per link
        # before ANY chunk starts (build 47).
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(2, len(use))) as _pex:
            with G_LOCK:
                _probe_ips = list(use)
            _pres = list(_pex.map(_probe_one, _probe_ips))
        for _ip, _r in _pres:
            if not _r:
                continue
            _a, _b = _r
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
    if not size or not ar_ok or not use:
        log('no range support: downloading as one stream via default route')
        use = [None]; size = size or 0
        NCHUNKS = 1; TPL = 1
        TOTAL_SIZE = size
    # Size-aware chunk count (build 47): 16 fixed chunks is silly for a 0.3 MB
    # file (16 tiny round-trips) and coarse for a 500 MB one. ~4 MB per chunk
    # keeps overhead low and work-stealing granularity fine.
    if size and _want_auto_chunks and NCHUNKS != 1:
        _n = max(4, min(64, int(size / 4e6)))
        if _n != NCHUNKS:
            log(f'size-aware chunks: {NCHUNKS} -> {_n} (~{size/_n/1e6:.1f} MB each)')
            NCHUNKS = _n
    pardir = os.path.dirname(os.path.abspath(out))
    if pardir:
        os.makedirs(pardir, exist_ok=True)
    manifest = out + '.resume'
    if size:
        try:
            free = shutil.disk_usage(pardir or '.').free
            if os.path.exists(manifest) and os.path.exists(out):
                free += os.path.getsize(out)   # resuming: the partial's space is already ours
        except Exception:
            free = None
        if free is not None and free < size:
            msg = f'Not enough disk space: need {size/1e6:.0f} MB, free {free/1e6:.0f} MB at {pardir}. Free space or pick another drive.'
            print(msg, flush=True)
            log(msg)
            # Distinct from 'error': a disk shortfall cannot be fixed by any
            # download engine (single-connection needs the same bytes), so
            # callers must NOT fall back to plain yt-dlp (build 47 harden).
            return 'disk-space'
    skip = set()
    resume_pos = {}
    have_resume = False
    if os.path.exists(manifest) and os.path.exists(out) and size:
        try:
            with open(manifest) as _mf:
                m = json.load(_mf)
            # The saved chunk plan wins over today's: the link count, or a typed
            # chunk number, may differ on the rerun and that must not wipe the
            # partial. The plan only has to fit this same file.
            _mc = m.get('chunks')
            if NCHUNKS > 1 and isinstance(_mc, int) and 1 < _mc <= size and m.get('size') == size and _mc != NCHUNKS:
                log(f'resume: keeping the saved plan of {_mc} chunks')
                NCHUNKS = _mc
                _want_auto_chunks = False
            same_file = (m.get('url') == ORIG_URL and m.get('size') == size
                         and m.get('chunks') == NCHUNKS)
            # YouTube split streams: googlevideo re-signs the URL on every
            # extraction, so across two runs the URL can never match. The tmp
            # path is scoped to video id + stream index and the size is
            # binding, so a partial from a stopped run is safe to continue
            # (build 47 audit M3/M4 - previously a stopped YT run could NEVER
            # resume: URL mismatch forced a silent full re-download).
            _yt_stream = out.endswith('.tmp') and bool(re.search(r'_s\d+\.tmp$', out))
            if not same_file and _yt_stream and m.get('size') == size and m.get('chunks') == NCHUNKS:
                log('resume: stream URL re-signed but size matches - continuing partial')
                same_file = True
            elif _yt_stream and m.get('size') == size and m.get('chunks') != NCHUNKS:
                log(f'resume rejected: chunk plan changed ({m.get("chunks")} -> {NCHUNKS}) - re-downloading')
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
                resume_pos = m.get('pos') or {}
                have_resume = True
                log(f'resuming: skipping {len(skip)} done chunks (existing bytes kept)')
        except Exception as ex:
            log(f'ignoring resume file: {ex}')
            skip = set()
    # Never eat a file we did not start. (A leftover .resume means it IS our
    # partial; a 0-byte rename is our own single-stream retry landing again.)
    _mapped = _RENAMED.get(out)
    if _mapped and os.path.exists(_mapped) and os.path.getsize(_mapped) == 0 and not os.path.exists(_mapped + '.resume'):
        out, manifest = _mapped, _mapped + '.resume'
    elif (not have_resume and not OVERWRITE and not SPEEDTEST and not _flg.get('overwrite')
            and not out.endswith('.tmp') and not os.path.exists(manifest)
            and os.path.exists(out) and os.path.getsize(out) > 0):
        _new = _unused_path(out)
        print(CYLW(f'{os.path.basename(out)} already exists - keeping it and saving as {os.path.basename(_new)}'), flush=True)
        log(f'existing file kept, saving as {_new}')
        _RENAMED[out] = _new
        out, manifest = _new, _new + '.resume'
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
    CUR_Q = q
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
            if PRIO_HIGH:
                start_cap = 4   # priority tier: every link starts maxed out
            else:
                s = mem.get(ip, 0)
                start_cap = 3 if s >= 3 else (2 if s >= 1 else 1)
            WORKER_CAP[ip] = start_cap
            LINK_THREADS[ip] = start_cap
        log(('priority tier: high - 4 connections/link on EVERY link' if PRIO_HIGH
             else 'auto-tune start caps from history') + ': ' +
            ', '.join(f'{ip or "?"}x{WORKER_CAP[ip]}' for ip in use))
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
        _rp = resume_pos.get(str(i))
        if isinstance(_rp, int) and isinstance(e, int) and s < _rp <= e + 1:
            with TOTAL_DONE_LOCK:
                TOTAL_DONE += _rp - s   # part-finished chunk: pick it up where it stopped
            s = _rp
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
        _tier = ('priority tier HIGH: 4 connections per link on ALL links'
                 if PRIO_HIGH else
                 '1 connection per link (speed comes from links, not threads)')
        log(f'auto-tune on: per-link throttle handling, {_tier}')
        at = _t.Thread(target=auto_tune, daemon=True)
        at.start()
    dd = None
    if DASH_MODE == 'full':
        dd = _t.Thread(target=dash, args=(out, use, NCHUNKS), daemon=True)
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
                            _requeue_link(bad_ip, q)
                            with G_LOCK:
                                _left = [u for u in use if u]
                            if not _left:
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
                            if 'disk is full' in s:
                                hint = 'free up some space, then run the same link again - your progress is saved.'
                            elif 'web page' in s:
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
                            _say(CNO(f'Download failed: {s}') + CYLW(f'\n{hint}'))
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
                    STOP_USER = True
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
        dd.join(timeout=3)
    _screen_restore()
    _flush_said()
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
    done_list = [i for i in range(NCHUNKS) if CHUNK_STATE.get(i) == 'done']
    if stopped or len(done_list) < NCHUNKS:
        try:
            _write_manifest(out, vok=not VALIDATOR_BROKEN)
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
        if failed is None:
            print(CYLW(f'Stopped - {TOTAL_DONE/1e6:,.1f} MB saved. Run the same link again to continue.'), flush=True)
        notify('BandwidthGoblin: download failed' if failed is not None else 'BandwidthGoblin: stopped',
               f'{os.path.basename(out)} - resume saved ({len(done_list)}/{NCHUNKS} chunks)')
        return
    print(CYES('Finished - checking the file...'), flush=True)
    log('finalizing: verifying all chunks')
    got = os.path.getsize(out)
    log(f'saved: {out} ({got/1e6:.1f} MB) in {time.time()-T0:.1f}s')
    if TOTAL_SIZE and got != TOTAL_SIZE:
        log(f'WARNING: size mismatch got={got} expected={TOTAL_SIZE}')
    else:
        log('size verified OK')
    _ok, _why = _yticks('verifying chunks (final integrity check)',
                        lambda: verify_file(out, TOTAL_SIZE))
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
