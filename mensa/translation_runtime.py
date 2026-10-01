"""resume_configured_translations가 비공개 번역 체크포인트를 읽고, 새 번역이 필요할 때만 설정된 모델 접속 환경을 준비합니다."""

from collections.abc import Mapping
from contextlib import ExitStack
import copy
from pathlib import Path

from mensa.checkpoints import TranslationCheckpointStore
from mensa.config import InferenceSettings
from mensa.errors import GenerationError
from mensa.generation import request_translation
from mensa.model_runtime import model_session
from mensa.translation_runner import resume_translations


def _validate_settings(settings, key):
    # 전체 설정 선언과 URL 문법 검증은 로더가 담당합니다. _validate_settings는 모든 번역을 재사용할 수
    # 있어도 모델 실행 경계에 필요한 설정을 검사합니다.
    if not isinstance(settings, InferenceSettings) or not isinstance(key, str):
        raise GenerationError('invalid_config')
    if not all(isinstance(value, str) and value.strip()
               for value in (settings.mode, settings.provider, settings.model, settings.revision)):
        raise GenerationError('invalid_config')
    if settings.mode == 'managed':
        valid = (settings.provider == 'ollama' and settings.url is None
                 and all(isinstance(path, Path) and path.is_absolute()
                         for path in (settings.binary, settings.models_dir, settings.log_dir)))
    elif settings.mode == 'external':
        valid = (settings.provider in ('ollama', 'openai')
                 and isinstance(settings.url, str) and bool(settings.url.strip())
                 and all(value is None for value in (settings.binary, settings.models_dir, settings.log_dir)))
    else:
        valid = False
    if not valid:
        raise GenerationError('invalid_config')


def _runtime_config(value, identity, key):
    if (not isinstance(value, Mapping)
            or any(value.get(name) != expected for name, expected in identity.items())
            or not isinstance(value.get('url'), str) or not value['url'].strip()
            or value.get('key') != key):
        raise GenerationError('invalid_config')
    return copy.deepcopy(dict(value))


def resume_configured_translations(menu, published, phrases, settings, *, state_dir,
                                   glossary_provider, translate=request_translation,
                                   runtime=model_session, key='', checkpoint=None):
    """resume_configured_translations가 선언된 공급자·모델·리비전으로 번역을 재개하고, 새 번역이 필요할 때만 모델 실행 환경을 사용합니다.

    호출자는 큐 잠금, load_worker_config로 검증된 설정 및 상태 디렉터리의 상위 경로
    준비를 담당합니다. 이 함수는 승인된 각 항목과 최종 캐시를 저장한 뒤, 선택적인
    콜백에 독립적인 복사본을 전달합니다. 결과를 공개하거나 이력을 지우지 않으며,
    결과가 공개 처리에 전달되기 전에 모델 실행 환경의 정리를 마칩니다.
    """
    _validate_settings(settings, key)
    store = TranslationCheckpointStore(state_dir)
    for name, port in (('glossary_provider', glossary_provider), ('translate', translate),
                       ('runtime', runtime)):
        if not callable(port):
            raise TypeError(f'{name} must be callable')
    if checkpoint is not None and not callable(checkpoint):
        raise TypeError('checkpoint must be callable')

    # 관리하는 포트는 실행마다 달라질 수 있습니다. resume_configured_translations는 비공개 이력의
    # 호환성을 임시 엔드포인트나 자격 증명이 아니라 선언된 리비전으로 결정합니다.
    identity = {'provider': settings.provider, 'model': settings.model,
                'backend_revision': settings.revision}

    def persist(cache):
        store.save(cache)
        if checkpoint is not None:
            checkpoint(copy.deepcopy(cache))

    with ExitStack() as stack:
        active = None

        def generate(source, config):
            nonlocal active
            if active is None:
                # generate는 모델 실행 환경이 반환한 식별 정보를 검사하기 전에 ExitStack에 정리 책임을 등록합니다.
                yielded = stack.enter_context(runtime(settings, key=key))
                active = _runtime_config(yielded, identity, key)
            return copy.deepcopy(translate(copy.deepcopy(source), copy.deepcopy(active)))

        return resume_translations(menu, published, phrases, identity,
                                   glossary_provider=glossary_provider, translate=generate,
                                   load_checkpoint=store.load, save_checkpoint=persist)
