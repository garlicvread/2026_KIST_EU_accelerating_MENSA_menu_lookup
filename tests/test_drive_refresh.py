"""Drive 작업이 소스 파일을 수정하지 않고 준비된 묶음을 재사용하는지 검사합니다."""

import copy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mensa.config import PublicationSettings
from mensa.jobs import JobRunner
from mensa.queue import load_state, save_state, reconcile
from mensa.releases import DirectoryPublisher, _json_bytes, _release_id
import test_directory_refresh as directory_fixture

NOW = directory_fixture.NOW


class DriveRefreshTests(unittest.TestCase):
    def setup_drive(self, f):
        f.config = replace(f.config, publication=PublicationSettings(
            mode='drive', repository=None, workflow=None, remote='fixture:',
            folder_id='folder_1234567890', file_id='snapshot_1234567890'))
        f.state.mkdir(mode=0o700)
        f.remote_pair = None
        f.uploads = []
        f.source_calls = 0
        f.translation_calls = 0
        f.fail_upload = False
        def source():
            f.source_calls += 1
            return copy.deepcopy(f.menu)
        f.source = source
        def translate(menu, cache, phrases, *, checkpoint=None):
            f.translation_calls += 1
            result = f.cache(menu)
            if checkpoint is not None:
                checkpoint(copy.deepcopy(result))
            return result
        def publish(menu, cache, **kwargs):
            f.uploads.append((copy.deepcopy(menu), copy.deepcopy(cache)))
            f.remote_pair = (copy.deepcopy(menu), copy.deepcopy(cache))
            if f.fail_upload:
                raise OSError('Upload response lost')
            return SimpleNamespace(release_id=_release_id(_json_bytes(menu), _json_bytes(cache)),
                                   file_id=f.config.publication.file_id)
        f.remote = SimpleNamespace(load_current=lambda: copy.deepcopy(f.remote_pair), publish=publish)
        from mensa.drive_refresh import DriveRefreshJob
        f.job = DriveRefreshJob(f.config, source=source, clock=lambda: f.now,
                               translation_builder=translate, publisher=f.remote)
        f.run = lambda: JobRunner(f.state, resources=lambda: (True, 'ready'), work=f.job).run(f.now)

    def test_drive_refresh_preserves_checkout_and_queue_has_no_git_ids(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            before = {p.relative_to(f.checkout): p.read_bytes() for p in f.checkout.rglob('*') if p.is_file()}
            result = f.run()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(f.uploads), 2)
            self.assertEqual(f.uploads[0][1]['entries'], {})
            self.assertEqual((f.source_calls, f.translation_calls), (1, 1))
            self.assertEqual(before, {p.relative_to(f.checkout): p.read_bytes() for p in f.checkout.rglob('*') if p.is_file()})
            self.assertIsNone(load_state(f.queue)['pending'])
            self.assertEqual(DirectoryPublisher(f.public, notice_glossary=f.glossary).load_current(), f.remote_pair)

    def test_lost_upload_response_reuses_prepared_pair_without_collecting(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            f.fail_upload = True
            self.assertEqual(f.run()['status'], 'failed')
            prepared = copy.deepcopy(f.uploads[-1])
            queue = load_state(f.queue)
            self.assertTrue(all(queue['pending'][name] is None for name in ('commit_sha', 'run_id', 'dispatch_requested_at')))
            queue['pending']['next_attempt_at'] = None
            save_state(f.queue, queue)
            f.fail_upload = False
            self.assertEqual(f.run()['status'], 'completed')
            self.assertEqual((f.source_calls, f.translation_calls), (1, 1))
            self.assertEqual(f.uploads[1], prepared)
            self.assertEqual(f.uploads[-1], f.remote_pair)

    def test_remote_history_bootstraps_without_tracked_runtime_json(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            old = f.menu_for(('Old soup',), day='18.09.2026')
            f.remote_pair = (old, f.cache(old))
            result = f.run()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(f.remote_pair[0]['days'][0]['date'], '2026-09-18')
            self.assertFalse((f.checkout/'site/data').exists())

    def test_old_days_expire_and_unused_translations_stay_private(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            old = f.menu_for(('Old soup',), day='01.08.2026')
            f.remote_pair = (old, f.cache(old))
            old_key = next(iter(f.remote_pair[1]['entries']))
            self.preserving_remote_builder(f)
            self.assertEqual(f.run()['status'], 'completed')
            self.assertEqual([day['date'] for day in f.remote_pair[0]['days']], ['2026-09-21'])
            required = {m['translation_key'] for d in f.remote_pair[0]['days'] for m in d['meals']}
            self.assertEqual(set(f.remote_pair[1]['entries']), required)
            private = json.loads((f.state/'translation-state.json').read_text())
            self.assertIn(old_key, private['cache']['entries'])
            self.assertNotIn('usage', f.remote_pair[1])

    def test_expired_private_entry_does_not_return_from_legacy_checkpoint(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            old = f.menu_for(('Unused model soup',), day='01.08.2026')
            cache = f.cache(old)
            key = next(iter(cache['entries']))
            cache['entries'][key].update(origin='model', model='fixture-model')
            f.write(f.state/'translation-checkpoint.json', cache)
            usage = {'schema_version': 1, 'entries': {key: {'last_used': '2025-01-01', 'pinned': False}},
                     'notices': {label: {'last_used': '2025-01-01', 'pinned': False} for label in cache['notices']}}
            f.write(f.state/'translation-state.json', {'schema_version': 1, 'cache': cache, 'usage': usage})
            (f.state/'translation-state.json').chmod(0o600)
            self.preserving_remote_builder(f)
            self.assertEqual(f.run()['status'], 'completed')
            private = json.loads((f.state/'translation-state.json').read_text())
            self.assertNotIn(key, private['cache']['entries'])
            f.now += timedelta(days=1)
            f.menu = f.menu_for(day='22.09.2026')
            self.assertEqual(f.run()['status'], 'completed')
            private = json.loads((f.state/'translation-state.json').read_text())
            self.assertNotIn(key, private['cache']['entries'])

    def test_legacy_git_pending_is_preserved_and_refused(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            pending = reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, NOW)
            pending['pending'].update(phase='publish', commit_sha='a'*40)
            before = copy.deepcopy(pending)
            with self.assertRaisesRegex(ValueError, 'Git'):
                f.job(pending)
            self.assertEqual(pending, before)
            self.assertEqual(f.uploads, [])

    def test_invalid_source_does_not_replace_prepared_or_remote_data(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            self.assertEqual(f.run()['status'], 'completed')
            remote_before = copy.deepcopy(f.remote_pair)
            f.now += timedelta(days=1)
            f.menu['days'][0]['meals'][0]['price_source']['name'] = 'Wrong provenance'
            self.assertEqual(f.run()['status'], 'failed')
            self.assertEqual(f.remote_pair, remote_before)
            self.assertEqual(len(f.uploads), 2)

    def test_native_builder_resumes_private_completed_entries_after_interruption(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            complete = f.cache(f.menu)
            first = next(iter(complete['entries']))
            attempts = []
            def build(menu, published, phrases, *, checkpoint=None):
                attempts.append(set((published or {}).get('entries', {})))
                if len(attempts) == 1:
                    partial = copy.deepcopy(complete)
                    partial['entries'] = {first: partial['entries'][first]}
                    checkpoint(partial)
                    raise OSError('Translation interrupted')
                return copy.deepcopy(complete)
            from mensa.drive_refresh import DriveRefreshJob
            f.job = DriveRefreshJob(f.config, source=lambda: copy.deepcopy(f.menu),
                                   clock=lambda: f.now, translation_builder=build, publisher=f.remote)
            first_result = f.run()
            self.assertEqual(first_result['status'], 'completed')
            self.assertEqual(first_result['translations']['status'], 'failed')
            from mensa.translation_work import retry_translations
            retry_translations(f.state)
            second_result = f.run()
            self.assertEqual(second_result['status'], 'idle')
            self.assertEqual(second_result['translations']['status'], 'completed')
            self.assertIn(first, attempts[1])

    def preserving_remote_builder(self, f):
        def build(menu, published, phrases, *, checkpoint=None):
            result = f.cache(menu)
            result['entries'].update(copy.deepcopy((published or {}).get('entries', {})))
            return result
        from mensa.drive_refresh import DriveRefreshJob
        f.job = DriveRefreshJob(f.config, source=f.source, clock=lambda: f.now,
                               translation_builder=build, publisher=f.remote)

    def test_next_period_preserves_published_drive_correction(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            self.preserving_remote_builder(f)
            self.assertEqual(f.run()['status'], 'completed')
            key = next(iter(f.remote_pair[1]['entries']))
            f.remote_pair[1]['entries'][key]['ko']['name'] = '공개본에서 교정한 요리'
            f.now += timedelta(days=1)
            f.menu = f.menu_for(day='22.09.2026')
            self.assertEqual(f.run()['status'], 'completed')
            self.assertEqual(f.remote_pair[1]['entries'][key]['ko']['name'], '공개본에서 교정한 요리')

    def test_source_refresh_keeps_reviewed_title_component_and_notice_while_filling_gaps(self):
        from scripts.menu_source import parse_menu
        from test_menu_source import meal, page
        def menu(names, day, generated_at):
            html = ''.join(meal(name=name, date=day).replace(
                "<div class='component-name'>Reis</div>",
                "<div class='component-name'>Peperoni</div>") for name in names)
            return parse_menu(page(html, date=day), generated_at)
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            f.menu = menu(('Wikingertopf',), '21.09.2026', f.now.isoformat())
            self.setup_drive(f)
            published = f.cache(f.menu)
            key = f.menu['days'][0]['meals'][0]['translation_key']
            reviewed = published['entries'][key]
            reviewed['origin'] = 'reviewed-draft'
            reviewed['en']['name'] = 'Publicly reviewed stew title'
            reviewed['ko']['name'] = '공개본에서 검토한 스튜 이름'
            reviewed['en']['components'][0] = 'Publicly reviewed pepper wording'
            reviewed['ko']['components'][0] = '공개본에서 검토한 고추 표현'
            reviewed = copy.deepcopy(reviewed)
            corrected_notice = {'en': 'Publicly reviewed wheat notice', 'ko': '공개본에서 검토한 밀 표시'}
            published['notices']['Weizen'] = copy.deepcopy(corrected_notice)
            published['notices'].pop('Sellerie')
            f.remote_pair = (copy.deepcopy(f.menu), published)
            f.write(f.checkout/'data/editorial-translations.json', {'phrases': {
                text: {'en': 'Fixture authored translation', 'ko': '테스트 편집 번역'}
                for text in ('New fixture meal', 'Peperoni', 'Soße & Gemüse')}})
            f.now += timedelta(days=1)
            f.menu = menu(('Wikingertopf', 'New fixture meal'), '22.09.2026', f.now.isoformat())
            result = JobRunner(f.state, resources=lambda: self.fail('Source defaults need no model'), work=f.job).run(f.now)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['translations']['status'], 'idle')
            self.assertEqual((f.source_calls, f.translation_calls), (1, 0))
            actual = f.remote_pair[1]
            with self.subTest(correction='reviewed title and component'):
                self.assertEqual(actual['entries'][key], reviewed)
            with self.subTest(correction='reviewed notice'):
                self.assertEqual(actual['notices']['Weizen'], corrected_notice)
            self.assertEqual(actual['notices']['Sellerie'], f.glossary['Sellerie'])
            missing_key = f.menu['days'][0]['meals'][1]['translation_key']
            self.assertIn(missing_key, actual['entries'])
            self.assertEqual(actual['entries'][missing_key]['origin'], 'editorial-draft')

    def test_retry_reprepares_when_published_drive_changes_after_lost_response(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            self.preserving_remote_builder(f)
            f.remote_pair = (copy.deepcopy(f.menu), f.cache(f.menu))
            f.fail_upload = True
            self.assertEqual(f.run()['status'], 'failed')
            key = next(iter(f.remote_pair[1]['entries']))
            f.remote_pair[1]['entries'][key]['ko']['name'] = '업로드 후 교정한 요리'
            queue = load_state(f.queue)
            queue['pending']['next_attempt_at'] = None
            save_state(f.queue, queue)
            f.fail_upload = False
            self.assertEqual(f.run()['status'], 'completed')
            self.assertEqual(f.remote_pair[1]['entries'][key]['ko']['name'], '업로드 후 교정한 요리')
            self.assertEqual(f.source_calls, 2)


if __name__ == '__main__':
    unittest.main()


class DriveTranslationLaneTests(unittest.TestCase):
    setup_drive = DriveRefreshTests.setup_drive

    def test_long_translation_failure_schedules_backoff_from_failure_time(self):
        from mensa.errors import GenerationError
        from unittest.mock import patch
        cases = ((OSError('Translation interrupted'), 35),
                 (GenerationError('http_retryable', retry_after=3600), 80))
        for error, retry_minutes in cases:
            with self.subTest(error=type(error).__name__), directory_fixture.DirectoryRefreshTests().fixture() as f:
                self.setup_drive(f)
                result = JobRunner(f.state, resources=lambda: (False, 'busy'), work=f.job).run(f.now)
                self.assertEqual(result['status'], 'completed')
                elapsed = [0]
                def generate(menu, cache, phrases, *, checkpoint):
                    elapsed[0] = 1200
                    raise error
                f.job._translation_builder = generate
                with patch('time.monotonic', side_effect=lambda: elapsed[0]):
                    result = f.job.translation_work(f.now, resources=lambda: (True, 'ready'),
                                                     source_state=load_state(f.queue))
                self.assertEqual(result['status'], 'failed')
                state = json.loads((f.state/'translation-work.json').read_text())
                self.assertEqual(state['attempts'], 1)
                expected = (f.now + timedelta(minutes=retry_minutes)).isoformat().replace('+00:00', 'Z')
                self.assertEqual(state['next_attempt_at'], expected)

    def test_malformed_identity_refresh_preserves_valid_fallback_and_backs_off(self):
        from contextlib import contextmanager
        from mensa.translation_contract import generation_identity
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            fallback = f.cache(f.menu)
            identity = generation_identity({'provider': f.config.inference.provider,
                'model': f.config.inference.model, 'backend_revision': 'previous-revision'},
                notice_glossary=f.glossary)
            for entry in fallback['entries'].values():
                entry.update(origin='model', model=f.config.inference.model,
                             generation_identity=identity)
            f.remote_pair = (copy.deepcopy(f.menu), copy.deepcopy(fallback))
            requests = []
            def generate(source, config):
                requests.append(source['name_de'])
                return {'en': {'name': 'Incomplete new model result', 'components': []}}
            @contextmanager
            def runtime(settings, *, key):
                yield {'provider': settings.provider, 'model': settings.model,
                       'backend_revision': settings.revision, 'url': 'http://fixture.invalid/api/chat', 'key': key}
            f.job._translation_builder = None
            f.job._translate = generate
            f.job._runtime = runtime
            result = f.run()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['translations']['status'], 'failed')
            self.assertEqual(len(requests), 2)
            self.assertEqual(f.remote_pair[1]['entries'], fallback['entries'])
            path = f.state/'translation-work.json'
            before = path.read_bytes()
            state = json.loads(before)
            self.assertEqual(state['attempts'], 1)
            self.assertIsNotNone(state['next_attempt_at'])
            f.remote.load_current = Mock(side_effect=AssertionError('Backoff must precede transport'))
            f.now += timedelta(seconds=1)
            result = f.run()
            self.assertEqual(result['translations']['status'], 'waiting')
            self.assertEqual(len(requests), 2)
            self.assertEqual(path.read_bytes(), before)

    def test_idle_public_read_failure_records_translation_backoff(self):
        from unittest.mock import Mock
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            self.assertEqual(f.run()['status'], 'completed')
            f.remote.load_current = Mock(side_effect=[OSError('Read interrupted'), copy.deepcopy(f.remote_pair)])
            result = f.run()
            self.assertEqual((result['status'], result['translations']['status']), ('idle', 'failed'))
            path = f.state/'translation-work.json'
            before = path.read_bytes()
            state = json.loads(before)
            self.assertEqual(state['attempts'], 1)
            self.assertIsNotNone(state['next_attempt_at'])
            f.now += timedelta(seconds=1)
            waiting = f.run()
            self.assertEqual(waiting['translations'], {'status': 'waiting', 'retry_at': state['next_attempt_at']})
            self.assertEqual(f.remote.load_current.call_count, 1)
            self.assertEqual(path.read_bytes(), before)
            from datetime import datetime
            f.now = datetime.fromisoformat(state['next_attempt_at'])
            self.assertEqual(f.run()['translations']['status'], 'idle')
            self.assertEqual(f.remote.load_current.call_count, 2)
            final = json.loads(path.read_text())
            self.assertEqual(final['attempts'], 1)
            self.assertIsNone(final['next_attempt_at'])

    def test_injected_native_builder_keeps_legacy_same_model_reuse_without_resource_probe(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            cache = f.cache(f.menu)
            for entry in cache['entries'].values():
                entry.update(origin='model', model=f.config.inference.model)
            f.remote_pair = (copy.deepcopy(f.menu), cache)
            result = JobRunner(f.state, resources=lambda: self.fail('Reusable native history'), work=f.job).run(f.now)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['translations']['status'], 'idle')
            self.assertEqual(f.translation_calls, 0)

    def test_unresolved_source_upload_defers_all_translation_work(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            f.fail_upload = True
            result = f.run()
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['translations']['status'], 'deferred')
            self.assertEqual(f.translation_calls, 0)
            result = f.run()
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(result['translations']['status'], 'deferred')
            self.assertEqual((f.source_calls, f.translation_calls), (1, 0))

    def test_translation_upload_response_loss_reuses_prepared_without_regeneration(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            actual = f.remote.publish
            def publish(menu, cache, **kwargs):
                f.fail_upload = bool(cache['entries'])
                return actual(menu, cache, **kwargs)
            f.remote.publish = publish
            result = f.run()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['translations']['status'], 'failed')
            prepared = copy.deepcopy(f.uploads[-1])
            path = f.state/'translation-work.json'
            before = path.read_bytes()
            state = json.loads(before)
            f.remote.publish = actual
            f.fail_upload = False
            actual_read = f.remote.load_current
            f.remote.load_current = Mock(side_effect=actual_read)
            f.now += timedelta(seconds=1)
            result = f.run()
            self.assertEqual(result['translations'], {'status': 'waiting', 'retry_at': state['next_attempt_at']})
            self.assertEqual(path.read_bytes(), before)
            f.remote.load_current.assert_not_called()
            self.assertEqual(len(f.uploads), 2)
            from datetime import datetime
            f.now = datetime.fromisoformat(state['next_attempt_at'])
            result = f.run()
            self.assertEqual(result['status'], 'idle')
            self.assertEqual(result['translations']['status'], 'completed')
            self.assertEqual((f.source_calls, f.translation_calls), (1, 1))
            self.assertEqual(f.uploads[-1], prepared)
            self.assertEqual(len(f.uploads), 2)
            state = json.loads((f.state/'translation-work.json').read_text())
            self.assertIsNone(state['prepared_release_id'])

    def test_incomplete_result_publishes_progress_then_waits_and_resumes_without_source(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            calls = []
            def build(menu, history, phrases, *, checkpoint):
                calls.append(copy.deepcopy(history))
                result = f.cache(menu)
                if len(calls) == 1:
                    first = next(iter(result['entries']))
                    result['entries'] = {first: result['entries'][first]}
                return result
            f.job._translation_builder = build
            result = f.run()
            self.assertEqual((result['status'], result['translations']['status']), ('completed', 'failed'))
            self.assertEqual(len(f.remote_pair[1]['entries']), 1)
            result = f.run()
            self.assertEqual(result['translations']['status'], 'waiting')
            self.assertEqual(len(calls), 1)
            f.now += timedelta(minutes=16)
            result = f.run()
            self.assertEqual((result['status'], result['translations']['status']), ('idle', 'completed'))
            self.assertEqual((f.source_calls, len(calls)), (1, 2))
            self.assertEqual(len(calls[-1]['entries']), 1)
            self.assertEqual(len(f.remote_pair[1]['entries']), 2)

    def test_new_public_source_during_translation_is_never_overwritten(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            latest = f.menu_for(('Newer public source',))
            calls = []
            def build(menu, history, phrases, *, checkpoint):
                calls.append(copy.deepcopy(menu))
                if len(calls) == 1:
                    f.remote_pair = (copy.deepcopy(latest), {'schema_version': 1, 'entries': {}})
                return f.cache(menu)
            f.job._translation_builder = build
            result = f.run()
            self.assertEqual(result['translations']['status'], 'deferred')
            self.assertEqual(f.remote_pair[0], latest)
            self.assertEqual(len(f.uploads), 1)
            result = f.run()
            self.assertEqual(result['translations']['status'], 'completed')
            self.assertEqual(f.remote_pair[0], latest)
            self.assertEqual(calls[-1], latest)
            self.assertEqual(f.source_calls, 1)

    def test_lost_local_preparation_rebases_instead_of_repeating_missing_release(self):
        from mensa.translation_work import load_translation_state, save_translation_state
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            result = JobRunner(f.state, resources=lambda: (False, 'busy'), work=f.job).run(f.now)
            self.assertEqual(result['status'], 'completed')
            current_id = f.job._identity(f.remote_pair)
            path = f.state/'translation-work.json'
            state = load_translation_state(path)
            state.update(target_release_id=current_id, prepared_release_id='a'*64,
                         previous_release_id=current_id)
            save_translation_state(path, state)
            result = f.run()
            self.assertEqual(result['translations']['status'], 'completed')
            self.assertEqual(f.translation_calls, 1)

    def test_translation_only_tick_does_not_advance_last_used(self):
        from mensa.translation_work import retry_translations
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            self.assertEqual(f.run()['status'], 'completed')
            usage = json.loads((f.state/'translation-state.json').read_text())['usage']
            f.now += timedelta(days=1)
            state = load_state(f.queue)
            state['completed_period'] = '2026-09-22T11:00'
            save_state(f.queue, state)
            retry_translations(f.state)
            self.assertEqual(f.run()['status'], 'idle')
            self.assertEqual(json.loads((f.state/'translation-state.json').read_text())['usage'], usage)
