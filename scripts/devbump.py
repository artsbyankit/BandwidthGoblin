"""Dev helper: bump BandwidthGoblin BUILD by 1, add a CHANGELOG entry,
recompile, and sync all 4 script copies. Stdlib only.

Usage:
    py devbump.py "what changed"
    py devbump.py            (asks interactively)
"""
import os
import re
import sys
import py_compile
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # repo root - devbump.py lives in scripts/
MAIN = os.path.join(ROOT, 'bandwidthgoblin.py')
COPIES = [os.path.join(ROOT, p) for p in (
    'bandwidthgoblin.py',
    os.path.join('pack', 'windows', 'bandwidthgoblin.py'),
    os.path.join('pack', 'mac', 'bandwidthgoblin.py'),
    os.path.join('pack', 'linux', 'bandwidthgoblin.py'),
)]
CHANGELOG = os.path.join(ROOT, 'CHANGELOG.md')


def sha(p):
    with open(p, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    msg = ' '.join(sys.argv[1:]).strip()
    if not msg:
        try:
            msg = input('what changed? ').strip()
        except EOFError:
            msg = ''
    if not msg:
        print('nothing to record, aborting.')
        return 1
    with open(MAIN, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r"^VERSION = '([^']+)'\nBUILD = (\d+)", src, re.M)
    if not m:
        print('VERSION/BUILD not found in bandwidthgoblin.py')
        return 1
    ver, build = m.group(1), int(m.group(2)) + 1
    src = src[:m.start()] + f"VERSION = '{ver}'\nBUILD = {build}" + src[m.end():]
    with open(MAIN, 'w', encoding='utf-8', newline='') as f:
        f.write(src)
    with open(CHANGELOG, encoding='utf-8') as f:
        cl = f.read()
    anchor = 'Log line 1 of every download carries `v{VERSION} build {BUILD}`.\n'
    entry = f'\n## v{ver} build {build} (dev)\n- {msg}\n'
    if anchor in cl:
        cl = cl.replace(anchor, anchor + entry, 1)
    else:
        cl = cl.rstrip() + '\n' + entry
    with open(CHANGELOG, 'w', encoding='utf-8', newline='') as f:
        f.write(cl)
    py_compile.compile(MAIN, doraise=True)
    for c in COPIES[1:]:
        with open(MAIN, 'rb') as fsrc, open(c, 'wb') as fdst:
            fdst.write(fsrc.read())
    base = sha(COPIES[0])
    ok = all(sha(c) == base for c in COPIES)
    print(f'bumped to v{ver} build {build}, all copies match: {ok}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
