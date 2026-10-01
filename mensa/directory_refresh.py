"""DirectoryRefreshJob이 한 큐 작업의 식단을 번역하고 완성된 메뉴·번역 JSON 쌍을 공개 디렉터리에 게시합니다.

JobRunner는 비공개 상태 디렉터리 준비, 큐 잠금 및 완료 기록을 담당합니다. 완료 기록이
실패하면 DirectoryRefreshJob은 원본을 다시 수집하면서 비공개 번역 이력을 재사용할 수
있습니다. 이 모듈은 Git 공개 진행 상태를 관리하거나 Git 명령을 실행하거나 체크아웃에 쓰지 않습니다.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mensa.config import WorkerConfig
from mensa.generation import request_translation
from mensa.model_runtime import model_session
from mensa.publication import validate_publication
from mensa.refresh_service import RefreshService
from mensa.releases import DirectoryPublisher
from mensa.translation_runtime import resume_configured_translations
from scripts.notices import load_glossary
from scripts.update_menu import read_json, require_current_coverage


class DirectoryRefreshJob:
    def __init__(self, config, *, source, clock=None, translate=None, runtime=None, key=''):
        if not isinstance(config, WorkerConfig) or getattr(config.publication, 'mode', None) != 'directory':
            raise ValueError('Directory refresh requires WorkerConfig with directory publication')
        if not callable(source) or any(port is not None and not callable(port) for port in (clock, translate, runtime)):
            raise ValueError('source and optional clock/translate/runtime ports must be callable')
        if not isinstance(key, str):
            raise ValueError('key must be a string')
        self._config = config
        self._source = source
        self._clock = clock
        self._translate = translate
        self._runtime = runtime
        self._key = key

    def __call__(self, state):
        pending = state.get('pending') if isinstance(state, dict) else None
        if (not isinstance(pending, dict) or pending.get('phase') != 'collect' or
                any(pending.get(name) is not None for name in ('commit_sha', 'run_id', 'dispatch_requested_at'))):
            raise ValueError('Directory refresh requires pending collect work without Git publication IDs')
        now = datetime.now(timezone.utc) if self._clock is None else self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Directory refresh clock must return an aware datetime')
        today = now.astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()
        paths = self._config.paths
        reviewed = load_glossary(paths.checkout_dir / 'data/notice-translations.json')
        # 검토된 안내 문구와 번역은 변경 불가능한 문자열입니다. glossary 함수는 호출할 때마다
        # 새 사전을 반환하여 외부 함수가 공유된 용어집 스냅샷을 수정하지 못하도록 합니다.
        snapshot = tuple((label, tuple(text.items())) for label, text in reviewed.items())
        def glossary():
            return {label: dict(text) for label, text in snapshot}
        publisher = DirectoryPublisher(paths.public_dir, notice_glossary=glossary())
        current = publisher.load_current()
        if current is None:
            previous = read_json(paths.checkout_dir / 'site/data/menu.json')
            cache = read_json(paths.checkout_dir / 'site/data/translations.json')
        else:
            previous, cache = current
        phrases = read_json(paths.checkout_dir / 'data/editorial-translations.json')['phrases']
        service = RefreshService(
            source=self._source,
            coverage_guard=lambda menu: require_current_coverage(menu, today=today),
            translate=lambda menu, published, phrases, *, checkpoint=None: resume_configured_translations(
                menu, published, phrases, self._config.inference,
                state_dir=paths.state_dir, glossary_provider=glossary,
                translate=request_translation if self._translate is None else self._translate,
                runtime=model_session if self._runtime is None else self._runtime,
                key=self._key, checkpoint=checkpoint,
            ),
            validator=lambda menu, candidate: validate_publication(menu, candidate, notice_glossary=glossary()),
            publisher=lambda menu, candidate, *, previous: publisher.publish(menu, candidate, previous=previous, today=today),
        )
        return service.refresh(previous, cache, phrases)
