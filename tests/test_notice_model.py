"""새 안내 문구를 모델로 번역하고, 실제 호출 오류를 갱신 성공으로 숨기지 않는지 검사합니다."""

from contextlib import contextmanager
import io
import json
import unittest
from unittest.mock import patch

from mensa import generation
from mensa.errors import GenerationError
from scripts import local_refresh as worker
from scripts.translations import build_translations
from test_translations import sample_meal


LABEL = 'Kann Spuren von Krebstieren enthalten'
RESULT = {'en': {'name': 'May contain traces of crustaceans', 'components': []},
          'ko': {'name': '갑각류가 미량 함유될 수 있음', 'components': []}}


class NoticeModelTests(unittest.TestCase):
    def setUp(self):
        self.meal = sample_meal()
        self.menu = {'days': [{'meals': [self.meal]}]}
        phrases = {'Schupfnudeln': {'en': 'Potato dumplings', 'ko': '감자 경단'},
                   'Gemüse': {'en': 'Vegetables', 'ko': '채소'}}
        self.cache = build_translations(self.menu, {}, phrases, None)
        self.meal['notices'] = [LABEL]

    def test_mac_starts_model_for_notice_when_all_dishes_are_cached(self):
        @contextmanager
        def runtime(_):
            yield {'model': worker.MODEL}
        with patch.object(worker, 'local_model', side_effect=runtime) as start, \
                patch('scripts.translations.request_translation', return_value=RESULT) as translate:
            result = worker.RefreshJob('/unused').translate(self.menu, self.cache, {})
        start.assert_called_once()
        self.assertEqual(translate.call_args.args[0], {'kind': 'notice', 'name_de': LABEL, 'components': []})
        self.assertEqual(result['notices'][LABEL]['ko'], RESULT['ko']['name'])
        self.assertEqual(result['entries'], self.cache['entries'])

    def test_mac_notice_model_start_failure_propagates_for_worker_retry(self):
        with patch.object(worker, 'local_model', side_effect=GenerationError('network')):
            with self.assertRaises(GenerationError) as error:
                worker.RefreshJob('/unused').translate(self.menu, self.cache, {})
        self.assertTrue(error.exception.retryable)
        self.assertEqual(self.meal['notices'], [LABEL])

    def test_mac_does_not_hide_model_start_failure_for_untranslated_dish(self):
        with patch.object(worker, 'local_model', side_effect=GenerationError('network')):
            with self.assertRaises(GenerationError):
                worker.RefreshJob('/unused').translate(self.menu, {'schema_version': 1, 'entries': {}}, {})

    def test_notice_request_uses_specific_prompt_and_provider_schema(self):
        for provider in ('openai', 'ollama'):
            with self.subTest(provider=provider):
                content = json.dumps(RESULT)
                outer = ({'done': True, 'done_reason': 'stop', 'message': {'content': content}}
                         if provider == 'ollama' else
                         {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
                response = io.BytesIO(json.dumps(outer).encode())
                with patch.object(generation.urllib.request.OpenerDirector, 'open', return_value=response) as request:
                    result = generation.request_translation(
                        {'kind': 'notice', 'name_de': LABEL, 'components': []},
                        {'provider': provider, 'url': 'http://127.0.0.1/api/chat', 'model': 'fixture'})
                self.assertEqual(result, RESULT)
                payload = json.loads(request.call_args.args[0].data)
                self.assertIn('preserve all negations', payload['messages'][0]['content'])
                self.assertEqual(json.loads(payload['messages'][1]['content'])['name_de'], LABEL)
                if provider == 'ollama':
                    self.assertEqual(payload['format']['properties']['ko']['properties']['components']['maxItems'], 0)


if __name__ == '__main__':
    unittest.main()
