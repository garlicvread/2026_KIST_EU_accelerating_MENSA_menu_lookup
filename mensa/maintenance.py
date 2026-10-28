"""유한한 로컬 생성 자료의 정리 계획과 소유권을 재검사하는 적용 경로를 제공합니다.

호출자는 기록·계획·적용 동안 worker.lock을 보유합니다. 성공 기록은 Drive 읽기 확인
뒤에만 작성하며, 미등록 이전 릴리스는 자동으로 소유했다고 가정하지 않습니다. 정리는
Drive·Git·모델을 변경하지 않습니다. 로그는 열린 stdout/stderr의 inode를 보존합니다.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from mensa.publication import validate_publication
from mensa.releases import PAIR_FILES, _json_bytes, _release_id
from mensa.schedule import BERLIN


_RELEASE = re.compile(r'[0-9a-f]{64}')
_REGISTRY = 'retention-releases.json'
_BACKUPS = '.mensa-backups.json'
_BACKUP_ROOT = 'retention-backup-root.json'
_LOG_NAMES = ('worker.stdout.log', 'worker.stderr.log', 'model.log')
_DEFAULTS = {'keep_releases': 10, 'log_days': 14, 'log_max_bytes': 10_485_760,
             'backup_days': 14, 'max_storage_bytes': 268_435_456}


def _day(value):
    if type(value) is not date:
        raise ValueError('An explicit calendar date is required')
    return value


def _parsed_day(value):
    if not isinstance(value, str):
        raise ValueError('Invalid maintenance date')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Invalid maintenance date')
    return parsed


def _policy(config, name):
    value = getattr(getattr(config, 'retention', None), name, _DEFAULTS[name])
    if type(value) is not int or value <= 0:
        raise ValueError(f'Invalid retention policy: {name}')
    return value


def _absolute(path):
    if not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts:
        raise ValueError('Maintenance requires an explicit absolute Path')
    return path


@contextmanager
def _directory(path):
    """각 경로 성분을 O_NOFOLLOW로 열어 상위 디렉터리 교체·symlink를 거부합니다."""
    path = _absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            try:
                following = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
            except OSError as error:
                raise ValueError(f'Maintenance directory is missing, changed or a symlink: {path}') from error
            os.close(descriptor)
            descriptor = following
        yield descriptor
    finally:
        os.close(descriptor)


def _exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _identity(info, *, directory=False):
    value = {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode}
    if not directory:
        value.update(size=info.st_size, mtime_ns=info.st_mtime_ns, links=info.st_nlink)
    return value


def _dir_identity(path):
    with _directory(path) as descriptor:
        return _identity(os.fstat(descriptor), directory=True)


def _open_regular_at(directory, name, flags, *, expected=None):
    """FIFO를 열어 잠금을 막지 않고, 열기 전후의 일반 파일 정체성을 확인합니다."""
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f'Maintenance requires a regular file, not a symlink: {name}')
        if expected is not None and _identity(before) != {k: v for k, v in expected.items() if k != 'sha256'}:
            raise ValueError('Maintenance file identity changed before opening')
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise ValueError(f'Maintenance requires a regular file, not a symlink: {name}') from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ValueError('Maintenance regular file identity changed while opening')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _snapshot_file(path, *, content):
    with _directory(path.parent) as parent:
        descriptor, before = _open_regular_at(parent, path.name, os.O_RDONLY)
        with os.fdopen(descriptor, 'rb') as stream:
            payload = stream.read() if content else None
            digest = hashlib.sha256(payload).hexdigest() if content else hashlib.file_digest(stream, 'sha256').hexdigest()
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ValueError(f'File identity changed while reading: {path}')
        identity = _identity(before)
        identity['sha256'] = digest
        return payload, identity


def _read_file(path):
    return _snapshot_file(path, content=True)


def _file_identity(path):
    return _snapshot_file(path, content=False)[1]


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate maintenance JSON field')
        result[name] = value
    return result


def _json(payload):
    try:
        return json.loads(payload, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ValueError('Invalid maintenance JSON') from error


def _optional_json(path, default):
    if not _exists(path):
        return default, None
    payload, identity = _read_file(path)
    return _json(payload), identity


def _release_id_valid(identifier):
    if not isinstance(identifier, str) or _RELEASE.fullmatch(identifier) is None:
        raise ValueError('Invalid maintenance release identity')
    return identifier


def _pair(config, identifier):
    identifier = _release_id_valid(identifier)
    directory = _absolute(config.paths.public_dir) / 'data/releases' / identifier
    directory_identity = _dir_identity(directory)
    # Extra files make the directory unknown; they never become implicitly owned.
    names = sorted(child.name for child in directory.iterdir())
    files = {name: _read_file(directory / name) for name in PAIR_FILES}
    payloads = tuple(files[name][0] for name in PAIR_FILES)
    if _release_id(*payloads) != identifier:
        raise ValueError('Release pair differs from its immutable identity')
    menu, cache = (_json(payload) for payload in payloads)
    if not isinstance(cache, dict):
        raise ValueError('Invalid release cache')
    validate_publication(menu, cache, notice_glossary=cache.get('notices', {}))
    if tuple(_json_bytes(value) for value in (menu, cache)) != payloads:
        raise ValueError('Release pair is not canonical JSON')
    return {'directory': directory_identity,
            'files': {name: files[name][1] for name in PAIR_FILES},
            'owned': names == sorted(PAIR_FILES)}


def _registry(config):
    path = _absolute(config.paths.state_dir) / _REGISTRY
    value, identity = _optional_json(path, {'schema_version': 1, 'next_sequence': 1, 'releases': {}})
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'next_sequence', 'releases'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            type(value['next_sequence']) is not int or value['next_sequence'] < 1 or
            not isinstance(value['releases'], dict)):
        raise ValueError('Invalid maintenance release registry')
    sequences = set()
    for identifier, record in value['releases'].items():
        _release_id_valid(identifier)
        if (not isinstance(record, dict) or set(record) != {'state', 'recorded_on', 'sequence', 'tree'} or
                record['state'] not in {'prepared', 'successful'} or
                type(record['sequence']) is not int or record['sequence'] < 1 or
                record['sequence'] >= value['next_sequence'] or record['sequence'] in sequences):
            raise ValueError('Invalid maintenance release record')
        _parsed_day(record['recorded_on'])
        if not isinstance(record['tree'], dict) or set(record['tree']) != {'files', 'directories'}:
            raise ValueError('Invalid maintenance release ownership tree')
        sequences.add(record['sequence'])
    return value, identity


def _atomic_json(path, value):
    payload = _json_bytes(value)
    with _directory(path.parent) as directory:
        if _exists(path):
            _read_file(path)
        temporary = f'.{path.name}-{secrets.token_hex(16)}.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _record_release(config, identifier, *, today, successful):
    _day(today)
    pair = _pair(config, identifier)
    if not pair['owned']:
        raise ValueError('Cannot record ownership of a release containing unknown files')
    registry, _ = _registry(config)
    release_root = config.paths.public_dir / 'data/releases'
    registry['releases'] = {key: record for key, record in registry['releases'].items()
                            if _exists(release_root / key)}
    previous = registry['releases'].get(identifier)
    tree = {'directories': {'': pair['directory']}, 'files': pair['files']}
    if previous is not None and previous['tree'] != tree:
        raise ValueError('Recorded release ownership identity changed')
    if previous is not None and (previous['state'] == 'successful' or not successful):
        return
    registry['releases'][identifier] = {
        'state': 'successful' if successful else 'prepared',
        'recorded_on': today.isoformat(), 'sequence': registry['next_sequence'], 'tree': tree}
    registry['next_sequence'] += 1
    ensure_capacity(config, additional_bytes=len(_json_bytes(registry)))
    _atomic_json(config.paths.state_dir / _REGISTRY, registry)


def record_prepared_release(config, release_id, *, today):
    """로컬 준비가 끝난 쌍의 소유권만 기록하며 원격 게시 성공을 추정하지 않습니다."""
    _record_release(config, release_id, today=today, successful=False)


def record_successful_release(config, release_id, *, today):
    """호출자가 원격 읽기 확인을 마친 쌍을 성공 이력에 기록합니다."""
    _record_release(config, release_id, today=today, successful=True)


def _current(config):
    path = _absolute(config.paths.public_dir) / 'data/current.json'
    value, identity = _optional_json(path, None)
    if value is None:
        if identity is not None:
            raise ValueError('Invalid local current release selection')
        return None, None
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'release_id'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('Invalid local current release selection')
    identifier = _release_id_valid(value['release_id'])
    _pair(config, identifier)
    return identifier, identity


def _tree(path):
    """직접 소유권을 기록할 때만 쓰는 symlink 없는 정확한 파일·디렉터리 목록입니다."""
    directory_identity = _dir_identity(path)
    files, directories = {}, {'': directory_identity}
    for child in sorted(path.iterdir()):
        mode = child.lstat().st_mode
        if stat.S_ISDIR(mode):
            nested = _tree(child)
            files.update({f'{child.name}/{name}': value for name, value in nested['files'].items()})
            directories.update({f'{child.name}/{name}'.rstrip('/'): value
                                for name, value in nested['directories'].items()})
        elif stat.S_ISREG(mode):
            files[child.name] = _file_identity(child)
        else:
            raise ValueError(f'Backup contains a symlink or unknown path: {child}')
    return {'files': files, 'directories': directories}


def _owned_subset(actual, recorded):
    return (isinstance(recorded, dict) and set(recorded) == {'files', 'directories'} and
            all(isinstance(recorded[name], dict) and
                all(recorded[name].get(path) == identity for path, identity in actual[name].items())
                for name in ('files', 'directories')))


def _backup_manifest(backup_dir):
    backup_dir = _absolute(backup_dir)
    _dir_identity(backup_dir)
    manifest, identity = _optional_json(backup_dir / _BACKUPS,
                                      {'schema_version': 1, 'cutover_verified_on': None, 'backups': {}})
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'cutover_verified_on', 'backups'} or
            type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or
            not isinstance(manifest['backups'], dict)):
        raise ValueError('Invalid migration backup ownership manifest')
    if manifest['cutover_verified_on'] is not None:
        _parsed_day(manifest['cutover_verified_on'])
    for name, record in manifest['backups'].items():
        if (not isinstance(name, str) or name in {'', '.', '..', _BACKUPS} or Path(name).name != name or
                not isinstance(record, dict) or set(record) != {'created_on', 'tree'}):
            raise ValueError('Invalid migration backup ownership record')
        _parsed_day(record['created_on'])
        if _exists(backup_dir / name) and not _owned_subset(_tree(backup_dir / name), record['tree']):
            raise ValueError(f'Migration backup identity changed: {name}')
    return manifest, identity


def record_migration_backup(backup_dir, backup_path, *, today):
    """명시적인 직계 하위 복사본의 정확한 파일 정체성을 소유권 목록에 기록합니다."""
    _day(today)
    backup_dir, backup_path = _absolute(backup_dir), _absolute(backup_path)
    if backup_path.parent != backup_dir or backup_path.name == _BACKUPS:
        raise ValueError('A migration backup must be a direct child of its explicit backup root')
    manifest, _ = _backup_manifest(backup_dir)
    tree = _tree(backup_path)
    existing = manifest['backups'].get(backup_path.name)
    record = {'created_on': today.isoformat(), 'tree': tree}
    if existing is not None and existing != record:
        raise ValueError('Migration backup was already recorded with a different identity or date')
    manifest['backups'][backup_path.name] = record
    _atomic_json(backup_dir / _BACKUPS, manifest)


def confirm_migration_cutover(backup_dir, *, today):
    """호출자가 공개·작업자 검증을 마친 날짜를 기록하며 백업은 즉시 제거하지 않습니다."""
    _day(today)
    manifest, _ = _backup_manifest(backup_dir)
    if manifest['cutover_verified_on'] is not None:
        return
    manifest['cutover_verified_on'] = today.isoformat()
    _atomic_json(backup_dir / _BACKUPS, manifest)


def _backup_root(config, explicit=None):
    value, identity = _optional_json(_absolute(config.paths.state_dir) / _BACKUP_ROOT, None)
    bound = None
    if identity is not None and value is None:
        raise ValueError('Invalid registered migration backup root')
    if value is not None:
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'path', 'identity'} or
                type(value['schema_version']) is not int or value['schema_version'] != 1 or
                not isinstance(value['path'], str)):
            raise ValueError('Invalid registered migration backup root')
        bound = _absolute(Path(value['path']))
        if _dir_identity(bound) != value['identity']:
            raise ValueError('Registered migration backup root identity changed')
        _file_identity(bound / _BACKUPS)
    if explicit is not None:
        explicit = _absolute(explicit)
        if bound is not None and explicit != bound:
            raise ValueError('Explicit backup root differs from the registered root')
        _dir_identity(explicit)
        return explicit
    return bound


def register_backup_root(config, backup_dir):
    """명시적으로 소유권을 기록한 백업 루트를 이후 용량 검사·정리에 연결합니다."""
    backup_dir = _absolute(backup_dir)
    _, manifest_identity = _backup_manifest(backup_dir)
    if manifest_identity is None:
        raise ValueError('Register backup ownership before registering its root')
    existing = _backup_root(config)
    if existing is not None:
        if existing != backup_dir:
            raise ValueError('A different migration backup root is already registered')
        return
    value = {'schema_version': 1, 'path': str(backup_dir), 'identity': _dir_identity(backup_dir)}
    ensure_capacity(config, additional_bytes=len(_json_bytes(value)), backup_dir=backup_dir)
    _atomic_json(config.paths.state_dir / _BACKUP_ROOT, value)


def _log_roots(config):
    roots = {_absolute(config.paths.state_dir) / 'logs': _LOG_NAMES}
    log_dir = getattr(config.inference, 'log_dir', None)
    if log_dir is not None:
        log_dir = _absolute(log_dir)
        roots[log_dir] = tuple(sorted(set(roots.get(log_dir, ())) | {'model.log'}))
    return roots


def _log_name(name, allowed):
    if name in allowed:
        return 'active'
    for base in allowed:
        if re.fullmatch(re.escape(base) + r'\.(?:[1-9][0-9]?|[0-9]{4}-[0-9]{2}-[0-9]{2})', name):
            return 'rotation'
    return None


def _log_plan(config, today):
    actions, protected, candidates, roots = [], [], [], {}
    unknown_bytes = 0
    cutoff = today - timedelta(days=_policy(config, 'log_days'))
    log_roots = _log_roots(config)
    for root, allowed in sorted(log_roots.items()):
        if not _exists(root):
            continue
        roots[str(root)] = _dir_identity(root)
        for path in sorted(root.iterdir()):
            kind = _log_name(path.name, allowed)
            if kind is None:
                size = _size(path, excluded=tuple(other for other in log_roots if root != other and root in other.parents))
                unknown_bytes += size
                protected.append({'path': str(path), 'reason': 'unrecognized log path', 'bytes': size})
                continue
            identity = _file_identity(path)
            if identity['links'] != 1:
                raise ValueError(f'Log ownership forbids hardlinks: {path}')
            expired = datetime.fromtimestamp(identity['mtime_ns'] / 1_000_000_000, BERLIN).date() < cutoff
            target = 0 if expired else identity['size']
            candidates.append({'path': str(path), 'kind': kind, 'identity': identity,
                               'target_bytes': target, 'expired': expired})
    limit = _policy(config, 'log_max_bytes')
    if unknown_bytes > limit:
        raise ValueError('Protected unrecognized logs exceed log storage capacity')
    excess = max(0, unknown_bytes + sum(item['target_bytes'] for item in candidates) - limit)
    for item in sorted(candidates, key=lambda item: (item['identity']['mtime_ns'], item['path'])):
        if excess:
            reduction = min(excess, item['target_bytes'])
            item['target_bytes'] -= reduction
            excess -= reduction
        if item['target_bytes'] == item['identity']['size'] and not (item['kind'] == 'rotation' and item['expired']):
            continue
        action = 'delete_log' if item['kind'] == 'rotation' and item['target_bytes'] == 0 else 'truncate_log'
        actions.append({'action': action, 'path': item['path'], 'identity': item['identity'],
                        'target_bytes': item['target_bytes'],
                        'bytes': item['identity']['size'] - item['target_bytes']})
    return actions, protected, roots


def _excluded(config):
    inference = config.inference
    excluded = [_absolute(config.paths.checkout_dir)]
    models = getattr(inference, 'models_dir', None)
    binary = getattr(inference, 'binary', None)
    if models is not None:
        excluded.append(_absolute(models))
    if binary is not None:
        binary = _absolute(binary)
        excluded.append(binary.parent if binary.parent.name == 'runtime' else binary)
    aliases = {'checkout': _absolute(config.paths.checkout_dir)}
    if models is not None:
        aliases['models'] = _absolute(models)
    if binary is not None:
        aliases['runtime'] = binary.parent
    for name, target in aliases.items():
        connection = _absolute(config.paths.state_dir) / name
        if not connection.is_symlink():
            continue
        raw = Path(os.readlink(connection))
        linked = raw if raw.is_absolute() else connection.parent / raw
        # Compare the known connection's literal target; never traverse it.
        if Path(os.path.normpath(linked)) == target:
            excluded.append(connection)
    return tuple(excluded)


def _size(path, *, excluded):
    if path.name == '.git' or any(path == root or root in path.parents for root in excluded):
        return 0
    if not _exists(path):
        return 0
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ValueError(f'Generated storage contains a symlink: {path}')
    if stat.S_ISDIR(mode):
        _dir_identity(path)
        return sum(_size(child, excluded=excluded) for child in path.iterdir())
    if not stat.S_ISREG(mode):
        raise ValueError(f'Generated storage contains an unrecognized file type: {path}')
    with _directory(path.parent):
        info = path.lstat()
    return info.st_size


def generated_bytes(config, backup_dir=None):
    """모델·실행 파일·소스·Git을 제외한 상태·공개 자료·로그·명시적 백업 크기를 셉니다."""
    backup_dir = _backup_root(config, backup_dir)
    roots = {_absolute(config.paths.state_dir), _absolute(config.paths.public_dir) / 'data',
             *_log_roots(config)}
    if backup_dir is not None:
        roots.add(_absolute(backup_dir))
    roots = sorted(root for root in roots if not any(other != root and other in root.parents for other in roots))
    excluded = _excluded(config)
    return sum(_size(root, excluded=tuple(path for path in excluded if path != root and path not in root.parents))
               for root in roots)


def ensure_capacity(config, additional_bytes=0, backup_dir=None):
    """새 파일·임시 복사본을 쓰기 전에 생성 자료 전체의 공간을 예약 검사합니다."""
    if type(additional_bytes) is not int or additional_bytes < 0:
        raise ValueError('additional_bytes must be a nonnegative integer')
    actual = generated_bytes(config, backup_dir=backup_dir)
    limit = _policy(config, 'max_storage_bytes')
    if actual + additional_bytes > limit:
        raise ValueError(f'Generated storage capacity exceeded: {actual} + {additional_bytes} > {limit}')


def _plan(config, *, today, pinned=(), backup_dir=None, logs_only=False):
    _day(today)
    backup_dir = None if logs_only else _backup_root(config, backup_dir)
    actions, protected, roots = _log_plan(config, today)
    plan = {'schema_version': 1, 'scope': 'logs' if logs_only else 'all', 'today': today.isoformat(),
            'state_dir': str(_absolute(config.paths.state_dir)),
            'public_dir': str(_absolute(config.paths.public_dir)),
            'backup_dir': None if backup_dir is None else str(_absolute(backup_dir)),
            'pinned': [], 'roots': roots, 'observed': {}, 'actions': actions, 'protected': protected}
    if not logs_only:
        current, pointer_identity = _current(config)
        registry, registry_identity = _registry(config)
        pins = {_release_id_valid(identifier) for identifier in pinned}
        if len(pins - {current}) > 2:
            raise ValueError('At most two active release pins are permitted')
        plan['pinned'] = sorted(pins)
        plan['observed'] = {'current': current, 'pointer': pointer_identity, 'registry': registry_identity}
        root = config.paths.public_dir / 'data/releases'
        if _exists(root):
            roots[str(root)] = _dir_identity(root)
            successful = sorted((record['sequence'], identifier)
                                for identifier, record in registry['releases'].items()
                                if record['state'] == 'successful' and _exists(root / identifier))
            keep = {current} if current in {identifier for _, identifier in successful} else set()
            for _, identifier in reversed(successful):
                if len(keep) >= _policy(config, 'keep_releases'):
                    break
                keep.add(identifier)
            keep.update(pins)
            if current is not None:
                keep.add(current)
            for directory in sorted(root.iterdir()):
                if _RELEASE.fullmatch(directory.name) is None:
                    protected.append({'path': str(directory), 'reason': 'unrecognized release path'})
                    continue
                record = registry['releases'].get(directory.name)
                complete = all(_exists(directory / name) for name in PAIR_FILES)
                if complete:
                    pair = _pair(config, directory.name)
                    tree = {'directories': {'': pair['directory']}, 'files': pair['files']}
                    if record is not None and record['tree'] != tree:
                        raise ValueError('Recorded release ownership identity changed')
                elif record is not None and directory.name not in keep:
                    # A prior interrupted cleanup may have removed only part of the
                    # recorded pair. Remaining files retain their exact ownership.
                    tree = _tree(directory)
                    if not _owned_subset(tree, record['tree']):
                        raise ValueError('Partial release ownership identity changed')
                    pair = {'owned': True}
                else:
                    _pair(config, directory.name)
                reason = ('current or pinned/recent successful release' if directory.name in keep else
                          'unregistered legacy release' if record is None else
                          'release contains unknown files' if not pair['owned'] else
                          'no confirmed publication permits prepared cleanup' if not successful else None)
                if reason is not None:
                    protected.append({'path': str(directory), 'reason': reason})
                    continue
                actions.append({'action': 'delete_release', 'path': str(directory), 'release_id': directory.name,
                                'tree': tree,
                                'bytes': sum(item['size'] for item in tree['files'].values())})
        if backup_dir is not None:
            manifest, manifest_identity = _backup_manifest(backup_dir)
            plan['observed']['backup_manifest'] = manifest_identity
            roots[str(backup_dir)] = _dir_identity(backup_dir)
            existing = sorted((record['created_on'], name) for name, record in manifest['backups'].items()
                              if _exists(backup_dir / name))
            for path in sorted(backup_dir.iterdir()):
                if path.name != _BACKUPS and path.name not in manifest['backups']:
                    protected.append({'path': str(path), 'reason': 'unregistered migration backup path'})
            latest = existing[-1][1] if existing else None
            verified = manifest['cutover_verified_on']
            for _, name in existing:
                record = manifest['backups'][name]
                eligible = (verified is not None and name != latest and
                            max(_parsed_day(verified), _parsed_day(record['created_on'])) +
                            timedelta(days=_policy(config, 'backup_days')) <= today)
                if eligible:
                    tree = _tree(backup_dir / name)
                    actions.append({'action': 'delete_backup', 'path': str(backup_dir / name),
                                    'tree': tree,
                                    'bytes': sum(item['size'] for item in tree['files'].values())})
                else:
                    protected.append({'path': str(backup_dir / name),
                                      'reason': 'latest backup or cutover retention window'})
        before = generated_bytes(config, backup_dir=backup_dir)
        after = before - sum(action['bytes'] for action in actions)
        if after > _policy(config, 'max_storage_bytes'):
            raise ValueError(f'Protected generated storage exceeds capacity after maintenance: {after}')
        plan.update(before_bytes=before, after_bytes=after)
    else:
        plan.update(before_bytes=None, after_bytes=None)
    return plan


def plan_maintenance(config, *, today, pinned=(), backup_dir=None):
    """제거·축소 후보와 보호 이유·파일 정체성을 담은 JSON 직렬화 가능한 계획을 만듭니다."""
    return _plan(config, today=today, pinned=pinned, backup_dir=backup_dir)


def _check_file_at(directory, name, expected):
    descriptor, info = _open_regular_at(directory, name, os.O_RDONLY, expected=expected)
    with os.fdopen(descriptor, 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError('Maintenance file identity changed before removal')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected['sha256'] or _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError('Maintenance file contents changed before removal')


def _remove_tree(path, expected):
    if _tree(path) != expected:
        raise ValueError('Maintenance tree identity changed before removal')
    for name, identity in sorted(expected['files'].items()):
        target = path / name
        with _directory(target.parent) as parent:
            _check_file_at(parent, target.name, identity)
            os.unlink(target.name, dir_fd=parent)
    for name, identity in sorted(expected['directories'].items(), key=lambda item: (-len(Path(item[0]).parts), item[0])):
        target = path / name if name else path
        with _directory(target.parent) as parent:
            info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            if _identity(info, directory=True) != identity:
                raise ValueError('Maintenance directory identity changed before removal')
            os.rmdir(target.name, dir_fd=parent)


def _apply_log(action):
    path = Path(action['path'])
    with _directory(path.parent) as parent:
        _check_file_at(parent, path.name, action['identity'])
        if action['action'] == 'delete_log':
            os.unlink(path.name, dir_fd=parent)
            return
        descriptor, _ = _open_regular_at(parent, path.name, os.O_RDWR, expected=action['identity'])
        with os.fdopen(descriptor, 'r+b') as stream:
            if _identity(os.fstat(stream.fileno())) != {k: v for k, v in action['identity'].items() if k != 'sha256'}:
                raise ValueError('Log identity changed before truncation')
            target = action['target_bytes']
            stream.seek(-target, os.SEEK_END) if target else stream.seek(0)
            tail = stream.read() if target else b''
            stream.seek(0)
            stream.write(tail)
            stream.truncate(target)
            stream.flush()
            os.fsync(stream.fileno())


def apply_maintenance(config, plan):
    """계획 전체를 다시 계산·대조한 뒤 각 제거 직전 경로·소유권·정체성을 재검사합니다."""
    if not isinstance(plan, dict) or plan.get('schema_version') != 1 or plan.get('scope') not in {'all', 'logs'}:
        raise ValueError('Invalid maintenance plan')
    try:
        fresh = _plan(config, today=_parsed_day(plan['today']), pinned=plan['pinned'],
                      backup_dir=None if plan['backup_dir'] is None else Path(plan['backup_dir']),
                      logs_only=plan['scope'] == 'logs')
    except (KeyError, TypeError, OSError) as error:
        raise ValueError('Maintenance plan cannot be revalidated') from error
    if fresh != plan:
        raise ValueError('Maintenance plan is stale or ownership/path identity changed')
    for action in plan['actions']:
        path = Path(action['path'])
        if action['action'] in {'delete_release', 'delete_backup'}:
            _remove_tree(path, action['tree'])
        else:
            _apply_log(action)
    # Deleted identities must not accumulate in the private ownership registries.
    deleted_releases = {action['release_id'] for action in plan['actions'] if action['action'] == 'delete_release'}
    if deleted_releases:
        registry, _ = _registry(config)
        registry['releases'] = {identifier: record for identifier, record in registry['releases'].items()
                                if identifier not in deleted_releases}
        ensure_capacity(config, additional_bytes=len(_json_bytes(registry)))
        _atomic_json(config.paths.state_dir / _REGISTRY, registry)
    deleted_backups = {Path(action['path']).name for action in plan['actions'] if action['action'] == 'delete_backup'}
    if deleted_backups:
        root = Path(plan['backup_dir'])
        manifest, _ = _backup_manifest(root)
        manifest['backups'] = {name: record for name, record in manifest['backups'].items() if name not in deleted_backups}
        ensure_capacity(config, additional_bytes=len(_json_bytes(manifest)), backup_dir=root)
        _atomic_json(root / _BACKUPS, manifest)
    return {'actions': len(plan['actions']), 'reclaimed_bytes': sum(action['bytes'] for action in plan['actions'])}


def maintain_logs(config, *, today):
    """동일 worker.lock 안의 성공·실패·유휴 tick에서 알려진 로그만 제한합니다."""
    return apply_maintenance(config, _plan(config, today=today, logs_only=True))
