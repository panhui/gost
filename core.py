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

PANEL_VERSION = '0.4.0'
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
        self.migrate_groups()
        self.migrate_direct_rules()
        if not self.db.execute("SELECT value FROM settings WHERE key='password'").fetchone():
            if not initial_password or len(initial_password) < 12:
                raise ValueError('首次启动需要设置至少 12 位的 GOST_ADMIN_PASSWORD')
            self.db.execute("INSERT INTO settings VALUES ('password', ?)", (password_hash(initial_password),))
        # Existing installation commands become permanent too.
        self.db.execute('UPDATE installs SET expires=0')
        self.db.commit()

    def migrate_groups(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
            offline_after INTEGER NOT NULL DEFAULT 60,
            failover_id TEXT REFERENCES groups(id), remark TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS group_installs (
            token_hash TEXT PRIMARY KEY, group_id TEXT NOT NULL UNIQUE REFERENCES groups(id) ON DELETE CASCADE,
            token TEXT NOT NULL DEFAULT '');
        ''')
        if 'token' not in {r[1] for r in self.db.execute('PRAGMA table_info(group_installs)')}:
            self.db.execute("ALTER TABLE group_installs ADD COLUMN token TEXT NOT NULL DEFAULT ''")
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(nodes)')}
        if 'group_id' not in columns:
            self.db.execute('ALTER TABLE nodes ADD COLUMN group_id TEXT REFERENCES groups(id)')
        if 'machine_id' not in columns:
            self.db.execute("ALTER TABLE nodes ADD COLUMN machine_id TEXT NOT NULL DEFAULT ''")
        if 'enabled' not in columns:
            self.db.execute('ALTER TABLE nodes ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS machine_identity ON nodes(machine_id) WHERE machine_id<>''")
        if not self.db.execute("SELECT 1 FROM settings WHERE key='groups_schema'").fetchone():
            # Preserve rule and node IDs, credentials, certificates and installed agents.
            # One legacy node becomes one group; new enrollment can add more members.
            with self.db:
                for row in self.db.execute('SELECT id,name,role FROM nodes WHERE group_id IS NULL').fetchall():
                    self.db.execute('INSERT INTO groups(id,name,role) VALUES (?,?,?)', tuple(row))
                    self.db.execute('UPDATE nodes SET group_id=? WHERE id=?', (row['id'], row['id']))
                self.db.execute('''CREATE TABLE rules_grouped (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
                    entry_id TEXT NOT NULL REFERENCES groups(id), exit_id TEXT NOT NULL REFERENCES groups(id),
                    listen_port INTEGER NOT NULL, tunnel_port INTEGER NOT NULL,
                    target_host TEXT NOT NULL, target_port INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, secret TEXT NOT NULL, targets_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(entry_id,protocol,listen_port), UNIQUE(exit_id,tunnel_port))''')
                self.db.execute('INSERT INTO rules_grouped SELECT * FROM rules')
                self.db.execute('DROP TABLE rules')
                self.db.execute('ALTER TABLE rules_grouped RENAME TO rules')
                self.db.execute("INSERT INTO settings VALUES ('groups_schema','1')")

    def migrate_direct_rules(self):
        if self.db.execute("SELECT 1 FROM settings WHERE key='direct_schema'").fetchone():
            return
        # NULL exit and tunnel port explicitly mean entry-to-target forwarding.
        # Existing tunnel rules retain all IDs, ports and credentials.
        with self.db:
            self.db.execute('''CREATE TABLE rules_direct (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
                entry_id TEXT NOT NULL REFERENCES groups(id), exit_id TEXT REFERENCES groups(id),
                listen_port INTEGER NOT NULL, tunnel_port INTEGER,
                target_host TEXT NOT NULL, target_port INTEGER NOT NULL,
                enabled INTEGER NOT NULL, secret TEXT NOT NULL, targets_json TEXT NOT NULL DEFAULT '[]',
                CHECK ((exit_id IS NULL AND tunnel_port IS NULL) OR
                       (exit_id IS NOT NULL AND tunnel_port IS NOT NULL)),
                UNIQUE(entry_id,protocol,listen_port), UNIQUE(exit_id,tunnel_port))''')
            self.db.execute('INSERT INTO rules_direct SELECT * FROM rules')
            self.db.execute('DROP TABLE rules')
            self.db.execute('ALTER TABLE rules_direct RENAME TO rules')
            self.db.execute("INSERT INTO settings VALUES ('direct_schema','1')")

    def group(self, group_id):
        row = self.db.execute('SELECT * FROM groups WHERE id=?', (group_id,)).fetchone()
        if not row:
            raise ValueError('设备组不存在')
        return dict(row)

    def group_path(self, group_id):
        result, seen = [], set()
        while group_id:
            if group_id in seen:
                raise ValueError('故障转移不能形成循环')
            seen.add(group_id)
            group = self.group(group_id)
            result.append(group)
            group_id = group['failover_id']
        return result

    def validate_group_ports(self):
        reserved = {}
        for rule in self.db.execute('SELECT * FROM rules'):
            for side, port_key in (('entry', 'listen_port'), ('exit', 'tunnel_port')):
                for group in self.group_path(rule[side + '_id']):
                    key = (side, group['id'], rule['protocol'] if side == 'entry' else 'tcp', rule[port_key])
                    if key in reserved:
                        raise ValueError('设备组或故障转移组存在重复监听端口，请调整规则端口')
                    reserved[key] = rule['id']

    def save_group(self, data, group_id=None):
        with self.lock, self.db:
            old = self.group(group_id) if group_id else None
            role = data.get('role', old['role'] if old else 'entry')
            if role not in ('entry', 'exit') or (old and old['role'] != role):
                raise ValueError('设备组类型必须为入口或出口，创建后不能修改')
            value = data.get('offline_after', 60)
            if isinstance(value, bool) or not str(value).isdigit() or not 20 <= int(value) <= 3600:
                raise ValueError('负载下线时间必须为 20–3600 秒的整数')
            failover = data.get('failover_id') or None
            if failover and self.group(failover)['role'] != role:
                raise ValueError('故障转移组必须与当前组类型相同')
            title, remark = name(data.get('name', '')), str(data.get('remark', '')).strip()
            if len(remark) > 500:
                raise ValueError('备注最多 500 字')
            values = (title, role, int(value), failover, remark)
            if old:
                self.db.execute('UPDATE groups SET name=?,role=?,offline_after=?,failover_id=?,remark=? WHERE id=?', values + (group_id,))
            else:
                group_id = secrets.token_hex(8)
                self.db.execute('INSERT INTO groups(name,role,offline_after,failover_id,remark,id) VALUES (?,?,?,?,?,?)', values + (group_id,))
            self.group_path(group_id)
            self.validate_group_ports()
            self.audit('保存设备组：' + title)
            return group_id

    def delete_group(self, group_id):
        with self.lock, self.db:
            group = self.group(group_id)
            if self.db.execute('SELECT id FROM nodes WHERE group_id=?', (group_id,)).fetchone():
                raise ValueError('请先移除组内设备')
            if self.db.execute('SELECT id FROM rules WHERE entry_id=? OR exit_id=?', (group_id, group_id)).fetchone():
                raise ValueError('请先删除使用该设备组的规则')
            if self.db.execute('SELECT id FROM groups WHERE failover_id=?', (group_id,)).fetchone():
                raise ValueError('请先取消其他组对该组的故障转移设置')
            self.db.execute('DELETE FROM groups WHERE id=?', (group_id,))
            self.audit('删除设备组：' + group['name'])

    def members(self, group_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM nodes WHERE group_id=? ORDER BY id', (group_id,))]

    def online(self, node, now=None):
        return bool(node['last_seen'] and (time.time() if now is None else now) - node['last_seen'] < self.group(node['group_id'])['offline_after'])

    def ready(self, node, now=None):
        return bool(node['enabled'] and self.online(node, now) and node['running'] and not node['error'] and node['applied'])

    def exit_pool(self, group_id, now=None, desired_cache=None):
        now = time.time() if now is None else now
        # Preload one healthy backup tier as well as all healthy primary members.
        # GOST backup selectors can use it when entry-to-exit dialing fails even
        # while the exit's separate management heartbeat still succeeds.
        result = []
        desired_cache = {} if desired_cache is None else desired_cache
        for index, group in enumerate(self.group_path(group_id)):
            members = []
            for node in self.members(group['id']):
                if not self.ready(node, now):
                    continue
                if node['id'] not in desired_cache:
                    desired_cache[node['id']] = self.exit_config(node)
                if node['applied'] == desired_cache[node['id']]['revision']:
                    members.append(node)
            result.extend((n, index > 0) for n in members)
            if index > 0 and members:
                break
        return result

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
            enabled = data.get('enabled', bool(old['enabled']) if node_id else True)
            if type(enabled) is not bool:
                raise ValueError('enabled 必须为布尔值')
            group_id = data.get('group_id', old['group_id'] if node_id else None)
            if node_id and not group_id:
                raise ValueError('请选择设备组')
            if group_id and self.group(group_id)['role'] != role:
                raise ValueError('设备类型必须与设备组一致')
            if group_id and (not node_id or old['group_id'] != group_id) and len(self.members(group_id)) >= 200:
                raise ValueError('单组最多允许 200 台设备')
            if node_id:
                self.db.execute('UPDATE nodes SET name=?,host=?,group_id=?,enabled=? WHERE id=?', (title, hostname, group_id, int(enabled), node_id))
            else:
                node_id = secrets.token_hex(8)
                if not group_id:
                    group_id = node_id
                    self.db.execute('INSERT INTO groups(id,name,role) VALUES (?,?,?)', (group_id,title,role))
                cert, key = certificate('exit-' + node_id + '.gost.internal') if role == 'exit' else ('', '')
                self.db.execute('INSERT INTO nodes(id,name,role,host,cert,private_key,group_id,enabled) VALUES (?,?,?,?,?,?,?,?)',
                                (node_id, title, role, hostname, cert, key, group_id, int(enabled)))
            self.audit('保存节点：' + title)
            return node_id

    def delete_node(self, node_id):
        with self.lock, self.db:
            node = self.node(node_id)
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
            mode = data.get('mode', 'direct' if 'exit_id' in data and data['exit_id'] is None else 'tunnel')
            if mode not in ('direct', 'tunnel'):
                raise ValueError('转发模式必须为 direct 或 tunnel')
            entry = self.group(data.get('entry_id'))
            exit_node = None if mode == 'direct' else self.group(data.get('exit_id'))
            if entry['role'] != 'entry' or (exit_node and exit_node['role'] != 'exit'):
                raise ValueError('请选择入口设备组和出口设备组')
            protocol = data.get('protocol', 'tcp')
            if protocol not in ('tcp', 'udp'):
                raise ValueError('仅支持 TCP 或 UDP')
            listen = data.get('listen_port')
            if not listen:
                used_entry = self.reserved_ports(entry['id'], 'entry', protocol, rule_id)
                available = [p for p in range(2000, 60001) if p not in used_entry]
                if not available:
                    raise ValueError('入口没有可用的自动分配端口')
                listen = secrets.choice(available)
            listen = port(listen)
            tunnel = data.get('tunnel_port') if exit_node else None
            if exit_node and not tunnel:
                used = self.reserved_ports(exit_node['id'], 'exit', 'tcp', rule_id)
                tunnel = next((p for p in range(20000, 60000) if p not in used), None)
                if tunnel is None:
                    raise ValueError('出口没有可用的自动分配端口')
            tunnel = port(tunnel) if exit_node else None
            target_list = targets(data['targets']) if 'targets' in data else [{'host': host(data.get('target_host', '')), 'port': port(data.get('target_port'))}]
            title, target, target_port = name(data.get('name', '')), target_list[0]['host'], target_list[0]['port']
            if type(data.get('enabled', True)) is not bool:
                raise ValueError('enabled 必须为布尔值')
            values = (title, protocol, entry['id'], exit_node['id'] if exit_node else None, listen, tunnel, target,
                      target_port, int(data.get('enabled', True)), old['secret'] if old else secrets.token_urlsafe(32), json.dumps(target_list))
            try:
                if old:
                    self.db.execute('UPDATE rules SET name=?,protocol=?,entry_id=?,exit_id=?,listen_port=?,tunnel_port=?,target_host=?,target_port=?,enabled=?,secret=?,targets_json=? WHERE id=?', values + (rule_id,))
                else:
                    rule_id = secrets.token_hex(8)
                    self.db.execute('INSERT INTO rules(id,name,protocol,entry_id,exit_id,listen_port,tunnel_port,target_host,target_port,enabled,secret,targets_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (rule_id,) + values)
            except sqlite3.IntegrityError:
                raise ValueError('入口监听端口或出口隧道端口已被其他规则使用')
            self.validate_group_ports()
            self.audit('保存规则：' + title)
            return rule_id

    def delete_rule(self, rule_id):
        with self.lock, self.db:
            row = self.db.execute('SELECT name FROM rules WHERE id=?', (rule_id,)).fetchone()
            if not row:
                raise ValueError('规则不存在')
            self.db.execute('DELETE FROM rules WHERE id=?', (rule_id,))
            self.audit('删除规则：' + row[0])

    def reserved_ports(self, group_id, side, protocol, excluded=None):
        scope = {g['id'] for g in self.group_path(group_id)}
        used = set()
        for row in self.db.execute('SELECT * FROM rules WHERE id<>?', (excluded or '',)):
            if side == 'entry' and row['protocol'] != protocol:
                continue
            if scope.intersection(g['id'] for g in self.group_path(row[side + '_id'])):
                used.add(row['listen_port' if side == 'entry' else 'tunnel_port'])
        return used

    def rules_for(self, node):
        if not node['enabled']:
            return []
        return [dict(r) for r in self.db.execute('SELECT * FROM rules WHERE enabled=1 ORDER BY id')
                if node['group_id'] in {g['id'] for g in self.group_path(r[node['role'] + '_id'])}]

    @staticmethod
    def payload(config, files):
        payload = {'config': config, 'files': files}
        payload['revision'] = digest(json.dumps(payload, sort_keys=True))
        return payload

    def exit_config(self, node):
        config = {'services': [], 'chains': [], 'log': {'level': 'warn'}}
        files = {}
        for rule in self.rules_for(node):
            target_list = json.loads(rule['targets_json']) or [{'host': rule['target_host'], 'port': rule['target_port']}]
            files['server.pem'], files['server.key'] = node['cert'], node['private_key']
            config['services'].append({
                'name': 'rule-' + rule['id'], 'addr': ':' + str(rule['tunnel_port']),
                'handler': {'type': 'relay', 'auth': {'username': rule['id'], 'password': rule['secret']},
                            'metadata': {'readTimeout': '10s', 'udpBufferSize': '65535', 'nodelay': True}},
                'listener': {'type': 'tls', 'tls': {'certFile': 'server.pem', 'keyFile': 'server.key',
                             'options': {'minVersion': 'VersionTLS12'}}},
                'forwarder': {'nodes': [{'name': 'target-' + str(i), 'addr': address(t['host'], t['port'])} for i, t in enumerate(target_list)],
                              'selector': {'strategy': 'round', 'maxFails': 1, 'failTimeout': '10s'}}})
        return self.payload(config, files)

    def config(self, node_id, desired_cache=None):
        with self.lock:
            desired_cache = {} if desired_cache is None else desired_cache
            node = self.node(node_id)
            if node['role'] == 'exit':
                if node_id not in desired_cache:
                    desired_cache[node_id] = self.exit_config(node)
                return desired_cache[node_id]
            config = {'services': [], 'chains': [], 'log': {'level': 'warn'}}
            files = {}
            now = time.time()
            for rule in self.rules_for(node):
                rule_name = 'rule-' + rule['id']
                target_list = json.loads(rule['targets_json']) or [{'host': rule['target_host'], 'port': rule['target_port']}]
                if rule['exit_id'] is None:
                    config['services'].append({
                        'name': rule_name, 'addr': ':' + str(rule['listen_port']),
                        'handler': {'type': rule['protocol'], **({'metadata': {'readBufferSize': '65535'}} if rule['protocol'] == 'udp' else {})},
                        'listener': {'type': rule['protocol'], **({'metadata': {'keepAlive': True, 'ttl': '60s', 'readBufferSize': '65535'}} if rule['protocol'] == 'udp' else {})},
                        'forwarder': {'nodes': [{'name': 'target-' + str(i), 'addr': address(t['host'], t['port'])} for i, t in enumerate(target_list)],
                                      'selector': {'strategy': 'round', 'maxFails': 1, 'failTimeout': '10s'}}})
                    continue
                pool = self.exit_pool(rule['exit_id'], now, desired_cache)
                if not pool:
                    # Never generate an empty hop: GOST could interpret that as
                    # a direct route. No healthy exit means no entry listener.
                    continue
                target = address(target_list[0]['host'], target_list[0]['port'])
                hops = []
                has_primary = any(not backup for _, backup in pool)
                for exit_node, backup in pool:
                    cert_name = 'exit-' + exit_node['id'] + '.pem'
                    files[cert_name] = exit_node['cert']
                    hops.append({
                        'name': 'exit-' + exit_node['id'], 'addr': address(exit_node['host'], rule['tunnel_port']),
                        'metadata': {'backup': bool(backup and has_primary)},
                        'connector': {'type': 'relay', 'auth': {'username': rule['id'], 'password': rule['secret']}, 'metadata': {'nodelay': True}},
                        'dialer': {'type': 'tls', 'tls': {'caFile': cert_name, 'secure': True,
                                   'serverName': 'exit-' + exit_node['id'] + '.gost.internal',
                                   'options': {'minVersion': 'VersionTLS12'}}}})
                # The selector must never become an empty route when ALL exits
                # fail between heartbeats. A permanent unreachable backup keeps
                # such requests closed instead of dialing the target directly.
                hops.append({'name': 'closed-route', 'addr': '127.0.0.1:0',
                             'metadata': {'backup': True, 'maxFails': '2147483647'},
                             'connector': {'type': 'relay'}, 'dialer': {'type': 'tcp'}})
                config['services'].append({
                    'name': rule_name, 'addr': ':' + str(rule['listen_port']),
                    'handler': {'type': rule['protocol'], 'chain': rule_name, 'retries': min(len(hops), 32),
                                **({'metadata': {'readBufferSize': '65535'}} if rule['protocol'] == 'udp' else {})},
                    'listener': {'type': rule['protocol'], **({'metadata': {'keepAlive': True, 'ttl': '60s', 'readBufferSize': '65535'}} if rule['protocol'] == 'udp' else {})},
                    'forwarder': {'nodes': [{'name': 'target', 'addr': target}]}})
                config['chains'].append({'name': rule_name, 'hops': [{'name': rule_name + '-exit',
                    'selector': {'strategy': 'round', 'maxFails': 1, 'failTimeout': '10s'}, 'nodes': hops}]})
            return self.payload(config, files)

    def snapshot(self):
        with self.lock:
            now = time.time()
            desired_cache = {}
            nodes = []
            for row in self.db.execute('SELECT id,name,role,host,group_id,enabled,last_seen,applied,running,error,version FROM nodes ORDER BY rowid'):
                node = dict(row)
                desired = self.config(node['id'], desired_cache)
                node['online'] = self.online(node, now)
                node['synced'] = node['applied'] == desired['revision']
                node['service_count'] = len(desired['config']['services'])
                node['desired'] = desired['revision']
                node['eligible'] = bool(self.ready(node, now) and node['synced'])
                nodes.append(node)
            groups = [dict(r) for r in self.db.execute('SELECT * FROM groups ORDER BY rowid')]
            for group in groups:
                members = [n for n in nodes if n['group_id'] == group['id']]
                group['node_count'] = len(members)
                group['online_count'] = sum(n['online'] for n in members)
                group['eligible_count'] = sum(n['eligible'] for n in members)
            rules = [dict(r) for r in self.db.execute('SELECT id,name,protocol,entry_id,exit_id,listen_port,tunnel_port,target_host,target_port,enabled,targets_json FROM rules ORDER BY rowid DESC')]
            for rule in rules:
                rule['mode'] = 'direct' if rule['exit_id'] is None else 'tunnel'
                rule['targets'] = [address(t['host'], t['port']) for t in (json.loads(rule.pop('targets_json')) or [{'host': rule['target_host'], 'port': rule['target_port']}])]
                pool = self.exit_pool(rule['exit_id'], now, desired_cache) if rule['enabled'] else []
                primary = [n['id'] for n, backup in pool if not backup]
                rule['exit_nodes'] = primary or [n['id'] for n, _ in pool]
                rule['backup_nodes'] = [n['id'] for n, backup in pool if backup] if primary else []
                rule['failover_active'] = bool(pool and not primary)
                rule['entry_nodes'] = [n['id'] for n in nodes if n['enabled'] and n['online'] and
                    n['group_id'] in {g['id'] for g in self.group_path(rule['entry_id'])}]
            audit = [dict(r) for r in self.db.execute('SELECT time,message FROM audit ORDER BY id DESC LIMIT 30')]
            return {'nodes': nodes, 'groups': groups, 'rules': rules, 'audit': audit, 'gost_version': GOST_VERSION, 'panel_version': PANEL_VERSION}

    def installation(self, node_id):
        with self.lock, self.db:
            self.node(node_id)
            token = secrets.token_urlsafe(32)
            self.db.execute('DELETE FROM installs WHERE node_id=?', (node_id,))
            self.db.execute('INSERT INTO installs VALUES (?,?,?)', (digest(token), node_id, 0))
            self.audit('生成节点安装脚本：' + self.node(node_id)['name'])
            return token

    def group_installation(self, group_id, rotate=False):
        with self.lock, self.db:
            group = self.group(group_id)
            existing = self.db.execute('SELECT token FROM group_installs WHERE group_id=?', (group_id,)).fetchone()
            if existing and existing[0] and not rotate:
                return existing[0]
            token = secrets.token_urlsafe(32)
            self.db.execute('DELETE FROM group_installs WHERE group_id=?', (group_id,))
            self.db.execute('INSERT INTO group_installs(token_hash,group_id,token) VALUES (?,?,?)', (digest(token), group_id, token))
            self.audit('生成设备组安装命令：' + group['name'])
            return token

    def bootstrap_node(self, token):
        row = self.db.execute('SELECT node_id FROM installs WHERE token_hash=?', (digest(token),)).fetchone()
        if not row:
            row = self.db.execute('SELECT group_id FROM group_installs WHERE token_hash=?', (digest(token),)).fetchone()
        if not row:
            raise ValueError('安装凭证已被撤销或节点已删除，请在面板重新生成')
        return row[0]

    def enroll(self, token, metadata=None, remote_host=None):
        with self.lock, self.db:
            self.bootstrap_node(token)
            row = self.db.execute('SELECT node_id FROM installs WHERE token_hash=?', (digest(token),)).fetchone()
            if row:
                node_id = row[0]
            else:
                group_id = self.db.execute('SELECT group_id FROM group_installs WHERE token_hash=?', (digest(token),)).fetchone()[0]
                group = self.group(group_id)
                metadata = metadata or {}
                machine_id = str(metadata.get('machine_id', ''))
                if not re.fullmatch('[a-f0-9]{32,64}', machine_id):
                    raise ValueError('设备身份无效，请使用最新版组安装命令')
                existing = self.db.execute('SELECT id FROM nodes WHERE machine_id=?', (machine_id,)).fetchone()
                if not existing and metadata.get('previous_token'):
                    previous = self.authenticate_node(str(metadata['previous_token']))
                    existing = (previous,) if previous else None
                if existing:
                    node_id = existing[0]
                    if self.node(node_id)['role'] != group['role']:
                        raise ValueError('本机已注册为其他设备类型，请先在面板移除原设备')
                    if self.node(node_id)['group_id'] != group_id and len(self.members(group_id)) >= 200:
                        raise ValueError('单组最多允许 200 台设备')
                    self.db.execute('UPDATE nodes SET group_id=?,machine_id=? WHERE id=?', (group_id,machine_id,node_id))
                    if metadata.get('host'):
                        self.db.execute('UPDATE nodes SET host=? WHERE id=?', (host(metadata['host']),node_id))
                else:
                    if len(self.members(group_id)) >= 200:
                        raise ValueError('单组最多允许 200 台设备')
                    hostname = host(metadata.get('host') or remote_host or '')
                    node_id = self.save_node({'name': name(metadata.get('name') or '设备-' + machine_id[:8]),
                        'role': group['role'], 'group_id': group_id, 'host': hostname})
                    self.db.execute('UPDATE nodes SET machine_id=? WHERE id=?', (machine_id,node_id))
            credential = secrets.token_urlsafe(48)
            self.db.execute('UPDATE nodes SET token_hash=?,last_seen=0,applied=?,running=0,error=? WHERE id=?',
                            (digest(credential), '', '', node_id))
            self.audit('节点完成注册：' + self.node(node_id)['name'])
            return {'node_id': node_id, 'group_id': self.node(node_id)['group_id'], 'token': credential}

    def authenticate_node(self, token):
        with self.lock:
            row = self.db.execute('SELECT id FROM nodes WHERE token_hash=?', (digest(token),)).fetchone()
            return row[0] if row else None

    def heartbeat(self, node_id, data):
        with self.lock, self.db:
            self.db.execute('UPDATE nodes SET last_seen=?,applied=?,running=?,error=?,version=? WHERE id=?',
                            (time.time(), str(data.get('applied', ''))[:64], int(data.get('running') is True),
                             str(data.get('error', ''))[:500], str(data.get('version', ''))[:32], node_id))
