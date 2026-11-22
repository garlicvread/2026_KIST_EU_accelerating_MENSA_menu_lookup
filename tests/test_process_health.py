"""이 테스트 모듈은 소유 프로세스 그룹의 CPU 활동과 관찰 실패를 확인합니다."""

from decimal import Decimal
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from mensa.process_health import ProcessObservationError, process_activity


class ProcessActivityTests(unittest.TestCase):
    def setUp(self):
        self.process = Mock(pid=410)

    def observe(self, output):
        result = subprocess.CompletedProcess(['ps'], 0, output, '')
        with patch('mensa.process_health.subprocess.run', return_value=result):
            return process_activity(self.process)

    def test_child_cpu_progress_changes_signature_while_leader_is_idle(self):
        before = self.observe('410 410 Ss 00:00.10\n411 410 R 00:01.25\n')
        after = self.observe('410 410 Ss 00:00.10\n411 410 R 00:02.25\n')
        self.assertNotEqual(before, after)
        self.assertEqual(before, ((410, Decimal('.10')), (411, Decimal('1.25'))))
        hash(before)

    def test_unrelated_progress_and_row_order_do_not_change_signature(self):
        before = self.observe('410 410 Ss 00:00.10\n411 410 R 00:01.25\n500 500 R 01:01.50\n')
        after = self.observe('500 500 R 02:01.50\n411 410 R 00:01.25\n410 410 Ss 00:00.10\n')
        self.assertEqual(before, after)

    def test_exited_and_zombie_children_are_absent_from_signature(self):
        live = self.observe('410 410 Ss 00:00.10\n411 410 R 00:01.25\n')
        exited = self.observe('410 410 Ss 00:00.10\n')
        zombie = self.observe('410 410 Ss 00:00.10\n411 410 Z+ 00:01.25\n')
        self.assertNotEqual(live, exited)
        self.assertEqual(exited, zombie)
        self.assertEqual(self.observe('500 500 R 00:00.00\n'), ())

    def test_orphaned_child_keeps_owned_group_observable(self):
        self.assertEqual(self.observe('411 410 S 00:02.50\n'), ((411, Decimal('2.50')),))

    def test_portable_cpu_time_formats_are_normalized(self):
        for cpu_time, seconds in (
                ('01:02.34', '62.34'), ('123:45.67', '7425.67'),
                ('01:02:03', '3723'), ('123:02:03', '442923'),
                ('2-01:02:03', '176523'), ('00:00.100', '.10')):
            with self.subTest(cpu_time=cpu_time):
                self.assertEqual(self.observe(f'410 410 S {cpu_time}\n'),
                                 ((410, Decimal(seconds)),))

    def test_invalid_owned_cpu_times_cannot_be_reported_as_idle(self):
        for cpu_time in ('SECRET', '-01:02', '00:60', '00:60:00', '1-24:00:00',
                         '1-02:03', 'nan', '00:01,20', '00:01.2junk'):
            with self.subTest(cpu_time=cpu_time):
                self.assert_safe_failure(lambda: self.observe(f'410 410 S {cpu_time}\n'))

    def test_malformed_process_rows_cannot_be_reported_as_idle(self):
        for output in ('SECRET', 'pid 410 S 00:01\n', '410 group S 00:01\n',
                       '-410 410 S 00:01\n', '410 -1 S 00:01\n',
                       '410 410 S 00:01 extra\n'):
            with self.subTest(output=output):
                self.assert_safe_failure(lambda: self.observe(output))

    def assert_safe_failure(self, operation):
        with self.assertRaises(ProcessObservationError) as raised:
            operation()
        self.assertNotIn('SECRET', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_ps_failures_raise_safe_observation_error(self):
        for error in (OSError('SECRET'),
                      subprocess.TimeoutExpired('SECRET', 1, output='SECRET')):
            with self.subTest(error=error):
                with patch('mensa.process_health.subprocess.run', side_effect=error):
                    self.assert_safe_failure(lambda: process_activity(self.process))
        result = subprocess.CompletedProcess(['ps'], 1, '', 'SECRET')
        with patch('mensa.process_health.subprocess.run', return_value=result):
            self.assert_safe_failure(lambda: process_activity(self.process))

    def test_process_snapshot_is_bounded_and_uses_stable_locale(self):
        result = subprocess.CompletedProcess(['ps'], 0, '410 410 S 00:01\n', '')
        with patch('mensa.process_health.subprocess.run', return_value=result) as run:
            process_activity(self.process)
        options = run.call_args.kwargs
        self.assertGreater(options['timeout'], 0)
        self.assertLessEqual(options['timeout'], 2)
        self.assertEqual(options['env']['LC_ALL'], 'C')
        self.assertTrue(options['capture_output'])
        self.assertTrue(options['text'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups are required')
    def test_real_disposable_group_is_observed_without_affecting_it(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   start_new_session=True)
        try:
            signature = process_activity(process)
            self.assertIn(process.pid, {pid for pid, seconds in signature})
            self.assertIsNone(process.poll())
            self.assertTrue(all(seconds >= 0 for pid, seconds in signature))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
