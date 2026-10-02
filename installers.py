"""Generate a self-contained Linux enrollment script with a revocable permanent credential."""
import base64
import gzip
import shlex
from pathlib import Path
from core import CHECKSUMS, GOST_VERSION


def install_script(panel_url, certificate, token):
    source = base64.b64encode(gzip.compress(Path(__file__).with_name('agent.py').read_bytes())).decode()
    ca = base64.b64encode(certificate.encode()).decode()
    return f'''#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || {{ echo "请使用 sudo bash 运行安装脚本" >&2; exit 1; }}
command -v systemctl >/dev/null || {{ echo "需要 systemd Linux 系统" >&2; exit 1; }}
if command -v apt-get >/dev/null; then
  apt-get update
  echo '等待其他 apt/dpkg 任务完成（最多 10 分钟）…'
  apt-get -o DPkg::Lock::Timeout=600 install -y python3 curl ca-certificates tar
elif command -v dnf >/dev/null; then
  dnf install -y python3 curl ca-certificates tar
else
  for cmd in python3 curl tar sha256sum; do command -v "$cmd" >/dev/null || {{ echo "缺少依赖: $cmd"; exit 1; }}; done
fi
case "$(uname -m)" in
  x86_64) arch=amd64; checksum={CHECKSUMS['amd64']} ;;
  aarch64|arm64) arch=arm64; checksum={CHECKSUMS['arm64']} ;;
  *) echo "仅支持 Linux amd64 / arm64" >&2; exit 1 ;;
esac
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
asset="gost_{GOST_VERSION}_linux_${{arch}}.tar.gz"
printf '%s' {shlex.quote(ca)} | base64 -d > "$scratch/panel-ca.pem"
echo "从面板下载 GOST {GOST_VERSION} ($arch)…"
curl --fail --show-error --location --cacert "$scratch/panel-ca.pem" \
  --header {shlex.quote('Authorization: Bearer ' + token)} \
  --connect-timeout 15 --max-time 900 --retry 2 --retry-max-time 1800 --proto '=https' --tlsv1.2 \
  {shlex.quote(panel_url + '/downloads/gost/')}"$arch" -o "$scratch/$asset"
printf '%s  %s\\n' "$checksum" "$scratch/$asset" | sha256sum --check -
tar -xzf "$scratch/$asset" -C "$scratch" gost
id gost-agent >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gost-agent --shell /usr/sbin/nologin gost-agent
install -d -m 700 -o gost-agent -g gost-agent /var/lib/gost-agent
install -d -m 755 /opt/gost-agent
python3 - <<'INSTALL_PAYLOAD'
import base64, gzip, os
from pathlib import Path
Path('/opt/gost-agent/agent.py').write_bytes(gzip.decompress(base64.b64decode('{source}')))
os.chmod('/opt/gost-agent/agent.py', 0o644)
Path('/var/lib/gost-agent/panel-ca.pem').write_bytes(base64.b64decode('{ca}'))
os.chmod('/var/lib/gost-agent/panel-ca.pem', 0o600)
INSTALL_PAYLOAD
export PANEL_URL={shlex.quote(panel_url)}
export INSTALL_TOKEN={shlex.quote(token)}
python3 /opt/gost-agent/agent.py --enroll
unset INSTALL_TOKEN
systemctl stop gost-agent.service 2>/dev/null || true
install -m 755 "$scratch/gost" /usr/local/bin/gost
# A reinstall must apply the newly enrolled identity before serving old rules.
rm -rf /var/lib/gost-agent/configs /var/lib/gost-agent/active.json
chown -R gost-agent:gost-agent /var/lib/gost-agent
cat > /etc/systemd/system/gost-agent.service <<'SERVICE'
[Unit]
Description=GOST tunnel panel agent
After=network-online.target
Wants=network-online.target
[Service]
User=gost-agent
Group=gost-agent
ExecStart=/usr/bin/python3 /opt/gost-agent/agent.py
Restart=on-failure
RestartPreventExitStatus=77
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/gost-agent
PrivateTmp=true
UMask=0077
[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable --now gost-agent.service
sleep 2
systemctl is-active --quiet gost-agent.service
echo "节点代理已启动。请回面板确认节点在线和规则同步；放行入口监听端口、出口 TCP 隧道端口。"
'''


def install_command(panel_url, certificate, token):
    ca = base64.b64encode(certificate.encode()).decode()
    url = panel_url + '/install/' + token
    return f'''(umask 077; t=$(mktemp -d); trap 'rm -rf "$t"' EXIT; printf '%s' {shlex.quote(ca)} | base64 -d > "$t/ca.pem"; curl --fail --show-error --cacert "$t/ca.pem" {shlex.quote(url)} -o "$t/install.sh" && sudo env GOST_NODE_HOST="${{GOST_NODE_HOST:-}}" bash "$t/install.sh")'''
