"""resume_translations가 공개 번역 캐시와 비공개 체크포인트를 병합하고, 전달된 번역·저장 함수로 중단된 번역을 재개합니다."""

import copy

from mensa.translation_contract import generation_identity, reusable_translation, validate_cache
from mensa.translation_service import TranslationService


def resume_translations(menu, published, phrases, config, *, glossary_provider,
                        translate, load_checkpoint, save_checkpoint):
    """resume_translations가 검증된 번역 이력을 병합하고, 승인된 각 번역과 최종 캐시를 비공개 체크포인트에 저장합니다.

    공개된 편집 번역 이력을 우선합니다. 충돌하는 비공개 모델 항목은 현재 설정된
    생성 식별 정보와 호환될 때만 호환되지 않는 공개 모델 항목을 대체할 수 있습니다.
    TranslationService는 보존된 번역의 정책 보정과 현재 편집 정책 적용을 담당합니다.
    실패는 그대로 전달하며, 확정되지 않은 병합을 저장하거나 이력을 지우지 않습니다.
    """
    if published is None or (isinstance(published, dict) and not published):
        published = {"schema_version": 1, "entries": {}}
    validate_cache(published, allow_legacy_names=True)
    for name, port in (("glossary_provider", glossary_provider), ("translate", translate),
                       ("load_checkpoint", load_checkpoint), ("save_checkpoint", save_checkpoint)):
        if not callable(port):
            raise TypeError(f"{name} must be callable")

    private = load_checkpoint()
    if private is not None:
        validate_cache(private, allow_legacy_names=True)
        private = copy.deepcopy(private)
    history = copy.deepcopy(published)
    glossary = copy.deepcopy(glossary_provider())
    identity = generation_identity(config, notice_glossary=glossary) if config is not None else None

    if private is not None:
        # resume_translations는 번역 항목·안내 문구 외의 과거 메타데이터도 보존하며, 공개된 값이 있으면 우선합니다.
        for name, value in private.items():
            if name not in ("entries", "notices"):
                history.setdefault(name, value)
        for key, candidate in private["entries"].items():
            public = history["entries"].get(key)
            if public is None:
                history["entries"][key] = candidate
            elif (config is not None and public["origin"] == "model" and candidate["origin"] == "model"
                  and not reusable_translation(public, config["model"], identity=identity)
                  and reusable_translation(candidate, config["model"], identity=identity)):
                history["entries"][key] = candidate
        if "notices" in private:
            notices = private["notices"]
            notices.update(history.get("notices", {}))
            history["notices"] = notices

    def persist(cache):
        save_checkpoint(copy.deepcopy(cache))

    service = TranslationService(glossary_provider=lambda: glossary, translate=translate)
    final = service.build(menu, history, phrases, config, checkpoint=persist)
    persist(final)
    return copy.deepcopy(final)
