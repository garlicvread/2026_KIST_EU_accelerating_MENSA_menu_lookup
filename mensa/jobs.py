"""JobRunner가 비공개 상태 파일을 준비하고 큐 작업을 실행하며 last-result.json에 작업자 결과를 기록합니다.

호출자는 이미 존재하는 상위 디렉터리와 신뢰할 수 있는 비공개 상태 경로를 전달합니다.
JobRunner는 상태 디렉터리의 유형·비공개 권한과 기존 상태 파일의 유형을 검사하지만,
악의적인 동시 변경에 대한 안전성을 보장하지 않습니다. 대기하지 않는 큐 잠금은 작업 실행부터 최종 결과
파일 교체까지 유지됩니다.
"""

from datetime import datetime, timedelta
import copy
import inspect
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from mensa.errors import GenerationError
from mensa.queue import (load_state, save_state, reconcile, failed, completed,
                         exclusive_lock, BusyError)


def record_result(directory, result, now):
    """record_result가 작업자의 실행 결과와 시각을 last-result.json에 교체 기록합니다.

    호출자는 worker.lock을 보유해야 하며, JobRunner.run이 이 함수의 호출 동안 잠금을
    유지합니다. record_result 자체는 잠금을 얻거나 유지하지 않습니다.
    """
    target = directory / 'last-result.json'
    descriptor, name = tempfile.mkstemp(prefix='.last-result-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'at': now.isoformat(), **result}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return result

class JobRunner:
    """JobRunner가 전달된 자원 검사·작업·시계 함수를 사용하여 대기 중인 큐 작업 하나를 실행하고 상태를 저장합니다."""

    def __init__(self, state_dir: Path, *, resources, work, clock=None):
        if not isinstance(state_dir, Path) or not state_dir.is_absolute():
            raise ValueError('state_dir must be an explicit absolute Path')
        if not callable(resources) or not callable(work) or (clock is not None and not callable(clock)):
            raise ValueError('resources, work and an optional clock must be callable')
        self._state_dir = state_dir
        self._resources = resources
        self._work = work
        self._clock = clock
        def declared(name, default):
            try:
                inspect.getattr_static(work, name)
            except AttributeError:
                return default
            return getattr(work, name)
        self._source_needs_model_resources = declared('source_needs_model_resources', True)
        if type(self._source_needs_model_resources) is not bool:
            raise TypeError('source_needs_model_resources must be boolean')
        self._translation_work = declared('translation_work', None)
        if self._translation_work is not None and not callable(self._translation_work):
            raise TypeError('translation_work must be callable')

    def _prepare_state(self):
        directory = self._state_dir
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                # 다른 작업자의 첫 실행이 잠금을 얻기 전에 같은 상태 디렉터리를 생성했을 수 있습니다.
                pass
            mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise ValueError('State path must be a directory, not a symlink')
        if stat.S_IMODE(mode) & 0o077:
            raise ValueError('State directory must have private permissions')
        for name in ('queue.json', 'worker.lock', 'last-result.json', 'translation-work.json'):
            try:
                mode = (directory / name).lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f'{name} must be a regular file, not a symlink')

    def run(self, now):
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('now must be an aware datetime')
        self._prepare_state()
        directory = self._state_dir
        clock = time.monotonic if self._clock is None else self._clock
        path = directory / 'queue.json'
        try:
            with exclusive_lock(directory / 'worker.lock'):
                started = clock()
                def finish(result):
                    return record_result(directory, result, now + timedelta(seconds=clock() - started))
                maintain = getattr(self._work, 'maintain', None)
                if callable(maintain):
                    try:
                        maintain(now)
                    except Exception as exc:
                        return finish({'status': 'failed', 'reason': str(exc)[:500]})
                state = load_state(path)
                pending = state['pending']
                if (not self._source_needs_model_resources and pending is not None
                        and pending['phase'] == 'collect' and 'generation_failure' in pending):
                    adopt = getattr(self._work, 'adopt_translation_failure', None)
                    if not callable(adopt):
                        raise ValueError('Source-only work must preserve legacy translation failure')
                    adopt(copy.deepcopy(pending), now)
                    pending.pop('generation_failure')
                    pending.update(next_attempt_at=None, last_error=None)
                state = reconcile(state, now)
                save_state(path, state)
                result = self._collect(path, state, now, lambda: now + timedelta(seconds=clock() - started))
                # 상태·유지보수 검사를 통과한 경로에서만 수집 뒤의 번역 작업을 실행합니다.
                if self._translation_work is not None:
                    try:
                        result['translations'] = self._translation_work(
                            now + timedelta(seconds=clock() - started), resources=self._resources,
                            source_state=load_state(path))
                    except GenerationError as error:
                        result['translations'] = {'status': 'failed' if error.retryable else 'blocked',
                                                  'failure_code': error.code, 'reason': str(error)}
                    except Exception:
                        result['translations'] = {'status': 'failed', 'reason': 'Translation work could not run'}
                return finish(result)
        except BusyError:
            return {'status': 'busy', 'reason': 'Another refresh worker holds the queue lock'}

    def _collect(self, path, state, now, failed_at):
        pending = state['pending']
        if not pending:
            return {'status': 'idle', 'completed_period': state['completed_period']}
        failure = pending.get('generation_failure')
        if failure is not None and not failure['retryable']:
            return {'status': 'blocked', 'reason': str(GenerationError(failure['code'])),
                    'failure_code': failure['code'], 'retryable': False, 'period': pending['period']}
        retry = pending['next_attempt_at']
        if retry and datetime.fromisoformat(retry) > now:
            return {'status': 'waiting', 'retry_at': retry}
        if self._source_needs_model_resources:
            ready, reason = self._resources()
            if not ready:
                return {'status': 'deferred', 'reason': reason, 'period': pending['period']}
        try:
            self._work(state)
        except Exception as error:
            typed = isinstance(error, GenerationError)
            state = failed(load_state(path), error if typed else str(error)[:500], failed_at())
            save_state(path, state)
            result = {'status': 'failed', 'reason': str(error)[:500], 'period': state['pending']['period']}
            if typed:
                failure = state['pending']['generation_failure']
                result.update(status='failed' if failure['retryable'] else 'blocked',
                              reason=state['pending']['last_error'], failure_code=failure['code'],
                              retryable=failure['retryable'])
            return result
        state = completed(load_state(path))
        save_state(path, state)
        return {'status': 'completed', 'period': state['completed_period']}
