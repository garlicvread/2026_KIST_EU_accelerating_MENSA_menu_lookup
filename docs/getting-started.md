# 유지보수 시작 안내

이 프로젝트는 Python 워커가 식단을 수집·번역하여 Drive에 게시하고, 브라우저가 Pages의 화면 코드와 Drive의 공개 묶음을 읽는 구조입니다. 웹 백엔드나 데이터베이스 엔진은 없습니다. 먼저 코드·비공개 상태·로컬 데이터 준비·공개 파일의 위치를 구별해 주세요.

## 기존 환경을 이어받을 때

현재 Mac의 운영 코드는 `~/mensa`, 큐·모델·로그는 `~/Library/Application Support/Mensa`에 있습니다. Drive 방식의 `--base`도 BASE의 `worker.toml`을 읽습니다. 설정이 없으면 자동 Git 게시로 되돌아가지 않습니다. 기존 모델·LaunchAgent는 보존하며 코드 수정 때마다 재설치하지 않습니다.

Drive 설정을 준비한 것과 실제 전환 완료는 다릅니다. [배포 안내](deployment.md#기존-mac을-drive-방식으로-전환하기)에서 공개 읽기·Pages·자동 워커·Git 무변경 확인이 끝났는지 먼저 확인해 주세요. 기존 설치를 그대로 맡으면 [운영 현황과 상태 명령](operations.md#현재-운영-환경)에서 시작합니다. 계정이나 컴퓨터가 달라지면 [후임자 Drive 연결](operations.md#drive-연결과-후임자-인계)과 [새 설치](operations.md#새-환경에-설치할-때)를 사용합니다.

후임자는 자신의 rclone 인증·remote·폴더·파일 ID를 설정합니다. OAuth 비밀을 소스나 워커 TOML에 넣지 않습니다. 기존 사용자의 공유 설정을 바꾸는 작업은 이번 전환에 포함하지 않으며, 새로운 설치의 공개 읽기는 해당 운영자가 준비합니다.

## 먼저 읽을 문서

1. [README](../README.md): 실행 구조, 검사 명령과 두 가지 로컬 빌드 방법을 확인합니다.
2. [유지보수 설명서](maintenance.md): 수정 대상과 필수 데이터 불변 조건을 찾습니다.
3. [구조 문서](architecture.md): JobRunner·DriveRefreshJob·DrivePublisher·브라우저의 책임을 확인합니다.
4. [데이터 계약](data-contract.md): Drive envelope, 메뉴·번역·가격·ID·해시 규칙을 읽습니다.
5. [운영 문서](operations.md): 경로·모델·Drive 인증·백업·실패 재시도를 확인합니다.
6. [배포 안내](deployment.md): 검토 코드 반영과 최초 활성화, 화면 배포를 진행할 때 사용합니다.

## 코드를 수정하는 순서

운영 체크아웃과 분리된 개발 폴더에서 `git status --short`로 기존 변경을 확인하고 보존합니다. 원본 HTML은 `scripts/menu_source.py`, 메뉴·가격은 `mensa/menu_contract.py`, 번역 key·표현은 `mensa/translation_contract.py`, Drive 게시·복구는 `mensa/drive_publication.py`·`drive_refresh.py`, 화면 선택은 `frontend/selectors.ts`에서 시작합니다.

메뉴 필드를 바꾸면 Python 검사와 `frontend/contracts.ts`를 함께 검토합니다. `visibleMeals`는 같은 날짜에서 ID 이외의 모든 내용이 같은 발생만 한 번 표시하므로 새 필드의 비교도 확인합니다. 원본의 반복 발생·주의 표시·검증된 가격을 화면 정리를 위해 JSON에서 지우지 않습니다.

Python 3.12 이상과 Node.js 24로 변경한 기능의 회귀 검사를 실행합니다. 새 개발 환경은 README의 `npm ci`로 컴파일러를 설치합니다. 기본 검사는 다음과 같습니다.

```sh
python3 -m unittest discover -s tests -v
npm test
python3 -m scripts.update_menu --validate-only --data-dir tests/fixtures/menu-data
npm run build -- --data-dir tests/fixtures/menu-data --output .tmp/build-example
```

출력 경로는 아직 없는 폴더로 바꿔 사용합니다. 로컬 웹서버는 `site/` 대신 생성된 출력 폴더를 제공합니다. 공개 Drive를 읽는 화면 검사는 `npm run build -- --drive-file-id FILE_ID --output .tmp/build-drive`로 준비합니다. 테스트의 고정 날짜를 최신 운영 식단으로 해석하지 않습니다.

문서만 바꾸면 사실·명령·링크를 검토합니다. 문서 확인을 위해 모델 실행이나 식단 수집을 새로 할 필요는 없습니다. main push의 코드 배포는 워크플로의 트리거를 따르며, 일상 Drive 갱신 자체는 저장소나 Actions를 변경하지 않습니다.

## 실패한 작업을 이어갈 때

`--status`로 큐·마지막 결과를 먼저 읽고 실제 Drive 릴리스·수집 시각과 대조합니다. `translation-checkpoint.json`은 미완성일 수 있는 비공개 번역이고 `drive-publication.json`은 준비한 묶음의 재시도 입력입니다. Mac `--base`의 Drive 경로도 이를 사용합니다. 큐·체크포인트·로컬 릴리스·설정·기존 복구 기록을 삭제해 재시작하지 않습니다.

상태 파일 수정·백업·운영 코드 반영 전에는 자동/수동 작업을 멈추고 같은 `worker.lock`을 확보합니다. 인증·모델·자원·원문·번역의 실제 원인을 고친 뒤 [잠금 아래 재시도 절차](operations.md#실패-확인과-재시도)를 사용합니다. 일일 업로드 실패를 해결하려고 Git 데이터 커밋이나 Pages 배포를 만들지 않습니다.
