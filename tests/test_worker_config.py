"""이 테스트 모듈은 메뉴 갱신 설정을 명시적으로 지정하고 검사하는 과정을 확인합니다. 실제 환경에 영향을 주지 않도록 테스트 예시는 모델을 설치하거나 갱신 작업을 시작하지 않습니다."""

import json
from pathlib import Path
import tempfile
import unittest

import mensa.config as config_module


class WorkerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_dir = self.root / 'config'
        self.config_dir.mkdir()
        self.config_path = self.config_dir / 'worker.toml'
        self.paths = {'checkout_dir': '../checkout', 'state_dir': '../private', 'public_dir': '../public'}
        self.inference = {
            'mode': 'managed', 'provider': 'ollama', 'model': ' Example Model ',
            'revision': ' Revision 1 ', 'binary': '../private/bin/ollama',
            'models_dir': '../private/models', 'log_dir': '../private/logs',
        }
        self.resources = {'platform': 'linux', 'min_available_bytes': 1024, 'max_load_per_cpu': 1.5}
        self.publication = {'mode': 'directory'}
        self.retention = None

    def write_config(self):
        tables = {'paths': self.paths, 'inference': self.inference,
                  'resources': self.resources, 'publication': self.publication}
        if self.retention is not None:
            tables['retention'] = self.retention
        lines = []
        for name, values in tables.items():
            lines.append(f'[{name}]')
            lines.extend(f'{key} = {json.dumps(value)}' for key, value in values.items())
        self.config_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


    def load(self):
        return config_module.load_worker_config(self.config_path)

    def assert_invalid(self, message):
        with self.assertRaisesRegex(ValueError, message):
            config_module.load_worker_config(self.config_path)

    def external(self, provider='ollama', url='http://localhost:11434'):
        self.inference = {key: self.inference[key] for key in ('mode', 'provider', 'model', 'revision')}
        self.inference.update(mode='external', provider=provider, url=url)

    def test_managed_settings_resolve_paths_and_preserve_exact_model_revision(self):
        self.write_config()
        result = self.load()
        self.assertEqual(result.paths.checkout_dir, self.root / 'checkout')
        self.assertEqual(result.paths.state_dir, self.root / 'private')
        self.assertEqual(result.paths.public_dir, self.root / 'public')
        self.assertEqual(result.inference.binary, self.root / 'private/bin/ollama')
        self.assertEqual(result.inference.models_dir, self.root / 'private/models')
        self.assertEqual(result.inference.log_dir, self.root / 'private/logs')
        self.assertEqual(result.inference.model, ' Example Model ')
        self.assertEqual(result.inference.revision, ' Revision 1 ')
        self.assertEqual(result.inference.mode, 'managed')
        self.assertEqual(result.inference.provider, 'ollama')
        self.assertIsNone(result.inference.url)
        self.assertEqual(result.resources.platform, 'linux')
        self.assertEqual(result.resources.min_available_bytes, 1024)
        self.assertEqual(result.resources.max_load_per_cpu, 1.5)

    def test_retention_defaults_preserve_existing_configs_and_constructor_callers(self):
        self.write_config()
        result = self.load()
        self.assertEqual(result.retention, config_module.RetentionSettings())
        self.assertEqual(vars(result.retention), {
            'history_days': 30, 'cache_unused_days': 180, 'cache_max_entries': 5000,
            'cache_max_bytes': 16777216, 'keep_releases': 10, 'log_days': 14,
            'log_max_bytes': 10485760, 'backup_days': 14, 'max_bundle_bytes': 8388608,
            'max_storage_bytes': 268435456,
        })
        legacy = config_module.WorkerConfig(result.paths, result.inference, result.resources, result.publication)
        self.assertEqual(legacy.retention, result.retention)

    def test_retention_optional_table_overrides_only_declared_positive_integer_limits(self):
        self.retention = {'history_days': 7, 'cache_max_entries': 123, 'keep_releases': 4}
        self.write_config()
        result = self.load()
        self.assertEqual(result.retention.history_days, 7)
        self.assertEqual(result.retention.cache_max_entries, 123)
        self.assertEqual(result.retention.keep_releases, 4)
        self.assertEqual(result.retention.cache_unused_days, 180)
        self.retention = {}
        self.write_config()
        self.assertEqual(self.load().retention, config_module.RetentionSettings())

    def test_retention_rejects_unknown_names_nonintegers_nonpositive_and_inconsistent_caps(self):
        for declaration in ({'unlimited': 1}, {'history_days': -1}, {'history_days': 0},
                            {'keep_releases': True}, {'cache_max_entries': 1.5},
                            {'cache_unused_days': '180'}, {'max_storage_bytes': 100},
                            {'max_bundle_bytes': 268435457}, {'cache_max_bytes': 268435457},
                            {'log_max_bytes': 268435457}):
            with self.subTest(declaration=declaration):
                self.retention = declaration
                self.write_config()
                self.assert_invalid('retention')

    def test_drive_example_declares_approved_retention_defaults(self):
        example = Path(__file__).resolve().parents[1] / 'config/worker.drive.example.toml'
        result = config_module.load_worker_config(example)
        self.assertEqual(result.retention, config_module.RetentionSettings())
        self.assertIn('[retention]', example.read_text())


    def test_publication_modes_preserve_explicit_declarations_without_provisioning(self):
        for declaration in ({'mode': 'directory'},
                            {'mode': 'github', 'repository': 'Example_Owner/menu-site',
                             'workflow': 'publish.yaml', 'remote': 'https://example.invalid:443/menu.git'}):
            with self.subTest(declaration=declaration):
                self.publication = declaration
                self.write_config()
                before = set(self.root.rglob('*'))
                result = self.load()
                self.assertEqual(result.publication.mode, declaration['mode'])
                for name in ('repository', 'workflow', 'remote'):
                    self.assertEqual(getattr(result.publication, name), declaration.get(name))
                self.assertEqual(set(self.root.rglob('*')), before)


    def test_external_providers_have_only_url_and_no_managed_paths(self):
        for provider in ('ollama', 'openai'):
            with self.subTest(provider=provider):
                self.external(provider, 'https://api.example.test/v1?version=one')
                self.write_config()
                result = self.load()
                self.assertEqual(result.inference.provider, provider)
                self.assertEqual(result.inference.url, 'https://api.example.test/v1?version=one')
                for name in ('binary', 'models_dir', 'log_dir'):
                    self.assertIsNone(getattr(result.inference, name))

    def test_drive_publication_is_explicit_and_has_no_git_destination(self):
        self.publication = {'mode': 'drive', 'remote': 'my-drive:',
                            'folder_id': 'folder_1234567890', 'file_id': 'snapshot_1234567890'}
        self.write_config()
        before = set(self.root.rglob('*'))
        result = self.load()
        self.assertEqual(result.publication.mode, 'drive')
        self.assertEqual(result.publication.remote, 'my-drive:')
        self.assertEqual(result.publication.folder_id, 'folder_1234567890')
        self.assertEqual(result.publication.file_id, 'snapshot_1234567890')
        self.assertIsNone(result.publication.repository)
        self.assertIsNone(result.publication.workflow)
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_drive_publication_rejects_paths_options_and_missing_ids(self):
        valid = {'mode': 'drive', 'remote': 'gdrive:',
                 'folder_id': 'folder_1234567890', 'file_id': 'snapshot_1234567890'}
        for field, value in (('remote', 'gdrive:another-folder'), ('remote', 'gdrive,token=secret:'),
                             ('folder_id', '../folder'), ('file_id', ''), ('file_id', 'id?key=secret')):
            with self.subTest(field=field, value=value):
                self.publication = {**valid, field: value}
                self.write_config()
                self.assert_invalid('publication.' + field)


    def test_managed_inference_rejects_unsupported_mode_and_provider(self):
        for key, value in (('mode', 'automatic'), ('provider', 'openai')):
            with self.subTest(key=key):
                original = self.inference[key]
                self.inference[key] = value
                self.write_config()
                self.assert_invalid(key)
                self.inference[key] = original


    def test_external_endpoint_rejects_relative_address_and_embedded_credentials(self):
        for url in ('/v1', 'http://user:secret@host'):
            with self.subTest(url=url):
                self.external(url=url)
                self.write_config()
                self.assert_invalid('url')


    def test_resources_reject_unsupported_platform_and_zero_limits(self):
        for key, value in (('platform', 'windows'), ('min_available_bytes', 0), ('max_load_per_cpu', 0)):
            with self.subTest(key=key):
                original = self.resources[key]
                self.resources[key] = value
                self.write_config()
                self.assert_invalid(key)
                self.resources[key] = original


    def test_managed_models_and_logs_stay_out_of_published_and_checkout_trees(self):
        original = dict(self.inference)
        for key, boundary in (('models_dir', 'public_dir'), ('log_dir', 'checkout_dir')):
            with self.subTest(key=key):
                self.inference = dict(original)
                self.inference[key] = self.paths[boundary] + '/child'
                self.write_config()
                self.assert_invalid(key + '.*' + boundary + '.*overlap')

    def test_managed_binary_cannot_be_in_public_output(self):
        self.inference['binary'] = '../public/bin/ollama'
        self.write_config()
        self.assert_invalid('binary.*public_dir')


    def test_example_loads_without_provisioning(self):
        example = Path(__file__).resolve().parents[1] / 'config/worker.example.toml'
        result = config_module.load_worker_config(example)
        self.assertEqual(result.inference.mode, 'managed')
        self.assertEqual(result.inference.provider, 'ollama')
        self.assertIsNone(result.inference.url)
        self.assertEqual(result.resources.platform, 'linux')
        self.assertEqual(result.publication.mode, 'directory')
        self.assertIn(result.paths.state_dir, result.inference.models_dir.parents)
        self.assertIn(result.paths.state_dir, result.inference.log_dir.parents)
        self.assertIn(result.paths.state_dir, result.inference.binary.parents)


if __name__ == '__main__':
    unittest.main()
