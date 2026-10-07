"""이 테스트 모듈은 갱신한 메뉴가 이전 날짜를 보존하고 새 가격과 수집 시각을 반영하는지 확인합니다."""

import copy
import unittest

from scripts.menu_source import parse_menu
from test_menu_source import meal, page


from mensa.refresh_service import RefreshService


class RefreshServiceTests(unittest.TestCase):
    def setUp(self):
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        self.cache = {'schema_version': 1, 'entries': {}}
        self.events = []
        self.ports = {
            'source': lambda: self.record('source', self.menu),
            'coverage_guard': lambda menu: self.record('coverage_guard'),
            'translate': lambda menu, cache, phrases, *, checkpoint=None: self.record('translate', self.cache),
            'validator': lambda menu, candidate: self.record('validator'),
            'publisher': lambda menu, candidate, *, previous: self.record('publisher'),
        }

    def record(self, stage, result=None):
        self.events.append(stage)
        return result

    def service(self, **overrides):
        return RefreshService(**{**self.ports, **overrides})


    def test_refresh_retains_earlier_dates_but_uses_new_overlap_and_drops_old_future(self):
        previous = copy.deepcopy(self.menu)
        old_overlap = parse_menu(page(date='22.09.2026'))
        future = parse_menu(page(date='25.09.2026'))
        previous['days'] += old_overlap['days'] + future['days']
        previous['coverage']['end'] = '2026-09-25'
        self.menu = parse_menu(page(meal(date='22.09.2026', prices=
            '<p><strong>Preise:</strong> S: 4,00 | M: 5,00 | G: 6,00</p>'),
            date='22.09.2026'), '2026-09-22T10:00:00Z')
        before = copy.deepcopy((previous, self.menu))
        observed = []
        result, _ = self.service(
            translate=lambda menu, *args, **kwargs: (observed.append(menu) or self.cache),
            publisher=lambda menu, *args, **kwargs: observed.append(menu),
        ).refresh(previous, self.cache, {})
        self.assertEqual([day['date'] for day in result['days']], ['2026-09-21', '2026-09-22'])
        self.assertEqual(result['days'][0], previous['days'][0])
        self.assertEqual(result['days'][1], self.menu['days'][0])
        self.assertEqual(result['days'][1]['meals'][0]['prices']['student'], 400)
        self.assertEqual(result['source'], self.menu['source'])
        self.assertEqual(observed, [result, result])
        self.assertEqual((previous, self.menu), before)

    def test_retained_dates_cannot_make_expired_new_source_pass_coverage_guard(self):
        from scripts.update_menu import require_current_coverage
        previous = copy.deepcopy(self.menu)
        future = parse_menu(page(date='25.09.2026'))
        previous['days'] += future['days']
        previous['coverage']['end'] = '2026-09-25'
        with self.assertRaisesRegex(ValueError, 'only past menus'):
            self.service(coverage_guard=lambda menu: require_current_coverage(
                menu, today='2026-09-22')).refresh(previous, self.cache, {})
        self.assertEqual(self.events, ['source'])


if __name__ == '__main__':
    unittest.main()
