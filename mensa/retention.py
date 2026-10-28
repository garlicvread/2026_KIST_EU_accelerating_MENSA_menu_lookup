"""보관 함수가 주입한 날짜·정책으로 공개 범위와 비공개 재사용 캐시를 제한하며 파일이나 현재 시각을 읽지 않습니다."""

from copy import deepcopy
from datetime import date, timedelta

from mensa.notice_contract import source_notices
from mensa.releases import _json_bytes, _release_id
from mensa.translation_contract import validate_cache


_ENTRY_FIELDS = frozenset({
    'source', 'en', 'ko', 'origin', 'model', 'prompt_version',
    'generation_identity', 'name_policy_version', 'component_policy_version',
})
_PINNED_ORIGINS = frozenset({'editorial-draft', 'reviewed-draft'})


def _date(value, label):
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == value:
                return parsed
    raise ValueError(f'Invalid {label}: expected a YYYY-MM-DD string or datetime.date')


def _limit(policy, name):
    value = getattr(policy, name, None)
    if type(value) is not int or value <= 0:
        raise ValueError(f'Invalid retention policy: {name} must be a positive integer')
    return value


def retain_menu(menu, today, policy):
    """retain_menu가 지난 history_days 경계부터 미래 원본 날짜까지 독립 복사본으로 보존합니다.

    호출자는 정리 전에 원본의 구조·최신 날짜·이전 가격 연결을 검증해야 합니다.
    이 함수는 source·식단·가격·번역 key를 변경하지 않으며 coverage만 다시 계산합니다.
    """
    cutoff = _date(today, 'today') - timedelta(days=_limit(policy, 'history_days'))
    result = deepcopy(menu)
    result['days'] = [day for day in result['days'] if _date(day['date'], 'menu date') >= cutoff]
    if not result['days']:
        raise ValueError('No retained menu days remain within the retention period')
    result['coverage'] = {'start': result['days'][0]['date'], 'end': result['days'][-1]['date']}
    return result


def public_translations(menu, cache):
    """public_translations가 메뉴가 실제 참조하는 번역·안내만 공개 스키마로 복사하며 누락된 참조를 거부합니다."""
    keys = {meal['translation_key'] for day in menu['days'] for meal in day['meals']}
    labels = source_notices(menu)
    _require_references(cache, keys, labels)
    result = {
        'schema_version': cache['schema_version'],
        'entries': {key: {field: deepcopy(value) for field, value in cache['entries'][key].items()
                          if field in _ENTRY_FIELDS} for key in sorted(keys)},
    }
    if 'notices' in cache or labels:
        result['notices'] = {label: deepcopy(cache['notices'][label]) for label in sorted(labels)}
    return result


def _require_references(cache, keys, labels):
    missing_keys = set(keys) - cache['entries'].keys()
    missing_labels = set(labels) - cache.get('notices', {}).keys()
    if missing_keys or missing_labels:
        raise ValueError('Missing required translation cache references: '
                         f'{len(missing_keys)} entries, {len(missing_labels)} notices')


def _usage_records(usage, cache, today):
    if usage is not None:
        if (not isinstance(usage, dict) or set(usage) != {'schema_version', 'entries', 'notices'} or
                type(usage['schema_version']) is not int or usage['schema_version'] != 1):
            raise ValueError('Invalid cache usage schema')
        for kind in ('entries', 'notices'):
            if not isinstance(usage[kind], dict):
                raise ValueError('Invalid cache usage mapping')
            for key, record in usage[kind].items():
                if (not isinstance(key, str) or not isinstance(record, dict) or
                        set(record) != {'last_used', 'pinned'} or type(record['pinned']) is not bool or
                        not isinstance(record['last_used'], str)):
                    raise ValueError('Invalid cache usage record')
                _date(record['last_used'], 'cache usage last_used')
    result = {'schema_version': 1, 'entries': {}, 'notices': {}}
    for kind in ('entries', 'notices'):
        for key, value in cache.get(kind, {}).items():
            existing = None if usage is None else usage[kind].get(key)
            record = ({'last_used': today.isoformat(), 'pinned': False}
                      if existing is None else deepcopy(existing))
            if kind == 'entries' and value.get('origin') in _PINNED_ORIGINS:
                record['pinned'] = True
            result[kind][key] = record
    return result


def _state_size(cache, usage):
    return len(_json_bytes({'schema_version': 1, 'cache': cache, 'usage': usage}))


def _removed_bytes(mapping, key):
    # 정규 JSON 객체에서 한 쌍을 제거할 때 쉼표 한 개도 사라지며 마지막 쌍에는 쉼표가 없습니다.
    return len(_json_bytes(key)) + 1 + len(_json_bytes(mapping[key])) + (len(mapping) > 1)


def prune_cache(cache, usage, *, required_keys, required_notices, observed_keys,
                observed_notices, today, policy):
    """prune_cache가 참조·고정 항목을 보호하고 TTL 다음 LRU로 유한 비공개 캐시·사용 기록을 반환합니다.

    사용 기록이 없는 항목은 한 번만 오늘로 초기화합니다. observed 집합만 새 원본에서
    실제 사용한 것으로 간주하며 보존된 지난 메뉴의 required 집합은 날짜를 갱신하지
    않습니다. 항목·안내 개수와 정규 translation-state.json 전체 바이트를 함께 제한합니다.
    보호 자료만으로 한도를 넘으면 입력을 수정하지 않고 실패합니다.
    """
    current = _date(today, 'today')
    cutoff = current - timedelta(days=_limit(policy, 'cache_unused_days'))
    max_entries = _limit(policy, 'cache_max_entries')
    max_bytes = _limit(policy, 'cache_max_bytes')
    validate_cache(cache, allow_legacy_names=True)
    if type(cache['schema_version']) is not int:
        raise ValueError('Invalid translation cache schema version')
    required = {'entries': set(required_keys), 'notices': set(required_notices)}
    observed = {'entries': set(observed_keys), 'notices': set(observed_notices)}
    _require_references(cache, required['entries'], required['notices'])
    result = deepcopy(cache)
    used = _usage_records(usage, result, current)
    candidates = []
    for kind in ('entries', 'notices'):
        for key, record in list(used[kind].items()):
            if key in observed[kind]:
                record['last_used'] = current.isoformat()
            if key in required[kind] or record['pinned']:
                continue
            if _date(record['last_used'], 'cache usage last_used') < cutoff:
                del result[kind][key]
                del used[kind][key]
            else:
                candidates.append((record['last_used'], kind, key))

    count = sum(len(result.get(kind, {})) for kind in ('entries', 'notices'))
    size = _state_size(result, used)
    for _, kind, key in sorted(candidates):
        if count <= max_entries and size <= max_bytes:
            break
        size -= _removed_bytes(result[kind], key) + _removed_bytes(used[kind], key)
        del result[kind][key]
        del used[kind][key]
        count -= 1
    if count > max_entries or size > max_bytes:
        raise ValueError('Protected translation cache exceeds retention count or byte limits')
    return result, used


def check_bundle_size(menu, cache, policy):
    """check_bundle_size가 정규 Drive 공개 envelope의 바이트를 반환하며 상한 초과는 자료를 자르지 않고 거부합니다."""
    menu_bytes, cache_bytes = _json_bytes(menu), _json_bytes(cache)
    envelope = {'schema_version': 1, 'release_id': _release_id(menu_bytes, cache_bytes),
                'menu': menu, 'translations': cache}
    size = len(_json_bytes(envelope))
    if size > _limit(policy, 'max_bundle_bytes'):
        raise ValueError('Public bundle exceeds retention.max_bundle_bytes')
    return size
