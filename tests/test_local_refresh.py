import unittest
import json
import io
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager, redirect_stdout

from scripts import local_refresh as worker
from scripts.local_refresh import process_queue, RefreshJob, REMOTE
from scripts.refresh_queue import load_state, save_state, reconcile, exclusive_lock
from scripts.menu_source import parse_menu
from test_menu_source import page
from test_publication import translation_entry

from mensa import jobs as jobs
from mensa.errors import GenerationError

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
            with self.assertRaisesRegex(ValueError, 'unrelated'):
                RefreshJob(folder).collect()
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_text(), 'Keep target')

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
                    self.assertEqual(pending['period'], '2026-10-19T11:00')
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
                self.assertEqual(load_state(Path(folder) / 'queue.json')['completed_period'], '2026-10-19T11:00')
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
            self.assertEqual(load_state(Path(folder) / 'queue.json')['completed_period'], '2026-09-21T11:00')
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


class RefreshCompositionTests(unittest.TestCase):
    def setUp(self):
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        self.cache = {'schema_version': 1, 'entries': {}}
        record = self.menu['days'][0]['meals'][0]
        source = {'name_de': record['name_de'],
                  'components': [component['name_de'] for component in record['components']]}
        self.candidate = {'schema_version': 1,
                          'entries': {record['translation_key']: translation_entry(source)}}


    def test_translate_offline_first_uses_real_builder_and_forwards_checkpoint(self):
        record = self.menu['days'][0]['meals'][0]
        glossary = {label: {'en': 'Fixture', 'ko': '테스트'} for label in
                    ['vegan', 'Weizen', 'Sellerie', 'Milch und Laktose']}
        generated = {lang: self.candidate['entries'][record['translation_key']][lang]
                     for lang in ('en', 'ko')}
        for editorial in (True, False):
            with self.subTest(editorial=editorial), TemporaryDirectory() as folder:
                translate = RefreshJob(folder).translate
                checkpoints, events = [], []
                phrases = {text: {'en': 'Vegetable dish', 'ko': '채소 요리'} for text in
                           [record['name_de'], *(part['name_de'] for part in record['components'])]} if editorial else {}
                @contextmanager
                def model(base):
                    events.append('enter')
                    try:
                        yield {'provider': 'ollama', 'model': worker.MODEL,
                               'url': 'http://fixture.invalid/api/chat', 'key': ''}
                    finally:
                        events.append('exit')
                with (patch.object(worker, 'local_model', side_effect=AssertionError('Unexpected model') if editorial else model),
                      patch('scripts.translations.load_glossary', return_value=glossary),
                      patch('scripts.translations.request_translation', return_value=generated) as generation):
                    result = translate(self.menu, self.cache, phrases, checkpoint=checkpoints.append) if editorial else translate(self.menu, self.cache, phrases)
                entry = result['entries'][record['translation_key']]
                self.assertEqual(entry['en']['name'], 'Vegetable dish')
                self.assertEqual(entry['ko']['name'], '채소 요리')
                self.assertEqual(self.cache['entries'], {})
                if editorial:
                    self.assertEqual(checkpoints, [result])
                    generation.assert_not_called()
                    self.assertEqual(events, [])
                else:
                    self.assertEqual({lang: entry[lang] for lang in ('en', 'ko')}, generated)
                    generation.assert_called_once()
                    self.assertEqual(events, ['enter', 'exit'])


class JobOwnershipTests(unittest.TestCase):
    """JobOwnershipTests는 실제 JobRunner를 사용하여 비공개 작업 기록과 디스크에 저장되는 실행 결과를 검사합니다. 다른 실행이 잡은 잠금을 존중하고 자신이 맡은 작업만 수행하는지 확인합니다."""

    def runner(self, state_dir, *, resources=None, work=None):
        return jobs.JobRunner(state_dir, resources=resources if resources is not None else lambda: (True, 'ready'),
                              work=work if work is not None else lambda state: None)


    def test_publicly_accessible_state_directory_is_refused_without_repairs(self):
        with TemporaryDirectory() as folder:
            state_dir = Path(folder) / 'private'
            state_dir.mkdir(mode=0o755)
            state_dir.chmod(0o755)
            before_mode = state_dir.stat().st_mode
            resources, work = Mock(), Mock()
            with self.assertRaises(ValueError):
                self.runner(state_dir, resources=resources, work=work).run(NOW)
            self.assertEqual(state_dir.stat().st_mode, before_mode)
            self.assertEqual(list(state_dir.iterdir()), [])
            resources.assert_not_called()
            work.assert_not_called()

    def test_symlinked_queue_is_refused_without_changing_target_or_status(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            target = base / 'keep'
            target.write_text('unchanged')
            owned = base / 'queue.json'
            owned.symlink_to(target)
            before = set(base.iterdir())
            resources, work = Mock(), Mock()
            with self.assertRaises(ValueError):
                self.runner(base, resources=resources, work=work).run(NOW)
            self.assertEqual(set(base.iterdir()), before)
            self.assertEqual(target.read_text(), 'unchanged')
            self.assertTrue(owned.is_symlink())
            resources.assert_not_called()
            work.assert_not_called()

    def test_cancellation_preserves_pending_checkpoint_and_status_and_releases_lock(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            status = base / 'last-result.json'
            status.write_text('{"status":"previous"}')
            before = status.read_bytes()
            durable = publication_state()
            durable['pending']['dispatch_requested_at'] = NOW.isoformat()
            def work(state):
                state['pending'].update(durable['pending'])
                save_state(base / 'queue.json', state)
                raise SystemExit(143)
            with self.assertRaises(SystemExit) as raised:
                self.runner(base, work=work).run(NOW)
            self.assertEqual(raised.exception.code, 143)
            self.assertEqual(load_state(base / 'queue.json'), durable)
            self.assertEqual(status.read_bytes(), before)
            with exclusive_lock(base / 'worker.lock'):
                pass

    def test_final_status_replacement_still_owns_lock_and_busy_preserves_files(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            status = base / 'last-result.json'
            status.write_text('{"status":"previous"}')
            before = status.read_bytes()
            replace = jobs.os.replace
            observations = []
            def record_replace(source, destination):
                if Path(destination) == status:
                    self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                    queue_before = (base / 'queue.json').read_bytes()
                    resources, work = Mock(), Mock()
                    result = self.runner(base, resources=resources, work=work).run(NOW)
                    self.assertEqual(result, {'status': 'busy', 'reason': 'Another refresh worker holds the queue lock'})
                    self.assertEqual((base / 'queue.json').read_bytes(), queue_before)
                    self.assertEqual(status.read_bytes(), before)
                    resources.assert_not_called(); work.assert_not_called()
                    observations.append(result)
                return replace(source, destination)
            runner = self.runner(base)
            with patch.object(jobs.os, 'replace', side_effect=record_replace):
                self.assertEqual(runner.run(NOW)['status'], 'completed')
            self.assertEqual(len(observations), 1)
            self.assertEqual(json.loads(status.read_text())['status'], 'completed')
            self.assertEqual(list(base.glob('.last-result-*')), [])
            with exclusive_lock(base / 'worker.lock'):
                pass


class GenerationWorkerTests(unittest.TestCase):
    """GenerationWorkerTests는 process_queue가 종류별 번역 오류를 작업 기록에 저장하고, 재시도 불가 오류는 작업을 차단하며 재시도 가능 오류는 예정 시각까지 기다리는지 확인합니다. 실제 작업과 호스트 자원에 영향을 주지 않도록 작업 함수와 자원 확인 함수는 로컬 테스트 대체물을 사용합니다."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / 'queue.json'
        self.resources = Mock(return_value=(True, 'ready'))

    def test_permanent_failure_blocks_repeated_and_future_ticks(self):
        error = GenerationError('authentication', status_code=401)
        work = Mock(side_effect=error)
        first = process_queue(self.base, NOW, self.resources, work)
        self.assertEqual(first, {'status': 'blocked', 'reason': str(error),
                                'failure_code': 'authentication', 'retryable': False, 'period': '2026-09-21T11:00'})
        self.assertIsNone(load_state(self.path)['pending']['next_attempt_at'])
        self.resources.reset_mock(); work.reset_mock()
        for tick in (NOW + timedelta(hours=1), NOW + timedelta(weeks=4)):
            result = process_queue(self.base, tick, self.resources, work)
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['reason'], str(error))
            self.assertEqual(result['failure_code'], 'authentication')
            self.assertIs(result['retryable'], False)
            recorded = json.loads((self.base / 'last-result.json').read_text())
            self.assertEqual({key: value for key, value in recorded.items() if key != 'at'}, result)
        self.resources.assert_not_called(); work.assert_not_called()
        pending = load_state(self.path)['pending']
        self.assertEqual(pending['period'], '2026-10-19T11:00')
        self.assertEqual(pending['attempts'], 1)

    def test_explicit_retry_reenables_work_and_preserves_attempts(self):
        from scripts import refresh_queue
        process_queue(self.base, NOW, self.resources, Mock(side_effect=GenerationError('invalid_config')))
        save_state(self.path, refresh_queue.request_retry(load_state(self.path)))
        work = Mock()
        result = process_queue(self.base, NOW, self.resources, work)
        self.assertEqual(result['status'], 'completed')
        work.assert_called_once()

    def test_transient_failure_waits_until_retry_after_deadline(self):
        error = GenerationError('http_retryable', retry_after=3600)
        with patch.object(worker.time, 'monotonic', return_value=0):
            result = process_queue(self.base, NOW, self.resources, Mock(side_effect=error))
        self.assertEqual(result, {'status': 'failed', 'reason': str(error), 'period': '2026-09-21T11:00',
                                 'failure_code': 'http_retryable', 'retryable': True})
        self.assertEqual(load_state(self.path)['pending']['next_attempt_at'], '2026-09-21T11:00:00Z')
        self.resources.reset_mock(); work = Mock()
        result = process_queue(self.base, NOW + timedelta(seconds=3599), self.resources, work)
        self.assertEqual(result, {'status': 'waiting', 'retry_at': '2026-09-21T11:00:00Z'})
        self.resources.assert_not_called(); work.assert_not_called()
        result = process_queue(self.base, NOW + timedelta(hours=1), self.resources, work)
        self.assertEqual(result['status'], 'completed')
        work.assert_called_once()


    def test_cli_reports_failure_exit_for_blocked_and_failed_results(self):
        for status in ('blocked', 'failed'):
            with self.subTest(status=status), patch('sys.argv', ['local_refresh', '--base', str(self.base)]), \
                 patch.object(worker, 'process_queue', return_value={'status': status}), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    worker.main()
                self.assertEqual(raised.exception.code, 1)


class ConfiguredWorkerTests(unittest.TestCase):
    def setUp(self):
        self.job_type = worker.ConfiguredRefreshJob
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checkout = self.root / 'checkout'
        (self.checkout / 'data').mkdir(parents=True)
        self.state = self.root / 'private'
        self.public = self.root / 'public'
        self.glossary = {label: {'en': 'Checkout fixture', 'ko': '체크아웃 번역'} for label in
                         ['vegan', 'Weizen', 'Sellerie', 'Milch und Laktose']}
        (self.checkout / 'data/notice-translations.json').write_text(json.dumps(
            {'schema_version': 1, 'notices': self.glossary}), encoding='utf-8')
        self.config_path = self.root / 'worker.toml'
        self.config_path.write_text("""[paths]
checkout_dir = "checkout"
state_dir = "private"
public_dir = "public"
[inference]
mode = "managed"
provider = "ollama"
model = "fixture-model"
revision = "fixture-revision"
binary = "private/bin/ollama"
models_dir = "private/models"
log_dir = "private/logs"
[resources]
platform = "linux"
min_available_bytes = 1024
max_load_per_cpu = 1.5
""" + f'''[publication]
mode = "github"
repository = "Fixture/menu"
workflow = "update-and-deploy.yml"
remote = {json.dumps(str(self.root / 'remote.git'))}
''', encoding='utf-8')
        self.menu = parse_menu(page(), NOW.isoformat())
        record = self.menu['days'][0]['meals'][0]
        source = {'name_de': record['name_de'], 'components': [part['name_de'] for part in record['components']]}
        entry = translation_entry(source)
        self.generated = {lang: entry[lang] for lang in ('en', 'ko')}
        self.events = []

    @contextmanager
    def runtime(self, settings, *, key):
        self.events.append(('enter', settings.model, key))
        try:
            yield {'provider': settings.provider, 'model': settings.model,
                   'backend_revision': settings.revision, 'url': 'http://fixture.invalid/api/chat', 'key': key}
        finally:
            self.events.append(('exit',))

    def generation(self, source, config):
        self.events.append(('generate', config['model'], config['backend_revision'], config['key']))
        return self.generated

    def main_report(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            worker.main(list(arguments))
        return json.loads(output.getvalue())

    def test_directory_cli_publishes_declared_output_and_status_remains_read_only(self):
        (self.checkout/'data/editorial-translations.json').write_text(json.dumps({'phrases': {}}))
        from mensa.releases import DirectoryPublisher
        self.config_path.write_text(self.config_path.read_text().split('[publication]')[0] +
                                    '[publication]\nmode = "directory"\n')
        with (patch.object(worker, 'configured_resource_status', return_value=(True, 'fixture ready')),
              patch.object(worker, 'fetch_html', return_value=page()),
              patch.object(worker, 'parse_menu', side_effect=lambda html: parse_menu(html, NOW.isoformat())),
              patch.object(worker, 'datetime', wraps=datetime) as clock,
              patch.object(worker, 'model_session', side_effect=self.runtime),
              patch.object(worker, 'request_translation', side_effect=self.generation),
              patch.object(worker, 'command', side_effect=AssertionError('Directory must not execute commands')),
              patch.object(worker, 'local_model', side_effect=AssertionError('Directory must not use legacy model'))):
            clock.now.return_value = NOW
            completed = self.main_report('--config', str(self.config_path))
        self.assertEqual(completed['status'], 'completed')
        published = DirectoryPublisher(self.public, notice_glossary=self.glossary).load_current()
        entry = next(iter(published[1]['entries'].values()))
        self.assertEqual({lang: entry[lang] for lang in ('en', 'ko')}, self.generated)
        self.assertEqual(published[1]['notices'], self.glossary)
        self.assertEqual(json.loads((self.state/'translation-checkpoint.json').read_text()), published[1])
        files = {path: path.read_bytes() for directory in (self.state, self.public)
                 for path in directory.rglob('*') if path.is_file()}
        with (patch.object(worker, 'configured_resource_status', side_effect=AssertionError('Status resource probe')),
              patch.object(worker, 'fetch_html', side_effect=AssertionError('Status source')),
              patch.object(worker, 'model_session', side_effect=AssertionError('Status runtime')),
              patch.object(worker, 'request_translation', side_effect=AssertionError('Status generation'))):
            report = self.main_report('--config', str(self.config_path), '--status')
        self.assertIsNone(report['queue']['pending'])
        self.assertEqual(report['last_result']['status'], 'completed')
        self.assertEqual({path: path.read_bytes() for directory in (self.state, self.public)
                 for path in directory.rglob('*') if path.is_file()}, files)


    def test_configured_cli_defers_then_saves_private_translation_and_completes(self):
        from mensa import resources
        from mensa.resources import ResourceSnapshot
        candidates = []
        def work(job, state):
            self.events.append(('work',))
            candidate = job.translate(self.menu, {'schema_version': 1, 'entries': {}}, {})
            job.validate_complete(self.menu, candidate)
            candidates.append(candidate)
        with (patch.object(resources, 'linux_resources', side_effect=[ResourceSnapshot(0, 0, 2, True), ResourceSnapshot(2048, 0, 2, True)]),
              patch.object(worker, 'datetime') as clock,
              patch.object(self.job_type, '__call__', work),
              patch.object(worker, 'model_session', side_effect=self.runtime),
              patch.object(worker, 'request_translation', side_effect=self.generation),
              patch.object(worker, 'resource_status', side_effect=AssertionError('Legacy resource probe')),
              patch.object(worker, 'local_model', side_effect=AssertionError('Legacy runtime'))):
            clock.now.return_value = NOW
            deferred = self.main_report('--config', str(self.config_path))
            self.assertEqual(deferred['status'], 'deferred')
            self.assertEqual(self.events, [])
            self.assertIsNotNone(load_state(self.state / 'queue.json')['pending'])
            self.assertFalse((self.state / 'translation-checkpoint.json').exists())
            completed = self.main_report('--config', str(self.config_path))
            self.assertEqual(completed['status'], 'completed')
        candidate = candidates[0]
        entry = next(iter(candidate['entries'].values()))
        self.assertEqual({lang: entry[lang] for lang in ('en', 'ko')}, self.generated)
        self.assertEqual(entry['model'], 'fixture-model')
        self.assertEqual(candidate['notices'], self.glossary)
        self.assertEqual(json.loads((self.state / 'translation-checkpoint.json').read_text()), candidate)
        state = load_state(self.state / 'queue.json')
        self.assertIsNone(state['pending'])
        self.assertEqual(state['completed_period'], completed['period'])
        self.assertFalse(self.public.exists())


    def test_status_before_first_run_does_not_create_state(self):
        with patch.object(worker, 'configured_resource_status', side_effect=AssertionError('Status resources')):
            report = self.main_report('--config', str(self.config_path), '--status')
        self.assertIsNone(report['queue']['pending'])
        self.assertIsNone(report['last_result'])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.public.exists())


class SnapshotRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.repository_type = worker.SnapshotRepository

    @contextmanager
    def fixture(self):
        from types import SimpleNamespace
        from mensa.config import load_worker_config
        from mensa.translation_contract import source_for
        from test_menu_source import meal
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            remote, maintainer, checkout_dir = root/'remote.git', root/'maintainer', root/'checkout'
            remote.mkdir()
            def git_at(directory, *args):
                result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=directory,
                    capture_output=True, text=True, env={'PATH': __import__('os').environ['PATH'],
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'})
                if result.returncode:
                    raise RuntimeError('Fixture git failed: '+result.stderr)
                return result.stdout.rstrip('\n')
            git_at(remote, 'init', '--bare', '--initial-branch=main')
            git_at(root, 'clone', str(remote), str(maintainer))
            git_at(maintainer, 'config', 'user.name', 'Fixture Maintainer')
            git_at(maintainer, 'config', 'user.email', 'fixture@example.invalid')
            f = SimpleNamespace(root=root, remote=remote, maintainer=maintainer, checkout=checkout_dir,
                                now=datetime(2026,10,1,10,tzinfo=timezone.utc), calls=[], runs=[], dispatches=0,
                                collections=0, pushes=0, reruns=0, git_at=git_at)
            f.glossary = {label:{'en':'Fixture notice','ko':'테스트 표시'} for label in
                          ['vegan','Weizen','Sellerie','Milch und Laktose']}
            initial = parse_menu(page(meal(name='Gemüsepfanne',date='01.10.2026'),date='01.10.2026'),
                                 (f.now-timedelta(hours=1)).isoformat())
            record = initial['days'][0]['meals'][0]
            cache = {'schema_version':1,'entries':{record['translation_key']:translation_entry(source_for(record))},
                     'notices':f.glossary}
            def write(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
            f.write = write
            write(maintainer/'data/notice-translations.json',{'schema_version':1,'notices':f.glossary})
            write(maintainer/'data/editorial-translations.json',{'phrases':{}})
            write(maintainer/'site/data/menu.json',initial)
            write(maintainer/'site/data/translations.json',cache)
            (maintainer/'maintainer.txt').write_text('Initial maintainer file\n')
            git_at(maintainer,'add','.')
            git_at(maintainer,'commit','-m','Complete fixture')
            git_at(maintainer,'push','origin','HEAD:main')
            f.base = git_at(maintainer,'rev-parse','HEAD')
            git_at(root,'clone',str(remote),str(checkout_dir))
            git_at(checkout_dir,'config','user.name','Fixture Worker')
            git_at(checkout_dir,'config','user.email','worker@example.invalid')
            config_path=root/'worker.toml'
            config_path.write_text('''[paths]
checkout_dir="checkout"
state_dir="private"
public_dir="reserved-public"
[inference]
mode="managed"
provider="ollama"
model="fixture-model"
revision="fixture-revision"
binary="private/bin/ollama"
models_dir="private/models"
log_dir="private/logs"
[resources]
platform="linux"
min_available_bytes=1024
max_load_per_cpu=1.5
''' + f'''[publication]
mode="github"
repository="Fixture/menu"
workflow="update-and-deploy.yml"
remote={json.dumps(str(remote))}
''')
            f.config=load_worker_config(config_path)
            f.config.paths.state_dir.mkdir(mode=0o700)
            @contextmanager
            def runtime(settings,*,key):
                f.calls.append('runtime-enter')
                try:
                    yield {'provider':settings.provider,'model':settings.model,'backend_revision':settings.revision,
                           'url':'http://fixture.invalid/api/chat','key':key}
                finally:
                    f.calls.append('runtime-exit')
            def generate(source,config):
                f.calls.append('generate')
                entry=translation_entry(source)
                return {lang:entry[lang] for lang in ('en','ko')}
            def gh(*args):
                if args[:2]==('run','list'):return json.dumps(f.runs)
                if args[:2]==('run','view'):return json.dumps(next(r for r in f.runs if str(r['databaseId'])==args[2]))
                if args[:2]==('run','rerun'):
                    f.reruns+=1
                    return ''
                if args[:2]==('workflow','run'):
                    f.dispatches+=1
                    sha=load_state(f.config.paths.state_dir/'queue.json')['pending']['commit_sha']
                    f.runs=[{'databaseId':123,'displayTitle':f'Publish menu {sha}', 'status':'completed','conclusion':'success'}]
                    return ''
                raise AssertionError('Unexpected fake publication command')
            f.intercept = None
            def worker_git(*args):
                if args[0]=='push':f.pushes+=1
                if f.intercept is not None:return f.intercept(args)
                return git_at(checkout_dir,*args)
            def job():
                result=worker.ConfiguredRefreshJob(f.config,translate=generate,runtime=runtime)
                result.git=worker_git
                result.gh=gh
                return result
            f.job=job
            f.repository=lambda:self.repository_type(checkout_dir,f.config.paths.state_dir,git=worker_git)
            def fetch():
                f.collections+=1
                return page(date=f.now.strftime('%d.%m.%Y'))
            def advance(descendant=False):
                if descendant:
                    git_at(maintainer,'fetch','origin','main')
                    git_at(maintainer,'merge','--ff-only','origin/main')
                current=parse_menu(page(date=f.now.strftime('%d.%m.%Y')),f.now.isoformat())['days'][0]['meals'][0]
                reviewed=translation_entry(source_for(current))
                reviewed['en']['name']='Latest maintainer review'
                reviewed['ko']['name']='최신 검토 번역'
                public=json.loads((maintainer/'site/data/translations.json').read_text())
                public['entries'][current['translation_key']]=reviewed
                write(maintainer/'site/data/translations.json',public)
                (maintainer/'maintainer.txt').write_text('Latest unrelated maintainer change\n')
                git_at(maintainer,'add','site/data/translations.json','maintainer.txt')
                git_at(maintainer,'commit','-m','Reviewed translation and unrelated change')
                git_at(maintainer,'push','origin','HEAD:main')
                return git_at(maintainer,'rev-parse','HEAD')
            f.advance=advance
            with (patch.object(worker,'REMOTE',str(remote)),
                  patch.object(worker,'fetch_html',side_effect=fetch),
                  patch.object(worker,'parse_menu',side_effect=lambda html:parse_menu(html,f.now.isoformat())),
                  patch.object(worker,'datetime',wraps=datetime) as clock,
                  patch('scripts.update_menu.datetime',wraps=datetime) as source_clock,
                  patch('scripts.update_menu.load_glossary',return_value=f.glossary),
                  patch.object(worker,'resource_status',side_effect=AssertionError('No host resources')),
                  patch.object(worker,'local_model',side_effect=AssertionError('No real model'))):
                clock.now.side_effect=lambda *args:f.now
                source_clock.now.side_effect=lambda *args:f.now
                yield f

    def prepared(self, f):
        job=f.job()
        job.collect()
        period='2026-10-01T11:00'
        sha=f.repository().prepare_snapshot(period)
        state=reconcile({'schema_version':1,'completed_period':None,'pending':None},f.now)
        state['pending'].update(phase='publish',commit_sha=sha)
        save_state(job.state_path,state)
        return job,period,sha

    def test_configured_transport_honors_repository_workflow_remote_and_watch(self):
        from dataclasses import replace
        for wrong_origin in (False, True):
            with self.subTest(wrong_origin=wrong_origin), self.fixture() as f:
                f.config = replace(f.config, publication=replace(f.config.publication,
                    repository='OtherOwner/other-site', workflow='fixture-release.yaml'))
                job, period, sha = self.prepared(f)
                del job.gh  # 이 테스트는 job.gh 대체 함수를 제거하여 실제 명령 구성 코드가 지정한 저장소를 명령 인자에 반영하는지 확인합니다.
                commands = []
                visible = []
                def transport(argv, cwd=None, timeout=120):
                    self.assertEqual(argv[0], 'gh')
                    commands.append((argv, cwd, timeout))
                    if argv[1:3] == ['run', 'list']:
                        return json.dumps(visible)
                    if argv[1:3] == ['workflow', 'run']:
                        durable = load_state(job.state_path)['pending']
                        self.assertEqual((durable['period'], durable['commit_sha']), (period, sha))
                        self.assertIsNotNone(durable['dispatch_requested_at'])
                        visible.append({'databaseId': 123, 'displayTitle': f'Publish menu {sha}',
                                        'status': 'in_progress', 'conclusion': None})
                        return ''
                    if argv[1:3] == ['run', 'watch']:
                        visible[0].update(status='completed', conclusion='success')
                        return ''
                    if argv[1:3] == ['run', 'view']:
                        return json.dumps(visible[0])
                    raise AssertionError('Unexpected publication command')
                before_files = {name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}
                if wrong_origin:
                    f.git_at(f.checkout, 'remote', 'set-url', 'origin', str(f.root/'unexpected.git'))
                with (patch.object(worker, 'REMOTE', str(f.root/'legacy-default.git')),
                      patch.object(worker, 'command', side_effect=transport)):
                    result = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), job)
                self.assertEqual(f.git_at(f.checkout, 'rev-parse', 'HEAD'), sha)
                self.assertEqual({name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}, before_files)
                if wrong_origin:
                    self.assertEqual(result['status'], 'failed')
                    self.assertIn('remote', result['reason'])
                    pending = load_state(job.state_path)['pending']
                    self.assertEqual((pending['period'], pending['commit_sha'], pending['run_id'], pending['dispatch_requested_at']),
                                     (period, sha, None, None))
                    self.assertEqual(commands, [])
                    self.assertEqual(f.pushes, 0)
                else:
                    self.assertEqual((result['status'], result['period']), ('completed', period))
                    self.assertIsNone(load_state(job.state_path)['pending'])
                    self.assertEqual(f.git_at(f.remote, 'rev-parse', 'main'), sha)
                    self.assertEqual(f.pushes, 1)
                    for argv, cwd, timeout in commands:
                        self.assertEqual(argv[-2:], ['--repo', 'OtherOwner/other-site'])
                        self.assertEqual(cwd, f.checkout)
                    listing = next(argv for argv, _, _ in commands if argv[1:3] == ['run', 'list'])
                    self.assertEqual(listing[listing.index('--workflow')+1], 'fixture-release.yaml')
                    dispatch = next(argv for argv, _, _ in commands if argv[1:3] == ['workflow', 'run'])
                    self.assertEqual(dispatch[3:8], ['fixture-release.yaml', '--ref', 'main', '-f', f'snapshot_sha={sha}'])
                    watch = next(item for item in commands if item[0][1:3] == ['run', 'watch'])
                    self.assertEqual(watch[0][3:8], ['123', '--exit-status', '--interval', '15', '--repo'])
                    self.assertEqual(watch[2], 600)

    def test_lost_dispatch_response_preserves_checkpoint_until_matching_run_is_visible(self):
        with self.fixture() as f:
            job, period, sha = self.prepared(f)
            calls = []
            accepted = []
            actual_gh = job.gh
            def gh(*args):
                calls.append(args)
                if args[:2] == ('workflow', 'run'):
                    durable = load_state(job.state_path)['pending']
                    self.assertEqual((durable['period'], durable['commit_sha']), (period, sha))
                    self.assertIsNotNone(durable['dispatch_requested_at'])
                    accepted.append({'databaseId': 321, 'displayTitle': f'Publish menu {sha}',
                                     'status': 'completed', 'conclusion': 'success'})
                    raise OSError('Accepted dispatch response lost')
                return actual_gh(*args)
            job.gh = gh
            failed = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), job)
            self.assertEqual(failed['status'], 'failed')
            durable = load_state(job.state_path)
            pending = durable['pending'].copy()
            self.assertEqual((pending['period'], pending['commit_sha'], pending['run_id']), (period, sha, None))
            self.assertIsNotNone(pending['dispatch_requested_at'])
            retained = {name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}
            f.now = datetime.fromisoformat(pending['next_attempt_at']) + timedelta(seconds=1)
            invisible = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), job)
            self.assertEqual(invisible['status'], 'failed')
            self.assertIn('not visible yet', invisible['reason'])
            now_pending = load_state(job.state_path)['pending']
            self.assertEqual((now_pending['period'], now_pending['commit_sha'], now_pending['run_id'], now_pending['dispatch_requested_at']),
                             (period, sha, None, pending['dispatch_requested_at']))
            f.now = datetime.fromisoformat(now_pending['next_attempt_at']) + timedelta(seconds=1)
            f.runs = accepted
            completed = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), job)
            self.assertEqual((completed['status'], completed['period']), ('completed', period))
            self.assertIsNone(load_state(job.state_path)['pending'])
            self.assertEqual(f.git_at(f.checkout, 'rev-parse', 'HEAD'), sha)
            self.assertEqual({name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}, retained)
            self.assertEqual(f.collections, 1)
            self.assertEqual(f.pushes, 1)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(len([args for args in calls if args[:2] == ('workflow', 'run')]), 1)

    def test_contention_rebuilds_latest_and_rolls_period_only_after_preparation(self):
        for descendant,rollover in ((False,False),(True,False),(False,True)):
            with self.subTest(descendant=descendant,rollover=rollover), self.fixture() as f:
                raced=[False]
                def race(args):
                    if args[0]=='push' and not raced[0]:
                        raced[0]=True
                        if descendant:
                            f.git_at(f.checkout,*args)
                            f.advance(descendant=True)
                            raise OSError('Lost response after successful local push')
                        f.advance()
                    return f.git_at(f.checkout,*args)
                f.intercept=race
                first=process_queue(f.config.paths.state_dir,f.now,lambda:(True,'fixture ready'),f.job())
                self.assertEqual(first['status'],'failed')
                original=load_state(f.config.paths.state_dir/'queue.json')['pending']
                S=original['commit_sha']
                self.assertEqual(f.collections,1)
                self.assertEqual(f.dispatches,0)
                self.assertEqual(f.git_at(f.checkout,'rev-parse','HEAD'),S)
                f.intercept=None
                f.now=datetime(2026,10,5,10,tzinfo=timezone.utc) if rollover else f.now+timedelta(minutes=20)
                result=process_queue(f.config.paths.state_dir,f.now,lambda:(True,'fixture ready'),f.job())
                self.assertEqual(result['status'],'completed')
                self.assertEqual(result['period'],'2026-10-05T11:00' if rollover else '2026-10-01T11:00')
                R=f.git_at(f.checkout,'rev-parse','HEAD')
                self.assertNotEqual(R,S)
                self.assertEqual(R,f.git_at(f.remote,'rev-parse','main'))
                if not descendant:self.assertEqual(f.git_at(f.checkout,'rev-parse','refs/mensa/superseded/'+S),S)
                self.assertEqual((f.checkout/'maintainer.txt').read_text(),'Latest unrelated maintainer change\n')
                candidate=json.loads((f.checkout/'site/data/translations.json').read_text())
                current=parse_menu(page(date=f.now.strftime('%d.%m.%Y')),f.now.isoformat())['days'][0]['meals'][0]
                self.assertEqual(candidate['entries'][current['translation_key']]['en']['name'],'Latest maintainer review')
                self.assertEqual(candidate['entries'][current['translation_key']]['ko']['name'],'최신 검토 번역')
                self.assertEqual(f.calls,['runtime-enter','generate','runtime-exit'])
                self.assertEqual((f.collections,f.pushes,f.dispatches),(2,2,1))

    def test_dispatched_or_visible_run_keeps_original_snapshot_identity(self):
        with self.fixture() as f:
            job, period, S = self.prepared(f)
            f.git_at(f.checkout, 'push', 'origin', 'HEAD:main')
            f.advance(descendant=True)
            state = load_state(job.state_path)
            requested = (f.now - timedelta(minutes=40)).isoformat()
            state['pending']['dispatch_requested_at'] = requested
            save_state(job.state_path, state)
            job.publish(state)
            self.assertEqual(f.git_at(f.checkout, 'rev-parse', 'HEAD'), S)
            self.assertEqual(f.collections, 1)
            self.assertEqual(state['pending']['commit_sha'], S)
            self.assertEqual(state['pending']['period'], period)
            self.assertEqual(state['pending']['dispatch_requested_at'], requested)

        for case in ('success', 'ack-write-gap', 'prepared-conflict'):
            with self.subTest(visible_original=case), self.fixture() as f:
                job, period, S = self.prepared(f)
                M = f.advance()
                f.git_at(f.checkout, 'fetch', 'origin', 'main')
                repository = f.repository()
                repository.plan_replacement(period, S, M)
                if case == 'prepared-conflict':
                    repository.resume_replacement(period, S)
                    job.collect()
                    repository.prepare_snapshot(period)
                before = (f.git_at(f.checkout, 'rev-parse', 'HEAD'), f.git_at(f.checkout, 'rev-parse', 'main'))
                original_files = {name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}
                f.runs = [{'databaseId': 123, 'displayTitle': f'Publish menu {S}',
                           'status': 'completed', 'conclusion': 'success'}]
                if case == 'ack-write-gap':
                    import mensa.jobs as job_module
                    actual_save = job_module.save_state
                    def interrupted_ack(path, state):
                        if state['pending'] is None:
                            raise OSError('Original queue acknowledgment interrupted')
                        return actual_save(path, state)
                    with patch.object(job_module, 'save_state', side_effect=interrupted_ack):
                        with self.assertRaisesRegex(OSError, 'acknowledgment interrupted'):
                            process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), f.job())
                    durable = load_state(job.state_path)
                    self.assertEqual((durable['pending']['period'], durable['pending']['commit_sha'], durable['pending']['run_id']),
                                     (period, S, 123))
                    self.assertIsNone(durable['completed_period'])
                result = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), f.job())
                self.assertEqual(f.dispatches, 0)
                self.assertEqual(f.collections, 2 if case == 'prepared-conflict' else 1)
                self.assertEqual((f.git_at(f.checkout, 'rev-parse', 'HEAD'), f.git_at(f.checkout, 'rev-parse', 'main')), before)
                self.assertEqual({name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}, original_files)
                durable = load_state(job.state_path)
                if case == 'prepared-conflict':
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(durable['pending']['commit_sha'], S)
                    self.assertIn('inspection', result['reason'])
                else:
                    self.assertEqual((result['status'], result['period']), ('completed', period))
                    self.assertEqual(durable['completed_period'], period)
                    self.assertIsNone(durable['pending'])
                    self.assertIsNotNone(json.loads((f.config.paths.state_dir/'snapshot-journal.json').read_text())['replacement'])
                    f.now = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
                    following = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), f.job())
                    self.assertEqual((following['status'], following['period']), ('completed', '2026-10-05T11:00'))
                    self.assertEqual((f.dispatches, f.collections), (1, 2))
                    self.assertEqual((f.checkout/'maintainer.txt').read_text(), 'Latest unrelated maintainer change\n')
                    self.assertIsNone(json.loads((f.config.paths.state_dir/'snapshot-journal.json').read_text())['replacement'])

        with self.fixture() as f:
            job, period, S = self.prepared(f)
            M = f.advance()
            f.git_at(f.checkout, 'fetch', 'origin', 'main')
            f.repository().plan_replacement(period, S, M)
            state = load_state(job.state_path)
            state['pending']['commit_sha'] = 'f' * 40
            save_state(job.state_path, state)
            original = (state['pending']['period'], state['pending']['commit_sha'], state['pending']['run_id'])
            f.runs = [{'databaseId': 123, 'displayTitle': f'Publish menu {S}', 'status': 'completed', 'conclusion': 'success'}]
            journal = f.config.paths.state_dir/'snapshot-journal.json'
            before = (f.git_at(f.checkout, 'rev-parse', 'HEAD'), f.git_at(f.checkout, 'rev-parse', 'main'), journal.read_bytes(),
                      {name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES})
            result = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'fixture ready'), f.job())
            self.assertEqual(result['status'], 'failed')
            durable = load_state(job.state_path)
            self.assertEqual((durable['pending']['period'], durable['pending']['commit_sha'], durable['pending']['run_id']), original)
            self.assertIsNone(durable['completed_period'])
            self.assertEqual((f.git_at(f.checkout, 'rev-parse', 'HEAD'), f.git_at(f.checkout, 'rev-parse', 'main'), journal.read_bytes(),
                              {name: (f.checkout/name).read_bytes() for name in worker.DATA_FILES}), before)
            self.assertEqual((f.collections, f.dispatches, f.reruns), (1, 0, 0))

    def test_unknown_history_files_or_provenance_reject_before_checkout_mutation(self):
        for bad in ('untracked','ignored','bytecode-collision','commit'):
            with self.subTest(bad=bad), self.fixture() as f:
                job,period,S=self.prepared(f)
                M=f.advance()
                f.git_at(f.checkout,'fetch','origin','main')
                journal=f.config.paths.state_dir/'snapshot-journal.json'
                if bad=='untracked':(f.checkout/'unknown.txt').write_text('Keep unknown bytes\n')
                elif bad in ('ignored', 'bytecode-collision'):
                    ignored = 'ignored-target.txt' if bad == 'ignored' else 'mensa/__pycache__/queue.cpython-314.pyc'
                    target = ignored
                    (f.checkout/'.git/info/exclude').write_text(ignored+'\n')
                    (f.checkout/ignored).parent.mkdir(parents=True, exist_ok=True)
                    (f.maintainer/target).parent.mkdir(parents=True, exist_ok=True)
                    (f.checkout/ignored).write_text('Keep ignored local bytes\n')
                    (f.maintainer/target).write_text('Upstream tracked content\n')
                    f.git_at(f.maintainer,'add','--force',target)
                    f.git_at(f.maintainer,'commit','-m','Track conflicting ignored path')
                    f.git_at(f.maintainer,'push','origin','HEAD:main')
                    M=f.git_at(f.maintainer,'rev-parse','HEAD')
                    f.git_at(f.checkout,'fetch','origin','main')
                elif bad=='commit':
                    (f.checkout/'maintainer.txt').write_text('Unknown local commit\n')
                    f.git_at(f.checkout,'add','maintainer.txt')
                    f.git_at(f.checkout,'commit','-m','Refresh menu with locally validated translations')
                before=(f.git_at(f.checkout,'rev-parse','HEAD'),f.git_at(f.checkout,'rev-parse','main'),journal.read_bytes(),job.state_path.read_bytes(),
                        {p.relative_to(f.checkout).as_posix():p.read_bytes() for p in [f.checkout/'maintainer.txt',f.checkout/'site/data/menu.json',f.checkout/'site/data/translations.json']})
                with self.assertRaises(ValueError):f.repository().plan_replacement(period,S,M)
                after=(f.git_at(f.checkout,'rev-parse','HEAD'),f.git_at(f.checkout,'rev-parse','main'),journal.read_bytes(),job.state_path.read_bytes(),
                       {p.relative_to(f.checkout).as_posix():p.read_bytes() for p in [f.checkout/'maintainer.txt',f.checkout/'site/data/menu.json',f.checkout/'site/data/translations.json']})
                self.assertEqual(after,before)
                if bad=='untracked':self.assertEqual((f.checkout/'unknown.txt').read_text(),'Keep unknown bytes\n')
                if bad in ('ignored', 'bytecode-collision'):
                    self.assertEqual((f.checkout/ignored).read_text(),'Keep ignored local bytes\n')

    def test_reconstruction_resumes_after_recorded_main_ref_update(self):
        with self.fixture() as f:
            job, period, S = self.prepared(f)
            M = f.advance()
            f.git_at(f.checkout, 'fetch', 'origin', 'main')
            f.repository().plan_replacement(period, S, M)
            stopped = [False]
            def interrupt(args):
                value = f.git_at(f.checkout, *args)
                if args[:2] == ('update-ref', 'refs/heads/main') and not stopped[0]:
                    stopped[0] = True
                    raise SystemExit(143)
                return value
            f.intercept = interrupt
            with self.assertRaises(SystemExit):
                process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'ready'), f.job())
            self.assertEqual(load_state(job.state_path)['pending']['commit_sha'], S)
            self.assertEqual(f.collections, 1)
            f.intercept = None
            result = process_queue(f.config.paths.state_dir, f.now, lambda: (True, 'ready'), f.job())
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(f.git_at(f.checkout, 'rev-parse', 'HEAD'), f.git_at(f.remote, 'rev-parse', 'main'))
            self.assertEqual(f.git_at(f.checkout, 'rev-parse', 'refs/mensa/superseded/'+S), S)
            self.assertEqual(f.collections, 2)

    def test_prepared_commit_is_adopted_from_exact_tree_after_interruption(self):
        with self.fixture() as f:
            f.job().collect()
            stopped=[False]
            def interrupt(args):
                result=f.git_at(f.checkout,*args)
                if args[0]=='commit' and not stopped[0]:stopped[0]=True;raise SystemExit(143)
                return result
            f.intercept=interrupt
            with self.assertRaises(SystemExit):f.repository().prepare_snapshot('2026-09-28')
            S=f.git_at(f.checkout,'rev-parse','HEAD')
            f.intercept=None
            self.assertEqual(f.repository().prepare_snapshot('2026-09-28'),S)
            self.assertEqual(f.git_at(f.checkout,'rev-parse','HEAD'),S)
            journal=json.loads((f.config.paths.state_dir/'snapshot-journal.json').read_text())
            self.assertEqual(journal['snapshot']['sha'],S)
            self.assertEqual(journal['snapshot']['base'],f.base)

    def test_queue_write_and_marker_cleanup_gaps_reuse_prepared_replacement(self):
        for gap in ('queue','cleanup','expired-ready'):
            with self.subTest(gap=gap), self.fixture() as f:
                job,period,S=self.prepared(f);M=f.advance();f.git_at(f.checkout,'fetch','origin','main')
                f.repository().plan_replacement(period,S,M)
                next_job=f.job()
                real_checkpoint=next_job.checkpoint
                def checkpoint(state):
                    if state['pending']['commit_sha']!=S:raise OSError('Queue write interrupted')
                    real_checkpoint(state)
                if gap in ('queue','expired-ready'):
                    next_job.checkpoint=checkpoint
                    result=process_queue(f.config.paths.state_dir,f.now,lambda:(True,'ready'),next_job)
                    self.assertEqual(result['status'],'failed')
                    self.assertEqual(load_state(job.state_path)['pending']['commit_sha'],S)
                else:
                    with patch.object(self.repository_type,'complete_replacement',side_effect=SystemExit(143)):
                        with self.assertRaises(SystemExit):process_queue(f.config.paths.state_dir,f.now,lambda:(True,'ready'),next_job)
                    pending=load_state(job.state_path)['pending']
                    self.assertNotEqual(pending['commit_sha'],S)
                    self.assertEqual(pending['period'],period)
                R=f.git_at(f.checkout,'rev-parse','HEAD')
                collections=f.collections
                f.now=f.now+timedelta(minutes=20)
                if gap=='expired-ready':f.now=datetime(2026,10,5,10,tzinfo=timezone.utc)
                result=process_queue(f.config.paths.state_dir,f.now,lambda:(True,'ready'),f.job())
                if gap=='expired-ready':
                    self.assertEqual(result['status'],'failed')
                    self.assertIn('expired',result['reason'].lower())
                    self.assertEqual(load_state(job.state_path)['pending']['commit_sha'],S)
                    self.assertEqual(f.dispatches,0)
                else:
                    self.assertEqual(result['status'],'completed')
                    self.assertEqual(R,f.git_at(f.remote,'rev-parse','main'))
                    self.assertEqual(f.dispatches,1)
                self.assertEqual(f.collections,collections)
                self.assertEqual(f.git_at(f.checkout,'rev-parse','HEAD'),R)

    def test_unproven_dirty_recollection_is_retained_after_failure(self):
        with self.fixture() as f:
            job,period,S=self.prepared(f);M=f.advance();f.git_at(f.checkout,'fetch','origin','main')
            f.repository().plan_replacement(period,S,M)
            real_write=worker.write_snapshot
            def fail_after_write(*args,**kwargs):
                real_write(*args,**kwargs)
                raise OSError('Interrupted before complete prepared tree')
            with patch.object(worker,'write_snapshot',side_effect=fail_after_write):
                first=process_queue(f.config.paths.state_dir,f.now,lambda:(True,'ready'),f.job())
            self.assertEqual(first['status'],'failed')
            files={name:(f.checkout/name).read_bytes() for name in worker.DATA_FILES}
            head=f.git_at(f.checkout,'rev-parse','HEAD')
            f.now+=timedelta(minutes=20)
            second=process_queue(f.config.paths.state_dir,f.now,lambda:(True,'ready'),f.job())
            self.assertEqual(second['status'],'failed')
            self.assertEqual(files,{name:(f.checkout/name).read_bytes() for name in worker.DATA_FILES})
            self.assertEqual(f.git_at(f.checkout,'rev-parse','HEAD'),head)
            self.assertEqual(load_state(job.state_path)['pending']['commit_sha'],S)
            self.assertEqual(f.collections,2)

    def test_publication_preserves_ignored_python_bytecode_cache(self):
        """게시 검사는 실행 중 생성한 Python 캐시를 실제 식단 변경으로 판단하면 안 됩니다."""
        with self.fixture() as f:
            job, period, sha = self.prepared(f)
            cache = f.checkout / 'mensa/__pycache__/queue.cpython-314.pyc'
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b'generated bytecode cache')
            (f.checkout / '.git/info/exclude').write_text('__pycache__/\n')
            result = worker.process_queue(f.config.paths.state_dir, f.now,
                                         lambda: (True, 'ready'), job)
            self.assertEqual(result, {'status': 'completed', 'period': period})
            self.assertEqual(f.collections, 1)
            self.assertEqual(f.dispatches, 1)
            self.assertEqual(cache.read_bytes(), b'generated bytecode cache')

    def test_journal_save_failure_preserves_record_and_git_head(self):
        with self.fixture() as f:
            repository = f.repository()
            repository.prepare_snapshot('2026-09-28')
            journal = f.config.paths.state_dir/'snapshot-journal.json'
            before = journal.read_bytes()
            f.job().collect()
            with patch('mensa.git_repository.os.replace', side_effect=OSError('Journal replace failed')):
                with self.assertRaises(OSError):
                    repository.prepare_snapshot('2026-09-28')
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(f.git_at(f.checkout, 'rev-parse', 'HEAD'), f.base)

if __name__=='__main__':unittest.main()
