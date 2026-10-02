import json
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import PanelServer
from core import Store, certificate, digest, targets
from installers import install_script


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, 'correct-horse-password')
        self.entry = self.store.save_node({'name':'广州入口','role':'entry','host':'127.0.0.1'})
        self.exit = self.store.save_node({'name':'香港出口','role':'exit','host':'127.0.0.1'})
        self.rule = {'name':'测试规则','entry_id':self.entry,'exit_id':self.exit,'listen_port':18080,
                     'tunnel_port':18443,'protocol':'tcp','targets':'127.0.0.1:19080\n[::1]:19081','enabled':True}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_real_configuration_and_no_secret_leak(self):
        rid = self.store.save_rule(self.rule)
        entry = self.store.config(self.entry)
        exit_config = self.store.config(self.exit)
        self.assertEqual(entry['config']['chains'][0]['hops'][0]['nodes'][0]['dialer']['tls']['secure'], True)
        self.assertNotIn('server.key', entry['files'])
        self.assertEqual([x['addr'] for x in exit_config['config']['services'][0]['forwarder']['nodes']], ['127.0.0.1:19080','[::1]:19081'])
        self.assertEqual(exit_config['config']['services'][0]['handler']['auth']['username'], rid)
        state = json.dumps(self.store.snapshot())
        self.assertNotIn('private_key', state)
        self.assertNotIn('secret', state)
        before = entry['revision']
        self.store.save_rule({**self.rule,'enabled':False}, rid)
        self.assertNotEqual(before, self.store.config(self.entry)['revision'])
        self.assertEqual(self.store.config(self.exit)['config']['services'], [])

    def test_ports_roles_and_addresses(self):
        self.store.save_rule(self.rule)
        with self.assertRaisesRegex(ValueError,'端口已'):
            self.store.save_rule({**self.rule,'name':'重复'})
        with self.assertRaises(ValueError):
            self.store.save_rule({**self.rule,'entry_id':self.exit})
        with self.assertRaises(ValueError):
            self.store.save_rule({**self.rule,'listen_port':65536})
        with self.assertRaises(ValueError):
            targets('::1:443')
        with self.assertRaises(ValueError):
            targets('$(whoami):443')
        with self.assertRaises(ValueError):
            targets('example.com:0')
        with self.assertRaisesRegex(ValueError,'先删除'):
            self.store.delete_node(self.entry)
        rid = self.store.save_rule({**self.rule,'listen_port':None,'tunnel_port':None})
        rule = next(r for r in self.store.snapshot()['rules'] if r['id']==rid)
        self.assertTrue(2000 <= rule['listen_port'] <= 60000)
        self.assertNotEqual(rule['listen_port'],18080)
        self.assertNotEqual(rule['tunnel_port'],18443)

    def test_one_time_enrollment_rotation_and_persistence(self):
        token = self.store.installation(self.entry)
        second = self.store.installation(self.entry)
        with self.assertRaises(ValueError):
            self.store.enroll(token)
        identity = self.store.enroll(second)
        self.assertEqual(self.store.authenticate_node(identity['token']),self.entry)
        with self.assertRaises(ValueError):
            self.store.enroll(second)
        replacement = self.store.enroll(self.store.installation(self.entry))
        self.assertIsNone(self.store.authenticate_node(identity['token']))
        self.assertEqual(self.store.authenticate_node(replacement['token']),self.entry)
        expired = self.store.installation(self.exit)
        with self.store.db:
            self.store.db.execute('UPDATE installs SET expires=0 WHERE token_hash=?',(digest(expired),))
        with self.assertRaises(ValueError):
            self.store.enroll(expired)
        self.store.db.close()
        self.store = Store(self.temp.name)
        self.assertEqual(len(self.store.snapshot()['nodes']),2)
        self.store.delete_node(self.entry)
        self.assertIsNone(self.store.authenticate_node(replacement['token']))

    def test_generated_installer_shell_syntax(self):
        import subprocess
        script = install_script('https://example.com:8443', 'certificate', 'test-token')
        result = subprocess.run(['bash','-n'],input=script.encode(),capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertIn('sha256sum --check',script)
        self.assertNotIn('--insecure',script)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        cert,key = certificate('127.0.0.1')
        (root/'cert.pem').write_text(cert)
        (root/'key.pem').write_text(key)
        self.store = Store(root/'data','correct-horse-password')
        self.server = PanelServer(('127.0.0.1',0),self.store,'https://127.0.0.1',cert,True)
        self.url = 'https://127.0.0.1:'+str(self.server.server_port)
        self.server.public_url = self.url
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(root/'cert.pem'),str(root/'key.pem'))
        self.server.socket = context.wrap_socket(self.server.socket,server_side=True,do_handshake_on_connect=False)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.context = ssl.create_default_context(cafile=str(root/'cert.pem'))
        self.cookie, self.csrf = '', ''

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.db.close()
        self.temp.cleanup()

    def call(self,path,method='GET',data=None,csrf=True,token=None):
        headers={'Content-Type':'application/json','Cookie':self.cookie}
        if csrf: headers['X-CSRF-Token']=self.csrf
        if token: headers['Authorization']='Bearer '+token
        req=urllib.request.Request(self.url+path,headers=headers,method=method,data=json.dumps(data).encode() if data is not None else None)
        try:
            response=urllib.request.urlopen(req,context=self.context,timeout=5)
        except urllib.error.HTTPError as error:
            return error.code,json.loads(error.read()),error.headers
        content=response.read()
        return response.status,json.loads(content) if response.headers['Content-Type'].startswith('application/json') else content,response.headers

    def login(self):
        status,data,headers=self.call('/api/login','POST',{'password':'correct-horse-password'})
        self.assertEqual(status,200)
        self.cookie=headers['Set-Cookie'].split(';')[0]
        self.csrf=data['csrf']
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('Secure',headers['Set-Cookie'])

    def test_authentication_csrf_and_node_isolation(self):
        self.assertEqual(self.call('/api/state')[0],401)
        self.assertEqual(self.call('/api/login','POST',{'password':'bad'})[0],401)
        self.login()
        data={'name':'入口','role':'entry','host':'127.0.0.1'}
        self.assertEqual(self.call('/api/nodes','POST',data,csrf=False)[0],403)
        status,result,_=self.call('/api/nodes','POST',data)
        self.assertEqual(status,201)
        nid=result['id']
        status,install,_=self.call('/api/nodes/'+nid+'/install','POST',{})
        self.assertEqual(status,200)
        self.assertNotIn('--insecure',install['command'])
        token=self.store.installation(nid)
        self.assertEqual(self.call('/install/'+token)[0],200)
        status,identity,_=self.call('/agent/enroll','POST',{'token':token})
        self.assertEqual(status,200)
        self.assertEqual(self.call('/install/'+token)[0],400)
        self.assertEqual(self.call('/agent/config')[0],401)
        self.assertEqual(self.call('/agent/config',token=identity['token'])[0],200)
        status,payload,_=self.call('/agent/config',token=identity['token'])
        self.call('/agent/heartbeat','POST',{'running':False,'applied':payload['revision'],'version':'3.3.0'},token=identity['token'])
        snapshot=self.call('/api/state')[1]
        self.assertTrue(snapshot['nodes'][0]['online'])
        self.assertTrue(snapshot['nodes'][0]['synced'])
        self.assertEqual(self.call('/api/password','POST',{'old_password':'correct-horse-password','new_password':'new-correct-password'})[0],200)
        self.assertEqual(self.call('/api/state')[0],401)

    def test_authenticated_cached_asset_download_and_expired_credentials(self):
        import hashlib
        from unittest import mock
        from cache import CHECKSUMS
        from core import GOST_VERSION
        self.login()
        nid = self.store.save_node({'name':'入口','role':'entry','host':'127.0.0.1'})
        token = self.store.installation(nid)
        data = b'cached GOST archive'
        checksum = hashlib.sha256(data).hexdigest()
        path = self.store.directory / 'downloads' / ('gost_' + GOST_VERSION + '_linux_amd64.tar.gz')
        path.write_bytes(data)
        with mock.patch.dict(CHECKSUMS, {'amd64':checksum}):
            self.assertEqual(self.call('/downloads/gost/amd64')[0],401)
            status, archive, headers = self.call('/downloads/gost/amd64',token=token)
            self.assertEqual(status,200)
            self.assertEqual(archive,data)
            self.assertEqual(headers['Content-Length'],str(len(data)))
            self.assertEqual(self.call('/downloads/gost/not-supported',token=token)[0],400)
        self.store.enroll(token)
        self.assertEqual(self.call('/downloads/gost/amd64',token=token)[0],401)

    def test_rate_limit_and_security_headers(self):
        status,_,headers=self.call('/')
        self.assertEqual(status,200)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        for _ in range(10):
            self.assertEqual(self.call('/api/login','POST',{'password':'bad'})[0],401)
        self.assertEqual(self.call('/api/login','POST',{'password':'bad'})[0],429)


if __name__=='__main__':
    unittest.main()
