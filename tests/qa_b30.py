"""qa_b30.py: audit-fix QA - reset_state must clear PAUSED + LINK_SPEED, and
headless --out-dir mode must not collide filenames for URLs without a path name."""
import os, sys, time, hashlib, urllib.request

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
URL = 'http://127.0.0.1:8765'
SERVED = os.path.join(QA, 'big.src')

sys.path.insert(0, ROOT)
import linkboost as lb

res = []
def check(name, ok, extra=''):
    res.append(bool(ok))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {extra}')

FULL = open(SERVED, 'rb').read()
SHA_FULL = hashlib.sha256(FULL).hexdigest()

# ---- 1. reset_state clears PAUSED (the freeze leak) ----
lb.PAUSED = True
lb.reset_state()
check('reset_state clears PAUSED', lb.PAUSED is False)

# ---- 2. reset_state clears LINK_SPEED (stale seeding) ----
lb.LINK_SPEED['10.9.8.7'] = 12345.0
lb.reset_state()
check('reset_state clears LINK_SPEED', lb.LINK_SPEED == {})

# ---- 3. headless: two no-name URLs land in distinct files ----
def no_notify(*a, **kw): pass
lb.notify = no_notify
lb.local_ips = lambda: []

# wait for server
deadline = time.time() + 20
while time.time() < deadline:
    try:
        urllib.request.urlopen(URL + '/big.bin', timeout=2).read(64)
        break
    except Exception:
        time.sleep(0.5)
else:
    print('[server] NOT REACHABLE'); sys.exit(1)

od = os.path.join(QA, 'b30out')
for f in os.listdir(od) if os.path.isdir(od) else []:
    try: os.remove(os.path.join(od, f))
    except OSError: pass
os.makedirs(od, exist_ok=True)

for _k in ('LINKBOOST_SHA256', 'LINKBOOST_LIMIT', 'LINKBOOST_SPEEDTEST_MB',
           'LINKBOOST_REFERER', 'LINKBOOST_COOKIE', 'LINKBOOST_HEADERS'):
    os.environ.pop(_k, None)

sys.argv = ['linkboost.py', '--out-dir', od, f'{URL}/big.bin?t=1', f'{URL}/big.bin?t=2']
lb.main()

p1 = os.path.join(od, 'big.bin')
p2 = os.path.join(od, 'big_2.bin')
ok1 = os.path.exists(p1) and hashlib.sha256(open(p1, 'rb').read()).hexdigest() == SHA_FULL
ok2 = os.path.exists(p2) and hashlib.sha256(open(p2, 'rb').read()).hexdigest() == SHA_FULL
names = sorted(os.listdir(od))
check('two same-named URLs -> two distinct files (dedupe), both intact',
      ok1 and ok2 and p1 != p2 and len(names) == 2, f'({names})')

# ---- 4. named URLs still use their guessed names, no 1.. numbering ----
od2 = os.path.join(QA, 'b30out2')
for f in os.listdir(od2) if os.path.isdir(od2) else []:
    try: os.remove(os.path.join(od2, f))
    except OSError: pass
os.makedirs(od2, exist_ok=True)
sys.argv = ['linkboost.py', '--out-dir', od2, f'{URL}/big.bin']
lb.main()
ok3 = os.path.exists(os.path.join(od2, 'big.bin')) \
    and hashlib.sha256(open(os.path.join(od2, 'big.bin'), 'rb').read()).hexdigest() == SHA_FULL
check('named URL keeps its guessed filename', ok3)

print(f'\n{sum(res)}/{len(res)} checks passed')
sys.exit(0 if all(res) else 1)