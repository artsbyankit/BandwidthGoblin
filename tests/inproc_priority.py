"""In-process test: priority tier must raise EVERY link's connections + caps.
- normal (default): AUTO -> 1 connection per link, history-based caps.
- high (MULTILINK_PRIORITY=high): AUTO -> 4 connections per link, caps 4.
Run:  python tests/inproc_priority.py
"""
import os, sys, tempfile, http.server, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bandwidthgoblin as b

data = os.urandom(4 * 1024 * 1024)

class _H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def _ok(self, code, n, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(n))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', '"priotest"')
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()
    def do_HEAD(self): self._ok(200, len(data))
    def do_GET(self):
        rng = self.headers.get('Range')
        if rng and rng.startswith('bytes='):
            a_s, b_s = rng[6:].split(',')[0].split('-', 1)
            a = int(a_s) if a_s else 0
            b = min(int(b_s) if b_s else len(data) - 1, len(data) - 1)
            self._ok(206, b - a + 1, {'Content-Range': f'bytes {a}-{b}/{len(data)}'})
            try: self.wfile.write(data[a:b + 1])
            except Exception: pass
        else:
            self._ok(200, len(data))
            try: self.wfile.write(data)
            except Exception: pass
    def log_message(self, *a): pass

srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _H)
srv.handle_error = lambda *a: None
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

_saved = (b.local_ips, os.environ.get('MULTILINK_IFACES'), os.environ.get('MULTILINK_PRIORITY'),
          b.notify, b.OPEN_AFTER, b.QUIET, b.NO_LOG)
b.local_ips = lambda: [('prio-a', '127.0.0.2'), ('prio-b', '127.0.0.3')]
os.environ.pop('MULTILINK_IFACES', None)
os.environ.pop('MULTILINK_PRIORITY', None)
b.notify = lambda *a, **kw: None
b.OPEN_AFTER = False
b.QUIET = True
b.NO_LOG = True

ok = True
try:
    od = tempfile.mkdtemp(prefix='bwg_prio_')
    url = f'http://127.0.0.1:{port}/prio.bin'

    # --- normal tier: AUTO -> history-tier caps (1-3), never maxed at 4 ---
    b.reset_state()
    out1 = os.path.join(od, 'normal.bin')
    r1 = b.run_one(url, out1, ['4', 'auto'])
    t1 = dict(b.LINK_THREADS); c1 = dict(b.WORKER_CAP)
    ok1 = (r1 == 'ok' and all(1 <= v <= 3 for v in t1.values())
           and all(1 <= v <= 3 for v in c1.values()))
    print(f'normal: res={r1!r} threads={t1} caps={c1}')
    print('PASS: normal tier = history-based caps (1-3), not maxed' if ok1 else 'FAIL: normal tier wrong')

    # --- high tier: AUTO -> 4 connections per link, caps maxed ---
    os.environ['MULTILINK_PRIORITY'] = 'high'
    b.reset_state()
    out2 = os.path.join(od, 'high.bin')
    r2 = b.run_one(url, out2, ['4', 'auto'])
    t2 = dict(b.LINK_THREADS); c2 = dict(b.WORKER_CAP)
    ok2 = (r2 == 'ok' and all(v == 4 for v in t2.values()) and all(v == 4 for v in c2.values()))
    print(f'high: res={r2!r} threads={t2} caps={c2}')
    print('PASS: high tier = 4 connections per link, caps 4' if ok2 else 'FAIL: high tier wrong')

    ok = ok1 and ok2
finally:
    srv.shutdown(); srv.server_close()
    b.local_ips = _saved[0]
    if _saved[1] is not None: os.environ['MULTILINK_IFACES'] = _saved[1]
    else: os.environ.pop('MULTILINK_IFACES', None)
    if _saved[2] is not None: os.environ['MULTILINK_PRIORITY'] = _saved[2]
    else: os.environ.pop('MULTILINK_PRIORITY', None)
    b.notify, b.OPEN_AFTER, b.QUIET, b.NO_LOG = _saved[3], _saved[4], _saved[5], _saved[6]
    try:
        for f in os.listdir(od): os.remove(os.path.join(od, f))
        os.rmdir(od)
    except Exception:
        pass

sys.exit(0 if ok else 1)