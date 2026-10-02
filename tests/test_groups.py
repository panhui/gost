import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import Store, digest


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, 'correct-horse-password')
        self.entry = self.store.save_group({'name':'入口组', 'role':'entry'})
        self.primary = self.store.save_group({'name':'出口组', 'role':'exit'})
        self.backup = self.store.save_group({'name':'备用组', 'role':'exit'})
        self.store.save_group({'name':'出口组', 'role':'exit', 'offline_after':60, 'failover_id':self.backup}, self.primary)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def join(self, group, machine, hostname='127.0.0.1', token=None):
        token = token or self.store.group_installation(group)
        return self.store.enroll(token, {'machine_id':machine * 32,'name':'设备-' + machine, 'host':hostname}, '192.0.2.1')

    def rule(self, entry=None, exit_group=None, listen=18080, tunnel=18443):
        return self.store.save_rule({'name':'线路','entry_id':entry or self.entry,'exit_id':exit_group or self.primary,
            'listen_port':listen,'tunnel_port':tunnel,'protocol':'tcp','targets':'127.0.0.1:19080'})

    def ready(self, identity):
        self.store.heartbeat(identity['node_id'], {'running':True, 'applied':self.store.config(identity['node_id'])['revision']})

    def test_one_group_command_multiple_devices_and_reinstallation(self):
        token = self.store.group_installation(self.primary)
        one = self.join(self.primary,'a',token=token)
        two = self.join(self.primary,'b',token=token)
        self.assertNotEqual(one['node_id'],two['node_id'])
        self.assertNotEqual(one['token'],two['token'])
        self.assertNotEqual(self.store.node(one['node_id'])['cert'],self.store.node(two['node_id'])['cert'])
        self.store.save_node({'name':'编辑后的名称','role':'exit','host':'192.0.2.10','group_id':self.primary},one['node_id'])
        again = self.store.enroll(token, {'machine_id':'a'*32,'name':'自动名称'}, '192.0.2.99')
        self.assertEqual(again['node_id'],one['node_id'])
        self.assertEqual(self.store.node(again['node_id'])['host'],'192.0.2.10')
        self.assertEqual(self.store.node(again['node_id'])['name'],'编辑后的名称')
        self.assertIsNone(self.store.authenticate_node(one['token']))
        self.assertEqual(self.store.authenticate_node(two['token']),two['node_id'])
        self.assertEqual(self.store.group_installation(self.primary),token)
        self.store.group_installation(self.primary,rotate=True)
        with self.assertRaises(ValueError):
            self.store.enroll(token, {'machine_id':'c'*32}, '127.0.0.1')
        self.assertEqual(self.store.authenticate_node(again['token']),again['node_id'])
        self.assertNotIn('machine_id',json.dumps(self.store.snapshot()))
        with self.assertRaises(ValueError):
            self.store.enroll(self.store.group_installation(self.primary), {'machine_id':'$(id)'}, '127.0.0.1')

    def test_migrate_installed_identity_on_group_reinstall(self):
        nid = self.store.save_node({'name':'旧设备','role':'exit','host':'192.0.2.3'})
        old = self.store.enroll(self.store.installation(nid))
        token = self.store.group_installation(self.primary)
        new = self.store.enroll(token, {'machine_id':'c'*32,'previous_token':old['token']}, '192.0.2.20')
        self.assertEqual(new['node_id'],nid)
        self.assertEqual(self.store.node(nid)['group_id'],self.primary)
        self.assertEqual(self.store.node(nid)['host'],'192.0.2.3')
        self.assertIsNone(self.store.authenticate_node(old['token']))

    def test_membership_expiry_failover_recovery_and_disabled_devices(self):
        token = self.store.group_installation(self.primary)
        one,two = self.join(self.primary,'a',token=token), self.join(self.primary,'b',token=token)
        backup = self.join(self.backup,'c')
        entry = self.join(self.entry,'d')
        rid = self.rule()
        self.assertEqual(self.store.config(entry['node_id'])['config']['services'],[])
        for identity in (one,two,backup): self.ready(identity)
        def current(): return next(r for r in self.store.snapshot()['rules'] if r['id']==rid)
        self.assertEqual(set(current()['exit_nodes']),{one['node_id'],two['node_id']})
        self.assertEqual(current()['backup_nodes'],[backup['node_id']])
        original = self.store.config(entry['node_id'])['revision']
        self.ready(one)  # heartbeat timestamp changes alone must not restart entries.
        self.assertEqual(self.store.config(entry['node_id'])['revision'],original)
        with self.store.db:
            self.store.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(time.time()-61,one['node_id']))
        self.assertEqual(current()['exit_nodes'],[two['node_id']])
        self.assertNotEqual(self.store.config(entry['node_id'])['revision'],original)
        self.store.save_node({'name':'设备-b','role':'exit','host':'127.0.0.1','group_id':self.primary,'enabled':False},two['node_id'])
        self.assertEqual(current()['exit_nodes'],[backup['node_id']])
        self.assertTrue(current()['failover_active'])
        self.ready(one)
        self.assertEqual(current()['exit_nodes'],[one['node_id']])
        self.assertFalse(current()['failover_active'])
        self.store.heartbeat(one['node_id'], {'running':False,'applied':self.store.config(one['node_id'])['revision']})
        self.assertTrue(current()['failover_active'])
        self.store.heartbeat(backup['node_id'], {'running':True,'applied':'wrong','error':''})
        self.assertEqual(self.store.config(entry['node_id'])['config']['services'],[])

    def test_cycle_role_port_conflicts_and_group_deletion(self):
        with self.assertRaisesRegex(ValueError,'循环'):
            self.store.save_group({'name':'备用组','role':'exit','failover_id':self.primary},self.backup)
        self.assertIsNone(self.store.group(self.backup)['failover_id'])
        with self.assertRaisesRegex(ValueError,'类型相同'):
            self.store.save_group({'name':'入口组','role':'entry','failover_id':self.primary},self.entry)
        rid = self.rule()
        with self.assertRaisesRegex(ValueError,'重复监听端口'):
            self.rule(exit_group=self.backup,listen=18081)
        self.assertEqual(len(self.store.snapshot()['rules']),1)
        with self.assertRaisesRegex(ValueError,'使用该设备组'):
            self.store.delete_group(self.entry)
        with self.assertRaisesRegex(ValueError,'取消其他组'):
            self.store.delete_group(self.backup)
        self.store.delete_rule(rid)
        self.store.delete_group(self.entry)

    def test_v02_database_migration_is_idempotent_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(str(Path(folder)/'panel.db'))
            db.executescript('''
            CREATE TABLE nodes (id TEXT PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL,host TEXT NOT NULL,
              cert TEXT NOT NULL DEFAULT '',private_key TEXT NOT NULL DEFAULT '',token_hash TEXT,last_seen REAL NOT NULL DEFAULT 0,
              applied TEXT NOT NULL DEFAULT '',running INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',version TEXT NOT NULL DEFAULT '');
            CREATE TABLE rules (id TEXT PRIMARY KEY,name TEXT NOT NULL,protocol TEXT NOT NULL,
              entry_id TEXT NOT NULL REFERENCES nodes(id),exit_id TEXT NOT NULL REFERENCES nodes(id),
              listen_port INTEGER NOT NULL,tunnel_port INTEGER NOT NULL,target_host TEXT NOT NULL,target_port INTEGER NOT NULL,
              enabled INTEGER NOT NULL,secret TEXT NOT NULL,targets_json TEXT NOT NULL DEFAULT '[]',
              UNIQUE(entry_id,protocol,listen_port),UNIQUE(exit_id,tunnel_port));
            ''')
            db.execute("INSERT INTO nodes(id,name,role,host,token_hash) VALUES ('old-entry','入口','entry','127.0.0.1',?)",(digest('existing-agent-token'),))
            db.execute("INSERT INTO nodes(id,name,role,host,cert,private_key) VALUES ('old-exit','出口','exit','127.0.0.1','keep-cert','keep-key')")
            db.execute("INSERT INTO rules VALUES ('old-rule','原规则','tcp','old-entry','old-exit',18080,18443,'127.0.0.1',19080,1,'keep-secret','[]')")
            db.commit(); db.close()
            for _ in range(2):
                store = Store(folder,'correct-horse-password')
                self.assertEqual(store.authenticate_node('existing-agent-token'),'old-entry')
                self.assertEqual(store.node('old-exit')['private_key'],'keep-key')
                self.assertEqual(store.node('old-entry')['group_id'],'old-entry')
                self.assertEqual(len(store.snapshot()['groups']),2)
                self.assertEqual(store.snapshot()['rules'][0]['id'],'old-rule')
                self.assertEqual(store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
                store.db.close()


if __name__ == '__main__':
    unittest.main()
