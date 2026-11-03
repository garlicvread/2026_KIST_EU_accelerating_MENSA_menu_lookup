"""validate_publication이 준비된 메뉴 JSON과 번역 캐시가 함께 공개할 조건을 만족하는지 부작용 없이 검사합니다."""

from datetime import date

from mensa.menu_contract import validate_menu
from mensa.notice_contract import source_notices, validate_notice_translations
from mensa.translation_contract import source_for, validate_cache, recover_cache


def _checked(label, validator, *args, **kwargs):
    """_checked가 검증 함수를 호출하고, 잘못된 입력 오류에 메뉴·번역 캐시·용어집 중 어느 입력의 오류인지 덧붙입니다."""
    try:
        return validator(*args, **kwargs)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError,
            OverflowError, RecursionError) as exc:
        raise ValueError(f"Invalid {label}: {exc}") from exc


def _supplied_date(today):
    if type(today) is date:
        return today
    if isinstance(today, str):
        try:
            parsed = date.fromisoformat(today)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == today:
                return parsed
    raise ValueError("Invalid today: expected a YYYY-MM-DD string or datetime.date")


def validate_publication(menu, cache, *, notice_glossary, previous=None, today=None):
    """validate_publication이 메뉴와 존재하는 번역을 검사하고, 선택적으로 전달된 날짜를 기준으로 메뉴 유효 기간을 확인합니다.

    과거 캐시 항목도 허용하지만 함께 검증합니다. notice_glossary는 독일어 안내 문구를
    en/ko로 연결하는 명시적인 매핑입니다. 없는 요리·안내 번역은 독일어 원문으로
    표시할 수 있지만, 존재하는 번역은 두 언어와 원문 연결이 유효해야 합니다.
    today를 생략하면 날짜 최신성은 검사하지 않습니다. 이 함수는 현재 시각을
    조회하거나 누락된 번역을 생성하지 않습니다.
    """
    _checked("menu", validate_menu, menu, previous=previous)
    _checked("translation cache", validate_cache, cache)
    _checked("notice glossary", validate_notice_translations, notice_glossary)

    for day in menu["days"]:
        for meal in day["meals"]:
            key = meal["translation_key"]
            entry = cache["entries"].get(key)
            context = f"{key} ({day['date']}, {meal['name_de']})"
            if entry is None:
                continue
            if entry["source"] != source_for(meal):
                raise ValueError(f"Meal translation source mismatch: {context}")

    if today is not None:
        supplied_today = _supplied_date(today)
        end = menu["coverage"]["end"]
        if date.fromisoformat(end) < supplied_today:
            raise ValueError(f"Stale menu: coverage ends {end}, before today {supplied_today}")


def translation_gaps(menu, cache):
    """translation_gaps가 검증된 메뉴에서 빠진 요리 키·안내 라벨을 중복 없이 정렬하여 반환합니다.

    손상된 개별 파생 번역도 재생성이 필요한 누락으로 간주합니다.
    이 함수는 입력을 바꾸거나 원본·모델·현재 시각에 접근하지 않습니다.
    """
    cache = _checked("translation cache", recover_cache, cache)
    keys = {meal['translation_key'] for day in menu['days'] for meal in day['meals']}
    return {
        'entries': tuple(sorted(keys - cache['entries'].keys())),
        'notices': tuple(sorted(source_notices(menu) - cache.get('notices', {}).keys())),
    }
