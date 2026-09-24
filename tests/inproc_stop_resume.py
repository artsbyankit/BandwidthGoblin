"""In-process stop/resume probe (build 47 audit).

Runs the REAL _yt_run / _yt_split_download / run_one code in a thread and
simulates an ESC press by setting the module globals STOP + STOP_USER (the
exact flags _esc_listener sets). Phase 1 stops mid-video-stream; the probe
inspects what survived on disk. 'both' then re-runs without stop to see if
resume engages.

  python tests/inproc_stop_resume.py URL QUAL OUTDIR [STOP_AFTER] [phases]

phases: stop-only | both   (default stop-only)
"""
import glob
import json
import os
import sys
import threading
import time

URL = sys.argv[1] if len(sys.argv) > 1 else 'https://youtu.be/Wtg_s1GQiMU'
QUAL = sys.argv[2] if len(sys.argv) > 2 else 'best'
OUT = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.environ.get('TEMP', '.'), 'bg_inproc')
STOP_AFTER = float(sys.argv[4]) if len(sys.argv) > 4 else 20.0
PHASES = sys.argv[5] if len(sys.argv) > 5 else 'stop-only'

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)  # repo root (logs/ lives here)
sys.path.insert(0, _ROOT)
sys.argv = ['bandwidthgoblin.py']  # module must not treat args as flags
import bandwidthgoblin as bg  # noqa: E402


def state(tag):
    files = sorted(glob.glob(os.path.join(OUT, '*')))
    hid = sorted(glob.glob(os.path.join(OUT, '.*')))
    print(f'[probe:{tag}] visible: {[os.path.basename(f) for f in files] or "NONE"}')
    print(f'[probe:{tag}] hidden(tmp/resume/progress): {[os.path.basename(h) for h in hid] or "NONE"}')


def phase(tag, stop_after):
    res = {}

    def go():
        try:
            res['rc'] = bg._yt_run(URL, True, {'out-dir': [OUT]}, QUAL, [])
        except BaseException as ex:  # noqa: BLE001 - report anything
            res['ex'] = f'{type(ex).__name__}: {ex}'

    t = threading.Thread(target=go, daemon=True)
    t.start()
    if stop_after:
        time.sleep(stop_after)
        print(f'[probe:{tag}] ESC sim: STOP=STOP_USER=True at ~{stop_after}s')
        bg.STOP = True
        bg.STOP_USER = True
    t.join(420)
    print(f'[probe:{tag}] _yt_run -> {res.get("rc")!r} {res.get("ex", "")}')


def runlog_markers(since):
    tot = {}
    for fn in sorted(glob.glob(os.path.join('logs', '*.log')), key=os.path.getmtime):
        try:
            if os.path.getmtime(fn) < since:
                continue
            for ln in open(fn, errors='replace'):
                s = ln.strip()
                for k in ('resume saved', 'resuming: skipping', 'resume rejected',
                          'STOPPED', 'all chunks done', 'size:', 're-signed'):
                    if k in s:
                        tot.setdefault(os.path.basename(fn), []).append(s[:150])
        except OSError:
            pass
    for fn, lines in sorted(tot.items()):
        print(f'[probe:runlog] --- {fn}')
        for ln in lines[-6:]:
            print(f'[probe:runlog]    {ln}')


os.makedirs(OUT, exist_ok=True)
state('before')
t0 = time.time()
phase('stop1', STOP_AFTER)
state('after-stop')
runlog_markers(t0 - 1)
if PHASES == 'both':
    print('[probe] phase 2: same download, no stop - resume must engage')
    bg.STOP = False
    bg.STOP_USER = False
    t1 = time.time()
    phase('resume', 0)
    state('final')
    runlog_markers(t1 - 1)
print('[probe] done')