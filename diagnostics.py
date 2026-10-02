"""Explicit, authenticated checks from the panel's network location."""
import socket
import ssl
import struct
from concurrent.futures import ThreadPoolExecutor

from core import address


def receive(connection, length):
    data = b''
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise OSError('对端提前关闭连接')
        data += chunk
    return data


def failure(exc):
    if isinstance(exc, socket.gaierror):
        return 'DNS 解析失败，请为域名配置 A/AAAA 记录，或将节点地址改为公网 IP'
    if isinstance(exc, ssl.SSLCertVerificationError):
        return '出口 TLS 证书校验失败，请确认出口已应用当前配置'
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return '连接超时，请检查路由、服务器防火墙和云安全组'
    if isinstance(exc, ConnectionRefusedError):
        return '连接被拒绝，请检查 GOST 是否运行及端口是否监听'
    return str(exc)[:250]


def check_entry(entry, rule):
    endpoint = address(entry['host'], rule['listen_port'])
    try:
        socket.getaddrinfo(entry['host'], rule['listen_port'], type=socket.SOCK_STREAM)
        if rule['protocol'] == 'udp':
            return {'label': '入口地址', 'address': endpoint, 'ok': True, 'message': 'DNS 正常；UDP 业务响应需使用实际客户端验证'}
        with socket.create_connection((entry['host'], rule['listen_port']), timeout=5):
            pass
        return {'label': '入口监听', 'address': endpoint, 'ok': True, 'message': '面板可连接入口 TCP 端口'}
    except OSError as exc:
        return {'label': '入口监听', 'address': endpoint, 'ok': False, 'message': failure(exc)}


def check_exit(exit_node, rule):
    endpoint = address(exit_node['host'], rule['tunnel_port'])
    stage = '出口隧道'
    try:
        context = ssl.create_default_context(cadata=exit_node['cert'])
        with socket.create_connection((exit_node['host'], rule['tunnel_port']), timeout=5) as raw:
            stage = '出口 TLS'
            with context.wrap_socket(raw, server_hostname='exit-' + exit_node['id'] + '.gost.internal') as connection:
                stage = 'Relay 认证 / 出口到落地'
                username, password = rule['id'].encode(), rule['secret'].encode()
                auth = bytes([len(username)]) + username + bytes([len(password)]) + password
                features = b'\x01' + struct.pack('!H', len(auth)) + auth
                features += b'\x04\x00\x02' + struct.pack('!H', 1 if rule['protocol'] == 'udp' else 0)
                connection.sendall(b'\x01\x01' + struct.pack('!H', len(features)) + features)
                version, code, length = struct.unpack('!BBH', receive(connection, 4))
                receive(connection, length)
                if version != 1:
                    raise OSError('出口返回的 Relay 协议版本不匹配')
                if code != 0:
                    raise OSError({2: 'Relay 认证失败，请确认出口已同步规则', 5: '出口没有可用落地目标', 6: '出口连接落地目标失败，请检查落地地址、端口和防火墙'}.get(code, 'Relay 返回错误码 ' + str(code)))
        message = 'TLS 与 Relay 认证通过；出口已建立到一个落地目标的 TCP 连接'
        if rule['protocol'] == 'udp':
            message = 'TLS 与 Relay 认证通过；已创建 UDP 转发连接，尚未验证落地 UDP 响应'
        return {'label': stage, 'address': endpoint, 'ok': True, 'message': message}
    except OSError as exc:
        return {'label': stage, 'address': endpoint, 'ok': False, 'message': failure(exc)}


def diagnose(store, rule_id):
    with store.lock:
        row = store.db.execute('SELECT * FROM rules WHERE id=?', (rule_id,)).fetchone()
        if not row:
            raise ValueError('规则不存在')
        rule = dict(row)
        if not rule['enabled']:
            raise ValueError('请先启用规则并等待节点同步')
        entry, exit_node = store.node(rule['entry_id']), store.node(rule['exit_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        entry_result = pool.submit(check_entry, entry, rule)
        exit_result = pool.submit(check_exit, exit_node, rule)
        checks = [entry_result.result(), exit_result.result()]
    return {'checks': checks, 'scope': '检测由面板服务器发起。入口到出口的实际路由、客户端网络和业务协议仍需从客户端验证；多个落地目标仅检测出口本次选中的一个。'}
