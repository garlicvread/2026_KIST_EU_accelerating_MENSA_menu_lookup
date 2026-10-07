"""이 테스트 모듈은 설정한 모델 실행 코드가 자신이 시작한 프로세스와 파일을 관리하는지 확인합니다. 실제 배포 환경에 영향을 주지 않도록 삭제 가능한 임시 파일과 로컬 테스트 대체물만 사용합니다."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mensa.config import InferenceSettings
from mensa.errors import GenerationError

from mensa import model_runtime as runtime


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
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
        return runtime.model_session(settings if settings is not None else self.settings, **ports)

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


    def test_managed_session_sets_private_local_environment(self):
        with self.session() as result:
            self.assertEqual(result['url'], 'http://127.0.0.1:43210/api/chat')
            env = self.launch.call_args.kwargs['env']
            self.assertEqual({name: env[name] for name in (
                'OLLAMA_HOST', 'OLLAMA_MODELS', 'OLLAMA_NO_CLOUD',
                'OLLAMA_NUM_PARALLEL', 'OLLAMA_MAX_LOADED_MODELS')},
                {'OLLAMA_HOST': '127.0.0.1:43210', 'OLLAMA_MODELS': str(self.models),
                 'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NUM_PARALLEL': '1',
                 'OLLAMA_MAX_LOADED_MODELS': '1'})


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


    def test_wrong_ready_model_revision_is_refused_and_cleaned_up(self):
        self.probe.return_value = {'models': [{'name': 'fixture:1', 'digest': 'wrong'}]}
        self.assert_failure('invalid_config')
        self.stop.assert_called_once_with(self.process)


    def test_launch_exception_is_safe_and_closes_log_without_stopping_unowned_process(self):
        self.launch.side_effect = OSError('SECRET launch')
        error = self.assert_failure('network')
        self.assertTrue(error.__suppress_context__)
        self.stop.assert_not_called()
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)


    def test_transient_probe_failure_retries_until_ready(self):
        self.probe.side_effect = [OSError('SECRET unavailable'),
                                 {'models': [{'name': 'fixture:1', 'digest': 'sha256:fixture'}]}]
        with self.session():
            pass
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(len(self.waits), 1)
        self.stop.assert_called_once_with(self.process)

    def test_startup_timeout_bounds_waiting_and_cleans_up(self):
        self.probe.side_effect = GenerationError('network')
        self.assert_failure('timeout', startup_timeout=.6, poll_interval=.4)
        self.assertAlmostEqual(sum(self.waits), .6)
        self.stop.assert_called_once_with(self.process)
        self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)


    def test_body_errors_and_cancellation_propagate_after_cleanup(self):
        for error in (RuntimeError('body'), KeyboardInterrupt('cancel')):
            with self.subTest(error=error):
                with self.assertRaises(type(error)) as raised:
                    with self.session():
                        raise error
                self.assertIs(raised.exception, error)
                self.assertTrue(self.launch.call_args.kwargs['stdout'].closed)
        self.assertEqual(self.stop.call_count, 2)


    @contextmanager
    def http_fixture(self, body):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
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
        probe = runtime.probe_models
        payload = {'models': [{'name': 'fixture:1', 'digest': 'sha256:fixture'}]}
        with self.http_fixture(json.dumps(payload).encode()) as (host, requests):
            with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
                self.assertEqual(probe(host, .5), payload)
            self.assertEqual(requests, ['/api/tags'])

    def test_local_tag_probe_refuses_malformed_json_with_safe_message(self):
        with self.http_fixture(b'SECRET') as (host, requests):
            with self.assertRaises(GenerationError) as raised:
                runtime.probe_models(host, .5)
        self.assertEqual(raised.exception.code, 'invalid_json')
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn('SECRET', str(raised.exception))
        self.assertEqual(requests, ['/api/tags'])


    def test_real_disposable_group_cleanup_includes_orphan_and_leaves_other_group(self):
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
            with runtime.model_session(self.settings, probe=self.probe, choose_port=self.port,
                            stop=stop, startup_timeout=2) as result:
                self.assertEqual(result['backend_revision'], self.settings.revision)
        finally:
            for process in processes:
                if process.poll() is None:
                    runtime.stop_model(process)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)


if __name__ == '__main__':
    unittest.main()
