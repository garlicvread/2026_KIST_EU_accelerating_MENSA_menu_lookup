"""안내 문구 검증 함수가 정확한 독일어 원본 표기와 검토된 en/ko 번역을 검사하고, 용어집에 없는 문구를 거부합니다."""


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


def translate_notice_labels(labels, glossary):
    if not isinstance(labels, (set, frozenset)) or not all(_label(label) for label in labels):
        raise ValueError("Notice labels must be a set of nonempty strings")
    validate_notice_translations(glossary)
    unknown = sorted(labels - glossary.keys())
    if unknown:
        raise ValueError("Unknown notice labels: " + ", ".join(repr(label) for label in unknown)
                         + "; review and add en/ko translations to data/notice-translations.json before retrying")
    return {label: dict(glossary[label]) for label in sorted(labels)}
