"""이 테스트 모듈은 메뉴 갱신 설정을 명시적으로 지정하고 검사하는 과정을 확인합니다. 실제 환경에 영향을 주지 않도록 테스트 예시는 모델을 설치하거나 갱신 작업을 시작하지 않습니다."""

from dataclasses import FrozenInstanceError, fields
import itertools
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

import mensa.config as config_module


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ', '.join(f'{key} = {toml_value(item)}' for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and not math.isfinite(value):
        return 'nan' if math.isnan(value) else ('inf' if value > 0 else '-inf')
    return json.dumps(value)


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

    def write_config(self, tables=None):
        tables = {'paths': self.paths, 'inference': self.inference, 'resources': self.resources, 'publication': self.publication} if tables is None else tables
        lines = []
        for name, values in tables.items():
            if not isinstance(values, dict):
                lines.append(f'{name} = {toml_value(values)}')
        for name, values in tables.items():
            if isinstance(values, dict):
                lines.append(f'[{name}]')
                lines.extend(f'{key} = {toml_value(value)}' for key, value in values.items())
        self.config_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def loader(self):
        loader = getattr(config_module, 'load_worker_config', None)
        self.assertTrue(callable(loader), 'load_worker_config must expose the explicit settings loader')
        return loader

    def load(self):
        return self.loader()(self.config_path)

    def assert_invalid(self, message):
        loader = self.loader()  # assert_invalid는 설정 읽기 함수를 loader로 먼저 확인한 뒤 잘못된 설정 파일이 예상한 ValueError를 발생시키는지 검사합니다. 설정 읽기 함수 자체의 문제와 설정 값 검증의 실패를 구분하기 위함입니다.
        with self.assertRaisesRegex(ValueError, message):
            loader(self.config_path)

    def external(self, provider='ollama', url='http://localhost:11434'):
        self.inference = {key: self.inference[key] for key in ('mode', 'provider', 'model', 'revision')}
        self.inference.update(mode='external', provider=provider, url=url)

    def test_managed_settings_resolve_paths_and_preserve_exact_model_revision(self):
        self.write_config()
        result = self.load()
        for name in ('WorkerConfig', 'InferenceSettings', 'ResourceSettings'):
            self.assertTrue(isinstance(getattr(config_module, name, None), type), f'{name} must exist')
        self.assertIsInstance(result, config_module.WorkerConfig)
        self.assertIsInstance(result.paths, config_module.WorkerPaths)
        self.assertIsInstance(result.inference, config_module.InferenceSettings)
        self.assertIsInstance(result.resources, config_module.ResourceSettings)
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

    def test_all_settings_and_paths_are_frozen(self):
        self.write_config()
        result = self.load()
        for settings in (result, result.paths, result.inference, result.resources, result.publication):
            for field in fields(settings):
                with self.subTest(settings=type(settings).__name__, field=field.name):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(settings, field.name, None)

    def test_publication_modes_preserve_explicit_declarations_without_provisioning(self):
        remotes = ('https://example.invalid:443/menu.git', str(self.root / 'remote.git'),
                   str(self.root / 'unresolved/../remote.git'))
        for declaration in ({'mode': 'directory'}, *({'mode': 'github', 'repository': 'Example_Owner/menu-site',
                         'workflow': 'publish.yaml', 'remote': remote} for remote in remotes)):
            with self.subTest(declaration=declaration):
                self.publication = declaration
                self.write_config()
                before = set(self.root.rglob('*'))
                try:
                    result = self.load()
                except ValueError as error:
                    self.fail(f'Explicit publication declaration must load: {error}')
                self.assertTrue(hasattr(result, 'publication'), 'Loaded publication settings are missing')
                self.assertTrue(isinstance(getattr(config_module, 'PublicationSettings', None), type), 'PublicationSettings is missing')
                self.assertIsInstance(result.publication, config_module.PublicationSettings)
                self.assertEqual(result.publication.mode, declaration['mode'])
                for name in ('repository', 'workflow', 'remote'):
                    self.assertEqual(getattr(result.publication, name), declaration.get(name))
                self.assertEqual(set(self.root.rglob('*')), before)

    def test_publication_modes_require_exact_keys(self):
        github = {'mode': 'github', 'repository': 'Fixture/menu', 'workflow': 'publish.yml', 'remote': 'https://example.invalid/menu.git'}
        for missing in github:
            with self.subTest(missing=missing):
                self.publication = {name: value for name, value in github.items() if name != missing}
                self.write_config(); self.assert_invalid(missing)
        for base in ({'mode': 'directory'}, github):
            for extra in ('remote' if base['mode'] == 'directory' else 'url', 'extra'):
                with self.subTest(mode=base['mode'], extra=extra):
                    self.publication = {**base, extra: 'unapproved'}
                    self.write_config(); self.assert_invalid(extra)

    def test_publication_rejects_invalid_mode_identity_and_remote(self):
        valid = {'mode': 'github', 'repository': 'Fixture/menu', 'workflow': 'publish.yml', 'remote': 'https://example.invalid/menu.git'}
        invalid = {
            'mode': ('automatic', 'Github', ' github ', '', True, 1, [], {}),
            'repository': ('owner', 'owner/name/extra', '../name', 'owner/..', 'owner/-name', 'owner/name space', ' owner/name', '', True, 1, [], {}),
            'workflow': ('publish', 'dir/publish.yml', 'dir\\publish.yml', '-publish.yml', '.yml', 'publish.YML', ' publish.yml', '', True, 1, [], {}),
            'remote': ('remote.git', '~/remote.git', '--upload-pack=bad', 'http://host/repo.git', 'https://',
                       'https://user:fixture@host/repo.git', 'https://host:bad/repo.git', 'https://host:65536/repo.git',
                       'https://host:/repo.git', 'https://host/repo.git?x=1', 'https://host/repo.git#branch',
                       'https://host/with space', 'https://host/\npath', '', True, 1, [], {}),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    self.publication = {**valid, name: value}
                    self.write_config(); self.assert_invalid(name)

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

    def test_loading_creates_no_directories_or_other_files(self):
        self.write_config()
        before = set(self.root.rglob('*'))
        self.load()
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_relative_paths_are_independent_of_current_directory(self):
        self.write_config()
        original = Path.cwd()
        try:
            os.chdir(self.root)
            result = self.load()
        finally:
            os.chdir(original)
        self.assertEqual(result.paths.checkout_dir, self.root / 'checkout')
        self.assertEqual(result.inference.binary, self.root / 'private/bin/ollama')

    def test_absolute_paths_and_literal_home_environment_components(self):
        self.paths = {'checkout_dir': str(self.root / 'checkout'), 'state_dir': '$HOME/private', 'public_dir': '${HOME}/public'}
        self.inference.update(binary='~/ollama', models_dir='$MODELS', log_dir='${LOGS}')
        self.write_config()
        result = self.load()
        self.assertEqual(result.paths.checkout_dir, self.root / 'checkout')
        self.assertEqual(result.paths.state_dir, self.config_dir / '$HOME/private')
        self.assertEqual(result.paths.public_dir, self.config_dir / '${HOME}/public')
        self.assertEqual(result.inference.binary, self.config_dir / '~/ollama')
        self.assertEqual(result.inference.models_dir, self.config_dir / '$MODELS')
        self.assertEqual(result.inference.log_dir, self.config_dir / '${LOGS}')

    def test_exact_required_root_tables_and_shapes(self):
        valid = {'paths': self.paths, 'inference': self.inference, 'resources': self.resources, 'publication': self.publication}
        for table in valid:
            with self.subTest(missing=table):
                self.write_config({key: value for key, value in valid.items() if key != table})
                self.assert_invalid(table)
            for value in ('bad', 1, True, [], [{'bad': 1}]):
                with self.subTest(table=table, shape=value):
                    self.write_config({**valid, table: value})
                    self.assert_invalid(table)
            with self.subTest(array_of_tables=table):
                self.write_config()
                content = self.config_path.read_text().replace(f'[{table}]', f'[[{table}]]')
                self.config_path.write_text(content)
                self.assert_invalid(table)
        for extra in ('enabled', 'worker'):
            with self.subTest(extra=extra):
                self.write_config({**valid, extra: True})
                self.assert_invalid(extra)

    def test_missing_and_unknown_keys_in_each_table(self):
        for table, values in (('paths', self.paths), ('inference', self.inference), ('resources', self.resources), ('publication', self.publication)):
            for missing in values:
                with self.subTest(table=table, missing=missing):
                    tables = {'paths': self.paths, 'inference': self.inference, 'resources': self.resources, 'publication': self.publication}
                    tables[table] = {key: value for key, value in values.items() if key != missing}
                    self.write_config(tables)
                    self.assert_invalid(missing)
            for unknown in (1, {'nested': 1}):
                with self.subTest(table=table, unknown=unknown):
                    tables = {'paths': self.paths, 'inference': self.inference, 'resources': self.resources, 'publication': self.publication}
                    tables[table] = {**values, 'extra': unknown}
                    self.write_config(tables)
                    self.assert_invalid('extra')

    def test_nested_tables_are_rejected(self):
        for table in ('paths', 'inference', 'resources', 'publication'):
            with self.subTest(table=table):
                self.write_config()
                with self.config_path.open('a') as stream:
                    stream.write(f'[{table}.extra]\nvalue = "bad"\n')
                self.assert_invalid('extra')

    def test_inference_required_strings_are_nonblank_strings(self):
        for key in ('mode', 'provider', 'model', 'revision', 'binary', 'models_dir', 'log_dir'):
            for value in ('', '  ', 1, 1.5, True, [], {}):
                with self.subTest(key=key, value=value):
                    original = self.inference[key]
                    self.inference[key] = value
                    self.write_config()
                    self.assert_invalid(key)
                    self.inference[key] = original

    def test_managed_provider_and_mode_are_exact(self):
        for key, values in (('mode', ('automatic', 'Managed', ' managed ')), ('provider', ('openai', 'other', 'Ollama', ' ollama '))):
            for value in values:
                with self.subTest(key=key, value=value):
                    original = self.inference[key]
                    self.inference[key] = value
                    self.write_config()
                    self.assert_invalid(key)
                    self.inference[key] = original

    def test_managed_url_is_forbidden_even_if_blank(self):
        for value in ('http://localhost:11434', '', None):
            with self.subTest(value=value):
                self.inference['url'] = '' if value is None else value
                self.write_config()
                self.assert_invalid('url')

    def test_external_forbids_each_managed_path_and_requires_url(self):
        self.external()
        for key in ('binary', 'models_dir', 'log_dir'):
            with self.subTest(key=key):
                self.inference[key] = '../private/value'
                self.write_config()
                self.assert_invalid(key)
                del self.inference[key]
        del self.inference['url']
        self.write_config()
        self.assert_invalid('url')

    def test_external_provider_is_exact(self):
        for provider in ('other', 'OpenAI', ' openai ', 'Ollama'):
            with self.subTest(provider=provider):
                self.external(provider)
                self.write_config()
                self.assert_invalid('provider')

    def test_external_url_requires_absolute_http_hostname_valid_port_no_auth_fragment(self):
        urls = ('', '  ', '/v1', '//host/v1', 'ftp://host', 'http://', 'https:///v1', 'http://:11434',
                'http://host:bad', 'http://host:65536', 'http://host:-1', 'http://host:',
                'http://user@host', 'http://user:secret@host', 'http://@host',
                'https://host/#fragment', 'https://host/#', 'http://[::1',
                'http://host with space', ' http://host', 'http://host\n', 1, True, [], {})
        for url in urls:
            with self.subTest(url=url):
                self.external(url=url)
                self.write_config()
                self.assert_invalid('url')

    def test_external_url_rejects_uri_invalid_hostname_characters(self):
        for scheme, character in itertools.product(('http', 'https'), ('|', '\\', '^', '`', '<', '>', '{', '}', '"', '%', '%GG')):
            url = f'{scheme}://bad{character}host:11434'
            with self.subTest(url=url):
                self.external(url=url)
                self.write_config()
                self.assert_invalid('inference.url')

    def test_external_url_rejects_text_after_bracketed_ipv6_host(self):
        for url in ('http://[::1]bad', 'http://[::1]bad:11434'):
            with self.subTest(url=url):
                self.external(url=url)
                self.write_config()
                self.assert_invalid('inference.url')

    def test_external_url_preserves_valid_hostname_syntax(self):
        for url in ('http://model-server.example.test:11434', 'http://127.0.0.1:11434',
                    'https://xn--bcher-kva.example/v1', 'https://bücher.example/v1',
                    'http://model_host:11434', 'http://model%2Dhost:11434'):
            with self.subTest(url=url):
                self.external(url=url)
                self.write_config()
                self.assertEqual(self.load().inference.url, url)

    def test_external_url_accepts_http_https_ipv6_and_port_boundaries(self):
        for url in ('http://localhost:11434', 'https://example.test', 'http://[::1]:8080/v1',
                    'http://host:0', 'https://host:65535/path?query=1'):
            with self.subTest(url=url):
                self.external(url=url)
                self.write_config()
                self.assertEqual(self.load().inference.url, url)

    def test_resources_reject_wrong_platform_and_invalid_numbers(self):
        invalid = {
            'platform': ('windows', 'Linux', ' macos ', '', True, 1, [], {}),
            'min_available_bytes': (0, -1, 1.0, True, False, '1024', [], {}),
            'max_load_per_cpu': (0, -1, 0.0, -0.5, True, False, '1.5', [], {}, float('nan'), float('inf'), -float('inf')),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    original = self.resources[key]
                    self.resources[key] = value
                    self.write_config()
                    self.assert_invalid(key)
                    self.resources[key] = original

    def test_resources_accept_both_platforms_and_positive_integer_float_load(self):
        for platform, load in itertools.product(('linux', 'macos'), (1, 0.01, 1.5)):
            with self.subTest(platform=platform, load=load):
                self.resources.update(platform=platform, max_load_per_cpu=load, min_available_bytes=1)
                self.write_config()
                result = self.load()
                self.assertEqual(result.resources.platform, platform)
                self.assertEqual(result.resources.max_load_per_cpu, load)
                self.assertEqual(result.resources.min_available_bytes, 1)

    def test_managed_binary_existing_file_allowed_directory_rejected(self):
        binary = self.root / 'private/bin/ollama'
        binary.parent.mkdir(parents=True)
        binary.write_text('fixture only')
        self.write_config()
        self.assertEqual(self.load().inference.binary, binary)
        binary.unlink()
        binary.mkdir()
        self.assert_invalid('binary.*file')

    def test_existing_directory_kinds_are_checked_for_all_directory_paths(self):
        existing = self.root / 'wrong-kind'
        existing.write_text('fixture')
        for table, values in (('paths', self.paths), ('inference', self.inference)):
            keys = tuple(values) if table == 'paths' else ('models_dir', 'log_dir')
            for key in keys:
                with self.subTest(table=table, key=key):
                    original = values[key]
                    values[key] = str(existing)
                    self.write_config()
                    self.assert_invalid(key + '.*directory')
                    values[key] = original

    def test_managed_models_logs_cannot_intersect_checkout_or_public_in_any_direction(self):
        original_paths = dict(self.paths)
        original_inference = dict(self.inference)
        for key, boundary, suffix in itertools.product(('models_dir', 'log_dir'), ('checkout_dir', 'public_dir'), ('equal', 'inside', 'ancestor')):
            with self.subTest(key=key, boundary=boundary, relation=suffix):
                self.paths = dict(original_paths)
                self.inference = dict(original_inference)
                if suffix == 'ancestor':
                    # 이 테스트는 상위 경로와의 겹침을 검사하려는 디렉터리 하나를 별도로 배치합니다.
                    # 공통 루트는 다른 보호 대상도 포함하므로 원하는 두 경로의 겹침을 구분할 수 없기 때문입니다.
                    self.paths[boundary] = '../protected/child'
                    self.inference[key] = '../protected'
                else:
                    protected = self.paths[boundary]
                    self.inference[key] = protected if suffix == 'equal' else protected + '/child'
                self.write_config()
                self.assert_invalid(key + '.*' + boundary + '.*overlap')

    def test_managed_binary_cannot_be_in_public_output(self):
        for value in ('../public', '../public/bin/ollama'):
            with self.subTest(value=value):
                self.inference['binary'] = value
                self.write_config()
                self.assert_invalid('binary.*public_dir')

    def test_private_descendants_and_lexical_prefix_siblings_are_allowed(self):
        self.inference.update(binary='../checkout/bin/ollama', models_dir='../public-models', log_dir='../checkout-logs')
        self.write_config()
        self.load()

    def test_symlinks_are_resolved_and_cannot_hide_managed_overlap(self):
        private = self.root / 'private'
        private.mkdir()
        (self.root / 'private-alias').symlink_to(private, target_is_directory=True)
        self.inference.update(binary='../private-alias/bin/ollama', models_dir='../private-alias/models', log_dir='../private-alias/logs')
        self.write_config()
        result = self.load()
        self.assertEqual(result.inference.models_dir, private / 'models')
        self.assertEqual(result.inference.log_dir, private / 'logs')
        self.assertEqual(result.inference.binary, private / 'bin/ollama')
        public = self.root / 'public'
        public.mkdir()
        (self.root / 'public-alias').symlink_to(public, target_is_directory=True)
        for key in ('binary', 'models_dir', 'log_dir'):
            with self.subTest(key=key):
                original = self.inference[key]
                self.inference[key] = '../public-alias/absent-child'
                self.write_config()
                self.assert_invalid(key + '.*public_dir')
                self.inference[key] = original

    def test_symlink_to_wrong_existing_kind_is_rejected(self):
        file = self.root / 'file'
        file.write_text('fixture')
        (self.root / 'alias').symlink_to(file)
        self.inference['models_dir'] = '../alias'
        self.write_config()
        self.assert_invalid('models_dir.*directory')
        directory = self.root / 'directory'
        directory.mkdir()
        (self.root / 'dir-alias').symlink_to(directory, target_is_directory=True)
        self.inference['models_dir'] = '../private/models'
        self.inference['binary'] = '../dir-alias'
        self.write_config()
        self.assert_invalid('binary.*file')

    def test_worker_path_disjoint_protection_is_preserved(self):
        original = dict(self.paths)
        for first, second in itertools.permutations(self.paths, 2):
            for child in ('../shared', '../shared/child'):
                with self.subTest(first=first, second=second, child=child):
                    self.paths = {**original, first: '../shared', second: child}
                    self.write_config()
                    self.assert_invalid('overlap')
        self.paths = original

    def test_invalid_path_strings_are_rejected(self):
        for key in self.paths:
            for value in ('', '  ', 1, True, [], {}):
                with self.subTest(key=key, value=value):
                    original = self.paths[key]
                    self.paths[key] = value
                    self.write_config()
                    self.assert_invalid(key)
                    self.paths[key] = original

    def test_unreadable_and_invalid_toml_have_config_context(self):
        self.assert_invalid('worker.toml')
        for content in (b'\xff', b'[paths', b'[paths]\n[paths]\n'):
            with self.subTest(content=content):
                self.config_path.write_bytes(content)
                self.assert_invalid('worker.toml')

    def test_paths_only_loader_stays_strict_about_extended_config(self):
        self.write_config()
        self.loader()
        with self.assertRaisesRegex(ValueError, 'inference|resources'):
            config_module.load_worker_paths(self.config_path)

    def test_example_loads_without_provisioning(self):
        self.loader()
        example = Path(__file__).resolve().parents[1] / 'config/worker.example.toml'
        self.assertTrue(example.is_file(), 'Explicit worker example must exist')
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
