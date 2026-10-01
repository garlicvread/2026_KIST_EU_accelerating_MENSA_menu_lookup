"""매일 식단을 갱신하는 작업 큐를 JSON으로 보존하고 잠금과 재시도 조건을 제공합니다.

상태 전이 함수는 입력을 수정하지 않고 복사본을 반환합니다. 작업자는 상태 읽기·변경·
저장 동안 exclusive_lock을 유지하고, 외부 동작 전에 save_state(path, state)를
호출해야 합니다. 대기 중인 공개 작업은 완료 확인을 기록할 때까지 큐에 유지합니다.
"""

from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from mensa.errors import GenerationError
from mensa.schedule import BERLIN, due_period, validate_period


PENDING_FIELDS = frozenset(("period", "phase", "attempts", "next_attempt_at", "last_error",
                            "commit_sha", "run_id", "dispatch_requested_at"))


class BusyError(RuntimeError):
    """BusyError는 다른 프로세스가 이미 갱신 작업자의 잠금을 보유한 상황을 나타냅니다."""


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _aware(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             "An aware datetime is required")
    return value


def _period(value):
    return validate_period(value)


def _timestamp(value):
    _require(isinstance(value, str), "Timestamp must be an ISO string")
    _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def validate_state(state):
    """validate_state가 큐의 필드·주기·공개 식별 정보·재시도 조건을 검사하고, 잘못된 상태를 거부하여 대기 작업의 유실을 막습니다."""
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
    _require(isinstance(pending, dict) and set(pending) in (
        PENDING_FIELDS, PENDING_FIELDS | {"generation_failure"}), "Invalid pending refresh fields")
    _period(pending["period"])
    _require(acknowledged is None or pending["period"] > acknowledged,
             "Pending period has already been completed")
    _require(pending["phase"] in ("collect", "publish"), "Invalid refresh phase")
    _require(type(pending["attempts"]) is int and pending["attempts"] >= 0, "Invalid attempt count")
    for field in ("next_attempt_at", "dispatch_requested_at"):
        if pending[field] is not None:
            _timestamp(pending[field])
    _require(pending["last_error"] is None or isinstance(pending["last_error"], str), "Invalid last error")
    if "generation_failure" in pending:
        failure = pending["generation_failure"]
        _require(isinstance(failure, dict) and set(failure) == {"code", "retryable"},
                 "Invalid generation failure fields")
        error = GenerationError(failure["code"])
        _require(type(failure["retryable"]) is bool and failure["retryable"] == error.retryable,
                 "Invalid generation retryability")
        _require(error.retryable or pending["next_attempt_at"] is None,
                 "Permanent generation failure cannot schedule retry")
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
    """load_state가 스키마 1의 큐 JSON을 읽고 검증하며, 파일이 없으면 새 빈 큐를 반환합니다."""
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
    """save_state가 검증된 큐 상태로 파일을 원자적으로 교체하고 파일과 디렉터리를 동기화합니다."""
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
    """reconcile이 가장 최근의 갱신 대상 주기를 큐에 등록하고 아직 수집하지 않은 작업만 합칩니다.

    실패로 지정된 대기 시간이 아직 지나지 않았거나 자동 재시도가 중지되어 있으면
    수집 대상 시각만 갱신하고 대기·차단 상태는 유지합니다. 게시 중인 작업은 건드리지 않습니다.
    """
    validate_state(state)
    period = due_period(now)
    result = copy.deepcopy(state)
    if result["completed_period"] is not None and period <= result["completed_period"]:
        return result
    pending = result["pending"]
    if pending is None:
        result["pending"] = _new_pending(period)
    elif pending["phase"] == "collect" and period > pending["period"]:
        failure = pending.get("generation_failure")
        retry_at = pending["next_attempt_at"]
        waiting = retry_at is not None and datetime.fromisoformat(retry_at.replace("Z", "+00:00")) > now
        if waiting or (failure is not None and not failure["retryable"]):
            pending["period"] = period
        else:
            result["pending"] = _new_pending(period)
    return result


def failed(state, error, now):
    """failed가 대기 작업을 보존하고 시도 횟수를 늘리며 오류 유형에 맞게 재시도 조건을 기록합니다.

    GenerationError가 재시도 불가이면 다음 시도를 예약하지 않고 작업을 차단합니다.
    재시도 가능이면 지수 증가 대기 시간과 Retry-After 중 긴 시간을 적용합니다.
    일반 오류에는 지수 증가 대기 시간을 적용합니다.
    """
    validate_state(state)
    _aware(now)
    _require(state["pending"] is not None, "Cannot fail a refresh when no work is pending")
    result = copy.deepcopy(state)
    pending = result["pending"]
    pending["attempts"] += 1
    minutes = min(360, 15 * 2 ** min(pending["attempts"] - 1, 5))
    if isinstance(error, GenerationError):
        local_error = GenerationError(error.code)
        pending["generation_failure"] = {"code": local_error.code, "retryable": local_error.retryable}
        pending["last_error"] = str(local_error)
        pending["next_attempt_at"] = None
        if local_error.retryable:
            delay = max(minutes * 60, error.retry_after or 0)
            retry = now.astimezone(timezone.utc) + timedelta(seconds=delay)
            pending["next_attempt_at"] = retry.isoformat().replace("+00:00", "Z")
    else:
        pending.pop("generation_failure", None)
        retry = now.astimezone(timezone.utc) + timedelta(minutes=minutes)
        pending["next_attempt_at"] = retry.isoformat(timespec="seconds").replace("+00:00", "Z")
        pending["last_error"] = str(error)[:2000]
    return result


def request_retry(state):
    """request_retry가 대기 작업과 누적 시도 횟수는 유지하면서 오류·재시도 차단 조건을 명시적으로 해제합니다."""
    validate_state(state)
    _require(state["pending"] is not None, "Cannot retry a refresh when no work is pending")
    result = copy.deepcopy(state)
    pending = result["pending"]
    pending.pop("generation_failure", None)
    pending.update(next_attempt_at=None, last_error=None)
    return result


def completed(state):
    """completed가 대기 주기를 completed_period에 기록하고 pending을 비우며, 반복 호출은 추가 효과가 없습니다."""
    validate_state(state)
    result = copy.deepcopy(state)
    if result["pending"] is not None:
        result["completed_period"] = result["pending"]["period"]
        result["pending"] = None
    return result


@contextmanager
def exclusive_lock(path):
    """exclusive_lock이 잠금 파일의 inode를 제거하거나 대기하지 않고 프로세스 간 권고 잠금을 유지합니다."""
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
