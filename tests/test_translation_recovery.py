"""모델 번역은 교체 가능한 파생 자료이며 손상된 한 항목이 복구를 막지 않습니다."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mensa.checkpoints import TranslationCheckpointStore
from mensa.errors import GenerationError
from mensa.publication import translation_gaps
from mensa.retention import public_translations
from mensa.translation_contract import source_key, validate_cache
from mensa.translation_runner import resume_translations
from mensa.translation_service import TranslationService


def meal(name):
    source = {'name_de': name, 'components': ['Brot']}
    return {'name_de': name, 'components': [{'name_de': 'Brot'}],
            'translation_key': source_key(source)}


def result(name='Soup'):
    return {'en': {'name': name, 'components': ['Bread']},
            'ko': {'name': '수프', 'components': ['빵']}}


def entry(name):
    source = {'name_de': name, 'components': ['Brot']}
    return {'source': source, **result(), 'origin': 'reviewed-draft',
            'model': 'fixture', 'prompt_version': 'menu-v4'}


class TranslationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.meals = [meal(name) for name in ('Suppe A', 'Suppe B')]
        self.menu = {'days': [{'meals': self.meals}]}
        self.cache = {'schema_version': 1, 'entries': {
            item['translation_key']: entry(item['name_de']) for item in self.meals}}
        self.calls = []

    def generate(self, source, config):
        self.calls.append(source['name_de'])
        return result('Fresh soup')

    def build(self, cache, *, config=None, generate=None, phrases=None):
        return TranslationService(glossary_provider=lambda: {},
            translate=generate or self.generate).build(self.menu, cache, phrases or {}, config)

    def test_each_unusable_cached_entry_is_regenerated_without_losing_healthy_sibling(self):
        key = self.meals[0]['translation_key']
        mutations = [lambda e: e.pop('ko'),
                     lambda e: e['source'].update(name_de='Wrong source'),
                     lambda e: e['en'].update(components=[]),
                     lambda e: e['ko'].update(name='바미 고<0xEB><0xA0><0x9D> 국수')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                broken = copy.deepcopy(self.cache)
                mutate(broken['entries'][key])
                before = copy.deepcopy(broken)
                self.calls.clear()
                repaired = self.build(broken, config={'model': 'fixture'})
                self.assertEqual(self.calls, ['Suppe A'])
                self.assertEqual(repaired['entries'][key]['en']['name'], 'Fresh soup')
                other = self.meals[1]['translation_key']
                self.assertEqual(repaired['entries'][other], self.cache['entries'][other])
                self.assertEqual(broken, before)
                validate_cache(repaired)

    def test_unusable_cache_without_model_is_missing_for_source_fallback(self):
        key = self.meals[0]['translation_key']
        self.cache['entries'][key]['ko']['name'] = '<0xED><0x95><0x9C>'
        repaired = self.build(self.cache)
        self.assertNotIn(key, repaired['entries'])
        self.assertIn(self.meals[1]['translation_key'], repaired['entries'])
        self.assertEqual(self.calls, [])

    def test_unused_bad_entry_and_notice_do_not_block_healthy_cache(self):
        self.cache['entries']['broken-history'] = {'ko': 'invalid'}
        self.cache['notices'] = {'Old notice': {'en': 'Incomplete'}}
        repaired = self.build(self.cache)
        self.assertNotIn('broken-history', repaired['entries'])
        self.assertEqual(repaired['notices'], {})
        self.assertEqual(len(repaired['entries']), 2)

    def test_bad_model_result_is_omitted_and_later_items_still_progress(self):
        first = self.meals[0]['name_de']
        for invalid in ({'en': {'name': 'Missing Korean', 'components': []}},
                        GenerationError('invalid_result'), GenerationError('invalid_json')):
            with self.subTest(invalid=invalid):
                def generate(source, config):
                    if source['name_de'] == first:
                        if isinstance(invalid, Exception):
                            raise invalid
                        return copy.deepcopy(invalid)
                    return result()
                repaired = self.build({'schema_version': 1, 'entries': {}},
                                      config={'model': 'fixture'}, generate=generate)
                self.assertNotIn(self.meals[0]['translation_key'], repaired['entries'])
                self.assertIn(self.meals[1]['translation_key'], repaired['entries'])
                validate_cache(repaired)

    def test_valid_phrase_variants_do_not_have_to_match_prescribed_wording(self):
        for name, translated in [('Wikingertopf', 'Meatballs in a creamy sauce'),
                                 ('Vegan Wikingertopf', 'Vegan Wikingertopf stew')]:
            with self.subTest(name=name):
                candidate = entry(name)
                candidate['en']['name'] = translated
                candidate['source']['components'] = ['Peperoni']
                candidate['en']['components'] = ['Hot peppers']
                candidate['ko']['components'] = ['매운 고추']
                validate_cache({'schema_version': 1, 'entries': {
                    source_key(candidate['source']): candidate}})

    def test_resume_uses_healthy_private_entry_when_public_counterpart_is_damaged(self):
        public = copy.deepcopy(self.cache)
        public['entries'][self.meals[0]['translation_key']].pop('ko')
        saved = []
        repaired = resume_translations(self.menu, public, {}, None,
            glossary_provider=lambda: {}, translate=self.generate,
            load_checkpoint=lambda: self.cache, save_checkpoint=saved.append)
        self.assertEqual(repaired['entries'], self.cache['entries'])
        self.assertEqual(saved[-1], repaired)

    def test_checkpoint_read_recovers_records_and_atomic_save_persists_repair(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / 'translation-checkpoint.json'
            broken = copy.deepcopy(self.cache)
            broken['entries'][self.meals[0]['translation_key']].pop('ko')
            raw = json.dumps(broken).encode()
            path.write_bytes(raw)
            store = TranslationCheckpointStore(root)
            recovered = store.load()
            self.assertNotIn(self.meals[0]['translation_key'], recovered['entries'])
            self.assertEqual(path.read_bytes(), raw)
            store.save(recovered)
            self.assertEqual(store.load(), recovered)

    def test_invalid_container_and_authentication_are_not_silently_discarded(self):
        for invalid in ({'schema_version': 99, 'entries': {}},
                        {'schema_version': 1, 'entries': []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.build(invalid)
        with self.assertRaises(GenerationError):
            self.build({'schema_version': 1, 'entries': {}}, config={'model': 'fixture'},
                generate=lambda *_: (_ for _ in ()).throw(GenerationError('authentication')))

    def test_projection_and_gap_detection_treat_unusable_records_as_missing(self):
        key = self.meals[0]['translation_key']
        self.cache['entries'][key].pop('ko')
        self.cache['entries']['irrelevant-bad-history'] = None
        self.meals[0]['notices'] = ['New notice']
        self.cache['notices'] = {'New notice': {'en': 'Incomplete'}}
        projected = public_translations(self.menu, self.cache)
        self.assertEqual(set(projected['entries']), {self.meals[1]['translation_key']})
        self.assertEqual(projected['notices'], {})
        self.assertEqual(translation_gaps(self.menu, self.cache),
                         {'entries': (key,), 'notices': ('New notice',)})

    def test_bad_notice_is_omitted_without_blocking_dish_translation(self):
        self.meals[0]['notices'] = ['Unknown source notice']
        for bad in ('<0xED><0x95><0x9C>', 'Invalid\nnotice'):
            with self.subTest(bad=bad):
                def generate(source, config):
                    if source.get('kind') == 'notice':
                        return {lang: {'name': bad, 'components': []} for lang in ('en', 'ko')}
                    return result()
                cache = self.build({'schema_version': 1, 'entries': {},
                    'notices': {'Unused notice': {'en': bad, 'ko': bad}}},
                    config={'model': 'fixture'}, generate=generate)
                self.assertEqual(cache['notices'], {})
                self.assertEqual(len(cache['entries']), 2)

    def test_deeply_malformed_history_is_discarded_before_copying_healthy_siblings(self):
        nested = []
        for _ in range(600):
            nested = [nested]
        self.cache['entries']['bad-history'] = nested
        cache = json.loads(json.dumps(self.cache))
        repaired = self.build(cache)
        self.assertNotIn('bad-history', repaired['entries'])
        self.assertEqual(len(repaired['entries']), 2)
