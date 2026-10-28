"""DrivePublisher가 지정된 Drive 폴더의 current.json 한 개에 검증된 공개 식단·번역을 게시합니다.

호출자는 같은 폴더에 게시하는 작성자가 하나뿐임을 보장합니다. 게시기는 기존 파일의
ID와 MD5를 검사하고 업로드 뒤 다시 읽지만, Drive의 동시 수정이나 네트워크 중단을
원자적으로 되돌리지 않습니다. 업로드 결과를 확인하지 못하면 오류를 전달하며, 다음
호출은 원격 내용을 다시 검사하여 이미 게시한 같은 묶음의 업로드를 생략합니다.
"""

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from mensa.publication import validate_publication
from mensa.releases import _json_bytes, _release_id


_REMOTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.\-]*:')
_DRIVE_ID = re.compile(r'[A-Za-z0-9_-]{10,200}')
_RELEASE_ID = re.compile(r'[0-9a-f]{64}')
_MD5 = re.compile(r'[0-9a-fA-F]{32}')
_CURRENT = 'current.json'
_CACHE_FIELDS = {'schema_version', 'entries', 'notices'}
_ENTRY_FIELDS = {'source', 'en', 'ko', 'origin', 'model', 'prompt_version',
                 'generation_identity', 'name_policy_version', 'component_policy_version'}


@dataclass(frozen=True)
class DrivePublication:
    release_id: str
    file_id: str


def _subprocess_command(argv):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, encoding='utf-8',
                          timeout=120).stdout


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON field in Drive data')
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError('Drive data must contain finite JSON')


def _read_json(payload):
    try:
        return json.loads(payload, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError('Invalid JSON in Drive publication data') from error


def _checked_pair(menu, cache, *, previous=None, today=None):
    if not isinstance(cache, dict):
        raise ValueError('Invalid published translation cache')
    validate_publication(menu, cache, notice_glossary=cache.get('notices', {}),
                         previous=previous, today=today)
    if type(cache['schema_version']) is not int or set(cache) - _CACHE_FIELDS:
        raise ValueError('Unrecognized public translation cache fields')
    for entry in cache['entries'].values():
        if set(entry) - _ENTRY_FIELDS:
            raise ValueError('Unrecognized public translation entry fields')
        for name in ('name_policy_version', 'component_policy_version'):
            if name in entry and (not isinstance(entry[name], str) or not entry[name].strip()):
                raise ValueError('Invalid public translation policy version')
    return _json_bytes(menu), _json_bytes(cache)


def read_publication(payload):
    """Drive 묶음의 필드·메뉴·번역·내용 ID를 검사하고 같은 게시본의 두 분기를 반환합니다."""
    value = _read_json(payload)
    if (not isinstance(value, dict) or
            set(value) != {'schema_version', 'release_id', 'menu', 'translations'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            not isinstance(value['release_id'], str) or _RELEASE_ID.fullmatch(value['release_id']) is None):
        raise ValueError('Unrecognized Drive current.json publication envelope')
    menu, cache = value['menu'], value['translations']
    if _release_id(*_checked_pair(menu, cache)) != value['release_id']:
        raise ValueError('Drive publication pair differs from its release identity')
    return menu, cache


class DrivePublisher:
    def __init__(self, remote: str, folder_id: str, *, file_id: str | None = None,
                 command=None):
        if not isinstance(remote, str) or _REMOTE.fullmatch(remote) is None:
            raise ValueError('An explicit rclone remote name ending in a colon is required')
        if not isinstance(folder_id, str) or _DRIVE_ID.fullmatch(folder_id) is None:
            raise ValueError('An explicit Drive folder ID is required')
        if file_id is not None and (not isinstance(file_id, str) or _DRIVE_ID.fullmatch(file_id) is None):
            raise ValueError('Invalid expected Drive file ID')
        if command is not None and not callable(command):
            raise ValueError('command must be a callable argv port')
        self._remote = remote
        self._folder_id = folder_id
        self._file_id = file_id
        self._command = _subprocess_command if command is None else command

    def _run(self, operation, *arguments):
        argv = ['rclone', operation, *arguments, '--drive-root-folder-id', self._folder_id]
        try:
            output = self._command(argv)
        except Exception:
            # rclone 또는 주입한 포트의 오류에는 인증 정보가 포함될 수 있으므로 원문과 연결하지 않습니다.
            raise RuntimeError(f'Drive publication {operation} command failed') from None
        if not isinstance(output, str):
            raise ValueError('Invalid rclone command response')
        return output

    def _metadata(self, *, expected_id=None, required=False):
        listing = _read_json(self._run('lsjson', self._remote, '--hash'))
        if (not isinstance(listing, list) or
                any(not isinstance(item, dict) or not isinstance(item.get('Name'), str) for item in listing)):
            raise ValueError('Invalid Drive folder listing')
        matching = [item for item in listing if item['Name'] == _CURRENT]
        if len(matching) > 1:
            raise ValueError('Duplicate current.json files in the configured Drive folder')
        expected_id = self._file_id if expected_id is None else expected_id
        if not matching:
            if required or expected_id is not None:
                raise ValueError('Expected current.json is missing from the configured Drive folder')
            return None
        item = matching[0]
        file_id = item.get('ID')
        hashes = item.get('Hashes')
        md5 = None
        if isinstance(hashes, dict):
            md5 = next((value for name, value in hashes.items() if name.lower() == 'md5'), None)
        if (item.get('Path') != _CURRENT or item.get('IsDir') is not False or
                not isinstance(file_id, str) or _DRIVE_ID.fullmatch(file_id) is None or
                type(item.get('Size')) is not int or item['Size'] < 0 or
                not isinstance(md5, str) or _MD5.fullmatch(md5) is None):
            raise ValueError('Invalid current.json metadata in the configured Drive folder')
        if expected_id is not None and file_id != expected_id:
            raise ValueError('Drive current.json file ID differs from the expected file ID')
        return file_id, item['Size'], md5.lower()

    def _current(self, *, expected_id=None, required=False):
        metadata = self._metadata(expected_id=expected_id, required=required)
        if metadata is None:
            return None
        file_id, size, md5 = metadata
        payload = self._run('cat', self._remote + _CURRENT)
        raw = payload.encode('utf-8')
        if len(raw) != size or hashlib.md5(raw).hexdigest() != md5:
            raise ValueError('Drive current.json differs from its listed content hash')
        menu, cache = read_publication(payload)
        release_id = _release_id(_json_bytes(menu), _json_bytes(cache))
        return DrivePublication(release_id, file_id), (menu, cache)

    def load_current(self) -> tuple[dict, dict] | None:
        """DrivePublisher가 폴더나 파일을 생성하지 않고 검증된 공개 메뉴·번역 쌍을 읽습니다."""
        current = self._current()
        return None if current is None else current[1]

    def publish(self, menu, cache, *, previous=None, today=None) -> DrivePublication:
        """DrivePublisher가 입력과 기존 파일을 먼저 검증하고 같은 ID로 갱신한 공개 묶음을 다시 읽어 확인합니다."""
        menu, cache = copy.deepcopy(menu), copy.deepcopy(cache)
        payloads = _checked_pair(menu, cache, previous=copy.deepcopy(previous), today=today)
        release_id = _release_id(*payloads)
        current = self._current()
        expected_id = None if current is None else current[0].file_id
        if current is not None and current[0].release_id == release_id:
            if tuple(_json_bytes(value) for value in current[1]) != payloads:
                raise ValueError('Drive publication pair differs from its requested release')
            self._file_id = current[0].file_id
            return current[0]
        envelope = {'schema_version': 1, 'release_id': release_id,
                    'menu': menu, 'translations': cache}
        with tempfile.TemporaryDirectory(prefix='mensa-drive-publication-') as directory:
            source = Path(directory) / _CURRENT
            source.write_bytes(_json_bytes(envelope))
            self._run('copyto', str(source), self._remote + _CURRENT,
                      '--ignore-times', '--retries', '1', '--low-level-retries', '1')
        readback = self._current(expected_id=expected_id, required=True)
        if (readback[0].release_id != release_id or
                tuple(_json_bytes(value) for value in readback[1]) != payloads):
            raise ValueError('Drive publication readback differs from the requested pair')
        self._file_id = readback[0].file_id
        return readback[0]
