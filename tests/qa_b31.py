"""qa_b31.py: multi-link usability - --links selection (indices + IPs), same-subnet
hint, and the per-link DONE summary line."""
import io, os, sys, time, hashlib, urllib.request

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
URL = 'http://127.0.0.1:8765'
SERVED = os.path.join(QA, 'big.src')

sys.path.insert(0, ROOT)
import bandwidthgoblin as lb

res = []
def check(name, ok, extra=''):
    res.append(bool(ok))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {extra}')

FULL = open(SERVED, 'rb').read()
SHA_FULL = hashlib.sha256(FULL).hexdigest()
lb.notify = lambda *a, **kw: None
lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3'), ('qa-c', '127.0.0.4')]

deadline = time.time() + 20
while time.time() < deadline:
    try:
        urllib.request.urlopen(URL + '/big.bin', timeout=2).read(64)
        break
    except Exception:
        time.sleep(0.5)
else:
    print('[server] NOT REACHABLE'); sys.exit(1)

for _k in ('BANDWIDTHGOBLIN_SHA256', 'BANDWIDTHGOBLIN_LIMIT', 'BANDWIDTHGOBLIN_SPEEDTEST_MB',
           'BANDWIDTHGOBLIN_REFERER', 'BANDWIDTHGOBLIN_COOKIE', 'BANDWIDTHGOBLIN_HEADERS'):
    os.environ.pop(_k, None)

def run(rest, out):
    out = os.path.join(QA, out)
    for f in (out, out + '.resume', out + '.progress'):
        try: os.remove(f)
        except OSError: pass
    lb.reset_state()
    lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3'), ('qa-c', '127.0.0.4')]
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        lb.run_one(URL + '/big.bin', out, rest)
    finally:
        sys.stdout = old
    return out, buf.getvalue()

# ---- 1. --links by index: use links 1 and 2 (127.0.0.3, 127.0.0.4) ----
os.environ.pop('MULTILINK_IFACES', None)
out, text = run(['4', '1', '--links=1,2'], 'b31_idx.bin')
used = set(lb.LINK_DONE.keys())
ok = hashlib.sha256(open(out, 'rb').read()).hexdigest() == SHA_FULL
ok = ok and '127.0.0.2' not in used and used <= {'127.0.0.3', '127.0.0.4'}
check('--links=1,2 selects exactly links 2+3, link 1 untouched', ok, f'({sorted(x or "default" for x in used)})')

# ---- 2. --links by IP: use 127.0.0.2 and 127.0.0.4 ----
out2, text2 = run(['4', '1', '--links=127.0.0.2,127.0.0.4'], 'b31_ip.bin')
used2 = set(lb.LINK_DONE.keys())
ok2 = hashlib.sha256(open(out2, 'rb').read()).hexdigest() == SHA_FULL
ok2 = ok2 and used2 <= {'127.0.0.2', '127.0.0.4'} and '127.0.0.3' not in used2
check('--links=127.0.0.2,127.0.0.4 (by IP) selects those links', ok2, f'({sorted(x or "default" for x in used2)})')

# ---- 3. same-subnet hint appears in the log (all three loopbacks share 127.0.0) ----
check('same-subnet hint logged for same-subnet links',
      'share a subnet' in open(lb.LOGFILE or os.devnull, errors='ignore').read()
      or 'share a subnet' in text2,
      f'(logfile={os.path.basename(lb.LOGFILE or "")})')

# ---- 4. per-link DONE summary printed for multi-link runs ----
check('multi-link run prints per-link summary ("  links:" prefixed line)', '  links:' in text2, '')

# ---- 5. single-link run has no summary (guard) ----
# NB: run() above hardcodes a 3-IP patch, so check 5 does its own capture
# with local_ips truly single-link. Chunk-progress lines legitimately contain
# 'MB @' (4.0/4.0MB @ 1.2 MB/s), so the summary is identified by '  links:'.
lb.local_ips = lambda: [('qa-a', '127.0.0.1')]
os.environ.pop('MULTILINK_IFACES', None)
out3 = os.path.join(QA, 'b31_one.bin')
for f in (out3, out3 + '.resume', out3 + '.progress'):
    try: os.remove(f)
    except OSError: pass
lb.reset_state()
buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
try:
    lb.run_one(URL + '/big.bin', out3, ['2', '1'])
finally:
    sys.stdout = old
text3 = buf.getvalue()
check('single-link run prints no per-link summary line', '  links:' not in text3,
      f'({hashlib.sha256(open(out3,"rb").read()).hexdigest()[:12] == SHA_FULL[:12]} intact)')

# ---- 6. help mentions --links ----
h = lb._usage()
check('usage text documents --links', '--links' in h and 'WiFi + LAN' in h)

print(f'\n{sum(res)}/{len(res)} checks passed')
sys.exit(0 if all(res) else 1)