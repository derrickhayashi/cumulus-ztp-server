import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from device import Device, DeviceInfo, Network, InventoryConflictError, StaleInventoryError


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.inventory = root / "inventory.json"
        self.hosts = root / "dhcp-hosts.conf"
        self.config = root / "dnsmasq.conf"
        self.inventory.write_text(json.dumps({"leaf01": {
            "hostname": "leaf01", "mgmt_ip": "192.0.2.11", "eth0_mac": "02:00:00:00:01:01",
        }}))
        self.info = DeviceInfo(self.inventory, self.hosts, self.config,
                              ztp_url="http://192.0.2.1:81/cumulus-ztp")
        self.networks = {"rack-a": Network(subnet="192.0.2.0/24", gateway="192.0.2.1"),
                         "rack-b": Network(subnet="198.51.100.0/24", gateway="198.51.100.1")}

    def test_generates_two_ranges_and_preserves_networks_on_device_save(self):
        self.info.replace_networks(self.networks, self.info.revision)
        self.info.create(Device(hostname="leaf02", network="rack-b", mgmt_ip="198.51.100.11",
                                eth0_mac="02:00:00:00:02:01"), self.info.revision)
        config = self.config.read_text()
        for value in ("dhcp-range=set:network0,192.0.2.0,static,255.255.255.0",
                      "dhcp-range=set:network1,198.51.100.0,static,255.255.255.0",
                      "dhcp-option=tag:network1,option:router,198.51.100.1",
                      "dhcp-option=239,http://192.0.2.1:81/cumulus-ztp"):
            self.assertIn(value, config)
        reopened = DeviceInfo(self.inventory)
        self.assertEqual(reopened.networks, self.networks)
        self.assertEqual(reopened.by_hostname['leaf02'].network, 'rack-b')
        self.assertIn('02:00:00:00:02:01,198.51.100.11,leaf02', self.hosts.read_text())

    def test_rejects_invalid_gateway_overlap_and_wrong_device_subnet(self):
        for subnet, gateway in (("192.0.2.0/24", "198.51.100.1"), ("192.0.2.0/24", "192.0.2.255")):
            with self.assertRaises(ValueError):
                Network(subnet=subnet, gateway=gateway)
        with self.assertRaises(InventoryConflictError):
            self.info.replace_networks({**self.networks, 'overlap': Network(subnet='192.0.2.0/25', gateway='192.0.2.2')}, self.info.revision)
        self.info.replace_networks(self.networks, self.info.revision)
        for network, address in (('missing', '192.0.2.12'), ('rack-a', '198.51.100.12'),
                                 ('rack-a', '192.0.2.1'), ('rack-a', '192.0.2.255'),
                                 (None, '203.0.113.12')):
            with self.subTest(network=network, address=address), self.assertRaises(InventoryConflictError):
                self.info.create(Device(hostname='bad', serial='BAD', network=network, mgmt_ip=address), self.info.revision)

    def test_network_revision_and_deletion_protection(self):
        old_revision = self.info.revision
        self.info.replace_networks(self.networks, old_revision)
        with self.assertRaises(StaleInventoryError):
            self.info.delete('leaf01', old_revision)
        self.info.update('leaf01', Device(hostname='leaf01', serial='SERIAL', network='rack-a'), self.info.revision)
        with self.assertRaises(InventoryConflictError):
            self.info.replace_networks({}, self.info.revision)

    def test_legacy_defaults_and_no_rewrites_on_reservation_changes(self):
        self.info.reconcile_dhcp()
        self.assertIn('dhcp-range=192.168.200.0,static,255.255.255.0', self.config.read_text())
        self.info.replace_networks(self.networks, self.info.revision)
        before = self.config.stat().st_mtime_ns
        self.info.create(Device(hostname='serial', serial='SERIAL'), self.info.revision)
        self.info.reconcile_dhcp()
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_rolls_back_all_files_on_config_write_failure(self):
        self.info.reconcile_dhcp()
        before = {path: path.read_bytes() for path in (self.inventory, self.hosts, self.config)}
        real_replace = DeviceInfo._atomic_replace
        def fail_config(path, contents):
            if path == self.config:
                raise OSError('config write failure')
            real_replace(path, contents)
        with patch.object(DeviceInfo, '_atomic_replace', side_effect=fail_config):
            with self.assertRaises(OSError):
                self.info.replace_networks(self.networks, self.info.revision)
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertFalse(self.info.networks)

    def test_api_without_auth_retains_revision_validation_and_network_persistence(self):
        with patch('device.DeviceInfo', return_value=self.info):
            application = importlib.import_module('app')
        async def request(method, path, body=None, revision=None):
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(body).encode() if body is not None else b''}
            async def send(message):
                messages.append(message)
            headers = [(b'content-type', b'application/json')]
            if revision is not None:
                headers.append((b'if-match', f'"{revision}"'.encode()))
            await application.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'method': method, 'scheme': 'http', 'path': path, 'query_string': b'', 'root_path': '',
                'headers': headers, 'client': ('127.0.0.1', 1), 'server': ('test', 80)}, receive, send)
            return messages[0]['status'], json.loads(b''.join(m.get('body', b'') for m in messages[1:]))
        def call(*args, **kwargs):
            return asyncio.run(request(*args, **kwargs))
        with patch.object(application, 'device_info', self.info):
            body = {name: network.model_dump() for name, network in self.networks.items()}
            self.assertEqual(call('PUT', '/api/v1/networks', body)[0], 428)
            self.assertEqual(call('PUT', '/api/v1/networks', body, revision='stale')[0], 412)
            self.assertEqual(call('PUT', '/api/v1/networks', body, revision=self.info.revision)[0], 200)
            code, listing = call('GET', '/api/v1/devices')
            self.assertEqual(code, 200)
            self.assertEqual(listing['networks'], body)
            self.assertEqual(call('PUT', '/api/v1/networks', {'bad': {'subnet': '192.0.2.0/24', 'gateway': '203.0.113.1'}}, revision=self.info.revision)[0], 422)
            # A later device mutation must retain the network map.
            self.assertEqual(call('POST', '/api/v1/devices', {'hostname': 'serial', 'serial': 'SERIAL'}, revision=self.info.revision)[0], 201)
            self.assertEqual(DeviceInfo(self.inventory).networks, self.networks)


if __name__ == '__main__':
    unittest.main()
