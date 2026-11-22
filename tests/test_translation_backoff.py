"""완료한 번역 작업의 실패 횟수가 다음 장애의 재시도 간격에 남지 않는지 검사합니다."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from mensa.errors import GenerationError
from mensa.retention import public_translations
from mensa.translation_work import (_identity, load_translation_state, run_translation_work,
                                    save_translation_state, translation_failed)


NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


class TranslationBackoffTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        state_dir = root / 'private'
        state_dir.mkdir(mode=0o700)
        self.config = SimpleNamespace(
            paths=SimpleNamespace(state_dir=state_dir, public_dir=root / 'public',
                                  checkout_dir=root / 'checkout'),
            inference=SimpleNamespace(provider='ollama', model='gemma4:31b', revision='fixture'),
            retention=SimpleNamespace(history_days=30, cache_unused_days=180,
                                      cache_max_entries=5000, cache_max_bytes=16 * 1024 * 1024,
                                      max_storage_bytes=64 * 1024 * 1024))
        fixture = Path(__file__).parent / 'fixtures/menu-data'
        self.menu = json.loads((fixture / 'menu.json').read_text())
        cache = json.loads((fixture / 'translations.json').read_text())
        self.complete = (self.menu, public_translations(self.menu, cache))
        self.pair = (self.menu, {'schema_version': 1, 'entries': {}, 'notices': {}})
        self.path = state_dir / 'translation-work.json'
        self.events = []

    def seed_previous_failures(self):
        state = load_translation_state(self.path)
        failed_at = NOW - timedelta(days=1)
        for _ in range(6):
            state = translation_failed(state, GenerationError('network'), failed_at)
        self.assertEqual(state['attempts'], 6)
        retry_at = datetime.fromisoformat(state['next_attempt_at'].replace('Z', '+00:00'))
        self.assertEqual(retry_at - failed_at, timedelta(hours=6))
        save_translation_state(self.path, state)
        return state

    def publish(self, pair, release_id, previous_id, today):
        self.events.append('publish')
        self.pair = copy.deepcopy(pair)

    def confirm(self, pair, release_id, previous_id, today):
        self.events.append('confirm')

    def run_work(self, **ports):
        arguments = dict(resources=lambda: (True, 'ready'), current=lambda: self.pair,
                         load_prepared=lambda identifier: copy.deepcopy(self.complete),
                         publish=self.publish, confirm=self.confirm,
                         generate=lambda *args: copy.deepcopy(self.complete[1]),
                         glossary={}, phrases={}, strict_identity=False)
        arguments.update(ports)
        return run_translation_work(self.config, NOW, **arguments)

    def assert_new_failure_starts_with_initial_backoff(self):
        def unavailable():
            raise GenerationError('network')
        self.assertEqual(self.run_work(current=unavailable)['status'], 'failed')
        state = load_translation_state(self.path)
        retry_at = datetime.fromisoformat(state['next_attempt_at'].replace('Z', '+00:00'))
        self.assertGreaterEqual(retry_at - NOW, timedelta(minutes=15))
        self.assertLess(retry_at - NOW, timedelta(minutes=16))
        self.assertEqual(state['attempts'], 1)

    def test_completed_translation_resets_backoff_for_new_failure(self):
        self.seed_previous_failures()
        self.assertEqual(self.run_work()['status'], 'completed')
        self.assertEqual(self.events, ['publish'])
        self.assert_new_failure_starts_with_initial_backoff()

    def assert_recovered_publication_resets_backoff(self, *, selected):
        state = self.seed_previous_failures()
        state.update(target_release_id=_identity(self.pair),
                     prepared_release_id=_identity(self.complete),
                     previous_release_id=_identity(self.pair))
        save_translation_state(self.path, state)
        if selected:
            self.pair = copy.deepcopy(self.complete)
        self.assertEqual(self.run_work()['status'], 'completed')
        self.assertEqual(self.events, ['confirm' if selected else 'publish'])
        self.assert_new_failure_starts_with_initial_backoff()

    def test_recovered_publication_resets_backoff_for_new_failure(self):
        self.assert_recovered_publication_resets_backoff(selected=False)

    def test_confirmed_publication_resets_backoff_for_new_failure(self):
        self.assert_recovered_publication_resets_backoff(selected=True)
