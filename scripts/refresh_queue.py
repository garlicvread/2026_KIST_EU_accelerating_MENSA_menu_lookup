"""Durable weekly refresh state and nonblocking local execution gates.

All transitions return copies. The worker holds ``exclusive_lock`` across its
read/transition/save cycle and calls ``save_state(path, state)`` before each
external side effect. A pending publication remains intact until acknowledged.
"""

from contextlib import contextmanager
import copy
from datetime import date, datetime, time, timedelta, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
MIN_AVAILABLE_BYTES = 32 * 1024 ** 3
PENDING_FIELDS = frozenset(("period", "phase", "attempts", "next_attempt_at", "last_error",
                            "commit_sha", "run_id", "dispatch_requested_at"))


class BusyError(RuntimeError):
    """Another process already holds the refresh worker lock."""


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _aware(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             "An aware datetime is required")
    return value


def _period(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
             "Period must be an ISO Monday date")
    parsed = date.fromisoformat(value)
    _require(parsed.weekday() == 0, "Period must be a Monday")
    return value


def _timestamp(value):
    _require(isinstance(value, str), "Timestamp must be an ISO string")
    _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def due_period(now):
    """Return the latest Monday whose 09:17 Europe/Berlin deadline has arrived."""
    local = _aware(now).astimezone(BERLIN)
    monday = local.date() - timedelta(days=local.weekday())
    deadline = datetime.combine(monday, time(9, 17), tzinfo=BERLIN)
    if local < deadline:
        monday -= timedelta(days=7)
    return monday.isoformat()


def validate_state(state):
    """Reject malformed state instead of silently dropping queued work."""
    _require(isinstance(state, dict) and set(state) == {"schema_version", "completed_period", "pending"},
             "Invalid refresh state fields")
    _require(type(state["schema_version"]) is int and state["schema_version"] == 1,
             "Unsupported refresh state schema")
    acknowledged = state["completed_period"]
    if acknowledged is not None:
        _period(acknowledged)
    pending = state["pending"]
    if pending is None:
        return
    _require(isinstance(pending, dict) and set(pending) == PENDING_FIELDS, "Invalid pending refresh fields")
    _period(pending["period"])
    _require(acknowledged is None or pending["period"] > acknowledged,
             "Pending period has already been completed")
    _require(pending["phase"] in ("collect", "publish"), "Invalid refresh phase")
    _require(type(pending["attempts"]) is int and pending["attempts"] >= 0, "Invalid attempt count")
    for field in ("next_attempt_at", "dispatch_requested_at"):
        if pending[field] is not None:
            _timestamp(pending[field])
    _require(pending["last_error"] is None or isinstance(pending["last_error"], str), "Invalid last error")
    sha = pending["commit_sha"]
    _require(sha is None or (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)),
             "Invalid publication commit SHA")
    run_id = pending["run_id"]
    _require(run_id is None or (type(run_id) is int and run_id > 0), "Invalid publication run ID")
    if pending["phase"] == "collect":
        _require(sha is None and run_id is None and pending["dispatch_requested_at"] is None,
                 "Collection phase cannot contain publication identifiers")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate state key: {key}")
        result[key] = value
    return result


def load_state(path):
    """Read schema 1 state; a missing file represents a new empty queue."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            state = json.load(stream, object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return {"schema_version": 1, "completed_period": None, "pending": None}
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid refresh state at {path}: {exc}") from exc
    validate_state(state)
    return state


def save_state(path, state):
    """Atomically replace validated state, syncing both file and directory."""
    validate_state(state)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _new_pending(period):
    return {"period": period, "phase": "collect", "attempts": 0,
            "next_attempt_at": None, "last_error": None, "commit_sha": None,
            "run_id": None, "dispatch_requested_at": None}


def reconcile(state, now):
    """Queue the latest due week, coalescing only uncollected work."""
    validate_state(state)
    period = due_period(now)
    result = copy.deepcopy(state)
    if result["completed_period"] is not None and period <= result["completed_period"]:
        return result
    pending = result["pending"]
    if pending is None or (pending["phase"] == "collect" and period > pending["period"]):
        result["pending"] = _new_pending(period)
    return result


def failed(state, error, now):
    """Retain the job and defer retry by 15 minutes, doubling up to six hours."""
    validate_state(state)
    _aware(now)
    _require(state["pending"] is not None, "Cannot fail a refresh when no work is pending")
    result = copy.deepcopy(state)
    pending = result["pending"]
    pending["attempts"] += 1
    minutes = min(360, 15 * 2 ** min(pending["attempts"] - 1, 5))
    retry = now.astimezone(timezone.utc) + timedelta(minutes=minutes)
    pending["next_attempt_at"] = retry.isoformat(timespec="seconds").replace("+00:00", "Z")
    pending["last_error"] = str(error)[:2000]
    return result


def completed(state):
    """Acknowledge the pending period; repeated acknowledgement is harmless."""
    validate_state(state)
    result = copy.deepcopy(state)
    if result["pending"] is not None:
        result["completed_period"] = result["pending"]["period"]
        result["pending"] = None
    return result


@contextmanager
def exclusive_lock(path):
    """Hold an advisory process lock without waiting or removing its inode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BusyError(f"Another refresh worker holds {path}") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _command_output(command):
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    return result.stdout


def resource_status():
    """Return a macOS readiness decision immediately, without sleeping.

    Available memory means free + inactive + speculative vm_stat pages. A Mac
    with no battery is allowed; a reported battery must explicitly use AC power.
    Unavailable memory/load/power measurements defer work until a later wakeup.
    """
    try:
        vm = _command_output(["/usr/bin/vm_stat"])
        page_size = re.search(r"page size of (\d+) bytes", vm)
        _require(page_size is not None and int(page_size.group(1)) > 0, "Missing memory page size")
        pages = []
        for kind in ("free", "inactive", "speculative"):
            matches = re.findall(rf"^Pages {kind}:\s+(\d+)\.\s*$", vm, re.MULTILINE)
            _require(len(matches) == 1, f"Missing memory counter: {kind}")
            pages.append(int(matches[0]))
        available = sum(pages) * int(page_size.group(1))
        if available < MIN_AVAILABLE_BYTES:
            return False, f"Available memory {available / 1024 ** 3:.1f} GiB is below {MIN_AVAILABLE_BYTES / 1024 ** 3:g} GiB"
        load = os.getloadavg()[0]
        _require(math.isfinite(load) and load >= 0, "Invalid CPU load measurement")
        threshold = max(2, (os.cpu_count() or 1) * 0.6)
        if load > threshold:
            return False, f"CPU load {load:.2f} exceeds {threshold:.2f}"
        power = _command_output(["/usr/bin/pmset", "-g", "batt"])
        on_ac = bool(re.search(r"\bAC Power\b", power, re.IGNORECASE))
        on_battery = bool(re.search(r"\bBattery Power\b", power, re.IGNORECASE))
        has_battery = bool(re.search(r"InternalBattery|\bdischarging\b|\bcharging\b", power, re.IGNORECASE))
        if on_battery or (has_battery and not on_ac):
            return False, "Battery power: waiting for AC power"
        return True, f"Ready: {available / 1024 ** 3:.1f} GiB available, CPU load {load:.2f}/{threshold:.2f}, AC or no battery"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"Resource measurement unavailable: {exc}"
