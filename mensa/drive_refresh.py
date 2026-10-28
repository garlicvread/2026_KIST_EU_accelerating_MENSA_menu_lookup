"""DriveRefreshJob이 소스 체크아웃을 읽기만 하며 식단을 준비하고 Drive에 게시합니다."""

import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from mensa.config import WorkerConfig
from mensa.maintenance import (ensure_capacity, record_prepared_release, record_successful_release,
                               plan_maintenance, apply_maintenance, maintain_logs)
from mensa.publication import validate_publication
from mensa.refresh_service import RefreshService
from mensa.releases import DirectoryPublisher, _json_bytes, _release_id
from mensa.retention import retain_menu, public_translations, check_bundle_size
from mensa.retention_runtime import RetainedTranslationStore
from mensa.schedule import BERLIN, validate_period
from mensa.translation_runtime import resume_configured_translations
from scripts.notices import load_glossary
from scripts.update_menu import read_json, require_current_coverage


class _PublicationCheckpoint:
    """_PublicationCheckpoint가 큐 잠금 안에서 준비한 공개 묶음과 수집 주기의 연결만 저장합니다."""

    def __init__(self, state_dir):
        self.path = state_dir / 'drive-publication.json'

    def _regular(self):
        try:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise ValueError('Drive publication checkpoint must be a regular file')
        except FileNotFoundError:
            pass

    @staticmethod
    def _validate(value):
        if (not isinstance(value, dict) or set(value) != {
                'schema_version', 'period', 'release_id', 'previous_release_id', 'remote', 'folder_id', 'file_id'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('Invalid Drive publication checkpoint')
        validate_period(value['period'])
        if not isinstance(value['release_id'], str) or re.fullmatch(r'[0-9a-f]{64}', value['release_id']) is None:
            raise ValueError('Invalid prepared Drive release identity')
        previous = value['previous_release_id']
        if previous is not None and (not isinstance(previous, str) or re.fullmatch(r'[0-9a-f]{64}', previous) is None):
            raise ValueError('Invalid previous Drive release identity')
        if not all(isinstance(value[name], str) and value[name] for name in ('remote', 'folder_id', 'file_id')):
            raise ValueError('Missing prepared Drive destination')
        return value

    def load(self):
        self._regular()
        try:
            return self._validate(json.loads(self.path.read_text(encoding='utf-8')))
        except FileNotFoundError:
            return None

    def save(self, value):
        self._validate(value)
        self._regular()
        descriptor, temporary = tempfile.mkstemp(prefix='.drive-publication-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class DriveRefreshJob:
    """DriveRefreshJob이 기존 큐·번역 정책을 사용하며 준비한 동일 묶음을 업로드 재시도에 재사용합니다."""

    def __init__(self, config, *, source, clock=None, translate=None, runtime=None,
                 key='', translation_builder=None, publisher=None):
        if not isinstance(config, WorkerConfig) or config.publication.mode != 'drive':
            raise ValueError('Drive refresh requires WorkerConfig with drive publication')
        if not callable(source) or any(port is not None and not callable(port)
                                      for port in (clock, translate, runtime, translation_builder)):
            raise TypeError('Drive source and optional translation/runtime/clock ports must be callable')
        self._config = config
        self._source = source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._translate = translate
        self._runtime = runtime
        self._key = key
        self._translation_builder = translation_builder
        if publisher is None:
            from mensa.drive_publication import DrivePublisher
            settings = config.publication
            publisher = DrivePublisher(settings.remote, settings.folder_id, file_id=settings.file_id)
        if not callable(getattr(publisher, 'publish', None)) or not callable(getattr(publisher, 'load_current', None)):
            raise TypeError('Drive publisher must provide publish and load_current')
        self._publisher = publisher

    def __call__(self, state):
        pending = state.get('pending') if isinstance(state, dict) else None
        if (not isinstance(pending, dict) or pending.get('phase') != 'collect'
                or any(pending.get(name) is not None for name in ('commit_sha', 'run_id', 'dispatch_requested_at'))):
            raise ValueError('Drive refresh cannot abandon pending Git publication IDs')
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Drive refresh requires an aware clock')
        today = now.astimezone(BERLIN).date().isoformat()
        paths, settings = self._config.paths, self._config.publication
        glossary = load_glossary(paths.checkout_dir / 'data/notice-translations.json')
        staging = DirectoryPublisher(paths.public_dir, notice_glossary=glossary)
        checkpoint = _PublicationCheckpoint(paths.state_dir)
        prepared = checkpoint.load()
        destination = {name: getattr(settings, name) for name in ('remote', 'folder_id', 'file_id')}
        if prepared is not None and prepared['period'] > pending['period']:
            raise ValueError('Drive checkpoint is newer than the queued collection period')
        if prepared is not None and prepared['period'] == pending['period']:
            if any(prepared[name] != value for name, value in destination.items()):
                raise ValueError('Prepared Drive destination changed; preserve the job for inspection')
        # 공개 Drive 묶음을 매번 기준으로 삼아 운영자가 교정한 번역·이력을 로컬 준비본으로 덮지 않습니다.
        current = self._publisher.load_current()
        current_id = self._identity(current)
        if prepared is not None and prepared['period'] == pending['period']:
            pair = staging.load_release(prepared['release_id'])
            # 현재 날짜에 더는 유효하지 않은 준비본은 공개하지 않고 기존 자료를 보존한 채 다시 수집합니다.
            if (pair[0]['coverage']['end'] >= today
                    and retain_menu(pair[0], today, self._config.retention) == pair[0]
                    and public_translations(*pair) == pair[1]
                    and current_id in (prepared['previous_release_id'], prepared['release_id'])):
                return self._publish(pair, prepared['release_id'], glossary, today, prepared['previous_release_id'])

        if current is None:
            current = staging.load_current()
        previous, cache = current if current is not None else (None, None)
        phrases = read_json(paths.checkout_dir / 'data/editorial-translations.json')['phrases']
        fresh = {}

        def source():
            menu = self._source()
            fresh['menu'] = copy.deepcopy(menu)
            return menu

        def translate(menu, published, phrases, *, checkpoint=None):
            private_cache = RetainedTranslationStore(self._config, menu=menu,
                observed=fresh['menu'], today=today)
            history = private_cache.merge(published, published_menu=previous, glossary=glossary)
            private_cache.save(history)
            if self._translation_builder is not None:
                candidate = self._translation_builder(menu, history, phrases, checkpoint=private_cache.save)
            else:
                ports = {}
                if self._translate is not None:
                    ports['translate'] = self._translate
                if self._runtime is not None:
                    ports['runtime'] = self._runtime
                candidate = resume_configured_translations(
                    menu, history, phrases, self._config.inference, state_dir=paths.state_dir,
                    glossary_provider=lambda: copy.deepcopy(glossary), key=self._key,
                    checkpoint_store=private_cache, **ports)
            bounded = private_cache.save(candidate)
            return public_translations(menu, bounded)

        def prepare(menu, candidate, *, previous):
            size = check_bundle_size(menu, candidate, self._config.retention)
            ensure_capacity(self._config, additional_bytes=size + 4096)
            release_id = staging.publish(menu, candidate, previous=previous, today=today)
            record_prepared_release(self._config, release_id, today=date.fromisoformat(today))
            checkpoint.save({'schema_version': 1, 'period': pending['period'],
                             'release_id': release_id, 'previous_release_id': current_id, **destination})

        service = RefreshService(
            source=source, coverage_guard=lambda menu: require_current_coverage(menu, today=today),
            translate=translate,
            validator=lambda menu, candidate: validate_publication(menu, candidate, notice_glossary=glossary),
            publisher=prepare,
            menu_transform=lambda menu: retain_menu(menu, today, self._config.retention),
        )
        pair = service.refresh(previous, cache, phrases)
        prepared = checkpoint.load()
        return self._publish(pair, prepared['release_id'], glossary, today, prepared['previous_release_id'])

    @staticmethod
    def _identity(pair):
        return None if pair is None else _release_id(*(_json_bytes(value) for value in pair))

    def _publish(self, pair, release_id, glossary, today, previous_release_id):
        menu, cache = pair
        validate_publication(menu, cache, notice_glossary=glossary, today=today)
        check_bundle_size(menu, cache, self._config.retention)
        observed = self._identity(self._publisher.load_current())
        if observed not in (previous_release_id, release_id):
            raise ValueError('Published Drive data changed during collection; reprepare on the next retry')
        result = self._publisher.publish(menu, cache, today=today)
        if result.release_id != release_id or result.file_id != self._config.publication.file_id:
            raise ValueError('Drive upload confirmation differs from prepared data or configured file')
        record_successful_release(self._config, release_id, today=date.fromisoformat(today))
        pins = tuple(identifier for identifier in (release_id, previous_release_id) if identifier is not None)
        apply_maintenance(self._config, plan_maintenance(self._config,
            today=date.fromisoformat(today), pinned=pins))
        return pair

    def maintain(self, now):
        """JobRunner가 잠금 안에서 유휴·실패 상태에도 로그 한도를 적용합니다."""
        maintain_logs(self._config, today=now.astimezone(BERLIN).date())
        ensure_capacity(self._config)
