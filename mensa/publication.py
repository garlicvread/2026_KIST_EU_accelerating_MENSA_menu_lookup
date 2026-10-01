"""validate_publication이 준비된 메뉴 JSON과 번역 캐시가 함께 공개할 조건을 만족하는지 부작용 없이 검사합니다."""

from datetime import date

from mensa.menu_contract import validate_menu
from mensa.notice_contract import source_notices, validate_notice_translations
from mensa.translation_contract import source_for, validate_cache


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
    """validate_publication이 메뉴의 모든 번역과 안내 문구를 검사하고, 선택적으로 전달된 날짜를 기준으로 메뉴 유효 기간을 확인합니다.

    과거 캐시 항목도 허용하지만 함께 검증합니다. notice_glossary는 독일어 안내 문구를
    en/ko로 연결하는 명시적인 매핑입니다. today를 생략하면 날짜 최신성은 검사하지
    않습니다. 이 함수는 현재 시각을 조회하거나 누락된 번역을 생성하지 않습니다.
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
                raise ValueError(f"Missing meal translation: {context}")
            if entry["source"] != source_for(meal):
                raise ValueError(f"Meal translation source mismatch: {context}")

    notices = cache.get("notices", {})
    for label in sorted(source_notices(menu)):
        if label not in notice_glossary:
            raise ValueError(f"Unknown notice label in reviewed glossary: {label!r}")
        if label not in notices:
            raise ValueError(f"Missing notice translation: {label!r}")
        if notices[label] != notice_glossary[label]:
            raise ValueError(f"Notice translation differs from reviewed glossary: {label!r}")

    if today is not None:
        supplied_today = _supplied_date(today)
        end = menu["coverage"]["end"]
        if date.fromisoformat(end) < supplied_today:
            raise ValueError(f"Stale menu: coverage ends {end}, before today {supplied_today}")
