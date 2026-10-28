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
            self.assertEqual(len(f.uploads), 1)
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
            self.assertEqual(f.uploads[-1], prepared)

    def test_remote_history_bootstraps_without_tracked_runtime_json(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            old = f.menu_for(('Old soup',), day='18.09.2026')
            f.remote_pair = (old, f.cache(old))
            result = f.run()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(f.remote_pair[0]['days'][0]['date'], '2026-09-18')
            self.assertFalse((f.checkout/'site/data').exists())

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
            self.assertEqual(len(f.uploads), 1)

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
            self.assertEqual(f.run()['status'], 'failed')
            queue = load_state(f.queue)
            queue['pending']['next_attempt_at'] = None
            save_state(f.queue, queue)
            self.assertEqual(f.run()['status'], 'completed')
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

    def test_retry_reprepares_when_published_drive_changes_after_lost_response(self):
        with directory_fixture.DirectoryRefreshTests().fixture() as f:
            self.setup_drive(f)
            self.preserving_remote_builder(f)
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
