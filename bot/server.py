"""Loopback-only HTTP API. Receives evidence, never submits exchange orders."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from .engine import analyze, stamp, EvidenceError
from .report import render

PAGE = Path(__file__).with_name('index.html')
MAX_BODY = 2_000_000


class Handler(BaseHTTPRequestHandler):
    def send(self, code, data, mime='application/json; charset=utf-8'):
        payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self):
        valid = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in valid or origin and origin not in {'http://'+v for v in valid}:
            self.send(403, {'error': 'Только локальный доступ с того же сайта'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == '/':
            self.send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
        elif self.path == '/health':
            self.send(200, {'status': 'ok', 'system': 'VP-SMC-CVD', 'execution': False})
        else:
            self.send(404, {'error': 'Не найдено'})

    def do_POST(self):
        if not self.allowed():
            return
        if self.path != '/api/analyze':
            self.send(404, {'error': 'Не найдено'})
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.send(415, {'error': 'Требуется application/json'})
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                self.send(413, {'error': 'Допустимый размер JSON: 1–2000000 байт'})
                return
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(length), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            if not isinstance(data, dict):
                raise ValueError('Требуется JSON-объект')
            result = analyze(data)
            if 'as_of' in data and not 0 <= (datetime.now(timezone.utc)-stamp(data['as_of'])).total_seconds() <= 60:
                reason = 'Снимок устарел или датирован будущим: показан только анализ данных, вход запрещён'
                result['filters']['snapshot_freshness'] = {'passed': False, 'detail': reason}
                result['decision'] = 'ПРОПУСК'
                result['trade'] = None
                result['reasons'].insert(0, reason)
            self.send(200, {'analysis': result, 'report': render(result)})
        except (ValueError, TypeError, TimeoutError, RecursionError) as exc:
            self.send(400, {'error': str(exc)})


def serve(port=8765):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'VP-SMC-CVD: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
