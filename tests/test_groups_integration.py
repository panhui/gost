"""Real GOST load balancing; distinct local ports emulate three separate exit hosts."""
import copy
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from agent import Agent, atomic_json
from core import Store
from test_gost_integration import BINARY, Echo, free_port, tcp_roundtrip


@unittest.skipUnless(BINARY, 'Set GOST_BINARY for real grouped tunnel tests')
class GroupIntegration(unittest.TestCase):
    def test_round_robin_tcp_udp_offline_failover_recovery_and_closed_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'panel','correct-horse-password')
            agents, echoes = [], []
            try:
                entry_group = store.save_group({'name':'entry','role':'entry'})
                backup_group = store.save_group({'name':'backup','role':'exit'})
                exit_group = store.save_group({'name':'primary','role':'exit','failover_id':backup_group})
                entry = store.save_node({'name':'entry','role':'entry','group_id':entry_group,'host':'127.0.0.1'})
                exits = [store.save_node({'name':prefix,'role':'exit','group_id':exit_group if i<2 else backup_group,
                    'host':'127.0.0.1'}) for i,prefix in enumerate(('A','B','C'))]
                tcp_echoes = [Echo(prefix=(p+':').encode()) for p in ('A','B','C')]
                udp_echoes = [Echo(udp=True,prefix=(p+':').encode()) for p in ('A','B','C')]
                echoes += tcp_echoes + udp_echoes
                listen, tunnel, udp_listen, udp_tunnel = [free_port() for _ in range(4)]
                rid = store.save_rule({'name':'tcp','entry_id':entry_group,'exit_id':exit_group,'listen_port':listen,
                    'tunnel_port':tunnel,'targets':f'127.0.0.1:{tcp_echoes[0].port}'})
                store.save_rule({'name':'udp','protocol':'udp','entry_id':entry_group,'exit_id':exit_group,
                    'listen_port':udp_listen,'tunnel_port':udp_tunnel,'targets':f'127.0.0.1:{udp_echoes[0].port}'})
                exit_payloads = []
                host_ports = {nid:{tunnel:free_port(),udp_tunnel:free_port()} for nid in exits}
                def ingress_config():
                    payload = copy.deepcopy(store.config(entry))
                    for chain in payload['config']['chains']:
                        for member in chain['hops'][0]['nodes']:
                            if member['name'].startswith('exit-'):
                                nid = member['name'][5:]
                                original_port = int(member['addr'].rsplit(':',1)[1])
                                member['addr'] = '127.0.0.1:'+str(host_ports[nid][original_port])
                    return payload
                for i,nid in enumerate(exits):
                    payload = copy.deepcopy(store.config(nid))
                    # A real host listens on all interfaces and has its own local
                    # backend. Use distinct ports and independent echo servers here to
                    # identify which of the actual TLS relay processes was used.
                    for service in payload['config']['services']:
                        port = int(service['addr'][1:])
                        service['addr'] = '127.0.0.1:'+str(host_ports[nid][port])
                        backend = tcp_echoes[i] if port==tunnel else udp_echoes[i]
                        service['forwarder']['nodes'][0]['addr'] = '127.0.0.1:'+str(backend.port)
                    folder = root/nid; folder.mkdir(); atomic_json(folder/'agent.json',{})
                    agent = Agent(folder,BINARY); agent.apply(payload); agents.append(agent)
                    exit_payloads.append(payload)
                    store.heartbeat(nid, {'running':True,'applied':store.config(nid)['revision']})
                folder=root/entry;folder.mkdir();atomic_json(folder/'agent.json',{})
                ingress=Agent(folder,BINARY);ingress.apply(ingress_config());agents.append(ingress)
                message=b'hello through TLS'
                self.assertEqual({tcp_roundtrip(listen) for _ in range(10)}, {b'A:'+message,b'B:'+message})
                def udp():
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                        sock.settimeout(3)
                        sock.sendto(b'udp',('127.0.0.1',udp_listen))
                        return sock.recvfrom(65535)[0]
                self.assertEqual({udp() for _ in range(8)}, {b'A:udp',b'B:udp'})
                # Data-path failure while management heartbeat is still fresh.
                agents[1].stop_process()
                self.assertEqual({tcp_roundtrip(listen) for _ in range(5)}, {b'A:'+message})
                # A heartbeat timeout removes the stopped member from config.
                with store.db:
                    store.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(time.time()-61,exits[1]))
                ingress.apply(ingress_config())
                route=store.config(entry)['config']['chains'][0]['hops'][0]['nodes']
                self.assertNotIn('exit-'+exits[1],{n['name'] for n in route})
                # All primaries fail: preloaded backup handles new connections.
                agents[0].stop_process()
                self.assertEqual({tcp_roundtrip(listen) for _ in range(5)}, {b'C:'+message})
                with store.db:
                    store.db.execute('UPDATE nodes SET last_seen=? WHERE id=?',(time.time()-61,exits[0]))
                ingress.apply(ingress_config())
                rule=next(r for r in store.snapshot()['rules'] if r['id']==rid)
                self.assertTrue(rule['failover_active'])
                self.assertEqual(rule['exit_nodes'],[exits[2]])
                self.assertEqual(udp(),b'C:udp')
                # A recovered primary is preferred again, using its original cert.
                agents[0].apply(exit_payloads[0])
                store.heartbeat(exits[0], {'running':True,'applied':store.config(exits[0])['revision']})
                ingress.apply(ingress_config())
                self.assertEqual({tcp_roundtrip(listen) for _ in range(4)}, {b'A:'+message})
                # All exits die before the next heartbeat. The real target stays
                # alive: a direct-route fallback would return A:hello and fail.
                agents[0].stop_process();agents[2].stop_process()
                for _ in range(6):
                    try: self.assertNotIn(tcp_roundtrip(listen),{b'A:'+message,b'B:'+message,b'C:'+message})
                    except OSError: pass
                with store.db:
                    store.db.execute('UPDATE nodes SET last_seen=0 WHERE role=\'exit\'')
                ingress.apply(ingress_config())
                self.assertEqual(store.config(entry)['config']['services'],[])
                with self.assertRaises(OSError): tcp_roundtrip(listen)
            finally:
                for agent in agents: agent.stop_process()
                for echo in echoes: echo.close()
                store.db.close()


if __name__ == '__main__':
    unittest.main()
