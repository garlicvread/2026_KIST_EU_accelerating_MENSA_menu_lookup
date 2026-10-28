"""생성 데이터 정리가 소유권·고정 참조·파일 정체성을 지키는지 임시 경로에서 검사합니다."""

import copy
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from mensa import maintenance
except ImportError:
    maintenance = None

from mensa.releases import DirectoryPublisher
from mensa.translation_contract import source_for
from scripts.menu_source import parse_menu
from test_menu_source import page
from test_publication import translation_entry


TODAY = date(2026, 10, 28)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(maintenance, 'The guarded maintenance subsystem is required')
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.public = self.root / 'public'
        self.config = SimpleNamespace(
            paths=SimpleNamespace(state_dir=self.state, public_dir=self.public,
                                  checkout_dir=self.root / 'checkout'),
            inference=SimpleNamespace(log_dir=self.state / 'logs', models_dir=self.state / 'models',
                                      binary=self.state / 'runtime/ollama'),
            publication=SimpleNamespace(mode='directory'),
            retention=SimpleNamespace(keep_releases=2, log_days=14, log_max_bytes=32,
                                      backup_days=14, max_storage_bytes=10_000_000))
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.glossary = {
            'vegan': {'en': 'Vegan', 'ko': '비건'},
            'Weizen': {'en': 'Wheat', 'ko': '밀'},
            'Sellerie': {'en': 'Celery', 'ko': '셀러리'},
            'Milch und Laktose': {'en': 'Milk and lactose', 'ko': '우유 및 유당'},
        }
        self.cache = {'schema_version': 1,
                      'entries': {record['translation_key']: translation_entry(source_for(record))},
                      'notices': copy.deepcopy(self.glossary)}
        self.publisher = DirectoryPublisher(self.public, notice_glossary=self.glossary)

    def release(self, number, status='successful'):
        menu = copy.deepcopy(self.menu)
        menu['source']['fetched_at'] = f'2026-09-21T10:{number:02d}:00Z'
        identifier = self.publisher.publish(menu, self.cache)
        if status == 'successful':
            maintenance.record_successful_release(self.config, identifier, today=TODAY)
        elif status == 'prepared':
            maintenance.record_prepared_release(self.config, identifier, today=TODAY)
        return identifier

    def plan(self, **kwargs):
        return maintenance.plan_maintenance(self.config, today=TODAY, **kwargs)

    def release_exists(self, identifier):
        return (self.public / 'data/releases' / identifier).exists()

    def log(self, name, payload, age=0):
        logs = self.state / 'logs'
        logs.mkdir(exist_ok=True)
        path = logs / name
        path.write_bytes(payload)
        path.chmod(0o640)
        seconds = datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp()
        os.utime(path, (seconds - age * 86400, seconds - age * 86400))
        return path

    def test_success_limit_preserves_current_and_two_active_pins(self):
        identifiers = [self.release(i) for i in range(6)]
        plan = self.plan(pinned=identifiers[:2])
        json.dumps(plan, allow_nan=False)
        maintenance.apply_maintenance(self.config, plan)
        self.assertEqual({i for i in identifiers if self.release_exists(i)},
                         {identifiers[0], identifiers[1], identifiers[4], identifiers[5]})
        with self.assertRaisesRegex(ValueError, 'pin'):
            self.plan(pinned=identifiers[:3])

    def test_unconfirmed_preparation_does_not_displace_successes(self):
        successful = [self.release(i) for i in range(3)]
        failed = self.release(3, 'prepared')
        current = self.release(4, 'prepared')
        maintenance.apply_maintenance(self.config, self.plan(pinned=[failed]))
        self.assertTrue(self.release_exists(failed))
        self.assertTrue(self.release_exists(current))
        self.assertEqual([self.release_exists(i) for i in successful], [False, True, True])
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertFalse(self.release_exists(failed))

    def test_rolled_back_successful_current_uses_one_success_retention_slot(self):
        identifiers = [self.release(i) for i in range(4)]
        (self.public / 'data/current.json').write_text(json.dumps({'schema_version': 1, 'release_id': identifiers[0]}))
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertEqual({i for i in identifiers if self.release_exists(i)}, {identifiers[0], identifiers[-1]})

    def test_failed_publication_without_success_evidence_keeps_recovery(self):
        first = self.release(1, 'prepared')
        self.release(2, 'prepared')
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertTrue(self.release_exists(first))

    def test_unregistered_legacy_release_and_unknown_file_are_preserved(self):
        legacy = self.release(1, 'legacy')
        for index in range(2, 5):
            self.release(index)
        unknown = self.public / 'data/releases/notes.txt'
        unknown.write_text('not owned')
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertTrue(self.release_exists(legacy))
        self.assertEqual(unknown.read_text(), 'not owned')

    def test_release_symlink_and_noncanonical_pair_are_refused(self):
        identifier = self.release(1)
        pair_file = self.public / 'data/releases' / identifier / 'menu.json'
        payload = pair_file.read_bytes()
        pair_file.unlink()
        target = self.root / 'outside.json'
        target.write_bytes(payload)
        pair_file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink|regular'):
            self.plan()
        pair_file.unlink()
        pair_file.write_bytes(payload + b'\n')
        with self.assertRaisesRegex(ValueError, 'identity|canonical'):
            self.plan()

    def test_stale_plan_pointer_file_and_path_replacements_fail_before_removal(self):
        for mutation in ('pointer', 'file', 'directory', 'path'):
            with self.subTest(mutation=mutation):
                self.state = self.root / mutation / 'state'
                self.state.mkdir(parents=True, mode=0o700)
                self.public = self.root / mutation / 'public'
                self.config.paths.state_dir = self.state
                self.config.paths.public_dir = self.public
                self.config.inference.log_dir = self.state / 'logs'
                self.config.inference.models_dir = self.state / 'models'
                self.config.inference.binary = self.state / 'runtime/ollama'
                self.publisher = DirectoryPublisher(self.public, notice_glossary=self.glossary)
                first = self.release(1)
                self.release(2)
                self.release(3)
                plan = self.plan()
                directory = self.public / 'data/releases' / first
                if mutation == 'pointer':
                    (self.public / 'data/current.json').write_text(json.dumps({'schema_version': 1, 'release_id': first}))
                elif mutation == 'file':
                    target = directory / 'menu.json'
                    payload = target.read_bytes()
                    target.unlink()
                    target.write_bytes(payload)
                elif mutation == 'directory':
                    moved = directory.with_name('moved')
                    directory.rename(moved)
                    directory.mkdir()
                    for child in moved.iterdir():
                        (directory / child.name).write_bytes(child.read_bytes())
                else:
                    plan['actions'][0]['path'] = str(self.root)
                with self.assertRaises(ValueError):
                    maintenance.apply_maintenance(self.config, plan)
                self.assertTrue(directory.exists())
                if mutation == 'directory':
                    for child in moved.iterdir():
                        child.unlink()
                    moved.rmdir()

    def test_unknown_extra_release_file_prevents_deletion(self):
        first = self.release(1)
        self.release(2)
        self.release(3)
        extra = self.public / 'data/releases' / first / 'manual.txt'
        extra.write_text('preserve')
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertEqual(extra.read_text(), 'preserve')

    def test_log_cap_truncates_open_stream_in_place_and_expires_rotations(self):
        stdout = self.log('worker.stdout.log', b'a' * 40)
        stderr = self.log('worker.stderr.log', b'b' * 16)
        os.utime(stderr, ns=(stderr.stat().st_atime_ns, stderr.stat().st_mtime_ns + 1_000_000_000))
        expired = self.log('worker.stdout.log.1', b'old', age=15)
        before = stdout.stat()
        with stdout.open('ab') as active:
            maintenance.maintain_logs(self.config, today=TODAY)
            active.write(b'x')
            active.flush()
        self.assertFalse(expired.exists())
        self.assertEqual(stdout.stat().st_ino, before.st_ino)
        self.assertEqual(stdout.stat().st_mode, before.st_mode)
        self.assertEqual(stdout.read_bytes(), b'a' * 16 + b'x')
        self.assertEqual(stderr.read_bytes(), b'b' * 16)

    def test_logs_refuse_symlink_and_preserve_unknown_files(self):
        unknown = self.log('manual.txt', b'private')
        self.log('worker.stdout.log', b'a' * 16)
        maintenance.maintain_logs(self.config, today=TODAY)
        self.assertEqual(unknown.read_bytes(), b'private')
        link = self.state / 'logs/worker.stderr.log'
        link.symlink_to(unknown)
        with self.assertRaisesRegex(ValueError, 'symlink|regular'):
            maintenance.maintain_logs(self.config, today=TODAY)

    def test_fifo_logs_and_replacements_are_refused_without_blocking_worker_lock(self):
        probe = '''
from datetime import date
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import patch
from mensa import maintenance
from mensa.queue import exclusive_lock

path, boundary = Path(sys.argv[1]), sys.argv[2]
config = SimpleNamespace(
    paths=SimpleNamespace(state_dir=path.parent.parent, public_dir=path.parent.parent / 'public',
                          checkout_dir=path.parent.parent / 'checkout'),
    inference=SimpleNamespace(log_dir=path.parent),
    retention=SimpleNamespace(log_days=14, log_max_bytes=32))
actual_open = os.open
swapped = False
def replace_before_open(name, flags, *args, **kwargs):
    global swapped
    target_open = (boundary == 'check' or flags & os.O_ACCMODE == os.O_RDWR)
    if name == path.name and target_open and not swapped:
        path.unlink()
        os.mkfifo(path)
        swapped = True
    return actual_open(name, flags, *args, **kwargs)

try:
    with exclusive_lock(path.parent.parent / 'worker.lock'):
        if boundary == 'snapshot':
            path.unlink()
            os.mkfifo(path)
            maintenance.maintain_logs(config, today=date(2026, 10, 28))
        else:
            identity = maintenance._file_identity(path)
            with patch.object(maintenance.os, 'open', replace_before_open):
                if boundary == 'check':
                    with maintenance._directory(path.parent) as directory:
                        maintenance._check_file_at(directory, path.name, identity)
                else:
                    maintenance._apply_log({'action': 'truncate_log', 'path': str(path),
                                            'identity': identity, 'target_bytes': 0})
except ValueError:
    assert stat.S_ISFIFO(path.lstat().st_mode), 'The refused FIFO was modified'
    print('refused FIFO without reading or modifying it')
else:
    raise AssertionError('Maintenance accepted a FIFO')
'''
        for boundary in ('snapshot', 'check', 'apply'):
            with self.subTest(boundary=boundary):
                path = self.root / boundary / 'state/logs/worker.stdout.log'
                path.parent.mkdir(parents=True)
                path.write_bytes(b'original log')
                try:
                    result = subprocess.run([sys.executable, '-c', probe, str(path), boundary],
                                            capture_output=True, text=True, timeout=2)
                except subprocess.TimeoutExpired:
                    self.fail(f'{boundary} blocked on a FIFO while holding worker.lock')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('refused FIFO', result.stdout)

    def test_backup_requires_manifest_cutover_age_and_retains_latest(self):
        backup_root = self.root / 'backups'
        backup_root.mkdir()
        old = backup_root / 'copy-old'
        old.mkdir()
        (old / 'queue.json').write_text('old recovery')
        latest = backup_root / 'copy-latest'
        latest.mkdir()
        (latest / 'menu.json').write_text('latest recovery')
        unknown = backup_root / 'unrecorded'
        unknown.mkdir()
        (unknown / 'keep.txt').write_text('not owned')
        maintenance.record_migration_backup(backup_root, old, today=date(2026, 9, 1))
        maintenance.record_migration_backup(backup_root, latest, today=date(2026, 9, 2))
        maintenance.apply_maintenance(self.config, self.plan(backup_dir=backup_root))
        self.assertTrue(old.exists())
        maintenance.confirm_migration_cutover(backup_root, today=date(2026, 10, 20))
        maintenance.apply_maintenance(self.config, self.plan(backup_dir=backup_root))
        self.assertTrue(old.exists())
        later = maintenance.plan_maintenance(self.config, today=date(2026, 11, 4), backup_dir=backup_root)
        maintenance.apply_maintenance(self.config, later)
        self.assertFalse(old.exists())
        self.assertTrue(latest.exists())
        self.assertTrue(unknown.exists())

    def test_backup_modified_after_recording_is_not_owned_for_cleanup(self):
        root = self.root / 'backups'
        root.mkdir()
        for index in range(2):
            path = root / f'copy-{index}'
            path.mkdir()
            (path / 'owned').write_text('copy')
            maintenance.record_migration_backup(root, path, today=date(2026, 9, index + 1))
        maintenance.confirm_migration_cutover(root, today=date(2026, 9, 3))
        (root / 'copy-0/owned').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'backup|identity'):
            self.plan(backup_dir=root)
        self.assertTrue((root / 'copy-0/owned').exists())

    def test_capacity_counts_generated_state_and_excludes_model_source_and_git(self):
        (self.state / 'translation-checkpoint.json').write_bytes(b'cache')
        for directory in (self.state / 'models', self.state / 'runtime', self.state / '.git',
                          self.config.paths.checkout_dir):
            directory.mkdir()
            (directory / 'large').write_bytes(b'x' * 1000)
        self.assertEqual(maintenance.generated_bytes(self.config), 5)
        self.config.retention.max_storage_bytes = 10
        maintenance.ensure_capacity(self.config, additional_bytes=5)
        with self.assertRaisesRegex(ValueError, 'capacity|storage'):
            maintenance.ensure_capacity(self.config, additional_bytes=6)
        with self.assertRaises(ValueError):
            maintenance.ensure_capacity(self.config, additional_bytes=-1)

    def test_binary_directly_in_state_does_not_exempt_generated_files(self):
        self.config.inference.binary = self.state / 'ollama'
        self.config.inference.binary.write_bytes(b'executable')
        (self.state / 'translation-checkpoint.json').write_bytes(b'cache')
        self.assertEqual(maintenance.generated_bytes(self.config), 5)

    def test_capacity_counts_each_owned_hardlink_path_conservatively(self):
        original = self.state / 'checkpoint'
        original.write_bytes(b'cache')
        os.link(original, self.state / 'copy')
        self.assertEqual(maintenance.generated_bytes(self.config), 10)

    def test_explicit_backup_inside_checkout_still_counts_toward_capacity(self):
        root = self.config.paths.checkout_dir / '.tmp/backups'
        root.mkdir(parents=True)
        (root / 'copy').write_bytes(b'backup')
        self.assertEqual(maintenance.generated_bytes(self.config, backup_dir=root), 6)

    def test_log_hardlink_cannot_truncate_unowned_file(self):
        target = self.root / 'source'
        target.write_bytes(b'x' * 100)
        (self.state / 'logs').mkdir()
        os.link(target, self.state / 'logs/worker.stdout.log')
        with self.assertRaisesRegex(ValueError, 'link|ownership'):
            maintenance.maintain_logs(self.config, today=TODAY)
        self.assertEqual(target.read_bytes(), b'x' * 100)

    def test_empty_expired_log_rotation_is_removed(self):
        expired = self.log('worker.stdout.log.1', b'', age=15)
        maintenance.maintain_logs(self.config, today=TODAY)
        self.assertFalse(expired.exists())

    def test_known_checkout_link_is_excluded_without_following_other_links(self):
        self.config.paths.checkout_dir.mkdir()
        (self.config.paths.checkout_dir / 'source').write_bytes(b'x' * 1000)
        (self.state / 'checkout').symlink_to(self.config.paths.checkout_dir, target_is_directory=True)
        (self.state / 'translation-checkpoint.json').write_bytes(b'cache')
        self.assertEqual(maintenance.generated_bytes(self.config), 5)
        (self.state / 'checkout').unlink()
        other = self.root / 'other'
        other.mkdir()
        (self.state / 'checkout').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            maintenance.generated_bytes(self.config)

    def test_registered_backup_root_is_counted_and_expires_without_repeat_argument(self):
        root = self.root / 'backups'
        root.mkdir()
        for index in range(2):
            path = root / f'copy-{index}'
            path.mkdir()
            (path / 'owned').write_text('copy')
            maintenance.record_migration_backup(root, path, today=date(2026, 9, index + 1))
        maintenance.register_backup_root(self.config, root)
        self.assertEqual(maintenance.generated_bytes(self.config),
                         sum(path.stat().st_size for directory in (self.state, root)
                             for path in directory.rglob('*') if path.is_file()))
        maintenance.confirm_migration_cutover(root, today=date(2026, 9, 3))
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertFalse((root / 'copy-0').exists())
        self.assertTrue((root / 'copy-1/owned').exists())
        replacement = self.root / 'replacement'
        root.rename(replacement)
        root.mkdir()
        with self.assertRaisesRegex(ValueError, 'identity|root'):
            maintenance.generated_bytes(self.config)

    def test_malformed_registered_backup_root_cannot_disable_accounting(self):
        (self.state / 'retention-backup-root.json').write_text('null')
        with self.assertRaisesRegex(ValueError, 'backup root'):
            maintenance.generated_bytes(self.config)

    def test_interrupted_release_deletion_can_resume_using_recorded_identity(self):
        first = self.release(1)
        self.release(2)
        self.release(3)
        plan = self.plan()
        actual_unlink = os.unlink
        def interrupt(path, *args, **kwargs):
            if str(path) == 'translations.json':
                raise OSError('interrupted cleanup')
            return actual_unlink(path, *args, **kwargs)
        with patch.object(os, 'unlink', interrupt), self.assertRaisesRegex(OSError, 'interrupted'):
            maintenance.apply_maintenance(self.config, plan)
        maintenance.apply_maintenance(self.config, self.plan())
        self.assertFalse(self.release_exists(first))

    def test_backup_stale_apply_preserves_all_candidates(self):
        root = self.root / 'backups'
        root.mkdir()
        for index in range(3):
            path = root / f'copy-{index}'
            path.mkdir()
            (path / 'owned').write_text('copy')
            maintenance.record_migration_backup(root, path, today=date(2026, 9, index + 1))
        maintenance.confirm_migration_cutover(root, today=date(2026, 9, 4))
        plan = self.plan(backup_dir=root)
        (root / 'copy-1/owned').write_text('modified after dry-run')
        with self.assertRaises(ValueError):
            maintenance.apply_maintenance(self.config, plan)
        self.assertTrue((root / 'copy-0/owned').exists())

    def test_protected_capacity_overflow_fails_without_deleting_current(self):
        current = self.release(1)
        self.config.retention.max_storage_bytes = 1
        with self.assertRaisesRegex(ValueError, 'capacity|storage'):
            self.plan()
        self.assertTrue(self.release_exists(current))


class RetentionCliTests(unittest.TestCase):
    def test_real_directory_cli_defaults_to_json_dry_run_then_applies_under_lock(self):
        self.assertIsNotNone(maintenance)
        from scripts import retention
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            state = root / 'state'
            state.mkdir(mode=0o700)
            (state / 'logs').mkdir()
            log = state / 'logs/worker.stdout.log'
            log.write_bytes(b'expired log')
            os.utime(log, (0, 0))
            config = root / 'worker.toml'
            config.write_text(f'''[paths]
checkout_dir = "{root / 'checkout'}"
state_dir = "{state}"
public_dir = "{root / 'public'}"
[inference]
mode = "external"
provider = "ollama"
model = "fixture"
revision = "fixture"
url = "http://127.0.0.1:12345/api/chat"
[resources]
platform = "macos"
min_available_bytes = 1
max_load_per_cpu = 1
[publication]
mode = "directory"
''')
            output = io.StringIO()
            with patch('sys.stdout', output):
                self.assertEqual(retention.main(['--config', str(config)]), 0)
            self.assertTrue(json.loads(output.getvalue())['dry_run'])
            self.assertEqual(log.read_bytes(), b'expired log')
            with patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(retention.main(['--config', str(config), '--apply']), 0)
            self.assertEqual(log.read_bytes(), b'')

    def test_apply_requires_idle_queue_and_pins_read_only_remote_current(self):
        self.assertIsNotNone(maintenance)
        from scripts import retention
        with TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            config = SimpleNamespace(paths=SimpleNamespace(state_dir=state),
                                     publication=SimpleNamespace(mode='drive', remote='fixture:',
                                                                 folder_id='folder_identifier', file_id='file_identifier'))
            pair = ({'menu': 1}, {'translations': 1})
            fake = SimpleNamespace(load_current=lambda: pair)
            with patch.object(retention, 'load_worker_config', return_value=config), \
                    patch.object(retention, 'DrivePublisher', return_value=fake) as factory, \
                    patch.object(retention, 'plan_maintenance', return_value={'actions': []}) as planner, \
                    patch.object(retention, 'apply_maintenance', return_value={}) as apply, \
                    patch.object(retention, 'load_state', return_value={'pending': {'period': '2026-10-28'}}), \
                    patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                self.assertNotEqual(retention.main(['--config', str(state / 'worker.toml'), '--apply']), 0)
                apply.assert_not_called()
                with patch.object(retention, 'load_state', return_value={'pending': None}):
                    self.assertEqual(retention.main(['--config', str(state / 'worker.toml'), '--apply']), 0)
                factory.assert_called_once_with('fixture:', 'folder_identifier', file_id='file_identifier')
                self.assertEqual(len(planner.call_args.kwargs['pinned']), 1)
                apply.assert_called_once()


if __name__ == '__main__':
    unittest.main()
