"""GitHubPublication이 지정된 저장소에서 실행하는 명령 함수로 GitHub 공개 워크플로를 요청하고 확인합니다.

작업자는 큐 체크포인트, 재시도 시점, 워크플로 재실행 여부 및 메뉴 스냅샷 식별 정보를
관리합니다. GitHubPublication은 생성 시 저장소를 선택하거나 워크플로를 실행하지 않습니다.
"""

import json


class GitHubPublication:
    def __init__(self, workflow: str, *, gh, watch):
        if not isinstance(workflow, str) or not workflow.strip():
            raise ValueError('workflow must be an explicit nonblank string')
        if not callable(gh) or not callable(watch):
            raise ValueError('gh and watch must be callable repository-bound ports')
        self._workflow = workflow
        self._gh = gh
        self._watch = watch

    def find_run(self, sha):
        runs = json.loads(self._gh('run', 'list', '--workflow', self._workflow,
                                   '--event', 'workflow_dispatch', '--limit', '50',
                                   '--json', 'databaseId,headSha,displayTitle,status,conclusion'))
        # 공개 워크플로는 snapshot_sha의 데이터를 체크아웃하며, 워크플로 정의 자체는 더 최신일 수 있습니다.
        return next((run for run in runs if run['displayTitle'] == f'Publish menu {sha}'), None)

    def read_run(self, run_id, sha):
        run = json.loads(self._gh('run', 'view', str(run_id),
                                 '--json', 'databaseId,displayTitle,status,conclusion'))
        if run['displayTitle'] != f'Publish menu {sha}':
            raise ValueError('Deployment run does not match the checkpoint snapshot')
        return run

    def dispatch(self, sha):
        return self._gh('workflow', 'run', self._workflow, '--ref', 'main', '-f', f'snapshot_sha={sha}')

    def rerun(self, run_id, *, failed):
        return self._gh('run', 'rerun', str(run_id), *(['--failed'] if failed else []))

    def watch(self, run_id):
        return self._watch(run_id)
