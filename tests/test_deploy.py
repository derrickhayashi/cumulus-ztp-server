import os
from pathlib import Path
import subprocess
import tempfile
import unittest


DEPLOY = Path(__file__).resolve().parents[1] / 'scripts/deploy.sh'


class DeployTests(unittest.TestCase):
    def test_fetch_precedes_activation_and_failure_preserves_running_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / 'docker'
            docker.write_text('''#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$DEPLOY_LOG"
case "$*" in
  *fetch-cumulus-os*) exit "${FETCH_EXIT-0}" ;;
  *'--profile tools pull') exit "${PULL_EXIT-0}" ;;
esac
''')
            docker.chmod(0o755)
            log = root / 'calls'
            environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                           'DEPLOY_LOG': str(log)}
            for options in ([], ['--build']):
                with self.subTest(options=options):
                    log.unlink(missing_ok=True)
                    subprocess.run([str(DEPLOY), *options], env=environment, check=True)
                    calls = log.read_text().splitlines()
                    self.assertIn('config --quiet', calls[0])
                    if options:
                        self.assertEqual(len(calls), 3)
                        self.assertTrue(all('-f compose.build.yaml' in call for call in calls))
                        self.assertIn('run --build --rm fetch-cumulus-os', calls[1])
                        self.assertIn('up -d --build --wait', calls[2])
                    else:
                        self.assertEqual(len(calls), 4)
                        self.assertTrue(all('-f compose.yaml' in call for call in calls))
                        self.assertIn('--profile tools pull', calls[1])
                        self.assertIn('run --pull never --rm fetch-cumulus-os', calls[2])
                        self.assertIn('up -d --no-build --pull never --wait', calls[3])
                    log.unlink()
                    failed = subprocess.run([str(DEPLOY), *options], env={**environment, 'FETCH_EXIT': '22'})
                    self.assertEqual(failed.returncode, 22)
                    self.assertFalse(any('up -d' in call for call in log.read_text().splitlines()))
            log.unlink()
            failed = subprocess.run([str(DEPLOY)], env={**environment, 'PULL_EXIT': '23'})
            self.assertEqual(failed.returncode, 23)
            self.assertEqual(len(log.read_text().splitlines()), 2)
            log.unlink()
            invalid = subprocess.run([str(DEPLOY), '--unknown'], env=environment, capture_output=True)
            self.assertEqual(invalid.returncode, 2)
            self.assertFalse(log.exists())
