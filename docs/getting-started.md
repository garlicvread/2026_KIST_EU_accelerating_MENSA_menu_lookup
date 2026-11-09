# 유지보수 시작 안내

이 프로젝트의 Python 수집기(worker)는 수집 호스트에서 식단을 수집하고 메뉴·가격과 사용할 수 있는 번역을 Google Drive의 `current.json`에 게시합니다. GitHub Actions 배포 실행기는 이 공개 JSON을 화면 코드와 함께 GitHub Pages에 배포하고, 웹브라우저는 같은 Pages 사이트에서 화면과 데이터를 읽습니다. 웹 백엔드나 데이터베이스 엔진은 없습니다.

원본 수집은 모델을 실행하지 않습니다. 번역이 없거나 손상된 항목은 독일어 원문으로 표시하며 별도 번역 작업이 보완합니다. 번역 작업의 자원 부족이나 재시도 대기가 다음 메뉴·가격 수집을 막지 않습니다.

## 기존 환경을 이어받을 때

운영자가 인계한 실행 계정과 `worker.toml`의 위치를 먼저 확인해 주세요. 설정의 `paths.checkout_dir`는 수집기 코드, `paths.state_dir`는 큐·번역 상태 등 비공개 자료, `paths.public_dir`는 로컬 게시 준비 폴더입니다. 세 폴더는 서로 겹치지 않아야 합니다. 모델·로그 경로, 자동 실행 서비스와 Drive 연결도 해당 설치의 설정을 확인합니다.

수집기 코드 폴더에서 인계받은 설정의 절대 경로로 상태를 조회합니다.

```sh
python3 -m scripts.local_refresh --config /path/to/worker.toml --status
```

`--status`는 수집 큐·번역 작업·마지막 결과를 읽으며 수집이나 모델 생성을 실행하지 않습니다. `--base /path/to/base`는 같은 폴더의 `worker.toml`을 읽는 편의 인자입니다. Drive 모드의 일반 실행은 두 인자 모두 같은 설정과 작업을 사용합니다.

기존 설치의 점검은 [운영 구성과 상태 확인](operations.md#운영-구성과-상태-확인)에서 시작합니다. 기존 Git 게시에서 전환하는 설치는 [전환 절차](deployment.md#기존-git-게시에서-drive로-전환하기)의 공개 읽기·Pages 배포·자동 수집 확인을 마쳐야 합니다. 계정이나 호스트가 달라지면 [후임자 Drive 연결](operations.md#drive-연결과-후임자-인계)과 [새 설치](operations.md#새-환경에-설치할-때)를 사용합니다. 수집기의 자원 검사는 macOS와 Linux를 지원하며, 플랫폼별 설치 명령은 운영 문서에 있습니다.

운영자는 수집 호스트의 실제 경로와 실행 계정, 모델 설정, 자동 실행 서비스, rclone remote·폴더·파일 ID를 인계합니다. 후임자는 자신의 계정에 맞는 인증과 공개 읽기를 준비합니다. OAuth 비밀은 rclone의 비공개 설정에 보관하고 소스나 수집기 TOML에는 넣지 않습니다.

## 먼저 읽을 문서

1. [README](../README.md)에서 실행 구조, 검사 명령과 두 가지 로컬 빌드 방법을 확인합니다.
2. [유지보수 설명서](maintenance.md)에서 수정 대상과 유지해야 할 데이터 조건을 찾습니다.
3. [구조 문서](architecture.md)에서 수집·번역·Drive 게시·화면의 책임과 구현 위치를 확인합니다.
4. [데이터 계약](data-contract.md)에서 Drive 게시 묶음, 메뉴·번역·가격·ID·해시 규칙을 읽습니다.
5. [운영 문서](operations.md)에서 경로·모델·Drive 인증·백업·실패 재시도를 확인합니다.
6. [배포 안내](deployment.md)에 따라 검토한 코드 반영과 최초 활성화, 화면 배포를 진행합니다.

## 코드를 수정하는 순서

수집기가 사용하는 코드 폴더와 분리된 개발 체크아웃에서 `git status --short`로 기존 변경을 확인하고 보존합니다. 원본 HTML 처리는 `scripts/menu_source.py`, 메뉴·가격은 `mensa/menu_contract.py`, 번역 키·표현은 `mensa/translation_contract.py`, Drive 게시·복구는 `mensa/drive_publication.py`·`mensa/drive_refresh.py`, 화면 선택은 `frontend/selectors.ts`에서 시작합니다.

메뉴 필드를 바꾸면 Python 검사와 `frontend/contracts.ts`를 함께 검토합니다. `visibleMeals`는 같은 날짜에서 ID 이외의 검증된 메뉴 필드가 모두 같은 기록을 한 번만 표시하므로 새 필드의 비교도 확인합니다. 원본의 반복 기록·주의 표시·검증된 가격을 화면 정리를 위해 JSON에서 지우지 않습니다.

저장소 CI와 같은 Python 3.12·Node.js 24 환경에서 변경한 기능의 회귀 검사를 실행합니다. 새 개발 환경은 README의 `npm ci`로 프런트엔드 컴파일러를 설치합니다. 기본 검사는 다음과 같습니다.

```sh
python3 -m unittest discover -s tests -v
npm test
python3 -m scripts.update_menu --validate-only --data-dir tests/fixtures/menu-data
npm run build -- --data-dir tests/fixtures/menu-data --output .tmp/build-example
```

출력 경로는 아직 없는 폴더로 바꿔 사용합니다. 로컬 웹서버는 생성된 출력 폴더를 제공합니다. 실제 게시 데이터를 확인할 때는 해당 설치의 공개 파일 ID를 넣어 `npm run build -- --drive-file-id FILE_ID --output .tmp/build-drive`를 실행합니다. 테스트의 고정 날짜를 최신 운영 식단으로 해석하지 않습니다.

문서만 바꾸면 사실·명령·링크를 검토합니다. 문서 확인을 위해 모델 실행이나 식단 수집을 새로 할 필요는 없습니다. Drive 갱신은 데이터 커밋·PR을 만들지 않습니다. Actions는 30분 간격으로 게시본 변경을 확인해 Pages에 반영하며 `main` push·수동 실행도 지원합니다. 빌드와 웹브라우저의 공개 데이터 읽기에는 API 키가 필요하지 않습니다.

## 실패한 작업을 이어갈 때

`--status`로 수집 큐·번역 작업·마지막 결과를 먼저 읽고 Drive의 게시본과 원본 수집 시각인 `menu.source.fetched_at`을 대조합니다. `completed_period`는 처리가 끝난 Berlin 예정 시각을 뜻합니다. 실제 식단의 최신 여부는 원본 수집 시각과 포함된 날짜로 확인해 주세요. 수집 호스트의 `translation-state.json`에는 비공개 재사용 캐시·사용 기록이 있고, `translation-work.json`에는 별도 번역 시도·대기 상태가 있습니다. `drive-publication.json`은 준비한 수집 묶음의 재시도 입력입니다. 이러한 상태 파일과 큐·설정을 보존한 채 실패 원인을 확인해 주세요.

공개 메뉴의 기본 보관 범위는 지난 30일·오늘·원본의 미래 날짜이며, 공개 번역에는 그 메뉴가 참조하는 정상 항목만 담습니다. 비공개 재사용 캐시의 미사용 180일과 로컬 성공 게시본 10개 등 [보관 한도](operations.md#데이터-보관-한도와-정리)를 확인합니다. 재부팅 뒤 복구 기록의 소유권 오류가 나타나면 상태 파일을 지우기 전에 [실패 확인과 재시도](operations.md#실패-확인과-재시도)에서 적용된 코드와 기록을 점검해 주세요.

상태 파일 수정·백업·수집기 코드 반영 전에는 자동·수동 작업을 멈추고 같은 `worker.lock`을 확보합니다. 인증·모델·자원·원본·번역의 실제 원인을 고친 뒤 [잠금 아래 재시도 절차](operations.md#실패-확인과-재시도)를 사용합니다. 수집과 번역의 대기 상태가 분리되어 있으므로 실패한 단계에 맞춰 복구해 주세요.
