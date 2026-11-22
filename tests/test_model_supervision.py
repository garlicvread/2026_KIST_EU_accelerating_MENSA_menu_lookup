"""실제 준비 상태와 소유 프로세스 활동에 따라 모델을 기다리고 복구하는지 검사합니다."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mensa.config import InferenceSettings
from mensa.errors import GenerationError
from mensa import model_runtime as runtime


class ModelSupervisionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        binary = self.root / 'ollama'
        binary.touch()
        models = self.root / 'models'
        models.mkdir()
        self.settings = InferenceSettings('managed', 'ollama', 'fixture:1', 'revision',
                                         None, binary, models, self.root / 'logs')
        self.process = Mock(pid=123456)
        self.process.poll.return_value = None
        self.launch = Mock(return_value=self.process)
        self.stop = Mock()
        self.now = 0
        self.ready = {'models': [{'name': 'fixture:1', 'digest': 'revision'}]}

    def wait(self, seconds):
        self.now += seconds

    def session(self, **options):
        arguments = dict(launch=self.launch, choose_port=lambda: 43210,
                         clock=lambda: self.now, wait=self.wait, stop=self.stop)
        arguments.update(options)
        return runtime.model_session(self.settings, **arguments)

    def test_active_initialization_can_finish_after_old_thirty_second_deadline(self):
        def probe(host, timeout):
            self.now += 1
            if self.now < 45:
                raise GenerationError('network')
            return self.ready
        with patch.object(runtime, 'process_activity', create=True,
                          side_effect=lambda process: ((process.pid, self.now),)):
            with self.session(probe=probe):
                self.assertGreaterEqual(self.now, 45)
        self.launch.assert_called_once()
        self.stop.assert_called_once_with(self.process)

    def test_child_activity_extends_idle_window_without_extending_each_http_probe(self):
        timeouts = []
        def probe(host, timeout):
            timeouts.append(timeout)
            self.now += timeout
            if self.now < 12:
                raise GenerationError('timeout')
            return self.ready
        with self.session(probe=probe, startup_idle_timeout=3, probe_timeout=1,
                          activity=lambda process: ((process.pid, 0), (process.pid+1, self.now))):
            pass
        self.assertGreater(self.now, 3)
        self.assertLessEqual(max(timeouts), 1)
        self.launch.assert_called_once()

    def test_inactive_process_is_cleaned_before_restart_and_then_recovers(self):
        second = Mock(pid=123457)
        second.poll.return_value = None
        self.launch.side_effect = [self.process, second]
        events = []
        def stop(process):
            events.append(('stop', process.pid))
        def probe(host, timeout):
            if self.launch.call_count == 1:
                self.now += timeout
                raise GenerationError('timeout')
            self.assertEqual(events, [('stop', self.process.pid)])
            return self.ready
        with self.session(probe=probe, startup_idle_timeout=3, probe_timeout=1,
                          activity=lambda process: ((process.pid, 0),), stop=stop):
            pass
        self.assertEqual(events, [('stop', self.process.pid), ('stop', second.pid)])
        self.assertIn('model_startup_stalled', (self.settings.log_dir/'model.log').read_text())

    def test_exited_process_is_restarted_without_waiting_for_idle_deadline(self):
        first = self.process
        first.poll.return_value = 17
        second = Mock(pid=123457)
        second.poll.return_value = None
        self.launch.side_effect = [first, second]
        probe = Mock(return_value=self.ready)
        with self.session(probe=probe, activity=lambda process: ()):
            pass
        self.assertEqual(self.now, 0)
        self.assertEqual(self.stop.call_args_list[0].args, (first,))
        probe.assert_called_once()

    def test_persistent_stall_reports_startup_failure_after_bounded_recovery(self):
        def probe(host, timeout):
            self.now += timeout
            raise GenerationError('timeout')
        with self.assertRaises(GenerationError) as error:
            with self.session(probe=probe, startup_idle_timeout=2, probe_timeout=1,
                              activity=lambda process: ((process.pid, 0),)):
                self.fail('Stalled server cannot be ready')
        self.assertEqual(error.exception.code, 'model_startup_stalled')
        self.assertTrue(error.exception.retryable)
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(self.stop.call_count, 2)

    def test_ready_model_configuration_error_does_not_restart(self):
        with self.assertRaises(GenerationError) as error:
            with self.session(probe=lambda *_: {'models': []}, activity=lambda process: ()):
                self.fail('Wrong model must not be used')
        self.assertEqual(error.exception.code, 'invalid_config')
        self.launch.assert_called_once()
        self.stop.assert_called_once()

    def test_ready_api_is_usable_even_when_optional_activity_snapshot_fails(self):
        from mensa.process_health import ProcessObservationError
        with self.session(probe=Mock(return_value=self.ready),
                          activity=Mock(side_effect=ProcessObservationError())):
            pass
        self.launch.assert_called_once()

    def test_transient_activity_observation_error_does_not_kill_initializing_process(self):
        from mensa.process_health import ProcessObservationError
        probe = Mock(side_effect=[GenerationError('network'), GenerationError('network'), self.ready])
        observer = Mock(side_effect=[ProcessObservationError(), ((self.process.pid, 1),)])
        with self.session(probe=probe, startup_idle_timeout=3, activity=observer):
            pass
        self.launch.assert_called_once()

    def test_log_activity_supports_initialization_when_cpu_is_waiting(self):
        def probe(host, timeout):
            self.now += .5
            stream = self.launch.call_args.kwargs['stdout']
            stream.write('loaded another initialization stage\n')
            stream.flush()
            if self.now < 5:
                raise GenerationError('network')
            return self.ready
        with self.session(probe=probe, startup_idle_timeout=1,
                          activity=lambda process: ((process.pid, 0),)):
            pass
        self.assertGreaterEqual(self.now, 5)
        self.launch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
