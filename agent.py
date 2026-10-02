#!/usr/bin/env python3
"""Outbound-only agent. Keeps the last applied GOST configuration during outages."""
import argparse
import json
import os
import platform
import re
import secrets
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def request(settings, path, data=None):
    context = ssl.create_default_context(cafile=settings['ca_file'])
    headers = {'Authorization': 'Bearer ' + settings.get('token', ''), 'Content-Type': 'application/json'}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(settings['panel_url'] + path, data=body, headers=headers)
    with urllib.request.urlopen(req, context=context, timeout=15) as response:
        return json.loads(response.read(8 * 1024 * 1024))


class Agent:
    def __init__(self, directory, binary):
        self.directory, self.binary = Path(directory).resolve(), str(Path(binary).resolve())
        if (self.directory / 'revoked').exists():
            raise SystemExit(77)
        self.settings = json.loads((self.directory / 'agent.json').read_text())
        self.process, self.applied, self.active = None, '', None
        self.error, self.stopping = '', False
        marker = self.directory / 'active.json'
        if marker.exists():
            self.active = json.loads(marker.read_text())
            self.applied = self.active['revision']

    def stop_process(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None

    def start(self, payload):
        if not payload['config']['services']:
            return
        folder = self.directory / 'configs' / payload['revision']
        self.process = subprocess.Popen([self.binary, '-C', 'gost.json'], cwd=str(folder))
        time.sleep(1)
        if self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            raise RuntimeError('GOST 启动失败（退出码 %s），请检查端口占用和节点日志' % code)

    def apply(self, payload):
        revision = payload['revision']
        if not re.fullmatch('[a-f0-9]{64}', revision):
            raise ValueError('Invalid configuration revision')
        if revision == self.applied and self.active:
            if self.active['config']['services'] and (not self.process or self.process.poll() is not None):
                self.start(self.active)
            self.error = ''
            return
        folder = self.directory / 'configs' / revision
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for filename, content in payload['files'].items():
            if not re.fullmatch(r'[a-zA-Z0-9-]+\.(pem|key)', filename):
                raise ValueError('Invalid certificate filename')
            file = folder / filename
            file.write_text(content)
            os.chmod(file, 0o600)
        atomic_json(folder / 'gost.json', payload['config'])
        old = self.active
        self.stop_process()
        try:
            self.start(payload)
        except Exception:
            if old:
                self.start(old)
            raise
        self.active, self.applied = payload, revision
        self.error = ''
        atomic_json(self.directory / 'active.json', payload)
        # Keep the applied and previous generation for rollback only.
        keep = {revision, old['revision'] if old else ''}
        for previous in (self.directory / 'configs').iterdir():
            if previous.is_dir() and previous.name not in keep:
                for item in previous.iterdir():
                    item.unlink()
                previous.rmdir()

    def stop(self, *_):
        self.stopping = True

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if self.active:
            try:
                self.start(self.active)
            except Exception as exc:
                self.error = str(exc)
        try:
            while not self.stopping:
                try:
                    payload = request(self.settings, '/agent/config')
                    self.apply(payload)
                except urllib.error.HTTPError as exc:
                    if exc.code == 401:
                        self.stop_process()
                        (self.directory / 'active.json').unlink(missing_ok=True)
                        (self.directory / 'revoked').write_text('Credential revoked by panel')
                        print('Agent credential revoked; stopping forwarding.', flush=True)
                        raise SystemExit(77)
                    self.error = '面板请求失败（HTTP %s）' % exc.code
                except Exception as exc:
                    self.error = str(exc)[:500]
                    print('Agent synchronization failed: ' + self.error, file=sys.stderr, flush=True)
                # Recover the last applied generation even while the panel is unreachable.
                if self.active and self.active['config']['services'] and (not self.process or self.process.poll() is not None):
                    try:
                        self.start(self.active)
                    except Exception as exc:
                        self.error = str(exc)
                try:
                    request(self.settings, '/agent/heartbeat', {
                        'applied': self.applied, 'running': bool(self.process and self.process.poll() is None),
                        'error': self.error, 'version': '3.3.0'})
                except Exception:
                    pass
                for _ in range(10):
                    if self.stopping:
                        break
                    time.sleep(1)
        finally:
            self.stop_process()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='/var/lib/gost-agent')
    parser.add_argument('--binary', default='/usr/local/bin/gost')
    parser.add_argument('--enroll', action='store_true')
    args = parser.parse_args()
    directory = Path(args.directory)
    if args.enroll:
        settings = {'panel_url': os.environ['PANEL_URL'], 'ca_file': str(directory / 'panel-ca.pem')}
        identity = directory / 'installation-id'
        if not identity.exists():
            identity.write_text(secrets.token_hex(32))
            os.chmod(identity, 0o600)
        previous = json.loads((directory / 'agent.json').read_text()) if (directory / 'agent.json').exists() else {}
        settings.update(request(settings, '/agent/enroll', {'token': os.environ['INSTALL_TOKEN'],
            'machine_id': identity.read_text().strip(), 'name': (platform.node() or 'Linux 设备')[:64],
            'host': os.getenv('GOST_NODE_HOST', ''), 'previous_token': previous.get('token', '')}))
        atomic_json(directory / 'agent.json', settings)
        (directory / 'revoked').unlink(missing_ok=True)
    else:
        Agent(directory, args.binary).run()


if __name__ == '__main__':
    main()
