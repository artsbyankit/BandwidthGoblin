"""QA-only local HTTP server for LinkBoost. Stdlib only.

Serves:
  /big.bin   4 MiB random payload, FULL byte-Range support (206 + Content-Range,
             single ranges only), ETag + If-Range validation, Accept-Ranges.
  /tiny.txt  a 5-byte file (exercises the small-file chunk clamp end-to-end).
  /nohead.bin HEAD -> 405, GET -> 200 (server that refuses HEAD, like some CDNs).

Run: python qa_server.py <port> <payload-path>
"""
import http.server, os, sys

def make_handler(payload, payload_path, tiny=b'hello'):
    SIZE = len(payload)
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'LinkBoostQA/1.0'

        def _send(self, code, ctype, length, extra=None):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()

        def _range_of(self):
            rng = self.headers.get('Range')
            if not rng or not rng.startswith('bytes='):
                return None
            spec = rng[6:].split(',')[0]
            a_s, b_s = spec.split('-', 1)
            return (int(a_s) if a_s else 0, int(b_s) if b_s else None)

        def _echo_body(self):
            parts = []
            for k in ('Referer', 'Cookie', 'X-Qa'):
                parts.append('%s=%s' % (k, self.headers.get(k) or '-'))
            return ('\n'.join(parts) + '\n').encode()

        def do_HEAD(self):
            p = self.path.split('?')[0]
            if p == '/big.bin':
                self._send(200, 'application/octet-stream', SIZE, {'ETag': '"qa-big"'})
            elif p == '/tiny.txt':
                self._send(200, 'text/plain', len(tiny), {'ETag': '"qa-tiny"'})
            elif p == '/norange.bin':
                self._send(200, 'application/octet-stream', 262144, {'ETag': '"qa-norange"'})
            elif p == '/echo-headers':
                self._send(200, 'text/plain', len(self._echo_body()), {'ETag': '"qa-echo"'})
            else:
                self.send_response(405 if p == '/nohead.bin' else 404)
                self.send_header('Content-Length', '0')
                self.end_headers()

        def do_GET(self):
            p = self.path.split('?')[0]
            if p == '/big.bin':
                if not self._serve_range(payload, SIZE, '"qa-big"', 'application/octet-stream'):
                    pass
            elif p == '/tiny.txt':
                self._serve_range(tiny, len(tiny), '"qa-tiny"', 'text/plain')
            elif p == '/norange.bin':
                # Sends 200 full bodies even when Range is requested - the
                # exact behaviour LinkBoost must detect, drop the link, and
                # fall back to single-stream for.
                body = payload[:262144]
                if self.headers.get('Range'):
                    self._send(200, 'application/octet-stream', len(body),
                               {'ETag': '"qa-norange"', 'Content-Length': str(len(body))})
                else:
                    self._send(200, 'application/octet-stream', len(body), {'ETag': '"qa-norange"'})
                self.wfile.write(body)
            elif p == '/echo-headers':
                body = self._echo_body()
                self._send(200, 'text/plain', len(body), {'ETag': '"qa-echo"'})
                self.wfile.write(body)
            elif p == '/nohead.bin':
                self._serve_range(payload[:262144], 262144, '"qa-nohead"', 'application/octet-stream')
            else:
                self._send(404, 'text/plain', 0)

        def _serve_range(self, body, size, etag, ctype):
            total = size
            rng = self._range_of()
            ir = self.headers.get('If-Range')
            if ir and str(ir).strip('"') not in (str(etag).strip('"'),):
                # If-Range mismatch -> full 200 body
                self._send(200, ctype, total, {'ETag': etag})
                self.wfile.write(body)
                return True
            if rng is None:
                self._send(200, ctype, total, {'ETag': etag})
                self.wfile.write(body)
                return True
            a, b = rng
            if b is None:
                b = total - 1
            b = min(b, total - 1)
            if a > b:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{total}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return True
            self.send_response(206)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Range', f'bytes {a}-{b}/{total}')
            self.send_header('Content-Length', str(b - a + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', etag)
            self.end_headers()
            mv = memoryview(body)
            self.wfile.write(mv[a:b + 1])
            return True

        def log_message(self, *a):
            pass
    return H

if __name__ == '__main__':
    port = int(sys.argv[1])
    payload_path = sys.argv[2]
    with open(payload_path, 'rb') as f:
        payload = f.read()
    handler = make_handler(payload, payload_path)
    srv = http.server.ThreadingHTTPServer(('', port), handler)
    print(f'qa-server on :{port} payload={len(payload)} bytes', flush=True)
    srv.serve_forever()