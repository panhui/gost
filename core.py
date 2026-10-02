"""Persistent control plane and deterministic GOST 3 configuration generation."""
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

PANEL_VERSION = '0.2.0'
GOST_VERSION = '3.3.0'
CHECKSUMS = {
    'amd64': '676fb7f78d267b6ae73df719c0c7f2b565dde7147da935cfafbc1e1da558b6d5',
    'arm64': 'd03699e3f385d4ff5dad68046712adfcc7515325a064d2ab046e0bece30f8f8f',
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()


def verify_password(password, stored):
    return secrets.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def port(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('端口必须为 1–65535 的整数')
    return int(value)


def host(value):
    value = str(value).strip().strip('[]')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if len(value) > 253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
            raise ValueError('请输入有效的 IP 地址或域名，不含协议和端口')
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
            raise ValueError('域名格式错误')
        return value.lower()


def address(h, p):
    return ('[' + h + ']' if ':' in h else h) + ':' + str(p)


def targets(value):
    if isinstance(value, str):
        value = [line.strip() for line in value.splitlines() if line.strip()]
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError('请填写 1–32 个目标地址，每行一个 IP:端口 或 域名:端口')
    result = []
    for target in value:
        target = str(target).strip()
        if target.startswith('['):
            match = re.fullmatch(r'\[([^\]]+)\]:(\d+)', target)
        else:
            match = re.fullmatch(r'([^:]+):(\d+)', target)
        if not match:
            raise ValueError('目标地址格式错误：' + target + '（IPv6 请使用 [地址]:端口）')
        result.append({'host': host(match[1]), 'port': port(match[2])})
    return result


def name(value):
    value = str(value).strip()
    if not 1 <= len(value) <= 64:
        raise ValueError('名称长度必须为 1–64 个字符')
    return value


def certificate(server_name):
    # Separate exit certificate, pinned by entries; private keys never go to entries.
    with tempfile.TemporaryDirectory() as temp:
        cert, key = Path(temp) / 'cert.pem', Path(temp) / 'key.pem'
        try:
            ipaddress.ip_address(server_name)
            san = 'IP:' + server_name
        except ValueError:
            san = 'DNS:' + server_name
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '3650', '-subj', '/CN=' + server_name,
                        '-addext', 'subjectAltName=' + san,
                        '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        return cert.read_text(), key.read_text()


class Store:
    def __init__(self, directory, initial_password=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.directory / 'panel.db'), check_same_thread=False)
        os.chmod(self.directory / 'panel.db', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, host TEXT NOT NULL,
            cert TEXT NOT NULL DEFAULT '', private_key TEXT NOT NULL DEFAULT '',
            token_hash TEXT, last_seen REAL NOT NULL DEFAULT 0,
            applied TEXT NOT NULL DEFAULT '', running INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS rules (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
            entry_id TEXT NOT NULL REFERENCES nodes(id), exit_id TEXT NOT NULL REFERENCES nodes(id),
            listen_port INTEGER NOT NULL, tunnel_port INTEGER NOT NULL,
            target_host TEXT NOT NULL, target_port INTEGER NOT NULL,
            enabled INTEGER NOT NULL, secret TEXT NOT NULL,
            UNIQUE(entry_id, protocol, listen_port), UNIQUE(exit_id, tunnel_port));
        CREATE TABLE IF NOT EXISTS installs (
            token_hash TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, message TEXT NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(rules)')}
        if 'targets_json' not in columns:
            self.db.execute("ALTER TABLE rules ADD COLUMN targets_json TEXT NOT NULL DEFAULT '[]'")
        if not self.db.execute("SELECT value FROM settings WHERE key='password'").fetchone():
            if not initial_password or len(initial_password) < 12:
                raise ValueError('首次启动需要设置至少 12 位的 GOST_ADMIN_PASSWORD')
            self.db.execute("INSERT INTO settings VALUES ('password', ?)", (password_hash(initial_password),))
        # Existing installation commands become permanent too.
        self.db.execute('UPDATE installs SET expires=0')
        self.db.commit()

    def audit(self, message):
        self.db.execute('INSERT INTO audit(time,message) VALUES (?,?)', (time.time(), message))
        self.db.execute('DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 200)')

    def node(self, node_id):
        row = self.db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
        if not row:
            raise ValueError('节点不存在')
        return dict(row)

    def save_node(self, data, node_id=None):
        with self.lock, self.db:
            if node_id:
                old = self.node(node_id)
                # Address edits also affect configuration; role edits cannot silently move rules.
                if data.get('role') != old['role']:
                    raise ValueError('节点类型不能修改，请重新添加节点')
            role, hostname = data.get('role'), host(data.get('host', ''))
            if role not in ('entry', 'exit'):
                raise ValueError('节点类型必须为入口或出口')
            title = name(data.get('name', ''))
            if node_id:
                self.db.execute('UPDATE nodes SET name=?,host=? WHERE id=?', (title, hostname, node_id))
            else:
                node_id = secrets.token_hex(8)
                cert, key = certificate('exit-' + node_id + '.gost.internal') if role == 'exit' else ('', '')
                self.db.execute('INSERT INTO nodes(id,name,role,host,cert,private_key) VALUES (?,?,?,?,?,?)',
                                (node_id, title, role, hostname, cert, key))
            self.audit('保存节点：' + title)
            return node_id

    def delete_node(self, node_id):
        with self.lock, self.db:
            node = self.node(node_id)
            if self.db.execute('SELECT id FROM rules WHERE entry_id=? OR exit_id=?', (node_id, node_id)).fetchone():
                raise ValueError('请先删除使用该节点的转发规则')
            self.db.execute('DELETE FROM nodes WHERE id=?', (node_id,))
            self.audit('删除节点：' + node['name'])

    def save_rule(self, data, rule_id=None):
        with self.lock, self.db:
            old = None
            if rule_id:
                row = self.db.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
                if not row:
                    raise ValueError('规则不存在')
                old = dict(row)
            entry, exit_node = self.node(data.get('entry_id')), self.node(data.get('exit_id'))
            if entry['role'] != 'entry' or exit_node['role'] != 'exit':
                raise ValueError('必须选择一个入口节点和一个出口节点')
            protocol = data.get('protocol', 'tcp')
            if protocol not in ('tcp', 'udp'):
                raise ValueError('仅支持 TCP 或 UDP')
            listen = data.get('listen_port')
            if not listen:
                used_entry = {r[0] for r in self.db.execute('SELECT listen_port FROM rules WHERE entry_id=? AND protocol=? AND id<>?', (entry['id'], protocol, rule_id or ''))}
                available = [p for p in range(2000, 60001) if p not in used_entry]
                if not available:
                    raise ValueError('入口没有可用的自动分配端口')
                listen = secrets.choice(available)
            listen = port(listen)
            tunnel = data.get('tunnel_port')
            if not tunnel:
                used = {r[0] for r in self.db.execute('SELECT tunnel_port FROM rules WHERE exit_id=? AND id<>?',
                                                     (exit_node['id'], rule_id or ''))}
                tunnel = next((p for p in range(20000, 60000) if p not in used), None)
                if tunnel is None:
                    raise ValueError('出口没有可用的自动分配端口')
            tunnel = port(tunnel)
            target_list = targets(data['targets']) if 'targets' in data else [{'host': host(data.get('target_host', '')), 'port': port(data.get('target_port'))}]
            title, target, target_port = name(data.get('name', '')), target_list[0]['host'], target_list[0]['port']
            if type(data.get('enabled', True)) is not bool:
                raise ValueError('enabled 必须为布尔值')
            values = (title, protocol, entry['id'], exit_node['id'], listen, tunnel, target,
                      target_port, int(data.get('enabled', True)), old['secret'] if old else secrets.token_urlsafe(32), json.dumps(target_list))
            try:
                if old:
                    self.db.execute('UPDATE rules SET name=?,protocol=?,entry_id=?,exit_id=?,listen_port=?,tunnel_port=?,target_host=?,target_port=?,enabled=?,secret=?,targets_json=? WHERE id=?', values + (rule_id,))
                else:
                    rule_id = secrets.token_hex(8)
                    self.db.execute('INSERT INTO rules(id,name,protocol,entry_id,exit_id,listen_port,tunnel_port,target_host,target_port,enabled,secret,targets_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (rule_id,) + values)
            except sqlite3.IntegrityError:
                raise ValueError('入口监听端口或出口隧道端口已被其他规则使用')
            self.audit('保存规则：' + title)
            return rule_id

    def delete_rule(self, rule_id):
        with self.lock, self.db:
            row = self.db.execute('SELECT name FROM rules WHERE id=?', (rule_id,)).fetchone()
            if not row:
                raise ValueError('规则不存在')
            self.db.execute('DELETE FROM rules WHERE id=?', (rule_id,))
            self.audit('删除规则：' + row[0])

    def config(self, node_id):
        with self.lock:
            node = self.node(node_id)
            config = {'services': [], 'chains': [], 'log': {'level': 'warn'}}
            # GOST metadata integer readers accept strings; JSON numbers decode as
            # float64 upstream and would silently fall back to small UDP buffers.
            files = {}
            rules = self.db.execute('SELECT * FROM rules WHERE enabled=1 AND (entry_id=? OR exit_id=?) ORDER BY id',
                                    (node_id, node_id)).fetchall()
            for row in rules:
                rule = dict(row)
                rule_name = 'rule-' + rule['id']
                target_list = json.loads(rule['targets_json']) or [{'host': rule['target_host'], 'port': rule['target_port']}]
                target = address(target_list[0]['host'], target_list[0]['port'])
                forward_nodes = [{'name': 'target-' + str(i), 'addr': address(t['host'], t['port'])} for i, t in enumerate(target_list)]
                if node['role'] == 'exit':
                    files['server.pem'], files['server.key'] = node['cert'], node['private_key']
                    config['services'].append({
                        'name': rule_name, 'addr': ':' + str(rule['tunnel_port']),
                        'handler': {'type': 'relay', 'auth': {'username': rule['id'], 'password': rule['secret']},
                                    'metadata': {'readTimeout': '10s', 'udpBufferSize': '65535', 'nodelay': True}},
                        'listener': {'type': 'tls', 'tls': {'certFile': 'server.pem', 'keyFile': 'server.key',
                                     'options': {'minVersion': 'VersionTLS12'}}},
                        'forwarder': {'nodes': forward_nodes, 'selector': {'strategy': 'round', 'maxFails': 1, 'failTimeout': '10s'}}})
                else:
                    exit_node = self.node(rule['exit_id'])
                    cert_name = 'exit-' + exit_node['id'] + '.pem'
                    files[cert_name] = exit_node['cert']
                    config['services'].append({
                        'name': rule_name, 'addr': ':' + str(rule['listen_port']),
                        'handler': {'type': rule['protocol'], 'chain': rule_name, **({'metadata': {'readBufferSize': '65535'}} if rule['protocol'] == 'udp' else {})},
                        'listener': {'type': rule['protocol'], **({'metadata': {'keepAlive': True, 'ttl': '60s', 'readBufferSize': '65535'}} if rule['protocol'] == 'udp' else {})},
                        'forwarder': {'nodes': [{'name': 'target', 'addr': target}]}})
                    config['chains'].append({'name': rule_name, 'hops': [{'name': rule_name + '-exit', 'nodes': [{
                        'name': 'exit', 'addr': address(exit_node['host'], rule['tunnel_port']),
                        'connector': {'type': 'relay', 'auth': {'username': rule['id'], 'password': rule['secret']},
                                      'metadata': {'nodelay': True}},
                        'dialer': {'type': 'tls', 'tls': {'caFile': cert_name, 'secure': True,
                                   'serverName': 'exit-' + exit_node['id'] + '.gost.internal',
                                   'options': {'minVersion': 'VersionTLS12'}}}}]}]})
            payload = {'config': config, 'files': files}
            payload['revision'] = digest(json.dumps(payload, sort_keys=True))
            return payload

    def snapshot(self):
        with self.lock:
            nodes = []
            for row in self.db.execute('SELECT id,name,role,host,last_seen,applied,running,error,version FROM nodes ORDER BY rowid'):
                node = dict(row)
                desired = self.config(node['id'])
                node['online'] = time.time() - node['last_seen'] < 40
                node['synced'] = node['applied'] == desired['revision']
                node['service_count'] = len(desired['config']['services'])
                node['desired'] = desired['revision']
                nodes.append(node)
            rules = [dict(r) for r in self.db.execute('SELECT id,name,protocol,entry_id,exit_id,listen_port,tunnel_port,target_host,target_port,enabled,targets_json FROM rules ORDER BY rowid DESC')]
            for rule in rules:
                rule['targets'] = [address(t['host'], t['port']) for t in (json.loads(rule.pop('targets_json')) or [{'host': rule['target_host'], 'port': rule['target_port']}])]
            audit = [dict(r) for r in self.db.execute('SELECT time,message FROM audit ORDER BY id DESC LIMIT 30')]
            return {'nodes': nodes, 'rules': rules, 'audit': audit, 'gost_version': GOST_VERSION, 'panel_version': PANEL_VERSION}

    def installation(self, node_id):
        with self.lock, self.db:
            self.node(node_id)
            token = secrets.token_urlsafe(32)
            self.db.execute('DELETE FROM installs WHERE node_id=?', (node_id,))
            self.db.execute('INSERT INTO installs VALUES (?,?,?)', (digest(token), node_id, 0))
            self.audit('生成节点安装脚本：' + self.node(node_id)['name'])
            return token

    def bootstrap_node(self, token):
        row = self.db.execute('SELECT node_id FROM installs WHERE token_hash=?', (digest(token),)).fetchone()
        if not row:
            raise ValueError('安装凭证已被撤销或节点已删除，请在面板重新生成')
        return row[0]

    def enroll(self, token):
        with self.lock, self.db:
            node_id = self.bootstrap_node(token)
            credential = secrets.token_urlsafe(48)
            self.db.execute('UPDATE nodes SET token_hash=?,last_seen=0,applied=?,running=0,error=? WHERE id=?',
                            (digest(credential), '', '', node_id))
            self.audit('节点完成注册：' + self.node(node_id)['name'])
            return {'node_id': node_id, 'token': credential}

    def authenticate_node(self, token):
        with self.lock:
            row = self.db.execute('SELECT id FROM nodes WHERE token_hash=?', (digest(token),)).fetchone()
            return row[0] if row else None

    def heartbeat(self, node_id, data):
        with self.lock, self.db:
            self.db.execute('UPDATE nodes SET last_seen=?,applied=?,running=?,error=?,version=? WHERE id=?',
                            (time.time(), str(data.get('applied', ''))[:64], int(data.get('running') is True),
                             str(data.get('error', ''))[:500], str(data.get('version', ''))[:32], node_id))
