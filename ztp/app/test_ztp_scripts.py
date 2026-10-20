"""Check rendered payload addressing without executing provisioning commands."""

from pathlib import Path
import asyncio
import importlib
import os
import subprocess
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from device import ProvisioningSettings
from progress import ProgressStore


SCRIPTS = Path(__file__).resolve().with_name("scripts")


class PayloadAddressTests(unittest.TestCase):
    def test_endpoint_renders_packaged_scripts_from_another_directory(self):
        with patch("device.DeviceInfo", autospec=True):
            application = importlib.import_module("app")
        for version, expected in (
            ("5.12.0", "cumulus-linux-5.18.3-mlx-amd64.bin"),
            ("5.18.3", "configs/example-leaf.yaml"),
        ):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                request = Request({
                    "type": "http",
                    "method": "GET",
                    "path": "/cumulus-ztp",
                    "headers": [
                        (b"user-agent", b"CumulusLinux-AutoProvision/1.0"),
                        (b"cumulus-version", version.encode()),
                    ],
                })
                original_directory = os.getcwd()
                try:
                    os.chdir(directory)
                    with (
                        patch.object(application.device_info, "find", return_value=
                                     SimpleNamespace(hostname="example-leaf", eth0_mac=None, target_os=None,
                                                     serial="EXAMPLE-SERIAL")),
                        patch.object(application, "progress_store",
                                     ProgressStore(Path(directory) / "progress.sqlite3")),
                        patch.object(application, "cumulus_target_release", "5.18.3"),
                        patch.object(application.device_info, "settings", ProvisioningSettings(), create=True),
                        patch.object(application, "ztp_server_hostname", "2001:db8::10"),
                        patch.object(application, "nginx_port", "81"),
                        patch.object(application, "cumulus_password", "test-placeholder"),
                    ):
                        response = asyncio.run(application.cumulus_ztp(request))
                finally:
                    os.chdir(original_directory)
                self.assertEqual(response.status_code, 200)
                payload = response.body.decode()
                self.assertIn(expected, payload)
                self.assertIn('IMAGE_SERVER_HOSTNAME="2001:db8::10"', payload)
                self.assertIn('IMAGE_SERVER_PORT="81"', payload)
                subprocess.run(["bash", "-n"], input=payload, text=True, check=True)

    def test_upgrade_result_controls_reboot(self):
        environment = Environment(loader=FileSystemLoader(SCRIPTS))
        environment.filters["shellquote"] = shlex.quote
        payload = environment.get_template("upgrade-os.sh.j2").render(
            IMAGE_SERVER_HOSTNAME="10.158.166.82",
            NGINX_PORT="81",
            CUMULUS_TARGET_RELEASE="5.18.3",
        )
        self.assertIn("# CUMULUS-AUTOPROVISIONING-CASCADE", payload)
        for install_status, reboot_status, expected_status in (
            (0, 0, 0), (42, 0, 42), (0, 27, 27),
        ):
            with self.subTest(install=install_status, reboot=reboot_status):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    commands = {
                        "ping": "exit 0\n",
                        "sleep": "exit 0\n",
                        "installer": 'printf "%s\\n" "$@" > "$INSTALL_ARGS"\n'
                                     'exit "$INSTALL_STATUS"\n',
                        "reboot": 'touch "$REBOOT_MARKER"\nexit "$REBOOT_STATUS"\n',
                    }
                    for name, body in commands.items():
                        command = root / name
                        command.write_text("#!/bin/bash\n" + body)
                        command.chmod(0o755)
                    safe_payload = payload.replace(
                        "/usr/cumulus/bin/onie-install", str(root / "installer")
                    )
                    result = subprocess.run(
                        ["bash"], input=safe_payload, text=True, capture_output=True,
                        env={
                            **os.environ,
                            "PATH": directory + os.pathsep + os.environ["PATH"],
                            "INSTALL_ARGS": str(root / "args"),
                            "INSTALL_STATUS": str(install_status),
                            "REBOOT_MARKER": str(root / "reboot-requested"),
                            "REBOOT_STATUS": str(reboot_status),
                        },
                    )
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual(
                        (root / "reboot-requested").exists(), install_status == 0
                    )
                    self.assertEqual(
                        (root / "args").read_text().splitlines(),
                        ["-fa", "-i", "http://10.158.166.82:81/"
                         "cumulus-linux-5.18.3-mlx-amd64.bin"],
                    )
                    if install_status:
                        self.assertIn("OS installation failed", result.stderr)

    def test_ping_host_and_http_urls(self):
        environment = Environment(loader=FileSystemLoader(SCRIPTS))
        environment.filters["shellquote"] = shlex.quote
        for template in ("configure-switch.sh.j2", "upgrade-os.sh.j2"):
            for host, authority_host in (
                ("10.158.166.82", "10.158.166.82"),
                ("ztp.example.test", "ztp.example.test"),
                ("2001:db8::10", "[2001:db8::10]"),
            ):
                for port in ("80", "81"):
                    with self.subTest(template=template, host=host, port=port):
                        payload = environment.get_template(template).render(
                            IMAGE_SERVER_HOSTNAME=host,
                            NGINX_PORT=port,
                            DEVICE_HOSTNAME="example-leaf",
                            CUMULUS_TARGET_RELEASE="5.18.0",
                            CUMULUS_PASSWORD="test-placeholder",
                        )
                        subprocess.run(
                            ["bash", "-n"], input=payload, text=True, check=True
                        )
                        # Execute only initial variable assignments and URL
                        # construction, before any provisioning functions.
                        prefix = payload.split("function ", 1)[0]
                        variable = (
                            "CONFIG_URL" if template == "configure-switch.sh.j2"
                            else "IMAGE_SERVER"
                        )
                        output = subprocess.check_output(
                            ["bash", "-c", prefix + '\nprintf "%s\\n%s\\n" '
                             '"$IMAGE_SERVER_HOSTNAME" "${' + variable + '}"'],
                            text=True,
                        ).splitlines()
                        self.assertEqual(output[0], host)
                        suffix = (
                            "configs/example-leaf.yaml"
                            if template == "configure-switch.sh.j2"
                            else "cumulus-linux-5.18.0-mlx-amd64.bin"
                        )
                        self.assertEqual(
                            output[1], f"http://{authority_host}:{port}/{suffix}"
                        )


if __name__ == "__main__":
    unittest.main()
