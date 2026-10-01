"""DirectoryPublisher가 완전한 메뉴·번역 JSON 쌍을 불변 릴리스로 설치하고 원자적으로 공개 대상을 선택합니다.

릴리스 ID는 SHA256(u64be(len(menu)) + menu + u64be(len(cache)) + cache)이며,
u64be는 바이트 길이를 8바이트 부호 없는 빅 엔디언 정수로 표현합니다. 각 데이터는
키를 정렬하고 불필요한 공백을 없앤 UTF-8 JSON이며 숫자는 모두 유한해야 합니다. 호출자는 작성자가 하나뿐임을
보장합니다. 읽는 쪽은 current.json을 한 번 읽어 선택한 릴리스를 고정하며, 이후 다른
유효한 릴리스를 선택해도 그 JSON 쌍은 바뀌지 않습니다. 악의적인 파일 시스템 변경이나
정전의 안전성은 보장하지 않습니다. 새로 소유하는 디렉터리·파일은 umask와 무관하게
공개 권한 0755/0644를 사용합니다. 기존 경로는 검사하며 수정하지 않습니다. 설치가
끝난 릴리스와 이전 릴리스는 남겨 둡니다.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from mensa.notice_contract import validate_notice_translations
from mensa.publication import validate_publication


PAIR_FILES = ('menu.json', 'translations.json')


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Release data must be finite JSON') from error


def _release_id(menu_bytes, cache_bytes):
    return hashlib.sha256(len(menu_bytes).to_bytes(8, 'big') + menu_bytes +
                          len(cache_bytes).to_bytes(8, 'big') + cache_bytes).hexdigest()


def _kind(path, predicate, *, missing=False):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        if missing:
            return False
        raise ValueError(f'Missing public release path: {path}') from error
    if not predicate(mode):
        raise ValueError(f'Unexpected public release path type (including symlink): {path}')
    return True


def _directory(path):
    if not _kind(path, stat.S_ISDIR, missing=True):
        path.mkdir(mode=0o755)
        path.chmod(0o755)


def _read_file(path):
    _kind(path, stat.S_ISREG)
    try:
        return path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f'Missing public release file: {path}') from error


def _write_file(path, payload):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(payload)


class DirectoryPublisher:
    def __init__(self, public_dir: Path, *, notice_glossary):
        if not isinstance(public_dir, Path) or not public_dir.is_absolute():
            raise ValueError('An explicit absolute public directory Path is required')
        validate_notice_translations(notice_glossary)
        # DirectoryPublisher는 검증된 안내 문구와 번역 문자열의 두 매핑 계층을 변경 불가능한 튜플로 보관합니다.
        self._glossary = tuple((label, tuple(text.items())) for label, text in notice_glossary.items())
        self._root = public_dir
        self._data = public_dir / 'data'
        self._releases = self._data / 'releases'
        self._pointer = self._data / 'current.json'

    def _selection(self):
        if not _kind(self._pointer, stat.S_ISREG, missing=True):
            return None
        value = json.loads(_read_file(self._pointer))
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'release_id'} or
                type(value['schema_version']) is not int or value['schema_version'] != 1 or
                not isinstance(value['release_id'], str) or re.fullmatch(r'[0-9a-f]{64}', value['release_id']) is None):
            raise ValueError('Invalid current release selection')
        return value['release_id']

    def _pair(self, release_id, *, expected=None):
        directory = self._releases / release_id
        _kind(self._releases, stat.S_ISDIR)
        _kind(directory, stat.S_ISDIR)
        payloads = tuple(_read_file(directory / name) for name in PAIR_FILES)
        if _release_id(*payloads) != release_id or (expected is not None and payloads != expected):
            raise ValueError('Release pair differs from its immutable identity')
        menu, cache = (json.loads(payload) for payload in payloads)
        if not isinstance(cache, dict):
            raise ValueError('Invalid published translation cache')
        validate_publication(menu, cache, notice_glossary=cache.get('notices', {}))
        if tuple(_json_bytes(value) for value in (menu, cache)) != payloads:
            raise ValueError('Release pair is not canonical finite JSON')
        return menu, cache

    def load_current(self):
        """DirectoryPublisher가 경로를 생성하지 않고 current.json의 선택 정보와 해당 릴리스의 완전한 메뉴·번역 쌍을 읽습니다."""
        if not _kind(self._root, stat.S_ISDIR, missing=True):
            return None
        if not _kind(self._data, stat.S_ISDIR, missing=True):
            return None
        _kind(self._releases, stat.S_ISDIR, missing=True)
        selected = self._selection()
        return None if selected is None else self._pair(selected)

    def _install_pair(self, release_id, payloads):
        target = self._releases / release_id
        if _kind(target, stat.S_ISDIR, missing=True):
            self._pair(release_id, expected=payloads)
            return
        temporary = Path(tempfile.mkdtemp(prefix='.release-', dir=self._releases))
        try:
            temporary.chmod(0o755)
            for name, payload in zip(PAIR_FILES, payloads):
                _write_file(temporary / name, payload)
            os.rename(temporary, target)
        finally:
            # DirectoryPublisher는 current.json 갱신이 실패해도 설치가 끝난 릴리스를 보존합니다.
            try:
                shutil.rmtree(temporary)
            except FileNotFoundError:
                pass

    def _select(self, release_id):
        descriptor, temporary = tempfile.mkstemp(prefix='.current-', suffix='.tmp', dir=self._data)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(_json_bytes({'schema_version': 1, 'release_id': release_id}))
            _kind(self._pointer, stat.S_ISREG, missing=True)
            os.replace(temporary, self._pointer)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def publish(self, menu, cache, *, previous=None, today=None):
        """DirectoryPublisher가 메뉴·번역 입력을 검증하고 완전한 JSON 쌍을 설치한 뒤 current.json을 원자적으로 교체합니다."""
        menu, cache = copy.deepcopy(menu), copy.deepcopy(cache)
        glossary = {label: dict(text) for label, text in self._glossary}
        validate_publication(menu, cache, notice_glossary=glossary,
                             previous=copy.deepcopy(previous), today=today)
        payloads = (_json_bytes(menu), _json_bytes(cache))
        release_id = _release_id(*payloads)
        _kind(self._root.parent, stat.S_ISDIR)
        for directory in (self._root, self._data, self._releases):
            _directory(directory)
        selected = self._selection()
        if selected is not None:
            self._pair(selected, expected=payloads if selected == release_id else None)
            if selected == release_id:
                return release_id
        self._install_pair(release_id, payloads)
        self._select(release_id)
        return release_id
