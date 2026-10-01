"""이 테스트 모듈은 설정한 모델 실행 코드가 자신이 시작한 프로세스와 파일을 관리하는지 확인합니다. 실제 배포 환경에 영향을 주지 않도록 삭제 가능한 임시 파일과 로컬 테스트 대체물만 사용합니다."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mensa.config import InferenceSettings, load_worker_config
from mensa.errors import GenerationError

try:
    runtime = importlib.import_module('mensa.model_runtime')
except ModuleNotFoundError as error:
    if error.name != 'mensa.model_runtime':
        raise
    runtime = None


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(runtime, 'model_session', None)),
                        'model_session API must exist before exercising runtime behavior')
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.binary = self.base / 'fake-ollama'
        self.binary.touch()
        self.models = self.base / 'models'
        self.models.mkdir()
        self.logs = self.base / 'logs'
        self.settings = InferenceSettings('managed', 'ollama', 'fixture:1', 'sha256:fixture',
                                          None, self.binary, self.models, self.logs)
        self.process = Mock(pid=123456)
        self.process.poll.return_value = None
        self.launch = Mock(return_value=self.process)
        self.port = Mock(return_value=43210)
        self.probe = Mock(return_value={'models': [{'name': 'fixture:1', 'digest': 'sha256:fixture'}]})
        self.now = 0.0
        self.waits = []
        self.stop = Mock()

    def api(self, name='model_session'):
        function = getattr(runtime, name, None)
        self.assertTrue(callable(function), f'{name} API must exist')
        return function

    def clock(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def session(self, settings=None, **overrides):
        ports = dict(launch=self.launch, choose_port=self.port, probe=self.probe,
                     clock=self.clock, wait=self.wait, stop=self.stop,
                     startup_timeout=1, poll_interval=.25)
        ports.update(overrides)
        return self.api()(settings if settings is not None else self.settings, **ports)

    def assert_failure(self, code, settings=None, **ports):
        with self.assertRaises(GenerationError) as raised:
            with self.session(settings, **ports):
                self.fail('Invalid runtime must not yield')
        error = raised.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.retryable, code in ('network', 'timeout'))
        self.assertIsNone(error.__cause__)
        self.assertNotIn('SECRET', str(error))
        return error

    def test_external_has_no_lifecycle_calls_files_or_shared_result(self):
        external = replace(self.settings, mode='external', provider='openai',
                           url='https://fixture.invalid/v1/chat/completions', binary=None,
                           models_dir=None, log_dir=None)
        forbidden = Mock(side_effect=AssertionError('External ownership was touched'))
        ports = {name: forbidden for name in ('launch', 'choose_port', 'probe', 'clock', 'wait', 'stop')}
        with self.api()(external, key=' SECRET ', **ports) as first:
            self.assertEqual(first, {'provider': 'openai', 'model': 'fixture:1',
                                    'url': external.url, 'key': ' SECRET ',
                                    'backend_revision': 'sha256:fixture'})
            first['model'] = 'mutated'
        with self.api()(external, key=' SECRET ', **ports) as second:
            self.assertEqual(second['model'], 'fixture:1')
            self.assertIsNot(first, second)
        forbidden.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_external_body_cancellation_propagates_without_cleanup(self):
        external = replace(self.settings, mode='external', url='http://fixture.invalid/api/chat',
                           binary=None, models_dir=None, log_dir=None)
        error = KeyboardInterrupt('cancel')
        with self.assertRaises(KeyboardInterrupt) as raised:
            with self.session(external):
                raise error
        self.assertIs(raised.exception, error)
        self.stop.assert_not_called()
        self.launch.assert_not_called()
        self.probe.assert_not_called()

    def test_managed_launch_uses_owned_group_environment_and_stable_identity(self):
        with patch.dict(os.environ, {'CARD20_INHERITED': 'fixture'}):
            with self.session(key=' exact key ') as result:
                self.assertEqual(result, {'provider': 'ollama', 'model': 'fixture:1',
                                         'url': 'http://127.0.0.1:43210/api/chat',
                                         'key': ' exact key ', 'backend_revision': 'sha256:fixture'})
                self.stop.assert_not_called()
                args, kwargs = self.launch.call_args
                self.assertEqual(args, ([str(self.binary), 'serve'],))
                self.assertIs(kwargs['start_new_session'], True)
                self.assertIs(kwargs['stdout'], kwargs['stderr'])
                self.assertFalse(kwargs['stdout'].closed)
                env = kwargs['env']
                self.assertEqual(env['CARD20_INHERITED'], 'fixture')
                self.assertEqual({name: env[name] for name in (
                    'OLLAMA_HOST', 'OLLAMA_MODELS', 'OLLAMA_NO_CLOUD',
                    'OLLAMA_NUM_PARALLEL', 'OLLAMA_MAX_LOADED_MODELS')},
                    {'OLLAMA_HOST': '127.0.0.1:43210', 'OLLAMA_MODELS': str(self.models),
                     'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NUM_PARALLEL': '1',
                     'OLLAMA_MAX_LOADED_MODELS': '1'})
        self.stop.assert_called_once_with(self.process)
        self.assertTrue(kwargs['stdout'].closed)
        self.assertEqual(self.probe.call_args.args, ('http://127.0.0.1:43210', 1))
        self.port.return_value = 43211
        with self.session() as other:
            self.assertEqual(other['backend_revision'], result['backend_revision'])
            self.assertNotEqual(other['url'], result['url'])

    def test_runtime_accepts_settings_from_reviewed_loader(self):
        config = self.base / 'worker.toml'
        config.write_text('''[paths]
checkout_dir = "checkout"
state_dir = "state"
public_dir = "public"
[inference]
mode = "managed"
provider = "ollama"
model = "fixture:1"
revision = "sha256:fixture"
binary = "fake-ollama"
models_dir = "models"
log_dir = "logs"
[resources]
platform = "macos"
min_available_bytes = 1
max_load_per_cpu = 1
[publication]
mode = "directory"
''')
        loaded = load_worker_config(config).inference
        with self.session(loaded) as result:
            self.assertEqual(result['backend_revision'], loaded.revision)

    def test_log_leaf_and_file_are_private_and_latest_run_is_truncated(self):
        with self.session():
            stream = self.launch.call_args.kwargs['stdout']
            stream.write('fixture output')
            stream.flush()
            self.assertGreater((self.logs / 'model.log').stat().st_size, 0)
        self.assertEqual(self.logs.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.logs / 'model.log').stat().st_mode & 0o777, 0o600)
        with self.session():
            self.assertEqual((self.logs / 'model.log').stat().st_size, 0)
        self.assertEqual(list(self.logs.iterdir()), [self.logs / 'model.log'])

    def test_invalid_settings_fail_before_lifecycle_or_files(self):
        bad = [object(), replace(self.settings, mode='other'),
               replace(self.settings, provider='openai'), replace(self.settings, model=' '),
               replace(self.settings, revision=''), replace(self.settings, binary=str(self.binary)),
               replace(self.settings, models_dir=None), replace(self.settings, log_dir=None),
               replace(self.settings, url='http://fixture.invalid'),
               replace(self.settings, mode='external', binary=None, models_dir=None, log_dir=None, url=None)]
        for settings in bad:
            with self.subTest(settings=settings):
                self.assert_failure('invalid_config', settings)
        self.port.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_invalid_timing_key_and_ports_fail_before_effects(self):
        for name in ('startup_timeout', 'poll_interval'):
            for value in (0, -1, True, float('nan'), float('inf'), 10 ** 1000, '1', None):
                with self.subTest(name=name, value=value):
                    self.assert_failure('invalid_config', **{name: value})
        self.assert_failure('invalid_config', key=None)
        for name in ('launch', 'choose_port', 'probe', 'clock', 'wait', 'stop'):
            with self.subTest(name=name):
                self.assert_failure('invalid_config', **{name: None})
        self.port.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_invalid_chosen_port_has_no_log_or_launch_effect(self):
        for value in (True, 0, -1, 65536, 1.0, '1', None):
            with self.subTest(value=value):
                self.port.return_value = value
                self.assert_failure('invalid_config')
        self.port.side_effect = OSError('SECRET port')
        self.assert_failure('invalid_config')
        self.launch.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_missing_wrong_kind_paths_and_log_parent_are_rejected(self):
        for settings in (replace(self.settings, binary=self.base / 'missing'),
                         replace(self.settings, binary=self.models),
                         replace(self.settings, models_dir=self.base / 'missing'),
                         replace(self.settings, models_dir=self.binary),
                         replace(self.settings, log_dir=self.base / 'missing' / 'logs')):
            with self.subTest(settings=settings):
                self.assert_failure('invalid_config', settings)
        self.launch.assert_not_called()
        self.assertFalse(self.logs.exists())

    def test_log_symlinks_wrong_kind_or_accessible_permissions_rejected(self):
        for kind in ('symlink', 'file', 'group', 'world'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.logs.symlink_to(self.models, target_is_directory=True)
                elif kind == 'file':
                    self.logs.touch()
                else:
                    self.logs.mkdir(mode=0o700)
                    self.logs.chmod(0o750 if kind == 'group' else 0o701)
                self.assert_failure('invalid_config')
                if kind in ('symlink', 'file'):
                    self.logs.unlink()
                else:
                    self.logs.rmdir()
        self.launch.assert_not_called()

    def test_logfile_symlink_nonregular_and_accessible_permissions_rejected_without_truncate(self):
        self.logs.mkdir(mode=0o700)
        log = self.logs / 'model.log'
        for kind in ('symlink', 'directory', 'fifo', 'group', 'world'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    log.symlink_to(self.binary)
                elif kind == 'directory':
                    log.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(log, mode=0o600)
                else:
                    log.write_text('do not truncate')
                    log.chmod(0o640 if kind == 'group' else 0o601)
                self.assert_failure('invalid_config')
                if kind in ('group', 'world'):
                    self.assertEqual(log.stat().st_size, len('do not truncate'))
                if kind == 'directory':
                    log.rmdir()
                else:
                    log.unlink()
        self.launch.assert_not_called()

    def test_ready_tags_must_match_model_and_nonblank_exact_revision(self):
        for models in ([], [{'name': 'other', 'digest': 'sha256:fixture'}],
                       [{'name': 'fixture:1', 'digest': 'wrong'}],
                       [{'name': 'fixture:1', 'digest': ' '}], [{'name': 'fixture:1'}]):
            with self.subTest(models=models):
                self.probe.return_value = {'models': models}
                self.assert_failure('invalid_config')
        self.assertEqual(self.stop.call_count, 5)

    def test_malformed_envelopes_are_permanent_and_cleaned_up(self):
        for value in (None, [], {}, {'models': {}}, {'models': ['text']}, {'models': [None]}):
            with self.subTest(value=value):
                self.probe.return_value = value
                self.assert_failure('invalid_envelope')
        self.assertEqual(self.stop.call_count, 6)

    def test_launch_exception_is_safe_and_closes_log_without_stopping_unowned_process(self):
        self.launch.side_effect = OSError('SECRET launch')
        error = self.assert_failure('network')
        self.assertTrue(error.__suppress_context__)
        self.stop.assert_not_called()
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)

    def test_early_exit_is_retryable_and_stops_owned_group(self):
        self.process.poll.return_value = 1
        self.assert_failure('network')
        self.probe.assert_not_called()
        self.stop.assert_called_once_with(self.process)

    def test_unavailable_probe_retries_until_ready_with_remaining_budget(self):
        self.probe.side_effect = [OSError('SECRET unavailable'), TimeoutError('SECRET timeout'),
                                 {'models': [{'name': 'fixture:1', 'digest': 'sha256:fixture'}]}]
        with self.session():
            pass
        self.assertEqual(self.waits, [.25, .25])
        self.assertEqual([call.args[1] for call in self.probe.call_args_list], [1, .75, .5])
        self.stop.assert_called_once_with(self.process)

    def test_timeout_bounds_probe_and_wait_and_cleans_up(self):
        self.probe.side_effect = GenerationError('network')
        self.assert_failure('timeout', startup_timeout=.6, poll_interval=.4)
        self.assertAlmostEqual(sum(self.waits), .6)
        self.assertEqual(len(self.waits), 2)
        self.assertAlmostEqual(self.waits[-1], .2)
        self.assertEqual(len(self.probe.call_args_list), 2)
        self.assertAlmostEqual(self.probe.call_args_list[-1].args[1], .2)
        self.stop.assert_called_once_with(self.process)
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)

    def test_probe_spending_budget_does_not_yield_or_sleep_past_deadline(self):
        def probe(host, timeout):
            self.now += timeout
            return {'models': [{'name': 'fixture:1', 'digest': 'sha256:fixture'}]}
        self.assert_failure('timeout', probe=probe)
        self.assertEqual(self.waits, [])
        self.stop.assert_called_once_with(self.process)

    def test_probe_errors_are_safe_and_permanent_errors_do_not_retry(self):
        for code in ('invalid_json', 'invalid_envelope', 'response_too_large', 'redirect'):
            with self.subTest(code=code):
                self.probe.side_effect = GenerationError(code)
                self.assert_failure(code)
        self.probe.side_effect = json.JSONDecodeError('SECRET', 'SECRET', 0)
        self.assert_failure('invalid_json')
        self.probe.side_effect = ValueError('SECRET envelope')
        self.assert_failure('invalid_envelope')
        self.assertEqual(self.waits, [])
        self.assertEqual(self.stop.call_count, 6)

    def test_body_errors_and_cancellation_propagate_after_cleanup(self):
        for error in (RuntimeError('body'), GenerationError('invalid_result'),
                      KeyboardInterrupt('cancel'), SystemExit(143)):
            with self.subTest(error=error):
                with self.assertRaises(type(error)) as raised:
                    with self.session():
                        raise error
                self.assertIs(raised.exception, error)
                self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)
        self.assertEqual(self.stop.call_count, 4)

    def test_readiness_and_wait_cancellation_unwind_owned_runtime(self):
        for name in ('probe', 'wait'):
            with self.subTest(name=name):
                self.probe.side_effect = OSError('unavailable') if name == 'wait' else KeyboardInterrupt()
                with self.assertRaises(KeyboardInterrupt):
                    with self.session(**({name: Mock(side_effect=KeyboardInterrupt())} if name == 'wait' else {})):
                        self.fail('cancelled startup yielded')
                self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)
        self.assertEqual(self.stop.call_count, 2)

    def test_body_failure_survives_simultaneous_stop_failure_without_secondary_diagnostics(self):
        primary = RuntimeError('body failure')
        secondary = RuntimeError('SECRET cleanup backend key')
        self.stop.side_effect = secondary
        with self.assertRaises(BaseException) as raised:
            with self.session():
                raise primary
        self.assertIs(raised.exception, primary)
        self.assertEqual(primary.args, ('body failure',))
        self.assertIsNone(primary.__context__)
        self.assertIsNone(primary.__cause__)
        self.assertNotIn('SECRET', ''.join(getattr(primary, '__notes__', [])))
        self.stop.assert_called_once_with(self.process)
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)

    def test_body_cancellation_survives_simultaneous_stop_failure(self):
        for primary in (KeyboardInterrupt('cancel'), SystemExit(143)):
            with self.subTest(primary=type(primary).__name__):
                self.stop.reset_mock()
                self.stop.side_effect = RuntimeError('SECRET cleanup backend key')
                original_args = primary.args
                with self.assertRaises(BaseException) as raised:
                    with self.session():
                        raise primary
                self.assertIs(raised.exception, primary)
                self.assertEqual(primary.args, original_args)
                self.assertIsNone(primary.__context__)
                self.assertIsNone(primary.__cause__)
                self.assertNotIn('SECRET', ''.join(getattr(primary, '__notes__', [])))
                self.stop.assert_called_once_with(self.process)
                self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)

    def test_readiness_failure_survives_simultaneous_stop_failure_without_remote_diagnostics(self):
        self.probe.return_value = {'models': 'malformed'}
        self.stop.side_effect = RuntimeError('SECRET cleanup backend key')
        original_readiness = runtime._readiness
        primary = []
        def capture_primary(*args):
            try:
                original_readiness(*args)
            except GenerationError as error:
                primary.append(error)
                raise
        with patch.object(runtime, '_readiness', side_effect=capture_primary):
            with self.assertRaises(BaseException) as raised:
                with self.session():
                    self.fail('Malformed readiness must not yield')
        self.assertEqual(len(primary), 1)
        self.assertIs(raised.exception, primary[0])
        self.assertEqual(primary[0].code, 'invalid_envelope')
        self.assertFalse(primary[0].retryable)
        self.assertIsNone(primary[0].__context__)
        self.assertIsNone(primary[0].__cause__)
        import traceback
        self.assertNotIn('SECRET', ''.join(traceback.format_exception(primary[0])))
        self.stop.assert_called_once_with(self.process)
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)

    def test_stop_failure_still_closes_log(self):
        error = RuntimeError('cleanup')
        self.stop.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            with self.session():
                pass
        self.assertIs(raised.exception, error)
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)

    def test_default_port_helper_returns_exact_available_loopback_port(self):
        port = self.api('choose_loopback_port')()
        self.assertIs(type(port), int)
        self.assertGreaterEqual(port, 1)
        self.assertLessEqual(port, 65535)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))

    @contextmanager
    def http_fixture(self, body, *, status=200):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(status)
                if status == 302:
                    self.send_header('Location', '/must-not-follow')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}', requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_default_probe_reads_local_tags_without_proxy(self):
        probe = self.api('probe_models')
        payload = {'models': [{'name': 'fixture:1', 'digest': 'sha256:fixture'}]}
        with self.http_fixture(json.dumps(payload).encode()) as (host, requests):
            with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
                self.assertEqual(probe(host, .5), payload)
            self.assertEqual(requests, ['/api/tags'])

    def test_default_probe_rejects_redirect_json_envelope_and_oversized_body(self):
        probe = self.api('probe_models')
        for body, status, code in ((b'{}', 302, 'redirect'), (b'SECRET', 200, 'invalid_json'),
                                   (b'{"models":{}}', 200, 'invalid_envelope'),
                                   (b'x' * (1024 * 1024 + 1), 200, 'response_too_large')):
            with self.subTest(code=code), self.http_fixture(body, status=status) as (host, requests):
                with self.assertRaises(GenerationError) as raised:
                    probe(host, .5)
                self.assertEqual(raised.exception.code, code)
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn('SECRET', str(raised.exception))
                self.assertEqual(requests, ['/api/tags'])

    def test_default_probe_passes_timeout_and_maps_local_unavailable_network(self):
        probe = self.api('probe_models')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            host = f'http://127.0.0.1:{sock.getsockname()[1]}'
            with self.assertRaises(GenerationError) as raised:
                probe(host, .05)
        self.assertEqual(raised.exception.code, 'network')
        opener = Mock()
        opener.open.side_effect = TimeoutError('SECRET')
        with patch.object(runtime.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(GenerationError) as raised:
                probe('http://127.0.0.1:12345', .125)
        self.assertEqual(raised.exception.code, 'timeout')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], .125)

    def test_real_disposable_group_cleanup_includes_orphan_and_leaves_other_group(self):
        self.api('stop_model')
        marker = self.base / 'child.pid'
        child_code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
        code = ('import subprocess,sys,pathlib,time\n'
                f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}])\n'
                f'pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n'
                'time.sleep(.1)\n')
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        processes = []
        def launch(args, **kwargs):
            process = subprocess.Popen([sys.executable, '-c', code], **kwargs)
            processes.append(process)
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), 'Disposable child must start')
            process.wait(timeout=3)
            return process
        try:
            self.assert_failure('network', launch=launch, stop=runtime.stop_model)
            self.assertIsNone(other.poll(), 'Unrelated process group must survive')
            child = int(marker.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True)
                if status.returncode or status.stdout.strip().startswith('Z'):
                    break
                time.sleep(.02)
            self.assertTrue(status.returncode or status.stdout.strip().startswith('Z'),
                            'Owned orphan must be dead (a zombie is not running)')
            self.assertIsNotNone(processes[0].returncode)
        finally:
            os.killpg(other.pid, signal.SIGKILL)
            other.wait(timeout=3)
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)

    def test_default_launch_executes_only_configured_disposable_binary_and_closes_log(self):
        self.binary.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n')
        self.binary.chmod(0o700)
        processes = []
        def stop(process):
            processes.append(process)
            runtime.stop_model(process)
        try:
            with self.api()(self.settings, probe=self.probe, choose_port=self.port,
                            stop=stop, startup_timeout=2) as result:
                self.assertEqual(result['backend_revision'], self.settings.revision)
        finally:
            for process in processes:
                if process.poll() is None:
                    runtime.stop_model(process)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(self.probe.call_args.args[0], 'http://127.0.0.1:43210')


if __name__ == '__main__':
    unittest.main()
