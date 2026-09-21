"""Reviewed translations for exact source notice labels, without model inference."""

import json
from pathlib import Path

GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "data" / "notice-translations.json"


def _label(value):
    return (isinstance(value, str) and 0 < len(value.strip()) <= 1000
            and not any(ord(character) < 32 for character in value))


def validate_notice_translations(notices):
    if not isinstance(notices, dict):
        raise ValueError("Notice translations must be an object")
    for source, entry in notices.items():
        if not _label(source) or not isinstance(entry, dict) or set(entry) != {"en", "ko"}:
            raise ValueError("Notice translation needs an exact German key and exactly en/ko text")
        if not all(_label(entry[language]) for language in ("en", "ko")):
            raise ValueError("Notice translations must contain nonempty en/ko text")


def load_glossary():
    glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    if not isinstance(glossary, dict) or glossary.get("schema_version") != 1:
        raise ValueError("Unsupported notice translation glossary")
    notices = glossary.get("notices")
    validate_notice_translations(notices)
    return notices


def source_notices(menu):
    labels = set()
    for day in menu["days"]:
        for meal in day["meals"]:
            for record in (meal, *meal.get("components", [])):
                notices = record.get("notices", [])
                if not isinstance(notices, list) or not all(_label(label) for label in notices):
                    raise ValueError("Source notices must be a list of nonempty strings")
                labels.update(notices)
    return labels


def translated_notices(menu):
    labels = source_notices(menu)
    glossary = load_glossary()
    unknown = sorted(labels - glossary.keys())
    if unknown:
        raise ValueError("Unknown notice labels: " + ", ".join(repr(label) for label in unknown)
                         + "; review and add en/ko translations to data/notice-translations.json before retrying")
    return {label: dict(glossary[label]) for label in sorted(labels)}


def require_notice_coverage(menu, cache):
    expected = translated_notices(menu)
    notices = cache.get("notices", {})
    validate_notice_translations(notices)
    missing = sorted(expected.keys() - notices.keys())
    if missing:
        raise ValueError("Notice translations missing for: " + ", ".join(missing)
                         + "; rebuild translations before publication")
    if any(notices[label] != translation for label, translation in expected.items()):
        raise ValueError("Notice translations differ from the reviewed glossary; rebuild translations before publication")
