"""이 테스트 모듈은 메뉴 갱신을 올바른 시점과 자원 조건에서 실행하도록 디스크에 저장하는 작업 대기열, 실행 일정, 중단 후 복구, 호스트 자원 확인을 검사합니다."""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from dataclasses import FrozenInstanceError

from mensa.config import ResourceSettings

from scripts import refresh_queue as resource_adapter

RESOURCES_PATH = Path(__file__).resolve().parents[1] / 'mensa/resources.py'
resources = None
if RESOURCES_PATH.exists():
    resources = importlib.import_module('mensa.resources')


MODULE = Path(__file__).resolve().parents[1] / "mensa" / "queue.py"
if MODULE.exists():
    spec = importlib.util.spec_from_file_location("mensa.queue", MODULE)
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
        self.assertIsNotNone(queue, "mensa.queue implementation is missing")
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

    def test_schedule_uses_three_berlin_morning_slots_and_rejects_naive_time(self):
        cases = [(7, 0, "2026-09-21T09:00"), (8, 0, "2026-09-21T10:00"),
                 (9, 0, "2026-09-21T11:00"), (6, 59, "2026-09-20T11:00")]
        for hour, minute, expected in cases:
            self.assertEqual(queue.due_period(datetime(2026, 9, 21, hour, minute, tzinfo=timezone.utc)), expected)
        with self.assertRaises(ValueError):
            queue.due_period(datetime(2026, 9, 21, 9))

    def test_schedule_handles_winter_dst_and_year_boundary(self):
        self.assertEqual(queue.due_period(datetime(2026, 12, 28, 7, 59, tzinfo=timezone.utc)), "2026-12-27T11:00")
        self.assertEqual(queue.due_period(datetime(2026, 12, 28, 8, 0, tzinfo=timezone.utc)), "2026-12-28T09:00")
        self.assertEqual(queue.due_period(datetime(2027, 1, 1, 13, tzinfo=timezone.utc)), "2027-01-01T11:00")
        self.assertEqual(queue.due_period(datetime(2026, 3, 30, 7, 0, tzinfo=timezone.utc)), "2026-03-30T09:00")

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
        for field, value in (("period", "2026-09-32"), ("phase", "running"), ("attempts", True),
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
        self.assertEqual(state["pending"], {"period": "2026-09-21T10:00", "phase": "collect", "attempts": 0,
                         "next_attempt_at": None, "last_error": None, "commit_sha": None, "run_id": None,
                         "dispatch_requested_at": None})
        self.assertEqual(queue.reconcile(state, NOW), state)

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

    def test_acknowledging_old_publish_then_reconcile_queues_only_latest_period(self):
        state = queue.completed(self.publishing())
        latest = queue.reconcile(state, NOW + timedelta(weeks=6, hours=1))
        self.assertEqual(latest["completed_period"], "2026-09-21T10:00")
        self.assertEqual(latest["pending"]["period"], "2026-11-02T10:00")

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
        self.assertIsNotNone(resources, "mensa.resources implementation is missing")

    def status(self, vm=VM_OK, power="Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", load=1, cpus=8):
        def command(args, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 10)
            output = vm if Path(args[0]).name == "vm_stat" else power
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        with (patch.object(resources.subprocess, "run", side_effect=command),
              patch.object(resources.os, "getloadavg", return_value=(load, load, load)),
              patch.object(resources.os, "cpu_count", return_value=cpus)):
            return resource_adapter.resource_status()

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
        with (patch.object(resources.subprocess, "run", side_effect=OSError("not available")),
              patch.object(resources.os, "getloadavg", return_value=(1, 1, 1))):
            allowed, reason = resource_adapter.resource_status()
        self.assertFalse(allowed)
        self.assertTrue(reason)


class ConfiguredResourceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(resources, 'mensa.resources implementation is missing')

    def test_configured_thresholds_use_measured_fractional_capacity_without_legacy_floor(self):
        snapshot = resources.ResourceSnapshot(4096, .5, .5, True)
        with self.assertRaises(FrozenInstanceError):
            snapshot.cpus = 4
        policy = ResourceSettings('linux', 4096, 1)
        self.assertTrue(resources.resource_status(policy, probe=lambda: snapshot)[0])
        self.assertFalse(resources.resource_status(ResourceSettings('linux', 4097, 1), probe=lambda: snapshot)[0])
        self.assertFalse(resources.resource_status(ResourceSettings('linux', 4096, .99), probe=lambda: snapshot)[0])
        self.assertTrue(resources.resource_status(ResourceSettings('linux', 4096, .1), probe=lambda: snapshot, minimum_load=2)[0])

    def test_declared_platform_selects_measurement_and_reasons_do_not_claim_linux_ac(self):
        with (patch.object(resources, 'macos_resources', return_value=resources.ResourceSnapshot(8192, 1, 2, True)),
              patch.object(resources, 'linux_resources', return_value=resources.ResourceSnapshot(1024, 1, 2, True))):
            self.assertTrue(resources.resource_status(ResourceSettings('macos', 4096, 1))[0])
            self.assertFalse(resources.resource_status(ResourceSettings('linux', 4096, 1))[0])
        ready, reason = resources.resource_status(ResourceSettings('linux', 1, 1),
                                                  probe=lambda: resources.ResourceSnapshot(4096, 0, 1, True))
        self.assertTrue(ready)
        self.assertNotIn('AC', reason)

    def test_invalid_configuration_and_ports_are_rejected_before_probing(self):
        probe = Mock()
        for policy in (None, ResourceSettings('unknown', 1, 1), ResourceSettings('linux', True, 1),
                       ResourceSettings('linux', 0, 1), ResourceSettings('linux', 1, True),
                       ResourceSettings('linux', 1, float('nan')), ResourceSettings('linux', 1, 0)):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                resources.resource_status(policy, probe=probe)
        for kwargs in ({'probe': 1}, {'minimum_load': True}, {'minimum_load': -1}, {'minimum_load': float('inf')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                resources.resource_status(ResourceSettings('linux', 1, 1), **kwargs)
        probe.assert_not_called()
        for name, ports in (('macos_resources', ('command', 'load', 'cpu_count')),
                            ('linux_resources', ('read_text', 'load', 'cpu_count', 'affinity'))):
            for port in ports:
                with self.subTest(name=name, port=port), self.assertRaises(ValueError):
                    getattr(resources, name)(**{port: 1})

    def test_bad_measurements_defer_with_bounded_reasons_and_cancellation_propagates(self):
        policy = ResourceSettings('linux', 1, 1)
        for snapshot in (None, resources.ResourceSnapshot(True, 0, 1, True),
                         resources.ResourceSnapshot(-1, 0, 1, True), resources.ResourceSnapshot(1, float('inf'), 1, True),
                         resources.ResourceSnapshot(1, 0, 0, True), resources.ResourceSnapshot(1, 0, 1, 'yes'),
                         resources.ResourceSnapshot(1, 0, 10 ** 400, True)):
            with self.subTest(snapshot=snapshot):
                try:
                    ready, reason = resources.resource_status(policy, probe=lambda: snapshot)
                except OverflowError as error:
                    self.fail(f'Overflowing snapshot must return a bounded defer reason: {error}')
                self.assertFalse(ready); self.assertTrue(reason); self.assertLessEqual(len(reason), 550)
        for error in (ValueError('x'*1000), OSError('not available'), subprocess.SubprocessError('failed'), UnicodeError('invalid')):
            ready, reason = resources.resource_status(policy, probe=Mock(side_effect=error))
            self.assertFalse(ready); self.assertTrue(reason); self.assertLessEqual(len(reason), 550)
        for error in (SystemExit(143), KeyboardInterrupt()):
            with self.assertRaises(type(error)) as raised:
                resources.resource_status(policy, probe=Mock(side_effect=error))
            self.assertIs(raised.exception, error)

    def test_unknown_power_and_cpu_defer_instead_of_inventing_ready_measurements(self):
        for power in ('', 'unknown', 'InternalBattery discharging', "Now drawing from 'Battery Power'"):
            with self.subTest(power=power):
                def command(args):
                    return VM_OK if Path(args[0]).name == 'vm_stat' else power
                ready, reason = resources.resource_status(ResourceSettings('macos', 1, 1),
                    probe=lambda: resources.macos_resources(command=command, load=lambda: 0, cpu_count=lambda: 8))
                self.assertFalse(ready); self.assertTrue(reason)
        for cpus in (None, 0, True, 1.5):
            with self.subTest(cpus=cpus):
                ready, _ = resources.resource_status(ResourceSettings('macos', 1, 1),
                    probe=lambda: resources.macos_resources(command=lambda args: VM_OK if Path(args[0]).name=='vm_stat' else 'No batteries.', load=lambda: 0, cpu_count=lambda: cpus))
                self.assertFalse(ready)


class LinuxResourceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(resources, 'mensa.resources implementation is missing')
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

    def measure(self, **ports):
        return resources.linux_resources(proc_root=self.proc, cgroup_root=self.cgroup,
            read_text=ports.pop('read_text', lambda path: path.read_text()),
            load=ports.pop('load', lambda: .4), cpu_count=ports.pop('cpu_count', lambda: 8),
            affinity=ports.pop('affinity', lambda: {0,1,2,3}), **ports)

    def status(self):
        return resources.resource_status(ResourceSettings('linux', 1, 1), probe=self.measure)

    def test_real_current_group_mapping_applies_ancestor_memory_and_cpu_caps(self):
        snapshot = self.measure()
        self.assertEqual(snapshot, resources.ResourceSnapshot(70000000, .4, 2, True))
        self.controllers(self.cgroup, memory='50000000', current='10000000', cpu='50000 100000')
        self.assertEqual(self.measure(), resources.ResourceSnapshot(40000000, .4, .5, True))
        self.controllers(self.cgroup / 'tenant/worker', memory='100', current='200', cpu='max 100000')
        self.assertEqual(self.measure().available_bytes, 0)
        self.assertFalse(self.status()[0])

    def test_unlimited_limits_still_cap_by_host_available_and_affinity(self):
        self.controllers(self.cgroup / 'tenant', memory='max', current='0', cpu='max 100000')
        self.assertEqual(self.measure(affinity=lambda: {1}), resources.ResourceSnapshot(102400000, .4, 1, True))
        self.assertEqual(self.measure(cpu_count=lambda: 2), resources.ResourceSnapshot(102400000, .4, 2, True))

    def test_unsafe_ambiguous_and_unresolvable_metadata_never_reads_outside_mount(self):
        original_group = (self.proc / 'self/cgroup').read_text()
        original_mount = (self.proc / 'self/mountinfo').read_text()
        for group, mount in [('0::/host/../escape\n', original_mount), ('0::/elsewhere\n', original_mount),
                             ('1:memory:/host/tenant/worker\n', original_mount), (original_group*2, original_mount),
                             (original_group, original_mount*2), (original_group, original_mount.replace('cgroup2', 'cgroup')),
                             (original_group, original_mount.replace('/host ', '/host/../escape ')),
                             (original_group, original_mount.replace(str(self.cgroup).replace(' ', r'\040'), '/sys/fs/cgroup'))]:
            with self.subTest(group=group, mount=mount):
                self.write(self.proc / 'self/cgroup', group); self.write(self.proc / 'self/mountinfo', mount)
                def read(path):
                    self.assertTrue(path.is_relative_to(self.proc) or path.is_relative_to(self.cgroup), path)
                    return path.read_text()
                ready, reason = resources.resource_status(ResourceSettings('linux', 1, 1), probe=lambda: self.measure(read_text=read))
                self.assertFalse(ready); self.assertTrue(reason)

    def test_missing_invalid_controller_and_host_measurements_defer(self):
        leaf = self.cgroup / 'tenant/worker'
        cases = [(leaf/'memory.max', None), (leaf/'memory.current', None), (leaf/'cpu.max', None),
                 (leaf/'memory.max', '-1'), (leaf/'memory.current', '-1'), (leaf/'cpu.max', '0 100000'),
                 (leaf/'cpu.max', 'max 0'), (leaf/'cpu.max', '100000'),
                 (leaf/'cpu.max', '9' * 400 + ' 1'),
                 (self.proc/'meminfo', 'MemAvailable: 1 MB\n'), (self.proc/'meminfo', 'MemAvailable: 1 kB\nMemAvailable: 2 kB\n')]
        for path, value in cases:
            old = path.read_bytes()
            with self.subTest(path=path, value=value):
                if value is None: path.unlink()
                else: path.write_text(value)
                try:
                    ready, reason = self.status()
                except OverflowError as error:
                    self.fail(f'Overflowing controller must return a bounded defer reason: {error}')
                self.assertFalse(ready)
                self.assertTrue(reason)
                self.assertLessEqual(len(reason), 550)
            path.write_bytes(old)
        for port, value in [('cpu_count', None), ('cpu_count', True), ('affinity', set()), ('affinity', {True}),
                            ('affinity', {-1}), ('affinity', [0]), ('load', float('nan'))]:
            with self.subTest(port=port, value=value):
                ready, _ = resources.resource_status(ResourceSettings('linux', 1, 1), probe=lambda: self.measure(**{port: lambda: value}))
                self.assertFalse(ready)
        def unavailable(path):
            if path == leaf/'memory.max': raise PermissionError('inaccessible controller')
            return path.read_text()
        ready, _ = resources.resource_status(ResourceSettings('linux', 1, 1), probe=lambda: self.measure(read_text=unavailable))
        self.assertFalse(ready)



class GenerationQueueTests(unittest.TestCase):
    """GenerationQueueTests는 queue 코드가 번역 오류의 종류와 재시도 가능 여부를 작업 대기열 파일에 올바르게 저장하고 읽는지 확인합니다. 작업을 안전하게 재개하도록 오류에 따른 차단, 재시도 시각 계산, 명시적 재시도 요청의 처리도 검사합니다."""

    setUp = QueueTests.setUp
    pending = QueueTests.pending
    publishing = QueueTests.publishing

    def generation_error(self, code, **kwargs):
        from mensa.errors import GenerationError
        return GenerationError(code, **kwargs)

    def blocked(self, state=None):
        return queue.failed(state or self.pending(), self.generation_error("authentication"), NOW)

    def test_card16_valid_markers_roundtrip(self):
        for code in ("authentication", "http_error", "http_retryable", "timeout", "network",
                     "redirect", "invalid_config", "invalid_ollama_endpoint", "invalid_json",
                     "invalid_envelope", "incomplete_generation", "invalid_result", "response_too_large"):
            state = self.pending()
            error = self.generation_error(code)
            state["pending"]["generation_failure"] = {"code": code, "retryable": error.retryable}
            with self.subTest(code=code):
                try:
                    queue.save_state(self.path, state)
                except ValueError as exc:
                    self.fail(f"Valid typed marker must be accepted: {exc}")
                self.assertEqual(queue.load_state(self.path), state)

    def test_card16_malformed_markers_are_rejected(self):
        invalid = (None, [], "authentication", {}, {"code": "authentication"},
                   {"code": "unknown", "retryable": False},
                   {"code": 1, "retryable": False},
                   {"code": "authentication", "retryable": 0},
                   {"code": "timeout", "retryable": 1},
                   {"code": "authentication", "retryable": "false"},
                   {"code": "authentication", "retryable": True},
                   {"code": "timeout", "retryable": False},
                   {"code": "authentication", "retryable": False, "url": "remote"})
        for marker in invalid:
            state = self.pending()
            state["pending"]["generation_failure"] = marker
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                queue.validate_state(state)
        state = self.pending()
        state["pending"].update(generation_failure={"code": "authentication", "retryable": False},
                                next_attempt_at=NOW.isoformat())
        with self.assertRaises(ValueError):
            queue.validate_state(state)

    def test_card16_permanent_failure_keeps_safe_message_work_and_identifiers(self):
        original = self.publishing()
        original["pending"]["dispatch_requested_at"] = NOW.isoformat()
        untouched = copy.deepcopy(original)
        error = self.generation_error("authentication", status_code=401)
        state = queue.failed(original, error, NOW)
        expected = copy.deepcopy(original)
        expected["pending"].update(attempts=1, next_attempt_at=None, last_error=str(error),
                                   generation_failure={"code": "authentication", "retryable": False})
        self.assertEqual(state, expected)
        self.assertEqual(original, untouched)
        self.assertIsNot(state["pending"], original["pending"])
        queue.save_state(self.path, state)
        self.assertEqual(queue.load_state(self.path), state)

    def test_card16_retry_after_is_lower_bound_for_backoff(self):
        for attempts, requested, seconds in ((0, None, 900), (0, 60, 900), (0, 1800, 1800),
                                             (8, 60, 21600), (8, 86400, 86400)):
            original = self.pending()
            original["pending"]["attempts"] = attempts
            untouched = copy.deepcopy(original)
            state = queue.failed(original, self.generation_error("http_retryable", retry_after=requested), NOW)
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

    def test_card16_fractional_retry_delay_and_clock_never_schedule_early(self):
        now = NOW.replace(microsecond=123456)
        state = queue.failed(self.pending(), self.generation_error("http_retryable", retry_after=1800.75), now)
        self.assertEqual(datetime.fromisoformat(state["pending"]["next_attempt_at"].replace("Z", "+00:00")),
                         now + timedelta(seconds=1800.75))

    def test_card16_generic_failure_clears_stale_marker_and_keeps_old_limit(self):
        state = self.blocked()
        changed = queue.failed(state, "x" * 2500, NOW)
        self.assertNotIn("generation_failure", changed["pending"])
        self.assertEqual(changed["pending"]["last_error"], "x" * 2000)
        self.assertEqual(changed["pending"]["next_attempt_at"], "2026-09-21T08:30:00Z")
        self.assertIn("generation_failure", state["pending"])

    def test_card16_future_week_coalesces_but_keeps_permanent_block(self):
        original = self.blocked()
        original["pending"]["attempts"] = 7
        untouched = copy.deepcopy(original)
        state = queue.reconcile(original, NOW + timedelta(weeks=4))
        expected = copy.deepcopy(original)
        expected["pending"]["period"] = "2026-10-19T10:00"
        self.assertEqual(state, expected)
        self.assertEqual(original, untouched)

    def test_card16_publication_failure_survives_future_week(self):
        state = self.blocked(self.publishing())
        state["pending"]["dispatch_requested_at"] = NOW.isoformat()
        changed = queue.reconcile(state, NOW + timedelta(weeks=4))
        self.assertEqual(changed, state)
        self.assertIsNot(changed["pending"], state["pending"])

    def test_card16_request_retry_clears_only_failure_schedule_and_reason(self):
        self.assertTrue(callable(getattr(queue, "request_retry", None)), "request_retry must exist")
        for original in (self.blocked(self.publishing()),
                         queue.failed(self.publishing(), self.generation_error("timeout"), NOW),
                         queue.failed(self.publishing(), "ordinary", NOW), self.pending()):
            original["pending"]["dispatch_requested_at"] = NOW.isoformat() if original["pending"]["phase"] == "publish" else None
            untouched = copy.deepcopy(original)
            expected = copy.deepcopy(original)
            expected["pending"].pop("generation_failure", None)
            expected["pending"].update(next_attempt_at=None, last_error=None)
            actual = queue.request_retry(original)
            self.assertEqual(actual, expected)
            self.assertEqual(original, untouched)
            self.assertIsNot(actual["pending"], original["pending"])
        with self.assertRaises(ValueError):
            queue.request_retry(copy.deepcopy(EMPTY))
        malformed = self.pending()
        malformed["pending"]["generation_failure"] = {"code": "unknown", "retryable": False}
        with self.assertRaises(ValueError):
            queue.request_retry(malformed)

if __name__ == "__main__":
    unittest.main()
