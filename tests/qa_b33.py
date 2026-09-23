"""qa_b33.py: build 33 - --selftest and --verify. Covers:
  * --selftest: offline 2-link ranged download, sha256-intact, rc=0, no traceback
    noise, temp artifact cleaned up
  * --verify manifest (sha256sum-format): all-PASS -> rc 0
  * --verify corrupted file -> FAIL sha256 mismatch, rc 1
  * --verify missing file -> FAIL (missing), rc 1
  * --verify dir with .sha256 sidecars -> PASS
  * --verify empty/nonexistent target -> 'no checksums found', rc 1
  * usage documents both flags
"""
import os, sys, time, hashlib, subprocess, urllib.request

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
URL = 'http://127.0.0.1:8765'
PY = r'C:\Users\ankit\AppData\Local\Programs\Python\Python313\python.exe'
LB = os.path.join(ROOT, 'bandwidthgoblin.py')

sys.path.insert(0, ROOT)
import bandwidthgoblin as lb

res = []
def check(name, ok, extra=''):
    res.append(bool(ok))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {extra}')

FULL = open(os.path.join(QA, 'big.src'), 'rb').read()
SHA_FULL = hashlib.sha256(FULL).hexdigest()

for _d in ('b33_ok', 'b33_bad', 'b33_s', 'b33_empty'):
    _dd = os.path.join(QA, _d)
    if os.path.isdir(_dd):
        for _f in os.listdir(_dd):
            try: os.remove(os.path.join(_dd, _f))
            except OSError: pass
        os.rmdir(_dd) if not os.listdir(_dd) else None

deadline = time.time() + 20
while time.time() < deadline:
    try:
        urllib.request.urlopen(URL + '/big.bin', timeout=2).read(64)
        break
    except Exception:
        time.sleep(0.5)
else:
    print('[server] NOT REACHABLE'); sys.exit(1)

def cli(*args, timeout=180):
    p = subprocess.run([PY, LB] + list(args), capture_output=True, text=True,
                       timeout=timeout, cwd=QA)
    return p.returncode, p.stdout + p.stderr

# ---- 1. --selftest end-to-end ----
import tempfile
rp = os.path.join(tempfile.gettempdir(), 'lb_selftest_FIXME')  # placeholder; real pid unknown
rc_self, t_self = cli('--selftest')
noise = 'Traceback' in t_self or 'Exception occurred during processing' in t_self
leftover = False
for _f in os.listdir(tempfile.gettempdir()):
    if _f.startswith('lb_selftest_') and _f.endswith('.bin'):
        leftover = True
        try: os.remove(os.path.join(tempfile.gettempdir(), _f))
        except OSError: pass
check('--selftest: passes offline (2 links, ranged, sha256-intact), rc=0',
      rc_self == 0 and 'selftest PASS' in t_self, f'(rc={rc_self})')
check('--selftest: no traceback noise; temp artifact removed', (not noise) and (not leftover),
      f'(noise={noise}, leftover={leftover})')

# ---- 2. --verify manifest, all good ----
od = os.path.join(QA, 'b33_ok'); os.makedirs(od, exist_ok=True)
open(os.path.join(od, 'orig.bin'), 'wb').write(FULL)
with open(os.path.join(od, 'SHA256SUMS'), 'w') as f:
    f.write(f'{SHA_FULL}  orig.bin\n')
rc, t = cli('--verify', od)
check('--verify dir: all PASS, exit 0', rc == 0 and 'PASS orig.bin' in t and '1/1 ok' in t, f'(rc={rc})')

# ---- 3. --verify corrupted file ----
bd = os.path.join(QA, 'b33_bad'); os.makedirs(bd, exist_ok=True)
bad_data = bytearray(FULL); bad_data[100000] ^= 0xFF
open(os.path.join(bd, 'orig.bin'), 'wb').write(bytes(bad_data))
with open(os.path.join(bd, 'SHA256SUMS'), 'w') as f:
    f.write(f'{SHA_FULL}  orig.bin\n')
rc, t = cli('--verify', bd)
check('--verify corrupted file: FAIL sha256 mismatch, exit 1', rc == 1 and 'FAIL orig.bin' in t
      and 'sha256 mismatch' in t, f'(rc={rc})')

# ---- 4. --verify missing file ----
md = os.path.join(QA, 'b33_ok')
with open(os.path.join(md, 'SHA256SUMS'), 'w') as f:
    f.write(f'{SHA_FULL}  orig.bin\n')
    f.write(f'{SHA_FULL}  ghost.bin\n')
rc, t = cli('--verify', md)
check('--verify missing file: FAIL (missing), exit 1', rc == 1 and 'FAIL ghost.bin' in t
      and 'missing)' in t, f'(rc={rc})')

# ---- 5. --verify dir with .sha256 sidecars ----
sd = os.path.join(QA, 'b33_s'); os.makedirs(sd, exist_ok=True)
open(os.path.join(sd, 'clip.bin'), 'wb').write(FULL)
open(os.path.join(sd, 'clip.bin.sha256'), 'w').write(SHA_FULL + '\n')
rc, t = cli('--verify', sd)
check('--verify sidecars: file.sha256 verified, exit 0', rc == 0 and 'PASS clip.bin' in t, f'(rc={rc})')

# ---- 6. --verify with nothing to check ----
ed = os.path.join(QA, 'b33_empty'); os.makedirs(ed, exist_ok=True)
rc, t = cli('--verify', ed)
rc2, t2 = cli('--verify', os.path.join(QA, 'b33_nope_zzz'))
check('--verify empty/missing target: no checksums found, exit 1', rc == 1 and 'no checksums found' in t
      and rc2 == 1 and 'no checksums found' in t2, f'(rc={rc}, rc2={rc2})')

# ---- 7. usage documents --verify and --selftest ----
h = lb._usage()
check('usage documents --verify and --selftest', '--verify' in h and '--selftest' in h)

print(f'\n{sum(res)}/{len(res)} checks passed')
sys.exit(0 if all(res) else 1)