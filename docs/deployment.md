# 사이트 배포 안내

이 문서에서 게시 담당자는 웹사이트와 자동 실행 프로그램을 관리하는 운영자입니다. 게시 담당자는 검토된 코드를 사이트에 반영하고, 이용자가 읽는 HTML/CSS/JavaScript와 메뉴/번역 파일이 서로 맞는지 확인합니다. 코드와 설정의 위치는 [유지보수 설명서](maintenance.md), 계정과 파일 권한은 [운영 문서](operations.md)에 설명되어 있습니다.

## 화면 코드 반영과 매일 식단 갱신의 차이

유지보수자가 frontend/ 또는 site/의 화면 코드를 바꾸면 게시 담당자는 전체 사이트를 다시 만들어 게시합니다. 사이트 생성 프로그램 scripts/build_site.py가 HTML/CSS/JavaScript와 메뉴/번역을 출력 폴더에 모읍니다.

매일 식단 갱신 Python 프로그램 scripts/local_refresh.py는 원본 페이지를 읽고 완성된 메뉴/번역 JSON을 준비합니다. 파일 직접 게시 방식(directory)에서는 이 프로그램이 사이트 data 폴더만 바꾸며 HTML/CSS/JavaScript는 바꾸지 않습니다. GitHub 방식(github)에서는 프로그램이 데이터 커밋을 보내고 GitHub 작업이 전체 사이트를 만들어 게시합니다.

게시 담당자는 아래 준비 단계를 공통으로 진행한 뒤 실제로 사용하는 게시 방법을 선택해 주세요. Linux에서 일별 갱신 프로그램을 활성화하고 모델 파일을 준비하는 작업은 정적 파일을 GitHub Pages에 게시하는 작업과 별도로 확인합니다.

## 게시할 코드와 파일을 준비하기

1. 유지보수자와 게시 담당자는 프로젝트 폴더에서 현재 브랜치와 `git status --short`를 확인하고 게시할 변경 파일을 정합니다. 이미 있는 미커밋/미추적 파일과 main의 다른 변경을 보존합니다. 관련 없는 파일을 reset/clean으로 지우지 않습니다.
2. 유지보수자는 변경한 기능의 Python/브라우저 검사를 실행합니다. [README](../README.md)의 `python3 -m unittest discover -s tests -v`, `npm test`, `python3 -m scripts.update_menu --validate-only`는 기본 명령입니다. 마지막 명령은 site/data의 메뉴/번역 형식과 원문 연결을 검사합니다.
3. 게시 담당자는 프로젝트 폴더에서 아래 날짜 검사를 실행해 menu.json의 coverage.end가 Berlin 오늘보다 과거가 아닌지 확인합니다. 저장 파일 검사인 validate-only에는 이 판단이 없으므로 별도로 실행합니다.
4. 유지보수자는 `npm run build -- --output .tmp/build-example`으로 새 출력 폴더를 만들고 로컬 웹서버가 이 폴더를 읽게 합니다. 이미 이 폴더가 있으면 프로그램이 기존 파일 보호를 위해 중단하므로 다른 이름을 사용합니다. 유지보수자는 일간/주간, 세 언어와 가격, 원문/열린 상세, 날짜 이동, 작은 화면과 하위 URL 경로를 확인합니다.
5. GitHub에 반영하는 게시 담당자는 검토한 변경을 커밋하고 최신 main의 변경과 충돌을 해결합니다. 게시 대상은 검토 결과를 포함한 main의 정확한 커밋 SHA입니다. SHA는 Git에서 한 커밋을 식별하는 값이며 아래 워크플로는 40자리 형식을 요구합니다.

```sh
python3 -c "from pathlib import Path; from scripts.update_menu import read_json, require_current_coverage; require_current_coverage(read_json(Path('site/data/menu.json')))"
```

생성한 출력 폴더에는 data/current.json이 선택한 같은 ID의 menu.json과 translations.json이 있어야 합니다. 현재 선택 파일이 ID A를 지정한다면 두 파일 모두 data/releases/A에서 읽어야 합니다. 게시 담당자는 작업 기록 queue.json·개별 번역 기록·모델·로그가 공개 출력에 들어가지 않는지 확인합니다.

## GitHub Pages에 게시하기

GitHub 게시 작업 정의는 [update-and-deploy.yml](../.github/workflows/update-and-deploy.yml)입니다. 게시 담당자는 main에서 수동 workflow_dispatch를 실행하고 필수 입력 snapshot_sha에 게시할 main 커밋 SHA를 지정합니다. 이 파일에는 push/주간 자동 실행 일정이 없습니다.

GitHub 작업은 입력 SHA가 40자리이고 origin/main의 이력에 포함되는지 확인한 뒤 그 커밋의 파일을 읽습니다. 작업은 잠금 파일의 컴파일러 설치, Python/브라우저 검사, 저장 메뉴/번역 검사와 현재 날짜 검사, 사이트 생성을 차례로 실행하고 `.tmp/pages` 폴더를 Pages에 게시합니다. 같은 mensa-pages 실행 그룹은 진행 중인 작업을 취소하지 않도록 설정되어 있습니다.

일별 갱신 Python 프로그램도 이 GitHub 작업을 요청합니다. 프로그램은 작업 기록 폴더의 queue.json에 데이터 commit_sha, 요청 시각 dispatch_requested_at, 실행 번호 run_id를 저장합니다. 게시 담당자는 수동 코드 게시 전에 이미 진행 중인 식단 게시가 있는지 확인해 두 게시가 경합하지 않게 해 주세요. 응답을 잃거나 게시가 실패했을 때 프로그램은 같은 커밋/실행 번호를 먼저 조회하며 실패한 실행은 같은 번호로 다시 요청합니다.

게시 담당자는 요청한 SHA의 GitHub 작업이 성공했는지 확인하고 [공개 식단](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/)에서 파일 로딩·식단 날짜·일간/주간·번역·가격·상세 표시를 확인합니다. 확인 대상은 이용자가 쓰는 이 주소입니다.

## 사이트 폴더에 직접 게시하기

파일 직접 게시 방식을 사용하는 운영자는 전체 출력 폴더를 별도 서버 폴더에 준비하고 웹서버가 읽을 수 있는지 먼저 확인합니다. 운영자가 실제 제공 폴더를 바꿀 때 TOML의 public_dir과 Linux 자동 실행 파일의 쓰기 허용 경로도 맞춥니다. 예제의 웹사이트 폴더는 /srv/www/mensa이며 Python 프로그램이 쓰는 범위는 그 안의 data와 비공개 작업 기록 /var/lib/mensa/private입니다.

Linux 자동 실행 파일 deploy/mensa-refresh.service는 Python 프로그램을 User=mensa로 실행합니다. 운영자는 이 OS 계정이 기존 data 파일까지 읽고 쓸 수 있고 정적 웹서버 계정은 공개 파일을 읽을 수 있게 권한을 준비합니다. 사이트 전체의 HTML/CSS/JavaScript 교체는 운영자가 하고, 이후 일별 메뉴 JSON 갱신은 Python 프로그램이 합니다.

운영자가 deploy의 service/timer를 설치할 때는 실제 계정·경로·Python/시간대·Ollama 실행 파일/모델 가중치·비공개 권한과 메모리/GPU 측정·종료 동작을 대상 Linux에서 확인합니다. TOML을 읽는 함수가 성공했다는 사실만으로 모델이나 자동 실행이 준비된 것은 아닙니다. GitHub 방식을 선택하면 프로그램이 전용 main 코드 폴더를 써야 하므로 읽기 전용 코드 폴더를 가정한 directory용 Linux 파일을 그대로 사용하지 않습니다.

## 실패를 조사하고 이전 사이트로 되돌리기

게시 담당자는 현재 공개된 전체 사이트와 같은 ID의 메뉴/번역을 보존합니다. 작업 기록을 수정하거나 백업하기 전에는 자동 실행과 현재 Python 작업을 멈추고 worker.lock의 파일 잠금을 사용합니다. 운영 문서에는 Linux/Mac 중지 명령과 잠금 아래의 request_retry 예제가 있습니다.

운영자는 실제 작업 기록 폴더의 queue.json·last-result.json을 백업해 주세요. `--config` 실행을 사용하는 운영자는 translation-checkpoint.json과 실제 TOML도 함께 보존해 주세요. GitHub 게시 방식을 사용하는 운영자는 복구용 snapshot-journal.json도 보존해 주세요. Mac 기본 `--base` 실행은 TOML이나 translation-checkpoint.json을 사용하지 않으므로, 운영자는 이 파일이 없다는 이유로 복구 실패라고 판단하지 마세요. GitHub 운영자는 같은 커밋/실행 번호의 성공 여부를 조사한 뒤 원인을 고칩니다. 파일이나 게시 번호를 지우면 프로그램이 이미 수행한 게시를 잊을 수 있으므로 운영자는 queue.json/번역 기록/복구 기록 삭제나 force push로 해결하지 않습니다.

Git 변경을 복구하는 SnapshotRepository(`mensa/git_repository.py`)는 워커가 두 데이터 JSON을 기록해 만든 커밋인지 snapshot-journal.json의 부모 커밋·Git 트리·데이터 파일 내용으로 확인합니다. 이 커밋을 소유 커밋이라고 부르며 커밋 메시지만으로 판단하지 않습니다. 게시 준비 중 원격 main이 앞서가면 SnapshotRepository의 plan_replacement/resume_replacement는 두 이력을 구별합니다. 원격 main 이력에 워커의 기존 커밋이 이미 들어 있으면 로컬 main을 원격 main 위치까지 앞으로 이동합니다(fast-forward). 기존 커밋이 main 이력에 남아 있으므로 이 경우에는 refs/mensa/superseded/<SHA>를 만들지 않습니다. 원격 main이 기존 커밋을 포함하지 않고 두 이력이 갈라졌다면, 클래스는 워커가 만든 커밋인지와 기록된 출발 커밋이 원격 main에 남아 있는지를 확인한 뒤 기존 커밋을 refs/mensa/superseded/<SHA>에 보존합니다. 프로그램은 최신 main에서 식단을 다시 수집하고 새 게시용 데이터 커밋을 준비합니다. 이 새 커밋을 대체 커밋이라고 부릅니다. 사람이 만든 변경과 알 수 없는 커밋은 보존하며 운영자가 검토합니다.

이전 화면으로 되돌릴 때 게시 담당자는 검증된 전체 HTML/CSS/JavaScript와 같은 ID의 메뉴/번역을 함께 제공합니다. 작업 기록도 되돌릴 필요가 있다면 현재 게시 커밋/실행 번호와 백업의 갱신 날짜/게시 번호/복구 기록을 먼저 대조합니다. 운영자는 알 수 없는 변경과 대체 커밋의 충돌을 보존하고 검토해야 합니다.

게시 담당자는 마지막으로 정확한 코드 SHA, 현재 선택한 data/current.json과 식단 날짜, 실제 게시 성공과 공개 화면, 일별 갱신 프로그램의 대기/완료 기록이 서로 맞는지 확인합니다. 모델 준비와 Linux 자동 실행 확인은 해당 컴퓨터에서 실제 수행한 항목을 기준으로 판단합니다.
