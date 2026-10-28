"""Drive 설정 명령이 잠금과 기존 기록을 보존하면서 검증된 공개 데이터로 설정을 준비하는지 검사합니다."""

import copy
import hashlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mensa.config import load_worker_config
from mensa.model_runtime import model_session, _validate_paths
from mensa.drive_publication import DrivePublisher
from mensa.queue import BusyError, exclusive_lock, reconcile, save_state
from mensa.releases import DirectoryPublisher, _json_bytes, _release_id
from mensa.translation_contract import source_for
from scripts import configure_drive
from scripts.menu_source import parse_menu
from test_drive_publication import FakeRclone, FILE_ID, FOLDER_ID, REMOTE, envelope
from test_menu_source import page
from test_publication import translation_entry


NOW = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


class ConfigureDriveTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = self.root / 'private'
        self.base.mkdir(mode=0o700)
        self.checkout = self.root / 'checkout'
        (self.checkout / 'data').mkdir(parents=True)
        self.staging = self.root / 'public-staging'
        self.source = self.root / 'old-data'
        self.source.mkdir()
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.key = record['translation_key']
        self.glossary = {
            'vegan': {'en': 'Vegan', 'ko': '비건'},
            'Weizen': {'en': 'Wheat', 'ko': '밀'},
            'Sellerie': {'en': 'Celery', 'ko': '셀러리'},
            'Milch und Laktose': {'en': 'Milk and lactose', 'ko': '우유 및 유당'},
        }
        self.cache = {'schema_version': 1,
                      'entries': {self.key: translation_entry(source_for(record))},
                      'notices': copy.deepcopy(self.glossary)}
        self.write_json(self.checkout / 'data/notice-translations.json',
                        {'schema_version': 1, 'notices': self.glossary})
        self.write_source(self.menu, self.cache)
        self.queue = self.base / 'queue.json'
        self.queue.write_bytes(b'{"schema_version":1, "completed_period":"2026-09-20", "pending":null}\n')
        (self.base / 'worker.lock').write_bytes(b'preserved lock inode and bytes\n')
        self.write_json(self.base / 'translation-checkpoint.json', {'schema_version': 1, 'entries': {}})
        self.write_json(self.base / 'snapshot-journal.json',
                        {'version': 1, 'checkout': str(self.base / 'old-checkout'),
                         'period': '2026-09-20', 'snapshot': None, 'replacement': None})
        (self.base / 'snapshot-journal.json').chmod(0o600)
        self.write_json(self.base / 'last-result.json',
                        {'at': '2026-09-20T10:00:00Z', 'status': 'completed', 'period': '2026-09-20'})
        for relative, body in (('runtime/ollama', b'existing runtime'),
                               ('models/weights.bin', b'existing model'),
                               ('logs/worker.log', b'existing log'),
                               ('snapshot-backup.bin', b'existing recovery bytes')):
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.command = FakeRclone()
        self.publisher_calls = []

    def write_json(self, path, value):
        path.write_bytes(_json_bytes(value))

    def write_source(self, menu, cache):
        self.write_json(self.source / 'menu.json', menu)
        self.write_json(self.source / 'translations.json', cache)

    def module(self):
        return configure_drive

    def publisher(self, remote, folder_id, *, file_id=None):
        self.publisher_calls.append((remote, folder_id, file_id))
        return DrivePublisher(remote, folder_id, file_id=file_id, command=self.command)

    def configure(self, *, source=True, **overrides):
        module = self.module()
        options = {'source_data': self.source if source else None, 'publisher': self.publisher,
                   'model_revision': 'a' * 64}
        options.update(overrides)
        positional = {name: options.pop(name, value) for name, value in (
            ('base', self.base), ('checkout', self.checkout), ('data_dir', self.staging),
            ('remote', REMOTE), ('folder_id', FOLDER_ID))}
        with patch.object(module, 'datetime', FixedDatetime):
            return module.configure(**positional, **options)

    def state_files(self):
        return {path.relative_to(self.base): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in self.base.rglob('*') if path.is_file()}

    def test_source_bootstrap_writes_private_config_and_preserves_existing_state(self):
        before = self.state_files()
        lock_inode = (self.base / 'worker.lock').stat().st_ino
        source_before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        result = self.configure()
        expected_id = _release_id(_json_bytes(self.menu), _json_bytes(self.cache))
        self.assertEqual(result, {'config_path': str(self.base / 'worker.toml'),
                                  'file_id': FILE_ID, 'release_id': expected_id,
                                  'data_dir': str(self.staging)})
        json.dumps(result)
        config = load_worker_config(self.base / 'worker.toml')
        self.assertEqual((config.paths.checkout_dir, config.paths.state_dir, config.paths.public_dir),
                         (self.checkout, self.base, self.staging))
        self.assertEqual((config.inference.mode, config.inference.provider, config.inference.model,
                          config.inference.revision),
                         ('managed', 'ollama', 'gemma4:31b', 'a' * 64))
        self.assertEqual((config.inference.binary, config.inference.models_dir, config.inference.log_dir),
                         (self.base / 'runtime/ollama', self.base / 'models', self.base / 'logs'))
        self.assertEqual((config.resources.platform, config.resources.min_available_bytes,
                          config.resources.max_load_per_cpu), ('macos', 32 * 1024 ** 3, 0.6))
        self.assertEqual((config.publication.mode, config.publication.remote,
                          config.publication.folder_id, config.publication.file_id),
                         ('drive', REMOTE, FOLDER_ID, FILE_ID))
        self.assertEqual((self.base / 'worker.toml').stat().st_mode & 0o777, 0o600)
        after = self.state_files()
        after.pop(Path('worker.toml'))
        after.pop(Path('translation-state.json'))
        after.pop(Path('retention-releases.json'))
        self.assertEqual(after, before)
        self.assertEqual((self.base / 'worker.lock').stat().st_ino, lock_inode)
        self.assertEqual({path.name: path.read_bytes() for path in self.source.iterdir()}, source_before)
        self.assertEqual(DirectoryPublisher(self.staging, notice_glossary=self.glossary).load_current(),
                         (self.menu, self.cache))
        self.assertEqual(json.loads(self.command.uploads[0]), envelope(self.menu, self.cache))

    def test_installed_manifest_revision_is_ready_through_the_configured_runtime(self):
        manifest = self.base / 'models/manifests/registry.ollama.ai/library/gemma4/31b'
        manifest.parent.mkdir(parents=True)
        raw = b'{"schemaVersion":2,"config":{},"layers":[]}\n'
        manifest.write_bytes(raw)
        self.configure(model_revision=None)
        config = load_worker_config(self.base / 'worker.toml')
        digest = hashlib.sha256(raw).hexdigest()
        process = Mock(pid=123456)
        process.poll.return_value = None
        stop = Mock()
        with model_session(config.inference, launch=Mock(return_value=process),
                           choose_port=lambda: 43210,
                           probe=lambda *_: {'models': [{'name': 'gemma4:31b', 'digest': digest}]},
                           stop=stop) as runtime:
            self.assertEqual(runtime['backend_revision'], digest)
        stop.assert_called_once_with(process)
        self.assertEqual(config.inference.revision, digest)

    def test_explicit_revision_supports_unprovisioned_runtime(self):
        self.configure(model_revision='a' * 64)
        config = load_worker_config(self.base / 'worker.toml')
        self.assertEqual(config.inference.revision, 'a' * 64)

    def test_bootstrap_prepares_owned_legacy_logs_without_replacing_or_truncating(self):
        logs = self.base / 'logs'
        model_log = logs / 'model.log'
        model_log.write_bytes(b'previous model output\n')
        logs.chmod(0o755)
        model_log.chmod(0o644)
        before = {path: (path.stat().st_ino, path.read_bytes())
                  for path in (model_log, logs / 'worker.log', self.queue,
                               self.base / 'snapshot-journal.json')}
        log_inode = logs.stat().st_ino
        self.configure()
        config = load_worker_config(self.base / 'worker.toml')
        _validate_paths(config.inference)
        self.assertEqual(logs.stat().st_mode & 0o777, 0o700)
        self.assertEqual(model_log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(logs.stat().st_ino, log_inode)
        self.assertEqual({path: (path.stat().st_ino, path.read_bytes()) for path in before}, before)
        self.assertEqual((logs / 'worker.log').stat().st_mode & 0o777, 0o644)

    def test_repeat_bootstrap_repairs_owned_logs_with_existing_config(self):
        first = self.configure()
        config_path = self.base / 'worker.toml'
        identity = (config_path.stat().st_ino, config_path.read_bytes())
        model_log = self.base / 'logs/model.log'
        model_log.write_bytes(b'preserve on repeat')
        model_log.chmod(0o644)
        model_log.parent.chmod(0o755)
        self.assertEqual(self.configure(source=False), first)
        _validate_paths(load_worker_config(config_path).inference)
        self.assertEqual((config_path.stat().st_ino, config_path.read_bytes()), identity)
        self.assertEqual(model_log.read_bytes(), b'preserve on repeat')
        self.assertEqual(len(self.command.uploads), 1)

    def test_unsafe_model_log_is_refused_before_remote_access(self):
        logs = self.base / 'logs'
        target = logs / 'model.log'
        outside = self.root / 'operator-file'
        outside.write_bytes(b'do not touch')
        original_mode = outside.stat().st_mode
        for kind in ('symlink', 'fifo', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    target.symlink_to(outside)
                elif kind == 'fifo':
                    os.mkfifo(target)
                else:
                    os.link(outside, target)
                try:
                    with self.assertRaises(ValueError):
                        self.configure()
                    self.assertEqual(self.command.commands, [])
                    self.assertEqual(logs.stat().st_mode & 0o777, 0o755)
                    self.assertEqual(outside.stat().st_mode, original_mode)
                finally:
                    target.unlink()
        with patch.object(self.module().os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(ValueError):
                self.configure()
        self.assertEqual(self.command.commands, [])

    def test_replaced_log_directory_during_upload_is_never_chmodded(self):
        logs = self.base / 'logs'
        original = self.root / 'original-logs'
        operator_log = logs / 'model.log'
        def replace_logs(_):
            logs.rename(original)
            logs.mkdir(mode=0o755)
            operator_log.write_bytes(b'operator-owned replacement')
            operator_log.chmod(0o644)
        self.command.on_copy = replace_logs
        with self.assertRaisesRegex(ValueError, 'log directory changed'):
            self.configure()
        self.assertEqual(logs.stat().st_mode & 0o777, 0o755)
        self.assertEqual(original.stat().st_mode & 0o777, 0o755)
        self.assertEqual(operator_log.stat().st_mode & 0o777, 0o644)
        self.assertEqual(operator_log.read_bytes(), b'operator-owned replacement')
        self.assertFalse((self.base / 'worker.toml').exists())
        self.assertFalse(self.staging.exists())

    def test_symlinked_log_directory_is_refused_before_remote_access(self):
        logs = self.base / 'logs'
        actual = self.root / 'operator-logs'
        logs.rename(actual)
        logs.symlink_to(actual, target_is_directory=True)
        before = actual.stat().st_mode
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.command.commands, [])
        self.assertEqual(actual.stat().st_mode, before)

    def test_log_permissions_wait_for_successful_configuration_install(self):
        logs = self.base / 'logs'
        target = logs / 'model.log'
        target.write_bytes(b'previous model output')
        target.chmod(0o644)
        before = (logs.stat().st_mode, target.stat().st_mode, target.stat().st_ino, target.read_bytes())
        self.command.fail_operation = 'copyto'
        with self.assertRaises(RuntimeError):
            self.configure()
        self.assertEqual((logs.stat().st_mode, target.stat().st_mode, target.stat().st_ino,
                          target.read_bytes()), before)
        self.command.fail_operation = None
        with patch.object(self.module().os, 'link', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.configure()
        self.assertEqual((logs.stat().st_mode, target.stat().st_mode, target.stat().st_ino,
                          target.read_bytes()), before)

    def test_missing_manifest_requires_explicit_revision_before_remote_writes(self):
        before = self.state_files()
        with self.assertRaisesRegex(ValueError, 'model-revision'):
            self.configure(model_revision=None)
        self.assertEqual(self.state_files(), before)
        self.assertEqual(self.command.commands, [])

    def test_invalid_explicit_revision_is_rejected_before_remote_writes(self):
        for revision in ('', ' ', 'digest with spaces', 'a\ndigest', 'x' * 257, 123):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.configure(model_revision=revision)
        self.assertEqual(self.command.commands, [])

    def test_changed_manifest_during_upload_does_not_install_a_stale_revision(self):
        manifest = self.base / 'models/manifests/registry.ollama.ai/library/gemma4/31b'
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(b'{"schemaVersion":2,"config":{},"layers":[]}')
        self.command.on_copy = lambda _: manifest.write_bytes(
            b'{"schemaVersion":2,"config":{},"layers":[],"changed":true}')
        with self.assertRaisesRegex(ValueError, 'inference destination changed'):
            self.configure(model_revision=None)
        self.assertFalse((self.base / 'worker.toml').exists())
        self.assertFalse(self.staging.exists())

    def test_remote_bootstrap_does_not_read_tracked_source_data(self):
        self.command.add(envelope(self.menu, self.cache))
        tracked = self.checkout / 'site/data'
        tracked.mkdir(parents=True)
        (tracked / 'menu.json').write_text('invalid tracked data')
        (tracked / 'translations.json').write_text('invalid tracked data')
        result = self.configure(source=False)
        self.assertEqual(result['file_id'], FILE_ID)
        self.assertEqual(self.command.uploads, [])
        self.assertEqual((tracked / 'menu.json').read_text(), 'invalid tracked data')

    def test_bootstrap_projects_public_data_but_seeds_full_private_reuse(self):
        from test_menu_source import meal
        old = parse_menu(page(meal(name='Old unrelated meal', date='01.08.2026'),
                              date='01.08.2026'), '2026-09-21T10:00:00Z')
        record = old['days'][0]['meals'][0]
        old_key = record['translation_key']
        combined = copy.deepcopy(self.menu)
        combined['days'] = old['days'] + combined['days']
        combined['coverage']['start'] = '2026-08-01'
        cache = copy.deepcopy(self.cache)
        cache['entries'][old_key] = translation_entry(source_for(record))
        self.write_source(combined, cache)
        self.configure()
        public = json.loads(self.command.uploads[0])
        self.assertEqual([d['date'] for d in public['menu']['days']], ['2026-09-21'])
        self.assertNotIn(old_key, public['translations']['entries'])
        private = json.loads((self.base/'translation-state.json').read_text())
        self.assertIn(old_key, private['cache']['entries'])
        self.assertEqual((self.base/'translation-state.json').stat().st_mode & 0o777, 0o600)
        config = (self.base/'worker.toml').read_text()
        self.assertIn('[retention]', config)

    def test_source_bootstrap_rejects_different_published_data_without_writes(self):
        corrected = copy.deepcopy(self.cache)
        corrected['entries'][self.key]['en']['name'] = 'Published correction'
        remote = self.command.add(envelope(self.menu, corrected))
        remote_before = copy.deepcopy(remote)
        before = self.state_files()
        with self.assertRaisesRegex(ValueError, 'different data'):
            self.configure()
        self.assertEqual(remote, remote_before)
        self.assertEqual(self.command.uploads, [])
        self.assertEqual(self.state_files(), before)
        self.assertFalse(self.staging.exists())

    def test_source_bootstrap_reuses_identical_published_data(self):
        self.command.add(envelope(self.menu, self.cache))
        result = self.configure()
        self.assertEqual(result['file_id'], FILE_ID)
        self.assertEqual(self.command.uploads, [])

    def test_missing_remote_pair_has_no_implicit_source_fallback(self):
        tracked = self.checkout / 'site/data'
        tracked.mkdir(parents=True)
        self.write_json(tracked / 'menu.json', self.menu)
        self.write_json(tracked / 'translations.json', self.cache)
        before = self.state_files()
        with self.assertRaises(ValueError):
            self.configure(source=False)
        self.assertEqual(self.command.uploads, [])
        self.assertEqual(self.state_files(), before)
        self.assertFalse(self.staging.exists())

    def test_invalid_incomplete_stale_or_wrong_glossary_pair_fails_before_upload(self):
        cases = []
        missing = copy.deepcopy(self.cache)
        missing['entries'].clear()
        cases.append((self.menu, missing))
        wrong_notice = copy.deepcopy(self.cache)
        wrong_notice['notices']['Weizen']['en'] = 'Unreviewed wheat'
        cases.append((self.menu, wrong_notice))
        old_menu = parse_menu(page(date='20.09.2026'), '2026-09-20T10:00:00Z')
        cases.append((old_menu, self.cache))
        invalid = copy.deepcopy(self.menu)
        invalid['days'][0]['meals'][0]['price_source']['name'] = 'Unrelated meal'
        cases.append((invalid, self.cache))
        for index, pair in enumerate(cases):
            with self.subTest(case=index):
                self.write_source(*pair)
                before = self.state_files()
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual(self.command.commands, [])
                self.assertEqual(self.state_files(), before)
                self.assertFalse(self.staging.exists())

    def test_pending_or_corrupt_queue_is_preserved_and_blocks_remote_operations(self):
        pending = reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, NOW)
        cases = [_json_bytes(pending), b'not json', b'{"schema_version":2,"completed_period":null,"pending":null}',
                 b'{"schema_version":1,"pending":null,"pending":null,"completed_period":null}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.queue.write_bytes(raw)
                before = self.state_files()
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual(self.state_files(), before)
                self.assertEqual(self.command.commands, [])

    def test_busy_lock_fails_without_waiting_or_touching_queue(self):
        before = self.state_files()
        with exclusive_lock(self.base / 'worker.lock'):
            with self.assertRaises(BusyError):
                self.configure()
        self.assertEqual(self.state_files(), before)
        self.assertEqual(self.command.commands, [])

    def test_overlapping_paths_and_nonprivate_state_are_refused_before_upload(self):
        for overrides in ({'data_dir': self.base / 'public'},
                          {'data_dir': self.checkout / 'public'},
                          {'checkout': self.base / 'checkout'},
                          {'data_dir': self.source}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.configure(**overrides)
        self.base.chmod(0o755)
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.base.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.command.commands, [])

    def test_repeat_setup_preserves_config_and_data_and_reuses_remote_file(self):
        first = self.configure()
        config = self.base / 'worker.toml'
        identity = (config.stat().st_ino, config.stat().st_mtime_ns, config.read_bytes())
        before = self.state_files()
        second = self.configure(source=False)
        self.assertEqual(first, second)
        self.assertEqual((config.stat().st_ino, config.stat().st_mtime_ns, config.read_bytes()), identity)
        self.assertEqual(self.state_files(), before)
        self.assertEqual(len(self.command.uploads), 1)
        self.assertEqual(self.publisher_calls[-1], (REMOTE, FOLDER_ID, FILE_ID))

    def test_conflicting_existing_config_or_local_data_is_not_overwritten(self):
        self.configure()
        config = self.base / 'worker.toml'
        config.write_text(config.read_text().replace('gemma4:31b', 'different-model'))
        saved = config.read_bytes()
        commands = len(self.command.commands)
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(config.read_bytes(), saved)
        self.assertEqual(len(self.command.commands), commands)

    def test_conflicting_local_release_is_refused_before_drive_access(self):
        changed = copy.deepcopy(self.menu)
        changed['source']['fetched_at'] = '2026-09-21T11:00:00Z'
        local = DirectoryPublisher(self.staging, notice_glossary=self.glossary)
        local.publish(changed, self.cache)
        selected = (self.staging / 'data/current.json').read_bytes()
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual((self.staging / 'data/current.json').read_bytes(), selected)
        self.assertEqual(self.command.commands, [])

    def test_corrupt_checkpoint_or_unfinished_recovery_is_not_ignored(self):
        checkpoint = self.base / 'translation-checkpoint.json'
        checkpoint.write_text('broken checkpoint')
        before = self.state_files()
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.state_files(), before)
        self.write_json(checkpoint, {'schema_version': 1, 'entries': {}})
        journal = self.base / 'snapshot-journal.json'
        self.write_json(journal, {'version': 1, 'checkout': str(self.base / 'old-checkout'),
                                  'period': '2026-09-20', 'snapshot': {
                                      'base': 'a' * 40, 'tree': 'b' * 40,
                                      'blobs': {'site/data/menu.json': 'c' * 40,
                                                'site/data/translations.json': 'd' * 40},
                                      'sha': None, 'owned': True}, 'replacement': None})
        before = self.state_files()
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.state_files(), before)
        self.assertEqual(self.command.commands, [])

    def test_prepared_drive_destination_and_pair_must_match_without_rewriting_checkpoint(self):
        checkpoint = self.base / 'drive-publication.json'
        intended = {'schema_version': 1, 'period': '2026-09-21T11:00',
                    'release_id': envelope(self.menu, self.cache)['release_id'],
                    'previous_release_id': None,
                    'remote': REMOTE, 'folder_id': FOLDER_ID, 'file_id': FILE_ID}
        for field, value in (('remote', 'different-operator:'), ('folder_id', 'other_folder_12345'),
                             ('release_id', 'a' * 64)):
            with self.subTest(field=field):
                changed = {**intended, field: value}
                self.write_json(checkpoint, changed)
                before = self.state_files()
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual(self.state_files(), before)
                self.assertEqual(self.command.commands, [])
        self.write_json(checkpoint, intended)
        original = checkpoint.read_bytes()
        self.command.add(envelope(self.menu, self.cache))
        self.assertEqual(self.configure()['file_id'], FILE_ID)
        self.assertEqual(checkpoint.read_bytes(), original)
        self.assertEqual(self.command.uploads, [])

    def test_upload_failure_and_unknown_outcome_preserve_state_and_reconcile_on_retry(self):
        before = self.state_files()
        self.command.fail_operation = 'copyto'
        with self.assertRaises(RuntimeError):
            self.configure()
        self.assertEqual(self.state_files(), before)
        self.assertFalse(self.staging.exists())
        self.command.fail_operation = None
        def lose_response(_):
            raise RuntimeError('Upload response lost')
        self.command.on_copy = lose_response
        with self.assertRaises(RuntimeError):
            self.configure()
        self.assertEqual(self.state_files(), before)
        self.assertFalse(self.staging.exists())
        self.command.on_copy = None
        self.configure()
        self.assertEqual(len(self.command.uploads), 1)

    def test_failed_config_install_keeps_state_and_verified_new_staging_for_retry(self):
        before = self.state_files()
        with patch.object(self.module().os, 'link', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.configure()
        self.assertEqual(self.state_files(), before)
        self.assertEqual(DirectoryPublisher(self.staging, notice_glossary=self.glossary).load_current(),
                         (self.menu, self.cache))
        self.configure()
        self.assertEqual(len(self.command.uploads), 1)

    def test_config_created_during_upload_is_preserved_and_blocks_local_install(self):
        config = self.base / 'worker.toml'
        self.command.on_copy = lambda _: config.write_bytes(b'operator wrote a different config')
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(config.read_bytes(), b'operator wrote a different config')
        self.assertFalse(self.staging.exists())

    def test_replaced_staging_directory_is_refused_before_local_mutation(self):
        self.staging.mkdir()
        def replace_staging(_):
            self.staging.rename(self.root / 'original-staging')
            self.staging.mkdir()
            (self.staging / 'operator-notes.txt').write_bytes(b'new directory belongs to operator')
        self.command.on_copy = replace_staging
        with self.assertRaises(ValueError):
            self.configure()
        self.assertFalse((self.base / 'worker.toml').exists())
        self.assertEqual(list(self.staging.iterdir()), [self.staging / 'operator-notes.txt'])

    def test_fresh_paths_allow_unprovisioned_runtime_without_creating_queue_or_model(self):
        base = self.root / 'new-private-parent/state'
        staging = self.root / 'new-public-parent/staging'
        result = self.configure(base=base, data_dir=staging)
        config = load_worker_config(Path(result['config_path']))
        self.assertEqual(config.paths.state_dir, base)
        self.assertEqual(config.paths.public_dir, staging)
        self.assertEqual(base.stat().st_mode & 0o777, 0o700)
        self.assertFalse((base / 'queue.json').exists())
        self.assertFalse((base / 'translation-checkpoint.json').exists())
        self.assertFalse((base / 'runtime').exists())
        self.assertFalse((base / 'models').exists())

    def test_cli_requires_explicit_paths_and_outputs_machine_readable_result(self):
        module = self.module()
        argv = ['--base', str(self.base), '--checkout', str(self.checkout),
                '--data-dir', str(self.staging), '--remote', REMOTE, '--folder-id', FOLDER_ID,
                '--source-data', str(self.source)]
        result = {'config_path': str(self.base / 'worker.toml'), 'file_id': FILE_ID,
                  'release_id': 'a' * 64, 'data_dir': str(self.staging)}
        with patch.object(module, 'configure', return_value=result) as configure, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(module.main(argv), 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        configure.assert_called_once_with(self.base, self.checkout, self.staging, REMOTE, FOLDER_ID,
                                          source_data=self.source, model_revision=None)
        with patch.object(module, 'configure', return_value=result) as configure, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(module.main([*argv, '--model-revision', 'a' * 64]), 0)
        self.assertEqual(configure.call_args.kwargs['model_revision'], 'a' * 64)
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as missing:
            module.main(['--base', str(self.base)])
        self.assertEqual(missing.exception.code, 2)
        with patch.object(module, 'configure', side_effect=ValueError('Refusing changed config')), \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(module.main(argv), 1)
        self.assertIn('Refusing changed config', output.getvalue())


if __name__ == '__main__':
    unittest.main()
