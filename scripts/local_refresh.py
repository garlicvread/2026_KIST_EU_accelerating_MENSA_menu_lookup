"""Durable local refresh worker. Model inference exists only for one queued job."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

from scripts.menu_source import fetch_html, parse_menu, validate_menu
from scripts.translations import build_translations, validate_cache, PROMPT_VERSION
from scripts.update_menu import read_json, require_current_coverage, write_snapshot, summarize
from scripts.refresh_queue import (load_state, save_state, reconcile, failed, completed,
                                   exclusive_lock, resource_status, BusyError, BERLIN)

BASE = Path.home() / 'Library/Application Support/Mensa'
REPOSITORY = 'garlicvread/2026_KIST_EU_accelerating_MENSA_menu_lookup'
REMOTE = f'https://github.com/{REPOSITORY}.git'
MODEL = 'gemma4:31b'
DATA_FILES = ['site/data/menu.json', 'site/data/translations.json']


def record_result(directory, result, now):
    """Publish an owned worker result while worker.lock is still held."""
    target = directory / 'last-result.json'
    descriptor, name = tempfile.mkstemp(prefix='.last-result-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'at': now.isoformat(), **result}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return result


def process_queue(directory, now, resources, work):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'queue.json'
    try:
        with exclusive_lock(directory / 'worker.lock'):
            started = time.monotonic()
            def finish(result):
                return record_result(directory, result, now + timedelta(seconds=time.monotonic() - started))
            state = reconcile(load_state(path), now)
            save_state(path, state)
            pending = state['pending']
            if not pending:
                return finish({'status': 'idle', 'completed_period': state['completed_period']})
            retry = pending['next_attempt_at']
            if retry and datetime.fromisoformat(retry) > now:
                return finish({'status': 'waiting', 'retry_at': retry})
            ready, reason = resources()
            if not ready:
                return finish({'status': 'deferred', 'reason': reason, 'period': pending['period']})
            try:
                work(state)
            except Exception as exc:
                # Reload durable checkpoints: publication may have advanced before failure.
                failed_at = now + timedelta(seconds=time.monotonic() - started)
                state = failed(load_state(path), str(exc)[:500], failed_at)
                save_state(path, state)
                return finish({'status': 'failed', 'reason': str(exc)[:500], 'period': state['pending']['period']})
            state = completed(load_state(path))
            save_state(path, state)
            return finish({'status': 'completed', 'period': state['completed_period']})
    except BusyError:
        return {'status': 'busy', 'reason': 'Another refresh worker holds the queue lock'}


def translation_needed(menu, cache):
    for day in menu['days']:
        for meal in day['meals']:
            entry = cache['entries'].get(meal['translation_key'])
            if not entry or entry['prompt_version'] != PROMPT_VERSION:
                return True
            if entry['origin'] == 'model' and entry['model'] != MODEL:
                return True
    return False


def command(args, cwd=None, timeout=120):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Commands do not receive credentials as arguments; keep output bounded.
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()[-350:]}')
    return result.stdout.rstrip('\n')


def stop_model(process):
    """Reap the leader and terminate its owned group, including orphaned runners."""
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # The leader may exit before its descendants. Always finish group cleanup.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def local_model(base):
    binary = base / 'runtime' / 'ollama'
    if not binary.is_file():
        raise ValueError('Local Ollama binary is missing; restore the Mensa runtime')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    host = f'http://127.0.0.1:{port}'
    env = dict(os.environ, OLLAMA_HOST=f'127.0.0.1:{port}',
               OLLAMA_MODELS=str(base / 'models'), OLLAMA_NO_CLOUD='1',
               OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1')
    logs = base / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / 'model.log').open('w') as log:
        process = subprocess.Popen([str(binary), 'serve'], env=env, stdout=log, stderr=log,
                                   start_new_session=True)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError('Local Ollama stopped during startup; see model.log')
                try:
                    with opener.open(host + '/api/tags', timeout=1) as response:
                        models = json.load(response)['models']
                    if not any(m['name'] == MODEL for m in models):
                        raise ValueError(f'{MODEL} is not installed in the Mensa model directory')
                    break
                except (OSError, TimeoutError):
                    time.sleep(.25)
            else:
                raise RuntimeError('Local Ollama did not become ready within 30 seconds')
            yield {'provider': 'ollama', 'url': host + '/api/chat', 'model': MODEL, 'key': ''}
        finally:
            # Terminate this process group only; never stop an unrelated Ollama server.
            stop_model(process)


class RefreshJob:
    def __init__(self, base):
        self.base = Path(base)
        self.repo = self.base / 'checkout'
        self.state_path = self.base / 'queue.json'

    def git(self, *args):
        return command(['git', *args], self.repo)

    def gh(self, *args):
        return command(['gh', *args, '--repo', REPOSITORY], self.repo)

    def checkpoint(self, state):
        save_state(self.state_path, state)

    def check_repository(self, allow_snapshots=False, recover_staging=False):
        if self.git('remote', 'get-url', 'origin') != REMOTE:
            raise ValueError('Worker checkout has an unexpected Git remote')
        if self.git('branch', '--show-current') != 'main':
            raise ValueError('Worker checkout must be on main')
        if recover_staging:
            # write_snapshot reserves this prefix for disposable staging files.
            # A hard kill skips its finally block. Remove only regular, untracked
            # files in this exact directory; never follow a symlink or touch Git data.
            for path in (self.repo / 'site/data').glob('.pending-*'):
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.repo).as_posix()
                if not self.git('ls-files', '--', relative):
                    path.unlink()
        changed = self.git('status', '--porcelain').splitlines()
        if changed and (not allow_snapshots or any(line[3:] not in DATA_FILES for line in changed)):
            raise ValueError('Worker checkout has unrelated or unfinished changes; inspect before retry')

    def collect(self):
        # This dedicated checkout owns only generated snapshots. A killed collection
        # can leave those files ahead of its queue checkpoint; restore and recollect.
        # The allow-list check runs first, so unrelated files are never discarded.
        self.check_repository(allow_snapshots=True, recover_staging=True)
        if self.git('status', '--porcelain'):
            self.git('restore', '--source=HEAD', '--staged', '--worktree', '--', *DATA_FILES)
        self.git('fetch', 'origin', 'main')
        self.git('merge', '--ff-only', 'origin/main')
        directory = self.repo / 'site/data'
        previous = read_json(directory / 'menu.json')
        cache = read_json(directory / 'translations.json')
        phrases = read_json(self.repo / 'data/editorial-translations.json')['phrases']
        menu = parse_menu(fetch_html())
        validate_menu(menu, previous=previous)
        require_current_coverage(menu)
        candidate = build_translations(menu, cache, phrases, None)
        if translation_needed(menu, candidate):
            with local_model(self.base) as config:
                candidate = build_translations(menu, candidate, phrases, config)
        self.validate_complete(menu, candidate)
        write_snapshot(directory, menu, candidate, previous)
        return summarize(menu, candidate)

    @staticmethod
    def validate_complete(menu, cache):
        validate_menu(menu)
        validate_cache(cache)
        if any(m['translation_key'] not in cache['entries'] for d in menu['days'] for m in d['meals']):
            raise ValueError('A menu translation is missing; refusing partial publication')

    def find_run(self, sha):
        runs = json.loads(self.gh('run', 'list', '--workflow', 'update-and-deploy.yml',
                                  '--event', 'workflow_dispatch', '--limit', '50',
                                  '--json', 'databaseId,headSha,displayTitle,status,conclusion'))
        # The workflow checks out snapshot_sha; its own definition may be newer.
        return next((r for r in runs if r['displayTitle'] == f'Publish menu {sha}'), None)

    def read_run(self, run_id, sha):
        run = json.loads(self.gh('run', 'view', str(run_id),
                                '--json', 'databaseId,displayTitle,status,conclusion'))
        if run['displayTitle'] != f'Publish menu {sha}':
            raise ValueError('Deployment run does not match the checkpoint snapshot')
        return run

    def publish(self, state):
        pending = state['pending']
        self.check_repository(allow_snapshots=True)
        directory = self.repo / 'site/data'
        menu = read_json(directory / 'menu.json')
        self.validate_complete(menu, read_json(directory / 'translations.json'))
        if not pending['commit_sha']:
            self.git('add', '--', *DATA_FILES)
            if self.git('diff', '--cached', '--name-only'):
                self.git('commit', '-m', 'Refresh menu with locally validated translations')
            pending['commit_sha'] = self.git('rev-parse', 'HEAD')
            self.checkpoint(state)
        sha = pending['commit_sha']
        if self.git('rev-parse', 'HEAD') != sha:
            raise ValueError('Local checkout moved since publication checkpoint; refusing a different snapshot')
        run = None
        if pending['run_id']:
            run = self.read_run(pending['run_id'], sha)
        else:
            run = self.find_run(sha)
        if not (run and run['status'] == 'completed' and run['conclusion'] == 'success'):
            # A laptop may have slept beyond this snapshot's coverage. Never start,
            # rerun or watch publication of expired data, even across midnight.
            require_current_coverage(menu, today=datetime.now(timezone.utc).astimezone(BERLIN).date().isoformat())
        if run is None:
            self.git('fetch', 'origin', 'main')
            # A previous push can have succeeded before dispatch/checkpoint failed.
            # A newer remote descendant does not need (and may reject) another push.
            if self.git('merge-base', sha, 'origin/main') != sha:
                self.git('push', 'origin', 'HEAD:main')
            requested = pending.get('dispatch_requested_at')
            now = datetime.now(timezone.utc)
            if requested and now - datetime.fromisoformat(requested) < timedelta(minutes=30):
                raise RuntimeError('Deployment dispatch is not visible yet; waiting before any retry')
            pending['dispatch_requested_at'] = now.isoformat()
            self.checkpoint(state)
            self.gh('workflow', 'run', 'update-and-deploy.yml', '--ref', 'main', '-f', f'snapshot_sha={sha}')
            for _ in range(12):
                run = self.find_run(sha)
                if run:
                    break
                time.sleep(2)
            if run is None:
                raise RuntimeError('Deployment requested; its run is not visible yet')
        pending['run_id'] = run['databaseId']
        self.checkpoint(state)
        if run['status'] == 'completed' and run['conclusion'] != 'success':
            flags = [] if run['conclusion'] == 'cancelled' else ['--failed']
            self.gh('run', 'rerun', str(run['databaseId']), *flags)
            # Keep this run ID; process_queue applies backoff before polling it again.
            raise RuntimeError(f"Deployment run {pending['run_id']} rerun requested; waiting for the next retry")
        if run['status'] != 'completed':
            command(['gh', 'run', 'watch', str(run['databaseId']), '--exit-status', '--interval', '15',
                     '--repo', REPOSITORY], self.repo, timeout=600)
            run = self.read_run(run['databaseId'], sha)
        if run['conclusion'] != 'success':
            raise RuntimeError(f"Deployment run {pending['run_id']} failed; it will be retried after backoff")

    def __call__(self, state):
        pending = state['pending']
        if pending['phase'] == 'publish':
            menu = read_json(self.repo / 'site/data/menu.json')
            now = datetime.now(timezone.utc)
            if menu['coverage']['end'] < now.astimezone(BERLIN).date().isoformat():
                sha = pending['commit_sha']
                run = self.read_run(pending['run_id'], sha) if pending['run_id'] else self.find_run(sha) if sha else None
                if run and run['status'] == 'completed' and run['conclusion'] == 'success':
                    # Acknowledge only this original period; the next queue tick
                    # independently creates the newest due period.
                    return
                if run and run['status'] != 'completed':
                    raise RuntimeError('Expired deployment run is still active; waiting before collecting its replacement')
                # Keep old commits as history. Only reset queue identifiers, then
                # let normal collection fast-forward and create a current snapshot.
                pending.update(phase='collect', attempts=0, next_attempt_at=None, last_error=None,
                               commit_sha=None, run_id=None, dispatch_requested_at=None)
                state['pending'] = reconcile(state, now)['pending']
                self.checkpoint(state)
        if state['pending']['phase'] == 'collect':
            self.collect()
            # One collection attempt per invocation; stale results cannot recurse
            # into another collection or become a publish checkpoint.
            require_current_coverage(read_json(self.repo / 'site/data/menu.json'),
                                     today=datetime.now(timezone.utc).astimezone(BERLIN).date().isoformat())
            state['pending']['phase'] = 'publish'
            self.checkpoint(state)
        self.publish(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=BASE)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.status:
        try:
            last_result = json.loads((args.base / 'last-result.json').read_text())
        except FileNotFoundError:
            last_result = None
        print(json.dumps({'queue': load_state(args.base / 'queue.json'), 'last_result': last_result},
                         ensure_ascii=False, indent=2))
        return
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        result = process_queue(args.base, datetime.now(timezone.utc), resource_status, RefreshJob(args.base))
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] == 'failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
