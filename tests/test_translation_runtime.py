"""이 테스트 모듈은 설정한 모델 실행과 번역 재개 코드가 함께 동작하는지 확인하기 위해 실제 번역 규칙과 임시 비공개 JSON 파일을 사용합니다."""

import copy
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mensa.checkpoints import TranslationCheckpointStore
from mensa.config import InferenceSettings
from mensa.errors import GenerationError
from mensa.model_runtime import model_session
from mensa.translation_contract import PROMPT_VERSION, generation_identity, source_key

from mensa import translation_runtime as adapter


class TranslationRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = self.base / 'private'
        self.settings = InferenceSettings('managed', 'ollama', 'fixture:1', 'immutable-r1',
                                          None, self.base / 'binary', self.base / 'models', self.base / 'logs')
        self.glossary = {'Weizen': {'en': 'Wheat', 'ko': '밀'}}
        self.sources = [{'name_de': name, 'components': ['Brot']} for name in ('Suppe A', 'Suppe B')]
        self.menu = {'days': [{'meals': [self.meal(source) for source in self.sources]}]}
        self.events = []
        self.requests = []
        self.url = 'http://127.0.0.1:43210/api/chat'
        self.key = ' SECRET fixture key '

    def meal(self, source):
        return {'name_de': source['name_de'], 'components': [
            {'name_de': name, 'notices': ['Weizen']} for name in source['components']],
            'translation_key': source_key(source)}

    def result(self, name='Generated soup'):
        return {'en': {'name': name, 'components': ['Bread']},
                'ko': {'name': '수프', 'components': ['빵']}}

    def identity(self, settings=None):
        settings = settings or self.settings
        return generation_identity({'provider': settings.provider, 'model': settings.model,
                                    'backend_revision': settings.revision}, notice_glossary=self.glossary)

    def entry(self, source, *, name='Cached soup', origin='model', settings=None):
        entry = {'source': copy.deepcopy(source), **self.result(name), 'origin': origin,
                 'model': self.settings.model if origin == 'model' else 'assistant-draft',
                 'prompt_version': PROMPT_VERSION}
        if origin == 'model':
            entry['generation_identity'] = self.identity(settings)
        return entry

    def cache(self, *entries):
        return {'schema_version': 1, 'entries': {source_key(e['source']): e for e in entries},
                'notices': copy.deepcopy(self.glossary)}

    @contextmanager
    def runtime(self, settings, *, key):
        self.events.append('open')
        try:
            yield {'provider': settings.provider, 'model': settings.model, 'url': self.url,
                   'key': key, 'backend_revision': settings.revision}
        finally:
            self.events.append('close')

    def generate(self, source, config):
        self.requests.append(copy.deepcopy((source, config)))
        self.events.append(source['name_de'])
        return self.result('Generated ' + source['name_de'])

    def run_adapter(self, published=None, phrases=None, settings=None, **ports):
        supplied = {'state_dir': self.state, 'glossary_provider': lambda: self.glossary,
                    'translate': self.generate, 'runtime': self.runtime, 'key': self.key}
        supplied.update(ports)
        return adapter.resume_configured_translations(self.menu, published, phrases or {},
                                                       settings or self.settings, **supplied)

    def stored(self):
        return TranslationCheckpointStore(self.state).load()

    def test_lazy_single_session_saves_real_json_and_closes_before_return(self):
        final = self.run_adapter()
        self.assertEqual(self.events, ['open', 'Suppe A', 'Suppe B', 'close'])
        self.assertEqual(self.stored(), final)
        self.assertEqual(set(final['entries']), {source_key(s) for s in self.sources})
        for entry in final['entries'].values():
            self.assertEqual(entry['generation_identity'], self.identity())
        self.assertTrue(all(config['key'] == self.key for _, config in self.requests))
        self.assertNotIn(self.key, (self.state / 'translation-checkpoint.json').read_text())

    def test_interruption_then_changed_managed_url_resumes_only_missing_entry(self):
        failure = GenerationError('timeout')
        def interrupted(source, config):
            if source['name_de'] == 'Suppe B':
                raise failure
            return self.generate(source, config)
        with self.assertRaises(GenerationError) as raised:
            self.run_adapter(translate=interrupted)
        self.assertIs(raised.exception, failure)
        self.assertEqual(set(self.stored()['entries']), {source_key(self.sources[0])})
        self.assertEqual(self.events[-1], 'close')
        self.events.clear()
        self.requests.clear()
        self.url = 'http://127.0.0.1:54321/api/chat'
        final = self.run_adapter()
        self.assertEqual([source['name_de'] for source, _ in self.requests], ['Suppe B'])
        self.assertEqual(self.stored(), final)
        self.assertEqual(self.requests[0][1]['url'], self.url)

    def test_changed_revision_regenerates_incompatible_private_model_entries(self):
        TranslationCheckpointStore(self.state).save(self.cache(*(self.entry(s) for s in self.sources)))
        current = replace(self.settings, revision='immutable-r2')
        final = self.run_adapter(settings=current)
        self.assertEqual([s['name_de'] for s, _ in self.requests], ['Suppe A', 'Suppe B'])
        self.assertTrue(all(e['generation_identity'] == self.identity(current) for e in final['entries'].values()))

    def test_all_current_private_or_public_history_saves_final_without_runtime(self):
        history = self.cache(*(self.entry(s) for s in self.sources))
        for location in ('private', 'public'):
            with self.subTest(location=location):
                state_dir = self.base / f'{location}-private'
                store = TranslationCheckpointStore(state_dir)
                self.assertIsNone(store.load(), 'Each reuse variant must start without private history')
                if location == 'private':
                    store.save(history)
                forbidden = Mock(side_effect=AssertionError('Lifecycle or generation must stay lazy'))
                callbacks = []
                final = self.run_adapter(history if location == 'public' else None,
                                         state_dir=state_dir, runtime=forbidden, translate=forbidden,
                                         checkpoint=callbacks.append)
                self.assertEqual(final, history)
                self.assertEqual(store.load(), history)
                self.assertEqual(callbacks, [final])
                forbidden.assert_not_called()


    def test_empty_menu_still_saves_final_without_runtime(self):
        self.menu = {'days': []}
        forbidden = Mock(side_effect=AssertionError('Empty work must stay lazy'))
        final = self.run_adapter(runtime=forbidden, translate=forbidden)
        self.assertEqual(final['entries'], {})
        self.assertEqual(self.stored(), final)
        forbidden.assert_not_called()

    def test_invalid_settings_key_or_state_path_blocks_before_history_and_ports(self):
        invalids = [None, {}, replace(self.settings, mode='unknown'), replace(self.settings, provider='openai'),
                    replace(self.settings, model=' '), replace(self.settings, revision=''),
                    replace(self.settings, url='unexpected'), replace(self.settings, binary=Path('relative')),
                    replace(self.settings, mode='external', url=' ', binary=None, models_dir=None, log_dir=None)]
        forbidden = Mock(side_effect=AssertionError('Invalid preconditions must not load or call ports'))
        with patch.object(TranslationCheckpointStore, 'load', forbidden):
            for settings in invalids:
                with self.subTest(settings=settings), self.assertRaises(GenerationError) as raised:
                    adapter.resume_configured_translations(self.menu, None, {}, settings,
                        state_dir=self.state, glossary_provider=forbidden, translate=forbidden, runtime=forbidden)
                self.assertEqual(raised.exception.code, 'invalid_config')
                self.assertFalse(raised.exception.retryable)
            for key in (None, False, {}, 1):
                with self.subTest(key=key), self.assertRaises(GenerationError):
                    self.run_adapter(key=key, glossary_provider=forbidden, runtime=forbidden, translate=forbidden)
            for state in (None, 'relative', Path('relative')):
                with self.subTest(state=state), self.assertRaises(ValueError):
                    self.run_adapter(state_dir=state, glossary_provider=forbidden)
        forbidden.assert_not_called()

    def test_noncallable_ports_rejected_before_history_load(self):
        forbidden = Mock(side_effect=AssertionError('Port validation must precede history'))
        with patch.object(TranslationCheckpointStore, 'load', forbidden):
            for name in ('runtime', 'translate', 'glossary_provider', 'checkpoint'):
                with self.subTest(name=name), self.assertRaisesRegex(TypeError, name):
                    self.run_adapter(**{name: False})
        forbidden.assert_not_called()

    def test_corrupt_private_json_blocks_before_glossary_and_runtime(self):
        self.state.mkdir(mode=0o700)
        path = self.state / 'translation-checkpoint.json'
        path.write_text('{invalid JSON')
        forbidden = Mock(side_effect=AssertionError('Corrupt history must block ports'))
        with self.assertRaises(ValueError):
            self.run_adapter(glossary_provider=forbidden, runtime=forbidden, translate=forbidden)
        self.assertEqual(path.read_text(), '{invalid JSON')
        forbidden.assert_not_called()

    def test_runtime_mapping_mismatch_unwinds_before_request_or_save(self):
        correct = {'provider': self.settings.provider, 'model': self.settings.model,
                   'backend_revision': self.settings.revision, 'url': self.url, 'key': self.key}
        invalids = [None, [], *({**correct, name: value} for name, value in (
            ('provider', 'openai'), ('model', 'wrong'), ('backend_revision', 'wrong'),
            ('url', ''), ('url', None), ('key', 'changed')))]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                self.events.clear()
                @contextmanager
                def mismatch(settings, *, key):
                    self.events.append('open')
                    try:
                        yield invalid
                    finally:
                        self.events.append('close')
                with self.assertRaises(GenerationError) as raised:
                    self.run_adapter(runtime=mismatch)
                self.assertEqual(raised.exception.code, 'invalid_config')
                self.assertFalse(raised.exception.retryable)
                self.assertNotIn('SECRET', str(raised.exception))
                self.assertEqual(self.events, ['open', 'close'])
                self.assertFalse(self.state.exists())
                self.assertEqual(self.requests, [])

    def test_save_then_isolated_callback_cannot_change_final_or_saved_history(self):
        callbacks = []
        def callback(cache):
            on_disk = self.stored()
            self.assertEqual(cache, on_disk)
            callbacks.append(copy.deepcopy(cache))
            cache['entries'].clear()
            cache['notices']['Weizen']['en'] = 'Mutation'
        final = self.run_adapter(checkpoint=callback)
        self.assertEqual([len(cache['entries']) for cache in callbacks], [1, 2, 2])
        self.assertEqual(final, callbacks[-1])
        self.assertEqual(self.stored(), final)

    def test_generator_and_runtime_result_mutation_cannot_change_future_requests_or_identity(self):
        active = {'provider': self.settings.provider, 'model': self.settings.model,
                  'backend_revision': self.settings.revision, 'url': self.url, 'key': self.key,
                  'nested': {'value': 'original'}}
        original = copy.deepcopy((self.menu, self.glossary))
        @contextmanager
        def runtime(settings, *, key):
            yield active
        def mutate(source, config):
            self.assertEqual(config, {**active, 'provider': self.settings.provider,
                'model': self.settings.model, 'backend_revision': self.settings.revision,
                'url': self.url, 'key': self.key, 'nested': {'value': 'original'}})
            self.requests.append(copy.deepcopy((source, config)))
            source['name_de'] = 'Changed'
            source['components'].clear()
            config.update(model='Changed', backend_revision='Changed', key='Changed', url='Changed')
            config['nested']['value'] = 'Changed'
            active.update(model='External mutation', key='External mutation')
            active['nested']['value'] = 'External mutation'
            return self.result()
        final = self.run_adapter(runtime=runtime, translate=mutate)
        self.assertEqual((self.menu, self.glossary), original)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.stored(), final)
        for entry in final['entries'].values():
            self.assertEqual(entry['model'], self.settings.model)
            self.assertEqual(entry['generation_identity'], self.identity())

    def test_save_failure_unwinds_and_skips_callback_and_next_generation(self):
        failure = OSError('fixture save failure')
        callback = Mock()
        with patch.object(TranslationCheckpointStore, 'save', side_effect=failure):
            with self.assertRaises(OSError) as raised:
                self.run_adapter(checkpoint=callback)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.events, ['open', 'Suppe A', 'close'])
        self.assertFalse(self.state.exists())
        callback.assert_not_called()

    def test_callback_failure_unwinds_but_accepted_json_resumes_next_call(self):
        failure = RuntimeError('fixture callback failure')
        def callback(cache):
            self.assertEqual(self.stored(), cache)
            raise failure
        with self.assertRaises(RuntimeError) as raised:
            self.run_adapter(checkpoint=callback)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.events, ['open', 'Suppe A', 'close'])
        self.assertEqual(set(self.stored()['entries']), {source_key(self.sources[0])})
        self.requests.clear()
        self.run_adapter()
        self.assertEqual([s['name_de'] for s, _ in self.requests], ['Suppe B'])

    def test_actual_managed_session_with_fake_ports_closes_log_and_owned_process(self):
        self.settings.binary.touch()
        self.settings.models_dir.mkdir()
        process = Mock(pid=123456)
        process.poll.return_value = None
        launch = Mock(return_value=process)
        stop = Mock()
        def runtime(settings, *, key):
            return model_session(settings, key=key, launch=launch, choose_port=lambda: 43210,
                probe=lambda host, timeout: {'models': [{'name': settings.model, 'digest': settings.revision}]},
                clock=lambda: 0.0, wait=lambda seconds: self.fail('Ready fixture must not wait'), stop=stop)
        final = self.run_adapter(runtime=runtime)
        self.assertEqual(self.stored(), final)
        launch.assert_called_once()
        stop.assert_called_once_with(process)
        self.assertTrue(launch.call_args.kwargs['stdout'].closed)

    def test_external_config_uses_actual_session_without_managed_files(self):
        external = replace(self.settings, mode='external', provider='openai',
            url='https://fixture.invalid/v1/chat/completions', binary=None, models_dir=None, log_dir=None)
        final = self.run_adapter(settings=external, runtime=model_session)
        self.assertEqual(self.stored(), final)
        self.assertTrue(all(config['url'] == external.url for _, config in self.requests))
        self.assertFalse((self.base / 'logs').exists())


if __name__ == '__main__':
    unittest.main()
