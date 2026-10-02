#!/usr/bin/env python3
"""Root-only systemd updater. Fixed repository/paths; no commands from web input."""
import fcntl
import json
import os
import py_compile
import re
import shlex
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

SOURCE = Path('/opt/gost-panel')
PREVIOUS = Path('/opt/gost-panel.previous')
DATA = Path('/var/lib/gost-panel')
STATE = Path('/var/lib/gost-panel-upgrade')
API = 'https://api.github.com/repos/panhui/gost'
MAX_DOWNLOAD = 16 * 1024 * 1024


def status(state, message, **details):
    temporary = STATE / 'status.tmp'
    temporary.write_text(json.dumps({'state': state, 'message': message, 'time': time.time(), **details}, ensure_ascii=False))
    os.chmod(temporary, 0o644)
    temporary.replace(STATE / 'status.json')


def download(url, destination, limit=MAX_DOWNLOAD):
    request = urllib.request.Request(url, headers={'User-Agent': 'GOST-Panel-Updater', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open('wb') as output:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise ValueError('升级下载必须使用 HTTPS')
        total = 0
        started = time.monotonic()
        while True:
            chunk = response.read(128 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if time.monotonic() - started > 180:
                raise TimeoutError('升级下载超过 3 分钟，请稍后重试')
            if total > limit:
                raise ValueError('升级包超过大小限制')
            output.write(chunk)


def extract(archive, destination):
    # GitHub archives contain a single top-level directory. Extract regular files
    # manually: no symlinks, devices, hard links, traversal, or tar metadata modes.
    with tarfile.open(archive, 'r:gz') as bundle:
        total = 0
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                raise ValueError('升级包包含不安全的文件路径或类型')
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError('升级包根目录无效')
                continue
            target = destination.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                total += member.size
                if total > 64 * 1024 * 1024:
                    raise ValueError('升级包解压后超过大小限制')
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with bundle.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, 0o644)
    for required in ('app.py', 'core.py', 'agent.py', 'installers.py', 'cache.py', 'upgrade.py', 'upgrade_control.py', 'diagnostics.py', 'static/index.html'):
        if not (destination / required).is_file():
            raise ValueError('升级包缺少文件：' + required)
    for file in destination.glob('*.py'):
        py_compile.compile(str(file), doraise=True)


def service(action):
    subprocess.run(['systemctl', action, 'gost-panel.service'], check=True, timeout=30)


def health():
    settings = {}
    for line in Path('/etc/gost-panel.env').read_text().splitlines():
        key, _, value = line.partition('=')
        if key in ('GOST_PUBLIC_URL', 'GOST_PORT', 'GOST_TLS_CERT', 'GOST_BIND'):
            values = shlex.split(value)
            if len(values) == 1:
                settings[key] = values[0]
    url = urllib.parse.urlsplit(settings['GOST_PUBLIC_URL'])
    context = ssl.create_default_context(cafile=settings.get('GOST_TLS_CERT', str(DATA / 'panel-cert.pem')))
    endpoint = settings.get('GOST_BIND', '127.0.0.1')
    if endpoint in ('0.0.0.0', '::'):
        endpoint = '127.0.0.1'
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((endpoint, int(settings.get('GOST_PORT', url.port or 443))), timeout=3) as raw:
                with context.wrap_socket(raw, server_hostname=url.hostname) as connection:
                    connection.sendall(b'GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n')
                    if b' 200 ' in connection.recv(4096).split(b'\r\n')[0]:
                        return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('升级后的面板未通过 HTTPS 健康检查')


def backup_database(destination):
    with sqlite3.connect('file:' + str(DATA / 'panel.db') + '?mode=ro', uri=True) as source:
        with sqlite3.connect(str(destination)) as output:
            source.backup(output)
    os.chmod(destination, 0o600)


def upgrade():
    with tempfile.TemporaryDirectory(prefix='gost-upgrade-', dir=str(SOURCE.parent)) as folder:
        folder = Path(folder)
        status('running', '查询 GitHub 最新版本')
        download(API + '/commits/main', folder / 'commit.json', 1024 * 1024)
        sha = json.loads((folder / 'commit.json').read_text())['sha']
        if not re.fullmatch('[a-f0-9]{40}', sha):
            raise ValueError('GitHub 返回的版本号无效')
        status('running', '下载并校验升级包', commit=sha)
        download(API + '/tarball/' + sha, folder / 'source.tar.gz')
        new = folder / 'source'
        new.mkdir(mode=0o755)
        extract(folder / 'source.tar.gz', new)
        (new / 'BUILD_COMMIT').write_text(sha + '\n')
        os.chmod(new / 'BUILD_COMMIT', 0o644)
        status('running', '备份数据库并重启面板', commit=sha)
        backup = STATE / 'panel-before-upgrade.db'
        stopped, swapped, backed_up = False, False, False
        try:
            service('stop')
            stopped = True
            backup_database(backup)
            backed_up = True
            if PREVIOUS.exists():
                shutil.rmtree(PREVIOUS)
            SOURCE.rename(PREVIOUS)
            swapped = True
            new.rename(SOURCE)
            service('start')
            health()
        except Exception:
            if stopped:
                service('stop')
                if swapped:
                    if SOURCE.exists():
                        shutil.rmtree(SOURCE)
                    PREVIOUS.rename(SOURCE)
                if backed_up:
                    # Restore the stopped database, including pre-migration schema.
                    owner = (DATA / 'panel.db').stat()
                    for suffix in ('-wal', '-shm'):
                        Path(str(DATA / 'panel.db') + suffix).unlink(missing_ok=True)
                    shutil.copyfile(backup, DATA / 'panel.db')
                    os.chmod(DATA / 'panel.db', 0o600)
                    os.chown(DATA / 'panel.db', owner.st_uid, owner.st_gid)
                service('start')
            raise
        status('success', '面板升级成功，节点、规则、密码和证书已保留', commit=sha)


def main():
    if os.geteuid() != 0:
        raise SystemExit('升级服务需要 root 身份')
    STATE.mkdir(mode=0o755, exist_ok=True)
    if sys.argv[1:] == ['--finalize']:
        try:
            result = json.loads((STATE / 'status.json').read_text())
        except (OSError, ValueError):
            result = {}
        if result.get('state') == 'running':
            status('failed', '升级服务意外中断，请检查面板服务和升级日志')
        (DATA / 'upgrade-request.json').unlink(missing_ok=True)
        return
    with (STATE / 'upgrade.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = DATA / 'upgrade-request.json'
        try:
            if request.stat().st_size > 1024:
                raise ValueError('升级请求过大')
            job = json.loads(request.read_text())
            if not re.fullmatch('[a-f0-9]{32}', str(job.get('id', ''))):
                raise ValueError('升级请求无效')
            upgrade()
        except Exception as exc:
            status('failed', '升级失败，已尝试恢复原版本：' + str(exc)[:300])
        finally:
            request.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
