"""qa_b32.py: build 32 - batch downloads. Covers:
  * queue lines 'URL [OUT]' per-line output names (sequential + parallel)
  * --jobs N parallel batch (children in own processes; concurrency wall-clock proof)
  * meaningful exit codes: 0 = saved, 1 = hard failure (direct + batch)
  * live 2-connection probe: dead link dropped, loud single-stream warning
  * duplicate output names get _2/_3 suffixes; no-name URLs -> download_N.bin
"""
import io, os, sys, time, hashlib, subprocess, urllib.request

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
lb.notify = lambda *a, **kw: None

for _k in ('BANDWIDTHGOBLIN_SHA256', 'BANDWIDTHGOBLIN_LIMIT', 'BANDWIDTHGOBLIN_SPEEDTEST_MB',
           'BANDWIDTHGOBLIN_REFERER', 'BANDWIDTHGOBLIN_COOKIE', 'BANDWIDTHGOBLIN_HEADERS',
           'MULTILINK_IFACES'):
    os.environ.pop(_k, None)

for _d in ('b32_od', 'b32_pd', 'b32_dup', 'b32_file'):
    _dd = os.path.join(QA, _d)
    if os.path.isdir(_dd):
        for _f in os.listdir(_dd):
            try: os.remove(os.path.join(_dd, _f))
            except OSError: pass
for _f in ('q32_seq.txt', 'q32_p.txt', 'q32_m.txt', 'b32_ec_ok.txt', 'b32_ec_bad.bin'):
    try: os.remove(os.path.join(QA, _f))
    except OSError: pass

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
    env = dict(os.environ)
    env['MULTILINK_NODASH'] = '1'
    p = subprocess.run([PY, LB] + list(args), capture_output=True, text=True,
                       env=env, timeout=timeout, cwd=QA)
    return p.returncode, p.stdout + p.stderr

def sha(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()

# ---- 1. exit codes for one-shot CLI runs ----
rc0, _ = cli(URL + '/tiny.txt', 'b32_ec_ok.txt')
rc1, t1 = cli(URL + '/nope-404', 'b32_ec_bad.bin')
check('direct CLI success exits 0 (file saved)', rc0 == 0, f'(rc={rc0})')
check('direct CLI 404 exits 1 with clear message', rc1 == 1 and 'broken or unreachable' in t1, f'(rc={rc1})')

# ---- 2. sequential queue with per-line OUT names ----
with open(os.path.join(QA, 'q32_seq.txt'), 'w') as f:
    f.write(f'{URL}/big.bin\n')
    f.write(f'{URL}/tiny.txt tiny_out.txt\n')
    f.write(f'{URL}/norange.bin sub/nband.bin\n')
rc, t = cli('--queue', 'q32_seq.txt', '--out-dir', 'b32_od')
big_ok = os.path.isfile(os.path.join(QA, 'b32_od', 'big.bin')) and sha(os.path.join(QA, 'b32_od', 'big.bin')) == SHA_FULL
tiny_ok = open(os.path.join(QA, 'b32_od', 'tiny_out.txt'), 'rb').read() == b'hello'
nband = os.path.join(QA, 'b32_od', 'sub', 'nband.bin')
nband_ok = os.path.isfile(nband) and os.path.getsize(nband) > 0
check('sequential queue: guessed name + OUT name + path OUT land correctly', rc == 0 and big_ok and tiny_ok and nband_ok,
      f'(rc={rc}, big={big_ok}, tiny={tiny_ok}, nband={nband_ok})')

# ---- 3. parallel --jobs=2: both copies intact, batch reports 2/2 ----
with open(os.path.join(QA, 'q32_p.txt'), 'w') as f:
    f.write(f'{URL}/big.bin big_a.bin\n')
    f.write(f'{URL}/big.bin big_b.bin\n')
rc, t = cli('--queue', 'q32_p.txt', '--out-dir', 'b32_pd', '--jobs=2')
a_ok = sha(os.path.join(QA, 'b32_pd', 'big_a.bin')) == SHA_FULL
b_ok = sha(os.path.join(QA, 'b32_pd', 'big_b.bin')) == SHA_FULL
check('--jobs=2 downloads both copies intact, batch 2/2 ok', rc == 0 and a_ok and b_ok and '2/2 ok' in t,
      f'(rc={rc}, a={a_ok}, b={b_ok})')

# ---- 4. concurrency proof: 2 jobs @ 1.2s each -> ~1.2s wall, not 2.4s ----
_orig_sub = lb._subrun
def _slow(url, out, rest):
    time.sleep(1.2)
    return 0
lb._subrun = _slow
_items = [(URL + '/big.bin', 'b32_file/proof_a.bin'), (URL + '/big.bin', 'b32_file/proof_b.bin')]
buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
_t0 = time.time()
lb._batch_run(_items, {'out-dir': [os.path.join(QA, 'b32_file')]}, [], jobs=2)
_wall = time.time() - _t0
sys.stdout = old
check('--jobs=2 runs concurrently (wall < 2.0s for 2x1.2s jobs)', _wall < 2.0, f'({_wall:.2f}s)')

# ---- 5. duplicate output names disambiguated + no-name fallback ----
def _touch(url, out, rest):
    os.makedirs(os.path.dirname(out) or os.path.join(QA, 'b32_dup'), exist_ok=True)
    open(out, 'wb').write(b'x')
    return 0
lb._subrun = _touch
_items = [(URL + '/big.bin', 'same.bin'), (URL + '/tiny.txt', 'same.bin'),
          (URL + '/?tok=1', None)]
buf = io.StringIO(); sys.stdout = buf
_od = os.path.join(QA, 'b32_dup')
os.makedirs(_od, exist_ok=True)
lb._batch_run(_items, {'out-dir': [_od]}, [], jobs=2)
sys.stdout = old
lb._subrun = _orig_sub
same1 = os.path.isfile(os.path.join(_od, 'same.bin'))
dup2 = os.path.isfile(os.path.join(_od, 'same_2.bin'))
dl3 = os.path.isfile(os.path.join(_od, 'download_3.bin'))  # 3rd line, no name
check('duplicate OUT names get _2 suffix; no-name URL falls back to download_<n>.bin',
      same1 and dup2 and dl3, f'(same, same_2, download_3 = {same1}, {dup2}, {dl3})')

# ---- 6. mixed batch: one 404 -> parent exit 1 and names the failure ----
with open(os.path.join(QA, 'q32_m.txt'), 'w') as f:
    f.write(f'{URL}/big.bin good.bin\n')
    f.write(f'{URL}/missing-xyz bad.bin\n')
rc, t = cli('--queue', 'q32_m.txt', '--out-dir', 'b32_od', '--jobs=2')
g_ok = sha(os.path.join(QA, 'b32_od', 'good.bin')) == SHA_FULL
check('mixed batch: exit 1, 1/2 ok, failed file named', rc == 1 and '1/2 ok' in t and 'failed: bad.bin' in t and g_ok,
      f'(rc={rc}, good={g_ok})')

# ---- 7. live 2-connection probe: dead link dropped + loud single-stream warning ----
def run_one_ghost():
    out = os.path.join(QA, 'b32_ghost.bin')
    for f in (out, out + '.resume', out + '.progress'):
        try: os.remove(f)
        except OSError: pass
    lb.reset_state()
    lb.local_ips = lambda: [('qa', '127.0.0.2'), ('ghost', '203.0.113.7')]
    buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
    try:
        lb.run_one(URL + '/big.bin', out, ['4', '1'])
    finally:
        sys.stdout = old
    return out, buf.getvalue()

_out, _text = run_one_ghost()
ghost_ok = sha(_out) == SHA_FULL
used = set(lb.LINK_DONE.keys())
# Single surviving link falls back to the default route, so LINK_DONE holds
# exactly one entry and never the probed-dead ghost IP.
dropped = '203.0.113.7' not in used and len(used) <= 1
warned = 'only 1 working connection' in _text and 'single-stream' in _text
check('ghost link probed & dropped; file intact via the surviving link', ghost_ok and dropped, 
      f'(intact={ghost_ok}, used={sorted(x or "default" for x in used)})')
check('loud single-stream warning when only 1 connection stays', warned, '')

# ---- 8. _probe_links unit ----
p1 = lb._probe_links(['127.0.0.2'], 'http://127.0.0.1:8765/big.bin')
p2 = lb._probe_links(['203.0.113.7'], 'http://127.0.0.1:8765/big.bin')
p3 = lb._probe_links(['127.0.0.2'], 'not a url')
check('probe: loopback link reachable; reserved IP dead; junk URL -> []', p1 == ['127.0.0.2'] and p2 == [] and p3 == [],
      f'(p1={p1}, p2={p2}, p3={p3})')

print(f'\n{sum(res)}/{len(res)} checks passed')
sys.exit(0 if all(res) else 1)