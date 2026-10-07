"""이 테스트 모듈은 메뉴 갱신을 올바른 시점과 자원 조건에서 실행하도록 디스크에 저장하는 작업 대기열, 실행 일정, 중단 후 복구, 호스트 자원 확인을 검사합니다."""

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mensa.config import ResourceSettings
from mensa.errors import GenerationError
from mensa import queue, resources

from scripts import refresh_queue as resource_adapter

NOW = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
EMPTY = {"schema_version": 1, "completed_period": None, "pending": None}
VM_OK = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              500000.
Pages active:                            100000.
Pages inactive:                         1600000.
Pages speculative:                        10000.
Pages wired down:                        100000.
'''


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "state.json"

    def pending(self):
        return queue.reconcile(copy.deepcopy(EMPTY), NOW)

    def publishing(self):
        state = self.pending()
        state["pending"].update(phase="publish", commit_sha="a" * 40, run_id=123)
        return state

    def test_schedule_uses_three_berlin_morning_slots(self):
        cases = [(7, 0, "2026-09-21T09:00"), (8, 0, "2026-09-21T10:00"),
                 (9, 0, "2026-09-21T11:00"), (6, 59, "2026-09-20T11:00")]
        for hour, minute, expected in cases:
            self.assertEqual(queue.due_period(datetime(2026, 9, 21, hour, minute, tzinfo=timezone.utc)), expected)

    def test_schedule_uses_berlin_winter_offset(self):
        self.assertEqual(queue.due_period(datetime(2026, 12, 28, 7, 59, tzinfo=timezone.utc)), "2026-12-27T11:00")
        self.assertEqual(queue.due_period(datetime(2026, 12, 28, 8, 0, tzinfo=timezone.utc)), "2026-12-28T09:00")


    def test_corrupt_state_is_rejected_instead_of_losing_pending_work(self):
        for raw in ("{", '{"schema_version": 2}'):
            self.path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                queue.load_state(self.path)


    def test_missed_collection_weeks_coalesce_and_reset_retry_state(self):
        state = queue.failed(self.pending(), "offline", NOW)
        fresh = queue.reconcile(state, NOW + timedelta(weeks=4))
        self.assertEqual(fresh["pending"]["period"], "2026-10-19T10:00")
        self.assertEqual(fresh["pending"]["attempts"], 0)
        self.assertIsNone(fresh["pending"]["next_attempt_at"])
        self.assertEqual(state["pending"]["period"], "2026-09-21T10:00")

    def test_publish_phase_survives_missed_weeks_and_retry_metadata_is_preserved(self):
        state = queue.failed(self.publishing(), "deployment unavailable", NOW)
        state["pending"]["dispatch_requested_at"] = NOW.isoformat()
        self.assertEqual(queue.reconcile(state, NOW + timedelta(weeks=4)), state)
        queue.save_state(self.path, state)
        self.assertEqual(queue.reconcile(queue.load_state(self.path), NOW + timedelta(weeks=4)), state)

    def test_publish_crash_checkpoint_without_ids_remains_resumable(self):
        state = self.pending()
        state["pending"]["phase"] = "publish"
        queue.save_state(self.path, state)
        self.assertEqual(queue.load_state(self.path), state)
        self.assertEqual(queue.reconcile(state, NOW + timedelta(weeks=1)), state)


    def test_completed_period_prevents_duplicate_jobs_and_next_day_survives_restart(self):
        state = queue.completed(self.publishing())
        self.assertEqual(state, {"schema_version": 1, "completed_period": "2026-09-21T10:00", "pending": None})
        self.assertEqual(queue.completed(state), state)
        self.assertEqual(queue.reconcile(state, NOW), state)
        # 같은 날 10시 작업을 끝내도 11시 작업은 새로 만들며, 같은 시간대는 반복하지 않습니다.
        next_slot = queue.reconcile(state, NOW + timedelta(hours=1))
        self.assertEqual(next_slot["pending"]["period"], "2026-09-21T11:00")
        self.assertEqual(queue.reconcile(next_slot, NOW + timedelta(hours=1)), next_slot)
        next_day = queue.reconcile(state, NOW + timedelta(days=1))
        self.assertEqual(next_day["pending"]["period"], "2026-09-22T10:00")
        queue.save_state(self.path, next_day)
        self.assertEqual(queue.reconcile(queue.load_state(self.path), NOW + timedelta(days=1)), next_day)


    def test_failed_atomic_replace_retains_previous_file_and_cleans_temporary_file(self):
        original = self.pending()
        queue.save_state(self.path, original)
        with patch.object(queue.os, "replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                queue.save_state(self.path, self.publishing())
        self.assertEqual(queue.load_state(self.path), original)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["state.json"])


    def test_another_process_cannot_acquire_held_lock(self):
        lock = self.directory / "worker.lock"
        script = """import fcntl,sys
with open(sys.argv[1], 'a') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(17)
sys.exit(0)
"""
        with queue.exclusive_lock(lock):
            result = subprocess.run([sys.executable, "-c", script, str(lock)], timeout=5, check=False)
        self.assertEqual(result.returncode, 17)
        result = subprocess.run([sys.executable, "-c", script, str(lock)], timeout=5, check=False)
        self.assertEqual(result.returncode, 0)


class ResourceTests(unittest.TestCase):
    def status(self, vm=VM_OK, power="Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged"):
        def command(args, **kwargs):
            output = vm if Path(args[0]).name == "vm_stat" else power
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        with (patch.object(resources.subprocess, "run", side_effect=command),
              patch.object(resources.os, "getloadavg", return_value=(1, 1, 1)),
              patch.object(resources.os, "cpu_count", return_value=8)):
            return resource_adapter.resource_status()

    def test_memory_below_thirty_two_gib_is_deferred(self):
        allowed, reason = self.status(vm=VM_OK.replace("1600000", "1500000"))
        self.assertFalse(allowed)
        self.assertIn("memory", reason.lower())
        self.assertIn("32 GiB", reason)


    def test_battery_blocks_but_desktop_without_battery_is_allowed(self):
        self.assertFalse(self.status(power="Now drawing from 'Battery Power'\n -InternalBattery-0 97%; discharging")[0])
        self.assertTrue(self.status(power="No batteries.")[0])

    def test_malformed_vm_stat_and_command_failure_defer_without_sleeping(self):
        self.assertFalse(self.status(vm="garbage")[0])
        with (patch.object(resources.subprocess, "run", side_effect=OSError("not available")),
              patch.object(resources.os, "getloadavg", return_value=(1, 1, 1))):
            allowed, reason = resource_adapter.resource_status()
        self.assertFalse(allowed)
        self.assertTrue(reason)


class ConfiguredResourceTests(unittest.TestCase):
    def test_configured_memory_and_fractional_cpu_limits_defer(self):
        snapshot = resources.ResourceSnapshot(4096, .5, .5, True)
        self.assertTrue(resources.resource_status(ResourceSettings('linux', 4096, 1), probe=lambda: snapshot)[0])
        self.assertFalse(resources.resource_status(ResourceSettings('linux', 4097, 1), probe=lambda: snapshot)[0])
        self.assertFalse(resources.resource_status(ResourceSettings('linux', 4096, .99), probe=lambda: snapshot)[0])


class LinuxResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.proc = self.base / 'proc'
        self.cgroup = self.base / 'visible cgroup'
        (self.proc / 'self').mkdir(parents=True)
        (self.cgroup / 'tenant' / 'worker').mkdir(parents=True)
        self.write(self.proc / 'meminfo', 'MemAvailable: 100000 kB\n')
        self.write(self.proc / 'self/cgroup', '0::/host/tenant/worker\n')
        mount = str(self.cgroup).replace(' ', r'\040')
        self.write(self.proc / 'self/mountinfo', f'29 23 0:26 /host {mount} rw - cgroup2 cgroup rw\n')
        self.controllers(self.cgroup / 'tenant', memory='80000000', current='10000000', cpu='200000 100000')
        self.controllers(self.cgroup / 'tenant/worker', memory='max', current='2000000', cpu='max 100000')

    def write(self, path, text):
        path.write_text(text)

    def controllers(self, path, *, memory, current, cpu):
        self.write(path / 'memory.max', memory+'\n')
        self.write(path / 'memory.current', current+'\n')
        self.write(path / 'cpu.max', cpu+'\n')

    def measure(self, *, affinity=None):
        return resources.linux_resources(proc_root=self.proc, cgroup_root=self.cgroup,
            read_text=lambda path: path.read_text(), load=lambda: .4, cpu_count=lambda: 8,
            affinity=(lambda: {0, 1, 2, 3}) if affinity is None else affinity)

    def status(self):
        return resources.resource_status(ResourceSettings('linux', 1, 1), probe=self.measure)

    def test_real_current_group_mapping_applies_ancestor_memory_and_cpu_caps(self):
        snapshot = self.measure()
        self.assertEqual(snapshot, resources.ResourceSnapshot(70000000, .4, 2, True))
        self.controllers(self.cgroup, memory='50000000', current='10000000', cpu='50000 100000')
        self.assertEqual(self.measure(), resources.ResourceSnapshot(40000000, .4, .5, True))

    def test_unlimited_limits_still_cap_by_host_available_and_affinity(self):
        self.controllers(self.cgroup / 'tenant', memory='max', current='0', cpu='max 100000')
        self.assertEqual(self.measure(affinity=lambda: {1}), resources.ResourceSnapshot(102400000, .4, 1, True))


    def test_missing_linux_controller_defers(self):
        (self.cgroup / 'tenant/worker/memory.max').unlink()
        ready, reason = self.status()
        self.assertFalse(ready)
        self.assertTrue(reason)


class GenerationQueueTests(unittest.TestCase):
    """GenerationQueueTests는 queue 코드가 번역 오류의 종류와 재시도 가능 여부를 작업 대기열 파일에 올바르게 저장하고 읽는지 확인합니다. 작업을 안전하게 재개하도록 오류에 따른 차단, 재시도 시각 계산, 명시적 재시도 요청의 처리도 검사합니다."""

    setUp = QueueTests.setUp
    pending = QueueTests.pending
    publishing = QueueTests.publishing


    def blocked(self, state=None):
        return queue.failed(state or self.pending(), GenerationError("authentication"), NOW)


    def test_permanent_failure_keeps_safe_message_work_and_identifiers(self):
        original = self.publishing()
        original["pending"]["dispatch_requested_at"] = NOW.isoformat()
        untouched = copy.deepcopy(original)
        error = GenerationError("authentication", status_code=401)
        state = queue.failed(original, error, NOW)
        expected = copy.deepcopy(original)
        expected["pending"].update(attempts=1, next_attempt_at=None, last_error=str(error),
                                   generation_failure={"code": "authentication", "retryable": False})
        self.assertEqual(state, expected)
        self.assertEqual(original, untouched)
        queue.save_state(self.path, state)
        self.assertEqual(queue.load_state(self.path), state)

    def test_retry_after_is_lower_bound_for_backoff(self):
        for attempts, requested, seconds in ((0, None, 900), (8, 60, 21600), (8, 86400, 86400)):
            original = self.pending()
            original["pending"]["attempts"] = attempts
            untouched = copy.deepcopy(original)
            state = queue.failed(original, GenerationError("http_retryable", retry_after=requested), NOW)
            with self.subTest(attempts=attempts, requested=requested):
                self.assertIn("generation_failure", state["pending"])
                self.assertEqual(state["pending"]["generation_failure"],
                                 {"code": "http_retryable", "retryable": True})
                self.assertEqual(datetime.fromisoformat(state["pending"]["next_attempt_at"].replace("Z", "+00:00")),
                                 NOW + timedelta(seconds=seconds))
                self.assertEqual(state["pending"]["attempts"], attempts + 1)
                self.assertEqual(original, untouched)
                if seconds > 3600:
                    # 다음 수집 시간이 와도 서버가 지정한 대기 시간을 지워서는 안 됩니다.
                    following = queue.reconcile(state, NOW + timedelta(hours=1))
                    self.assertEqual(following["pending"]["period"], "2026-09-21T11:00")
                    self.assertEqual(following["pending"]["next_attempt_at"], state["pending"]["next_attempt_at"])
                    self.assertEqual(following["pending"]["attempts"], state["pending"]["attempts"])


    def test_future_week_coalesces_but_keeps_permanent_block(self):
        original = self.blocked()
        untouched = copy.deepcopy(original)
        state = queue.reconcile(original, NOW + timedelta(weeks=4))
        expected = copy.deepcopy(original)
        expected["pending"]["period"] = "2026-10-19T10:00"
        self.assertEqual(state, expected)
        self.assertEqual(original, untouched)


    def test_request_retry_preserves_publication_work_and_clears_failure_block(self):
        original = self.blocked(self.publishing())
        original["pending"]["dispatch_requested_at"] = NOW.isoformat()
        untouched = copy.deepcopy(original)
        expected = copy.deepcopy(original)
        expected["pending"].pop("generation_failure")
        expected["pending"].update(next_attempt_at=None, last_error=None)
        self.assertEqual(queue.request_retry(original), expected)
        self.assertEqual(original, untouched)

if __name__ == "__main__":
    unittest.main()
