#!/usr/bin/env bash
# Install/update the panel on systemd Linux. No Docker or Python packages needed.
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo '请使用 sudo bash 运行安装脚本' >&2; exit 1; }
command -v systemctl >/dev/null || { echo '需要 systemd Linux 系统' >&2; exit 1; }
if command -v apt-get >/dev/null; then
  apt-get update
  echo '等待其他 apt/dpkg 任务完成（最多 10 分钟）…'
  apt-get -o DPkg::Lock::Timeout=600 install -y python3 openssl curl ca-certificates tar
elif command -v dnf >/dev/null; then
  dnf install -y python3 openssl curl ca-certificates tar
else
  for cmd in python3 openssl curl tar useradd; do command -v "$cmd" >/dev/null || { echo "缺少依赖: $cmd"; exit 1; }; done
fi
if [[ -z ${GOST_PUBLIC_URL:-} ]]; then
  read -r -p '面板公网 HTTPS 地址（例如 https://1.2.3.4:8443）: ' GOST_PUBLIC_URL < /dev/tty
fi
export GOST_PUBLIC_URL
python3 - <<'VALIDATE_URL'
import ipaddress, os, urllib.parse
url = urllib.parse.urlsplit(os.environ['GOST_PUBLIC_URL'].rstrip('/'))
if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
    raise SystemExit('请输入 HTTPS 根地址，不能包含路径、凭证或查询参数')
if '\n' in os.environ['GOST_PUBLIC_URL'] or '\r' in os.environ['GOST_PUBLIC_URL']:
    raise SystemExit('地址格式无效')
if not 1 <= (url.port or 443) <= 65535:
    raise SystemExit('面板端口无效')
# The panel runs without root; privileged ports require a reverse proxy.
if (url.port or 443) < 1024:
    raise SystemExit('直连安装请使用 >=1024 的端口，例如 :8443；443 请参考反向代理部署说明')
try:
    ipaddress.ip_address(url.hostname)
except ValueError:
    import re
    if not re.fullmatch(r'[A-Za-z0-9.-]+', url.hostname):
        raise SystemExit('域名格式无效')
VALIDATE_URL
id gost-panel >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gost-panel --shell /usr/sbin/nologin gost-panel
install -d -m 700 -o gost-panel -g gost-panel /var/lib/gost-panel
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
ref=${GOST_PANEL_REF:-main}
[[ $ref =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'GOST_PANEL_REF 格式无效' >&2; exit 1; }
echo "下载 GOST Panel ($ref)…"
curl --fail --show-error --location --proto '=https' --tlsv1.2 --retry 3 \
  "https://api.github.com/repos/panhui/gost/tarball/$ref" -o "$scratch/panel.tar.gz"
mkdir "$scratch/source"
tar -xzf "$scratch/panel.tar.gz" -C "$scratch/source" --strip-components=1
python3 -m py_compile "$scratch/source/app.py" "$scratch/source/core.py" "$scratch/source/agent.py" "$scratch/source/installers.py" "$scratch/source/cache.py"
if ! python3 - <<'CHECK_INITIALIZED'
import sqlite3
try:
    db = sqlite3.connect('file:/var/lib/gost-panel/panel.db?mode=ro', uri=True)
    initialized = bool(db.execute("SELECT value FROM settings WHERE key='password'").fetchone())
    db.close()
except sqlite3.Error:
    initialized = False
raise SystemExit(0 if initialized else 1)
CHECK_INITIALIZED
then
  if [[ -z ${GOST_ADMIN_PASSWORD:-} ]]; then
    read -r -s -p '管理员密码（至少 12 位）: ' GOST_ADMIN_PASSWORD < /dev/tty
    echo
    read -r -s -p '再次输入密码: ' repeat_password < /dev/tty
    echo
    [[ $GOST_ADMIN_PASSWORD == "$repeat_password" ]] || { echo '两次输入的密码不一致' >&2; exit 1; }
    unset repeat_password
  fi
  export GOST_ADMIN_PASSWORD
  PYTHONPATH="$scratch/source" python3 - <<'INITIALIZE'
import os
from core import Store
store = Store('/var/lib/gost-panel', os.environ['GOST_ADMIN_PASSWORD'])
store.db.close()
INITIALIZE
  unset GOST_ADMIN_PASSWORD
fi
python3 - <<'CONFIGURE'
import os, sys, subprocess, ipaddress, urllib.parse
from pathlib import Path
url = os.environ['GOST_PUBLIC_URL'].rstrip('/')
parsed = urllib.parse.urlsplit(url)
root = Path('/var/lib/gost-panel')
cert, key = root/'panel-cert.pem', root/'panel-key.pem'
try:
    ipaddress.ip_address(parsed.hostname)
    san = 'IP:' + parsed.hostname
    check = '-checkip'
except ValueError:
    san = 'DNS:' + parsed.hostname
    check = '-checkhost'
valid = cert.exists() and key.exists()
if valid:
    valid = subprocess.run(['openssl','x509','-in',str(cert),check,parsed.hostname,'-noout'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0
if not valid:
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','3650','-subj','/CN='+parsed.hostname,'-addext','subjectAltName='+san,'-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print('已生成面板 HTTPS 证书；如之前安装过节点，请重新安装以更新证书信任。')
def quote(value):
    return '"' + value.replace('\\','\\\\').replace('"','\\"') + '"'
Path('/etc/gost-panel.env').write_text('GOST_PUBLIC_URL='+quote(url)+'\nGOST_PORT='+str(parsed.port or 443)+'\nGOST_DATA=/var/lib/gost-panel\n')
os.chmod('/etc/gost-panel.env',0o600)
os.chmod(key,0o600)
CONFIGURE
# Cache releases here once, so domestic nodes only download from the panel.
if ! python3 "$scratch/source/cache.py" --directory /var/lib/gost-panel/downloads; then
  echo '安装包预缓存失败。面板仍将安装，节点首次下载时会重试；可查看 README 手动预缓存。' >&2
fi
systemctl stop gost-panel.service 2>/dev/null || true
# Preserve the previous source for manual rollback.
if [[ -d /opt/gost-panel ]]; then
  rm -rf /opt/gost-panel.previous
  mv /opt/gost-panel /opt/gost-panel.previous
fi
mkdir -p /opt/gost-panel
cp -R "$scratch/source/." /opt/gost-panel/
chmod -R a+rX /opt/gost-panel
chown -R gost-panel:gost-panel /var/lib/gost-panel
cat > /etc/systemd/system/gost-panel.service <<'SERVICE'
[Unit]
Description=GOST tunnel forwarding panel
After=network-online.target
Wants=network-online.target
[Service]
User=gost-panel
Group=gost-panel
EnvironmentFile=/etc/gost-panel.env
ExecStart=/usr/bin/python3 /opt/gost-panel/app.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/gost-panel
PrivateTmp=true
UMask=0077
[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable --now gost-panel.service
sleep 2
systemctl is-active --quiet gost-panel.service
echo "面板已启动：$GOST_PUBLIC_URL"
echo '请放行面板 TCP 端口。首次浏览器访问需确认自签名证书；可替换为可信证书。'
echo '查看日志：journalctl -u gost-panel -f'
