import unittest
import json
import io
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
from datetime import datetime, timezone, timedelta
from contextlib import redirect_stdout

from scripts import local_refresh as worker
from scripts.local_refresh import process_queue, translation_needed, RefreshJob, REMOTE
from scripts.refresh_queue import load_state, save_state, reconcile, exclusive_lock
from scripts.translations import PROMPT_VERSION, build_translations

NOW = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
SHA = 'a' * 40
FRESH_MENU = {'coverage': {'end': '2099-12-31'}}
OFFLINE_RETURN = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
EXPIRED_MENU = {'coverage': {'start': '2026-09-21', 'end': '2026-10-02'}}


def publication_state():
    state = reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, NOW)
    state['pending'].update(phase='publish', commit_sha=SHA, run_id=123)
    return state


def fake_publication(base, conclusion='failure'):
    job = RefreshJob(base)
    job.check_repository = Mock()
    job.validate_complete = Mock()
    job.checkpoint = lambda state: save_state(job.state_path, state)
    run = {'databaseId': 123, 'status': 'completed', 'conclusion': conclusion,
           'headSha': 'b' * 40, 'displayTitle': f'Publish menu {SHA}'}
    job.git = Mock(return_value=SHA)
    job.gh = Mock(side_effect=lambda *args: json.dumps(run) if args[:2] == ('run', 'view') else '')
    return job, run


def checkout(base):
    repo = Path(base) / 'checkout'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    git('init', '-b', 'main')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    git('remote', 'add', 'origin', REMOTE)
    for name in worker.DATA_FILES:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    phrases = repo / 'data/editorial-translations.json'
    phrases.parent.mkdir()
    phrases.write_text('{"phrases":{}}')
    git('add', '.')
    git('commit', '-m', 'Fixture')
    return repo

class LocalRefreshTests(unittest.TestCase):
    def test_busy_machine_retains_pending_request_without_running_work(self):
        with TemporaryDirectory() as folder:
            called=[]
            result=process_queue(Path(folder), NOW, lambda:(False,'busy'), lambda state:called.append(state))
            self.assertEqual(result['status'],'deferred')
            self.assertEqual(called,[])
            self.assertEqual(load_state(Path(folder)/'queue.json')['pending']['period'],'2026-09-21')

    def test_failed_refresh_is_retried_only_after_backoff(self):
        with TemporaryDirectory() as folder:
            def broken(state):
                raise ValueError('invalid source price')
            result=process_queue(Path(folder), NOW, lambda:(True,'ready'), broken)
            self.assertEqual(result['status'],'failed')
            state=load_state(Path(folder)/'queue.json')
            self.assertEqual(state['pending']['attempts'],1)
            called=[]
            result=process_queue(Path(folder), NOW, lambda:(True,'ready'),lambda s:called.append(s))
            self.assertEqual(result['status'],'waiting')
            self.assertEqual(called,[])

    def test_success_acknowledges_week_and_repeat_tick_does_no_work(self):
        with TemporaryDirectory() as folder:
            called=[]
            process_queue(Path(folder),NOW,lambda:(True,'ready'),lambda s:called.append(s))
            result=process_queue(Path(folder),NOW,lambda:(True,'ready'),lambda s:called.append(s))
            self.assertEqual(len(called),1)
            self.assertEqual(result['status'],'idle')
            self.assertEqual(load_state(Path(folder)/'queue.json')['completed_period'],'2026-09-21')

    def test_checkpoint_is_retained_after_publish_failure(self):
        with TemporaryDirectory() as folder:
            def broken(state):
                state['pending'].update(phase='publish',commit_sha='a'*40,run_id=123)
                from scripts.refresh_queue import save_state
                save_state(Path(folder)/'queue.json',state)
                raise OSError('network offline')
            process_queue(Path(folder),NOW,lambda:(True,'ready'),broken)
            pending=load_state(Path(folder)/'queue.json')['pending']
            self.assertEqual(pending['phase'],'publish')
            self.assertEqual(pending['run_id'],123)

    def test_existing_editorial_cache_does_not_start_a_model(self):
        menu={'days':[{'meals':[{'translation_key':'x'}]}]}
        cache={'entries':{'x':{'origin':'editorial-draft','model':'assistant-draft','prompt_version':PROMPT_VERSION}}}
        self.assertFalse(translation_needed(menu,cache))
        self.assertTrue(translation_needed(menu,{'entries':{}}))

    def test_current_migrated_snapshot_does_not_start_a_model(self):
        root = Path(__file__).resolve().parents[1]
        menu = json.loads((root / 'site/data/menu.json').read_text())
        previous = json.loads((root / 'site/data/translations.json').read_text())
        migrated = build_translations(menu, previous, {}, None)
        self.assertFalse(translation_needed(menu, migrated))

    def test_worker_reuses_compatible_generations_but_refreshes_changed_inputs(self):
        menu = {'days': [{'meals': [{'translation_key': 'current-source'}]}]}
        for origin, model, prompt, key, needed in (
            ('model', worker.MODEL, 'menu-v3', 'current-source', False),
            ('model', worker.MODEL, PROMPT_VERSION, 'current-source', False),
            ('editorial-draft', 'assistant-draft', 'menu-v3', 'current-source', False),
            ('reviewed-draft', 'reviewer', 'menu-v3', 'current-source', False),
            ('model', 'older-model', 'menu-v3', 'current-source', True),
            ('model', worker.MODEL, 'menu-v2', 'current-source', True),
            ('editorial-draft', 'assistant-draft', 'menu-v2', 'current-source', True),
            ('model', worker.MODEL, 'menu-v3', 'changed-source', True),
        ):
            with self.subTest(origin=origin, model=model, prompt=prompt, key=key):
                cache = {'entries': {key: {'origin': origin, 'model': model, 'prompt_version': prompt}}}
                self.assertEqual(translation_needed(menu, cache), needed)

    def test_real_porcelain_allows_only_generated_snapshot_changes(self):
        with TemporaryDirectory() as folder:
            repo = checkout(folder)
            (repo / worker.DATA_FILES[0]).write_text('{"interrupted":true}')
            try:
                RefreshJob(folder).check_repository(allow_snapshots=True)
            except ValueError as exc:
                self.fail(f'Generated snapshot must pass porcelain validation: {exc}')

    def test_interrupted_collection_restores_only_snapshots_then_recollects(self):
        with TemporaryDirectory() as folder:
            repo = checkout(folder)
            for name in worker.DATA_FILES:
                (repo / name).write_text('{"interrupted":true}')
            job = RefreshJob(folder)
            real_git = job.git
            job.git = lambda *args: '' if args[0] in ('fetch', 'merge') else real_git(*args)
            with patch.object(worker, 'fetch_html', side_effect=RuntimeError('recollection reached')):
                try:
                    with self.assertRaisesRegex(RuntimeError, 'recollection reached'):
                        job.collect()
                except ValueError as exc:
                    self.fail(f'Interrupted snapshots must be recovered before collection: {exc}')
            for name in worker.DATA_FILES:
                self.assertEqual((repo / name).read_text(), '{}')
            self.assertEqual(real_git('status', '--porcelain'), '')

    def test_unrelated_changes_are_never_restored(self):
        with TemporaryDirectory() as folder:
            repo = checkout(folder)
            target = repo / worker.DATA_FILES[0]
            target.write_text('{"interrupted":true}')
            unrelated = repo / 'notes.txt'
            unrelated.write_text('Keep my notes')
            with self.assertRaisesRegex(ValueError, 'unrelated'):
                RefreshJob(folder).collect()
            self.assertEqual(unrelated.read_text(), 'Keep my notes')
            self.assertEqual(target.read_text(), '{"interrupted":true}')

    def test_hard_kill_staging_files_are_recovered_but_tracked_files_are_kept(self):
        with TemporaryDirectory() as folder:
            repo = checkout(folder)
            job = RefreshJob(folder)
            tracked = repo / 'site/data/.pending-tracked'
            tracked.write_text('Tracked file stays')
            job.git('add', '--', 'site/data/.pending-tracked')
            job.git('commit', '-m', 'Tracked fixture')
            abandoned = repo / 'site/data/.pending-interrupted'
            abandoned.write_text('Partial generated staging data')
            real_git = job.git
            job.git = lambda *args: '' if args[0] in ('fetch', 'merge') else real_git(*args)
            with patch.object(worker, 'fetch_html', side_effect=RuntimeError('recollection reached')):
                try:
                    with self.assertRaisesRegex(RuntimeError, 'recollection reached'):
                        job.collect()
                except ValueError as exc:
                    self.fail(f'Reserved abandoned staging file must be recovered: {exc}')
            self.assertFalse(abandoned.exists())
            self.assertEqual(tracked.read_text(), 'Tracked file stays')

    def test_reserved_staging_prefix_never_removes_symlinks_or_other_paths(self):
        with TemporaryDirectory() as folder:
            repo = checkout(folder)
            outside = Path(folder) / 'keep.txt'
            outside.write_text('Keep target')
            link = repo / 'site/data/.pending-symlink'
            link.symlink_to(outside)
            elsewhere = repo / '.pending-elsewhere'
            elsewhere.write_text('Keep elsewhere')
            with self.assertRaisesRegex(ValueError, 'unrelated'):
                RefreshJob(folder).collect()
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_text(), 'Keep target')
            self.assertEqual(elsewhere.read_text(), 'Keep elsewhere')

    def test_failed_run_retries_same_id_once_before_backoff(self):
        for conclusion in ('failure', 'cancelled'):
            with self.subTest(conclusion=conclusion), TemporaryDirectory() as folder:
                save_state(Path(folder) / 'queue.json', publication_state())
                job, _ = fake_publication(folder, conclusion)
                with patch.object(worker, 'read_json', return_value=FRESH_MENU):
                    result = process_queue(folder, NOW, lambda: (True, 'ready'), job)
                    waiting = process_queue(folder, NOW, lambda: (True, 'ready'), job)
                reruns = [call.args for call in job.gh.call_args_list if call.args[:2] == ('run', 'rerun')]
                expected = ('run', 'rerun', '123') + (() if conclusion == 'cancelled' else ('--failed',))
                self.assertEqual(reruns, [expected])
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(waiting['status'], 'waiting')
                self.assertEqual(load_state(Path(folder) / 'queue.json')['pending']['run_id'], 123)

    def test_successful_existing_run_is_acknowledged_without_pushing_older_main(self):
        with TemporaryDirectory() as folder:
            job, _ = fake_publication(folder, 'success')
            state = publication_state()
            def git(*args):
                if args[0] == 'push':
                    raise RuntimeError('remote main advanced')
                return SHA
            job.git.side_effect = git
            with patch.object(worker, 'read_json', return_value=FRESH_MENU):
                try:
                    job.publish(state)
                except RuntimeError as exc:
                    self.fail(f'Completed matching publication must be acknowledged: {exc}')
            self.assertFalse(any(call.args[0] == 'push' for call in job.git.call_args_list))

    def test_run_discovery_matches_snapshot_title_even_if_workflow_head_is_newer(self):
        job = RefreshJob('/unused')
        correct = {'databaseId': 123, 'headSha': 'b' * 40, 'displayTitle': f'Publish menu {SHA}'}
        wrong = {'databaseId': 456, 'headSha': SHA, 'displayTitle': 'Other publication'}
        job.gh = Mock(return_value=json.dumps([wrong, correct]))
        self.assertEqual(job.find_run(SHA), correct)

    def test_checkpoint_run_must_still_match_expected_snapshot_title(self):
        with TemporaryDirectory() as folder:
            job, run = fake_publication(folder, 'success')
            run['displayTitle'] = 'Other publication'
            with patch.object(worker, 'read_json', return_value=FRESH_MENU):
                with self.assertRaisesRegex(ValueError, 'snapshot'):
                    job.publish(publication_state())

    def test_busy_wakeup_does_not_replace_owned_worker_status(self):
        with TemporaryDirectory() as folder:
            process_queue(folder, NOW, lambda: (False, 'Low available memory'), Mock())
            status = Path(folder) / 'last-result.json'
            self.assertTrue(status.exists(), 'Owned worker result must be persisted under its lock')
            before = status.read_bytes()
            with exclusive_lock(Path(folder) / 'worker.lock'):
                self.assertEqual(process_queue(folder, NOW, Mock(), Mock())['status'], 'busy')
            self.assertEqual(status.read_bytes(), before)
            self.assertEqual(json.loads(before)['reason'], 'Low available memory')

    def test_model_cleanup_kills_owned_group_after_leader_already_exited(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'runtime').mkdir()
            (base / 'runtime/ollama').touch()
            process = Mock(pid=43210)
            process.poll.return_value = 1
            with patch.object(worker.subprocess, 'Popen', return_value=process), patch.object(worker.os, 'killpg') as kill:
                with self.assertRaisesRegex(RuntimeError, 'stopped during startup'):
                    with worker.local_model(base):
                        self.fail('Exited model server must not become ready')
            kill.assert_any_call(43210, signal.SIGTERM)
            kill.assert_any_call(43210, signal.SIGKILL)

    def test_model_cleanup_tolerates_process_group_disappearing(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'runtime').mkdir()
            (base / 'runtime/ollama').touch()
            process = Mock(pid=43210)
            process.poll.return_value = 1
            with patch.object(worker.subprocess, 'Popen', return_value=process), patch.object(worker.os, 'killpg', side_effect=ProcessLookupError):
                with self.assertRaisesRegex(RuntimeError, 'stopped during startup'):
                    with worker.local_model(base):
                        pass

    def test_status_includes_last_resource_deferral_reason(self):
        with TemporaryDirectory() as folder:
            status = Path(folder) / 'last-result.json'
            status.write_text(json.dumps({'status': 'deferred', 'reason': 'Low available memory'}))
            output = io.StringIO()
            with patch('sys.argv', ['local_refresh', '--base', folder, '--status']), redirect_stdout(output):
                worker.main()
            report = json.loads(output.getvalue())
            self.assertIn('last_result', report)
            self.assertEqual(report['last_result']['reason'], 'Low available memory')

    def test_sigterm_uses_stack_unwinding_and_restores_prior_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        def stop(*args):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler), 'Worker must install an unwinding SIGTERM handler')
            handler(signal.SIGTERM, None)
        with TemporaryDirectory() as folder:
            with patch('sys.argv', ['local_refresh', '--base', folder]), patch.object(worker, 'process_queue', side_effect=stop):
                with self.assertRaises(SystemExit) as raised:
                    worker.main()
            self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_expired_pending_publication_recollects_latest_due_period_before_publish(self):
        for has_ids in (True, False):
            with self.subTest(has_ids=has_ids), TemporaryDirectory() as folder:
                state = publication_state()
                if not has_ids:
                    state['pending'].update(commit_sha=None, run_id=None)
                save_state(Path(folder) / 'queue.json', state)
                job, _ = fake_publication(folder)
                current = [EXPIRED_MENU]
                def collect():
                    pending = load_state(Path(folder) / 'queue.json')['pending']
                    self.assertEqual(pending['phase'], 'collect')
                    self.assertEqual(pending['period'], '2026-10-19')
                    self.assertIsNone(pending['commit_sha'])
                    self.assertIsNone(pending['run_id'])
                    self.assertIsNone(pending['dispatch_requested_at'])
                    current[0] = FRESH_MENU
                job.collect = Mock(side_effect=collect)
                job.publish = Mock()
                with patch.object(worker, 'datetime', wraps=datetime) as clock, patch.object(worker, 'read_json', side_effect=lambda path: current[0]):
                    clock.now.return_value = OFFLINE_RETURN
                    result = process_queue(folder, OFFLINE_RETURN, lambda: (True, 'ready'), job)
                job.collect.assert_called_once()
                job.publish.assert_called_once()
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(load_state(Path(folder) / 'queue.json')['completed_period'], '2026-10-19')
                self.assertFalse(any(call.args[:2] == ('run', 'rerun') for call in job.gh.call_args_list))

    def test_successful_expired_publication_only_acknowledges_its_original_period(self):
        with TemporaryDirectory() as folder:
            save_state(Path(folder) / 'queue.json', publication_state())
            job, _ = fake_publication(folder, 'success')
            job.collect = Mock()
            job.publish = Mock()
            with patch.object(worker, 'datetime', wraps=datetime) as clock, patch.object(worker, 'read_json', return_value=EXPIRED_MENU):
                clock.now.return_value = OFFLINE_RETURN
                result = process_queue(folder, OFFLINE_RETURN, lambda: (True, 'ready'), job)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(load_state(Path(folder) / 'queue.json')['completed_period'], '2026-09-21')
            job.collect.assert_not_called()
            job.publish.assert_not_called()

    def test_expired_active_run_waits_without_watching_or_replacement(self):
        with TemporaryDirectory() as folder:
            job, run = fake_publication(folder)
            run.update(status='in_progress', conclusion=None)
            job.collect = Mock()
            job.publish = Mock()
            state = publication_state()
            with patch.object(worker, 'datetime', wraps=datetime) as clock, patch.object(worker, 'read_json', return_value=EXPIRED_MENU):
                clock.now.return_value = OFFLINE_RETURN
                with self.assertRaisesRegex(RuntimeError, 'still active'):
                    job(state)
            self.assertEqual(state['pending']['run_id'], 123)
            job.collect.assert_not_called()
            job.publish.assert_not_called()

    def test_stale_recollection_stops_after_one_attempt_without_recursive_publication(self):
        with TemporaryDirectory() as folder:
            job, _ = fake_publication(folder)
            job.collect = Mock()
            job.publish = Mock()
            with patch.object(worker, 'datetime', wraps=datetime) as clock, patch.object(worker, 'read_json', return_value=EXPIRED_MENU):
                clock.now.return_value = OFFLINE_RETURN
                with self.assertRaisesRegex(ValueError, 'past menus'):
                    job(publication_state())
            job.collect.assert_called_once()
            job.publish.assert_not_called()
            self.assertEqual(load_state(Path(folder) / 'queue.json')['pending']['phase'], 'collect')

    def test_publish_boundary_rejects_expired_snapshot_before_rerunning(self):
        with TemporaryDirectory() as folder:
            job, _ = fake_publication(folder)
            with patch.object(worker, 'datetime', wraps=datetime) as clock, patch.object(worker, 'read_json', return_value=EXPIRED_MENU):
                clock.now.return_value = OFFLINE_RETURN
                with self.assertRaisesRegex(ValueError, 'past menus'):
                    job.publish(publication_state())
            self.assertFalse(any(call.args[:2] == ('run', 'rerun') for call in job.gh.call_args_list))

if __name__=='__main__':unittest.main()
