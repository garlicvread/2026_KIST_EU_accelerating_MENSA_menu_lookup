"""유휴 작업자도 큐 잠금 안에서 로그·용량 관리를 실행하는지 확인합니다."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mensa.jobs import JobRunner
from mensa.queue import BusyError, exclusive_lock, save_state


class JobMaintenanceTests(unittest.TestCase):
    def test_idle_worker_runs_maintenance_under_lock_without_collecting(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            base.chmod(0o700)
            now = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
            save_state(base/'queue.json', {'schema_version': 1,
                'completed_period': '2026-09-21T11:00', 'pending': None})
            events = []
            owner = self
            class Work:
                def maintain(self, moment):
                    with owner.assertRaises(BusyError):
                        with exclusive_lock(base/'worker.lock'):
                            pass
                    events.append(moment)
                def __call__(self, state):
                    raise AssertionError('Idle worker collected data')
            result = JobRunner(base, resources=lambda: (True, 'ready'), work=Work()).run(now)
            self.assertEqual(result['status'], 'idle')
            self.assertEqual(events, [now])
