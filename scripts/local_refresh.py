"""큐에 저장된 식단 갱신 작업 하나를 수집·번역·공개하는 명령 진입점입니다.

--base 경로는 macOS 자원 조건과 전용 GitHub 체크아웃을 사용하고, --config 경로는
TOML의 자원·모델·공개 설정을 사용합니다. JobRunner가 상태 디렉터리 준비, 큐 잠금,
완료·실패·재시도 기록과 last-result.json 저장을 담당합니다. 이 모듈의 작업 객체는
수집과 공개 진행을 수행하며, 재개에 필요한 큐 체크포인트를 잠금 안에서 저장합니다.
"""

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from mensa.jobs import JobRunner, record_result
from mensa.git_repository import SnapshotRepository
from mensa.github_publication import GitHubPublication
from mensa.directory_refresh import DirectoryRefreshJob
from mensa.config import load_worker_config
from mensa.generation import request_translation
from mensa.model_runtime import model_session
from mensa.resources import resource_status as configured_resource_status
from mensa.translation_runtime import resume_configured_translations
from mensa.model_runtime import stop_model
from mensa.publication import validate_publication
from mensa.refresh_service import RefreshService
from scripts.menu_source import fetch_html, parse_menu, validate_menu
from scripts.notices import load_glossary
from scripts.translations import build_translations, reusable_translation
from scripts.update_menu import read_json, require_current_coverage, write_snapshot, summarize
from scripts.refresh_queue import load_state, save_state, reconcile, resource_status, BERLIN

BASE = Path.home() / 'Library/Application Support/Mensa'
REPOSITORY = 'garlicvread/2026_KIST_EU_accelerating_MENSA_menu_lookup'
REMOTE = f'https://github.com/{REPOSITORY}.git'
WORKFLOW = 'update-and-deploy.yml'
MODEL = 'gemma4:31b'
DATA_FILES = ['site/data/menu.json', 'site/data/translations.json']


def process_queue(directory, now, resources, work):
    """상태 경로·시간대가 있는 시각·자원 검사·작업 함수를 JobRunner에 전달하고 결과 사전을 반환합니다.

    JobRunner는 잠금을 얻은 뒤 큐를 정리·저장하고 실행 조건을 검사합니다. 작업의 실패
    처리와 완료 기록, 결과 파일 교체도 JobRunner가 수행하며 이 함수는 잠금을 별도로
    얻지 않습니다. 다른 작업자가 잠금을 보유하면 기다리지 않고 busy 결과를 반환합니다.
    """
    return JobRunner(Path(directory).absolute(), resources=resources, work=work).run(now)


def translation_needed(menu, cache):
    """메뉴에 기본 MODEL로 재사용할 수 없는 번역 항목이 하나라도 있는지 반환합니다.

    기본 --base 실행에서 모델 시작 여부를 정하는 검사이며, 번역의 의미적 정확성이나
    TOML에 선언된 모델 리비전을 확인하는 검사는 아닙니다.
    """
    for day in menu['days']:
        for meal in day['meals']:
            entry = cache['entries'].get(meal['translation_key'])
            if not reusable_translation(entry, MODEL):
                return True
    return False


def command(args, cwd=None, timeout=120):
    """args 명령을 cwd에서 제한 시간 안에 실행하고 끝의 줄바꿈을 제거한 표준 출력을 반환합니다.

    이 함수가 실행한 Git·gh 명령은 파일·원격 저장소·워크플로 상태를 변경할 수 있습니다.
    실패한 종료 코드는 제한된 표준 오류를 포함한 RuntimeError로 알리고, 시간 초과 등의
    실행 오류는 호출자에게 전달하여 JobRunner가 실패와 재시도 시점을 기록하도록 합니다.
    """
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # command가 실행하는 명령은 인자로 자격 증명을 받지 않으며, 오류에 포함할 출력 길이를 제한합니다.
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()[-350:]}')
    return result.stdout.rstrip('\n')


@contextmanager
def local_model(base):
    """기본 --base 경로의 Ollama를 시작하고 생성 요청용 접속 설정을 잠시 제공합니다.

    base의 runtime/ollama와 models를 사용하고, logs/model.log를 새로 쓰며 루프백의
    /api/tags로 기본 MODEL이 설치되어 있는지 확인합니다. 모델을 내려받지는 않습니다.
    호출자가 컨텍스트를 나가면 성공·실패와 관계없이 stop_model로 이 함수가 시작한
    프로세스 그룹을 정리합니다. 모델이 필요한지 판단하는 책임은 translate에 있습니다.
    """
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
            # local_model은 자신이 시작한 프로세스 그룹만 종료하며, 관련 없는 Ollama 서버는 중지하지 않습니다.
            stop_model(process)


class RefreshJob:
    """기본 --base 실행의 전용 체크아웃에서 메뉴 JSON을 만들고 GitHub 공개를 재개합니다.

    base/checkout에서 Git을 실행하고 base/queue.json에 공개 단계·커밋 SHA·실행 ID를
    저장합니다. git·gh·watch_run은 이 체크아웃과 지정된 저장소에 명령을 묶어 제공합니다.
    저장소·원격·워크플로 속성은 공개 대상을 지정하며, 객체를 만들 때 명령을 실행하지는
    않습니다. SnapshotRepository가 스냅샷 커밋과 교체 저널을 관리하고 GitHubPublication이
    워크플로 조회·요청을 수행합니다. 호출자는 JobRunner의 큐 잠금을 유지해야 합니다.
    """

    def __init__(self, base):
        self.base = Path(base)
        self.repo = self.base / 'checkout'
        self.state_path = self.base / 'queue.json'

    @property
    def repository_name(self):
        return REPOSITORY

    @property
    def expected_remote(self):
        return REMOTE

    @property
    def workflow_name(self):
        return WORKFLOW

    def publication_transport(self):
        """저장소에 묶인 gh·실행 대기 함수로 GitHubPublication을 구성하며 요청은 아직 보내지 않습니다."""
        return GitHubPublication(self.workflow_name, gh=self.gh, watch=self.watch_run)

    def watch_run(self, run_id):
        """지정한 GitHub 실행을 gh로 최대 600초 동안 기다리고 성공하지 못하면 오류를 전달합니다."""
        return command(['gh', 'run', 'watch', str(run_id), '--exit-status', '--interval', '15',
                        '--repo', self.repository_name], self.repo, timeout=600)

    def git(self, *args):
        return command(['git', *args], self.repo)

    def gh(self, *args):
        return command(['gh', *args, '--repo', self.repository_name], self.repo)

    def checkpoint(self, state):
        """진행 중인 state를 queue.json에 교체 저장하며, 호출자가 보유한 큐 잠금을 사용합니다."""
        save_state(self.state_path, state)

    def snapshot_repository(self):
        """체크아웃·상태 경로·Git 함수를 SnapshotRepository에 연결하며 저널 저장은 해당 객체가 담당합니다."""
        return SnapshotRepository(self.repo.absolute(), self.state_path.parent.absolute(), git=self.git)

    def replace_snapshot(self, state, repository, resumed=None):
        """기록된 스냅샷 교체를 재개하고 검증된 새 커밋으로 공개 큐를 갱신합니다.

        resumed가 없으면 repository의 저널에서 재개 단계를 읽습니다. 재수집이 필요한
        단계에서는 collect를 실행하고, 준비된 단계에서는 기록된 SHA를 사용합니다.
        만료 여부와 번역 완결성을 검사한 뒤 교체 대상 주기·SHA를 저널에 기록하고 큐를
        저장합니다. repository가 교체 완료를 기록하는 것은 큐 체크포인트 저장 이후입니다.
        """
        pending = state['pending']
        original_period = pending['period']
        resumed = resumed or repository.resume_replacement(original_period, pending['commit_sha'])
        if resumed['status'] == 'collecting':
            self.collect()
            self._collected_this_call = True
            sha = repository.prepare_snapshot(original_period)
        else:
            sha = resumed['sha']
        menu = read_json(self.repo / 'site/data/menu.json')
        if menu['coverage']['end'] < datetime.now(timezone.utc).astimezone(BERLIN).date().isoformat():
            raise ValueError('Prepared replacement is expired; retain it for inspection')
        self.validate_complete(menu, read_json(self.repo / 'site/data/translations.json'))
        if resumed.get('period') is None:
            # 재수집 중 갱신 대상 주기가 바뀔 수 있으므로 reconcile로 공개할 주기를 다시
            # 정합니다. 저널에 그 주기를 먼저 남겨 큐 저장이 중단되어도 같은 교체를 재개합니다.
            intended = copy.deepcopy(state)
            intended['pending'].update(phase='collect', commit_sha=None, run_id=None, dispatch_requested_at=None)
            intended = reconcile(intended, datetime.now(timezone.utc))
            if intended['pending'] is None:
                raise ValueError('Replacement has no pending due period; retain it for inspection')
            intended['pending'].update(phase='publish', commit_sha=sha)
            repository.record_replacement(original_period, sha, intended['pending']['period'])
            state.update(intended)
        else:
            pending.update(period=resumed['period'], phase='publish', commit_sha=sha,
                           run_id=None, dispatch_requested_at=None)
        self.checkpoint(state)
        repository.complete_replacement(state['pending']['period'], sha)

    def check_repository(self, allow_snapshots=False, recover_staging=False):
        """전용 체크아웃의 원격·main 브랜치·변경 경로를 검사하고 허용 범위 밖의 상태를 거부합니다.

        allow_snapshots가 참이면 두 DATA_FILES의 변경만 허용합니다. recover_staging이
        참이면 site/data의 추적되지 않는 .pending-* 일반 파일을 삭제합니다. 이 정리는
        위치·파일 종류·이름을 기준으로 수행하며, 파일을 누가 만들었는지는 확인하지
        않습니다. 다른 변경은 자동 복구하지 않습니다.
        """
        if self.git('remote', 'get-url', 'origin') != self.expected_remote:
            raise ValueError('Worker checkout has an unexpected Git remote')
        if self.git('branch', '--show-current') != 'main':
            raise ValueError('Worker checkout must be on main')
        if recover_staging:
            # write_snapshot은 이 접두사를 삭제 가능한 임시 저장 파일에만 사용합니다.
            # 강제 종료되면 write_snapshot의 finally가 실행되지 않습니다. check_repository는 이 디렉터리의
            # 추적되지 않는 일반 파일만 삭제하며, 심볼릭 링크를 따라가거나 Git 데이터를 수정하지 않습니다.
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
        """체크아웃을 갱신하여 식단을 수집·번역·검증하고 두 공개 JSON 파일을 저장한 요약을 반환합니다.

        허용된 미완성 스냅샷 변경을 복원한 뒤 origin/main을 가져와 빨리 감기로 합칩니다.
        기존 메뉴·번역·편집 문구를 파일에서 읽고 RefreshService에 수집·번역·검증·저장
        함수를 연결합니다. 스냅샷 커밋과 GitHub 공개 요청은 publish가 수행합니다.
        """
        # 이 전용 체크아웃은 생성된 메뉴 스냅샷만 관리합니다. 수집 중 강제 종료되면 파일이
        # 큐 체크포인트보다 앞설 수 있으므로 collect가 해당 파일을 복원하고 다시 수집합니다.
        # collect가 먼저 허용 경로 목록을 검사하므로 관련 없는 파일은 버리지 않습니다.
        self.check_repository(allow_snapshots=True, recover_staging=True)
        if self.git('status', '--porcelain'):
            self.git('restore', '--source=HEAD', '--staged', '--worktree', '--', *DATA_FILES)
        self.git('fetch', 'origin', 'main')
        self.git('merge', '--ff-only', 'origin/main')
        directory = self.repo / 'site/data'
        previous = read_json(directory / 'menu.json')
        cache = read_json(directory / 'translations.json')
        phrases = read_json(self.repo / 'data/editorial-translations.json')['phrases']
        service = RefreshService(
            source=lambda: parse_menu(fetch_html()),
            coverage_guard=require_current_coverage,
            translate=self.translate,
            validator=self.validate_complete,
            publisher=lambda menu, candidate, previous: write_snapshot(directory, menu, candidate, previous),
        )
        menu, candidate = service.refresh(previous, cache, phrases)
        return summarize(menu, candidate)

    def translate(self, menu, cache, phrases, *, checkpoint=None):
        """편집 문구와 기존 캐시로 번역을 먼저 구성하고 부족한 항목에만 기본 로컬 모델을 사용합니다.

        반환값은 번역 캐시이며 checkpoint는 새 항목의 캐시 복사본을 받을 수 있습니다.
        이 기본 경로는 비공개 번역 이력 파일을 직접 저장하지 않습니다. local_model의
        컨텍스트가 끝나면 모델 프로세스를 정리한 뒤 캐시를 반환합니다.
        """
        candidate = build_translations(menu, cache, phrases, None, checkpoint=checkpoint)
        if translation_needed(menu, candidate):
            with local_model(self.base) as config:
                candidate = build_translations(menu, candidate, phrases, config, checkpoint=checkpoint)
        return candidate

    @staticmethod
    def validate_complete(menu, cache):
        """기본 안내 용어집 파일을 읽고 메뉴·번역 캐시가 공개 조건을 충족하는지 검사합니다."""
        validate_publication(menu, cache, notice_glossary=load_glossary())

    def find_run(self, sha):
        """지정한 스냅샷 SHA의 공개 실행을 GitHub에서 조회하여 실행 정보 또는 None을 반환합니다."""
        return self.publication_transport().find_run(sha)

    def read_run(self, run_id, sha):
        """GitHub 실행 ID를 조회하고 해당 실행이 큐에 기록된 스냅샷 SHA의 공개인지 확인합니다."""
        return self.publication_transport().read_run(run_id, sha)

    def publish(self, state):
        """공개 단계의 메뉴·캐시를 검증하고 커밋 준비·push·워크플로 요청·성공 확인을 진행합니다.

        state의 pending을 수정하고 SHA와 요청 시각을 queue.json에 저장합니다. 재시도에서는
        기록된 실행을 우선 확인하며, 원격 변경으로 교체가 필요하면 SnapshotRepository를
        통해 재수집을 준비합니다. 반환 전에 confirm_run으로 성공을 확인하지만, 큐의
        completed_period와 최종 실행 결과 파일은 JobRunner가 기록합니다.
        """
        pending = state['pending']
        self.check_repository(allow_snapshots=True)
        directory = self.repo / 'site/data'
        menu = read_json(directory / 'menu.json')
        self.validate_complete(menu, read_json(directory / 'translations.json'))
        if not pending['commit_sha']:
            pending['commit_sha'] = self.snapshot_repository().prepare_snapshot(pending['period'])
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
            # 노트북이 절전 중 메뉴의 유효 기간을 넘겼을 수 있습니다. publish는 자정을 넘긴 경우에도
            # 만료된 데이터의 공개를 시작하거나 재실행하거나 진행 상태를 기다리지 않습니다.
            require_current_coverage(menu, today=datetime.now(timezone.utc).astimezone(BERLIN).date().isoformat())
        if run is None:
            self.git('fetch', 'origin', 'main')
            upstream = self.git('rev-parse', 'origin/main')
            if upstream != sha and not pending.get('dispatch_requested_at'):
                repository = self.snapshot_repository()
                if repository.plan_replacement(pending['period'], sha, upstream):
                    if getattr(self, '_collected_this_call', False):
                        raise RuntimeError('Upstream advanced; latest snapshot preparation is queued for retry')
                    self.replace_snapshot(state, repository)
                    pending = state['pending']
                    sha = pending['commit_sha']
            # 워크플로 요청이나 체크포인트 저장이 실패하기 전에 이전 push가 성공했을 수 있습니다.
            # 원격 커밋이 이미 해당 SHA의 후손이면 publish는 다시 push하지 않으며, 재전송은 거부될 수도 있습니다.
            if self.git('merge-base', sha, 'origin/main') != sha:
                try:
                    self.git('push', 'origin', 'HEAD:main')
                except Exception:
                    # publish는 원격 커밋 그래프가 실제로 진행한 것을 확인해야 소유한 스냅샷의 교체를 예약합니다.
                    self.git('fetch', 'origin', 'main')
                    upstream = self.git('rev-parse', 'origin/main')
                    if upstream != sha and not pending.get('dispatch_requested_at'):
                        self.snapshot_repository().plan_replacement(pending['period'], sha, upstream)
                    raise
            requested = pending.get('dispatch_requested_at')
            now = datetime.now(timezone.utc)
            if requested and now - datetime.fromisoformat(requested) < timedelta(minutes=30):
                raise RuntimeError('Deployment dispatch is not visible yet; waiting before any retry')
            # GitHub 요청은 성공했지만 응답이나 다음 큐 저장이 끊길 수 있습니다. 요청 시각을
            # 먼저 저장하고 실행이 보이지 않는 동안 재요청을 늦춰 중복 실행을 줄입니다.
            pending['dispatch_requested_at'] = requested or now.isoformat()
            self.checkpoint(state)
            self.publication_transport().dispatch(sha)
            for _ in range(12):
                run = self.find_run(sha)
                if run:
                    break
                time.sleep(2)
            if run is None:
                raise RuntimeError('Deployment requested; its run is not visible yet')
        self.confirm_run(state, run)

    def confirm_run(self, state, run):
        """실행 ID를 큐에 저장하고 GitHub 공개의 성공을 확인하여 실행 정보를 반환합니다.

        이미 실패한 실행에는 재실행을 요청하고 오류를 전달하여 JobRunner가 재시도 대기를
        기록하게 합니다. 취소된 실행은 전체를 재실행하고 다른 실패는 실패한 작업만
        재실행합니다. 진행 중인 실행은 기다린 뒤 다시 조회하며 성공한 경우에만 반환합니다.
        """
        pending = state['pending']
        sha = pending['commit_sha']
        pending['run_id'] = run['databaseId']
        self.checkpoint(state)
        if run['status'] == 'completed' and run['conclusion'] != 'success':
            self.publication_transport().rerun(run['databaseId'], failed=run['conclusion'] != 'cancelled')
            # confirm_run은 실행 ID를 보존합니다. JobRunner가 재시도 시점을 기록하면,
            # 다음 큐 실행에서 RefreshJob이 같은 GitHub 실행을 다시 확인합니다.
            raise RuntimeError(f"Deployment run {pending['run_id']} rerun requested; waiting for the next retry")
        if run['status'] != 'completed':
            self.publication_transport().watch(run['databaseId'])
            run = self.read_run(run['databaseId'], sha)
        if run['conclusion'] != 'success':
            raise RuntimeError(f"Deployment run {pending['run_id']} failed; it will be retried after backoff")

        return run

    def __call__(self, state):
        """큐의 collect·publish 단계와 교체 저널을 재개하며 성공한 공개 작업을 마칩니다.

        입력 state와 queue.json의 진행 정보를 갱신하고 필요하면 JSON 파일·Git 이력·
        GitHub 실행을 변경합니다. 만료된 메뉴는 성공 기록이 있으면 그 주기만 인정하고,
        실행이 아직 진행 중이면 대기하며, 그 외에는 현재 주기의 수집으로 되돌립니다.
        오류를 호출자에게 전달하여 JobRunner가 대기·차단·완료 상태를 기록하도록 합니다.
        """
        self._collected_this_call = False
        pending = state['pending']
        repository = self.snapshot_repository()
        identity = repository.replacement_identity()
        if identity is not None:
            # 교체 준비 중 원래 공개 실행이 발견될 수 있습니다. 먼저 그 실행을 확인하여
            # 같은 주기를 두 번 공개하지 않으며, 새 스냅샷도 준비되었다면 자동 선택하지 않고
            # 검토를 요구합니다. 성공한 원래 실행의 완료 기록은 큐에 저장된 뒤 저널에서 정리합니다.
            confirmation = identity['confirmation']
            if confirmation and confirmation['success'] and state['completed_period'] == identity['period']:
                repository.retire_acknowledgment(state['completed_period'])
            else:
                repository.validate_replacement_queue(pending['period'], pending['commit_sha'])
                if confirmation:
                    run = self.read_run(confirmation['run_id'], identity['sha'])
                elif (pending['period'], pending['commit_sha']) == (identity['period'], identity['sha']) and pending['run_id']:
                    run = self.read_run(pending['run_id'], identity['sha'])
                else:
                    run = self.find_run(identity['sha'])
                if run is not None:
                    repository.record_original_run(identity['period'], identity['sha'], run['databaseId'])
                    if identity['prepared']:
                        raise ValueError('Prepared replacement conflicts with original publication; inspection required')
                    pending.update(period=identity['period'], phase='publish', commit_sha=identity['sha'], run_id=run['databaseId'])
                    self.checkpoint(state)
                    if not (run['status'] == 'completed' and run['conclusion'] == 'success'):
                        original_menu = json.loads(self.git('show', identity['sha'] + ':site/data/menu.json'))
                        require_current_coverage(original_menu, today=datetime.now(timezone.utc).astimezone(BERLIN).date().isoformat())
                    self.confirm_run(state, run)
                    repository.record_original_run(identity['period'], identity['sha'], run['databaseId'], success=True)
                    return
        if pending['phase'] == 'publish' and pending['commit_sha'] and not pending['run_id'] and not pending.get('dispatch_requested_at'):
            repository = self.snapshot_repository()
            resumed = repository.resume_replacement(pending['period'], pending['commit_sha'])
            if resumed is not None:
                self.replace_snapshot(state, repository, resumed)
                pending = state['pending']
        if pending['phase'] == 'publish':
            menu = read_json(self.repo / 'site/data/menu.json')
            now = datetime.now(timezone.utc)
            if menu['coverage']['end'] < now.astimezone(BERLIN).date().isoformat():
                sha = pending['commit_sha']
                run = self.read_run(pending['run_id'], sha) if pending['run_id'] else self.find_run(sha) if sha else None
                if run and run['status'] == 'completed' and run['conclusion'] == 'success':
                    # 이 호출은 성공한 원래 주기만 완료 처리합니다. 다음 큐 실행이 가장 최근의
                    # 갱신 대상 주기를 별도로 생성합니다.
                    return
                if run and run['status'] != 'completed':
                    raise RuntimeError('Expired deployment run is still active; waiting before collecting its replacement')
                # RefreshJob은 이전 커밋을 이력으로 보존하고 큐 식별 정보만 초기화합니다. 이후 일반 수집이
                # 빨리 감기 방식으로 체크아웃을 갱신하고 현재 메뉴 스냅샷을 생성합니다.
                pending.update(phase='collect', attempts=0, next_attempt_at=None, last_error=None,
                               commit_sha=None, run_id=None, dispatch_requested_at=None)
                state['pending'] = reconcile(state, now)['pending']
                self.checkpoint(state)
        if state['pending']['phase'] == 'collect':
            self.collect()
            self._collected_this_call = True
            # RefreshJob은 한 번 호출될 때 수집을 한 번만 시도합니다. 만료된 결과가 다시 수집을
            # 재귀적으로 시작하거나 공개 체크포인트로 기록되지 않도록 합니다.
            require_current_coverage(read_json(self.repo / 'site/data/menu.json'),
                                     today=datetime.now(timezone.utc).astimezone(BERLIN).date().isoformat())
            state['pending']['phase'] = 'publish'
            self.checkpoint(state)
        self.publish(state)


class ConfiguredRefreshJob(RefreshJob):
    """TOML로 선언한 경로·저장소·모델 설정을 사용하여 GitHub 공개 작업을 실행합니다.

    큐와 교체 저널의 진행 방식은 RefreshJob에서 사용하고, 번역은 상태 디렉터리의
    비공개 체크포인트를 재사용하는 resume_configured_translations에 위임합니다.
    고정된 기본 macOS 모델 대신 선언된 모델·리비전을 사용하며, 새 번역이 필요할 때만
    접속 설정을 준비합니다. model_session은 관리 모드에서 소유 프로세스 그룹을
    시작·정리하고 외부 모드에서 기존 URL을 사용합니다. 선택적인 translate·runtime
    함수는 생성 요청·접속 준비를 대체하며, key는 요청에 전달합니다. 안내 용어집은
    설정된 체크아웃에서 읽습니다.
    """

    def __init__(self, config, *, translate=None, runtime=None, key=''):
        if config.publication.mode != 'github':
            raise ValueError('Configured GitHub refresh requires github publication')
        super().__init__(config.paths.state_dir)
        self.repo = config.paths.checkout_dir
        self.config = config
        self._translate = translate
        self._runtime = runtime
        self._key = key

    @property
    def repository_name(self):
        return self.config.publication.repository

    @property
    def expected_remote(self):
        return self.config.publication.remote

    @property
    def workflow_name(self):
        return self.config.publication.workflow

    def translate(self, menu, cache, phrases, *, checkpoint=None):
        """공개 캐시와 비공개 번역 기록을 병합·재개하여 캐시를 반환하고 검증된 항목을 파일에 저장합니다.

        resume_configured_translations가 체크포인트 저장과 모델 접속 환경의 정리를
        담당합니다. 선택적인 checkpoint 콜백은 저장 이후 캐시 복사본을 받습니다.
        이 메서드는 공개 요청을 보내거나 비공개 번역 기록을 지우지 않습니다.
        """
        return resume_configured_translations(
            menu, cache, phrases, self.config.inference,
            state_dir=self.config.paths.state_dir,
            glossary_provider=lambda: load_glossary(self.repo / 'data/notice-translations.json'),
            translate=request_translation if self._translate is None else self._translate,
            runtime=model_session if self._runtime is None else self._runtime,
            key=self._key, checkpoint=checkpoint,
        )

    def validate_complete(self, menu, cache):
        """설정된 체크아웃의 안내 용어집을 읽어 메뉴·캐시의 공개 조건을 검사합니다."""
        validate_publication(menu, cache,
                             notice_glossary=load_glossary(self.repo / 'data/notice-translations.json'))


def main(argv=None):
    """명령 인자를 해석하여 상태를 출력하거나 큐 작업 하나를 실행하고 결과를 출력합니다.

    --status는 설정·기존 큐·마지막 결과만 읽으며 상태 디렉터리나 큐 파일을 만들지
    않습니다. 일반 실행은 --base 또는 TOML 설정에 맞는 자원 검사와 공개 작업을
    JobRunner에 연결합니다. failed·blocked 결과에는 종료 코드 1을 사용합니다.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument('--base', type=Path)
    location.add_argument('--config', type=Path)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args(argv)
    config = load_worker_config(args.config) if args.config is not None else None
    directory = config.paths.state_dir if config is not None else args.base
    if args.status:
        try:
            last_result = json.loads((directory / 'last-result.json').read_text())
        except FileNotFoundError:
            last_result = None
        print(json.dumps({'queue': load_state(directory / 'queue.json'), 'last_result': last_result},
                         ensure_ascii=False, indent=2))
        return
    def terminate(signum, frame):
        # SIGTERM을 종료 예외로 바꾸어 모델 컨텍스트와 큐 잠금의 정리 절차가 실행되게 합니다.
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        if config is None:
            resources, work = resource_status, RefreshJob(args.base)
        else:
            resources = lambda: configured_resource_status(config.resources)
            if config.publication.mode == 'directory':
                work = DirectoryRefreshJob(
                    config, source=lambda: parse_menu(fetch_html()), clock=lambda: datetime.now(timezone.utc),
                    translate=lambda source, settings: request_translation(source, settings),
                    runtime=lambda settings, *, key: model_session(settings, key=key),
                )
            else:
                work = ConfiguredRefreshJob(config)
        result = process_queue(directory, datetime.now(timezone.utc), resources, work)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] in ('failed', 'blocked'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
