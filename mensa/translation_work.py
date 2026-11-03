"""수집 큐와 분리된 유한 번역 상태·재시도·게시 복구를 제공합니다."""

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from mensa.errors import GenerationError
from mensa.queue import exclusive_lock, failed, reconcile


MAX_STATE_BYTES = 4096
_DERIVED_FAILURES = frozenset({'invalid_json', 'invalid_envelope', 'incomplete_generation',
                               'invalid_result', 'response_too_large'})
_FIELDS = {'schema_version', 'target_release_id', 'prepared_release_id', 'previous_release_id',
           'attempts', 'next_attempt_at', 'last_error', 'generation_failure'}


def _empty():
    return dict(schema_version=1, target_release_id=None, prepared_release_id=None,
                previous_release_id=None, attempts=0, next_attempt_at=None,
                last_error=None, generation_failure=None)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Invalid translation retry timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Translation retry timestamp must be aware')


def validate_translation_state(value):
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('Invalid translation work state')
    for name in ('target_release_id', 'prepared_release_id', 'previous_release_id'):
        identifier = value[name]
        if identifier is not None and (not isinstance(identifier, str)
                or re.fullmatch(r'[0-9a-f]{64}', identifier) is None):
            raise ValueError('Invalid translation release identity')
    if value['prepared_release_id'] is not None and value['target_release_id'] is None:
        raise ValueError('Prepared translation has no target')
    if value['prepared_release_id'] is None and value['previous_release_id'] is not None:
        raise ValueError('Previous translation release has no preparation')
    if type(value['attempts']) is not int or not 0 <= value['attempts'] <= 2 ** 31 - 1:
        raise ValueError('Invalid translation attempt count')
    if value['next_attempt_at'] is not None:
        _timestamp(value['next_attempt_at'])
    if value['last_error'] is not None and (not isinstance(value['last_error'], str)
            or len(value['last_error']) > 500):
        raise ValueError('Invalid translation error')
    failure = value['generation_failure']
    if failure is not None:
        if not isinstance(failure, dict) or set(failure) != {'code', 'retryable'}:
            raise ValueError('Invalid translation failure')
        error = GenerationError(failure['code'])
        if type(failure['retryable']) is not bool or failure['retryable'] != error.retryable:
            raise ValueError('Invalid translation retry policy')
        if not error.retryable and value['next_attempt_at'] is not None:
            raise ValueError('Permanent translation failure cannot schedule retry')


def _regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('Translation work state must be a private regular file')
    if info.st_size > MAX_STATE_BYTES:
        raise ValueError('Translation work state exceeds its byte limit')
    return True


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate translation work state key')
        result[key] = value
    return result


def load_translation_state(path):
    path = Path(path)
    if not _regular(path):
        return _empty()
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
    validate_translation_state(value)
    return value


def save_translation_state(path, value):
    path = Path(path)
    validate_translation_state(value)
    _regular(path)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError('Translation work state exceeds its byte limit')
    descriptor, temporary = tempfile.mkstemp(prefix='.translation-work-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def translation_failed(state, error, now):
    """기존 큐의 순수 실패 정책을 재사용하며 안전한 오류만 저장합니다."""
    validate_translation_state(state)
    queue = reconcile({'schema_version': 1, 'completed_period': None, 'pending': None}, now)
    queue['pending']['attempts'] = min(state['attempts'], 2 ** 31 - 2)
    # 잘못된 파생 결과는 다음 시도에서 다시 생성합니다. 인증·설정 오류의 수동 재시도는 유지합니다.
    if not isinstance(error, GenerationError) or error.code in _DERIVED_FAILURES:
        error = 'Translation work could not finish'
    pending = failed(queue, error, now)['pending']
    result = copy.deepcopy(state)
    for name in ('attempts', 'next_attempt_at', 'last_error'):
        result[name] = pending[name]
    result['generation_failure'] = pending.get('generation_failure')
    return result


def adopt_legacy_failure(directory, pending):
    path = directory / 'translation-work.json'
    state = load_translation_state(path)
    if state['generation_failure'] is None and state['attempts'] == 0:
        failure = copy.deepcopy(pending['generation_failure'])
        state.update(attempts=min(pending['attempts'], 2 ** 31 - 1),
                     next_attempt_at=pending['next_attempt_at'], last_error=pending['last_error'],
                     generation_failure=failure)
        if failure['code'] in _DERIVED_FAILURES:
            state.update(generation_failure=None, next_attempt_at=None,
                         last_error='Translation work could not finish')
        save_translation_state(path, state)


def retry_translations(directory):
    """같은 작업자 잠금 아래 번역 대기만 해제하고 시도 이력은 보존합니다."""
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('State directory must be a private directory')
    lock = directory / 'worker.lock'
    if lock.exists() or lock.is_symlink():
        if not stat.S_ISREG(lock.lstat().st_mode):
            raise ValueError('worker.lock must be a regular file')
    with exclusive_lock(lock):
        path = directory / 'translation-work.json'
        state = load_translation_state(path)
        state.update(next_attempt_at=None, last_error=None, generation_failure=None)
        save_translation_state(path, state)
        return state


def available_translations(config, menu, published, phrases, glossary, *, previous, observed, today):
    """원본 관찰 여부를 명시하여 모델 없이 공개·비공개 번역을 병합하고 정책을 적용합니다."""
    from mensa.retention_runtime import RetainedTranslationStore
    from mensa.translation_service import TranslationService
    store = RetainedTranslationStore(config, menu=menu, observed=observed, today=today)
    history = store.merge(published, published_menu=previous, glossary=glossary)
    service = TranslationService(glossary_provider=lambda: copy.deepcopy(glossary),
                                 translate=lambda *args: (_ for _ in ()).throw(
                                     AssertionError('Source collection cannot generate translations')))
    return store, store.save(service.build(menu, history, phrases, None))


def _identity(pair):
    from mensa.releases import _json_bytes, _release_id
    return None if pair is None else _release_id(*(_json_bytes(value) for value in pair))


def _needs_generation(menu, cache, settings, glossary, *, strict_identity):
    from mensa.publication import translation_gaps
    from mensa.translation_contract import generation_identity, reusable_translation
    gaps = translation_gaps(menu, cache)
    if gaps['entries'] or gaps['notices']:
        return True
    identity = (generation_identity({'provider': settings.provider, 'model': settings.model,
                                    'backend_revision': settings.revision}, notice_glossary=glossary)
                if strict_identity else None)
    return any(not reusable_translation(cache['entries'][meal['translation_key']],
                                        settings.model, identity=identity)
               for day in menu['days'] for meal in day['meals'])


def run_translation_work(config, now, *, resources, current, load_prepared, publish,
                         confirm, generate, glossary, phrases, source_pending=False, strict_identity=True):
    """최신 공개 메뉴의 번역만 재개하고 별도 게시 준비본을 최신 공개 ID에 결합합니다."""
    from mensa.publication import validate_publication
    from mensa.retention import public_translations, retain_menu
    from mensa.schedule import BERLIN
    path = config.paths.state_dir / 'translation-work.json'
    state = load_translation_state(path)
    if source_pending:
        return {'status': 'deferred', 'reason': 'Source publication is unresolved'}

    def paused():
        failure = state['generation_failure']
        if failure is not None and not failure['retryable']:
            return {'status': 'blocked', 'failure_code': failure['code'],
                    'retryable': False, 'reason': state['last_error']}
        retry = state['next_attempt_at']
        if retry is not None and datetime.fromisoformat(retry.replace('Z', '+00:00')) > now:
            return {'status': 'waiting', 'retry_at': retry}
        return None

    waiting = paused()
    if waiting is not None:
        return waiting

    def fail(error):
        nonlocal state
        state = translation_failed(state, error, now)
        save_translation_state(path, state)
        failure = state['generation_failure']
        result = {'status': 'blocked' if failure is not None and not failure['retryable'] else 'failed',
                  'reason': state['last_error']}
        if failure is not None:
            result.update(failure_code=failure['code'], retryable=failure['retryable'])
        return result
    try:
        pair = current()
    except Exception as error:
        return fail(error)
    if pair is None:
        return {'status': 'idle'}
    today = now.astimezone(BERLIN).date().isoformat()
    current_id = _identity(pair)
    recovered = False

    def clear_prepared():
        state.update(prepared_release_id=None, previous_release_id=None)

    def success(release_id):
        clear_prepared()
        state.update(target_release_id=release_id, next_attempt_at=None,
                     last_error=None, generation_failure=None)
        save_translation_state(path, state)

    try:
        if state['prepared_release_id'] is not None:
            if current_id in (state['previous_release_id'], state['prepared_release_id']):
                try:
                    prepared = load_prepared(state['prepared_release_id'])
                except (FileNotFoundError, ValueError):
                    # 본문은 공개본·비공개 캐시에서 다시 만들며 사라진 로컬 준비본을 고집하지 않습니다.
                    prepared = None
                if (prepared is not None and prepared[0]['coverage']['end'] >= today
                        and retain_menu(prepared[0], today, config.retention) == prepared[0]
                        and public_translations(*prepared) == prepared[1]):
                    validate_publication(*prepared, notice_glossary=glossary, today=today)
                    if current_id != state['prepared_release_id']:
                        waiting = paused()
                        if waiting is not None:
                            return waiting
                        publish(prepared, state['prepared_release_id'], state['previous_release_id'], today)
                    else:
                        confirm(prepared, state['prepared_release_id'], state['previous_release_id'], today)
                    current_id = state['prepared_release_id']
                    pair = prepared
                    recovered = True
                    success(current_id)
            clear_prepared()
        state['target_release_id'] = current_id
        save_translation_state(path, state)
        menu, published = pair
        if menu['coverage']['end'] < today:
            return {'status': 'deferred', 'reason': 'Published menu has expired'}
        store, candidate = available_translations(config, menu, published, phrases, glossary,
            previous=menu, observed={'days': []}, today=today)
        needed = _needs_generation(menu, candidate, config.inference, glossary, strict_identity=strict_identity)
        if needed:
            waiting = paused()
            if waiting is not None:
                return waiting
            ready, reason = resources()
            if not ready:
                return {'status': 'deferred', 'reason': reason}
            candidate = store.save(generate(menu, candidate, phrases, store, glossary))
        candidate = public_translations(menu, candidate)
        validate_publication(menu, candidate, notice_glossary=glossary, today=today)
        improved = (menu, candidate)
        release_id = _identity(improved)
        if release_id != current_id:
            # 원격 교정이나 새 수집이 실행 중 나타나면 과거 메뉴를 게시하지 않습니다.
            if _identity(current()) != current_id:
                clear_prepared()
                save_translation_state(path, state)
                return {'status': 'deferred', 'reason': 'Published data changed; translation work will rebase'}
            state.update(prepared_release_id=release_id, previous_release_id=current_id)
            save_translation_state(path, state)
            publish(improved, release_id, current_id, today)
            current_id = release_id
        if _needs_generation(menu, candidate, config.inference, glossary, strict_identity=strict_identity):
            clear_prepared()
            state['target_release_id'] = current_id
            raise ValueError('Translation work is incomplete')
        success(current_id)
        return {'status': 'completed' if recovered or needed or release_id != _identity(pair) else 'idle',
                'release_id': current_id}
    except Exception as error:
        return fail(error)
