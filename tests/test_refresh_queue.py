"""Persistent queue, scheduling, crash recovery, and machine resource gates."""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "refresh_queue.py"
if MODULE.exists():
    spec = importlib.util.spec_from_file_location("refresh_queue", MODULE)
    queue = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(queue)
else:
    queue = None


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
        self.assertIsNotNone(queue, "refresh_queue implementation is missing")
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

    def test_schedule_uses_berlin_monday_0917_and_rejects_naive_time(self):
        self.assertEqual(queue.due_period(datetime(2026, 9, 21, 7, 16, 59, tzinfo=timezone.utc)), "2026-09-14")
        self.assertEqual(queue.due_period(datetime(2026, 9, 21, 7, 17, tzinfo=timezone.utc)), "2026-09-21")
        self.assertEqual(queue.due_period(datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)), "2026-09-21")
        with self.assertRaises(ValueError):
            queue.due_period(datetime(2026, 9, 21, 9, 17))

    def test_schedule_handles_winter_dst_and_year_boundary(self):
        self.assertEqual(queue.due_period(datetime(2026, 12, 28, 8, 16, tzinfo=timezone.utc)), "2026-12-21")
        self.assertEqual(queue.due_period(datetime(2026, 12, 28, 8, 17, tzinfo=timezone.utc)), "2026-12-28")
        self.assertEqual(queue.due_period(datetime(2027, 1, 1, 13, tzinfo=timezone.utc)), "2026-12-28")
        self.assertEqual(queue.due_period(datetime(2026, 3, 30, 7, 17, tzinfo=timezone.utc)), "2026-03-30")

    def test_missing_state_is_empty_without_creating_a_file(self):
        self.assertEqual(queue.load_state(self.path), EMPTY)
        self.assertFalse(self.path.exists())

    def test_state_round_trip_and_parent_directory_creation(self):
        path = self.directory / "nested" / "state.json"
        state = self.publishing()
        queue.save_state(path, state)
        self.assertEqual(queue.load_state(path), state)

    def test_corrupt_state_is_rejected_instead_of_losing_pending_work(self):
        for raw in ("", "{", "[]", '{"schema_version": 2}', '{"schema_version":1,"schema_version":2}'):
            self.path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                queue.load_state(self.path)

    def test_state_validation_rejects_invalid_period_phase_and_typed_values(self):
        invalid = []
        for field, value in (("period", "2026-09-22"), ("phase", "running"), ("attempts", True),
                             ("attempts", -1), ("next_attempt_at", "2026-09-21T10:00:00"),
                             ("last_error", 3), ("run_id", -1), ("commit_sha", "not-a-sha")):
            state = self.pending()
            state["pending"][field] = value
            invalid.append(state)
        state = self.pending()
        state["completed_period"] = state["pending"]["period"]
        invalid.append(state)
        for state in invalid:
            with self.subTest(state=state), self.assertRaises(ValueError):
                queue.save_state(self.path, state)

    def test_collect_phase_cannot_carry_publication_identifiers(self):
        for field, value in (("commit_sha", "a" * 40), ("run_id", 123)):
            state = self.pending()
            state["pending"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                queue.validate_state(state)

    def test_initial_reconcile_creates_exactly_one_collect_job_without_mutating_input(self):
        initial = copy.deepcopy(EMPTY)
        state = queue.reconcile(initial, NOW)
        self.assertEqual(initial, EMPTY)
        self.assertEqual(state["pending"], {"period": "2026-09-21", "phase": "collect", "attempts": 0,
                         "next_attempt_at": None, "last_error": None, "commit_sha": None, "run_id": None,
                         "dispatch_requested_at": None})
        self.assertEqual(queue.reconcile(state, NOW), state)

    def test_missed_collection_weeks_coalesce_and_reset_retry_state(self):
        state = queue.failed(self.pending(), "offline", NOW)
        fresh = queue.reconcile(state, NOW + timedelta(weeks=4))
        self.assertEqual(fresh["pending"]["period"], "2026-10-19")
        self.assertEqual(fresh["pending"]["attempts"], 0)
        self.assertIsNone(fresh["pending"]["next_attempt_at"])
        self.assertEqual(state["pending"]["period"], "2026-09-21")

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

    def test_backoff_retains_work_and_phase_with_six_hour_cap(self):
        state = self.publishing()
        original = copy.deepcopy(state)
        for attempt, delay in enumerate((15, 30, 60, 120, 240, 360, 360, 360), 1):
            state = queue.failed(state, "still offline", NOW)
            pending = state["pending"]
            self.assertEqual(pending["attempts"], attempt)
            self.assertEqual(datetime.fromisoformat(pending["next_attempt_at"].replace("Z", "+00:00")), NOW + timedelta(minutes=delay))
            self.assertEqual(pending["phase"], "publish")
            self.assertEqual(pending["commit_sha"], "a" * 40)
            self.assertEqual(pending["run_id"], 123)
            self.assertEqual(pending["last_error"], "still offline")
        self.assertEqual(original["pending"]["attempts"], 0)
        queue.save_state(self.path, state)
        self.assertEqual(queue.load_state(self.path), state)

    def test_completed_period_prevents_duplicate_jobs_and_new_week_still_queues(self):
        state = queue.completed(self.publishing())
        self.assertEqual(state, {"schema_version": 1, "completed_period": "2026-09-21", "pending": None})
        self.assertEqual(queue.completed(state), state)
        self.assertEqual(queue.reconcile(state, NOW), state)
        self.assertEqual(queue.reconcile(state, NOW + timedelta(weeks=1))["pending"]["period"], "2026-09-28")

    def test_acknowledging_old_publish_then_reconcile_queues_only_latest_period(self):
        state = queue.completed(self.publishing())
        latest = queue.reconcile(state, NOW + timedelta(weeks=6, hours=1))
        self.assertEqual(latest["completed_period"], "2026-09-21")
        self.assertEqual(latest["pending"]["period"], "2026-11-02")

    def test_clock_rollback_cannot_replace_future_pending_or_completed_work(self):
        pending = self.pending()
        self.assertEqual(queue.reconcile(pending, NOW - timedelta(weeks=1)), pending)
        finished = queue.completed(pending)
        self.assertEqual(queue.reconcile(finished, NOW - timedelta(weeks=1)), finished)

    def test_failure_without_pending_job_is_rejected(self):
        with self.assertRaises(ValueError):
            queue.failed(copy.deepcopy(EMPTY), "no job", NOW)

    def test_failed_atomic_replace_retains_previous_file_and_cleans_temporary_file(self):
        original = self.pending()
        queue.save_state(self.path, original)
        with patch.object(queue.os, "replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                queue.save_state(self.path, self.publishing())
        self.assertEqual(queue.load_state(self.path), original)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["state.json"])

    def test_atomic_save_fsyncs_before_replace_and_after_replace(self):
        events = []
        real_fsync, real_replace = queue.os.fsync, queue.os.replace
        def fsync(fd):
            events.append("fsync")
            return real_fsync(fd)
        def replace(source, destination):
            events.append("replace")
            return real_replace(source, destination)
        with patch.object(queue.os, "fsync", side_effect=fsync), patch.object(queue.os, "replace", side_effect=replace):
            queue.save_state(self.path, self.pending())
        self.assertEqual(events, ["fsync", "replace", "fsync"])

    def test_exclusive_lock_rejects_contention_and_releases_on_exception(self):
        lock = self.directory / "worker.lock"
        with self.assertRaisesRegex(RuntimeError, "crash"):
            with queue.exclusive_lock(lock):
                with self.assertRaises(queue.BusyError):
                    with queue.exclusive_lock(lock):
                        self.fail("second worker acquired lock")
                raise RuntimeError("crash")
        with queue.exclusive_lock(lock):
            pass

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
    def setUp(self):
        self.assertIsNotNone(queue, "refresh_queue implementation is missing")

    def status(self, vm=VM_OK, power="Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", load=1, cpus=8):
        def command(args, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 10)
            output = vm if Path(args[0]).name == "vm_stat" else power
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        with (patch.object(queue.subprocess, "run", side_effect=command),
              patch.object(queue.os, "getloadavg", return_value=(load, load, load)),
              patch.object(queue.os, "cpu_count", return_value=cpus)):
            return queue.resource_status()

    def test_available_memory_counts_free_inactive_and_speculative_pages(self):
        allowed, reason = self.status()
        self.assertTrue(allowed, reason)

    def test_memory_below_thirty_two_gib_is_deferred(self):
        allowed, reason = self.status(vm=VM_OK.replace("1600000", "1500000"))
        self.assertFalse(allowed)
        self.assertIn("memory", reason.lower())
        self.assertIn("32 GiB", reason)

    def test_exact_thirty_two_gib_boundary_uses_all_available_page_categories(self):
        exact = VM_OK.replace("1600000", "1587152")
        self.assertTrue(self.status(vm=exact)[0])
        self.assertFalse(self.status(vm=exact.replace("1587152", "1587151"))[0])

    def test_page_size_is_read_from_vm_stat_not_hardcoded(self):
        allowed, reason = self.status(vm=VM_OK.replace("16384", "4096"))
        self.assertFalse(allowed)
        self.assertIn("memory", reason.lower())

    def test_load_threshold_is_sixty_percent_of_cpu_count_with_floor_two(self):
        self.assertTrue(self.status(load=4.8, cpus=8)[0])
        self.assertFalse(self.status(load=4.81, cpus=8)[0])
        self.assertTrue(self.status(load=2, cpus=1)[0])
        self.assertFalse(self.status(load=2.01, cpus=1)[0])

    def test_battery_blocks_but_desktop_without_battery_is_allowed(self):
        self.assertFalse(self.status(power="Now drawing from 'Battery Power'\n -InternalBattery-0 97%; discharging")[0])
        self.assertTrue(self.status(power="No batteries.")[0])

    def test_malformed_vm_stat_and_command_failure_defer_without_sleeping(self):
        self.assertFalse(self.status(vm="garbage")[0])
        with (patch.object(queue.subprocess, "run", side_effect=OSError("not available")),
              patch.object(queue.os, "getloadavg", return_value=(1, 1, 1))):
            allowed, reason = queue.resource_status()
        self.assertFalse(allowed)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
