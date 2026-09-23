"""qa_tier2.py: functional QA for build 27 Tier 2 nice-to-haves.

Covers: pause(P/R via _toggle_pause + mid-run gating), --limit, --history,
--resume (CLI), --speedtest, --quiet, --no-log, plus CLI smokes for --help.
Runs against the local QA server (self-connect URL 172.20.10.2) and re-runs
the three qa_drive regressions (multi / resume / norange).
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time, hashlib

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
PY = r'C:\Users\ankit\AppData\Local\Programs\Python\Python313\python.exe'
URL = 'http://127.0.0.1:8765'
SERVED = os.path.join(QA, 'big.src')
RESCLI = os.path.join(QA, 'rescli')

sys.path.insert(0, ROOT)
import linkboost as lb

lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3')]
lb.notify = lambda *a, **k: None   # the toast spawns a 7s-sleeping PowerShell; kill it for measurements

# --- wait for the QA server -------------------------------------------------
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

with open(SERVED, 'rb') as f:
    FULL = f.read()
SHA_FULL = hashlib.sha256(FULL).hexdigest()

_results = []
def check(name, ok, extra=''):
    _results.append((name, bool(ok)))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {extra}')

def cli(args, cwd=QA, env=None, timeout=300):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run([PY, os.path.join(ROOT, 'linkboost.py')] + args,
                       capture_output=True, text=True, cwd=cwd, env=e, timeout=timeout)
    return (p.stdout + p.stderr), p.returncode

def sha_of(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else 'missing'

def rm(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass

# === 1. flag parsing + help smoke ===========================================
fl, rest = lb._parse_flags(['--limit=2', '--quiet', '--no-log', '--history', '--resume', '--speedtest'])
check('parse: --limit value flag', fl.get('limit') == ['2'])
check('parse: new bool flags', all(fl.get(k) for k in ('quiet', 'no-log', 'history', 'resume', 'speedtest')))
check('parse: --limit stays out of rest', all('limit' not in str(r) for r in rest))
out, rc = cli(['--help'], timeout=60)
check('help lists new flags', all(s in out for s in ('--speedtest', '--resume', '--limit', '--quiet', '--no-log', '--history')))
out, rc = cli(['--version'])
check('version flag ok', rc == 0 and 'build ' in out)

# === 2. pause toggle + real mid-run gate ====================================
lb.PAUSED = False
lb._toggle_pause(); p1 = lb.PAUSED
lb._toggle_pause(); p2 = lb.PAUSED
lb._toggle_pause(); p3 = lb.PAUSED
lb._toggle_pause()
check('_toggle_pause flips flag', p1 is True and p2 is False and p3 is True)

# Unpaused baseline under IDENTICAL conditions (same payload + cap, in-process)
# records how long the tune+probe overhead really takes. Then the pause test
# must stretch elapsed by roughly the 2s forced pause vs that baseline - that
# discriminates a real mid-download halt from mere tune noise.
outb = os.path.join(QA, 'base.bin')
rm(outb, outb + '.resume', outb + '.progress', os.path.join(ROOT, 'logs', 'base.bin.log'))
sys.argv = ['linkboost.py', f'{URL}/big.bin', outb, '--limit=0.5']
t0 = time.time()
lb.main()
base_el = time.time() - t0
check('baseline: capped run intact', sha_of(outb) == SHA_FULL, f'({base_el:.1f}s)')

out = os.path.join(QA, 'pause.bin')
rm(out, out + '.resume', out + '.progress', os.path.join(ROOT, 'logs', 'pause.bin.log'))
def _pause_after_3s():
    time.sleep(3.0)
    lb.PAUSED = True
    time.sleep(2.0)
    lb.PAUSED = False
th = threading.Thread(target=_pause_after_3s); th.start()
sys.argv = ['linkboost.py', f'{URL}/big.bin', out, '--limit=0.5']
t0 = time.time()
lb.main()
el = time.time() - t0
th.join(timeout=10)
check('pause mid-run: +~2s vs baseline, stays intact',
      sha_of(out) == SHA_FULL and el - base_el >= 1.5,
      f'(base {base_el:.1f}s, paused {el:.1f}s)')

# === 3. --history ===========================================================
hpath = os.path.join(ROOT, 'history.jsonl')
had_history = os.path.exists(hpath)
if not had_history:
    out, rc = cli(['--history'])
    check('history: graceful on empty', 'no history yet' in out)
out, rc = cli(['--history'])
lines = [json.loads(l) for l in open(hpath) if l.strip()] if os.path.exists(hpath) else []
check('history: pause.bin recorded', any(r.get('out') == 'pause.bin' and r.get('outcome') == 'done' for r in lines))

# === 4. --limit =============================================================
out = os.path.join(QA, 'limit.bin')
rm(out, out + '.resume', out + '.progress', os.path.join(ROOT, 'logs', 'limit.bin.log'))
t0 = time.time()
out_txt, rc = cli([f'{URL}/big.bin', out, '--limit=0.5'])
el = time.time() - t0
check('--limit=0.5 caps a 4 MiB run near ~8s', sha_of(out) == SHA_FULL and 6.5 <= el < 60, f'({el:.1f}s)')

# === 5. --quiet =============================================================
out = os.path.join(QA, 'quiet.bin')
rm(out, out + '.resume', out + '.progress', os.path.join(ROOT, 'logs', 'quiet.bin.log'))
txt, rc = cli([f'{URL}/big.bin', out, '--quiet'])
check('--quiet: banner suppressed', 'LinkBoost v' not in txt)
check('--quiet: milestones still shown', 'all chunks done' in txt and 'Finalizing file' in txt)
check('--quiet: no per-chunk chatter', 'chunk0 via' not in txt and 'log file:' not in txt)
check('--quiet: file intact', sha_of(out) == SHA_FULL)

# === 6. --no-log ============================================================
nlpath = os.path.join(ROOT, 'logs')
def _log_files():
    return sorted(f for f in os.listdir(nlpath)) if os.path.isdir(nlpath) else []
before = _log_files()
out = os.path.join(QA, 'nolog.bin')
rm(out, out + '.resume', out + '.progress')
txt, rc = cli([f'{URL}/big.bin', out, '--no-log'])
after = _log_files()
check('--no-log: no new log file on disk', after == before, f'({len(after)} now, {len(before)} before)')
check('--no-log: download still completes', sha_of(out) == SHA_FULL)

# === 7. --speedtest =========================================================
tmpout = os.path.join(tempfile.gettempdir(), 'lb_speedtest.bin')
rm(tmpout, tmpout + '.resume', tmpout + '.progress')
txt, rc = cli(['--speedtest', f'{URL}/big.bin'], env={'LINKBOOST_SPEEDTEST_MB': '2'})
check('--speedtest: reports done + combined rate', 'SPEEDTEST done' in txt and 'combined:' in txt and 'MB/s' in txt)
check('--speedtest: temp output cleaned up', not os.path.exists(tmpout) and not os.path.exists(tmpout + '.resume'))

# === 8. --resume (CLI) ======================================================
shutil.rmtree(RESCLI, ignore_errors=True)
os.makedirs(RESCLI, exist_ok=True)
rout = os.path.join(RESCLI, 'res.bin')
half = len(FULL) // 2
with open(rout, 'wb') as f:
    f.write(FULL[:half])
    f.write(b'\x00' * (len(FULL) - half))
with open(rout + '.resume', 'w') as f:
    json.dump({'url': f'{URL}/big.bin', 'size': len(FULL), 'chunks': 16,
               'done': list(range(8)), 'etag': '"qa-big"', 'lastmod': '', 'build': 27}, f)
lb.save_lastdir(RESCLI)          # pin .lastdir so the child process scans RESCLI
txt, rc = cli(['--resume'], cwd=RESCLI)
check('--resume: completes the partial file', rc == 0 and sha_of(rout) == SHA_FULL, f'(rc={rc})')
check('--resume: resume manifest removed after success', not os.path.exists(rout + '.resume'))

# === 9. regression: qa_drive scenarios ======================================
for sc in ('multi', 'resume', 'norange'):
    p = subprocess.run([PY, os.path.join(QA, 'qa_drive.py'), sc],
                       capture_output=True, text=True, cwd=QA, timeout=300)
    ok = p.returncode == 0
    check(f'regression: qa_drive {sc}', ok, f'(rc={p.returncode})')
    if not ok:
        print(p.stdout[-1500:])

# === summary ================================================================
fails = [n for n, ok in _results if not ok]
print(f'\n{len(_results) - len(fails)}/{len(_results)} checks passed')
sys.exit(1 if fails else 0)