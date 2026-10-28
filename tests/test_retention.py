"""보관 테스트가 달력 경계·공개 부분집합·사용 기록과 유한 캐시 한도를 검사합니다."""

import copy
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import unittest

from mensa import retention
from mensa.config import RetentionSettings
from mensa.menu_contract import validate_menu
from mensa.notice_contract import source_notices
from mensa.releases import _json_bytes, _release_id
from mensa.translation_contract import source_key, validate_cache


def entry(name, *, origin='model'):
    source = {'name_de': name, 'components': []}
    return source_key(source), {
        'source': source,
        'en': {'name': name, 'components': []},
        'ko': {'name': name, 'components': []},
        'origin': origin, 'model': 'fixture-model', 'prompt_version': 'menu-v4',
    }


def cache_for(*names):
    return {'schema_version': 1, 'entries': dict(entry(name) for name in names), 'notices': {}}


def usage_for(cache, last_used='2026-01-01', *, pinned=()):
    return {'schema_version': 1, **{
        kind: {key: {'last_used': last_used, 'pinned': key in pinned}
               for key in cache.get(kind, {})}
        for kind in ('entries', 'notices')
    }}


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.policy = RetentionSettings()

    def prune(self, cache, usage=None, **kwargs):
        arguments = dict(required_keys=set(), required_notices=set(), observed_keys=set(),
                         observed_notices=set(), today=date(2026, 10, 28), policy=self.policy)
        arguments.update(kwargs)
        return retention.prune_cache(cache, usage, **arguments)

    def test_menu_retains_inclusive_calendar_boundary_today_and_all_future_dates(self):
        for today in (date(2026, 10, 26), date(2026, 3, 30), date(2027, 1, 2), date(2024, 3, 1)):
            with self.subTest(today=today):
                dates = [today - timedelta(days=31), today - timedelta(days=30),
                         today, today + timedelta(days=400)]
                menu = {'schema_version': 1, 'source': {'sha256': 'preserved'},
                        'coverage': {'start': dates[0].isoformat(), 'end': dates[-1].isoformat()},
                        'days': [{'date': day.isoformat(), 'meals': []} for day in dates]}
                original = copy.deepcopy(menu)
                result = retention.retain_menu(menu, today.isoformat(), self.policy)
                self.assertEqual([day['date'] for day in result['days']], [day.isoformat() for day in dates[1:]])
                self.assertEqual(result['coverage'], {'start': dates[1].isoformat(), 'end': dates[-1].isoformat()})
                self.assertEqual(result['source'], menu['source'])
                result['source']['sha256'] = 'changed'
                self.assertEqual(menu, original)

    def test_empty_retained_menu_fails_without_mutating_input(self):
        menu = {'schema_version': 1, 'source': {}, 'coverage': {'start': '2020-01-01', 'end': '2020-01-01'},
                'days': [{'date': '2020-01-01', 'meals': []}]}
        with self.assertRaisesRegex(ValueError, 'retained menu|No menu'):
            retention.retain_menu(menu, date(2026, 10, 28), self.policy)
        self.assertEqual(menu['days'][0]['date'], '2020-01-01')

    def test_retained_real_menu_preserves_source_hash_same_day_prices_and_validator(self):
        fixture = Path(__file__).parent / 'fixtures/menu-data/menu.json'
        menu = json.loads(fixture.read_text())
        today = date.fromisoformat(menu['days'][0]['date']) + timedelta(days=31)
        result = retention.retain_menu(menu, today, self.policy)
        cutoff = (today - timedelta(days=30)).isoformat()
        self.assertEqual(result['days'], [day for day in menu['days'] if day['date'] >= cutoff])
        self.assertEqual(result['source'], menu['source'])
        validate_menu(result)

    def test_public_cache_is_exact_reference_subset_without_private_metadata(self):
        fixture = Path(__file__).parent / 'fixtures/menu-data'
        menu = json.loads((fixture / 'menu.json').read_text())
        cache = json.loads((fixture / 'translations.json').read_text())
        cache['private_usage'] = {'secret': 'private'}
        extra_key, extra = entry('Unreferenced dish')
        cache['entries'][extra_key] = extra
        key = menu['days'][0]['meals'][0]['translation_key']
        cache['entries'][key]['last_used'] = '2026-10-28'
        cache['entries'][key]['pinned'] = True
        cache.setdefault('notices', {})['Unused notice'] = {'en': 'unused', 'ko': 'unused'}
        original = copy.deepcopy(cache)
        result = retention.public_translations(menu, cache)
        required = {meal['translation_key'] for day in menu['days'] for meal in day['meals']}
        self.assertEqual(set(result), {'schema_version', 'entries', 'notices'})
        self.assertEqual(set(result['entries']), required)
        self.assertEqual(set(result['notices']), source_notices(menu))
        self.assertNotIn('last_used', result['entries'][key])
        self.assertNotIn('pinned', result['entries'][key])
        self.assertEqual(result['schema_version'], cache['schema_version'])
        validate_cache(result)
        result['entries'][key]['en']['name'] = 'changed'
        self.assertEqual(cache, original)

    def test_public_cache_fails_for_missing_referenced_translation_or_notice(self):
        key, value = entry('Required dish')
        menu = {'days': [{'meals': [{'translation_key': key, 'notices': ['Required notice'], 'components': []}]}]}
        for cache in ({'schema_version': 1, 'entries': {}},
                      {'schema_version': 1, 'entries': {key: value}}):
            with self.subTest(cache=cache), self.assertRaisesRegex(ValueError, 'Missing|required'):
                retention.public_translations(menu, cache)

    def test_ttl_is_inclusive_and_observed_source_alone_touches_last_used(self):
        cache = cache_for('Expired', 'Boundary', 'Required history', 'Fresh source')
        keys = {value['source']['name_de']: key for key, value in cache['entries'].items()}
        cache['notices'] = {'Expired notice': {'en': 'old', 'ko': 'old'}, 'Fresh notice': {'en': 'new', 'ko': 'new'}}
        usage = usage_for(cache, '2026-04-30')
        usage['entries'][keys['Boundary']]['last_used'] = '2026-05-01'
        original = copy.deepcopy((cache, usage))
        result, used = self.prune(cache, usage, required_keys={keys['Required history']},
                                  observed_keys={keys['Fresh source']}, observed_notices={'Fresh notice'})
        self.assertEqual(set(result['entries']), {keys[name] for name in ('Boundary', 'Required history', 'Fresh source')})
        self.assertEqual(set(result['notices']), {'Fresh notice'})
        self.assertEqual(used['entries'][keys['Boundary']]['last_used'], '2026-05-01')
        self.assertEqual(used['entries'][keys['Required history']]['last_used'], '2026-04-30')
        self.assertEqual(used['entries'][keys['Fresh source']]['last_used'], '2026-10-28')
        self.assertEqual(used['notices']['Fresh notice']['last_used'], '2026-10-28')
        self.assertEqual((cache, usage), original)
        result['entries'][keys['Boundary']]['en']['name'] = 'changed'
        used['entries'][keys['Boundary']]['pinned'] = True
        self.assertEqual((cache, usage), original)

    def test_editorial_reviewed_and_explicit_pins_survive_without_being_touched(self):
        cache = cache_for('Explicit pin', 'Model')
        cache['entries'].update(dict(entry(name, origin=origin) for name, origin in
                                    (('Editorial', 'editorial-draft'), ('Reviewed', 'reviewed-draft'))))
        pinned_key = source_key({'name_de': 'Explicit pin', 'components': []})
        cache['notices'] = {'Pinned notice': {'en': 'pin', 'ko': 'pin'}}
        usage = usage_for(cache, pinned={pinned_key, 'Pinned notice'})
        result, used = self.prune(cache, usage)
        self.assertEqual(len(result['entries']), 3)
        self.assertEqual(set(result['notices']), {'Pinned notice'})
        for kind in ('entries', 'notices'):
            self.assertEqual(set(used[kind]), set(result[kind]))
            self.assertTrue(all(record == {'last_used': '2026-01-01', 'pinned': True} for record in used[kind].values()))

    def test_missing_usage_initializes_once_and_retention_cannot_resurrect_metadata(self):
        cache = cache_for('Initial')
        key = next(iter(cache['entries']))
        result, used = self.prune(cache)
        self.assertEqual(used['entries'][key], {'last_used': '2026-10-28', 'pinned': False})
        used['entries']['deleted-key'] = {'last_used': '2026-10-28', 'pinned': True}
        result, used = self.prune(result, used, today=date(2027, 4, 27))
        self.assertEqual(result['entries'], {})
        self.assertEqual(used, {'schema_version': 1, 'entries': {}, 'notices': {}})
        again, again_usage = self.prune(result, used, today=date(2027, 4, 28))
        self.assertEqual((again, again_usage), (result, used))

    def test_lru_count_includes_notices_and_preserves_required_records(self):
        cache = cache_for('Oldest', 'Required', 'Newest')
        keys = {value['source']['name_de']: key for key, value in cache['entries'].items()}
        cache['notices'] = {'Notice': {'en': 'notice', 'ko': 'notice'}}
        usage = usage_for(cache, '2026-10-01')
        usage['entries'][keys['Oldest']]['last_used'] = '2026-09-01'
        usage['entries'][keys['Newest']]['last_used'] = '2026-10-27'
        result, used = self.prune(cache, usage, required_keys={keys['Required']},
                                  policy=replace(self.policy, cache_max_entries=2))
        self.assertEqual(set(result['entries']), {keys['Required'], keys['Newest']})
        self.assertEqual(result['notices'], {})
        self.assertEqual(set(used['entries']), set(result['entries']))

    def test_byte_limit_includes_usage_and_protected_overflow_fails(self):
        cache = cache_for('Only translation')
        key = next(iter(cache['entries']))
        usage = usage_for(cache, '2026-10-28')
        exact = len(_json_bytes({'schema_version': 1, 'cache': cache, 'usage': usage}))
        result, used = self.prune(cache, usage, required_keys={key}, policy=replace(self.policy, cache_max_bytes=exact))
        self.assertEqual((result, used), (cache, usage))
        with self.assertRaisesRegex(ValueError, 'protected|Protected'):
            self.prune(cache, usage, required_keys={key}, policy=replace(self.policy, cache_max_bytes=exact - 1))
        result, used = self.prune(cache, usage, policy=replace(self.policy, cache_max_bytes=exact - 1))
        self.assertEqual(result['entries'], {})
        self.assertEqual(used['entries'], {})
        cache['notices']['notice'] = {'en': 'notice', 'ko': 'notice'}
        with self.assertRaisesRegex(ValueError, 'protected|Protected'):
            self.prune(cache, usage, required_keys={key}, required_notices={'notice'},
                       policy=replace(self.policy, cache_max_entries=1))

    def test_usage_schema_rejects_malformed_records_without_losing_pins(self):
        cache = cache_for('Dish')
        key = next(iter(cache['entries']))
        valid = usage_for(cache, '2026-10-28')
        for broken in ({**valid, 'schema_version': True}, {**valid, 'extra': {}},
                       {**valid, 'entries': {key: {'last_used': '2026-2-01', 'pinned': False}}},
                       {**valid, 'entries': {key: {'last_used': date(2026, 10, 28), 'pinned': False}}},
                       {**valid, 'entries': {key: {'last_used': '2026-10-28', 'pinned': 'yes'}}}):
            with self.subTest(broken=broken), self.assertRaisesRegex(ValueError, 'usage|Usage'):
                self.prune(cache, broken)

    def test_byte_lru_measures_unicode_and_mapping_commas_exactly(self):
        cache = cache_for('오래된 메뉴', 'Retained dish')
        old = source_key({'name_de': '오래된 메뉴', 'components': []})
        cache['notices'] = {'안내': {'en': 'notice', 'ko': '안내'}}
        usage = usage_for(cache, '2026-10-28')
        usage['entries'][old]['last_used'] = '2026-10-01'
        expected_cache, expected_usage = copy.deepcopy((cache, usage))
        del expected_cache['entries'][old]
        del expected_usage['entries'][old]
        exact = len(_json_bytes({'schema_version': 1, 'cache': expected_cache, 'usage': expected_usage}))
        result, used = self.prune(cache, usage, policy=replace(self.policy, cache_max_bytes=exact))
        self.assertEqual((result, used), (expected_cache, expected_usage))
        self.assertEqual(len(_json_bytes({'schema_version': 1, 'cache': result, 'usage': used})), exact)

    def test_bundle_size_is_canonical_drive_envelope_and_never_truncates(self):
        menu = {'schema_version': 1, 'days': []}
        cache = cache_for('A')
        release_id = _release_id(_json_bytes(menu), _json_bytes(cache))
        envelope = {'schema_version': 1, 'release_id': release_id, 'menu': menu, 'translations': cache}
        exact = len(_json_bytes(envelope))
        self.assertEqual(retention.check_bundle_size(menu, cache, replace(self.policy, max_bundle_bytes=exact)), exact)
        with self.assertRaisesRegex(ValueError, 'bundle|Bundle'):
            retention.check_bundle_size(menu, cache, replace(self.policy, max_bundle_bytes=exact - 1))
        self.assertEqual(menu['days'], [])
        self.assertEqual(len(cache['entries']), 1)

    def test_multiple_years_remain_bounded_and_expired_data_stays_absent(self):
        cache, usage = cache_for(), None
        history = {'schema_version': 1, 'source': {'sha256': 'unchanged'}, 'coverage': {}, 'days': []}
        start = date(2024, 1, 1)
        first_key = None
        policy = replace(self.policy, cache_max_entries=90)
        for offset in range(3 * 366):
            today = start + timedelta(days=offset)
            key, translated = entry(f'Dish {offset}')
            first_key = key if first_key is None else first_key
            cache['entries'][key] = translated
            notice = f'Notice {offset}'
            cache['notices'][notice] = {'en': notice, 'ko': notice}
            history['days'].append({'date': today.isoformat(), 'meals': [
                {'translation_key': key, 'notices': [notice], 'components': []}]})
            history['coverage'] = {'start': history['days'][0]['date'], 'end': today.isoformat()}
            history = retention.retain_menu(history, today, policy)
            required_keys = {meal['translation_key'] for day in history['days'] for meal in day['meals']}
            cache, usage = retention.prune_cache(cache, usage, required_keys=required_keys,
                required_notices=source_notices(history), observed_keys={key}, observed_notices={notice},
                today=today, policy=policy)
            self.assertLessEqual(len(history['days']), 31)
            self.assertLessEqual(len(cache['entries']) + len(cache['notices']), 90)
            self.assertLessEqual(len(_json_bytes({'schema_version': 1, 'cache': cache, 'usage': usage})), policy.cache_max_bytes)
            self.assertEqual(set(usage['entries']), set(cache['entries']))
            self.assertEqual(set(usage['notices']), set(cache['notices']))
            if offset > 180:
                self.assertNotIn(first_key, cache['entries'])
        self.assertEqual(len(history['days']), 31)


if __name__ == '__main__':
    unittest.main()
