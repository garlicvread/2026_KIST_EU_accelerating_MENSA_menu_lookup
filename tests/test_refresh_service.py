"""이 테스트 모듈은 RefreshService가 메뉴 수집·번역·검증·게시의 요구 조건을 지키는지 확인합니다. 각 단계를 명시적으로 전달한 메모리의 함수로 실행하여 호출 순서와 데이터를 검사합니다."""

import copy
import importlib
import importlib.util
import unittest

from mensa.publication import validate_publication
from mensa.translation_service import TranslationService
from scripts.menu_source import parse_menu
from scripts.translations import source_for
from test_menu_source import meal, page
from test_publication import translation_entry


spec = importlib.util.find_spec('mensa.refresh_service')
refresh_module = importlib.import_module('mensa.refresh_service') if spec else None


class RefreshServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(refresh_module, 'RefreshService application module is missing')
        self.service_type = getattr(refresh_module, 'RefreshService', None)
        self.assertTrue(callable(self.service_type), 'RefreshService callable is missing')
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.glossary = {
            'vegan': {'en': 'Vegan', 'ko': '비건'},
            'Weizen': {'en': 'Wheat', 'ko': '밀'},
            'Sellerie': {'en': 'Celery', 'ko': '셀러리'},
            'Milch und Laktose': {'en': 'Milk and lactose', 'ko': '우유 및 유당'},
        }
        self.cache = {'schema_version': 1, 'entries': {}}
        self.candidate = {
            'schema_version': 1,
            'entries': {record['translation_key']: translation_entry(source_for(record))},
            'notices': copy.deepcopy(self.glossary),
        }
        self.events = []
        self.checkpoint = lambda cache: None
        self.ports = {
            'source': lambda: self.record('source', self.menu),
            'coverage_guard': lambda menu: self.record('coverage_guard'),
            'translate': lambda menu, cache, phrases, *, checkpoint=None: self.record('translate', self.candidate),
            'validator': lambda menu, candidate: self.record('validator'),
            'publisher': lambda menu, candidate, *, previous: self.record('publisher'),
        }

    def record(self, stage, result=None):
        self.events.append(stage)
        return result

    def service(self, **overrides):
        return self.service_type(**{**self.ports, **overrides})

    def test_constructor_requires_five_explicit_callable_ports_without_invocation(self):
        self.service()
        self.assertEqual(self.events, [])
        for name in self.ports:
            with self.subTest(port=name), self.assertRaisesRegex(TypeError, name):
                self.service(**{name: None})
            missing = dict(self.ports)
            del missing[name]
            with self.subTest(missing=name), self.assertRaises(TypeError):
                self.service_type(**missing)
        self.assertEqual(self.events, [])

    def test_ordered_success_forwards_owned_inputs_and_returns_port_objects(self):
        previous = copy.deepcopy(self.menu)
        phrases = {'retained': {'en': 'Retained', 'ko': '보존'}}
        def coverage(menu):
            self.assertIs(menu, self.menu)
            self.record('coverage_guard')
        def translate(menu, cache, supplied_phrases, *, checkpoint):
            self.assertIs(menu, self.menu)
            self.assertIs(cache, self.cache)
            self.assertIs(supplied_phrases, phrases)
            self.assertIs(checkpoint, self.checkpoint)
            return self.record('translate', self.candidate)
        def validator(menu, candidate):
            self.assertIs(menu, self.menu)
            self.assertIs(candidate, self.candidate)
            self.record('validator')
        def publish(menu, candidate, *, previous):
            self.assertIs(menu, self.menu)
            self.assertIs(candidate, self.candidate)
            self.assertIs(previous, original_previous)
            self.record('publisher')
        original_previous = previous
        result = self.service(coverage_guard=coverage, translate=translate, validator=validator,
                              publisher=publish).refresh(previous, self.cache, phrases,
                                                         checkpoint=self.checkpoint)
        self.assertIs(result[0], self.menu)
        self.assertIs(result[1], self.candidate)
        self.assertEqual(self.events, list(self.ports))

    def test_invalid_checkpoint_is_rejected_before_every_port(self):
        for checkpoint in (False, 1, {}, 'callback'):
            with self.subTest(checkpoint=checkpoint), self.assertRaisesRegex(TypeError, 'checkpoint'):
                self.service().refresh(None, self.cache, {}, checkpoint=checkpoint)
        self.assertEqual(self.events, [])

    def test_default_checkpoint_is_forwarded_as_none(self):
        checkpoints = []
        def translate(menu, cache, phrases, *, checkpoint):
            checkpoints.append(checkpoint)
            return self.candidate
        self.service(translate=translate).refresh(None, self.cache, {})
        self.assertEqual(checkpoints, [None])

    def test_real_menu_validation_precedes_coverage_and_generation(self):
        for menu in (None, {}, {'coverage': self.menu['coverage']}):
            self.events.clear()
            self.menu = menu
            with self.subTest(menu=menu), self.assertRaises(ValueError):
                self.service().refresh(None, self.cache, {})
            self.assertEqual(self.events, ['source'])

    def test_real_previous_validation_precedes_coverage_and_generation(self):
        with self.assertRaises(ValueError):
            self.service().refresh({}, self.cache, {})
        self.assertEqual(self.events, ['source'])

    def test_previous_price_loss_stops_before_generation(self):
        previous = copy.deepcopy(self.menu)
        self.menu = parse_menu(page(meal(prices='')), '2026-09-21T10:00:00Z')
        with self.assertRaisesRegex(ValueError, 'prices disappeared'):
            self.service().refresh(previous, self.cache, {})
        self.assertEqual(self.events, ['source'])

    def test_each_port_failure_propagates_and_stops_later_ports(self):
        stages = list(self.ports)
        for index, stage in enumerate(stages):
            self.events.clear()
            error = RuntimeError(stage)
            def broken(*args, **kwargs):
                self.record(stage)
                raise error
            with self.subTest(stage=stage), self.assertRaises(RuntimeError) as raised:
                self.service(**{stage: broken}).refresh(None, self.cache, {})
            self.assertIs(raised.exception, error)
            self.assertEqual(self.events, stages[:index + 1])

    def test_incomplete_meal_candidate_never_reaches_publisher(self):
        self.candidate['entries'].clear()
        def validate(menu, candidate):
            self.record('validator')
            validate_publication(menu, candidate, notice_glossary=self.glossary)
        with self.assertRaisesRegex(ValueError, 'Missing meal translation'):
            self.service(validator=validate).refresh(None, self.cache, {})
        self.assertEqual(self.events, ['source', 'coverage_guard', 'translate', 'validator'])

    def test_incomplete_notice_candidate_never_reaches_publisher(self):
        del self.candidate['notices']['Sellerie']
        with self.assertRaisesRegex(ValueError, 'Missing notice translation'):
            self.service(validator=lambda menu, candidate: validate_publication(
                menu, candidate, notice_glossary=self.glossary)).refresh(None, self.cache, {})
        self.assertNotIn('publisher', self.events)

    def test_real_translation_service_preserves_inputs_and_checkpoint_ownership(self):
        record = self.menu['days'][0]['meals'][0]
        phrases = {text: {'en': 'Vegetable dish', 'ko': '채소 요리'} for text in
                   [record['name_de'], *(component['name_de'] for component in record['components'])]}
        previous = copy.deepcopy(self.menu)
        before = copy.deepcopy((self.menu, previous, self.cache, phrases))
        translations = TranslationService(glossary_provider=lambda: self.glossary,
                                          translate=lambda *args: self.fail('Unexpected generation'))
        checkpoints = []
        def checkpoint(cache):
            checkpoints.append(cache)
            cache['entries'].clear()
        def translate(menu, cache, phrases, *, checkpoint):
            return translations.build(menu, cache, phrases, None, checkpoint=checkpoint)
        result = self.service(translate=translate, validator=lambda menu, candidate: validate_publication(
            menu, candidate, notice_glossary=self.glossary)).refresh(
                previous, self.cache, phrases, checkpoint=checkpoint)
        self.assertEqual((self.menu, previous, self.cache, phrases), before)
        self.assertIsNot(result[1], self.cache)
        self.assertEqual(len(result[1]['entries']), 1)
        self.assertEqual(len(checkpoints), 1)
        result[1]['entries'].clear()
        self.assertEqual(self.cache, before[2])


if __name__ == '__main__':
    unittest.main()
