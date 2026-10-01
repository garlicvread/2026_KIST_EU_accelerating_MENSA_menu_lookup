"""이 테스트 모듈은 설정한 디렉터리에 메뉴를 게시하는 전체 과정을 확인하기 위해 실제 비공개 JSON 파일, 게시본 파일, 디스크에 저장하는 작업 대기열을 사용합니다."""

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mensa.config import load_worker_config, PublicationSettings
from mensa.jobs import JobRunner
from mensa.queue import load_state, save_state, reconcile
from mensa.releases import DirectoryPublisher
from mensa.translation_contract import source_for
from scripts.menu_source import parse_menu
from test_menu_source import meal, page
from test_publication import translation_entry

MODULE = Path(__file__).resolve().parents[1] / 'mensa/directory_refresh.py'
adapter = None
if MODULE.exists():
    spec = importlib.util.spec_from_file_location('mensa.directory_refresh', MODULE)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

NOW = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)


class DirectoryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(adapter, 'Directory refresh implementation is missing')
        self.job_type = getattr(adapter, 'DirectoryRefreshJob', None)
        self.assertTrue(callable(self.job_type), 'DirectoryRefreshJob implementation is missing')

    @contextmanager
    def fixture(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            checkout = root/'checkout'
            (checkout/'data').mkdir(parents=True)
            config_path = root/'worker.toml'
            config_path.write_text('''[paths]
checkout_dir="checkout"
state_dir="private"
public_dir="website"
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
[publication]
mode="directory"
''')
            config = load_worker_config(config_path)
            f = SimpleNamespace(root=root, checkout=checkout, config=config, state=config.paths.state_dir,
                public=config.paths.public_dir, now=NOW, requests=[], active=False, opened=0, closed=0,
                source_calls=0, clock_calls=0, fail_name=None)
            f.glossary = {label: {'en': 'Reviewed fixture notice', 'ko': '검토된 표시'} for label in
                          ('vegan', 'Weizen', 'Sellerie', 'Milch und Laktose')}
            def write(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            f.write = write
            write(checkout/'data/notice-translations.json', {'schema_version': 1, 'notices': f.glossary})
            write(checkout/'data/editorial-translations.json', {'phrases': {}})
            def menu(names=('Suppe A', 'Suppe B'), day='21.09.2026'):
                return parse_menu(page(''.join(meal(name=name, date=day) for name in names), date=day), f.now.isoformat())
            f.menu_for = menu
            f.menu = menu()
            def source():
                f.source_calls += 1
                return copy.deepcopy(f.menu)
            def clock():
                f.clock_calls += 1
                return f.now
            @contextmanager
            def runtime(settings, *, key):
                f.opened += 1
                f.active = True
                try:
                    yield {'provider': settings.provider, 'model': settings.model, 'backend_revision': settings.revision,
                           'url': 'http://fixture.invalid/api/chat', 'key': key}
                finally:
                    f.active = False
                    f.closed += 1
            def generate(source, config):
                f.requests.append((copy.deepcopy(source), copy.deepcopy(config)))
                if source['name_de'] == f.fail_name:
                    raise OSError('Fixture generation interrupted')
                entry = translation_entry(source)
                entry['en']['name'] = 'Generated '+source['name_de']
                entry['ko']['name'] = '생성된 '+source['name_de']
                return {lang: entry[lang] for lang in ('en', 'ko')}
            f.source, f.clock, f.runtime, f.generate = source, clock, runtime, generate
            def job(**overrides):
                ports = dict(source=source, clock=clock, translate=generate, runtime=runtime, key='fixture-secret')
                ports.update(overrides)
                return self.job_type(f.config, **ports)
            f.job = job
            f.run = lambda **ports: JobRunner(f.state, resources=lambda: (True, 'fixture ready'), work=job(**ports)).run(f.now)
            f.publisher = DirectoryPublisher(f.public, notice_glossary=f.glossary)
            def cache(menu):
                return {'schema_version': 1, 'entries': {record['translation_key']: translation_entry(source_for(record))
                    for day in menu['days'] for record in day['meals']}, 'notices': copy.deepcopy(f.glossary)}
            f.cache = cache
            f.queue = f.state/'queue.json'
            yield f

    def pending(self, now=NOW):
        return reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, now)

    def test_constructor_and_pending_boundary_reject_before_ports_or_io(self):
        with self.fixture() as f:
            forbidden = lambda *args, **kwargs: self.fail('Unexpected boundary effect')
            with (patch.object(Path, 'read_text', side_effect=forbidden), patch.object(Path, 'mkdir', side_effect=forbidden)):
                job = f.job(source=forbidden, clock=forbidden, translate=None, runtime=None)
                for config, ports in ((None, {}), (replace(f.config, publication=PublicationSettings('github', 'Fixture/menu', 'publish.yml', '/fixture/remote.git')), {}),
                        (f.config, {'source': None}), (f.config, {'clock': 1}), (f.config, {'translate': 1}),
                        (f.config, {'runtime': 1}), (f.config, {'key': None})):
                    with self.subTest(config=config, ports=ports), self.assertRaises(ValueError):
                        self.job_type(config, source=ports.get('source', forbidden), **{key: value for key, value in ports.items() if key!='source'})
                for change in ({'phase': 'publish'}, {'commit_sha': 'a'*40}, {'run_id': 123}, {'dispatch_requested_at': NOW.isoformat()}):
                    state = self.pending(); state['pending'].update(change)
                    with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'collect'):
                        job(state)
            self.assertFalse(f.state.exists())
            self.assertFalse(f.public.exists())

    def test_malformed_clock_fails_before_glossary_source_or_files(self):
        with self.fixture() as f:
            for value in (None, '2026-09-21', NOW.replace(tzinfo=None)):
                with self.subTest(value=value), patch.object(Path, 'read_text', side_effect=AssertionError('Clock must precede reads')):
                    with self.assertRaisesRegex(ValueError, 'aware'):
                        f.job(clock=lambda: value)(self.pending())
            self.assertEqual((f.source_calls, f.opened, f.requests), (0, 0, []))
            self.assertFalse(f.state.exists())
            self.assertFalse(f.public.exists())

    def test_complete_pair_uses_one_glossary_and_runtime_closes_before_real_selection(self):
        with self.fixture() as f:
            f.write(f.checkout/'site/data/menu.json', f.menu_for(('Suppe A',)))
            before = {path: path.read_bytes() for path in f.checkout.rglob('*') if path.is_file()}
            f.state.mkdir(mode=0o700)
            (f.state/'keep-private.log').write_text('private runtime detail')
            actual_publish = DirectoryPublisher.publish
            selections = []
            def publish(publisher, menu, cache, **kwargs):
                self.assertFalse(f.active, 'Runtime must close before actual publisher entry')
                self.assertEqual(f.closed, 1)
                self.assertEqual(load_state(f.queue)['pending']['phase'], 'collect')
                self.assertEqual(json.loads((f.state/'translation-checkpoint.json').read_text()), cache)
                self.assertEqual(kwargs['today'], '2026-09-21')
                selections.append(actual_publish(publisher, menu, cache, **kwargs))
                return selections[-1]
            # 이 테스트는 기본 번역 함수와 모델 실행 함수를 작업 호출 시에 선택하는지 확인합니다. f.job은 구성할 때 모델을 호출하지 않으므로 이후에 대체한 함수를 사용해야 합니다.
            job = f.job(translate=None, runtime=None)
            with (patch.object(adapter, 'request_translation', side_effect=f.generate),
                  patch.object(adapter, 'model_session', side_effect=f.runtime),
                  patch.object(adapter, 'load_glossary', wraps=adapter.load_glossary) as glossary,
                  patch.object(DirectoryPublisher, 'publish', publish)):
                result = JobRunner(f.state, resources=lambda: (True, 'fixture ready'), work=job).run(f.now)
                self.assertEqual(glossary.call_count, 1)
            self.assertEqual((result['status'], result['period']), ('completed', '2026-09-21T11:00'))
            menu, cache = f.publisher.load_current()
            self.assertEqual(menu, f.menu)
            for record in menu['days'][0]['meals']:
                entry = cache['entries'][record['translation_key']]
                self.assertEqual(entry['en']['name'], 'Generated '+record['name_de'])
                self.assertEqual(entry['ko']['name'], '생성된 '+record['name_de'])
            self.assertEqual(cache['notices'], f.glossary)
            self.assertEqual((f.source_calls, f.clock_calls, f.opened, f.closed), (1, 1, 1, 1))
            self.assertEqual({path: path.read_bytes() for path in f.checkout.rglob('*') if path.is_file()}, before)
            self.assertEqual({path.name for path in f.public.rglob('*') if path.is_file()}, {'menu.json', 'translations.json', 'current.json'})
            self.assertNotIn('fixture-secret', ''.join(path.read_text() for path in f.public.rglob('*') if path.is_file()))
            self.assertEqual(JobRunner(f.state, resources=lambda: self.fail('Idle resources'), work=job).run(f.now)['status'], 'idle')
            self.assertEqual(len(selections), 1)

    def test_partial_history_retry_uses_latest_published_review_and_only_generates_missing_b(self):
        with self.fixture() as f:
            old_menu = f.menu_for(('Old dish',))
            old_id = f.publisher.publish(old_menu, f.cache(old_menu), today='2026-09-21')
            f.fail_name = 'Suppe B'
            self.assertEqual(f.run()['status'], 'failed')
            private = json.loads((f.state/'translation-checkpoint.json').read_text())
            records = f.menu['days'][0]['meals']; A, B = (record['translation_key'] for record in records)
            self.assertIn(A, private['entries']); self.assertNotIn(B, private['entries'])
            self.assertEqual(f.publisher.load_current()[0], old_menu)
            reviewed_menu = f.menu_for(('Suppe A',)); reviewed = f.cache(reviewed_menu)
            reviewed['entries'][A]['en']['name'] = 'Newest reviewed A'
            reviewed['entries'][A]['ko']['name'] = '최신 검토 A'
            reviewed_id = f.publisher.publish(reviewed_menu, reviewed, today='2026-09-21')
            f.now = datetime.fromisoformat(load_state(f.queue)['pending']['next_attempt_at']) + timedelta(seconds=1)
            f.fail_name = None
            self.assertEqual(f.run()['status'], 'completed')
            _, final = f.publisher.load_current()
            self.assertEqual(final['entries'][A], reviewed['entries'][A])
            self.assertEqual(final['entries'][B]['en']['name'], 'Generated Suppe B')
            self.assertEqual(final['entries'][B]['ko']['name'], '생성된 Suppe B')
            self.assertEqual([source['name_de'] for source, _ in f.requests], ['Suppe A', 'Suppe B', 'Suppe B'])
            for release_id in (old_id, reviewed_id):
                self.assertTrue((f.public/'data/releases'/release_id/'menu.json').is_file())
            self.assertEqual((f.opened, f.closed, f.active), (2, 2, False))

    def test_expired_source_generation_and_selection_failures_retain_previous_release(self):
        for boundary in ('expired', 'generation', 'selection'):
            with self.subTest(boundary=boundary), self.fixture() as f:
                old = f.menu_for(('Old dish',)); f.publisher.publish(old, f.cache(old), today='2026-09-21')
                pointer = f.public/'data/current.json'; before = pointer.read_bytes()
                if boundary == 'expired': f.now = NOW.replace(hour=22, minute=30)
                if boundary == 'generation': f.fail_name = 'Suppe B'
                if boundary == 'selection':
                    with patch.object(DirectoryPublisher, '_select', side_effect=OSError('Fixture selection interrupted')):
                        result = f.run()
                else: result = f.run()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(pointer.read_bytes(), before)
                self.assertEqual(f.publisher.load_current()[0], old)
                pending = load_state(f.queue)['pending']
                self.assertEqual((pending['phase'], pending['commit_sha'], pending['run_id'], pending['dispatch_requested_at']), ('collect', None, None, None))
                self.assertFalse(f.active)
                if boundary == 'expired':
                    self.assertIn('past menus', result['reason'])
                    self.assertEqual((f.opened, f.requests), (0, []))
                    self.assertFalse((f.state/'translation-checkpoint.json').exists())
                else:
                    self.assertTrue((f.state/'translation-checkpoint.json').is_file())
                    self.assertEqual((f.opened, f.closed), (1, 1))

    def test_completion_write_gap_retries_same_release_from_private_history(self):
        import mensa.jobs as jobs
        with self.fixture() as f:
            old = f.menu_for(('Old dish',)); old_id = f.publisher.publish(old, f.cache(old), today='2026-09-21')
            actual_save = jobs.save_state
            def interrupted_completion(path, state):
                if state['pending'] is None: raise OSError('Fixture completion write interrupted')
                return actual_save(path, state)
            with patch.object(jobs, 'save_state', side_effect=interrupted_completion):
                with self.assertRaisesRegex(OSError, 'completion write interrupted'): f.run()
            pointer = f.public/'data/current.json'; selected = json.loads(pointer.read_text())['release_id']
            inode, mtime = pointer.stat().st_ino, pointer.stat().st_mtime_ns
            self.assertEqual(load_state(f.queue)['pending']['phase'], 'collect')
            self.assertEqual(f.publisher.load_current()[0], f.menu)
            self.assertEqual(len(f.requests), 2)
            self.assertEqual(f.run()['status'], 'completed')
            self.assertEqual(json.loads(pointer.read_text())['release_id'], selected)
            self.assertEqual((pointer.stat().st_ino, pointer.stat().st_mtime_ns), (inode, mtime))
            self.assertEqual((f.source_calls, len(f.requests), f.opened, f.closed), (2, 2, 1, 1))
            self.assertIsNone(load_state(f.queue)['pending'])
            self.assertTrue((f.public/'data/releases'/old_id/'translations.json').is_file())

    def test_old_collect_coalesces_to_latest_due_and_git_pending_is_rejected(self):
        for git_pending in (False, True):
            with self.subTest(git_pending=git_pending), self.fixture() as f:
                f.state.mkdir(mode=0o700)
                state = self.pending()
                if git_pending: state['pending'].update(phase='publish', commit_sha='a'*40, run_id=123, dispatch_requested_at=NOW.isoformat())
                save_state(f.queue, state)
                original = state['pending'].copy()
                f.now = datetime(2026, 9, 28, 10, tzinfo=timezone.utc)
                f.menu = f.menu_for(day='28.09.2026')
                result = f.run()
                if git_pending:
                    self.assertEqual(result['status'], 'failed'); self.assertIn('collect', result['reason'])
                    pending = load_state(f.queue)['pending']
                    for name in ('period', 'phase', 'commit_sha', 'run_id', 'dispatch_requested_at'):
                        self.assertEqual(pending[name], original[name])
                    self.assertEqual((f.source_calls, f.clock_calls, f.opened), (0, 0, 0))
                    self.assertFalse(f.public.exists())
                else:
                    self.assertEqual((result['status'], result['period']), ('completed', '2026-09-28T11:00'))
                    self.assertEqual(load_state(f.queue)['completed_period'], '2026-09-28T11:00')
                    self.assertEqual(f.publisher.load_current()[0], f.menu)
