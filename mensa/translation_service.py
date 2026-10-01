"""TranslationService가 제공된 안내 용어집·편집 번역·기존 캐시를 사용하고, 필요한 항목만 생성 함수에 요청하여 번역 캐시를 구성합니다."""

import copy

from mensa.notice_contract import source_notices, translate_notice_labels
from mensa.translation_contract import (
    PROMPT_VERSION, source_for, source_key, _text, validate_result,
    _apply_name_policy, _apply_component_policy, reusable_translation,
    validate_cache, generation_identity,
)


class TranslationService:
    """TranslationService가 편집 번역, 캐시 재사용, 모델 생성 중 사용할 번역을 선택하고 이름 정책을 적용하며, 실제 입출력과 모델 시작·종료는 전달된 함수가 담당합니다."""

    def __init__(self, *, glossary_provider, translate):
        if not callable(glossary_provider):
            raise TypeError("glossary_provider must be callable")
        if not callable(translate):
            raise TypeError("translate must be callable")
        self._glossary_provider = glossary_provider
        self._translate = translate

    def build(self, menu, previous, phrases, config, *, checkpoint=None):
        """TranslationService.build가 비공개 번역 캐시를 만들고, 선택적으로 승인된 각 새 항목을 체크포인트 콜백에 전달합니다.

        체크포인트에는 나머지 메뉴의 번역이 아직 없을 수 있습니다. 이 메서드는 콜백에
        검증된 캐시의 독립적인 복사본을 전달하며, 콜백이 실패하면 캐시 구성을 중단합니다.
        """
        if checkpoint is not None and not callable(checkpoint):
            raise TypeError("checkpoint must be callable")
        previous = previous or {"schema_version": 1, "entries": {}}
        validate_cache(previous, allow_legacy_names=True)
        cache = copy.deepcopy(previous)
        # build는 중단된 스냅샷 갱신에서 여전히 필요할 수 있으므로 보존된 항목에도 현재 이름 정책을 적용합니다.
        for entry in cache["entries"].values():
            _apply_name_policy(entry)
            _apply_component_policy(entry)
        # build는 모든 요리의 번역이 캐시에 있어도 원본 안내 문구가 검토된 용어집에 있는지 확인합니다.
        # build는 스냅샷 교체 사이에 중단되어도 기존 메뉴와 호환되도록 이전 안내 문구를 보존합니다.
        glossary = self._glossary_provider()
        cache.setdefault("notices", {}).update(translate_notice_labels(source_notices(menu), glossary))
        identity = generation_identity(config, notice_glossary=glossary) if config else None
        for day in menu["days"]:
            for meal in day["meals"]:
                source = source_for(meal)
                key = source_key(source)
                if meal["translation_key"] != key:
                    raise ValueError("Menu translation key mismatch")
                texts = [source["name_de"], *source["components"]]
                if all(text in phrases and isinstance(phrases[text], dict) and all(_text(phrases[text].get(lang)) for lang in ("en", "ko")) for text in texts):
                    result = {lang: {"name": phrases[texts[0]][lang], "components": [phrases[t][lang] for t in texts[1:]]} for lang in ("en", "ko")}
                    origin, model = "editorial-draft", "assistant-draft"
                elif reusable_translation(
                    cache["entries"].get(key), config["model"] if config else None, identity=identity
                ):
                    continue
                elif config:
                    result = copy.deepcopy(self._translate(source, config))
                    origin, model = "model", config["model"]
                else:
                    continue
                validate_result(result, source)
                cache["entries"][key] = {"source": source, **result, "origin": origin, "model": model, "prompt_version": PROMPT_VERSION}
                if origin == "model":
                    cache["entries"][key]["generation_identity"] = dict(identity)
                _apply_name_policy(cache["entries"][key])
                _apply_component_policy(cache["entries"][key])
                if checkpoint is not None:
                    validate_cache(cache)
                    checkpoint(copy.deepcopy(cache))
        validate_cache(cache)
        return cache
