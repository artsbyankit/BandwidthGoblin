"""Offline regression tests (stdlib only, no real network):

    python tests/test_core.py

Everything runs against a local ranged HTTP server over loopback links, so the
real engine (run_one, workers, stealing, resume) is exercised end to end."""
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bandwidthgoblin as bg

DATA = os.urandom(6 * 1024 * 1024)
SHA = hashlib.sha256(DATA).hexdigest()
ETAG = '"goblin-etag"'


class Server:
    """Ranged server. opts: strict_if_range, head_status, disposition, delay (s per 64 KB)."""
    def __init__(self, **opts):
        self.opts = opts
        self.sent = 0
        self.if_range = []
        self.starts = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def _hdr(self, code, n, extra=None):
                self.send_response(code)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(n))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('ETag', ETAG)
                if outer.opts.get('disposition'):
                    self.send_header('Content-Disposition', outer.opts['disposition'])
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()

            def do_HEAD(self):
                st = outer.opts.get('head_status')
                if st:
                    # e.g. a 405 whose small error body must NOT be taken as the file size
                    self.send_response(st)
                    self.send_header('Content-Length', '150')
                    self.end_headers()
                    return
                self._hdr(200, len(DATA))

            def _send(self, blob):
                delay = outer.opts.get('delay', 0)
                for i in range(0, len(blob), 65536):
                    part = blob[i:i + 65536]
                    self.wfile.write(part)
                    outer.sent += len(part)
                    if delay:
                        time.sleep(delay)

            def do_GET(self):
                rng = self.headers.get('Range')
                ir = self.headers.get('If-Range')
                if ir:
                    outer.if_range.append(ir)
                if ir and outer.opts.get('strict_if_range') and ir != ETAG:
                    rng = None   # RFC 9110: a validator mismatch means the full file
                try:
                    if rng:
                        a, b = rng[6:].split('-')
                        a, b = int(a), min(int(b or len(DATA) - 1), len(DATA) - 1)
                        outer.starts.append(a)
                        self._hdr(206, b - a + 1, {'Content-Range': f'bytes {a}-{b}/{len(DATA)}'})
                        self._send(DATA[a:b + 1])
                    else:
                        self._hdr(200, len(DATA))
                        self._send(DATA)
                except OSError:
                    self.close_connection = True

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.srv.handle_error = lambda *a: None
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.srv.server_address[1]}/file.bin'

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


TWO = [('a', '127.0.0.2'), ('b', '127.0.0.3')]
ONE = [('a', '127.0.0.2')]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.dir.name, 'file.bin')
        self.servers = []
        for p in (mock.patch.object(bg, 'notify', lambda *a, **k: None),
                  mock.patch.object(bg, 'OPEN_AFTER', False),
                  mock.patch.object(bg, 'NO_LOG', True),
                  mock.patch.object(bg, 'QUIET', True),
                  mock.patch.object(bg, 'perf_write', lambda *a, **k: None),
                  mock.patch.object(bg, '_hist_write', lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for s in self.servers:
            s.close()
        time.sleep(0.1)
        self.dir.cleanup()

    def server(self, **o):
        s = Server(**o)
        self.servers.append(s)
        return s

    def run_dl(self, url, links=TWO, rest=('auto', 'auto'), out=None):
        with mock.patch.object(bg, 'local_ips', lambda: list(links)):
            bg.reset_state()
            return bg.run_one(url, out or self.out, list(rest))

    def assertIntact(self, path=None):
        with open(path or self.out, 'rb') as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), SHA)

    def stop_after(self, seconds):
        def _s():
            time.sleep(seconds)
            bg.STOP = True
            bg.STOP_USER = True
        threading.Thread(target=_s, daemon=True).start()


class EngineTests(Base):
    def test_plain_download(self):
        self.assertEqual(self.run_dl(self.server().url), 'ok')
        self.assertIntact()

    def test_etag_sent_verbatim_in_if_range(self):
        # Bug: the ETag was wrapped in single quotes, so every standards-following
        # server said "file changed" and change protection silently switched off.
        s = self.server(strict_if_range=True)
        self.assertEqual(self.run_dl(s.url), 'ok')
        self.assertIntact()
        self.assertFalse(bg.VALIDATOR_BROKEN)
        self.assertTrue(s.if_range and all(v == ETAG for v in s.if_range))

    def test_head_refused_does_not_truncate(self):
        # Bug: a refused HEAD's error-body length was used as the file size.
        for status in (405, 403):
            with self.subTest(status=status):
                self.assertEqual(self.run_dl(self.server(head_status=status).url), 'ok')
                self.assertEqual(os.path.getsize(self.out), len(DATA))
                self.assertIntact()
                os.remove(self.out)

    def test_single_link_is_chunked_and_resumable(self):
        s = self.server(delay=0.03)
        self.stop_after(1.5)
        self.assertIsNone(self.run_dl(s.url, links=ONE))
        self.assertTrue(os.path.exists(self.out + '.resume'))
        self.assertGreater(bg.NCH, 1)
        s.opts['delay'] = 0
        s.starts.clear()
        self.assertEqual(self.run_dl(s.url, links=ONE), 'ok')
        self.assertIntact()
        self.assertNotIn(0, s.starts)

    def test_stop_keeps_part_finished_chunks(self):
        s = self.server(delay=0.02)
        self.stop_after(0.8)
        self.assertIsNone(self.run_dl(s.url, rest=('2', '1')))
        m = json.load(open(self.out + '.resume'))
        self.assertEqual(m['done'], [])            # neither big chunk finished...
        self.assertTrue(m.get('pos'))              # ...but their progress is on record
        s.opts['delay'] = 0
        s.starts.clear()
        self.assertEqual(self.run_dl(s.url, rest=('2', '1')), 'ok')
        self.assertIntact()
        self.assertNotIn(0, s.starts)              # nothing fetched again from byte 0

    def test_resume_survives_changed_chunk_plan(self):
        # Bug: a rerun with a different chunk count silently wiped the partial file.
        s = self.server(delay=0.005)
        self.stop_after(1.0)
        self.run_dl(s.url, rest=('8', '1'))
        s.opts['delay'] = 0
        s.starts.clear()
        self.assertEqual(self.run_dl(s.url, rest=('16', '1')), 'ok')
        self.assertEqual(bg.NCH, 8)                # the saved plan was kept
        self.assertIntact()
        self.assertNotIn(0, s.starts)

    def test_existing_file_is_not_overwritten(self):
        with open(self.out, 'wb') as f:
            f.write(b'precious')
        self.assertEqual(self.run_dl(self.server().url), 'ok')
        self.assertEqual(open(self.out, 'rb').read(), b'precious')
        self.assertIntact(os.path.join(self.dir.name, 'file (2).bin'))

    def test_quick_successive_runs_leave_no_ghost_files(self):
        # Bug: a helper thread from run A woke inside run B and rewrote A's .resume.
        s = self.server()
        for i in range(3):
            self.assertEqual(self.run_dl(s.url, out=os.path.join(self.dir.name, f'q{i}.bin')), 'ok')
        time.sleep(1.5)
        self.run_dl(s.url, out=os.path.join(self.dir.name, 'q3.bin'))
        leftovers = [f for f in os.listdir(self.dir.name) if f.endswith(('.resume', '.progress'))]
        self.assertEqual(leftovers, [])

    def test_disk_full_stops_cleanly_with_clear_reason(self):
        real = os.write

        def full(fd, b):
            if isinstance(b, memoryview):
                raise OSError(28, 'No space left on device')
            return real(fd, b)
        with mock.patch.object(bg.os, 'write', full):
            t0 = time.time()
            res = self.run_dl(self.server().url)
        self.assertIsNone(res)
        self.assertLess(time.time() - t0, 15)      # no minutes of pointless retries
        self.assertTrue(os.path.exists(self.out + '.resume'))

    def test_watchdog_marks_a_user_level_stop(self):
        # Bug: missing `global` made the watchdog's STOP_USER a throwaway local.
        bg.reset_state()
        bg.RUN_DONE.clear()
        bg.T0 = bg.LAST_PROGRESS = time.time() - 9999
        bg.NCH = 2
        bg.CHUNKS_DONE = 0
        with mock.patch.object(bg.time, 'sleep', lambda s: None):
            bg.watchdog()
        self.assertTrue(bg.STOP and bg.STOP_USER)
        bg.reset_state()


class LogicTests(unittest.TestCase):
    def test_guess_name_cannot_escape_the_folder(self):
        for url in ('http://x.y/a%2F..%2F..%2Fevil.bat', 'http://x.y/..%5C..%5Cevil.bat'):
            n = bg.guess_name(url)
            self.assertEqual(n, 'evil.bat')

    def test_guess_name_makes_windows_safe(self):
        self.assertEqual(bg.guess_name('http://x.y/a%3Fb%3Ac.iso'), 'a_b_c.iso')
        self.assertEqual(bg.guess_name('http://x.y/CON.txt'), '_CON.txt')

    def test_ipconfig_in_any_language(self):
        for label in ('IPv4 Address. . . . . . . . . . . :', 'IPv4-Adresse  . . . . . . . . . . :',
                      'Adresse IPv4. . . . . . . . . . . . . .:', 'Dirección IPv4. . . . . . . . . :'):
            got = bg._parse_ipconfig(f'Ethernet adapter E:\n   {label} 192.168.1.46(Preferred)\n')
            self.assertEqual([ip for _, ip in got], ['192.168.1.46'])

    def test_queue_file_with_notepad_bom(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'q.txt')
            with open(p, 'wb') as f:
                f.write('\ufeffhttp://x.y/a.bin\nhttp://x.y/b.bin föö.bin\n'.encode('utf-8'))
            items = bg._read_queue(p)
        self.assertEqual(items, [('http://x.y/a.bin', None), ('http://x.y/b.bin', 'föö.bin')])

    def test_content_disposition_names(self):
        self.assertEqual(bg._disposition_name('attachment; filename="a b.iso"'), 'a b.iso')
        self.assertEqual(bg._disposition_name("attachment; filename*=UTF-8''f%C3%B6%C3%B6.zip"), 'föö.zip')
        self.assertEqual(bg._disposition_name('attachment; filename="../../evil.exe"'), 'evil.exe')
        self.assertEqual(bg._disposition_name('inline'), '')

    def test_arrow_keys_are_not_commands(self):
        # Bug: on Linux/macOS an arrow key (ESC [ A) aborted the download.
        self.assertEqual(bg._parse_keys('\x1b[A\x1b[B'), ['up', 'down'])
        self.assertEqual(bg._parse_keys('\x1b'), ['esc'])
        self.assertEqual(bg._parse_keys('p'), ['p'])
        self.assertEqual(bg._parse_keys('\x1b[5~'), [])

    def test_r_only_resumes(self):
        # Bug: R toggled, so pressing "resume" while running paused the download.
        with mock.patch.object(bg, 'PAUSED', False):
            bg._handle_key('r')
            self.assertFalse(bg.PAUSED)
            bg._handle_key('p')
            self.assertTrue(bg.PAUSED)
            bg._handle_key('r')
            self.assertFalse(bg.PAUSED)

    def test_failed_log_write_kills_nothing(self):
        # Bug: a refused log write raised inside log() and killed the calling thread.
        with mock.patch.object(bg, 'LOGFILE', os.devnull), mock.patch.object(bg, 'NO_LOG', False), \
                mock.patch.object(bg, 'DASH_ON', True), mock.patch.object(bg, 'PAUSED', False), \
                mock.patch('builtins.open', side_effect=PermissionError('locked')):
            bg.log('hello')
            bg._bg_flush()
            bg._handle_key('p')
            bg._bg_flush()
            self.assertTrue(bg.PAUSED)
            self.assertTrue(bg._BG_THREAD.is_alive())

    def test_slow_log_drive_does_not_block_downloaders(self):
        # Bug: workers wrote logs themselves; a slow drive froze the download.
        real = open
        gate = threading.Event()

        def slow(*a, **k):
            gate.wait(5)
            return real(*a, **k)
        with mock.patch.object(bg, 'LOGFILE', os.devnull), mock.patch.object(bg, 'NO_LOG', False), \
                mock.patch.object(bg, 'DASH_ON', True), mock.patch('builtins.open', slow):
            t0 = time.time()
            for _ in range(50):
                bg.log('x')
            self.assertLess(time.time() - t0, 0.5)
            gate.set()
            bg._bg_flush()

    def test_speed_cap_never_freezes_the_download(self):
        with mock.patch.object(bg, 'SPEEDLIMIT', 0.0), mock.patch.dict(bg._TUI, {'speed': 3.0}):
            bg._handle_key('-')
            self.assertEqual(bg.SPEEDLIMIT, 6e6)
            for _ in range(40):
                bg._handle_key('-')
            self.assertEqual(bg.SPEEDLIMIT, 100e3)

    def test_speed_limit_keys(self):
        with mock.patch.object(bg, 'SPEEDLIMIT', 0.0), mock.patch.dict(bg._TUI, {'speed': 8e6}):
            bg._handle_key('-')
            self.assertEqual(bg.SPEEDLIMIT, 6e6)
            bg._handle_key('+')
            self.assertEqual(bg.SPEEDLIMIT, 8e6)
            bg._handle_key('0')
            self.assertEqual(bg.SPEEDLIMIT, 0.0)


class ScreenTests(unittest.TestCase):
    S = {'name': 'ubuntu-日本語-24.04-desktop-amd64.iso', 'tot': 1.2e9, 'size': 2e9, 'speed': 12.4e6,
         'avg': 10e6, 'peak': 20e6, 'eta': 65, 'elapsed': 100, 'paused': False, 'limit': 0, 'ver': 'v2.0 b48',
         'links': [{'ip': '10.0.0.%d' % i, 'label': f'Wi-Fi (10.0.0.{i})', 'speed': 3e6 * i, 'total': 4e8,
                    'conns': 1, 'status': 'on', 'hist': [1e6 * i] * 40, 'color': 'sky'} for i in range(1, 4)],
         'chunks': ['done'] * 20 + [0, 1, 2] + ['wait'] * 41, 'ndone': 20, 'hist': [float(i % 50) * 1e6 for i in range(400)],
         'log': ['12:00:00 something happened \x1b[31mred\x1b[0m'] * 8, 'help': False, 'panels': {'net', 'links', 'chunks', 'log'},
         'sel': 1, 'sort': 0, 'msg': '', 'mode': 'simple'}

    def test_every_frame_fits_exactly(self):
        for ascii_only in (False, True):
            C, G = bg._tui_style(color=True, unicode_ok=not ascii_only, braille=not ascii_only)
            for mode in ('simple', 'advanced'):
                for W in (10, 30, 59, 60, 80, 120, 200):
                    for H in (3, 8, 13, 14, 24, 50):
                        for extra in ({}, {'help': True}, {'size': 0, 'eta': 0}, {'paused': True}, {'links': []}):
                            S = dict(self.S, mode=mode, **extra)
                            fr = bg._frame(S, W, H, C, G)
                            self.assertEqual(len(fr), H, (mode, W, H, extra))
                            for ln in fr:
                                self.assertLessEqual(bg._vis_len(ln), W, (mode, W, H, extra, ln))

    def test_simple_mode_stays_plain(self):
        C, G = bg._tui_style(color=False, unicode_ok=True, braille=True)
        txt = '\n'.join(bg._frame(self.S, 80, 24, C, G))
        self.assertIn('60%', txt)
        self.assertIn('12.4 MB/s', txt)
        for jargon in ('chunk', 'ETag', 'x1', '10.0.0.'):
            self.assertNotIn(jargon, txt)

    def test_advanced_mode_has_all_panels(self):
        C, G = bg._tui_style(color=False, unicode_ok=True, braille=True)
        txt = '\n'.join(bg._frame(dict(self.S, mode='advanced'), 120, 40, C, G))
        for word in ('net', 'links', 'chunks', 'log', '10.0.0.2'):
            self.assertIn(word, txt)


if __name__ == '__main__':
    unittest.main(verbosity=1)
