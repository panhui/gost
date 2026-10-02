"""Batch changes are atomic; exports contain portable rules and no credentials."""
import copy
import json
import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from agent import Agent, atomic_json
from core import Store
from test_gost_integration import BINARY, Echo, free_port, tcp_roundtrip
import test_panel


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name,'correct-horse-password')
        self.entry=self.store.save_group({'name':'入口组','role':'entry'})
        self.exit=self.store.save_group({'name':'出口组','role':'exit'})
        self.ids=[self.store.save_rule({'name':'TLS 线路','entry_id':self.entry,'exit_id':self.exit,
            'listen_port':18080,'tunnel_port':18443,'targets':'198.51.100.20:443\n[2001:db8::1]:443'}),
            self.store.save_rule({'name':'UDP 直连','mode':'direct','entry_id':self.entry,'protocol':'udp',
            'listen_port':18081,'targets':'127.0.0.1:53','enabled':False})]

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def test_export_delete_preview_restore_and_fresh_credentials(self):
        secret=self.store.db.execute('SELECT secret FROM rules WHERE id=?',(self.ids[0],)).fetchone()[0]
        document=self.store.export_rules()
        self.assertNotIn(secret,json.dumps(document))
        self.assertNotIn('secret',json.dumps(document))
        self.assertNotIn('cert',json.dumps(document))
        self.assertEqual(len(self.store.export_rules([self.ids[1]])['rules']),1)
        self.assertEqual(self.store.delete_rules(self.ids),2)
        audit=list(self.store.db.execute('SELECT * FROM audit'))
        preview=self.store.import_rules(document,preview=True)
        self.assertEqual(preview['count'],2)
        self.assertEqual(self.store.snapshot()['rules'],[])
        self.assertEqual(list(self.store.db.execute('SELECT * FROM audit')),audit)
        result=self.store.import_rules(preview['document'])
        self.assertEqual(result['imported'],2)
        self.assertTrue(set(self.ids).isdisjoint(result['ids']))
        self.assertEqual(self.store.export_rules(),document)
        self.assertNotEqual(self.store.db.execute('SELECT secret FROM rules WHERE id=?',(result['ids'][0],)).fetchone()[0],secret)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.store.db.close();self.store=Store(self.temp.name)
        self.assertEqual(self.store.export_rules(),document)

    def test_invalid_batches_rollback_rules_and_audit(self):
        document=self.store.export_rules()
        document['rules'][0]['listen_port']=18090
        document['rules'][0]['tunnel_port']=18500
        before=self.store.export_rules()
        audit=list(self.store.db.execute('SELECT * FROM audit'))
        for preview in (False,True):
            with self.assertRaisesRegex(ValueError,'第 2 条'):
                self.store.import_rules(document,preview=preview)
            self.assertEqual(self.store.export_rules(),before)
            self.assertEqual(list(self.store.db.execute('SELECT * FROM audit')),audit)
        for ids in ([],[self.ids[0],'missing'],[self.ids[0]]*2,[None],self.ids[0],['id']*501):
            with self.assertRaises(ValueError): self.store.delete_rules(ids)
            self.assertEqual(self.store.export_rules(),before)
        for bad in ({}, {'format':'gost-panel-rules','version':True,'rules':[]},
                    {'format':'gost-panel-rules','version':1,'rules':[{}]*501}):
            with self.assertRaises(ValueError): self.store.import_rules(bad)
        with self.assertRaises(ValueError): self.store.import_rules(document,options=[])

    def test_group_name_matching_overrides_reset_ports_and_failover_conflicts(self):
        document=self.store.export_rules()
        with tempfile.TemporaryDirectory() as folder:
            other=Store(folder,'correct-horse-password')
            try:
                entry=other.save_group({'name':'入口组','role':'entry'})
                exit_group=other.save_group({'name':'出口组','role':'exit'})
                other.import_rules(document)
                exported=other.export_rules()
                self.assertEqual(exported['rules'][0]['entry_group']['id'],entry)
                self.assertEqual(exported['rules'][0]['exit_group']['id'],exit_group)
                other.save_group({'name':'入口组','role':'entry'})
                with self.assertRaisesRegex(ValueError,'唯一匹配'):
                    other.import_rules(document,{'reset_ports':True})
                plan=other.import_rules(document,{'entry_group_id':entry,'exit_group_id':exit_group,'reset_ports':True},preview=True)
                self.assertNotEqual(plan['document']['rules'][0]['listen_port'],18080)
                self.assertNotEqual(plan['document']['rules'][0]['tunnel_port'],18443)
                self.assertIsNone(plan['document']['rules'][1]['exit_group'])
                other.import_rules(plan['document'])
                self.assertEqual(len(other.snapshot()['rules']),4)
                with self.assertRaises(ValueError):other.import_rules(document,{'entry_group_id':exit_group})
            finally:other.db.close()
        backup=self.store.save_group({'name':'备用入口','role':'entry'})
        self.store.save_group({'name':'入口组','role':'entry','failover_id':backup},self.entry)
        conflict=copy.deepcopy(document);conflict['rules']=conflict['rules'][:1]
        conflict['rules'][0]['tunnel_port']=18500
        with self.assertRaisesRegex(ValueError,'重复监听端口'):
            self.store.import_rules(conflict,{'entry_group_id':backup})
        self.assertEqual(len(self.store.snapshot()['rules']),2)


class BulkAPI(test_panel.APITests):
    test_authentication_csrf_and_node_isolation=None
    test_rate_limit_and_security_headers=None
    test_upgrade_requires_login_csrf_and_enabled_helper=None
    test_group_enrollment_shared_command_isolated_devices_and_csrf=None
    test_authenticated_cached_asset_download_and_expired_credentials=None

    def test_authenticated_atomic_batch_endpoints(self):
        paths=['export','batch-delete','import-preview','import']
        for path in paths:self.assertEqual(self.call('/api/rules/'+path,'POST',{})[0],401)
        self.assertEqual(self.call('/api/rules/export')[0],401)
        self.login()
        for path in paths:self.assertEqual(self.call('/api/rules/'+path,'POST',{},csrf=False)[0],403)
        entry=self.store.save_group({'name':'entry','role':'entry'})
        rid=self.store.save_rule({'name':'direct','entry_id':entry,'mode':'direct','listen_port':18080,'targets':'127.0.0.1:19080'})
        status,document,_=self.call('/api/rules/export','POST',{'ids':[rid]})
        self.assertEqual(status,200)
        status,download,headers=self.call('/api/rules/export?ids='+urllib.parse.quote(json.dumps([rid])))
        self.assertEqual(status,200);self.assertEqual(download,document)
        self.assertIn('attachment; filename="gost-rules-',headers['Content-Disposition'])
        self.assertEqual(self.call('/api/rules/batch-delete','POST',{'ids':[rid,'missing']})[0],400)
        self.assertEqual(len(self.call('/api/state')[1]['rules']),1)
        self.assertEqual(self.call('/api/rules/batch-delete','POST',{'ids':[rid]})[1]['deleted'],1)
        status,plan,_=self.call('/api/rules/import-preview','POST',{'document':document})
        self.assertEqual(status,200);self.assertEqual(self.call('/api/state')[1]['rules'],[])
        self.assertEqual(self.call('/api/rules/import','POST',{'document':plan['document']})[0],201)
        self.assertEqual(self.call('/api/rules/import','POST',{'document':document})[0],400)
        self.assertEqual(len(self.call('/api/state')[1]['rules']),1)
        self.assertEqual(self.call('/api/rules/import','POST',{'document':document,'options':{'reset_ports':True}})[0],201)


@unittest.skipUnless(BINARY,'Set GOST_BINARY for restored batch forwarding')
class BulkIntegration(unittest.TestCase):
    def test_export_delete_import_restores_real_tls_tcp_and_direct_udp(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'panel','correct-horse-password')
            echoes=[Echo(),Echo(udp=True)]
            agents=[]
            try:
                entry=store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
                exit_node=store.save_node({'name':'exit','role':'exit','host':'127.0.0.1'})
                listen,udp_port,tunnel=[free_port() for _ in range(3)]
                store.save_rule({'name':'TLS tcp','entry_id':entry,'exit_id':exit_node,'listen_port':listen,
                    'tunnel_port':tunnel,'targets':'127.0.0.1:'+str(echoes[0].port)})
                store.save_rule({'name':'direct udp','mode':'direct','entry_id':entry,'protocol':'udp',
                    'listen_port':udp_port,'targets':'127.0.0.1:'+str(echoes[1].port)})
                for nid in (exit_node,entry):
                    folder=root/nid;folder.mkdir();atomic_json(folder/'agent.json',{})
                    agents.append(Agent(folder,BINARY))
                def apply():
                    agents[0].apply(store.config(exit_node))
                    store.heartbeat(exit_node,{'running':True,'applied':store.config(exit_node)['revision']})
                    agents[1].apply(store.config(entry))
                def verify():
                    self.assertEqual(tcp_roundtrip(listen,b'restored TLS'),b'restored TLS')
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                        sock.settimeout(3);sock.sendto(b'restored UDP',('127.0.0.1',udp_port))
                        self.assertEqual(sock.recvfrom(65535)[0],b'restored UDP')
                apply();verify()
                document=store.export_rules()
                store.delete_rules([r['id'] for r in store.snapshot()['rules']])
                apply()
                with self.assertRaises(OSError):tcp_roundtrip(listen)
                store.import_rules(document)
                apply();verify()
            finally:
                for agent in agents:agent.stop_process()
                for echo in echoes:echo.close()
                store.db.close()
