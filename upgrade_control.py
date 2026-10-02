"""Unprivileged upgrade request API; the systemd helper runs the fixed updater."""
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

STATUS_DIRECTORY = Path('/var/lib/gost-panel-upgrade')


class UpgradeControl:
    def __init__(self, directory, enabled=False, status_directory=STATUS_DIRECTORY):
        self.request = Path(directory) / 'upgrade-request.json'
        self.status_directory = Path(status_directory)
        self.enabled = enabled

    def status(self):
        try:
            result = json.loads((self.status_directory / 'status.json').read_text())
        except (OSError, ValueError):
            result = {'state': 'idle', 'message': '尚未执行升级'}
        if self.request.exists() and result.get('state') != 'running':
            result = {'state': 'queued', 'message': '等待升级服务启动'}
        return {**result, 'enabled': self.enabled}

    def start(self):
        if not self.enabled:
            raise ValueError('当前部署尚未启用升级服务，请先执行一次新版面板安装脚本')
        if self.status()['state'] in ('queued', 'running'):
            raise ValueError('已有升级任务正在执行')
        request = {'id': secrets.token_hex(16), 'time': time.time()}
        # Exclusive creation prevents concurrent requests from replacing a job.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.request.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump(request, output)
            os.link(temporary, self.request)
        except FileExistsError:
            raise ValueError('已有升级任务正在执行')
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return {'state': 'queued', 'message': '已提交升级，面板将在重启后自动恢复'}
