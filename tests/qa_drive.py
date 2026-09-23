"""QA driver: exercises LinkBoost's real engine against the local QA server
using two synthetic loopback links (127.0.0.2/3), so multi-link logic is
tested end-to-end with zero internet and one physical connection.

Usage: python qa_drive.py <scenario>
  scenario: multi | resume | norange
"""
import json, os, sys, hashlib

ROOT = r'C:\Users\ankit\Documents\LinkBoost'
QA = r'C:\Users\ankit\AppData\Local\Temp\opencode\lb_qa'
SERVED = os.path.join(QA, 'big.src')          # the deterministic 4 MiB payload
URL = 'http://127.0.0.1:8765'

sys.path.insert(0, ROOT)
import linkboost as lb

with open(SERVED, 'rb') as f:
    FULL = f.read()
SHA_FULL = hashlib.sha256(FULL).hexdigest()
SHA_FIRST256K = hashlib.sha256(FULL[:262144]).hexdigest()

# Two synthetic on-link loopback links - Windows allows arbitrary 127.0.0.x
# sources when connecting to the loopback network.
lb.local_ips = lambda: [('qa-a', '127.0.0.2'), ('qa-b', '127.0.0.3')]

scenario = sys.argv[1]

def run(url, out, tune):
    sys.argv = ['linkboost.py', url, out] + tune.split()
    lb.main()

if scenario == 'multi':
    out = os.path.join(QA, 'multi.bin')
    run(f'{URL}/big.bin', out, '4 auto')
    with open(out, 'rb') as f:
        got = hashlib.sha256(f.read()).hexdigest()
    print(f'[multi] sha match: {got == SHA_FULL}')
    raise SystemExit(0 if got == SHA_FULL else 1)

elif scenario == 'resume':
    out = os.path.join(QA, 'res.bin')
    half = len(FULL) // 2
    with open(out, 'wb') as f:
        f.write(FULL[:half])        # chunks 0-7 already on disk
        f.write(b'\x00' * (len(FULL) - half))
    manifest = {'url': f'{URL}/big.bin', 'size': len(FULL), 'chunks': 16,
                'done': list(range(8)), 'etag': '"qa-big"', 'lastmod': '', 'build': 24}
    with open(out + '.resume', 'w') as f:
        json.dump(manifest, f)
    run(f'{URL}/big.bin', out, '16 auto')
    with open(out, 'rb') as f:
        got = hashlib.sha256(f.read()).hexdigest()
    leftover = os.path.exists(out + '.resume')
    print(f'[resume] sha match: {got == SHA_FULL}, resume removed at end: {not leftover}')
    raise SystemExit(0 if (got == SHA_FULL and not leftover) else 1)

elif scenario == 'norange':
    out = os.path.join(QA, 'norange.bin')
    run(f'{URL}/norange.bin', out, '4 auto')
    with open(out, 'rb') as f:
        got = f.read()
    ok = hashlib.sha256(got).hexdigest() == SHA_FIRST256K and len(got) == 262144
    print(f'[norange] fell back to single stream and got exact bytes: {ok}')
    raise SystemExit(0 if ok else 1)

else:
    print(f'unknown scenario {scenario}')
    raise SystemExit(2)