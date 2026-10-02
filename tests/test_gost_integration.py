"""Run against the official GOST binary; no mocked forwarding path."""
import copy
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path

from agent import Agent, atomic_json
from core import certificate, digest
import test_panel

BINARY = os.getenv('GOST_BINARY')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class Echo:
    def __init__(self, udp=False, prefix=b''):
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1',0))
        self.port=self.sock.getsockname()[1]
        self.sock.settimeout(.2)
        self.stopping=False
        self.prefix=prefix
        if not udp: self.sock.listen()
        self.thread=threading.Thread(target=self.run,args=(udp,),daemon=True)
        self.thread.start()

    def run(self, udp):
        while not self.stopping:
            try:
                if udp:
                    data,peer=self.sock.recvfrom(65535)
                    self.sock.sendto(self.prefix+data,peer)
                else:
                    conn,_=self.sock.accept()
                    conn.settimeout(2)
                    with conn:
                        data=conn.recv(65535)
                        if data: conn.sendall(self.prefix+data)
            except (socket.timeout,OSError):
                pass

    def close(self):
        self.stopping=True
        self.thread.join(timeout=2)
        self.sock.close()


def tcp_roundtrip(port,payload=b'hello through TLS'):
    with socket.create_connection(('127.0.0.1',port),timeout=3) as conn:
        conn.sendall(payload)
        return conn.recv(65535)


@unittest.skipUnless(BINARY,'Set GOST_BINARY to run real TCP/UDP tunnel integration tests')
class GOSTIntegration(test_panel.APITests):
    # Reuse only the TLS fixture, not its test methods.
    test_authentication_csrf_and_node_isolation = None
    test_rate_limit_and_security_headers = None

    def setUp(self):
        super().setUp()
        self.children=[]
        self.echoes=[]
        self.log=(Path(self.temp.name)/'agent.log').open('wb')

    def tearDown(self):
        for child in self.children:
            child.terminate()
        for child in self.children:
            try: child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
        for echo in self.echoes: echo.close()
        self.log.close()
        super().tearDown()

    def wait_for(self,fn,timeout=35):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if fn(): return
            time.sleep(.25)
        self.fail('Timed out; agent logs:\n'+(Path(self.temp.name)/'agent.log').read_text(errors='replace')[-4000:])

    def start_agent(self,nid):
        folder=Path(self.temp.name)/nid
        folder.mkdir()
        (folder/'panel-ca.pem').write_text(self.server.certificate)
        identity=self.store.enroll(self.store.installation(nid))
        settings={'panel_url':self.url,'ca_file':str(folder/'panel-ca.pem'),**identity}
        atomic_json(folder/'agent.json',settings)
        process=subprocess.Popen([os.sys.executable,str(Path(__file__).resolve().parents[1]/'agent.py'),'--directory',str(folder),'--binary',str(Path(BINARY).resolve())],stdout=self.log,stderr=self.log)
        self.children.append(process)
        return folder,process

    def test_agents_forward_tcp_udp_multitarget_disable_and_panel_outage(self):
        entry=self.store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
        exit_node=self.store.save_node({'name':'exit','role':'exit','host':'127.0.0.1'})
        tcp1,tcp2,udp=Echo(prefix=b'A:'),Echo(prefix=b'B:'),Echo(udp=True)
        self.echoes += [tcp1,tcp2,udp]
        tcp_port,udp_port,tunnel_tcp,tunnel_udp=[free_port() for _ in range(4)]
        data={'name':'tcp','entry_id':entry,'exit_id':exit_node,'protocol':'tcp','listen_port':tcp_port,
              'tunnel_port':tunnel_tcp,'targets':[f'127.0.0.1:{tcp1.port}',f'127.0.0.1:{tcp2.port}'],'enabled':True}
        rid=self.store.save_rule(data)
        self.store.save_rule({**data,'name':'udp','protocol':'udp','listen_port':udp_port,'tunnel_port':tunnel_udp,'targets':[f'127.0.0.1:{udp.port}']})
        _,exit_process=self.start_agent(exit_node)
        entry_folder,entry_process=self.start_agent(entry)
        self.wait_for(lambda:all(n['online'] and n['synced'] and n['running'] for n in self.store.snapshot()['nodes']))
        replies={tcp_roundtrip(tcp_port) for _ in range(6)}
        self.assertEqual(replies,{b'A:hello through TLS',b'B:hello through TLS'})
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.settimeout(4)
            payload=b'UDP payload '+b'x'*5000
            sock.sendto(payload,('127.0.0.1',udp_port))
            self.assertEqual(sock.recvfrom(65535)[0],payload)
        self.store.save_rule({**data,'enabled':False},rid)
        self.wait_for(lambda:all(n['synced'] for n in self.store.snapshot()['nodes']))
        with self.assertRaises(OSError): tcp_roundtrip(tcp_port)
        self.store.save_rule(data,rid)
        self.wait_for(lambda:all(n['synced'] and n['running'] for n in self.store.snapshot()['nodes']))
        self.assertTrue(tcp_roundtrip(tcp_port).endswith(b'hello through TLS'))
        # Shut down the control plane; data plane and persisted configuration survive.
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        time.sleep(1)
        self.assertTrue(tcp_roundtrip(tcp_port).endswith(b'hello through TLS'))
        # Restart just the entry agent with an unavailable panel, using persisted state.
        entry_process.terminate();entry_process.wait(timeout=8)
        restarted=subprocess.Popen([os.sys.executable,str(Path(__file__).resolve().parents[1]/'agent.py'),'--directory',str(entry_folder),'--binary',str(Path(BINARY).resolve())],stdout=self.log,stderr=self.log)
        self.children.append(restarted)
        time.sleep(2)
        self.assertTrue(tcp_roundtrip(tcp_port).endswith(b'hello through TLS'))
        self.assertIsNone(exit_process.poll())

    def test_authentication_certificate_validation_and_failed_apply_rollback(self):
        entry=self.store.save_node({'name':'entry','role':'entry','host':'127.0.0.1'})
        exit_node=self.store.save_node({'name':'exit','role':'exit','host':'127.0.0.1'})
        echo=Echo();self.echoes.append(echo)
        listen,tunnel=free_port(),free_port()
        self.store.save_rule({'name':'test','entry_id':entry,'exit_id':exit_node,'listen_port':listen,
                             'tunnel_port':tunnel,'target_host':'127.0.0.1','target_port':echo.port})
        agents=[]
        try:
            for nid in [exit_node,entry]:
                folder=Path(self.temp.name)/('direct-'+nid);folder.mkdir()
                atomic_json(folder/'agent.json',{})
                agent=Agent(folder,BINARY);agent.apply(self.store.config(nid));agents.append(agent)
            self.assertEqual(tcp_roundtrip(listen),b'hello through TLS')
            good=copy.deepcopy(self.store.config(entry))
            def revision(payload):
                payload['revision']=digest(json.dumps({k:v for k,v in payload.items() if k!='revision'},sort_keys=True))
                return payload
            bad=copy.deepcopy(good)
            bad['config']['chains'][0]['hops'][0]['nodes'][0]['connector']['auth']['password']='incorrect'
            agents[1].apply(revision(bad))
            try: self.assertNotEqual(tcp_roundtrip(listen),b'hello through TLS')
            except OSError: pass
            bad=copy.deepcopy(good)
            bad['config']['chains'][0]['hops'][0]['nodes'][0]['dialer']['tls']['serverName']='wrong.example'
            agents[1].apply(revision(bad))
            try: self.assertNotEqual(tcp_roundtrip(listen),b'hello through TLS')
            except OSError: pass
            agents[1].apply(good)
            # A port conflict must leave the previous, working generation active.
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1',0));occupied.listen()
                bad=copy.deepcopy(good)
                bad['config']['services'][0]['addr']='127.0.0.1:'+str(occupied.getsockname()[1])
                with self.assertRaises(RuntimeError): agents[1].apply(revision(bad))
                self.assertEqual(agents[1].applied,good['revision'])
                self.assertEqual(tcp_roundtrip(listen),b'hello through TLS')
        finally:
            for agent in agents:agent.stop_process()


if __name__=='__main__':
    unittest.main()
