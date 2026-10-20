import asyncio
import importlib
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
from app_logging import QuietPollingFilter, UTCFormatter, log_event
from device import DeviceInfo
from progress import ProgressEvent, ProgressStore


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        inventory = self.root / 'inventory.json'
        inventory.write_text(json.dumps({'leaf01': {'serial': 'SERIAL01', 'eth0_mac': '02:00:00:00:01:01'},
                                         'leaf02': {'serial': 'SERIAL02', 'eth0_mac': '02:00:00:00:01:02'}}))
        self.info = DeviceInfo(inventory)
        self.store = ProgressStore(self.root / 'progress.sqlite3')
        with patch('device.DeviceInfo', return_value=self.info):
            self.application = importlib.import_module('app')
        for name, value in (('device_info', self.info), ('progress_store', self.store)):
            patcher = patch.object(self.application, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_startup_warns_for_missing_switch_configs_and_still_serves_health(self):
        configs = self.root / 'configs'
        configs.mkdir()
        (configs / 'leaf01.yaml').write_text('---\n- set: {}\n')

        async def start():
            async with self.application.lifespan(self.application.app):
                self.assertEqual(await self.application.healthz(), {'status': 'ok'})

        with patch.object(self.application, 'switch_config_dir', configs):
            with self.assertLogs('ztp', level='WARNING') as captured:
                asyncio.run(start())
            self.assertEqual(len(captured.records), 1)
            line = captured.records[0].getMessage()
            self.assertIn('device=leaf02', line)
            self.assertIn('event=switch_config_missing', line)
            self.assertIn(str(configs / 'leaf02.yaml'), line)
            (configs / 'leaf02.yaml').write_text('---\n- set: {}\n')
            with self.assertNoLogs('ztp', level='WARNING'):
                asyncio.run(start())

    def test_startup_treats_directory_as_missing_config_and_warns_for_each_switch(self):
        configs = self.root / 'configs'
        (configs / 'leaf01.yaml').mkdir(parents=True)

        async def start():
            async with self.application.lifespan(self.application.app):
                pass

        with patch.object(self.application, 'switch_config_dir', configs):
            with self.assertLogs('ztp', level='WARNING') as captured:
                asyncio.run(start())
        self.assertEqual(len(captured.records), 2)
        for hostname, record in zip(('leaf01', 'leaf02'), captured.records):
            self.assertIn(f'device={hostname}', record.getMessage())
            self.assertIn('event=switch_config_missing', record.getMessage())

    def test_startup_warns_when_config_presence_cannot_be_checked(self):
        async def start():
            async with self.application.lifespan(self.application.app):
                self.assertEqual(await self.application.healthz(), {'status': 'ok'})

        with patch.object(Path, 'is_file', side_effect=PermissionError('permission denied')):
            with self.assertLogs('ztp', level='WARNING') as captured:
                asyncio.run(start())
        self.assertEqual(len(captured.records), 2)
        for record in captured.records:
            self.assertIn('event=switch_config_check_failed', record.getMessage())
            self.assertIn('permission denied', record.getMessage())

    def payload(self, serial='SERIAL01', **headers):
        values = {'user-agent': 'CumulusLinux-AutoProvision/1.0', 'cumulus-serial': serial,
                  'cumulus-version': '5.15.0', **headers}
        request = Request({'type': 'http', 'method': 'GET', 'path': '/cumulus-ztp',
                           'headers': [(key.encode(), value.encode()) for key, value in values.items()]})
        return asyncio.run(self.application.cumulus_ztp(request))

    def test_unmatched_device_logs_identity_headers_without_other_headers(self):
        with self.assertLogs('ztp', level='INFO') as captured:
            response = self.payload('UNKNOWN', **{'cumulus-mgmt-mac': '02:ff:ff:ff:ff:ff',
                'cumulus-arch': 'x86_64', 'cumulus-base-mac': '02:ff:ff:ff:ff:00',
                'cumulus-productname': 'SN5600', 'authorization': 'Bearer must-not-appear',
                'cookie': 'also-must-not-appear'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.WARNING)
        for field in ('device=-', 'attempt=-', 'event=device_unmatched', 'serial=UNKNOWN',
                      'mgmt_mac=02:ff:ff:ff:ff:ff', 'base_mac=02:ff:ff:ff:ff:00',
                      'arch=x86_64', 'model=SN5600', 'observed_os=5.15.0'):
            self.assertIn(field, record.getMessage())
        self.assertNotIn('must-not-appear', record.getMessage())

    def test_payloads_for_two_devices_have_distinct_full_attempt_ids(self):
        with self.assertLogs('ztp', level='INFO') as captured:
            self.assertEqual(self.payload('SERIAL01').status_code, 200)
            self.assertEqual(self.payload('SERIAL02').status_code, 200)
        self.assertEqual(len(captured.records), 2)
        for index, hostname in enumerate(('leaf01', 'leaf02')):
            attempt = self.store.latest(hostname)[0]['id']
            self.assertEqual(len(attempt), 32)
            line = captured.records[index].getMessage()
            for value in (f'device={hostname}', f'attempt={attempt}', 'event=payload_served', 'type=upgrade'):
                self.assertIn(value, line)
            self.assertNotIn('completed', line)

    def test_configuration_payload_is_identified(self):
        with self.assertLogs('ztp', level='INFO') as captured:
            response = self.payload(**{'cumulus-version': '5.16.0'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('type=configuration', captured.records[0].getMessage())

    def test_accepted_progress_includes_stage_result_and_failure_exit_code(self):
        attempt = self.store.payload('leaf01', '5.16.0', '5.15.0', True)
        with self.assertLogs('ztp', level='INFO') as captured:
            for result, code in (('started', 0), ('failed', 42)):
                event = ProgressEvent(attempt_id=attempt, serial='SERIAL01', stage='os_install', result=result, exit_code=code)
                self.assertEqual(self.application.report_progress(event), {'recorded': True})
        self.assertEqual([record.levelno for record in captured.records], [logging.INFO, logging.ERROR])
        line = captured.records[-1].getMessage()
        for value in ('device=leaf01', f'attempt={attempt}', 'event=progress', 'stage=os_install', 'result=failed', 'exit_code=42'):
            self.assertIn(value, line)

    def test_rejected_progress_has_reason_and_identity(self):
        attempt = self.store.payload('leaf01', '5.16.0', '5.15.0', True)
        for changes, reason, code in (({'serial': 'UNKNOWN'}, 'device_unmatched', 404),
                                      ({'attempt_id': 'f' * 32}, 'unknown_attempt', 404),
                                      ({'mgmt_mac': '02:00:00:00:01:02'}, 'identity_conflict', 409),
                                      ({'stage': 'config_apply'}, 'invalid_stage', 409)):
            event = ProgressEvent(**{'attempt_id': attempt, 'serial': 'SERIAL01', 'stage': 'os_install', 'result': 'started', **changes})
            with self.subTest(reason=reason), self.assertLogs('ztp', level='WARNING') as captured:
                with self.assertRaises(HTTPException) as error:
                    self.application.report_progress(event)
                self.assertEqual(error.exception.status_code, code)
            self.assertIn('event=progress_rejected', captured.records[0].getMessage())
            self.assertIn(f'reason={reason}', captured.records[0].getMessage())

    def test_storage_errors_keep_device_context_and_payload_delivery(self):
        with patch.object(self.store, 'payload', side_effect=OSError('disk full')):
            with self.assertLogs('ztp', level='INFO') as captured:
                response = self.payload()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"REPORT_PREFIX=''", response.body)
        self.assertIn('device=leaf01 attempt=- event=progress_storage_unavailable', captured.records[0].getMessage())
        self.assertIn('provisioning_continues=true', captured.records[0].getMessage())
        event = ProgressEvent(attempt_id='a' * 32, serial='SERIAL01', stage='os_install', result='started')
        with patch.object(self.store, 'report', side_effect=OSError('disk full')):
            with self.assertLogs('ztp', level='ERROR') as captured, self.assertRaises(HTTPException):
                self.application.report_progress(event)
        self.assertIn('device=leaf01 attempt=' + 'a' * 32, captured.records[0].getMessage())

    def test_untrusted_identity_values_stay_on_one_line(self):
        with self.assertLogs('ztp', level='WARNING') as captured:
            self.payload('UNKNOWN\nforged event=completed')
        line = captured.records[0].getMessage()
        self.assertNotIn('\n', line)
        self.assertIn('serial="UNKNOWN\\nforged event=completed"', line)


class LogFormattingTests(unittest.TestCase):
    def test_poll_filter_suppresses_only_successful_gets(self):
        filter = QuietPollingFilter()
        for path in ('/healthz', '/api/v1/provisioning?limit=1000', '/api/v1/provisioning/leaf01/events'):
            for method, status, keep in (('GET', 200, False), ('GET', 503, True), ('GET', 404, True), ('POST', 201, True)):
                record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                                           ('127.0.0.1', method, path, '1.1', status), None)
                self.assertEqual(filter.filter(record), keep, (method, path, status))
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, 'fallback', (), None)
        self.assertTrue(filter.filter(record))
        record.args = ('127.0.0.1', 'GET', '/api/v1/devices', '1.1', 200)
        self.assertTrue(filter.filter(record))

    def test_formatter_uses_utc_and_escapes_multiline_messages(self):
        formatter = UTCFormatter('%(asctime)sZ %(levelname)s %(message)s', '%Y-%m-%dT%H:%M:%S')
        record = logging.LogRecord('ztp', logging.WARNING, '', 0, 'line one\nline two', (), None)
        record.created = 0
        output = formatter.format(record)
        self.assertEqual(output, '1970-01-01T00:00:00Z WARNING line one\\nline two')


if __name__ == '__main__':
    unittest.main()
