"""검토된 안내 용어집 파일을 읽고 메뉴에 필요한 영어·한국어 안내 번역을 확인합니다.

알레르기·첨가물 등의 원본 안내 문구는 정확한 독일어 키로 조회합니다. 미등록 문구의
모델 번역은 TranslationService가 담당합니다. 파일의 스키마 버전은 이 모듈이 확인하고,
안내 매핑의 필드·문자열과
원본 문구의 수집·조회 규칙은 mensa.notice_contract가 관리합니다. 용어집과 캐시를
수정하거나 파일에 쓰는 작업은 수행하지 않습니다.
"""

import json
from pathlib import Path

# 안내 계약 함수를 재노출하여 기존 scripts.notices 호출자의 가져오기 경로를 제공합니다.
from mensa.notice_contract import _label, source_notices, translate_notice_labels, validate_notice_translations

GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "data" / "notice-translations.json"


def load_glossary(path=None):
    """지정한 UTF-8 JSON 파일 또는 기본 검토 파일에서 검증된 notices 매핑을 반환합니다.

    바깥 문서의 schema_version은 1이어야 합니다. notices의 각 독일어 키에 대응하는
    항목은 en·ko 키만 허용하며, 두 값은 비어 있지 않은 문자열이어야 합니다. 파일 읽기·JSON 해석 오류는 호출자에게
    전달하고, 잘못된 스키마나 안내 항목은 ValueError로 거부합니다.
    """
    glossary = json.loads((GLOSSARY_PATH if path is None else Path(path)).read_text(encoding="utf-8"))
    if not isinstance(glossary, dict) or glossary.get("schema_version") != 1:
        raise ValueError("Unsupported notice translation glossary")
    notices = glossary.get("notices")
    validate_notice_translations(notices)
    return notices


def translated_notices(menu, *, glossary=None):
    """메뉴와 구성품의 안내 문구를 모아 해당 독일어 키의 en·ko 번역 사전을 반환합니다.

    glossary를 제공하면 그 매핑을 사용하고, 생략하면 기본 파일을 읽습니다. 결과는
    용어집 항목의 복사본이며, 미등록 원본 문구의 번역은 포함하지 않습니다.
    누락된 안내는 원문으로 표시하고 별도 번역 작업에서 모델로 보완할 수 있습니다.
    """
    labels = source_notices(menu)
    if glossary is None:
        glossary = load_glossary()
    return translate_notice_labels(labels, glossary)
