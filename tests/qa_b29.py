"""qa_b29.py: toast fix QA - notify() must return in well under a second and
an end-to-end download (with the REAL toast, no patch) must not gain the old
7-9s hang. Uses the same capped in-process run as build 28's 10.0s baseline."""
import json, os, sys, time, hashlib, threading

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
URL = 'http://127.0.0.1:8765'
SERVED = os.path.join(QA, 'big.src')

sys.path.insert(0, ROOT)
import bandwidthgoblin as lb
lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3')]

import urllib.request
deadline = time.time() + 20
while time.time() < deadline:
    try:
        urllib.request.urlopen(URL + '/big.bin', timeout=2).read(64)
        break
    except Exception:
        time.sleep(0.5)
else:
    print('[server] NOT REACHABLE'); sys.exit(1)

FULL = open(SERVED, 'rb').read()
SHA_FULL = hashlib.sha256(FULL).hexdigest()
res = []

def check(name, ok, extra=''):
    res.append(bool(ok))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {extra}')

# 1. notify() itself returns fast, real powershell toast. First spawn on a
#    cold box costs ~1.3s of OS CreateProcess time (measured raw,
#    environment-level; warm spawns 4ms) - assert a generous <2s ceiling.
for i in range(2):
    t0 = time.time()
    lb.notify('LinkBoost QA 29', f'toast timing {i}')
    el = time.time() - t0
    check(f'notify() is pure spawn cost, no 7s sleep ({i})', el < 2.0, f'({el*1000:.0f}ms)')

def _main_inproc(argv):
    sys.argv = argv
    try:
        lb.main()
        return 0
    except SystemExit as e:
        return 0 if e.code in (None, 0) else 1

# 2. end-to-end capped run WITHOUT patching notify: build 28 patched baseline
#    was 10.0s. With the REAL (now non-blocking) toast, wall time must stay at
#    the cap-driven ~10s - NOT balloon past ~17s.
out = os.path.join(QA, 'b29.bin')
for f in (out, out + '.resume', out + '.progress'):
    try: os.remove(f)
    except OSError: pass
t0 = time.time()
rc = _main_inproc(['bandwidthgoblin.py', f'{URL}/big.bin', out, '--limit=0.5'])
el = time.time() - t0
sha = hashlib.sha256(open(out, 'rb').read()).hexdigest()
check('download + real toast: no 7-9s hang', rc == 0 and sha == SHA_FULL and el <= 13.0, f'({el:.1f}s, build28 baseline 10.0s)')
check('still cap-limited near ~0.5 MB/s', 8.0 <= el, f'({el:.1f}s)')

print(f'\n{sum(res)}/{len(res)} checks passed')
sys.exit(0 if all(res) else 1)