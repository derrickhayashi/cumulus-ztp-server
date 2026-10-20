"""Run with DNSMASQ_TEST_IMAGE=<built image> python3 -m unittest discover -s dnsmasq/tests -v.

All DHCP activity stays inside a container with --network none. No switch or
host management network is contacted.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get('DNSMASQ_TEST_IMAGE'), 'requires an explicitly supplied Docker test image')
class DnsmasqReloadTest(unittest.TestCase):
    def docker(self, *args):
        result = subprocess.run(['docker', *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
        if result.returncode:
            self.fail(result.stdout)
        return result.stdout

    def state(self):
        return json.loads(self.docker('inspect', self.name))[0]

    def eventually(self, predicate, description, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.3)
        self.fail(description + '\n' + self.docker('logs', self.name))

    @staticmethod
    def replace(path, contents):
        temporary = path.with_suffix('.tmp')
        temporary.write_text(contents)
        temporary.replace(path)

    def test_first_start_runtime_failure_does_not_create_working_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            (root / 'dnsmasq').mkdir()
            (root / 'state').mkdir()
            (root / 'dnsmasq/dhcp-hosts.conf').write_text('02:00:00:00:01:01,192.0.2.11,leaf01\n')
            (root / 'dnsmasq/dnsmasq.conf').write_text(
                'no-hosts\nno-resolv\nbind-interfaces\nport=0\n'
                'dhcp-hostsfile=/deployment/dnsmasq/dhcp-hosts.conf\n'
                'dhcp-range=192.0.2.0,static,255.255.255.0\n'
                'interface=ztp-no-such-interface\n')
            name = 'ztp-dnsmasq-cold-failure-' + uuid.uuid4().hex[:12]
            try:
                result = subprocess.run(['docker', 'run', '--name', name, '--network', 'none', '--init',
                    '--cap-add', 'NET_ADMIN', '-v', f'{root}:/deployment:ro',
                    '-v', f'{root / "state"}:/var/lib/misc', os.environ['DNSMASQ_TEST_IMAGE']],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('No previous working dnsmasq configuration is available', result.stdout)
                self.assertFalse((root / 'state/dnsmasq-working.conf').exists())
            finally:
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)

    def test_reload_reject_restart_and_persistent_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            config_dir = root / 'dnsmasq'
            config_dir.mkdir(mode=0o755)
            state_dir = root / 'state'
            state_dir.mkdir(mode=0o755)
            hosts = config_dir / 'dhcp-hosts.conf'
            config = config_dir / 'dnsmasq.conf'
            hosts.write_text('02:00:00:00:01:01,192.0.2.11,leaf01\n')
            valid = ('no-hosts\nno-resolv\nlog-dhcp\nbind-interfaces\nport=0\n'
                     'dhcp-hostsfile=/deployment/dnsmasq/dhcp-hosts.conf\n'
                     'dhcp-leasefile=/var/lib/misc/dnsmasq.leases\n'
                     'dhcp-range=set:racka,192.0.2.0,static,255.255.255.0\n'
                     'dhcp-option=tag:racka,option:router,192.0.2.1\n'
                     'dhcp-range=set:rackb,198.51.100.0,static,255.255.255.0\n'
                     'dhcp-option=tag:rackb,option:router,198.51.100.1\n')
            config.write_text(valid)
            self.name = 'ztp-dnsmasq-test-' + uuid.uuid4().hex[:12]
            try:
                self.docker('run', '-d', '--name', self.name, '--network', 'none', '--init',
                            '--cap-add', 'NET_ADMIN', '--restart', 'unless-stopped',
                            '-v', f'{root}:/deployment:ro', '-v', f'{state_dir}:/var/lib/misc',
                            os.environ['DNSMASQ_TEST_IMAGE'], 'dnsmasq', '--log-facility=-',
                            '--conf-file=/var/lib/misc/dnsmasq-active.conf')
                self.eventually(lambda: 'started, version' in self.docker('logs', self.name), 'dnsmasq did not start')
                started = self.state()['State']['StartedAt']
                # Keep the first run up beyond Docker's restart-policy startup window.
                time.sleep(11)
                self.replace(hosts, '02:00:00:00:01:02,192.0.2.12,leaf02\n')
                self.eventually(lambda: 'DHCP reservations changed; reloading' in self.docker('logs', self.name), 'reservation edit was not reloaded')
                self.assertEqual(self.state()['State']['StartedAt'], started)
                self.assertEqual(self.state()['RestartCount'], 0)
                self.replace(config, valid)
                time.sleep(6)
                self.assertEqual(self.state()['RestartCount'], 0, 'identical content caused a restart')
                self.replace(config, 'this-is-not-a-dnsmasq-setting\n')
                self.eventually(lambda: 'Rejected dnsmasq configuration' in self.docker('logs', self.name), 'invalid configuration was not rejected')
                self.assertEqual(self.state()['State']['StartedAt'], started)
                self.assertEqual((state_dir / 'dnsmasq-active.conf').read_text(), valid)
                # A valid lease survives the container restart even without a connected subnet.
                lease = '0 02:00:00:00:01:02 192.0.2.12 leaf02 *\n'
                self.docker('exec', self.name, 'sh', '-c', "printf '%s\\n' '0 02:00:00:00:01:02 192.0.2.12 leaf02 *' > /var/lib/misc/dnsmasq.leases")
                updated = valid.replace('192.0.2.1\n', '192.0.2.254\n')
                self.replace(config, updated)
                self.eventually(lambda: self.state()['RestartCount'] == 1 and self.state()['State']['Running'], 'valid network change did not restart container')
                self.eventually(lambda: self.docker('logs', self.name).count('started, version') == 2, 'dnsmasq did not start after restart')
                self.assertEqual((state_dir / 'dnsmasq-active.conf').read_text(), updated)
                self.assertEqual((state_dir / 'dnsmasq.leases').read_text(), lease)
                self.eventually(lambda: (state_dir / 'dnsmasq-working.conf').exists(), 'working configuration was not recorded')
                self.assertEqual((state_dir / 'dnsmasq-working.conf').read_text(), updated)
                # This config passes syntax checking but fails actual interface binding.
                runtime_invalid = updated + 'interface=ztp-no-such-interface\n'
                self.replace(config, runtime_invalid)
                self.docker('exec', self.name, 'dnsmasq', '--test', '--conf-file=/deployment/dnsmasq/dnsmasq.conf')
                self.eventually(lambda: 'Restored working dnsmasq configuration' in self.docker('logs', self.name), 'runtime startup failure did not roll back')
                self.assertEqual(self.state()['RestartCount'], 2)
                self.assertEqual((state_dir / 'dnsmasq-active.conf').read_text(), updated)
                self.assertEqual((state_dir / 'dnsmasq-working.conf').read_text(), updated)
                self.assertEqual((state_dir / 'dnsmasq-rejected.conf').read_text(), runtime_invalid)
                time.sleep(6)
                self.assertEqual(self.state()['RestartCount'], 2, 'rejected runtime configuration caused a restart loop')
                # A corrected configuration must be accepted after rollback.
                corrected = updated.replace('192.0.2.254\n', '192.0.2.253\n')
                self.replace(config, corrected)
                self.eventually(lambda: self.state()['RestartCount'] == 3 and (state_dir / 'dnsmasq-working.conf').read_text() == corrected, 'corrected configuration was not accepted')
                updated = corrected
                # A daemon crash must also exit the wrapper and trigger Docker recovery.
                self.docker('exec', self.name, 'sh', '-c', 'kill -KILL "$(cat /var/lib/misc/dnsmasq.pid)"')
                self.eventually(lambda: self.state()['RestartCount'] == 4 and self.state()['State']['Running'], 'daemon crash did not restart container')
                self.eventually(lambda: 'dnsmasq exited unexpectedly' in self.docker('logs', self.name), 'daemon crash was not reported')
                self.eventually(lambda: (state_dir / 'dnsmasq.pid').exists(), 'daemon did not recover after crash')
                starts_before_manual_restart = self.docker('logs', self.name).count('dnsmasq startup confirmed')
                # Invalid candidate on a later startup must use the last validated copy.
                self.replace(config, 'invalid-setting\n')
                self.docker('restart', self.name)
                self.eventually(lambda: self.docker('logs', self.name).count('dnsmasq startup confirmed') > starts_before_manual_restart, 'last validated configuration was not reused')
                self.assertEqual((state_dir / 'dnsmasq-active.conf').read_text(), updated)
                print('Verified reservation reload, no-op edits, syntax rejection, runtime rollback, corrected-config recovery, daemon-crash recovery, persistent leases, and startup fallback.')
            finally:
                subprocess.run(['docker', 'rm', '-f', self.name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    unittest.main()
