import importlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


LABEL = 'io.github.garlicvread.mensa-refresh'


class InstallLocalWorkerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.installer = importlib.import_module('scripts.install_local_worker')
        except ModuleNotFoundError:
            self.fail('The local worker installer is not implemented')
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name).resolve()
        self.base = self.home / 'Library/Application Support/Mensa'
        self.worker = self.base / 'checkout/scripts/local_refresh.py'
        self.worker.parent.mkdir(parents=True)
        self.worker.write_text('# worker fixture\n', encoding='utf-8')
        self.plist_path = self.home / 'Library/LaunchAgents' / f'{LABEL}.plist'
        home_patch = patch.object(Path, 'home', return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def test_plist_runs_bounded_worker_with_expected_environment(self):
        python = '/opt/example/Python/bin/python3'
        result = self.installer.generate_plist(self.base, python)
        self.assertEqual(result['Label'], LABEL)
        self.assertEqual(result['ProgramArguments'], [
            python, '-m', 'scripts.local_refresh', '--base', str(self.base)])
        self.assertEqual(result['WorkingDirectory'], str(self.base / 'checkout'))
        self.assertTrue(result['RunAtLoad'])
        self.assertEqual(result['StartInterval'], 900)
        self.assertEqual(result['ProcessType'], 'Background')
        self.assertTrue(result['LowPriorityIO'])
        self.assertEqual(result['Nice'], 10)
        self.assertEqual(result['ThrottleInterval'], 60)
        self.assertNotIn('KeepAlive', result)
        self.assertEqual(result['EnvironmentVariables']['PATH'].split(':'), [
            '/opt/example/Python/bin', '/opt/homebrew/bin', '/usr/local/bin',
            '/usr/bin', '/bin', '/usr/sbin', '/sbin'])
        self.assertEqual(Path(result['StandardOutPath']).parent, self.base / 'logs')
        self.assertEqual(Path(result['StandardErrorPath']).parent, self.base / 'logs')
        self.assertNotEqual(result['StandardOutPath'], result['StandardErrorPath'])
        self.assertEqual(plistlib.loads(plistlib.dumps(result)), result)

    def test_relative_arguments_become_absolute_in_plist(self):
        result = self.installer.generate_plist('relative base', 'python3')
        self.assertTrue(Path(result['ProgramArguments'][0]).is_absolute())
        self.assertEqual(result['ProgramArguments'][-1], str(Path('relative base').resolve()))

    def test_interpreter_symlink_is_preserved_for_virtual_environments(self):
        python = self.home / 'venv/bin/python3'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        result = self.installer.generate_plist(self.base, python)
        self.assertEqual(result['ProgramArguments'][0], str(python))
        self.assertEqual(result['EnvironmentVariables']['PATH'].split(':')[0], str(python.parent))

    def test_write_only_atomically_replaces_plist_without_launchctl(self):
        self.plist_path.parent.mkdir(parents=True)
        self.plist_path.write_bytes(b'old plist')
        replace = os.replace

        def checked_replace(source, destination):
            self.assertEqual(self.plist_path.read_bytes(), b'old plist')
            self.assertEqual(Path(source).parent, self.plist_path.parent)
            self.assertEqual(Path(destination), self.plist_path)
            self.assertEqual(plistlib.loads(Path(source).read_bytes())['Label'], LABEL)
            replace(source, destination)

        with patch.object(self.installer.os, 'replace', side_effect=checked_replace), \
                patch.object(self.installer.subprocess, 'run') as run:
            result = self.installer.install(self.base, load=False)
        run.assert_not_called()
        self.assertEqual(result, self.plist_path)
        self.assertTrue((self.base / 'logs').is_dir())
        data = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(data['ProgramArguments'][0], str(Path(sys.executable).absolute()))
        self.assertEqual(list(self.plist_path.parent.iterdir()), [self.plist_path])

    def test_missing_dedicated_checkout_has_no_installation_side_effects(self):
        self.worker.unlink()
        with patch.object(self.installer.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'checkout'):
                self.installer.install(self.base)
        run.assert_not_called()
        self.assertFalse(self.plist_path.parent.exists())
        self.assertFalse((self.base / 'logs').exists())

    def test_failed_atomic_write_retains_old_plist_and_removes_staging_file(self):
        self.plist_path.parent.mkdir(parents=True)
        self.plist_path.write_bytes(b'old plist')
        with patch.object(self.installer.os, 'replace', side_effect=OSError('disk failure')), \
                patch.object(self.installer.subprocess, 'run') as run:
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.installer.install(self.base)
        run.assert_not_called()
        self.assertEqual(self.plist_path.read_bytes(), b'old plist')
        self.assertEqual(list(self.plist_path.parent.iterdir()), [self.plist_path])

    def test_first_install_bootstraps_only_its_user_agent(self):
        calls = []

        def launchctl(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if args[1] == 'print' else 0, '', '')

        with patch.object(self.installer.os, 'getuid', return_value=501), \
                patch.object(self.installer.subprocess, 'run', side_effect=launchctl):
            self.installer.install(self.base)
        self.assertEqual(calls, [
            ['/bin/launchctl', 'print', f'gui/501/{LABEL}'],
            ['/bin/launchctl', 'bootstrap', 'gui/501', str(self.plist_path)],
        ])

    def test_reinstall_unloads_only_existing_label_before_bootstrap(self):
        calls = []

        def launchctl(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(self.installer.os, 'getuid', return_value=501), \
                patch.object(self.installer.subprocess, 'run', side_effect=launchctl):
            self.installer.install(self.base)
        self.assertEqual(calls, [
            ['/bin/launchctl', 'print', f'gui/501/{LABEL}'],
            ['/bin/launchctl', 'bootout', f'gui/501/{LABEL}'],
            ['/bin/launchctl', 'bootstrap', 'gui/501', str(self.plist_path)],
        ])

    def test_failed_bootout_does_not_attempt_duplicate_bootstrap(self):
        calls = []

        def launchctl(args, **kwargs):
            calls.append(args)
            if args[1] == 'bootout':
                raise subprocess.CalledProcessError(5, args, stderr='bootout failed')
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(self.installer.os, 'getuid', return_value=501), \
                patch.object(self.installer.subprocess, 'run', side_effect=launchctl):
            with self.assertRaises(subprocess.CalledProcessError):
                self.installer.install(self.base)
        self.assertEqual([call[1] for call in calls], ['print', 'bootout'])

    def test_failed_bootstrap_is_reported_by_cli(self):
        def launchctl(args, **kwargs):
            return subprocess.CompletedProcess(args, 5, '', 'bootstrap failed')

        with patch.object(self.installer.subprocess, 'run', side_effect=launchctl), \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(self.installer.main(['--base', str(self.base)]), 1)
        self.assertIn('Worker installation failed', output.getvalue())

    def test_import_does_not_load_an_agent(self):
        with patch('subprocess.run') as run:
            importlib.reload(self.installer)
        run.assert_not_called()
        self.assertFalse(self.plist_path.exists())

    def test_cli_write_only_default_base_does_not_load_agent(self):
        with patch.object(self.installer.subprocess, 'run') as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(self.installer.main(['--write-only']), 0)
        run.assert_not_called()
        self.assertTrue(self.plist_path.is_file())
        self.assertIn('not loaded', output.getvalue())


if __name__ == '__main__':
    unittest.main()
