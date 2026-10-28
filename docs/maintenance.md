# 유지보수 설명서

유지보수자는 식단의 원문·가격·번역 규칙과 화면 코드를 관리합니다. 운영자는 실행 계정·모델·Drive 연결·큐·배포를 관리합니다. 같은 사람이 두 역할을 맡아도 코드 변경, 데이터 갱신, 운영 활성화의 결과를 구별해 주세요.

## 코드·데이터·비공개 상태

| 대상 | 위치와 관리 방법 |
| --- | --- |
| 소스 | `mensa/`, `scripts/`, `frontend/`, `site/`와 검토 번역 사전은 Git으로 관리합니다. |
| 테스트 예제 | `tests/fixtures/menu-data`에 작고 고정된 메뉴·번역 쌍을 둡니다. |
| 비공개 상태 | `state_dir`의 큐·결과·번역 체크포인트·Drive 준비 저널·설정과 모델·로그입니다. 웹에 공개하지 않습니다. |
| 로컬 데이터 준비 | `public_dir/data/releases/<release_id>/`의 검증된 메뉴·번역과 로컬 `current.json`입니다. Drive 모드에서는 정적 웹사이트 루트가 아닙니다. |
| 공개 데이터 | Drive의 안정된 파일 ID를 갖는 `current.json` 한 개입니다. |
| 공개 화면 | Pages의 HTML/CSS/favicon·내용별 해시 경로의 JavaScript입니다. Drive 빌드에는 JSON이 없습니다. |

일상 식단 갱신은 코드 체크아웃을 읽기만 하며 Git 커밋·PR·Actions·Pages 배포를 만들지 않습니다. 운영 JSON을 `site/data`에 다시 추적하지 않습니다. 기존 Git 이력·지난 공개 날짜·가격·번역·큐·모델을 전환 때문에 삭제하지 않습니다.

## 수정 대상을 찾는 지도

| 변경하려는 동작 | 시작할 코드 |
| --- | --- |
| HTML 추출·원문 요소 대조 | [`scripts/menu_source.py`](../scripts/menu_source.py) |
| 날짜·발생 ID·가격·과거 날짜 보존 | [`mensa/menu_contract.py`](../mensa/menu_contract.py), [`refresh_service.py`](../mensa/refresh_service.py) |
| 번역 key·표현·모델 재사용 | [`mensa/translation_contract.py`](../mensa/translation_contract.py) |
| 모델 요청·오류 분류 | [`mensa/generation.py`](../mensa/generation.py), [`errors.py`](../mensa/errors.py) |
| 중간 번역·모델 프로세스 | [`mensa/translation_runtime.py`](../mensa/translation_runtime.py), [`checkpoints.py`](../mensa/checkpoints.py), [`model_runtime.py`](../mensa/model_runtime.py) |
| 전체 게시 검사·주의 표시 | [`mensa/publication.py`](../mensa/publication.py), [`notice_contract.py`](../mensa/notice_contract.py), [`scripts/notices.py`](../scripts/notices.py) |
| 검토 문구 입력 | [`data/editorial-translations.json`](../data/editorial-translations.json), [`notice-translations.json`](../data/notice-translations.json) |
| Drive 원격 게시·준비본 재개 | [`mensa/drive_publication.py`](../mensa/drive_publication.py), [`drive_refresh.py`](../mensa/drive_refresh.py) |
| 로컬 릴리스·해시 | [`mensa/releases.py`](../mensa/releases.py) |
| 설정·큐·자원 | [`mensa/config.py`](../mensa/config.py), [`jobs.py`](../mensa/jobs.py), [`queue.py`](../mensa/queue.py), [`resources.py`](../mensa/resources.py) |
| 최초 이관·실행 | [`scripts/configure_drive.py`](../scripts/configure_drive.py), [`local_refresh.py`](../scripts/local_refresh.py) |
| Drive 읽기·표시 계약 | [`frontend/data_client.ts`](../frontend/data_client.ts), [`contracts.ts`](../frontend/contracts.ts), [`display.ts`](../frontend/display.ts) |
| 화면 상태·선택·초점 | [`frontend/controller.ts`](../frontend/controller.ts), [`state.ts`](../frontend/state.ts), [`selectors.ts`](../frontend/selectors.ts), [`app.ts`](../frontend/app.ts) |
| 정적 화면 빌드·배포 | [`scripts/build_site.py`](../scripts/build_site.py), [`update-and-deploy.yml`](../.github/workflows/update-and-deploy.yml) |

자세한 호출 흐름은 [구조 문서](architecture.md), 필드와 해시의 정의는 [데이터 계약](data-contract.md)에 있습니다.

## 변경 후에도 지킬 데이터 규칙

원본의 모든 메뉴 발생을 JSON에 보존하고 같은 날짜·분류·독일어 이름의 반복에는 고유한 발생 순번을 붙입니다. 화면만 `visibleMeals`로 ID 이외의 모든 내용이 같은 발생을 한 번 표시합니다. 다른 날짜·위치·구성·주의 표시·가격을 제목이나 선택 가격 하나로 합치지 않습니다.

가격은 원본 S/M/G와 학생·교직원·방문객의 정수 센트를 모두 대조합니다. 마지막 요리에 붙은 유일한 판매대 공통 가격은 `scope:counter`로 소유 원문을 기록합니다. 개별·공통 가격이 없는 항목은 `null/source_pending`으로 두고 원본 링크로 안내합니다. 다른 날짜·판매대·PDF에서 임의로 채우지 않습니다. 정상 메뉴 추가·삭제·교체·중복 수 변화·빈 식단을 이전 개수와 비교해 차단하지 않으며 실제 가격 소실·날짜·원문 연결은 계속 검사합니다.

새 원본의 유효성과 현재 날짜를 먼저 검사하고 Drive는 지난30일·오늘·원본의 미래 메뉴를 공개합니다. 원본 범위의 누락을 과거 메뉴로 메우지 않습니다. 공개 번역은 메뉴 참조만 담고 운영용 Mac의 재사용 캐시는 별도 수명으로 관리합니다. 원문·구성품·두 언어·주의 표시 번역이 완성돼야 공개합니다. `retention.py`, `retention_runtime.py`, `maintenance.py`와 [운영 한도](operations.md#데이터-보관-한도와-정리)를 함께 검토합니다. 미등록 주의 표시도 번역하며 원문 주의 표시를 삭제하거나 모델이 가격·알레르기를 만들게 하지 않습니다.

원문 이름과 구성품 순서는 번역 key를 결정합니다. 모델 번역의 재사용은 provider/model/revision과 번역 정책·주의 표시 사전의 지문을 확인합니다. 임시 포트·URL·인증 값은 설정 기반 생성 정체성에 넣지 않습니다. 모델 내용을 바꾸면 운영자가 안정된 revision도 바꿉니다. 이름·구성품 표현을 정리해도 원래 사람/모델 출처를 바꾸지 않으며 해독되지 않은 바이트 토큰은 게시하지 않습니다.

Drive의 `current.json`은 정확히 `schema_version`, `release_id`, `menu`, `translations`를 담습니다. 릴리스 ID는 기존 canonical 메뉴/번역 바이트의 길이와 내용을 묶은 SHA256입니다. 파일 ID는 설치의 안정된 식별자이며 릴리스 ID와 다릅니다. 브라우저는 한 묶음을 한 번 읽어 ID별로 고정하고 두 분기를 독립적으로 검사합니다. 번역 오류나 원문 불일치는 독일어 표시로 처리하고 오래된 응답으로 새 상태를 덮어쓰지 않습니다.

## 설정과 모델을 바꿀 때

Drive 전체 예제는 [`config/worker.drive.example.toml`](../config/worker.drive.example.toml)입니다. `--base BASE`는 `BASE/worker.toml`을 읽고 `--config PATH`는 명시한 전체 TOML을 읽습니다. 누락·알 수 없는 키를 허용하거나 설정 누락을 자동 Git 게시로 해석하지 않습니다. 코드·비공개 상태·로컬 준비 경로는 서로 겹치지 않아야 합니다. 상대 경로는 TOML 기준이며 `~`·환경변수는 확장하지 않습니다.

`publication.mode = "drive"`에는 rclone `remote`, `folder_id`, `file_id`가 모두 필요합니다. OAuth token/client secret은 rclone의 비공개 설정에만 두고 코드·TOML·사이트에 넣지 않습니다. 파일 ID를 바꾸면 Pages의 `MENSA_DRIVE_FILE_ID`도 설치 변경으로 한 번 갱신합니다. 평소 업로드는 이 변수를 바꾸지 않습니다. 인증 인계와 공용 OAuth client 종료 공지는 [운영 문서](operations.md#drive-연결과-후임자-인계)를 참고해 주세요.

managed 모델은 준비된 실행 파일·모델·로그 경로를 사용하고 자신이 시작한 프로세스 그룹만 종료합니다. external 모델은 해당 운영자가 가동·인증을 책임집니다. 설정 CLI에는 API key 필드가 없으므로 인증 endpoint를 추가하려면 실제 key 전달 경로를 검토합니다. 기존 Mac `--base`의 Drive 전환은 설치된 `gemma4:31b`와 네이티브 캐시 재사용 정책을 유지하며 모델을 다시 받지 않습니다.

## 실패와 복구 코드를 바꿀 때

JobRunner의 같은 `worker.lock` 아래에서 큐와 작업을 처리합니다. DriveRefreshJob은 완성된 묶음을 로컬에 먼저 저장하고 비공개 `drive-publication.json`에 수집 시각·릴리스·목적지를 기록한 뒤 원격 게시를 시도합니다. 업로드 응답이나 완료 기록이 사라져도 다음 시도는 같은 준비본을 읽고 원격을 확인합니다. 로컬 포인터의 준비본과 마지막 성공한 원격 게시본이 다를 수 있으므로 포인터 존재만으로 공개 성공을 기록하지 않습니다.

DrivePublisher는 목적지 폴더·안정된 파일 ID·기존 내용·중복 예약 이름을 확인하고 전체 공개 조건을 검사한 뒤 쓰며 업로드 후 읽어 확인합니다. 같은 내용의 업로드를 생략하고 알 수 없는 원격 내용을 덮어쓰지 않습니다. 같은 준비 폴더와 Drive 목적지에 여러 작성자를 연결하지 않습니다. 공유 변경·다른 파일 삭제·sync/purge·Git/gh 호출을 일상 경로에 추가하지 않습니다.

운영자는 `--status`의 완료/대기 수집 시각과 공개 수집 시각·릴리스 ID를 대조합니다. 설정·모델·인증·원문 문제를 먼저 고치고 큐·체크포인트·준비 저널·로컬 릴리스를 보존합니다. 백업·상태 수정·코드 fast-forward는 자동/수동 작업 중지 후 같은 잠금 안에서 수행합니다. 자동 재시도 중지 해제는 [운영 문서의 request_retry 예제](operations.md#실패-확인과-재시도)를 사용합니다. 복구를 위해 데이터 커밋·Pages 배포나 이력 재작성을 새로 만들지 않습니다.

기존 `directory`·명시적 `github` 설정과 `SnapshotRepository` 코드는 기존 설치 호환용입니다. 이전 Git 게시 번호·snapshot 저널·복구 참조는 전환 전에 확인하고 보존합니다. Drive의 평소 운영 절차와 구분해 주세요.

## 코드에 고정된 정책

아래 값은 별도 TOML 옵션이 아닙니다. 변경하면 해당 동작의 회귀 검사를 검토합니다. 호스트별 memory/load는 설정 값이고 자동 호출 간격은 LaunchAgent/timer가 정합니다.

| 정책 | 현재 값과 담당 코드 |
| --- | --- |
| 수집 일정 | Berlin 09:00·10:00·11:00, 미수집 과거 작업만 최신 시각으로 합침: `mensa/schedule.py` |
| 일반 재시도 | 15 → 30 → 60 → 120 → 240 → 360분, 이후 상한: `mensa/queue.py` |
| 모델 오류 | network/timeout/408·429·5xx 재시도, 유효한 Retry-After 반영; 영구 오류는 운영자 해제: `errors.py`, `queue.py` |
| HTML | 최대 3회, 요청 20초, 최대 5,000,000바이트: `scripts/menu_source.py` |
| 관리 모델 | 시작 30초·poll 0.25초, TERM 10초 후 필요 시 KILL 5초: `mensa/model_runtime.py` |
| 생성 요청 | Ollama 180초 / OpenAI-compatible 90초, 최대 100,000바이트: `mensa/generation.py` |
| Ollama | thinking/stream 꺼짐, temperature 0, num_ctx 8192, num_predict 4096, keep_alive 5m |
| 번역 정책 | 새 요청 menu-v4, 재사용 menu-v3/menu-v4, semantic-names-v1·semantic-components-v1 |
| 브라우저 | fetch와 JSON 읽기 포함 15초: `frontend/main.ts` |

## 개발 검사와 반영

운영 워커와 분리된 개발 체크아웃에서 관련 Python 회귀 검사·`npm test`·명시적 예제 검증을 실행합니다. 주요 변경은 [README의 전체 검사](../README.md#로컬에서-검사하고-화면-확인하기)를 적용합니다. 함수 이름·import 목록 같은 구현 모양 대신 가격 연결·날짜 보존·번역 재사용·같은 준비본 재시도·실패 시 공개본 보존처럼 실제 오류를 막는 검사를 유지합니다.

```sh
python3 -m scripts.update_menu --validate-only --data-dir tests/fixtures/menu-data
npm run build -- --data-dir tests/fixtures/menu-data --output .tmp/build-example
npm run build -- --drive-file-id FILE_ID --output .tmp/build-drive
```

빌드는 새 출력 폴더를 요구합니다. Drive 모드에는 JSON·비공개 상태·모델·로그가 없어야 하며, 로컬 예제 모드에는 선택된 같은 릴리스의 두 파일만 포함합니다. `site/`를 직접 서비스하지 않습니다. 화면 변경은 일간/주간·세 언어·가격·원문/상세·날짜 이동·초점·작은 화면을 확인합니다.

실제 코드 배포와 운영 체크아웃 fast-forward·최초 Drive 활성화는 [배포 안내](deployment.md)를 따릅니다. 문서 검토를 위해 식단을 새로 수집하거나 모델을 실행하지 않습니다. 로컬 검사와 실제 공개 읽기·워커 재개를 구별하고 관찰하지 않은 전환 완료를 기록하지 않습니다.
