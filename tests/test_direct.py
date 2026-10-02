"""Direct forwarding is explicit, persists across upgrades, and uses no exit."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import Agent, atomic_json
from core import Store
from diagnostics import diagnose
from test_gost_integration import BINARY, Echo, free_port, tcp_roundtrip
import test_panel


class DirectTests(unittest.TestCase):
    def test_direct_without_exit_roundtrip_migration_and_mode_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'correct-horse-password')
            try:
                entry = store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
                data = {'name':'direct','mode':'direct','entry_id':entry,'listen_port':18080,
                        'targets':'127.0.0.1:19080\n[::1]:19081'}
                rid = store.save_rule(data)
                payload = store.config(entry)
                self.assertEqual(payload['files'], {})
                self.assertEqual(payload['config']['chains'], [])
                service = payload['config']['services'][0]
                self.assertNotIn('chain', service['handler'])
                self.assertEqual([n['addr'] for n in service['forwarder']['nodes']], ['127.0.0.1:19080','[::1]:19081'])
                rule = store.snapshot()['rules'][0]
                self.assertEqual(rule['mode'], 'direct')
                self.assertIsNone(rule['exit_id'])
                self.assertIsNone(rule['tunnel_port'])
                self.assertEqual(rule['exit_nodes'], [])
                with self.assertRaises(ValueError): store.save_rule(data)
                with self.assertRaises(ValueError): store.save_rule({**data, 'mode':'invalid'})
                with self.assertRaises(ValueError): store.save_rule({**data, 'mode':'tunnel'})
                original = payload['revision']
                store.db.close()
                store = Store(directory)
                self.assertEqual(store.config(entry)['revision'], original)
                self.assertEqual(store.db.execute('PRAGMA foreign_key_check').fetchall(), [])
                exit_node = store.save_node({'name':'exit','role':'exit','host':'127.0.0.1'})
                store.save_rule({**data, 'mode':'tunnel','exit_id':exit_node},rid)
                self.assertEqual(store.config(entry)['config']['services'], [])
                store.heartbeat(exit_node, {'running':True,'applied':store.config(exit_node)['revision']})
                self.assertEqual(len(store.config(entry)['config']['chains']), 1)
                store.save_rule({**data,'exit_id':None},rid)
                self.assertEqual(store.config(exit_node)['config']['services'], [])
                self.assertEqual(store.config(entry)['revision'], original)
                # Tunnel rules elsewhere never change the direct payload.
                store.save_rule({**data,'mode':'tunnel','exit_id':exit_node,'listen_port':18081})
                with store.db: store.db.execute('UPDATE nodes SET last_seen=0 WHERE id=?',(exit_node,))
                self.assertEqual(store.config(entry)['revision'], original)
                store.save_rule({**data,'enabled':False},rid)
                self.assertEqual(store.config(entry)['config']['services'], [])
            finally:
                store.db.close()

    def test_v03_migration_preserves_tunnel_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'correct-horse-password')
            try:
                entry = store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
                exit_node = store.save_node({'name':'exit','role':'exit','host':'127.0.0.1'})
                rid = store.save_rule({'name':'tunnel','entry_id':entry,'exit_id':exit_node,'listen_port':18080,'targets':'127.0.0.1:19080'})
                store.heartbeat(exit_node, {'running':True,'applied':store.config(exit_node)['revision']})
                revisions = [store.config(n)['revision'] for n in (entry,exit_node)]
                secret = store.db.execute('SELECT secret FROM rules WHERE id=?',(rid,)).fetchone()[0]
                # Reconstruct the 0.3 rules schema to exercise the real upgrade.
                with store.db:
                    store.db.execute('''CREATE TABLE rules_v03 (
                      id TEXT PRIMARY KEY,name TEXT NOT NULL,protocol TEXT NOT NULL,
                      entry_id TEXT NOT NULL REFERENCES groups(id),exit_id TEXT NOT NULL REFERENCES groups(id),
                      listen_port INTEGER NOT NULL,tunnel_port INTEGER NOT NULL,target_host TEXT NOT NULL,target_port INTEGER NOT NULL,
                      enabled INTEGER NOT NULL,secret TEXT NOT NULL,targets_json TEXT NOT NULL DEFAULT '[]',
                      UNIQUE(entry_id,protocol,listen_port),UNIQUE(exit_id,tunnel_port))''')
                    store.db.execute('INSERT INTO rules_v03 SELECT * FROM rules')
                    store.db.execute('DROP TABLE rules')
                    store.db.execute('ALTER TABLE rules_v03 RENAME TO rules')
                    store.db.execute("DELETE FROM settings WHERE key='direct_schema'")
                store.db.close()
                store = Store(directory)
                self.assertEqual([store.config(n)['revision'] for n in (entry,exit_node)], revisions)
                self.assertEqual(store.db.execute('SELECT secret FROM rules WHERE id=?',(rid,)).fetchone()[0],secret)
                self.assertEqual(store.snapshot()['rules'][0]['mode'],'tunnel')
            finally:
                store.db.close()


class DirectAPI(test_panel.APITests):
    test_authentication_csrf_and_node_isolation = None
    test_rate_limit_and_security_headers = None
    test_upgrade_requires_login_csrf_and_enabled_helper = None
    test_group_enrollment_shared_command_isolated_devices_and_csrf = None
    test_authenticated_cached_asset_download_and_expired_credentials = None

    def test_direct_api_csrf_diagnostics_and_edit(self):
        self.login()
        entry = self.store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
        data = {'name':'direct','mode':'direct','entry_id':entry,'exit_id':None,'listen_port':18080,'targets':'127.0.0.1:19080'}
        self.assertEqual(self.call('/api/rules','POST',data,csrf=False)[0],403)
        status,created,_ = self.call('/api/rules','POST',data)
        self.assertEqual(status,201)
        rid = created['id']
        state = self.call('/api/state')[1]
        self.assertEqual(state['rules'][0]['mode'],'direct')
        with mock.patch('diagnostics.check_entry',return_value={'label':'入口监听','address':'127.0.0.1:18080','ok':True,'message':'test'}), mock.patch('diagnostics.check_target',return_value={'label':'直连目标','address':'127.0.0.1:19080','ok':True,'message':'test'}), mock.patch('diagnostics.check_exit',side_effect=AssertionError('Direct must not dial exits')):
            status,result,_=self.call('/api/rules/'+rid+'/diagnose','POST',{})
            self.assertEqual(status,200)
            self.assertTrue(all(c['ok'] for c in result['checks']))
            self.assertTrue(any(c['label']=='直连目标' for c in result['checks']))
            self.assertIn('面板服务器',result['scope'])
        self.assertEqual(self.call('/api/rules/'+rid,'PUT',{**data,'enabled':False})[0],200)
        self.assertEqual(self.store.config(entry)['config']['services'],[])


@unittest.skipUnless(BINARY, 'Set GOST_BINARY for actual direct TCP/UDP tests')
class DirectIntegration(unittest.TestCase):
    def test_tcp_udp_round_robin_large_datagrams_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root/'panel','correct-horse-password')
            echoes, agent = [], None
            try:
                entry=store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
                tcp_echoes=[Echo(prefix=p) for p in (b'A:',b'B:')]
                udp_echoes=[Echo(udp=True,prefix=p) for p in (b'A:',b'B:')]
                echoes=tcp_echoes+udp_echoes
                tcp_port,udp_port=free_port(),free_port()
                rule_ids=[]
                for protocol,port,backends in (('tcp',tcp_port,tcp_echoes),('udp',udp_port,udp_echoes)):
                    rule_ids.append(store.save_rule({'name':protocol,'mode':'direct','entry_id':entry,
                        'protocol':protocol,'listen_port':port,'targets':'\n'.join('127.0.0.1:'+str(e.port) for e in backends)}))
                folder=root/'entry';folder.mkdir();atomic_json(folder/'agent.json',{})
                agent=Agent(folder,BINARY);agent.apply(store.config(entry))
                self.assertTrue(all(c['ok'] for c in diagnose(store,rule_ids[0])['checks']))
                self.assertEqual({tcp_roundtrip(tcp_port,b'hello direct') for _ in range(8)}, {b'A:hello direct',b'B:hello direct'})
                payload=b'udp direct'+b'x'*8192
                responses=[]
                for _ in range(8):
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                        sock.settimeout(3);sock.sendto(payload,('127.0.0.1',udp_port))
                        responses.append(sock.recvfrom(65535)[0])
                self.assertEqual(set(responses),{b'A:'+payload,b'B:'+payload})
                agent.stop_process()
                agent.apply(store.config(entry))
                self.assertIn(tcp_roundtrip(tcp_port,b'reloaded'),{b'A:reloaded',b'B:reloaded'})
                for rid in rule_ids:
                    old=next(r for r in store.snapshot()['rules'] if r['id']==rid)
                    store.save_rule({**old,'enabled':False},rid)
                agent.apply(store.config(entry))
                with self.assertRaises(OSError): tcp_roundtrip(tcp_port)
            finally:
                if agent: agent.stop_process()
                for echo in echoes: echo.close()
                store.db.close()
