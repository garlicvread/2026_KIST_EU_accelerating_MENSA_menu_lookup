"""번역 검증 함수가 원본·캐시·생성 식별 정보를 검사하고, 명명 함수가 검토된 의미 기반 요리·구성 요소 이름을 일관되게 적용합니다."""

import hashlib
import json
import re

from mensa.notice_contract import validate_notice_translations

PROMPT_VERSION = "menu-v4"
# PROMPT_VERSION은 v4의 제목 지침을 지정하며, 명명 함수는 v3 캐시 제목도 현재 정책에 맞게 보정합니다.
SUPPORTED_PROMPT_VERSIONS = frozenset({"menu-v3", PROMPT_VERSION})
NAME_POLICY_VERSION = "semantic-names-v1"
COMPONENT_POLICY_VERSION = "semantic-components-v1"

# REVIEWED_DISH_NAMES는 원본과 정확히 일치할 때만 이름을 대체하며, 비건 등의 한정 표현을 제거해서는 안 됩니다.
# Wikingertopf의 일반적인 요리 유형을 확인하는 근거입니다: https://www.edeka.de/rezeptwelt/rezepte/wikingertopf/
REVIEWED_DISH_NAMES = {
    "Wikingertopf": {"en": "Meatball stew", "ko": "고기완자 스튜"},
    "Köttbullar": {"en": "Swedish meatballs", "ko": "스웨덴식 미트볼"},
    "Kaisergemüse mit gebackenem Tofu und Vollkornpasta": {
        "en": "Mixed vegetables with cooked tofu and whole-grain pasta",
        "ko": "조리한 두부와 통곡물 파스타를 곁들인 모둠 채소",
    },
    "Veganer Cornflakes Taler Chicken Style": {
        "en": "Vegan cornflake-crusted chicken-style patty",
        "ko": "콘플레이크를 입힌 비건 치킨 스타일 패티",
    },
}
# 독일어 Peperoni는 이름이 비슷한 영어 소시지가 아니라 고추를 뜻합니다.
# 고추 명칭을 확인하는 근거입니다: https://www.edeka.de/wissen/kuechenwissen/lebensmittellexikon/peperoni/
REVIEWED_COMPONENT_NAMES = {"Peperoni": {"en": "Chili peppers", "ko": "고추"}}
_CAMPAIGN_PREFIX = re.compile(r"^(?:(?:mensaVital|KlimaTeller)\s*:\s*)+", re.IGNORECASE)
_OPAQUE_TITLE_LABEL = re.compile(r"mensaVital|KlimaTeller|Wikingertopf|Köttbullar", re.IGNORECASE)


def source_for(meal):
    return {"name_de": meal["name_de"], "components": [c["name_de"] for c in meal["components"]]}


def source_key(source):
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 1000 and not any(ord(c) < 32 and c not in "\n\t" for c in value)


def validate_result(result, source):
    if not isinstance(result, dict) or set(result) != {"en", "ko"}:
        raise ValueError("Translation must contain exactly en and ko")
    for lang in ("en", "ko"):
        part = result[lang]
        if not isinstance(part, dict) or set(part) != {"name", "components"}:
            raise ValueError("Translation cannot add prices, notices or other fields")
        if not _text(part["name"]) or not isinstance(part["components"], list):
            raise ValueError("Translation name/components are invalid")
        if len(part["components"]) != len(source["components"]) or not all(_text(s) for s in part["components"]):
            raise ValueError("Translation lost or added components")


def _semantic_name(name, source, lang):
    reviewed = REVIEWED_DISH_NAMES.get(source["name_de"])
    if reviewed:
        return reviewed[lang]
    name = _CAMPAIGN_PREFIX.sub("", name.strip())
    if not _text(name) or _OPAQUE_TITLE_LABEL.search(name):
        raise ValueError("Translation title needs a reviewed descriptive name")
    return name


def _apply_name_policy(entry):
    changed = False
    for lang in ("en", "ko"):
        name = _semantic_name(entry[lang]["name"], entry["source"], lang)
        if name != entry[lang]["name"]:
            entry[lang]["name"] = name
            changed = True
    if changed:
        # _apply_name_policy는 결정적인 제목 보정만 수행하므로 원래의 생성 출처 정보를 유지합니다.
        entry["name_policy_version"] = NAME_POLICY_VERSION


def _semantic_component(name, source_name, lang):
    reviewed = REVIEWED_COMPONENT_NAMES.get(source_name)
    return reviewed[lang] if reviewed else name


def _apply_component_policy(entry):
    changed = False
    for lang in ("en", "ko"):
        for index, source_name in enumerate(entry["source"]["components"]):
            name = _semantic_component(entry[lang]["components"][index], source_name, lang)
            if name != entry[lang]["components"][index]:
                entry[lang]["components"][index] = name
                changed = True
    if changed:
        entry["component_policy_version"] = COMPONENT_POLICY_VERSION


def _identity_digest(value):
    """_identity_digest가 JSON 매핑을 정규 직렬화하여 삽입 순서와 무관한 SHA256 지문을 계산합니다."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generation_identity(config, *, notice_glossary):
    """generation_identity가 번역 정책·안내 용어집·백엔드의 지문을 만들어 캐시 재사용의 호환성 기준을 반환합니다.

    호출자가 backend_revision을 지정하면 엔드포인트 URL 대신 불변 백엔드 리비전을
    선언한 것으로 취급합니다. 이 선언은 모델 가중치나 번역의 정확성을 입증하지 않습니다.
    """
    validate_notice_translations(notice_glossary)
    policy = {
        "prompt_version": PROMPT_VERSION,
        "name_policy_version": NAME_POLICY_VERSION,
        "component_policy_version": COMPONENT_POLICY_VERSION,
        "reviewed_dish_names": REVIEWED_DISH_NAMES,
        "reviewed_component_names": REVIEWED_COMPONENT_NAMES,
    }
    backend = {
        "provider": config.get("provider", "openai"),
        "model": config["model"],
    }
    if "backend_revision" in config:
        revision = config["backend_revision"]
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("backend_revision must be a nonempty string")
        backend["backend_revision"] = revision
    else:
        backend["url"] = config.get("url", "")
    return {
        "schema_version": 1,
        "policy": _identity_digest(policy),
        "glossary": _identity_digest(notice_glossary),
        "backend": _identity_digest(backend),
    }


def validate_generation_identity(value):
    """validate_generation_identity가 선택적으로 저장된 생성 식별 정보의 필드·스키마·SHA256 지문을 검사하고 잘못된 정보를 거부합니다."""
    fields = {"schema_version", "policy", "glossary", "backend"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Generation identity must contain exactly schema_version, policy, glossary and backend")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported generation identity schema version")
    for field in ("policy", "glossary", "backend"):
        digest = value[field]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Generation identity requires lowercase SHA256 digests")


def reusable_translation(entry, model=None, *, identity=None):
    """reusable_translation이 검증된 번역 항목의 프롬프트·모델·생성 식별 정보를 대조하여 재사용 가능 여부를 반환합니다. identity가 없으면 기존 호환성 동작을 유지합니다."""
    if not entry or entry["prompt_version"] not in SUPPORTED_PROMPT_VERSIONS:
        return False
    if entry["origin"] != "model":
        return True
    if model is not None and entry["model"] != model:
        return False
    return identity is None or entry.get("generation_identity") == identity


def validate_cache(cache, *, allow_legacy_names=False):
    if not isinstance(cache, dict) or cache.get("schema_version") != 1 or not isinstance(cache.get("entries"), dict):
        raise ValueError("Unsupported translation cache")
    if "notices" in cache:
        validate_notice_translations(cache["notices"])
    for key, entry in cache["entries"].items():
        if not isinstance(entry, dict):
            raise ValueError("Invalid translation entry")
        source = entry.get("source")
        if not isinstance(source, dict) or set(source) != {"name_de", "components"}:
            raise ValueError("Missing translation source")
        if not _text(source["name_de"]) or not isinstance(source["components"], list) or not all(_text(c) for c in source["components"]):
            raise ValueError("Invalid translation source text")
        if source_key(source) != key:
            raise ValueError("Translation cache key does not match its source")
        validate_result({lang: entry.get(lang) for lang in ("en", "ko")}, source)
        if not allow_legacy_names:
            for lang in ("en", "ko"):
                if entry[lang]["name"] != _semantic_name(entry[lang]["name"], source, lang):
                    raise ValueError("Translation title does not follow the reviewed name policy")
                for source_name, name in zip(source["components"], entry[lang]["components"]):
                    if name != _semantic_component(name, source_name, lang):
                        raise ValueError("Translation component does not follow the reviewed name policy")
        if entry.get("origin") not in ("editorial-draft", "reviewed-draft", "model"):
            raise ValueError("Translation provenance is missing")
        if not _text(entry.get("model")) or not _text(entry.get("prompt_version")):
            raise ValueError("Translation model/prompt provenance is missing")
        if "generation_identity" in entry:
            validate_generation_identity(entry["generation_identity"])
