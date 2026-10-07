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

    def write_config(self):
        tables = {'paths': self.paths, 'inference': self.inference,
                  'resources': self.resources, 'publication': self.publication}
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
