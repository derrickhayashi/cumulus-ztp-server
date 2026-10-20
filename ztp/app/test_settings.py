import asyncio
import base64
import importlib
import json
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request
from device import Device, DeviceInfo, Network, ProvisioningSettings, StaleInventoryError
from progress import ProgressStore

KEY = 'ssh-ed25519 ' + base64.b64encode(struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'\x01' * 32).decode()


class JsonSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.inventory = self.root / 'inventory.json'
        self.data = {'settings': {'target_os': '5.18.3', 'authorized_keys': [KEY + ' example@host']},
                     'networks': {'management': {'subnet': '192.0.2.0/24', 'gateway': '192.0.2.1'}},
                     'devices': {'leaf': {'hostname': 'leaf', 'serial': 'LEAF', 'mgmt_ip': '192.0.2.11'}}}
        self.inventory.write_text(json.dumps(self.data))
        self.info = DeviceInfo(self.inventory, self.root / 'dhcp-hosts.conf', self.root / 'dnsmasq.conf')

    def test_settings_survive_device_and_network_mutations(self):
        self.info.create(Device(hostname='other', serial='OTHER'), self.info.revision)
        self.info.replace_networks({'management': Network(subnet='192.0.2.0/24', gateway='192.0.2.254')}, self.info.revision)
        self.assertEqual(json.loads(self.inventory.read_text())['settings'], self.data['settings'])
        self.assertEqual(DeviceInfo(self.inventory).settings, self.info.settings)
        old_revision = self.info.revision
        changed = json.loads(self.inventory.read_text())
        changed['settings']['target_os'] = '5.18.4'
        self.inventory.write_text(json.dumps(changed))
        with self.assertRaises(StaleInventoryError):
            self.info.delete('other', old_revision)

    def test_rejects_invalid_versions_private_keys_multiline_and_invalid_encoding(self):
        for settings in ({'target_os': '5.18.3;reboot'}, {'authorized_keys': ['-----BEGIN PRIVATE KEY-----']},
                         {'authorized_keys': [KEY + '\n' + KEY]}, {'authorized_keys': ['ssh-ed25519 broken']},
                         {'authorized_keys': ['ssh-rsa ' + KEY.split()[1]]}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ProvisioningSettings.model_validate(settings)
        self.assertEqual(ProvisioningSettings(authorized_keys=[]).authorized_keys, [])

    def payload(self, version):
        with patch('device.DeviceInfo', return_value=self.info):
            application = importlib.import_module('app')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/cumulus-ztp', 'headers': [
            (b'user-agent', b'CumulusLinux-AutoProvision/1.0'), (b'cumulus-serial', b'LEAF'),
            (b'cumulus-version', version.encode())]})
        with patch.object(application, 'device_info', self.info), patch.object(application, 'progress_store', ProgressStore(self.root / 'progress.sqlite3')), patch.object(application, 'cumulus_target_release', '9.9.9'):
            return asyncio.run(application.cumulus_ztp(request)).body.decode()

    def test_json_version_controls_upgrade_and_progress_target(self):
        upgrade = self.payload('5.16.0')
        self.assertIn('cumulus-linux-5.18.3-mlx-amd64.bin', upgrade)
        self.assertNotIn('9.9.9', upgrade)
        self.assertEqual(ProgressStore(self.root / 'progress.sqlite3').latest('leaf')[0]['target_os'], '5.18.3')

    def test_device_os_override_controls_upgrade_configuration_and_progress(self):
        self.info.update('leaf', Device(hostname='leaf', serial='LEAF', target_os='5.18.4'), self.info.revision)
        self.assertIn('cumulus-linux-5.18.4-mlx-amd64.bin', self.payload('5.18.3'))
        self.assertIn('nv config replace', self.payload('5.18.4'))
        self.assertEqual(ProgressStore(self.root / 'progress.sqlite3').latest('leaf')[0]['target_os'], '5.18.4')
        self.assertEqual(DeviceInfo(self.inventory).by_hostname['leaf'].target_os, '5.18.4')
        self.info.replace_networks(self.info.networks, self.info.revision)
        self.assertEqual(DeviceInfo(self.inventory).by_hostname['leaf'].target_os, '5.18.4')
        self.info.update('leaf', Device(hostname='leaf', serial='LEAF', target_os=None), self.info.revision)
        self.assertIn('cumulus-linux-5.18.3-mlx-amd64.bin', self.payload('5.18.4'))

    def test_device_os_override_validates_legacy_inventory_and_revision(self):
        for version in ('', '5.18', '5.18.3;reboot', '5.18.3\n'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                Device(hostname='leaf', serial='LEAF', target_os=version)
        data = self.data['devices']
        data['leaf']['target_os'] = '5.18.4'
        self.inventory.write_text(json.dumps(data))
        self.info.refresh()
        self.assertIn('cumulus-linux-5.18.4-mlx-amd64.bin', self.payload('5.18.3'))
        revision = self.info.revision
        data['leaf']['target_os'] = '5.18.5'
        self.inventory.write_text(json.dumps(data))
        with self.assertRaises(StaleInventoryError):
            self.info.delete('leaf', revision)

    def test_json_keys_are_embedded_and_shell_quoted(self):
        marker = self.root / 'must-not-exist'
        key = KEY + " comment ' $(touch " + str(marker) + ')'
        self.info.settings = ProvisioningSettings(target_os='5.18.3', authorized_keys=[key])
        payload = self.payload('5.18.3')
        self.assertNotIn('wget -q -O /home/cumulus/.ssh/authorized_keys', payload)
        subprocess.run(['bash', '-n'], input=payload, text=True, check=True)
        fragment = payload.split('mkdir -p /home/cumulus/.ssh\n', 1)[1].split('# Uncomment to unexpire', 1)[0]
        destination = self.root / 'authorized_keys'
        fragment = fragment.replace('/home/cumulus/.ssh/authorized_keys', shlex.quote(str(destination)))
        subprocess.run(['bash', '-c', fragment], check=True)
        self.assertEqual(destination.read_text(), key + '\n')
        self.assertFalse(marker.exists())
        self.info.settings = ProvisioningSettings(target_os='5.18.3', authorized_keys=[])
        empty_payload = self.payload('5.18.3')
        self.assertIn("printf '%s' '' > /home/cumulus/.ssh/authorized_keys", empty_payload)

    def test_settings_only_inventory_and_legacy_fallback(self):
        del self.data['networks']
        self.inventory.write_text(json.dumps(self.data))
        self.info.refresh()
        self.assertEqual(self.info.settings.target_os, '5.18.3')
        self.inventory.write_text(json.dumps(self.data['devices']))
        self.info.refresh()
        self.assertIsNone(self.info.settings.target_os)
        payload = self.payload('9.9.9')
        self.assertIn('wget -q -O /home/cumulus/.ssh/authorized_keys', payload)

    def test_password_quotes_and_shell_metacharacters_are_preserved(self):
        import os
        with patch('device.DeviceInfo', return_value=self.info):
            application = importlib.import_module('app')
        fake_bin = self.root / 'password-bin'
        fake_bin.mkdir()
        capture = self.root / 'password-record'
        executable = fake_bin / 'chpasswd'
        executable.write_text('#!/bin/sh\ncat > "$PASSWORD_CAPTURE"\n')
        executable.chmod(0o755)
        marker = self.root / 'must-not-run'
        passwords = ["it's-a-password", 'spaces and "double quotes"',
                     "'$(touch " + str(marker) + ")`touch " + str(marker) + "`$HOME\\", '-n %s café']
        for password in passwords:
            with self.subTest(password=password), patch.object(application, 'cumulus_password', password):
                payload = self.payload('5.18.3')
                subprocess.run(['bash', '-n'], input=payload, text=True, check=True)
                fragment = payload.split('passwd -x 99999 cumulus\n', 1)[1].split('# Uncomment to make', 1)[0]
                subprocess.run(['bash', '-c', fragment], check=True, env={**os.environ,
                    'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'], 'PASSWORD_CAPTURE': str(capture)})
                self.assertEqual(capture.read_text(), 'cumulus:' + password + '\n')
                self.assertFalse(marker.exists())

    def test_multiline_password_is_rejected_without_exposing_its_value(self):
        import os
        import sys
        for password in ('secret-first-line\nsecond-line', 'secret-first-line\rsecond-line'):
            env = {**os.environ, 'CUMULUS_PASSWORD': password, 'PYTHONPATH': str(Path(__file__).parent)}
            result = subprocess.run([sys.executable, '-c', 'import app'], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('CUMULUS_PASSWORD must not contain line breaks', result.stderr)
            self.assertNotIn('secret-first-line', result.stderr)

    def test_removed_environment_settings_do_not_affect_application_defaults(self):
        import os
        import sys
        self.data["settings"] = {}
        self.data["networks"] = {}
        self.inventory.write_text(json.dumps(self.data))
        env = {**os.environ, "DEVICE_CONFIG_FILE": str(self.inventory),
               "DHCP_HOSTS_FILE": str(self.root / "dhcp-hosts.conf"),
               "DNSMASQ_CONFIG_FILE": str(self.root / "dnsmasq.conf"),
               "CUMULUS_TARGET_RELEASE": "9.9.9", "DHCP_RANGE": "203.0.113.0,static,255.255.255.0",
               "SWITCH_MGMT_DEFAULT_GW": "203.0.113.1"}
        code = "import app; app.device_info.reconcile_dhcp(); print(app.cumulus_target_release)"
        result = subprocess.run([sys.executable, "-c", code], env={**env, "PYTHONPATH": str(Path(__file__).parent)},
                                check=True, text=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), "5.16.0")
        config = (self.root / "dnsmasq.conf").read_text()
        self.assertIn("dhcp-range=192.168.200.0,static,255.255.255.0", config)
        self.assertIn("dhcp-option=option:router,192.168.200.1", config)
        self.assertNotIn("203.0.113", config)

    def test_downloader_uses_json_then_argument_and_builtin_default(self):
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\nif [ "$1" = "--output" ]; then output="$2"; shift 2; else shift; fi\ndone\nprintf installer > "$output"\n')
        curl.chmod(0o755)
        import os
        env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
               'DEVICE_CONFIG_FILE': str(self.inventory), 'CUMULUS_IMAGE_DIR': str(self.root / 'web'),
               'CUMULUS_TARGET_RELEASE': '9.9.9'}
        script = Path(__file__).resolve().parents[1] / 'fetch-cumulus-os'
        for argument, expected in (([], '5.18.3'), (['5.18.4'], '5.18.4')):
            subprocess.run([str(script), *argument], env=env, check=True, stdout=subprocess.PIPE)
            self.assertTrue((self.root / 'web' / f'cumulus-linux-{expected}-mlx-amd64.bin').exists())
        self.inventory.write_text(json.dumps(self.data['devices']))
        subprocess.run([str(script)], env=env, check=True, stdout=subprocess.PIPE)
        self.assertTrue((self.root / 'web' / 'cumulus-linux-5.16.0-mlx-amd64.bin').exists())


if __name__ == '__main__':
    unittest.main()
