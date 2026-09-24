"""In-process test: a save-drive shortfall must fail run_one with 'disk-space'
(not 'error'), leave NO partial/.resume behind, and _yt_split_download must
map it to a 'not enough disk space' reason that _yt_run treats as a clean stop
instead of a single-connection yt-dlp fallback.
Run:  python tests/inproc_disk_check.py
"""
import os, sys, tempfile, http.server, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bandwidthgoblin as b

data = os.urandom(8 * 1024 * 1024)   # 8 MiB payload

class _H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def _ok(self, code, n, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(n))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', '"disktest"')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
    def do_HEAD(self):
        self._ok(200, len(data))
    def do_GET(self):
        rng = self.headers.get('Range')
        if rng and rng.startswith('bytes='):
            a_s, b_s = rng[6:].split(',')[0].split('-', 1)
            a = int(a_s) if a_s else 0
            b = int(b_s) if b_s else len(data) - 1
            b = min(b, len(data) - 1)
            self._ok(206, b - a + 1, {'Content-Range': f'bytes {a}-{b}/{len(data)}'})
            try: self.wfile.write(data[a:b + 1])
            except Exception: pass
        else:
            self._ok(200, len(data))
            try: self.wfile.write(data)
            except Exception: pass
    def log_message(self, *a):
        pass

srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

_saved = (b.local_ips, os.environ.get('MULTILINK_IFACES'),
          b.notify, b.OPEN_AFTER, b.QUIET, b.NO_LOG)
b.local_ips = lambda: [('disk-a', '127.0.0.2'), ('disk-b', '127.0.0.3')]
os.environ.pop('MULTILINK_IFACES', None)
b.notify = lambda *a, **kw: None
b.OPEN_AFTER = False
b.QUIET = True
b.NO_LOG = True

# Only 1 MiB free -> any real download must stop at the preflight.
b.shutil.disk_usage = lambda p: type('X', (), {'free': 1 * 1024 * 1024})()

od = tempfile.mkdtemp(prefix='bwg_disk_')
out = os.path.join(od, 'disk.bin')

orig = b.run_one
ok = True
try:
    b.reset_state()
    res = b.run_one(f'http://127.0.0.1:{port}/disk.bin', out, ['4', '2'])
    leftover = [f for f in ('disk.bin', 'disk.bin.resume', 'disk.bin.progress')
                if os.path.exists(os.path.join(od, f))]
    print(f'run_one returned {res!r}, leftover files: {leftover or "none"}')
    ok = (res == 'disk-space') and not leftover
    print('PASS: disk shortfall -> disk-space, no partials' if ok else
          'FAIL: ' + (f'unexpected return {res!r}' if res != 'disk-space'
                      else f'unexpected leftovers {leftover}'))

    # Plumbing: _yt_split_download must map 'disk-space' to the disk reason
    # (so _yt_run stops instead of falling back to single-connection yt-dlp).
    def _fake_run_one(u, t, rest):
        return 'disk-space'
    b.run_one = _fake_run_one
    try:
        info = {'requested_downloads': [{'requested_formats': [{'format_id': '701', 'ext': 'mp4',
                'url': f'http://127.0.0.1:{port}/disk.bin'}]}],
                'title': 'disk', 'id': 'disktest'}
        got = b._yt_split_download(info, od, 'ff', [])
        reason = got[1] if isinstance(got, tuple) else got
        ok2 = isinstance(got, tuple) and got[0] is None and 'not enough disk space' in reason
        print(f'_yt_split_download -> {reason!r}')
        print('PASS: disk reason propagated (no yt-dlp fallback)' if ok2 else 'FAIL: reason not propagated')
        ok = ok and ok2
    finally:
        b.run_one = orig
finally:
    srv.shutdown(); srv.server_close()
    b.local_ips = _saved[0]
    if _saved[1] is not None: os.environ['MULTILINK_IFACES'] = _saved[1]
    else: os.environ.pop('MULTILINK_IFACES', None)
    b.notify, b.OPEN_AFTER, b.QUIET, b.NO_LOG = _saved[2], _saved[3], _saved[4], _saved[5]
    try:
        for f in os.listdir(od): os.remove(os.path.join(od, f))
        os.rmdir(od)
    except Exception:
        pass

sys.exit(0 if ok else 1)