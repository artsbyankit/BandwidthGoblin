"""qa_b37.py: --speedtest cap must stop cleanly (build 37 fix).

Regression for the real-world bug found on tele2/OVH/kernel.org: in speedtest
mode every worker raced past the byte cap, raised _SpeedtestDone (empty str ->
logged as 'failed ()'), and the worker's generic except re-queued the same
chunk from the same offset forever (12x give-up churn) instead of letting the
runner stop the measurement. Build 37 added an explicit worker handler, so a
speedtest must now end quickly, print 'SPEEDTEST done', leave no resume file,
and never log 'failed (' or 'gives up'.
"""
import io, os, sys, time, urllib.request

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
URL = 'http://127.0.0.1:8765'

sys.path.insert(0, ROOT)
import linkboost as lb

res = []
def check(name, ok, extra=''):
    res.append(bool(ok))
    print(f'[{"PASS" if ok else "FAIL"}] {name} {extra}')

lb.notify = lambda *a, **kw: None
lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3')]

# server must be up
deadline = time.time() + 20
while time.time() < deadline:
    try:
        urllib.request.urlopen(URL + '/big.bin', timeout=2).read(64)
        break
    except Exception:
        time.sleep(0.5)
else:
    print('[server] NOT REACHABLE'); sys.exit(1)

for _k in ('LINKBOOST_SHA256', 'LINKBOOST_LIMIT', 'LINKBOOST_SPEEDTEST_MB',
           'LINKBOOST_REFERER', 'LINKBOOST_COOKIE', 'LINKBOOST_HEADERS'):
    os.environ.pop(_k, None)

def run(rest, outname, speedtest_mb=None):
    out = os.path.join(QA, outname)
    for f in (out, out + '.resume', out + '.progress'):
        try: os.remove(f)
        except OSError: pass
    if speedtest_mb:
        os.environ['LINKBOOST_SPEEDTEST_MB'] = str(speedtest_mb)
    else:
        os.environ.pop('LINKBOOST_SPEEDTEST_MB', None)
    lb.reset_state()
    lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3')]
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    t0 = time.time()
    try:
        lb.run_one(URL + '/big.bin', out, rest)
    finally:
        sys.stdout = old
    return out, buf.getvalue(), time.time() - t0

# ---- 1. speedtest stops at the cap, cleanly and quickly ----
out, text, el = run(['8', '1'], 'b37_speed.bin', speedtest_mb=4)
no_churn = 'failed (' not in text and 'gives up' not in text and 'FAILED 12x' not in text
check('build 36+ speedtest: no churn logs ("failed ()"/"gives up")', no_churn, f'(el={el:.1f}s)')
check('build 36+ speedtest: prints "SPEEDTEST done"', 'SPEEDTEST done' in text, '')
check('build 36+ speedtest: run ends quickly (< 20s)', el < 20, f'(el={el:.1f}s)')
check('build 36+ speedtest: no .resume file left behind', not os.path.exists(out + '.resume') and not os.path.exists(out), '')
# cap respected: measured MB should be ~4, never the whole file
import re
mb = None
m = re.search(r'SPEEDTEST done: ([0-9.]+) MB', text)
if m:
    mb = float(m.group(1))
check('build 36+ speedtest: measured ~4 MB (not the whole file)', mb is not None and 3.0 <= mb <= 5.5, f'(measured={mb}MB)')

# ---- 2. control: a normal (non-speedtest) download still completes fully ----
out2, text2, el2 = run(['2', '1'], 'b37_full.bin', speedtest_mb=None)
size_ok = os.path.exists(out2) and os.path.getsize(out2) > 4_000_000
check('control: plain download still completes intact (no cap)', size_ok and 'DONE' in text2, f'(el={el2:.1f}s)')

print(f'\n{sum(res)}/{len(res)} checks passed')
sys.exit(0 if all(res) else 1)