#!/usr/bin/env python3
"""GOST Panel: dependency-free HTTPS API and Chinese web console."""
import argparse
import json
import os
import secrets
import ssl
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cache import AssetCache, DownloadError
from core import Store, digest, password_hash, verify_password
from installers import install_command, install_script

ROOT = Path(__file__).resolve().parent


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, public_url, certificate, secure):
        super().__init__(address, Handler)
        self.store, self.public_url = store, public_url
        self.certificate, self.secure = certificate, secure
        self.assets = AssetCache(store.directory / 'downloads')
        self.login_attempts, self.login_lock = {}, threading.Lock()
        self.slots = threading.BoundedSemaphore(64)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(20)
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'GOSTPanel'

    def log_message(self, *_):
        # Enrollment URLs contain one-time secrets. Never put request paths in logs.
        pass

    def respond(self, status, value, content_type='application/json; charset=utf-8', extra=None):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False).encode()
        elif isinstance(value, str):
            value = value.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(value)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if self.server.secure:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(value)

    def send_asset(self, path):
        # Stream a validated cache file, without buffering the archive in memory.
        self.connection.settimeout(120)
        with path.open('rb') as source:
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Length', str(os.fstat(source.fileno()).st_size))
            self.send_header('Content-Disposition', 'attachment; filename="' + path.name + '"')
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            for chunk in iter(lambda: source.read(128 * 1024), b''):
                self.wfile.write(chunk)

    def body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 65536:
            raise ValueError('请求内容为空或过大')
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError('请求需要 JSON 对象')
        return value

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        token = cookie.get('gost_session')
        if not token:
            return None
        store = self.server.store
        with store.lock:
            row = store.db.execute('SELECT * FROM sessions WHERE token_hash=? AND expires>?', (digest(token.value), time.time())).fetchone()
            return dict(row) if row else None

    def cookie(self, token, age):
        return 'gost_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + str(age) + ('; Secure' if self.server.secure else '')

    def node_identity(self):
        auth = self.headers.get('Authorization', '')
        return self.server.store.authenticate_node(auth[7:]) if auth.startswith('Bearer ') else None

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def handle_request(self):
        try:
            self.route()
        except DownloadError as exc:
            self.respond(503, {'error': str(exc)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.respond(400, {'error': str(exc) or '输入无效'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as exc:
            print('Request failed: ' + type(exc).__name__, flush=True)
            self.respond(500, {'error': '服务器处理失败，请查看面板服务日志'})

    def route(self):
        path = urllib.parse.urlsplit(self.path).path
        method, store = self.command, self.server.store
        if method == 'GET' and path in ('/', '/app.js', '/style.css'):
            filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
            content_type = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
            return self.respond(200, (ROOT / 'static' / filename).read_bytes(), content_type + '; charset=utf-8')
        if path == '/healthz' and method == 'GET':
            return self.respond(200, {'ok': True})
        if path == '/api/login' and method == 'POST':
            data = self.body()
            now, ip = time.time(), self.client_address[0]
            with self.server.login_lock:
                attempts = self.server.login_attempts
                self.server.login_attempts = attempts = {key: [t for t in times if now - t < 300] for key, times in attempts.items() if times and now - times[-1] < 300}
                times = attempts.setdefault(ip, [])
                if len(times) >= 10:
                    return self.respond(429, {'error': '登录尝试过多，请 5 分钟后重试'})
                times.append(now)
            with store.lock, store.db:
                saved = store.db.execute("SELECT value FROM settings WHERE key='password'").fetchone()[0]
                if not verify_password(str(data.get('password', '')), saved):
                    return self.respond(401, {'error': '密码错误'})
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                store.db.execute('DELETE FROM sessions WHERE expires<?', (now,))
                store.db.execute('INSERT INTO sessions VALUES (?,?,?)', (digest(token), csrf, now + 43200))
                store.audit('管理员登录')
            return self.respond(200, {'csrf': csrf}, extra={'Set-Cookie': self.cookie(token, 43200)})
        if path.startswith('/downloads/gost/') and method == 'GET':
            auth = self.headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                return self.respond(401, {'error': '需要有效的节点安装凭证'})
            try:
                with store.lock:
                    store.bootstrap_node(auth[7:])
            except ValueError:
                return self.respond(401, {'error': '安装凭证已过期或已使用，请重新生成脚本'})
            arch = path[len('/downloads/gost/'):]
            return self.send_asset(self.server.assets.get(arch))
        if path.startswith('/install/') and method == 'GET':
            token = path[len('/install/'):]
            with store.lock:
                store.bootstrap_node(token)
            return self.respond(200, install_script(self.server.public_url, self.server.certificate, token),
                                'text/x-shellscript; charset=utf-8', {'Content-Disposition': 'attachment; filename="install-node.sh"'})
        if path == '/agent/enroll' and method == 'POST':
            data = self.body()
            return self.respond(200, store.enroll(str(data.get('token', ''))))
        if path.startswith('/agent/'):
            node_id = self.node_identity()
            if not node_id:
                return self.respond(401, {'error': '节点认证失败'})
            if path == '/agent/config' and method == 'GET':
                return self.respond(200, store.config(node_id))
            if path == '/agent/heartbeat' and method == 'POST':
                store.heartbeat(node_id, self.body())
                return self.respond(200, {'ok': True})
            return self.respond(404, {'error': '接口不存在'})
        session = self.session()
        if not session:
            return self.respond(401, {'error': '请先登录'})
        if method != 'GET' and not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            return self.respond(403, {'error': '请求校验失败，请刷新页面后重试'})
        if path == '/api/session' and method == 'GET':
            return self.respond(200, {'csrf': session['csrf'], 'public_url': self.server.public_url})
        if path == '/api/state' and method == 'GET':
            return self.respond(200, store.snapshot())
        if path == '/api/logout' and method == 'POST':
            with store.lock, store.db:
                store.db.execute('DELETE FROM sessions WHERE token_hash=?', (session['token_hash'],))
            return self.respond(200, {'ok': True}, extra={'Set-Cookie': self.cookie('', 0)})
        if path == '/api/password' and method == 'POST':
            data = self.body()
            new = str(data.get('new_password', ''))
            if len(new) < 12:
                raise ValueError('新密码至少 12 位')
            with store.lock, store.db:
                saved = store.db.execute("SELECT value FROM settings WHERE key='password'").fetchone()[0]
                if not verify_password(str(data.get('old_password', '')), saved):
                    return self.respond(400, {'error': '原密码错误'})
                store.db.execute("UPDATE settings SET value=? WHERE key='password'", (password_hash(new),))
                store.db.execute('DELETE FROM sessions')
                store.audit('修改管理员密码，注销所有会话')
            return self.respond(200, {'ok': True}, extra={'Set-Cookie': self.cookie('', 0)})
        parts = path.strip('/').split('/')
        if len(parts) >= 2 and parts[:2] == ['api', 'nodes']:
            if len(parts) == 2 and method == 'POST':
                return self.respond(201, {'id': store.save_node(self.body())})
            if len(parts) == 3 and method == 'PUT':
                return self.respond(200, {'id': store.save_node(self.body(), parts[2])})
            if len(parts) == 3 and method == 'DELETE':
                store.delete_node(parts[2])
                return self.respond(200, {'ok': True})
            if len(parts) == 4 and parts[3] == 'install' and method == 'POST':
                if not self.server.secure:
                    raise ValueError('生成安装脚本需要面板启用 HTTPS')
                token = store.installation(parts[2])
                return self.respond(200, {'command': install_command(self.server.public_url, self.server.certificate, token),
                                         'script': install_script(self.server.public_url, self.server.certificate, token),
                                         'expires_in': 3600})
        if len(parts) >= 2 and parts[:2] == ['api', 'rules']:
            if len(parts) == 2 and method == 'POST':
                return self.respond(201, {'id': store.save_rule(self.body())})
            if len(parts) == 3 and method == 'PUT':
                return self.respond(200, {'id': store.save_rule(self.body(), parts[2])})
            if len(parts) == 3 and method == 'DELETE':
                store.delete_rule(parts[2])
                return self.respond(200, {'ok': True})
        self.respond(404, {'error': '接口不存在'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.getenv('GOST_BIND', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.getenv('GOST_PORT', '8443')))
    parser.add_argument('--data', default=os.getenv('GOST_DATA', str(ROOT / 'data')))
    args = parser.parse_args()
    public_url = os.getenv('GOST_PUBLIC_URL', '').rstrip('/')
    url = urllib.parse.urlsplit(public_url)
    allow_http = os.getenv('GOST_ALLOW_HTTP') == '1'
    if url.scheme not in (('https', 'http') if allow_http else ('https',)) or not url.hostname or url.path or url.query or url.fragment or url.username is not None or url.password is not None:
        parser.error('GOST_PUBLIC_URL 必须是节点可访问的 HTTPS 根地址，例如 https://1.2.3.4:8443')
    cert_path = Path(os.getenv('GOST_TLS_CERT', str(Path(args.data) / 'panel-cert.pem')))
    key_path = Path(os.getenv('GOST_TLS_KEY', str(Path(args.data) / 'panel-key.pem')))
    secure = url.scheme == 'https'
    store = Store(args.data, os.getenv('GOST_ADMIN_PASSWORD'))
    server = PanelServer((args.host, args.port), store, public_url, cert_path.read_text() if secure else '', secure)
    if secure:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert_path), str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    print('GOST Panel listening at ' + public_url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.db.close()


if __name__ == '__main__':
    main()
