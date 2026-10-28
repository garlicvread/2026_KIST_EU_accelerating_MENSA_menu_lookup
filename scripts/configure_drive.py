"""기존 식단·작업 기록을 보존하면서 Drive 게시와 비공개 작업자 설정을 준비합니다.

호출자는 예약 작업자를 미리 멈추고 서로 겹치지 않는 상태·체크아웃·준비 경로를
명시합니다. 이 명령은 모델을 내려받거나 실행하지 않고 LaunchAgent·Git·공유 설정을
변경하지 않습니다. 네트워크나 설정 설치가 중단되면 검증된 새 Drive 파일 또는 로컬
릴리스가 남을 수 있으며, 재실행은 같은 데이터를 확인하고 재사용합니다.
"""

import argparse
import copy
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from mensa.checkpoints import TranslationCheckpointStore
from mensa.config import (InferenceSettings, PublicationSettings, ResourceSettings,
                          WorkerConfig, WorkerPaths, load_worker_config)
from mensa.drive_publication import DrivePublisher, _checked_pair
from mensa.drive_refresh import _PublicationCheckpoint
from mensa.git_repository import MAX_JOURNAL_BYTES, SnapshotRepository
from mensa.ollama_revision import installed_ollama_revision
from mensa.maintenance import ensure_capacity, record_successful_release
from mensa.publication import validate_publication
from mensa.queue import exclusive_lock, load_state
from mensa.releases import DirectoryPublisher, _json_bytes, _release_id
from mensa.retention import retain_menu, public_translations, check_bundle_size
from mensa.retention_runtime import RetainedTranslationStore
from mensa.schedule import BERLIN
from scripts.notices import load_glossary
from scripts.update_menu import require_current_coverage


_STATE_FILES = ('queue.json', 'worker.lock', 'worker.toml', 'last-result.json',
                'translation-checkpoint.json', 'snapshot-journal.json', 'drive-publication.json',
                'translation-state.json', 'retention-releases.json')


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _directory(path, *, missing=False, private=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        raise ValueError(f'Required directory is missing: {path}') from None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'Expected a directory without a symlink: {path}')
    if private and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f'Existing state directory must have private permissions: {path}')
    return info.st_dev, info.st_ino


def _record(path, *, private=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Expected a regular file without a symlink: {path}')
    if private and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f'Existing state file must have private permissions: {path}')
    return info.st_dev, info.st_ino, info.st_mode, path.read_bytes()


def _log_identity(path, *, directory=False, dir_fd=None):
    try:
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError('Cannot inspect owned runtime log path') from None
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.geteuid()
            or (not directory and info.st_nlink != 1)):
        raise ValueError('Runtime logs must be owned directories or single-link regular files without symlinks')
    return info.st_dev, info.st_ino


@contextmanager
def _runtime_log_permissions(path):
    """Validate exact owned log targets before I/O; tighten held objects only after installation."""
    directory = model = None
    try:
        original = _log_identity(path, directory=True)
        model_identity = None
        if original is not None:
            try:
                directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                model_identity = _log_identity('model.log', dir_fd=directory)
                if model_identity is not None:
                    model = os.open('model.log', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=directory)
            except OSError:
                raise ValueError('Cannot open owned runtime log paths without following links') from None

        def check():
            if _log_identity(path, directory=True) != original:
                raise ValueError('Runtime log directory changed during Drive configuration')
            if directory is not None:
                info = os.fstat(directory)
                if (info.st_dev, info.st_ino) != original or info.st_uid != os.geteuid():
                    raise ValueError('Runtime log directory changed during Drive configuration')
                if _log_identity('model.log', dir_fd=directory) != model_identity:
                    raise ValueError('Runtime model log changed during Drive configuration')
                if model is not None:
                    info = os.fstat(model)
                    if ((info.st_dev, info.st_ino) != model_identity or not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.geteuid() or info.st_nlink != 1):
                        raise ValueError('Runtime model log changed during Drive configuration')

        def prepare():
            check()
            if directory is None:
                path.mkdir(mode=0o700)
            else:
                os.fchmod(directory, 0o700)
                if model is not None:
                    os.fchmod(model, 0o600)

        check()
        yield check, prepare
    finally:
        if model is not None:
            os.close(model)
        if directory is not None:
            os.close(directory)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON field in bootstrap state')
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError('Bootstrap state must contain finite JSON')


def _json(payload):
    try:
        return json.loads(payload, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Invalid JSON in bootstrap source or state') from None


def _source_pair(directory):
    values = []
    for name in ('menu.json', 'translations.json'):
        record = _record(directory / name)
        if record is None:
            raise ValueError(f'Explicit source data is missing {name}')
        values.append(_json(record[3]))
    return tuple(values)


def _validate_pair(pair, glossary, today):
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        raise ValueError('A complete menu and translation pair is required')
    menu, cache = copy.deepcopy(pair)
    validate_publication(menu, cache, notice_glossary=glossary, today=today)
    require_current_coverage(menu, today=today)
    payloads = _checked_pair(menu, cache, today=today)
    return (menu, cache), _release_id(*payloads)


def _expected_config(base, checkout, data_dir, remote, folder_id, file_id, model_revision):
    inference_paths = {name: (base / relative).resolve() for name, relative in (
        ('binary', 'runtime/ollama'), ('models_dir', 'models'), ('log_dir', 'logs'))}
    for name, path in inference_paths.items():
        if path.exists() and not (path.is_file() if name == 'binary' else path.is_dir()):
            raise ValueError(f'Existing inference path has the wrong type: {path}')
        if name == 'binary':
            if path == data_dir or data_dir in path.parents:
                raise ValueError('The runtime binary must remain outside public staging')
        elif any(_overlap(path, protected) for protected in (checkout, data_dir)):
            raise ValueError('Model and log paths must remain outside checkout and public staging')
    return WorkerConfig(
        paths=WorkerPaths(checkout, base, data_dir),
        inference=InferenceSettings('managed', 'ollama', 'gemma4:31b', model_revision,
                                    None, **inference_paths),
        resources=ResourceSettings('macos', 32 * 1024 ** 3, 0.6),
        publication=PublicationSettings('drive', None, None, remote, folder_id, file_id),
    )


def _never_git(*_):
    raise RuntimeError('Bootstrap must not execute Git')


def _inspect_state(base, checkout, data_dir, remote, folder_id, model_revision):
    records = {name: _record(base / name, private=name in {'worker.toml', 'snapshot-journal.json'})
               for name in _STATE_FILES}
    state = load_state(base / 'queue.json')
    if state['pending'] is not None:
        raise ValueError('The existing refresh queue must be idle before Drive configuration')
    if records['translation-checkpoint.json'] is not None:
        _json(records['translation-checkpoint.json'][3])
    TranslationCheckpointStore(base).load()
    journal = records['snapshot-journal.json']
    if journal is not None:
        if len(journal[3]) > MAX_JOURNAL_BYTES:
            raise ValueError('Existing snapshot recovery journal is oversized')
        value = _json(journal[3])
        recorded_checkout = value.get('checkout') if isinstance(value, dict) else None
        if not isinstance(recorded_checkout, str) or not Path(recorded_checkout).is_absolute():
            raise ValueError('Existing snapshot recovery journal has no absolute checkout')
        SnapshotRepository(Path(recorded_checkout), base, git=_never_git)._validate(value)
        if value['replacement'] is not None or (value['snapshot'] is not None and value['snapshot']['sha'] is None):
            raise ValueError('Unfinished snapshot recovery must be resolved before Drive configuration')
    last_result = records['last-result.json']
    if last_result is not None:
        value = _json(last_result[3])
        if not isinstance(value, dict) or not isinstance(value.get('status'), str):
            raise ValueError('Invalid previous worker result')
    prepared_record = records['drive-publication.json']
    prepared = None if prepared_record is None else _PublicationCheckpoint._validate(_json(prepared_record[3]))
    if prepared is not None and (prepared['remote'] != remote or prepared['folder_id'] != folder_id):
        raise ValueError('Existing Drive preparation belongs to another destination')
    config = None
    if records['worker.toml'] is not None:
        config = load_worker_config(base / 'worker.toml')
        expected = _expected_config(base, checkout, data_dir, remote, folder_id,
                                    config.publication.file_id, model_revision)
        if config != expected:
            raise ValueError('Existing worker config conflicts with the requested Drive configuration')
    file_id = None if config is None else config.publication.file_id
    if prepared is not None:
        if file_id is not None and file_id != prepared['file_id']:
            raise ValueError('Existing Drive preparation file ID differs from worker config')
        file_id = prepared['file_id']
    # DrivePublisher 생성은 원격 동작 없이 연결 이름과 선택적 파일 식별자를 검증합니다.
    DrivePublisher(remote, folder_id, file_id=file_id)
    return records, file_id, prepared


def _assert_unchanged(base, records):
    for name, original in records.items():
        current = _record(base / name, private=name in {'worker.toml', 'snapshot-journal.json'})
        if current != original:
            raise ValueError(f'Existing {name} changed during Drive configuration')


def _pair_identity(pair):
    return None if pair is None else _release_id(*(_json_bytes(value) for value in pair))


def _toml(config):
    sections = (
        ('paths', {'checkout_dir': str(config.paths.checkout_dir), 'state_dir': str(config.paths.state_dir),
                   'public_dir': str(config.paths.public_dir)}),
        ('inference', {'mode': config.inference.mode, 'provider': config.inference.provider,
                       'model': config.inference.model, 'revision': config.inference.revision,
                       'binary': str(config.inference.binary), 'models_dir': str(config.inference.models_dir),
                       'log_dir': str(config.inference.log_dir)}),
        ('resources', {'platform': config.resources.platform,
                       'min_available_bytes': config.resources.min_available_bytes,
                       'max_load_per_cpu': config.resources.max_load_per_cpu}),
        ('publication', {'mode': 'drive', 'remote': config.publication.remote,
                         'folder_id': config.publication.folder_id, 'file_id': config.publication.file_id}),
        ('retention', asdict(config.retention)),
    )
    return '\n\n'.join(f'[{name}]\n' + '\n'.join(
        f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in values.items()
    ) for name, values in sections) + '\n'


def _install_config(base, expected, records):
    if records['worker.toml'] is not None:
        _assert_unchanged(base, records)
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.worker-', suffix='.toml', dir=base)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(_toml(expected))
            stream.flush()
            os.fsync(stream.fileno())
        if load_worker_config(Path(temporary)) != expected:
            raise ValueError('Prepared worker config differs from its intended values')
        _assert_unchanged(base, records)
        # 같은 디렉터리의 완성된 파일을 링크하면 목적지가 새로 생긴 경우 덮어쓰지 않고 실패합니다.
        os.link(temporary, base / 'worker.toml', follow_symlinks=False)
        directory = os.open(base, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(base, checkout, data_dir, remote, folder_id, *, source_data=None,
              model_revision=None, publisher=None):
    """configure가 명시적인 원본 또는 Drive 묶음으로 준비 경로를 채우고 새 worker.toml의 경로·공개 식별자를 반환합니다.

    publisher는 DrivePublisher와 같은 인자를 받아 load_current/publish 포트를 반환하는
    선택적 팩터리입니다. 기존 큐·체크포인트·복구·모델·로그·설정은 덮어쓰지 않습니다.
    model_revision이 없으면 설치된 Ollama manifest의 digest를 읽으며 모델을 실행하지 않습니다.
    """
    base, checkout, data_dir = (Path(path).expanduser().resolve() for path in (base, checkout, data_dir))
    for left, right in ((base, checkout), (base, data_dir), (checkout, data_dir)):
        if _overlap(left, right):
            raise ValueError('State, checkout and public staging paths must be disjoint')
    _directory(base.parent, missing=True)
    _directory(base, missing=True, private=True)
    _directory(checkout)
    _directory(data_dir.parent, missing=True)
    _directory(data_dir, missing=True)
    if publisher is not None and not callable(publisher):
        raise ValueError('publisher must be a callable Drive publisher factory')
    DrivePublisher(remote, folder_id)
    if model_revision is not None and (not isinstance(model_revision, str) or not model_revision.strip()
                                      or len(model_revision) > 256
                                      or any(ord(character) < 33 for character in model_revision)):
        raise ValueError('--model-revision must be the exact nonempty /api/tags digest')
    def revision():
        if model_revision is not None:
            return model_revision
        return installed_ollama_revision((base / 'models').resolve(), 'gemma4:31b')
    template = _expected_config(base, checkout, data_dir, remote, folder_id,
                                'bootstrap_pending_file', revision())
    today = datetime.now(BERLIN).date().isoformat()
    glossary = load_glossary(checkout / 'data/notice-translations.json')
    pair = None
    private_pair = None
    source_release_id = None
    release_id = None
    if source_data is not None:
        source_data = Path(source_data).expanduser().resolve()
        if _overlap(source_data, data_dir):
            raise ValueError('Explicit source data and public staging must be disjoint')
        _directory(source_data)
        private_pair, source_release_id = _validate_pair(_source_pair(source_data), glossary, today)
        menu = retain_menu(private_pair[0], today, template.retention)
        pair = (menu, public_translations(menu, private_pair[1]))
        release_id = _pair_identity(pair)
    local = DirectoryPublisher(data_dir, notice_glossary=glossary)
    current = local.load_current()
    selected = _pair_identity(current)
    if pair is not None and selected is not None and selected != release_id:
        raise ValueError('Existing local staging selects different data')
    initial_records, _, _ = _inspect_state(base, checkout, data_dir, remote, folder_id,
                                          template.inference.revision)
    if not base.exists():
        base.mkdir(mode=0o700, parents=True)
    with exclusive_lock(base / 'worker.lock'), _runtime_log_permissions(base / 'logs') as (check_logs, prepare_logs):
        records, expected_id, prepared = _inspect_state(base, checkout, data_dir, remote, folder_id,
                                                       template.inference.revision)
        # 잠금을 처음 만드는 경우에만 새 빈 worker.lock을 허용하며 기존 기록은 모두 비교합니다.
        for name, original in initial_records.items():
            if name != 'worker.lock' or original is not None:
                if records[name] != original:
                    raise ValueError(f'Existing {name} changed before the queue lock was acquired')
        destinations = {path: _directory(path, missing=True, private=path == base)
                        for path in (base, checkout, data_dir, data_dir.parent)}
        if pair is not None and prepared is not None and prepared['release_id'] != release_id:
            raise ValueError('Existing Drive preparation contains a different release')
        factory = DrivePublisher if publisher is None else publisher
        drive = factory(remote, folder_id, file_id=expected_id)
        if not callable(getattr(drive, 'load_current', None)) or not callable(getattr(drive, 'publish', None)):
            raise ValueError('Drive publisher must provide load_current and publish methods')
        observed = drive.load_current()
        if pair is None:
            if observed is None:
                raise ValueError('Drive has no current data; provide an explicit --source-data directory')
            private_pair, source_release_id = _validate_pair(observed, glossary, today)
            menu = retain_menu(private_pair[0], today, template.retention)
            pair = (menu, public_translations(menu, private_pair[1]))
            release_id = _pair_identity(pair)
        elif observed is not None and _pair_identity(observed) not in (release_id, source_release_id):
            raise ValueError('Existing Drive publication contains different data')
        if selected is not None and selected != release_id:
            raise ValueError('Existing local staging selects different data')
        if prepared is not None and prepared['release_id'] != release_id:
            raise ValueError('Existing Drive preparation contains a different release')
        private_store = RetainedTranslationStore(template, menu=pair[0], observed=private_pair[0], today=today)
        reuse = private_store.merge(private_pair[1], published_menu=private_pair[0], glossary=glossary)
        size = check_bundle_size(*pair, template.retention)
        ensure_capacity(template, additional_bytes=size + len(_json_bytes(reuse)) * 2 + 8192)
        result = drive.publish(*pair, today=today)
        if result.release_id != release_id or (expected_id is not None and result.file_id != expected_id):
            raise ValueError('Drive publication confirmation differs from the intended data or destination')
        DrivePublisher(remote, folder_id, file_id=result.file_id)
        _assert_unchanged(base, records)
        for path, identity in destinations.items():
            if path.resolve() != path or _directory(path, missing=True, private=path == base) != identity:
                raise ValueError('A configured destination changed during Drive configuration')
        check_logs()
        expected = _expected_config(base, checkout, data_dir, remote, folder_id, result.file_id, revision())
        if expected.inference != template.inference:
            raise ValueError('An inference destination changed during Drive configuration')
        if _pair_identity(local.load_current()) != selected:
            raise ValueError('Existing local staging changed during Drive configuration')
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        if local.publish(*pair, today=today) != release_id or _pair_identity(local.load_current()) != release_id:
            raise ValueError('Local staging verification differs from the Drive release')
        _install_config(base, expected, records)
        private_store.save(reuse)
        record_successful_release(expected, release_id, today=datetime.now(BERLIN).date())
        prepare_logs()
        return {'config_path': str(base / 'worker.toml'), 'file_id': result.file_id,
                'release_id': release_id, 'data_dir': str(data_dir)}


def main(argv=None):
    """main이 필수 설정 인자를 해석하고 성공 결과를 JSON으로 출력하며 실패 시 1을 반환합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'checkout', 'data-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--folder-id', required=True)
    parser.add_argument('--source-data', type=Path)
    parser.add_argument('--model-revision', help='Exact /api/tags digest; defaults to the installed Ollama manifest digest')
    args = parser.parse_args(argv)
    try:
        result = configure(args.base, args.checkout, args.data_dir, args.remote, args.folder_id,
                           source_data=args.source_data, model_revision=args.model_revision)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Drive configuration failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
