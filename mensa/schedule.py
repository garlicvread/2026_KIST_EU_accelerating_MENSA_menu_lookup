"""식단 수집 시간과 작업 식별값을 큐 및 Git 복구 기록에 공통으로 제공합니다.

작업 식별값은 독일 현지 날짜와 예정 시각을 묶습니다. 예전 날짜만 있는 기록도
읽을 수 있도록 허용하지만, 새 작업은 09:00·10:00·11:00 중 하나를 기록합니다.
이 모듈은 파일을 읽거나 현재 시각을 조회하지 않습니다.
"""

from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
REFRESH_HOURS = (9, 10, 11)


def validate_period(value):
    """큐와 Git 기록의 작업 날짜·예정 시각을 검사하고 기존 날짜 형식도 보존합니다."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T(?:09|10|11):00)?", value):
        raise ValueError("Invalid refresh period")
    date.fromisoformat(value[:10])
    return value


def due_period(now):
    """전달된 시각까지 도래한 가장 최근 수집 시간을 독일 현지 시각으로 반환합니다."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    local = now.astimezone(BERLIN)
    refresh_day = local.date()
    arrived = [hour for hour in REFRESH_HOURS if local >= datetime.combine(refresh_day, time(hour), tzinfo=BERLIN)]
    if arrived:
        hour = arrived[-1]
    else:
        refresh_day -= timedelta(days=1)
        hour = REFRESH_HOURS[-1]
    return f"{refresh_day.isoformat()}T{hour:02d}:00"
