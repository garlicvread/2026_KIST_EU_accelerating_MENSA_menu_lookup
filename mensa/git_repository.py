"""SnapshotRepository가 소유한 메뉴 스냅샷을 준비하고 기록된 범위 안에서 최신 원격 커밋으로의 교체를 재개합니다.

호출자는 비공개 상태 디렉터리와 작업자 잠금을 관리합니다. snapshot-journal.json은
정확한 Git 객체를 기록하며, 이 모듈은 범용 Git 클라이언트나 알 수 없는 파일을 복원할
권한을 제공하지 않습니다. 검사와 비교 후 갱신(CAS)은 악의적인 동시 변경이나 정전의
안전성을 보장하지 않습니다. 재수집이 중단되어 파일이 변경되었으나 준비된 Git 트리가
없으면 작업을 계속 차단합니다.
"""

import copy
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from mensa.schedule import validate_period

DATA_FILES = ('site/data/menu.json', 'site/data/translations.json')
MAX_JOURNAL_BYTES = 65536


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _oid(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) is not None


def _period(value):
    validate_period(value)


def _snapshot(value):
    if value is None:
        return
    _require(isinstance(value, dict) and set(value) == {'base', 'tree', 'blobs', 'sha', 'owned'},
             'Invalid snapshot ownership shape')
    _require(_oid(value['base']) and _oid(value['tree']) and
             (value['sha'] is None or _oid(value['sha'])) and type(value['owned']) is bool,
             'Invalid snapshot ownership identity')
    _require(isinstance(value['blobs'], dict) and set(value['blobs']) == set(DATA_FILES) and
             all(_oid(blob) for blob in value['blobs'].values()), 'Invalid snapshot data blobs')
    _require(value['owned'] or value['sha'] == value['base'], 'Invalid unchanged snapshot')


class SnapshotRepository:
    def __init__(self, checkout_dir: Path, state_dir: Path, *, git):
        _require(isinstance(checkout_dir, Path) and checkout_dir.is_absolute() and
                 isinstance(state_dir, Path) and state_dir.is_absolute(), 'Explicit absolute repository/state Paths required')
        _require(callable(git), 'git must be callable')
        self._checkout = checkout_dir
        self._state_dir = state_dir
        self._path = state_dir / 'snapshot-journal.json'
        self._git = git

    def _validate(self, value):
        _require(isinstance(value, dict) and set(value) == {'version', 'checkout', 'period', 'snapshot', 'replacement'},
                 'Invalid snapshot journal shape')
        _require(type(value['version']) is int and value['version'] == 1 and value['checkout'] == str(self._checkout),
                 'Snapshot journal belongs to another checkout/version')
        _period(value['period'])
        _snapshot(value['snapshot'])
        request = value['replacement']
        if request is not None:
            _require(isinstance(request, dict) and set(request) ==
                     {'original', 'target', 'previous', 'diverged', 'stage', 'ready', 'next_period', 'confirmation'},
                     'Invalid replacement request shape')
            _require(_oid(request['original']) and _oid(request['target']) and type(request['diverged']) is bool and
                     request['stage'] in ('planned', 'detached', 'ref-updated', 'collecting', 'ready') and
                     (request['ready'] is None or _oid(request['ready'])), 'Invalid replacement identity/stage')
            _snapshot(request['previous'])
            _require(not request['diverged'] or (request['previous'] is not None and
                     request['previous']['owned'] and request['previous']['sha'] == request['original']),
                     'Divergent replacement lacks owned original snapshot')
            confirmation = request['confirmation']
            if confirmation is not None:
                _require(isinstance(confirmation, dict) and set(confirmation) == {'run_id', 'success'} and
                         type(confirmation['run_id']) is int and confirmation['run_id'] > 0 and
                         type(confirmation['success']) is bool, 'Invalid original run confirmation')
            if request['next_period'] is not None:
                _period(request['next_period'])
                _require(request['ready'] is not None, 'Replacement period lacks ready snapshot')
        return value

    def _regular(self):
        try:
            mode = self._path.lstat().st_mode
        except FileNotFoundError:
            return False
        _require(stat.S_ISREG(mode), 'Snapshot journal must be a regular file, not a symlink')
        _require(not stat.S_IMODE(mode) & 0o077, 'Snapshot journal must have private permissions')
        return True

    def _load(self):
        mode = self._state_dir.lstat().st_mode
        _require(stat.S_ISDIR(mode) and not stat.S_IMODE(mode) & 0o077, 'Snapshot state must be a private directory')
        if not self._regular():
            return None
        descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_JOURNAL_BYTES, 'Invalid or oversized snapshot journal')
            payload = stream.read(MAX_JOURNAL_BYTES + 1)
        _require(len(payload) <= MAX_JOURNAL_BYTES, 'Oversized snapshot journal')
        return self._validate(json.loads(payload.decode('utf-8')))

    def _save(self, value):
        payload = json.dumps(self._validate(value), ensure_ascii=False, allow_nan=False).encode('utf-8')
        _require(len(payload) <= MAX_JOURNAL_BYTES, 'Oversized snapshot journal')
        mode = self._state_dir.lstat().st_mode
        _require(stat.S_ISDIR(mode) and not stat.S_IMODE(mode) & 0o077, 'Snapshot state must be a private directory')
        self._regular()
        descriptor, temporary = tempfile.mkstemp(prefix='.snapshot-journal-', suffix='.tmp', dir=self._state_dir)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._regular()
            os.replace(temporary, self._path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _new(self, period):
        _period(period)
        return {'version': 1, 'checkout': str(self._checkout), 'period': period,
                'snapshot': None, 'replacement': None}

    def _object(self, expression):
        value = self._git('rev-parse', expression)
        _require(_oid(value), 'Invalid Git object identity')
        return value

    def _clean(self, *, include_ignored=True, target=None):
        flags = ('--ignored',) if include_ignored else ()
        changes = self._git('status', '--porcelain', '--untracked-files=all', *flags).splitlines()
        if include_ignored and changes:
            # NUL 구분자로 읽어 한글·줄바꿈이 있는 경로도 Git의 인용 표기 없이 비교합니다.
            tracked = set(self._git('ls-tree', '-r', '--name-only', '-z', target or 'HEAD').split('\0')) - {''}
            remaining = []
            for line in changes:
                name = line[3:]
                # Python이 실행 중 만든 캐시는 식단 변경이 아닙니다. Git이 무시하는
                # 정상 캐시 파일만 허용하며, 이동할 커밋의 파일과 충돌하면 거부합니다.
                cache = re.fullmatch(r'(?:mensa|scripts)/__pycache__/[A-Za-z_]\w*\.(?:cpython|pypy)-\d+(?:\.opt-[12])?\.pyc', name)
                collision = any(name == path or name.startswith(path + '/') or path.startswith(name + '/')
                                for path in tracked)
                if (line.startswith('!! ') and cache and not collision and
                        stat.S_ISREG((self._checkout / name).lstat().st_mode)):
                    continue
                remaining.append(line)
            changes = remaining
        _require(not changes,
                 'Unknown or unprepared repository changes; inspect before recovery')

    def _position(self):
        return (self._git('rev-parse', '--abbrev-ref', 'HEAD'), self._object('HEAD'), self._object('refs/heads/main'))

    def _merge_base(self, left, right):
        try:
            return self._git('merge-base', left, right)
        except RuntimeError as error:
            raise ValueError('Cannot establish safe repository ancestry') from error

    def _verify(self, snapshot, sha):
        _require(snapshot is not None and (snapshot['sha'] in (None, sha)), 'Unknown snapshot ownership')
        _require(self._object(sha + '^{tree}') == snapshot['tree'] and
                 all(self._object(sha + ':' + name) == blob for name, blob in snapshot['blobs'].items()),
                 'Snapshot tree/data differs from private ownership')
        if snapshot['owned']:
            _require(self._git('show', '-s', '--format=%P', sha).split() == [snapshot['base']],
                     'Snapshot has an unknown parent/history')
            paths = self._git('diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines()
            _require(paths and set(paths) <= set(DATA_FILES), 'Snapshot contains unknown changed paths')
        else:
            _require(sha == snapshot['base'], 'Unchanged snapshot identity moved')

    def _finish(self, journal, sha):
        self._verify(journal['snapshot'], sha)
        self._clean(include_ignored=False)
        journal['snapshot']['sha'] = sha
        if journal['replacement'] is not None:
            journal['replacement'].update(ready=sha, stage='ready')
        self._save(journal)
        return sha

    def prepare_snapshot(self, period):
        _period(period)
        journal = self._load() or self._new(period)
        if journal['replacement'] is not None or (journal['snapshot'] is not None and journal['snapshot']['sha'] is None):
            _require(journal['period'] == period, 'Unresolved snapshot belongs to another period')
        else:
            journal['period'] = period
        intent = journal['snapshot']
        if intent is not None and intent['sha'] is None:
            head = self._object('HEAD')
            if head != intent['base']:
                return self._finish(journal, head)
            changes = self._git('status', '--porcelain', '--untracked-files=all').splitlines()
            _require(all(len(line) > 3 and line[:2] != '??' and line[3:] in DATA_FILES for line in changes),
                     'Unknown files alongside prepared snapshot')
            _require(self._object('HEAD^{tree}') != intent['tree'] and self._object(self._git('write-tree')) == intent['tree'] and
                     not self._git('diff', '--name-only'), 'Prepared snapshot bytes changed before commit')
        else:
            changes = self._git('status', '--porcelain', '--untracked-files=all').splitlines()
            _require(all(len(line) > 3 and line[:2] != '??' and line[3:] in DATA_FILES for line in changes),
                     'Unrelated/untracked snapshot changes; inspect before preparation')
            self._git('add', '--', *DATA_FILES)
            _require(not self._git('diff', '--name-only'), 'Unstaged snapshot changes remain')
            base, tree = self._object('HEAD'), self._git('write-tree')
            _require(_oid(tree), 'Invalid prepared tree identity')
            blobs = {name: self._object(':' + name) for name in DATA_FILES}
            if tree == self._object('HEAD^{tree}'):
                if intent is not None and intent['sha'] == base and intent['tree'] == tree:
                    self._verify(intent, base)
                else:
                    journal['snapshot'] = {'base': base, 'tree': tree, 'blobs': blobs, 'sha': base, 'owned': False}
                return self._finish(journal, base)
            journal['snapshot'] = {'base': base, 'tree': tree, 'blobs': blobs, 'sha': None, 'owned': True}
            self._save(journal)
        self._git('commit', '-m', 'Refresh menu with locally validated translations')
        return self._finish(journal, self._object('HEAD'))

    def _archive(self, sha, create=False):
        ref = 'refs/mensa/superseded/' + sha
        existing = self._git('for-each-ref', '--format=%(objectname)', ref)
        _require(existing in ('', sha), 'Superseded snapshot archive collision')
        if create and not existing:
            self._git('update-ref', ref, sha, '0' * len(sha))

    def plan_replacement(self, period, snapshot_sha, upstream_sha):
        _period(period)
        _require(_oid(snapshot_sha) and _oid(upstream_sha), 'Invalid replacement object identity')
        journal = self._load() or self._new(period)
        _require(journal['replacement'] is None, 'An unresolved replacement already exists')
        _require(self._position() == ('main', snapshot_sha, snapshot_sha), 'Unknown worker HEAD/ref; inspect before recovery')
        self._clean(target=upstream_sha)
        if upstream_sha == snapshot_sha:
            return False
        common = self._merge_base(snapshot_sha, upstream_sha)
        if common == upstream_sha:
            return False
        diverged = common != snapshot_sha
        previous = journal['snapshot']
        if diverged:
            _require(journal['period'] == period and previous is not None and previous['owned'] and
                     previous['sha'] == snapshot_sha, 'Snapshot lacks private ownership; inspect before recovery')
            self._verify(previous, snapshot_sha)
            _require(self._merge_base(previous['base'], upstream_sha) == previous['base'],
                     'Upstream rewrote recorded snapshot base')
            self._archive(snapshot_sha)
        journal['period'] = period
        journal['replacement'] = {'original': snapshot_sha, 'target': upstream_sha, 'previous': copy.deepcopy(previous),
                                  'diverged': diverged, 'stage': 'planned', 'ready': None, 'next_period': None, 'confirmation': None}
        self._save(journal)
        return True

    def _request(self, period, snapshot_sha):
        journal = self._load()
        if journal is None or journal['replacement'] is None:
            return None
        request = journal['replacement']
        _require((period, snapshot_sha) == (journal['period'], request['original']) or
                 (request['next_period'] is not None and (period, snapshot_sha) == (request['next_period'], request['ready'])),
                 'Queue does not match owned replacement identity')
        return journal

    def validate_replacement_queue(self, period, snapshot_sha):
        """SnapshotRepository가 호출자의 큐 주기·SHA를 원래 스냅샷 S 또는 저널에 기록된 정확한 교체 스냅샷 R과 대조합니다."""
        _require(self._request(period, snapshot_sha) is not None, 'No owned replacement matches the queue')

    def replacement_identity(self):
        """SnapshotRepository가 Git 참조나 파일을 이동하지 않고 저널에서 원래 공개 작업의 주기·SHA와 확인 상태를 읽습니다."""
        journal = self._load()
        if journal is None or journal['replacement'] is None:
            return None
        request = journal['replacement']
        return {'period': journal['period'], 'sha': request['original'],
                'confirmation': copy.deepcopy(request['confirmation']),
                'prepared': request['ready'] is not None or journal['snapshot'] != request['previous']}

    def record_original_run(self, period, sha, run_id, *, success=False):
        journal = self._request(period, sha)
        _require(journal is not None and (period, sha) == (journal['period'], journal['replacement']['original']),
                 'Original publication does not match replacement')
        previous = journal['replacement']['confirmation']
        _require(previous is None or previous['run_id'] == run_id, 'Original publication run identity changed')
        journal['replacement']['confirmation'] = {'run_id': run_id, 'success': success or bool(previous and previous['success'])}
        self._save(journal)

    def retire_acknowledgment(self, completed_period):
        journal = self._load()
        _require(journal is not None and journal['replacement'] is not None and journal['period'] == completed_period,
                 'Original queue acknowledgment is not durable')
        request = journal['replacement']
        _require(request['confirmation'] is not None and request['confirmation']['success'] and
                 request['ready'] is None and journal['snapshot'] == request['previous'],
                 'Prepared replacement conflicts with original acknowledgment; inspection required')
        self.resume_replacement(journal['period'], request['original'])
        journal = self._load()
        # SnapshotRepository는 원래 스냅샷 S를 소유한 보관 참조와 이력에 남깁니다. 원격 커밋 M은 생성한 메뉴 스냅샷이 아닙니다.
        journal['snapshot'] = None
        journal['replacement'] = None
        self._save(journal)

    def resume_replacement(self, period, snapshot_sha):
        _period(period)
        _require(_oid(snapshot_sha), 'Invalid queued snapshot identity')
        journal = self._request(period, snapshot_sha)
        if journal is None:
            return None
        request = journal['replacement']
        snapshot = journal['snapshot']
        if request['ready'] is None and snapshot != request['previous']:
            _require(snapshot is not None, 'Replacement has no prepared provenance')
            self.prepare_snapshot(journal['period'])
            journal = self._load()
            request = journal['replacement']
        if request['ready'] is not None:
            sha = request['ready']
            _require(self._position() == ('main', sha, sha), 'Prepared replacement HEAD/ref changed')
            self._verify(journal['snapshot'], sha)
            self._clean(target=sha)
            return {'status': 'ready', 'sha': sha, 'period': request['next_period']}
        S, M = request['original'], request['target']
        self._clean(target=request['target'])
        position = self._position()
        if request['diverged']:
            _require(position in {('main', S, S), ('HEAD', M, S), ('HEAD', M, M), ('main', M, M)},
                     'Unknown replacement HEAD/ref transition')
            self._verify(request['previous'], S)
            self._archive(S, create=True)
            if position == ('main', S, S):
                self._git('switch', '--detach', M)
                request['stage'] = 'detached'
                self._save(journal)
                position = ('HEAD', M, S)
            if position == ('HEAD', M, S):
                self._git('update-ref', 'refs/heads/main', M, S)
                request['stage'] = 'ref-updated'
                self._save(journal)
                position = ('HEAD', M, M)
            if position == ('HEAD', M, M):
                self._git('switch', 'main')
        else:
            _require(position in {('main', S, S), ('main', M, M)}, 'Unknown fast-forward replacement position')
            _require(self._merge_base(S, M) == S, 'Replacement no longer preserves ancestry')
            if position == ('main', S, S):
                self._git('merge', '--ff-only', M)
        request['stage'] = 'collecting'
        self._save(journal)
        return {'status': 'collecting'}

    def record_replacement(self, period, sha, replacement_period):
        _period(replacement_period)
        journal = self._load()
        _require(journal is not None and journal['period'] == period and journal['replacement'] is not None and
                 journal['replacement']['ready'] == sha, 'Replacement is not verified and ready')
        self._verify(journal['snapshot'], sha)
        journal['replacement']['next_period'] = replacement_period
        self._save(journal)

    def complete_replacement(self, period, sha):
        journal = self._request(period, sha)
        _require(journal is not None and journal['replacement']['next_period'] == period and
                 journal['replacement']['ready'] == sha, 'Replacement queue checkpoint is not confirmed')
        journal['period'] = period
        journal['replacement'] = None
        self._save(journal)
