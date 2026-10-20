import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


FETCH_SCRIPT = Path(__file__).parents[1] / "fetch-cumulus-os"


class FetchCumulusOsTest(unittest.TestCase):
    def test_downloaded_installer_is_world_readable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            test_root = Path(temporary_directory)
            output_directory = test_root / "web"
            bin_directory = test_root / "bin"
            bin_directory.mkdir()

            fake_curl = bin_directory / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    set -eu

                    while [ "$#" -gt 0 ]; do
                        if [ "$1" = "--output" ]; then
                            output_file="$2"
                            shift 2
                        else
                            shift
                        fi
                    done

                    printf 'installer contents' > "$output_file"
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_directory}{os.pathsep}{environment['PATH']}"
            environment["CUMULUS_IMAGE_DIR"] = str(output_directory)

            subprocess.run(
                [FETCH_SCRIPT, "5.16.6"],
                check=True,
                env=environment,
                preexec_fn=lambda: os.umask(0o077),
            )

            installer = output_directory / "cumulus-linux-5.16.6-mlx-amd64.bin"
            self.assertEqual(installer.read_text(encoding="utf-8"), "installer contents")
            self.assertEqual(stat.S_IMODE(installer.stat().st_mode), 0o644)


class MultiVersionFetchTests(unittest.TestCase):
    def setUp(self):
        import json
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.inventory = self.root / 'inventory.json'
        self.data = {'settings': {'target_os': '5.18.3'}, 'devices': {
            'leaf1': {'hostname': 'leaf1', 'serial': 'L1'},
            'leaf2': {'hostname': 'leaf2', 'serial': 'L2', 'target_os': '5.18.4'},
            'leaf3': {'hostname': 'leaf3', 'serial': 'L3', 'target_os': '5.18.4'},
            'leaf4': {'hostname': 'leaf4', 'serial': 'L4', 'target_os': None}}}
        self.inventory.write_text(json.dumps(self.data))
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text('''#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FETCH_LOG"
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output" ]; then output="$2"; shift 2; else shift; fi
done
printf '%s' "${FETCH_CONTENTS-installer}" > "$output"
exit "${FETCH_EXIT-0}"
''')
        curl.chmod(0o755)
        self.log = self.root / 'fetch.log'
        self.web = self.root / 'web'
        self.env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
                    'DEVICE_CONFIG_FILE': str(self.inventory), 'CUMULUS_IMAGE_DIR': str(self.web),
                    'FETCH_LOG': str(self.log)}
        self.env.pop('CUMULUS_IMAGE_DOWNLOADER_BASE_URL', None)

    def run_fetch(self, *arguments):
        return subprocess.run([str(FETCH_SCRIPT), *arguments], env=self.env, capture_output=True, text=True)

    def test_fetches_unique_effective_versions_and_reuses_cached_files(self):
        self.assertEqual(self.run_fetch().returncode, 0)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)
        self.assertEqual({p.name for p in self.web.iterdir()}, {
            'cumulus-linux-5.18.3-mlx-amd64.bin', 'cumulus-linux-5.18.4-mlx-amd64.bin'})
        cached = self.web / 'cumulus-linux-5.18.3-mlx-amd64.bin'
        cached.write_text('approved artifact')
        self.assertEqual(self.run_fetch().returncode, 0)
        self.assertEqual(cached.read_text(), 'approved artifact')
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_download_url_uses_default_or_custom_base(self):
        default_base = ('https://networkingdownloads.nvidia.com/custhelp/'
                        'Non_Monetized_Products/SwitchesandGateways/CumulusLinuxforMellanoxSwitches')
        for base, expected in ((None, default_base), ('', default_base),
                               ('https://images.example.com/cumulus', 'https://images.example.com/cumulus'),
                               ('https://images.example.com/cumulus/', 'https://images.example.com/cumulus')):
            with self.subTest(base=base):
                if base is None:
                    self.env.pop('CUMULUS_IMAGE_DOWNLOADER_BASE_URL', None)
                else:
                    self.env['CUMULUS_IMAGE_DOWNLOADER_BASE_URL'] = base
                installer = self.web / 'cumulus-linux-5.18.5-mlx-amd64.bin'
                installer.unlink(missing_ok=True)
                self.assertEqual(self.run_fetch('5.18.5').returncode, 0)
                self.assertEqual(self.log.read_text().splitlines()[-1].split()[-1],
                                 expected + '/cumulus-linux-5.18.5-mlx-amd64.bin')
    def test_rejects_bad_override_before_any_download_and_explicit_version_is_independent(self):
        import json
        self.data['devices']['leaf2']['target_os'] = '5.18.4;reboot'
        self.inventory.write_text(json.dumps(self.data))
        self.assertNotEqual(self.run_fetch().returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.run_fetch('5.18.5').returncode, 0)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn('5.18.5', self.log.read_text())

    def test_failed_or_empty_download_cleans_partial_files_and_is_retryable(self):
        for environment in ({'FETCH_EXIT': '22'}, {'FETCH_CONTENTS': ''}):
            with self.subTest(environment=environment):
                self.env.update(environment)
                self.assertNotEqual(self.run_fetch('5.18.3').returncode, 0)
                self.assertFalse(list(self.web.iterdir()))
                for key in environment:
                    self.env.pop(key)
        self.assertEqual(self.run_fetch('5.18.3').returncode, 0)

    def test_does_not_fetch_unused_global_target_when_every_device_overrides(self):
        import json
        for device in self.data['devices'].values():
            device['target_os'] = '5.18.4'
        self.inventory.write_text(json.dumps(self.data))
        self.assertEqual(self.run_fetch().returncode, 0)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertNotIn('5.18.3', self.log.read_text())


if __name__ == "__main__":
    unittest.main()
