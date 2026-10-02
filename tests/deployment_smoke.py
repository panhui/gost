"""CI-only: verify the real one-click installer and unprivileged systemd agent."""
import json
import os
import ssl
import subprocess
import tempfile
import time
import urllib.request
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
    node, _ = call('/api/nodes', {'name': 'CI exit', 'role': 'exit', 'host': '127.0.0.1'})
    generated, _ = call('/api/nodes/' + node['id'] + '/install', {})
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / 'install-agent.sh'
        script.write_text(generated['script'])
        os.chmod(script, 0o600)
        subprocess.run(['bash', str(script)], check=True, timeout=240)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state, _ = call('/api/state')
        if state['nodes'][0]['online'] and state['nodes'][0]['synced']:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Installed systemd agent did not come online')
    for service, expected_user in [('gost-panel', 'gost-panel'), ('gost-agent', 'gost-agent')]:
        subprocess.run(['systemctl', 'is-active', '--quiet', service], check=True)
        actual_user = subprocess.check_output(['systemctl', 'show', '-p', 'User', '--value', service], text=True).strip()
        if actual_user != expected_user:
            raise RuntimeError('Service does not use its unprivileged user')
    print('Real panel and node installers passed; systemd agent is online and synced.')


if __name__ == '__main__':
    main()
