"""독일어 원본 안내 문구와 en/ko 번역 형식을 검사하고 사전에서 알려진 번역을 선택합니다."""


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
    # 없는 문구는 모델 번역 또는 화면의 독일어 원문 표시로 처리합니다.
    return {label: dict(glossary[label]) for label in sorted(labels & glossary.keys())}
