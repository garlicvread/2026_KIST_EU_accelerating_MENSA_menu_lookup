"""수집과 번역의 작업 상태가 서로의 진행을 막지 않는지 검사합니다."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mensa.errors import GenerationError
from mensa.jobs import JobRunner
from mensa.queue import failed, load_state, reconcile, save_state


NOW = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)


class LaneRunnerTests(unittest.TestCase):
    def fixture(self, directory, *, translation_error=None):
        class Work:
            source_needs_model_resources = False
            source_calls = 0
            translation_calls = 0
            adopted = None

            def __call__(self, state):
                self.source_calls += 1

            def translation_work(self, now, *, resources, source_state):
                self.translation_calls += 1
                if translation_error is not None:
                    raise translation_error
                ready, reason = resources()
                return {'status': 'completed' if ready else 'deferred', 'reason': reason}

            def adopt_translation_failure(self, pending, now):
                self.adopted = pending
        return Work(), JobRunner(Path(directory), resources=lambda: (False, 'model memory busy'), work=Work())

    def test_model_resource_shortage_does_not_defer_source_collection(self):
        with TemporaryDirectory() as folder:
            work, _ = self.fixture(folder)
            result = JobRunner(Path(folder), resources=lambda: (False, 'model memory busy'), work=work).run(NOW)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['translations']['status'], 'deferred')
            self.assertEqual((work.source_calls, work.translation_calls), (1, 1))
            self.assertIsNone(load_state(Path(folder) / 'queue.json')['pending'])

    def test_translation_failure_does_not_fail_completed_source_period(self):
        with TemporaryDirectory() as folder:
            work, _ = self.fixture(folder, translation_error=GenerationError('authentication'))
            result = JobRunner(Path(folder), resources=lambda: (True, 'ready'), work=work).run(NOW)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['translations']['status'], 'blocked')
            self.assertIsNone(load_state(Path(folder) / 'queue.json')['pending'])

    def test_source_backoff_does_not_skip_translation_lane(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            state = reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, NOW)
            state = failed(state, 'source unavailable', NOW)
            save_state(path / 'queue.json', state)
            work, _ = self.fixture(folder)
            result = JobRunner(path, resources=lambda: (True, 'ready'), work=work).run(NOW + timedelta(seconds=1))
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(result['translations']['status'], 'completed')
            self.assertEqual((work.source_calls, work.translation_calls), (0, 1))

    def test_old_generation_failure_is_adopted_without_blocking_new_collection(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            state = failed(reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, NOW),
                           GenerationError('authentication'), NOW)
            save_state(path / 'queue.json', state)
            work, _ = self.fixture(folder)
            result = JobRunner(path, resources=lambda: (True, 'ready'), work=work).run(NOW + timedelta(days=1))
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(work.adopted['generation_failure']['code'], 'authentication')
            self.assertEqual(work.adopted['attempts'], 1)
            self.assertEqual(work.source_calls, 1)

    def test_invalid_source_state_never_starts_translation(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            (path / 'queue.json').write_text(json.dumps({'schema_version': 99}))
            work, _ = self.fixture(folder)
            with self.assertRaises(ValueError):
                JobRunner(path, resources=lambda: (True, 'ready'), work=work).run(NOW)
            self.assertEqual(work.translation_calls, 0)


class TranslationStateTests(unittest.TestCase):
    def test_missing_translation_state_is_read_only_and_bounded(self):
        from mensa.translation_work import load_translation_state, save_translation_state
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'missing' / 'translation-work.json'
            state = load_translation_state(path)
            self.assertEqual(state['attempts'], 0)
            self.assertFalse(path.parent.exists())
            path.parent.mkdir(mode=0o700)
            save_translation_state(path, state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertLess(path.stat().st_size, 4096)

    def test_retry_preserves_attempt_history_and_only_unblocks_translation_state(self):
        from mensa.translation_work import (load_translation_state, translation_failed,
                                             retry_translations, save_translation_state)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'translation-work.json'
            state = translation_failed(load_translation_state(path), GenerationError('authentication'), NOW)
            save_translation_state(path, state)
            result = retry_translations(Path(folder))
            self.assertEqual(result['attempts'], 1)
            self.assertIsNone(result['generation_failure'])
            self.assertIsNone(result['next_attempt_at'])
            self.assertFalse((path.parent / 'queue.json').exists())

    def test_unsafe_translation_state_is_rejected(self):
        from mensa.translation_work import load_translation_state, save_translation_state
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'translation-work.json'
            path.symlink_to(Path(folder) / 'target')
            with self.assertRaises(ValueError):
                load_translation_state(path)
            with self.assertRaises(ValueError):
                save_translation_state(path, {})
            path.unlink()
            path.write_text('{"schema_version":1,"schema_version":1}')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                load_translation_state(path)
            path.write_text('x' * 4097)
            with self.assertRaises(ValueError):
                load_translation_state(path)

    def test_backoff_uses_retry_after_and_invalid_result_is_regenerated(self):
        from mensa.translation_work import load_translation_state, translation_failed
        with TemporaryDirectory() as folder:
            state = load_translation_state(Path(folder) / 'translation-work.json')
            state = translation_failed(state, GenerationError('http_retryable', retry_after=3600), NOW)
            self.assertEqual(state['next_attempt_at'], '2026-09-21T11:00:00Z')
            state = translation_failed(state, GenerationError('invalid_result'), NOW)
            self.assertEqual(state['attempts'], 2)
            self.assertIsNone(state['generation_failure'])
            self.assertIsNotNone(state['next_attempt_at'])

    def test_explicit_retry_respects_worker_lock(self):
        from mensa.queue import BusyError, exclusive_lock
        from mensa.translation_work import retry_translations
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            with exclusive_lock(directory / 'worker.lock'):
                with self.assertRaises(BusyError):
                    retry_translations(directory)
            self.assertFalse((directory / 'translation-work.json').exists())
