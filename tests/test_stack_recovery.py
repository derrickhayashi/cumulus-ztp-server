"""Optional Docker recovery tests; internal network only, no published ports.

ZTP_RECOVERY_APP_IMAGE=<app> ZTP_RECOVERY_NGINX_IMAGE=<nginx> \\
    python3 -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('ZTP_RECOVERY_APP_IMAGE') and os.environ.get('ZTP_RECOVERY_NGINX_IMAGE'),
                     'requires explicitly supplied app and nginx Docker test images')
class StackRecoveryTest(unittest.TestCase):
    def docker(self, *args):
        result = subprocess.run(['docker', *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=45)
        if result.returncode:
            self.fail(result.stdout)
        return result.stdout

    def state(self, name):
        return json.loads(self.docker('inspect', name))[0]

    def healthy(self, name):
        state = self.state(name)
        return state['State']['Running'] and state['State'].get('Health', {}).get('Status') == 'healthy'

    def eventually(self, predicate, description):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.3)
        logs = '\n'.join(self.docker('logs', name) for name in (self.app, self.nginx))
        self.fail(description + '\n' + logs)

    def test_services_restart_and_keep_progress(self):
        services = json.loads(self.docker('compose', '-f', str(ROOT / 'compose.build.yaml'),
                                         'config', '--format', 'json'))['services']
        prefix = 'ztp-recovery-' + uuid.uuid4().hex[:10]
        self.app, self.nginx, network = prefix + '-app', prefix + '-nginx', prefix + '-network'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            deployment = root / 'deployment'
            for folder in ('ztp', 'dnsmasq', 'web/configs'):
                (deployment / folder).mkdir(parents=True, exist_ok=True)
            (root / 'progress').mkdir()
            (deployment / 'ztp/devices.json').write_text(json.dumps({
                'settings': {'target_os': '5.16.0', 'authorized_keys': []},
                'networks': {'mgmt': {'subnet': '192.0.2.0/24', 'gateway': '192.0.2.1'}},
                'devices': {'leaf01': {'hostname': 'leaf01', 'serial': 'SERIAL01', 'mgmt_ip': '192.0.2.11'}}}))
            self.docker('network', 'create', '--internal', network)
            try:
                for name, key, image in ((self.app, 'ztp', os.environ['ZTP_RECOVERY_APP_IMAGE']),
                                          (self.nginx, 'nginx', os.environ['ZTP_RECOVERY_NGINX_IMAGE'])):
                    service = services[key]
                    check = service['healthcheck']['test']
                    health = shlex.join(check[1:]) if check[0] == 'CMD' else check[1]
                    arguments = ['run', '-d', '--name', name, '--network', network,
                                 '--network-alias', key, '--restart', service['restart'],
                                 '--health-cmd', health, '--health-interval', '1s',
                                 '--health-timeout', '2s', '--health-retries', '5']
                    if service.get('init'):
                        arguments += ['--init']
                    if key == 'ztp':
                        arguments += ['-v', f'{deployment}:/deployment', '-v', f'{root / "progress"}:/var/lib/ztp',
                                      '-e', 'ZTP_SERVER_ADDRESS=192.0.2.1', '-e', 'CUMULUS_PASSWORD=test-password']
                    else:
                        arguments += ['-v', f'{deployment / "web"}:/deployment/web:ro']
                    self.docker(*arguments, image)
                self.eventually(lambda: self.healthy(self.app) and self.healthy(self.nginx), 'stack did not become healthy')
                # Establish a persisted attempt through the actual HTTP handler.
                self.docker('exec', self.app, 'curl', '--fail', '--silent',
                            '-H', 'User-Agent: CumulusLinux-AutoProvision/1.0',
                            '-H', 'CUMULUS-SERIAL: SERIAL01', '-H', 'CUMULUS-VERSION: 5.16.0',
                            'http://127.0.0.1:8080/cumulus-ztp')
                def status():
                    return json.loads(self.docker('exec', self.app, 'curl', '--fail', '--silent',
                                                 'http://127.0.0.1:8080/api/v1/provisioning/leaf01'))
                attempt = status()['id']
                time.sleep(11)
                for name in (self.app, self.nginx):
                    before = self.state(name)['RestartCount']
                    # Docker's manual kill/stop path can suppress restart policies.
                    # Crash the service child beneath Docker's init process instead.
                    pid = self.docker('exec', name, 'cat', '/proc/1/task/1/children').split()[0]
                    self.assertTrue(pid.isdigit())
                    crashed = subprocess.run(['docker', 'exec', name, 'sh', '-c', f'kill -KILL {pid}'],
                                             text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
                    self.assertIn(crashed.returncode, (0, 137), crashed.stdout)
                    self.eventually(lambda: self.state(name)['RestartCount'] > before and self.healthy(name),
                                    f'{name} did not automatically restart and recover')
                self.eventually(lambda: self.healthy(self.app) and self.healthy(self.nginx), 'stack health did not recover')
                self.assertEqual(status()['id'], attempt)
                self.docker('exec', self.app, 'curl', '--fail', '--silent', 'http://nginx/healthz')
                print('Verified app and nginx crash recovery, HTTP health checks, and persisted provisioning progress on an internal-only Docker network.')
            finally:
                for name in (self.nginx, self.app):
                    subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    unittest.main()
