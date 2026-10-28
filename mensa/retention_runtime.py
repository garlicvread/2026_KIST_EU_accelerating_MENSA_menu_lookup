"""비공개 번역과 사용 기록을 한 파일에 저장하여 공개 묶음과 수명을 분리합니다."""

import copy
from datetime import date, timedelta
import json
import os
import stat
import tempfile

from mensa.checkpoints import TranslationCheckpointStore
from mensa.maintenance import ensure_capacity
from mensa.notice_contract import source_notices
from mensa.releases import _json_bytes
from mensa.retention import prune_cache
from mensa.translation_contract import generation_identity, reusable_translation, validate_cache


class RetainedTranslationStore:
    """작업자 잠금 아래 번역·사용 기록을 원자적으로 교체하며 옛 체크포인트는 최초에만 읽습니다."""

    def __init__(self, config, *, menu, observed, today):
        self.config = config
        self.today = date.fromisoformat(today) if isinstance(today, str) else today
        self.path = config.paths.state_dir / 'translation-state.json'
        self.legacy = TranslationCheckpointStore(config.paths.state_dir)
        self.required = {m['translation_key'] for d in menu['days'] for m in d['meals']}
        self.notices = source_notices(menu)
        self.observed = {m['translation_key'] for d in observed['days'] for m in d['meals']}
        self.observed_notices = source_notices(observed)
        self.usage = None

    def _exists(self):
        self.legacy._directory_exists()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Translation retention state must be a private regular file')
        if info.st_size > self.config.retention.cache_max_bytes:
            raise ValueError('Translation retention state exceeds its byte limit')
        return True

    def _prune(self, cache):
        validate_cache(cache, allow_legacy_names=True)
        cache, self.usage = prune_cache(
            cache, self.usage, required_keys=self.required & cache['entries'].keys(),
            required_notices=self.notices & cache.get('notices', {}).keys(),
            observed_keys=self.observed, observed_notices=self.observed_notices,
            today=self.today, policy=self.config.retention)
        return cache

    def load(self):
        if self._exists():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or set(value) != {'schema_version', 'cache', 'usage'}
                    or type(value['schema_version']) is not int or value['schema_version'] != 1):
                raise ValueError('Invalid translation retention state')
            self.usage = value['usage']
            return self._prune(value['cache'])
        cache = self.legacy.load()
        return None if cache is None else self._prune(cache)

    def merge(self, published, *, published_menu=None, glossary):
        established = self._exists()
        private = self.load()
        history = copy.deepcopy(published or {'schema_version': 1, 'entries': {}})
        if established:
            # 미사용 공개 이력을 반복해서 가져오면 이미 만료한 번역이 새 항목처럼 되살아납니다.
            cutoff = (self.today - timedelta(days=self.config.retention.history_days)).isoformat()
            days = [] if published_menu is None else [d for d in published_menu['days'] if d['date'] >= cutoff]
            keys = {m['translation_key'] for d in days for m in d['meals']}
            labels = source_notices({'days': days})
            history['entries'] = {k: v for k, v in history['entries'].items() if k in keys}
            history['notices'] = {k: v for k, v in history.get('notices', {}).items() if k in labels}
        identity = generation_identity({'provider': self.config.inference.provider,
                                        'model': self.config.inference.model,
                                        'backend_revision': self.config.inference.revision}, notice_glossary=glossary)
        if private is not None:
            for key, candidate in private['entries'].items():
                public = history['entries'].get(key)
                if (public is None or (public['origin'] == candidate['origin'] == 'model'
                        and not reusable_translation(public, self.config.inference.model, identity=identity)
                        and reusable_translation(candidate, self.config.inference.model, identity=identity))):
                    history['entries'][key] = candidate
            history['notices'] = {**private.get('notices', {}), **history.get('notices', {})}
        return self._prune(history)

    def save(self, cache):
        bounded = self._prune(copy.deepcopy(cache))
        payload = _json_bytes({'schema_version': 1, 'cache': bounded, 'usage': self.usage})
        if len(payload) > self.config.retention.cache_max_bytes:
            raise ValueError('Translation retention state exceeds its byte limit')
        self.legacy._directory_exists()
        if not self.config.paths.state_dir.exists():
            self.config.paths.state_dir.mkdir(mode=0o700)
        self._exists()
        ensure_capacity(self.config, additional_bytes=len(payload))
        descriptor, temporary = tempfile.mkstemp(prefix='.translation-state-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            from pathlib import Path
            Path(temporary).unlink(missing_ok=True)
        return copy.deepcopy(bounded)
