"""수집 스크립트에서 사용할 번역 설정과 영어·한국어 캐시 구성 진입점을 제공합니다.

model_config는 수동 실행의 환경 변수를 읽고, build_translations는 파일 용어집과
번역 요청 함수를 TranslationService에 연결합니다. 번역 선택·재사용·이름 정책은
mensa.translation_service와 mensa.translation_contract가 담당합니다. 모델 프로세스의
시작·종료, 체크포인트 저장과 공개는 이 모듈의 호출자가 담당합니다.
"""

import os
import urllib.parse

# 기존 scripts.translations 가져오기 경로를 유지하는 호환 API도 제공합니다.
# HTTP 요청·응답 처리는 generation, 캐시와 정책 검증은 translation_contract가 소유합니다.
from mensa.generation import (
    MAX_RESPONSE_BYTES, _validate_ollama_url, _translation_schema, _NoRedirect,
    request_translation,
)
from scripts.notices import load_glossary, translated_notices
from mensa.translation_service import TranslationService
from mensa.translation_contract import (
    PROMPT_VERSION, SUPPORTED_PROMPT_VERSIONS, NAME_POLICY_VERSION,
    COMPONENT_POLICY_VERSION, REVIEWED_DISH_NAMES, REVIEWED_COMPONENT_NAMES,
    _CAMPAIGN_PREFIX, _OPAQUE_TITLE_LABEL, source_for, source_key, _text,
    validate_result, _semantic_name, _apply_name_policy, _semantic_component,
    _apply_component_policy, reusable_translation, validate_cache,
    validate_notice_translations, generation_identity,
)


def model_config(env=None):
    """환경 변수 매핑에서 접속 설정 사전을 만들며, 기본 OpenAI 설정이 모두 비면 None을 반환합니다.

    env를 생략하면 os.environ을 읽습니다. URL과 모델 중 일부만 설정한 경우나 허용하지
    않는 URL은 ValueError로 거부하며, 이 함수는 모델에 연결하거나 프로세스를 실행하지
    않습니다. 원격 접속에는 HTTPS를 요구하고 로컬 루프백의 HTTP만 허용합니다.

    이 경로는 backend_revision을 선언하지 않으므로 generation_identity가
    공급자·모델·URL을 묶어 번역 접속 설정을 구분하는 값으로 사용합니다.
    작업자 TOML의 선언된 리비전은 mensa.config와
    mensa.translation_runtime이 처리합니다.
    """
    env = os.environ if env is None else env
    url = env.get("MENU_TRANSLATION_URL", "").strip()
    model = env.get("MENU_TRANSLATION_MODEL", "").strip()
    key = env.get("MENU_TRANSLATION_API_KEY", "").strip()
    provider = env.get("MENU_TRANSLATION_PROVIDER", "openai").strip() or "openai"
    if provider not in ("openai", "ollama"):
        raise ValueError("Unknown translation provider")
    if not url and not model and not key and provider == "openai":
        return None
    if not url or not model:
        raise ValueError("Set both MENU_TRANSLATION_URL and MENU_TRANSLATION_MODEL")
    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if not parsed.hostname or (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
        raise ValueError("Translation endpoint requires HTTPS, except loopback development")
    # 접속 주소와 자격 증명을 분리하여 주소의 사용자 정보나 쿼리에 비밀값이 섞이지 않게 합니다.
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Keep credentials in MENU_TRANSLATION_API_KEY, never in the URL")
    config = {"url": url, "model": model, "key": key}
    if provider == "ollama":
        _validate_ollama_url(url)
        config["provider"] = provider
    return config


def build_translations(menu, previous, phrases, config, translate=None, *, checkpoint=None):
    """메뉴·이전 캐시·편집 문구·모델 설정을 받아 검증된 번역 캐시 사전을 반환합니다.

    TranslationService는 편집 문구, 재사용할 수 있는 캐시, 새 모델 번역을 순서대로
    선택합니다. config가 None이면 모델을 호출하지 않으므로 반환 캐시에 모든 요리의
    번역이 채워졌다고 가정하지 마십시오. 공개 가능 여부는 공개 단계에서 검사합니다.

    기본 용어집은 파일에서 읽고, 새 모델 번역에는 전달된 translate 또는 HTTP 요청
    함수인 request_translation을 사용합니다. checkpoint를 제공하면 검증된 새 항목을
    포함한 독립 캐시 복사본을 전달하며, 실제 저장은 콜백이 수행합니다. 이 함수는 캐시를
    직접 저장하거나 공개하지 않습니다.
    """
    service = TranslationService(glossary_provider=load_glossary,
                                 translate=translate or request_translation)
    return service.build(menu, previous, phrases, config, checkpoint=checkpoint)
