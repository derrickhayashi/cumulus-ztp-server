"""Exercise durable progress and rendered scripts without touching a switch."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError
from device import DeviceInfo
from progress import ProgressEvent, ProgressStore

SCRIPTS = Path(__file__).with_name("scripts")
MAC = "aa:bb:cc:dd:ee:01"


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        inventory = self.root / "devices.json"
        inventory.write_text(json.dumps({
            "leaf": {"eth0_mac": MAC, "serial": "SERIAL"},
            "other": {"serial": "OTHER"},
        }))
        self.inventory = DeviceInfo(inventory)
        self.store = ProgressStore(self.root / "state" / "progress.sqlite3")
        with patch("device.DeviceInfo", return_value=self.inventory):
            self.application = importlib.import_module("app")
        self.patch_inventory = patch.object(self.application, "device_info", self.inventory)
        self.patch_store = patch.object(self.application, "progress_store", self.store)
        self.patch_inventory.start()
        self.patch_store.start()
        self.addCleanup(self.patch_inventory.stop)
        self.addCleanup(self.patch_store.stop)

    def event(self, attempt, stage="os_install", result="started", code=0, **identity):
        return ProgressEvent(attempt_id=attempt, stage=stage, result=result,
                             exit_code=code, **(identity or {"mgmt_mac": MAC}))

    def request(self, method, path, body=None, headers=()):
        async def run():
            messages = []
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode() if body else b""}
            async def send(message):
                messages.append(message)
            await self.application.app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path,
                "query_string": b"", "root_path": "",
                "headers": [(b"content-type", b"application/json"), *headers],
                "client": ("127.0.0.1", 1234), "server": ("test", 80),
            }, receive, send)
            return messages[0]["status"], b"".join(m.get("body", b"") for m in messages[1:])
        return asyncio.run(run())

    def test_reboot_continuity_and_storage_reopen(self):
        attempt = self.store.payload("leaf", "5.18.3", "5.12.0", True)
        self.store.report("leaf", self.event(attempt, result="succeeded"))
        reopened = ProgressStore(self.store.path)
        self.assertEqual(reopened.payload("leaf", "5.18.3", "5.18.3", False), attempt)
        reopened.report("leaf", self.event(attempt, "provisioning", "succeeded"))
        status = reopened.latest("leaf")[0]
        self.assertEqual((status["stage"], status["observed_os"]), ("provisioning", "5.18.3"))
        self.assertEqual(len(reopened.history("leaf")), 4)

    def test_new_attempt_is_not_overwritten_by_old_or_lower_stage_report(self):
        old = self.store.payload("leaf", "5.18.3", "5.12.0", True)
        new = self.store.payload("leaf", "5.18.3", "5.12.0", True)
        self.store.report("leaf", self.event(new, "reboot", "requested"))
        self.store.report("leaf", self.event(new, result="succeeded"))
        self.store.report("leaf", self.event(old, result="failed", code=42))
        status = self.store.latest("leaf")[0]
        self.assertEqual((status["id"], status["stage"], status["result"]),
                         (new, "reboot", "requested"))
        self.assertEqual(len(self.store.history("leaf", limit=2)), 2)

    def test_api_identity_validation_status_and_history(self):
        attempt = self.store.payload("leaf", "5.18.3", "5.12.0", True)
        data = self.event(attempt, result="failed", code=42).model_dump()
        self.assertEqual(self.request("POST", "/api/v1/provisioning/events", data)[0], 201)
        code, body = self.request("GET", "/api/v1/provisioning/leaf")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["exit_code"], 42)
        self.assertEqual(self.request("GET", "/api/v1/provisioning")[0], 200)
        code, body = self.request("GET", "/api/v1/provisioning/leaf/events")
        self.assertEqual(len(json.loads(body)["events"]), 2)
        for changes, expected in (({"serial": "OTHER"}, 409),
                                  ({"mgmt_mac": "00:00:00:00:00:00"}, 404),
                                  ({"attempt_id": "f" * 32}, 404),
                                  ({"stage": "config_save"}, 409),
                                  ({"exit_code": 0}, 422),
                                  ({"password": "must-not-be-accepted"}, 422)):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("POST", "/api/v1/provisioning/events",
                                              {**data, **changes})[0], expected)
        serial_only = self.event(attempt, serial="SERIAL").model_dump()
        self.assertEqual(self.request("POST", "/api/v1/provisioning/events", serial_only)[0], 201)

    def test_missing_reports_never_create_success_or_failure(self):
        self.store.payload("leaf", "5.18.3", "5.12.0", True)
        status = self.store.latest("leaf")[0]
        self.assertEqual((status["stage"], status["result"]), ("upgrade_payload", "requested"))

    def test_storage_outage_does_not_block_payload(self):
        with patch.object(self.store, "payload", side_effect=OSError("disk full")):
            status, payload = self.request("GET", "/cumulus-ztp", headers=[
                (b"user-agent", b"CumulusLinux-AutoProvision/1.0"),
                (b"cumulus-mgmt-mac", MAC.encode()),
            ])
        self.assertEqual(status, 200)
        self.assertIn(b"REPORT_PREFIX=''", payload)
        with patch.object(self.store, "latest", side_effect=OSError("disk full")):
            self.assertEqual(self.request("GET", "/api/v1/provisioning")[0], 503)

    def test_invalid_event_fields(self):
        for changes in ({"mgmt_mac": None}, {"stage": "arbitrary"},
                        {"result": "failed"}, {"exit_code": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ProgressEvent(**{"attempt_id": "a" * 32, "mgmt_mac": MAC,
                                 "stage": "os_install", "result": "started", **changes})


class ScriptReportingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.renderer = Environment(loader=FileSystemLoader(SCRIPTS))
        self.renderer.filters["shellquote"] = shlex.quote
        self.identity = {"attempt_id": "a" * 32, "mgmt_mac": MAC, "serial": "SERIAL"}

    def render(self, name):
        return self.renderer.get_template(name).render(
            IMAGE_SERVER_HOSTNAME="127.0.0.1", NGINX_PORT="81",
            CUMULUS_TARGET_RELEASE="5.18.3", DEVICE_HOSTNAME="leaf",
            CUMULUS_PASSWORD="test-placeholder",
            REPORT_PREFIX_SHELL=shlex.quote(json.dumps(self.identity)[:-1]),
        )

    def executable(self, name, body):
        path = self.root / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
        return path

    def test_reporting_failure_preserves_install_and_reboot_results(self):
        self.executable("ping", "exit 0\n")
        self.executable("sleep", "exit 0\n")
        self.executable("curl", 'printf "%s\\n" "$*" >> "$REPORT_LOG"\nexit 7\n')
        installer = self.executable("installer", 'exit "$INSTALL_STATUS"\n')
        self.executable("reboot", 'touch "$REBOOT_MARKER"\nexit "$REBOOT_STATUS"\n')
        payload = self.render("upgrade-os.sh.j2").replace("/usr/cumulus/bin/onie-install", str(installer))
        for install, reboot, expected in ((0, 0, 0), (42, 0, 42), (0, 27, 27)):
            with self.subTest(install=install, reboot=reboot):
                marker = self.root / "rebooted"
                marker.unlink(missing_ok=True)
                result = subprocess.run(["bash"], input=payload, text=True, capture_output=True,
                    env={**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                         "INSTALL_STATUS": str(install), "REBOOT_STATUS": str(reboot),
                         "REBOOT_MARKER": str(marker), "REPORT_LOG": str(self.root / "reports")})
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(marker.exists(), install == 0)
        reports = (self.root / "reports").read_text()
        self.assertIn('"exit_code":42', reports)
        self.assertIn('"exit_code":27', reports)
        self.assertIn("--max-time 2 --retry 0", reports)

    def test_configuration_stops_at_failure_and_reports_completion_only_after_save(self):
        for name in ("ping", "sleep", "passwd", "chpasswd", "wget"):
            self.executable(name, "exit 0\n")
        self.executable("curl", '''case "$*" in
  */api/v1/provisioning/events*)
    printf '%s\\n' "$*" >> "$REPORT_LOG"
    exit 7;;
  *) while [ "$#" -gt 0 ]; do
       if [ "$1" = -o ]; then shift; echo 'config' > "$1"; fi
       shift
     done;;
esac
''')
        self.executable("nv", '''echo "$*" >> "$NV_LOG"
if [ "$2" = "$FAIL_STEP" ]; then exit 31; fi
exit 0
''')
        payload = self.render("configure-switch.sh.j2")
        payload = payload.replace("/home/cumulus", str(self.root / "home"))
        payload = payload.replace("/etc/sudoers.d/10_cumulus", str(self.root / "sudoers"))
        payload = payload.replace("/tmp/$DEVICE_HOSTNAME.yaml", str(self.root / "config.yaml"))
        for step in ("replace", "apply", "save", "none"):
            with self.subTest(step=step):
                reports = self.root / "reports"
                reports.unlink(missing_ok=True)
                result = subprocess.run(["bash"], input=payload, text=True, capture_output=True,
                    env={**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                         "FAIL_STEP": step, "REPORT_LOG": str(reports), "NV_LOG": str(self.root / "nv-log")})
                self.assertEqual(result.returncode, 0 if step == "none" else 31, result.stderr)
                events = reports.read_text()
                self.assertEqual('"stage":"provisioning"' in events, step == "none")
                if step != "none":
                    self.assertIn('"result":"failed","exit_code":31', events)

    def test_identity_is_json_encoded_and_shell_quoted(self):
        marker = self.root / "injected"
        self.identity["serial"] = "serial'\";$(touch " + str(marker) + ");\nnext"
        self.executable("curl", '''while [ "$#" -gt 0 ]; do
if [ "$1" = --data ]; then shift; printf '%s' "$1" > "$REPORT_LOG"; fi
shift
done
''')
        helper = self.renderer.get_template("report-progress.sh.j2").render(
            REPORT_PREFIX_SHELL=shlex.quote(json.dumps(self.identity)[:-1]))
        subprocess.run(["bash"], input=helper + '\nreport_progress os_install started\n',
                       text=True, check=True, env={**os.environ,
                       "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                       "REPORT_LOG": str(self.root / "json")})
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads((self.root / "json").read_text())["serial"], self.identity["serial"])

    def test_ping_retries_before_success_or_failure(self):
        self.executable("ping", '''echo attempt >> "$PING_LOG"
if [ "$PING_FAIL_ALWAYS" = 1 ] || [ "$(wc -l < "$PING_LOG")" -eq 1 ]; then
    exit 1
fi
exit 0
''')
        self.executable("sleep", "exit 0\n")
        self.executable("curl", "exit 7\n")
        for name, boundary in (("upgrade-os.sh.j2", "report_progress reachability started"),
                               ("configure-switch.sh.j2", "report_progress prepare started")):
            for always_fail, count, expected in (("0", 2, 0), ("1", 30, 1)):
                with self.subTest(template=name, always_fail=always_fail):
                    log = self.root / "ping-log"
                    log.unlink(missing_ok=True)
                    prefix = self.render(name).split(boundary, 1)[0]
                    result = subprocess.run(["bash"], input=prefix + '\nping_until_reachable "$IMAGE_SERVER_HOSTNAME"\n',
                        text=True, capture_output=True, env={**os.environ,
                        "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                        "PING_LOG": str(log), "PING_FAIL_ALWAYS": always_fail})
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(len(log.read_text().splitlines()), count)

    def test_unresponsive_server_has_bounded_wait(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        stop = threading.Event()
        def accept():
            connection, _ = listener.accept()
            with connection:
                stop.wait(4)
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        helper = self.renderer.get_template("report-progress.sh.j2").render(
            REPORT_PREFIX_SHELL=shlex.quote(json.dumps(self.identity)[:-1]))
        payload = helper + f'\nREPORT_URL=http://127.0.0.1:{listener.getsockname()[1]}/events\nreport_progress os_install started\necho continued\n'
        started = time.monotonic()
        try:
            result = subprocess.run(["bash"], input=payload, text=True,
                                    capture_output=True, timeout=4)
        finally:
            stop.set()
            thread.join(timeout=1)
        self.assertEqual(result.returncode, 0)
        self.assertIn("continued", result.stdout)
        self.assertLess(time.monotonic() - started, 3.5)


if __name__ == "__main__":
    unittest.main()
