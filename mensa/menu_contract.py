"""메뉴 검증 함수가 메뉴 JSON의 스키마·원본 출처·항목 식별 정보를 확인하고 이전 공개 스냅샷의 데이터 손실을 검사합니다."""

from collections import Counter
from datetime import date, datetime
import hashlib
import json
import re


SOURCE_URL = "https://www.stw-saarland.de/gastro/mensa-saarbruecken/"
PRICE_PATTERN = re.compile(r"S:\s*(\d+)[,.](\d{2})\s*\|\s*M:\s*(\d+)[,.](\d{2})\s*\|\s*G:\s*(\d+)[,.](\d{2})")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return " ".join(value.split())


def _digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _translation_key(name, components):
    return _digest({"name_de": name, "components": [item["name_de"] for item in components]})


def _meal_id(day, category, name, occurrence=1):
    base = day + "-" + _digest({"date": day, "category": category, "name": name})
    return base if occurrence == 1 else f"{base}-{occurrence}"


def _source_date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value),
             "Missing or invalid source day metadata")
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def _iso_date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "Invalid ISO date")
    date.fromisoformat(value)
    return value


def _prices(raw):
    _require(isinstance(raw, str), "Price block is not text")
    matched = PRICE_PATTERN.fullmatch(raw)
    _require(matched is not None, "Malformed, partial, or duplicate S/M/G price block")
    groups = matched.groups()
    amounts = [int(groups[i]) * 100 + int(groups[i + 1]) for i in range(0, 6, 2)]
    _require(all(0 < value <= 100_000 for value in amounts), "Price outside valid range")
    return dict(zip(("student", "staff", "guest"), amounts))


def _fields(value, required, label):
    _require(isinstance(value, dict) and set(value) == set(required.split()), f"Invalid {label} fields")


def _string(value, label):
    _require(isinstance(value, str) and bool(value.strip()) and value == _text(value), f"Invalid {label}")


def _notice_list(value):
    _require(isinstance(value, list), "Notices must be a list")
    for notice in value:
        _string(notice, "notice")


def validate_menu(menu, previous=None):
    """validate_menu가 메뉴 JSON의 구조·가격 출처·항목 식별 정보를 검증하고, previous가 있으면 겹치는 날짜의 메뉴·가격 손실을 검사합니다."""
    try:
        _validate_schema(menu)
        if previous is not None:
            _validate_schema(previous)
            _validate_previous(menu, previous)
    except (TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
        raise ValueError(f"Invalid menu data: {exc}") from exc


def _validate_schema(menu):
    _fields(menu, "schema_version source coverage days", "menu")
    _require(type(menu["schema_version"]) is int and menu["schema_version"] == 1, "Unsupported schema version")
    source = menu["source"]
    _fields(source, "url fetched_at sha256", "source")
    _require(source["url"] == SOURCE_URL, "Unexpected source URL")
    _require(isinstance(source["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]), "Invalid source hash")
    _require(isinstance(source["fetched_at"], str), "Invalid fetch timestamp")
    timestamp = datetime.fromisoformat(source["fetched_at"].replace("Z", "+00:00"))
    _require(timestamp.tzinfo is not None, "Fetch timestamp needs timezone")
    _fields(menu["coverage"], "start end", "coverage")
    _require(isinstance(menu["days"], list) and menu["days"], "No menu days")
    dates, identities, ids = [], Counter(), set()
    for day in menu["days"]:
        _fields(day, "date meals", "day")
        current_date = _iso_date(day["date"])
        dates.append(current_date)
        _require(isinstance(day["meals"], list), "Meals must be a list")
        for meal in day["meals"]:
            _fields(meal, "id translation_key category location name_de components notices prices price_status price_source", "meal")
            for name in ("id", "translation_key", "category", "location", "name_de"):
                _string(meal[name], name)
            _require(meal["category"] != "Information", "Information is not a meal")
            identity = (current_date, meal["category"], meal["name_de"])
            _require(meal["id"] not in ids, "Duplicate meal ID")
            identities[identity] += 1
            ids.add(meal["id"])
            _require(meal["id"] == _meal_id(*identity, identities[identity]), "Meal ID does not match source identity")
            _require(isinstance(meal["components"], list), "Components must be a list")
            for component in meal["components"]:
                _fields(component, "name_de notices", "component")
                _string(component["name_de"], "component name")
                _notice_list(component["notices"])
            _notice_list(meal["notices"])
            _require(meal["translation_key"] == _translation_key(meal["name_de"], meal["components"]), "Translation key mismatch")
            provenance = meal["price_source"]
            _fields(provenance, "date category name raw", "price provenance")
            _require((provenance["date"], provenance["category"], provenance["name"]) == identity,
                     "Price provenance does not match meal")
            if meal["prices"] is None:
                _require(meal["price_status"] == "source_pending" and provenance["raw"] is None,
                         "Pending price provenance mismatch")
            else:
                _fields(meal["prices"], "student staff guest", "prices")
                _require(meal["price_status"] == "verified", "Priced meal is not verified")
                _require(all(type(value) is int for value in meal["prices"].values()), "Prices must be integer cents")
                _require(meal["prices"] == _prices(provenance["raw"]), "Prices do not match raw source block")
    _require(dates == sorted(set(dates)), "Days must be unique and ordered")
    _require(menu["coverage"] == {"start": dates[0], "end": dates[-1]}, "Coverage does not match extracted dates")


def _validate_previous(menu, previous):
    current_days = {day["date"]: day["meals"] for day in menu["days"]}
    for old_day in previous["days"]:
        if old_day["date"] not in current_days:
            _require(not (menu["coverage"]["start"] <= old_day["date"] <= menu["coverage"]["end"]),
                     "Previously covered day disappeared inside current range")
            continue
        old_meals = old_day["meals"]
        current = current_days[old_day["date"]]
        # _validate_previous는 작은 수정을 허용하지만, 이미 공개된 하루 메뉴의 4분의 1을 넘는 항목이 사라지면
        # 자동 공개가 안전하지 않으므로 거부하고 검토를 요구합니다.
        _require(len(current) >= len(old_meals) * 0.75, "Suspicious loss of source meals on overlapping day")
        current_identities = {(meal["category"], meal["name_de"]) for meal in current}
        current_priced = Counter((meal["category"], meal["name_de"]) for meal in current if meal["prices"] is not None)
        old_priced = Counter((meal["category"], meal["name_de"]) for meal in old_meals if meal["prices"] is not None)
        for identity, count in old_priced.items():
            if identity in current_identities:
                _require(current_priced[identity] >= count, "Previously published same-meal prices disappeared")
