"""CI-only: verify the real one-click installer and unprivileged systemd agent."""
import json
import os
import ssl
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path


def main():
    url = 'https://127.0.0.1:18443'
    context = ssl.create_default_context(cafile='/var/lib/gost-panel/panel-cert.pem')
    cookie, csrf = '', ''

    def call(path, data=None):
        request = urllib.request.Request(url + path, headers={
            'Content-Type': 'application/json', 'Cookie': cookie, 'X-CSRF-Token': csrf},
            data=json.dumps(data).encode() if data is not None else None)
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            return json.loads(response.read()), response.headers

    result, headers = call('/api/login', {'password': os.environ['SMOKE_PASSWORD']})
    cookie, csrf = headers['Set-Cookie'].split(';')[0], result['csrf']
    group, _ = call('/api/groups', {'name': 'CI exits', 'role': 'exit', 'offline_after':60})
    generated, _ = call('/api/groups/' + group['id'] + '/install', {})
    # Cached official releases must exist before installation; the node script
    # itself must use only the panel download endpoint, without GitHub access.
    for arch in ['amd64', 'arm64']:
        if not Path('/var/lib/gost-panel/downloads/gost_3.3.0_linux_' + arch + '.tar.gz').is_file():
            raise RuntimeError('Panel installer did not pre-cache ' + arch)
    if 'github.com' in generated['script'] or '/downloads/gost/' not in generated['script']:
        raise RuntimeError('Node installer must download via the panel')
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / 'install-agent.sh'
        script.write_text(generated['script'])
        os.chmod(script, 0o600)
        subprocess.run(['bash', str(script)], check=True, timeout=240)
        # Reusing the same group command on this machine must keep its identity.
        first_identity = json.loads(Path('/var/lib/gost-agent/agent.json').read_text())['node_id']
        subprocess.run(['bash', str(script)], check=True, timeout=240)
        second_identity = json.loads(Path('/var/lib/gost-agent/agent.json').read_text())['node_id']
        if first_identity != second_identity:
            raise RuntimeError('Group reinstall duplicated the device')
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state, _ = call('/api/state')
        if state['nodes'][0]['online'] and state['nodes'][0]['synced']:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Installed systemd agent did not come online')
    if len(state['nodes']) != 1 or state['nodes'][0]['group_id'] != group['id']:
        raise RuntimeError('Installed device did not join its group')
    node = state['nodes'][0]
    for service, expected_user in [('gost-panel', 'gost-panel'), ('gost-agent', 'gost-agent')]:
        subprocess.run(['systemctl', 'is-active', '--quiet', service], check=True)
        actual_user = subprocess.check_output(['systemctl', 'show', '-p', 'User', '--value', service], text=True).strip()
        if actual_user != expected_user:
            raise RuntimeError('Service does not use its unprivileged user')
    # Exercise the real privileged one-click updater and data preservation.
    entry_group, _ = call('/api/groups', {'name':'CI direct entry','role':'entry'})
    direct, _ = call('/api/rules', {'name':'CI direct rule','mode':'direct','entry_id':entry_group['id'],
        'exit_id':None,'listen_port':18080,'protocol':'udp','targets':'127.0.0.1:19080'})
    before_cert = Path('/var/lib/gost-panel/panel-cert.pem').read_bytes()
    result, _ = call('/api/upgrade', {})
    if result['state'] != 'queued':
        raise RuntimeError('Upgrade was not queued')
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            result, _ = call('/api/upgrade')
            if result['state'] == 'failed':
                raise RuntimeError(result['message'])
            if result['state'] == 'success':
                break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    else:
        raise RuntimeError('One-click upgrade did not complete')
    state, _ = call('/api/state')
    if state['groups'][0]['id'] != group['id'] or state['nodes'][0]['id'] != node['id'] or Path('/var/lib/gost-panel/panel-cert.pem').read_bytes() != before_cert:
        raise RuntimeError('Upgrade did not preserve node or certificate')
    rule = next((r for r in state['rules'] if r['id']==direct['id']), None)
    if not rule or rule['mode']!='direct' or rule['exit_id'] is not None or rule['tunnel_port'] is not None:
        raise RuntimeError('Upgrade did not preserve direct forwarding rule')
    if not Path('/var/lib/gost-panel-upgrade/panel-before-upgrade.db').is_file():
        raise RuntimeError('Upgrade did not back up the database')
    print('Real panel/node installers and one-click upgrade passed; data and certificate preserved.')


if __name__ == '__main__':
    main()
