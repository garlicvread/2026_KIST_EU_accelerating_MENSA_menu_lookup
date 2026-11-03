"""DirectoryRefreshJob이 원본 메뉴·가격과 사용 가능한 번역을 게시하고 별도 번역 작업을 제공합니다.

JobRunner는 비공개 상태 디렉터리 준비, 큐 잠금 및 완료 기록을 담당합니다. 완료 기록이
실패하면 DirectoryRefreshJob은 원본을 다시 수집하면서 비공개 번역 이력을 재사용할 수
있습니다. 이 모듈은 Git 공개 진행 상태를 관리하거나 Git 명령을 실행하거나 체크아웃에 쓰지 않습니다.
"""

import copy
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from mensa.config import WorkerConfig
from mensa.generation import request_translation
from mensa.model_runtime import model_session
from mensa.maintenance import (ensure_capacity, record_prepared_release, record_successful_release,
                               plan_maintenance, apply_maintenance, maintain_logs)
from mensa.publication import validate_publication
from mensa.refresh_service import RefreshService
from mensa.releases import DirectoryPublisher
from mensa.translation_runtime import resume_configured_translations
from mensa.translation_work import (adopt_legacy_failure, available_translations,
                                     load_translation_state, run_translation_work)
from mensa.retention import retain_menu, public_translations, check_bundle_size
from scripts.notices import load_glossary
from scripts.update_menu import read_json, require_current_coverage


class DirectoryRefreshJob:
    source_needs_model_resources = False

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
        publisher = DirectoryPublisher(paths.public_dir, notice_glossary=glossary(), recover_translations=True)
        current = publisher.load_current()
        if current is None:
            previous = read_json(paths.checkout_dir / 'site/data/menu.json')
            cache = read_json(paths.checkout_dir / 'site/data/translations.json')
        else:
            previous, cache = current
        phrases = read_json(paths.checkout_dir / 'data/editorial-translations.json')['phrases']
        observed = {}
        def source():
            menu = self._source()
            observed['menu'] = copy.deepcopy(menu)
            return menu
        def translate(menu, published, phrases, *, checkpoint=None):
            _, candidate = available_translations(self._config, menu, published, phrases, glossary(),
                previous=previous, observed=observed['menu'], today=today)
            return public_translations(menu, candidate)
        service = RefreshService(
            source=source,
            coverage_guard=lambda menu: require_current_coverage(menu, today=today),
            translate=translate,
            validator=lambda menu, candidate: validate_publication(menu, candidate, notice_glossary=glossary()),
            publisher=lambda menu, candidate, *, previous: self._publish(
                publisher, menu, candidate, today, previous),
            menu_transform=lambda menu: retain_menu(menu, today, self._config.retention),
        )
        return service.refresh(previous, cache, phrases)

    def _publish(self, publisher, menu, candidate, today, previous):
        size = check_bundle_size(menu, candidate, self._config.retention)
        ensure_capacity(self._config, additional_bytes=size + 4096)
        release_id = publisher.publish(menu, candidate, previous=previous, today=today)
        record_prepared_release(self._config, release_id, today=date.fromisoformat(today))
        self._confirm(release_id, today)
        return release_id

    def _confirm(self, release_id, today):
        record_successful_release(self._config, release_id, today=date.fromisoformat(today))
        apply_maintenance(self._config, plan_maintenance(self._config,
            today=date.fromisoformat(today), pinned=(release_id,)))

    def maintain(self, now):
        load_translation_state(self._config.paths.state_dir / 'translation-work.json')
        maintain_logs(self._config, today=now.astimezone(ZoneInfo('Europe/Berlin')).date())
        ensure_capacity(self._config)

    def adopt_translation_failure(self, pending, now):
        adopt_legacy_failure(self._config.paths.state_dir, pending)

    def translation_work(self, now, *, resources, source_state):
        paths = self._config.paths
        glossary = load_glossary(paths.checkout_dir / 'data/notice-translations.json')
        publisher = DirectoryPublisher(paths.public_dir, notice_glossary=glossary, recover_translations=True)
        def generate(menu, history, phrases, store, reviewed):
            return resume_configured_translations(menu, history, phrases, self._config.inference,
                state_dir=paths.state_dir, glossary_provider=lambda: copy.deepcopy(reviewed),
                translate=request_translation if self._translate is None else self._translate,
                runtime=model_session if self._runtime is None else self._runtime,
                key=self._key, checkpoint_store=store)
        def publish(pair, release_id, previous_id, today):
            from mensa.translation_work import _identity
            if _identity(publisher.load_current()) not in (previous_id, release_id):
                raise ValueError('Published data changed during translation work')
            actual = self._publish(publisher, *pair, today, pair[0])
            if actual != release_id:
                raise ValueError('Prepared translation identity changed')
        pending = source_state['pending']
        return run_translation_work(self._config, now, resources=resources,
            current=publisher.load_current, load_prepared=publisher.load_release,
            publish=publish, confirm=lambda pair, release_id, previous_id, today: self._confirm(release_id, today),
            generate=generate, glossary=glossary,
            phrases=read_json(paths.checkout_dir / 'data/editorial-translations.json')['phrases'],
            source_pending=pending is not None and pending['phase'] != 'collect')
