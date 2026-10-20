import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from device import (
    Device,
    DeviceInfo,
    InventoryConflictError,
    StaleInventoryError,
)


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "ztp"
    / "devices.schema.json"
)
ADMIN_INDEX_PATH = Path(__file__).with_name("static") / "index.html"


class DeviceTest(unittest.TestCase):
    def test_normalizes_cidr_to_plain_ipv4_address(self):
        device = Device(
            hostname="switch",
            mgmt_ip="192.0.2.10/24",
            serial="SERIAL",
        )

        self.assertEqual(device.mgmt_ip, "192.0.2.10")

    def test_preserves_plain_ipv4_address(self):
        device = Device(
            hostname="switch",
            mgmt_ip="192.0.2.10",
            serial="SERIAL",
        )

        self.assertEqual(device.mgmt_ip, "192.0.2.10")

    def test_inventory_schema_accepts_ip_but_rejects_cidr(self):
        with SCHEMA_PATH.open() as schema_file:
            schema = json.load(schema_file)
        pattern = schema["$defs"]["device"]["properties"]["mgmt_ip"]["oneOf"][1][
            "pattern"
        ]

        self.assertIsNotNone(re.fullmatch(pattern, "192.0.2.10"))
        self.assertIsNone(re.fullmatch(pattern, "192.0.2.10/24"))

    def test_rejects_hostname_that_cannot_be_used_in_api_path(self):
        with self.assertRaises(ValueError):
            Device(hostname="leaf/01", serial="SERIAL")

    def test_admin_component_registers_before_alpine_starts(self):
        html = ADMIN_INDEX_PATH.read_text(encoding="utf-8")

        self.assertLess(
            html.index('src="/admin/app.js"'),
            html.index('src="/admin/alpine.min.js"'),
        )


class DeviceInfoTest(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json")
        json.dump(
            {
                "switch-by-mac": {
                    "mgmt_ip": "192.0.2.10",
                    "serial": "SERIAL-MAC",
                    "eth0_mac": "AA:BB:CC:DD:EE:01",
                },
                "switch-by-serial": {
                    "mgmt_ip": "192.0.2.11",
                    "serial": "SERIAL-FALLBACK",
                    "eth0_mac": "aa:bb:cc:dd:ee:02",
                },
            },
            self.temp_file,
        )
        self.temp_file.flush()
        self.device_info = DeviceInfo(self.temp_file.name)

    def tearDown(self):
        self.temp_file.close()

    def test_finds_device_by_serial_when_mac_is_unknown(self):
        device = self.device_info.find(
            mgmt_mac="aa:bb:cc:dd:ee:ff",
            serial="SERIAL-FALLBACK",
        )

        self.assertEqual(device.hostname, "switch-by-serial")

    def test_prefers_mac_when_mac_and_serial_match_different_devices(self):
        device = self.device_info.find(
            mgmt_mac="AA:BB:CC:DD:EE:01",
            serial="SERIAL-FALLBACK",
        )

        self.assertEqual(device.hostname, "switch-by-mac")


class InventoryMutationTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.inventory_file = root / "devices.json"
        self.dhcp_file = root / "dhcp-hosts.conf"
        self.inventory_file.write_text(
            json.dumps(
                {
                    "leaf01": {
                        "hostname": "leaf01",
                        "mgmt_ip": "192.0.2.11",
                        "serial": "SERIAL-01",
                        "eth0_mac": "02:00:00:00:01:01",
                    },
                    "leaf02": {
                        "hostname": "leaf02",
                        "mgmt_ip": "192.0.2.12",
                        "serial": "SERIAL-02",
                        "eth0_mac": "02:00:00:00:01:02",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.dhcp_file.write_text("old contents\n", encoding="utf-8")
        self.device_info = DeviceInfo(self.inventory_file, self.dhcp_file)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_create_updates_inventory_dhcp_and_lookup(self):
        original_revision = self.device_info.revision
        device = Device(
            hostname="spine01",
            mgmt_ip="192.0.2.21",
            serial="SERIAL-21",
            eth0_mac="02:00:00:00:02:01",
        )

        self.device_info.create(device, original_revision)

        inventory = json.loads(self.inventory_file.read_text(encoding="utf-8"))
        self.assertEqual(inventory["spine01"]["hostname"], "spine01")
        self.assertIn(
            "02:00:00:00:02:01,192.0.2.21,spine01",
            self.dhcp_file.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            self.device_info.find(serial="SERIAL-21").hostname,
            "spine01",
        )
        self.assertNotEqual(self.device_info.revision, original_revision)

    def test_update_can_rename_a_device(self):
        self.device_info.update(
            "leaf01",
            Device(
                hostname="leaf01-renamed",
                mgmt_ip="192.0.2.11",
                serial="SERIAL-01",
                eth0_mac="02:00:00:00:01:01",
            ),
            self.device_info.revision,
        )

        inventory = json.loads(self.inventory_file.read_text(encoding="utf-8"))
        self.assertNotIn("leaf01", inventory)
        self.assertIn("leaf01-renamed", inventory)

    def test_rejects_duplicate_identifiers(self):
        with self.assertRaises(InventoryConflictError):
            self.device_info.create(
                Device(
                    hostname="duplicate",
                    serial="SERIAL-02",
                ),
                self.device_info.revision,
            )

    def test_rejects_a_stale_revision(self):
        stale_revision = self.device_info.revision
        inventory = json.loads(self.inventory_file.read_text(encoding="utf-8"))
        inventory["leaf01"]["serial"] = "EXTERNAL-CHANGE"
        self.inventory_file.write_text(json.dumps(inventory), encoding="utf-8")

        with self.assertRaises(StaleInventoryError):
            self.device_info.delete("leaf02", stale_revision)

    def test_serial_only_device_has_no_dhcp_reservation(self):
        self.device_info.create(
            Device(hostname="serial-only", serial="SERIAL-ONLY"),
            self.device_info.revision,
        )

        self.assertNotIn(
            "serial-only",
            self.dhcp_file.read_text(encoding="utf-8"),
        )

    def test_rolls_back_inventory_when_dhcp_write_fails(self):
        inventory_before = self.inventory_file.read_bytes()
        dhcp_before = self.dhcp_file.read_bytes()
        real_atomic_replace = DeviceInfo._atomic_replace
        failed_once = False

        def fail_dhcp_once(path, contents):
            nonlocal failed_once
            if Path(path) == self.dhcp_file and not failed_once:
                failed_once = True
                raise OSError("simulated DHCP write failure")
            return real_atomic_replace(path, contents)

        with patch.object(
            DeviceInfo,
            "_atomic_replace",
            side_effect=fail_dhcp_once,
        ):
            with self.assertRaises(OSError):
                self.device_info.create(
                    Device(hostname="spine01", serial="SERIAL-SPINE"),
                    self.device_info.revision,
                )

        self.assertEqual(self.inventory_file.read_bytes(), inventory_before)
        self.assertEqual(self.dhcp_file.read_bytes(), dhcp_before)
        self.assertIsNone(self.device_info.find(serial="SERIAL-SPINE"))


if __name__ == "__main__":
    unittest.main()
