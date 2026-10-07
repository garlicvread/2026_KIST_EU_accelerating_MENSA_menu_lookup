# Mensa 유지보수 설명서

이 문서는 현재 코드의 구조, 설정, 데이터 계약과 유지보수 책임을 설명합니다.

[저장소 README](../README.md), [구조 문서](architecture.md), [데이터 계약](data-contract.md), [운영 문서](operations.md)가 코드 가까이의 상세 설명입니다. 이 매뉴얼은 새 유지보수자가 필요한 설계·책임·값·절차를 여기서 바로 파악하도록 정리했습니다. 문서를 읽고 개발을 시작하는 순서는 [시작 안내](getting-started.md), 실제 반영 절차는 [사이트 배포 안내](deployment.md)를 사용해 주세요.

## 제품과 설계 방향

Saarbrücken 멘자 식단을 한국어·영어·독일어로 보여 주는 정적 사이트입니다. 하루/주간 보기, 학생·교직원·방문객 가격, 독일어 이름과 구성품·주의 표시를 제공합니다. 공개 주소는 [Mensa 식단](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/)이고 원본은 [Studierendenwerk Saarland](https://www.stw-saarland.de/gastro/mensa-saarbruecken/)입니다.

브라우저는 정적 HTML/CSS/JavaScript와 완성된 JSON을 읽습니다. Python 표준 라이브러리 배치 워커가 수집·검증·번역·게시를 수행합니다. 웹 백엔드와 데이터베이스 엔진은 없으며 큐·번역 완료 데이터·복구 정보는 비공개 JSON 파일입니다. 모델은 필요한 새 번역에만 사용합니다.

설계의 기준은 원본 사실 보존, 순수 계약과 I/O 분리, 명시적인 실행 소유권, 게시 전 완전성 검증입니다. 가격과 주의 표시는 원본/검토 자료에서 가져오고 모델은 요리·구성품만 번역합니다. 부분 진행 데이터를 공개 스냅샷으로 취급하지 않습니다. 검토된 표현을 적용해도 원래 생성/편집 출처를 보존합니다.

## 현재 디렉터리

```text
mensa/                      순수 계약·서비스·큐·runtime·게시 어댑터
scripts/                    CLI·원본 수집·빌드·Mac 설치/호환 연결
frontend/                   TypeScript 계약·상태·선택·DOM 화면
site/                       HTML/CSS/favicon 및 menu/translations 빌드 입력
data/                       검토 editorial 구문과 notice glossary
config/                     상대 경로 설정 예제와 paths-only 개발 예제
deploy/                     Linux worker TOML·service·timer 선언 예제
docs/                       구조·데이터 계약·운영 문서
tests/                      Python과 프런트엔드 계약/행동 검사
.github/workflows/          정확한 main SHA의 검증과 Pages 게시
package.json, package-lock.json, tsconfig.json
                            잠긴 TypeScript 컴파일 및 엄격한 빌드 설정
```

빌드 결과는 별도 새 디렉터리이며 기본 `.tmp/pages`입니다. 공개 결과의 `data/current.json`이 `data/releases/<ID>/menu.json`과 `translations.json`을 선택합니다. checkout/state/public은 서로 겹치지 않는 트리로 선언합니다. `.tmp`는 유지보수자가 사용할 수 있는 개발 출력 위치이며 제품 계약의 입력/상태 저장소가 아닙니다.

## 흐름과 책임

`local_refresh`는 TOML 기반 `--config` 또는 Mac 모드 `--base`를 선택합니다. `JobRunner`가 비공개 state 준비와 비대기 락을 소유하고 갱신 예정 시각을 큐에 저장합니다. 리소스와 재시도 게이트를 통과하면 수집 → 원본/가격/coverage 검증 → 번역 → 후보 완전성 검증 → 게시를 수행합니다. 작업 성공 후에만 큐의 완료 수집 시각을 저장하며 마지막 결과를 쓰기까지 락을 유지합니다.

설정 기반 `--config` 실행의 번역 재개 함수는 공개 캐시와 비공개 `translation-checkpoint.json`에서 완료 번역을 읽습니다. 이 함수는 검사를 통과한 요리별 번역을 중간 파일에 저장합니다. 설정 기반 모델 실행 함수 `model_session`은 새 번역이 필요한 순간에만 모델 연결을 준비합니다. `managed` 설정에서는 이 함수가 자신이 시작한 Ollama 프로세스 그룹만 종료하며, `external` 설정에서는 외부 모델 프로세스를 시작하거나 종료하지 않습니다. 번역 재개 함수는 모델 연결을 정리한 뒤 게시자에게 결과를 넘깁니다. Mac 기본 `--base` 실행의 `RefreshJob.translate`는 중간 저장 파일을 사용하지 않습니다. Mac 기본 경로는 공개 캐시의 완료 번역을 재사용하고 새 번역을 메모리에 보관하다가 전체 식단 검사가 끝난 뒤 저장합니다.

`directory`는 현재 공개 release를 이전 입력으로 읽고 완전한 쌍을 설치한 뒤 포인터를 교체합니다. 첫 게시에는 checkout의 `site/data`를 사용합니다. checkout/Git/HTML/assets를 쓰지 않습니다. 게시 후 큐 저장이 실패하면 다음 실행이 수집을 다시 수행하고 완료 번역을 재사용합니다.

`github`는 전용 main checkout의 두 데이터 파일만 스냅샷으로 준비하고 일반 push·명시적 workflow dispatch·실행 확인을 수행합니다. 요청 시각을 dispatch 전에 저장하고 발견한 run ID를 watch/rerun 전에 저장합니다. 복구 소유권은 저널의 base/tree/blob/단일 부모 검증으로 판단하며 커밋 메시지만으로 인간의 변경을 버리지 않습니다. unknown 변경·커밋·미추적/ignored 파일과 소유 archive refs를 보존합니다.

## 유지보수 변경 지도

| 바꿀 대상 | 먼저 볼 파일 | 함께 지킬 계약 |
| --- | --- | --- |
| 원본 HTML 추출/페이지 형태 | `scripts/menu_source.py`, `mensa/menu_contract.py` | 별도 token 대조, 원문/반복 발생 보존, 가격 source 연결 |
| 메뉴 필드/가격/schema | `mensa/menu_contract.py`, `publication.py`, `frontend/contracts.ts` | 정수 센트·공개 schema·브라우저 검증·전체 내용 표시 비교 |
| 번역 이름/구성품/주의 표현 | `data/editorial-translations.json`, `data/notice-translations.json`, `translation_contract.py` | 원문 정확한 key, en/ko 완전성, 순서/식단 의미, 실제 출처 |
| 번역 재사용/재개 | `translation_service.py`, `translation_runner.py`, `translation_runtime.py`, `checkpoints.py` | 수락 항목 JSON 저장, 설정 기반 revision 정체성, 부분 데이터 비공개 |
| endpoint/생성 실패/모델 수명 | `generation.py`, `errors.py`, `model_runtime.py` | bounded 요청, 안전한 실패 분류, 소유 프로세스만 종료 |
| 작업 시각/재시도/락 | `schedule.py`, `queue.py`, `jobs.py`, `scripts/refresh_queue.py` | pending/게시 IDs 보존, 완료 확인과 게시 분리 |
| directory/GitHub 게시 | `releases.py`, `directory_refresh.py`, `git_repository.py`, `github_publication.py`, `scripts/local_refresh.py` | 완전한 불변 쌍, 소유 복구, 정확한 SHA/run 확인 |
| 날짜/중복 메뉴/선택 행동 | `frontend/state.ts`, `selectors.ts`, `controller.ts` | Berlin 날짜, 두 보기 동일 선택, 오래된 응답 무시 |
| 카드/표시/접근성 | `copy.ts`, `display.ts`, `dom.ts`, `cards.ts`, `render.ts`, `app.ts` | 독일어 fallback, focus/열린 상세와 선택 날짜 보존 |
| 자산/정적 출력/배포 | `scripts/build_site.py`, `.github/workflows/update-and-deploy.yml` | 도달 모듈만 포함, 새 출력, 정확한 main SHA의 완전한 게시 |
| 호스트 경로/threshold/서비스 | `mensa/config.py`, `resources.py`, `config/`, `deploy/` | 명시 설정, private/public 분리, 대상 호스트 측정/권한 |

Python 도메인/계약은 네트워크·프로세스·파일 작업을 섞지 않고 순수 판단을 담당합니다. 서비스는 순서를 조합하고 어댑터가 실제 외부 작업을 담당합니다. 새 책임은 위 지도에서 가장 가까운 경계로 넣어 주세요.

## 설정 파일 계약

[`mensa/config.py`](../mensa/config.py)의 `load_worker_config`는 정확히 `[paths]`, `[inference]`, `[resources]`, `[publication]`을 요구합니다. 아래 표의 필드는 모두 필수이며 생략 시 자동 기본값을 채우지 않습니다. 알려지지 않은 키/table도 거절합니다. 문자열은 비어 있으면 안 됩니다.

| 설정 | 허용값과 의미 | 예제값 / 선택 기준 |
| --- | --- | --- |
| `paths.checkout_dir` | 코드·입력 JSON·검토 glossary 트리 | `/opt/mensa/worker-checkout` |
| `paths.state_dir` | 비공개 큐·번역 JSON 이력·로그 트리 | `/var/lib/mensa/private` |
| `paths.public_dir` | 정적 웹사이트 루트 | `/srv/www/mensa` |
| `inference.mode` | `managed` 또는 `external` | 프로세스를 워커가 소유하면 `managed` |
| `inference.provider` | managed는 `ollama`, external은 `ollama`/`openai` | 실제 요청 형식에 맞춰 선택 |
| `inference.model` | 실제 준비한 모델 이름 | `replace-with-provisioned-model`은 바꿀 placeholder |
| `inference.revision` | 운영자가 선언한 모델 실행 환경의 revision | `replace-with-provisioned-revision`은 바꿀 placeholder |
| `resources.platform` | `linux` 또는 `macos` | 예제는 `linux` |
| `resources.min_available_bytes` | 양의 정수 바이트 | 예제 `8589934592` = 8 GiB |
| `resources.max_load_per_cpu` | 양의 유한 숫자 | 예제 `0.75`; 가용 CPU 용량에 곱할 1분 평균 부하(load) 기준 |
| `publication.mode` | `directory` 또는 `github` | 예제는 `directory` |

모드별 추가 필드는 다음과 같습니다. 반대 모드의 필드를 섞어 넣을 수 없습니다.

| 모드 | 필수 추가 키 | 조건 |
| --- | --- | --- |
| `inference.mode = "managed"` | `binary`, `models_dir`, `log_dir` | 실행 파일 경로와 준비된 모델/비공개 로그 디렉터리 |
| `inference.mode = "external"` | `url` | hostname이 있는 절대 HTTP/HTTPS URL, credentials/fragment/공백 금지 |
| `publication.mode = "directory"` | 없음 | `mode`만 있는 table |
| `publication.mode = "github"` | `repository`, `workflow`, `remote` | `owner/name`, `.yml`/`.yaml` basename, 절대 로컬 경로 또는 query/fragment/credentials 없는 HTTPS remote |

상대 파일 경로는 TOML 파일의 디렉터리 기준으로 resolve하며 symlink도 해석합니다. `~`와 환경변수 표기는 문자 그대로 취급합니다. checkout/state/public 세 트리는 서로 포함하거나 같으면 안 됩니다. managed의 models/log 트리는 checkout/public과 겹칠 수 없으며 binary는 public 안에 둘 수 없습니다. 아직 없는 경로는 선언할 수 있으나 기존 경로의 종류가 다르면 거절합니다. 설정 읽기는 디렉터리 생성·모델/endpoint 접속·가중치 검증·remote 조회·워커 활성화를 하지 않습니다.

[`config/worker.example.toml`](../config/worker.example.toml)은 상대 경로의 전체 설정 예제이고 [`deploy/worker.example.toml`](../deploy/worker.example.toml)은 Linux 절대 경로의 전체 설정 예제입니다. [`config/development.example.toml`](../config/development.example.toml)은 별도 `load_worker_paths`를 위한 `[paths]` 전용 예제입니다. 이 파일은 `local_refresh --config`의 전체 설정을 대체하지 않습니다.

external Ollama는 실제 생성 단계에서 loopback hostname/IP의 `/api/chat`만 허용하고 query도 금지합니다. OpenAI-compatible URL은 완전한 요청 endpoint를 지정합니다. 설정 기반 CLI에는 API key TOML 필드나 key CLI 옵션이 없으며 현재 빈 key로 연결합니다. 인증이 필요한 endpoint에는 별도의 검토된 key 전달 연결이 필요합니다. 수동 `update_menu`의 환경변수 경로와 혼동하지 마세요.

설정 기반 재사용 정체성은 provider/model/revision과 번역 정책/glossary 지문을 포함합니다. managed 포트나 external URL 및 API key는 일시적인 연결 정보이며 설정 기반 이력의 정체성에 넣지 않습니다. revision 선언은 실제 가중치를 증명하지 않습니다. 모델 실행 환경의 내용이 바뀌면 운영자가 revision을 관리해 주세요.

## 실행 명령과 기본값

프로젝트 루트에서 실행하며 상태를 쓰는 작업은 서비스와 같은 사용자로 수행해 주세요.

| 진입점 | 옵션과 기본값 | 효과 |
| --- | --- | --- |
| `python3 -m scripts.local_refresh` | `--config PATH` / `--base PATH` 중 하나 필수, `--status` 선택 | 읽기 전용 상태 조회 또는 큐 작업 한 번 |
| `python3 -m scripts.install_local_worker` | `--base` 기본 `$HOME/Library/Application Support/Mensa`, `--write-only` 선택 | Mac user agent 작성; 기본은 해당 label 등록, write-only는 등록/중지 생략 |
| `python3 -m scripts.update_menu` | `--validate-only` 선택 | validate-only는 저장된 후보 계약 검사; 옵션 없으면 수집·번역·`site/data` 교체 |
| `python3 -m scripts.build_site` | `--source` 기본 `site`, `--output` 기본 `.tmp/pages`, `--compiled` 기본 `dist/frontend`, `--glossary` 기본 `data/notice-translations.json` | 이미 컴파일된 모듈과 완전한 JSON으로 새 정적 출력 생성 |

빌더의 기본 경로는 소스 checkout 루트 기준입니다. 출력은 아직 존재하지 않는 경로여야 합니다. `--validate-only`는 날짜 freshness를 판정하지 않으며 게시 전에 `require_current_coverage`도 확인합니다. 모든 argparse 진입점의 `--help`로 옵션 설명을 볼 수 있습니다.

```sh
python3 -m scripts.local_refresh --config /etc/mensa/worker.toml --status
python3 -m scripts.local_refresh --config /etc/mensa/worker.toml
```

`--status`를 받은 식단 갱신 프로그램은 작업 기록 폴더의 `queue.json`과 `last-result.json`을 읽고 대기 작업과 마지막 실행 결과를 출력합니다. 이 조회는 파일을 변경하거나 첫 실행 전의 작업 기록 폴더를 만들지 않습니다. `--status` 없이 수동 실행하면 프로그램은 하루 세 번의 갱신 시각, 실패 후 대기 시간, 자동 재시도 중지 여부와 메모리·CPU 기준을 확인한 뒤 갱신을 진행합니다. 결과의 `idle`, `waiting`, `deferred`, `busy`, `completed`, `failed`, `blocked`를 구별해 주세요. `failed`/`blocked`는 CLI exit 1입니다.

수동 입력 준비용 [`scripts/translations.py`](../scripts/translations.py)의 환경변수는 `MENU_TRANSLATION_PROVIDER`(기본 `openai`), `MENU_TRANSLATION_URL`, `MENU_TRANSLATION_MODEL`, `MENU_TRANSLATION_API_KEY`입니다. 기본 provider에서 URL/model/key가 모두 없으면 모델 설정 없이 검토 데이터와 캐시를 사용합니다. 설정할 때 URL/model은 함께 필요하며 key는 선택입니다. URL은 HTTPS 또는 loopback HTTP이고 credentials/query/fragment를 포함할 수 없습니다. 이는 `local_refresh --config`의 TOML 설정과 별도 경로입니다.

## 코드에 고정된 정책

다음 값은 TOML 옵션이 아닙니다. 바꾸려면 해당 코드 계약과 관련 검사를 함께 검토해 주세요. 호스트에 맞춰 조정하는 memory/load는 위 TOML 필드이고, 스케줄러 간격은 서비스 선언입니다.

| 정책 | 현재 값 | 소유 모듈 |
| --- | --- | --- |
| 일별 갱신 | 매일 09:00·10:00·11:00 `Europe/Berlin`; 미수집 작업만 최신 갱신 예정 시각으로 합침 | `mensa/schedule.py` |
| 일반 재시도 | 15 → 30 → 60 → 120 → 240 → 360분, 이후 360분 상한 | `mensa/queue.py` |
| generation 재시도 | network/timeout/HTTP 408·429·5xx는 재시도, 안전한 Retry-After와 backoff 중 큰 값 적용 | `mensa/errors.py`, `queue.py`, `generation.py` |
| 영구 generation 실패 | 자동 재시도 없음; 날짜 변경에도 게이트 보존 | `mensa/queue.py` |
| HTML 수집 | 최대 3회, 요청 timeout 20초, 최대 5,000,000바이트 | `scripts/menu_source.py` |
| managed 모델 시작/정지 | 시작 예산 30초, poll 0.25초; 소유 그룹 TERM 대기 10초 후 KILL 대기 5초 | `mensa/model_runtime.py` |
| 생성 요청 | Ollama 180초 / OpenAI-compatible 90초, 응답 최대 100,000바이트 | `mensa/generation.py` |
| Ollama 생성 설정 | thinking/stream 꺼짐, temperature 0, num_ctx 8192, num_predict 4096, keep_alive 5m | `mensa/generation.py` |
| 생성 정책 | prompt `menu-v4`, 재사용 지원 `menu-v3`/`menu-v4`, `name_policy_version = "semantic-names-v1"`, `component_policy_version = "semantic-components-v1"` | `mensa/translation_contract.py` |
| GitHub 확인 | 보이지 않는 dispatch의 30분 확인 대기; 발견 조회 최대 12회×2초; watch timeout 600초 | `scripts/local_refresh.py` |
| 브라우저 요청 | fetch와 JSON 읽기 포함 15초 | `frontend/main.ts` |

## 데이터와 화면의 연결

메뉴 ID는 날짜/카테고리/독일어 이름의 hash이며 반복 발생에 `-2`, `-3` 등을 붙입니다. 수집기는 모든 원본 발생을 JSON과 앱 상태에 보존합니다. `frontend/selectors.ts`의 `visibleMeals(day)`는 같은 날짜에서 ID를 제외한 전체 내용이 같은 항목만 첫 번째로 표시합니다. 비교에는 분류·위치·원문·translation_key·순서가 있는 구성품/주의 표시·전체 가격·가격 source/status가 포함됩니다. 일간과 주간이 모두 이 선택을 사용하며 번역 제목이나 선택 가격 하나로 합치지 않습니다. 첫 번째 ID로 카드와 상세 상태를 유지합니다.

가격은 같은 날짜/카테고리/독일어 이름의 S/M/G 원본 가격과 정수 센트 전부를 대조합니다. 부분 블록이나 원래 있던 가격 소실은 게시를 막습니다. 가격 미제공 항목은 null/source_pending이며 원본 링크로 안내합니다. 다른 날짜/요리나 PDF에서 채우지 않습니다. 원문 요리/구성품의 notices는 보존하며 미검토 glossary key는 생성/후보 쓰기 전에 거절합니다.

번역 key는 원문 이름과 순서가 있는 구성품 이름 JSON의 SHA256입니다. entries는 source, en/ko의 name/components, origin/model/prompt_version과 해당 생성 metadata를 보관합니다. 원문 연결·구성품 수·두 언어·notice coverage가 완성되어야 공개 후보입니다. 정책을 적용해도 생성/편집 출처를 바꾸지 않습니다. 설정 기반 이력의 provider/model/revision과 정책/glossary 지문은 재사용 정체성이며 endpoint/credential은 제외됩니다.

공개 포인터는 정확히 schema_version 1과 소문자 SHA256 release_id를 가집니다. 게시자는 canonical menu/cache 바이트 길이와 내용을 묶은 SHA256으로 ID를 만들고 완전한 쌍을 설치한 뒤 포인터를 원자적으로 바꿉니다. 이전 release는 보존합니다. Python reader는 고정 쌍의 digest/계약을 검사하고 브라우저는 포인터/schema/고정 경로/원문 연결을 검사합니다. 브라우저는 digest를 다시 계산하지 않습니다.

메뉴와 번역 요청 상태는 독립적입니다. 유효한 메뉴가 먼저 오면 독일어로 표시하며 번역 실패는 별도 안내합니다. 요청 순서가 달라져도 오래된 응답은 새 상태를 덮어쓰지 않습니다. 언어·가격·번역 변경 때 화면 코드는 선택 날짜와 열린 상세를 유지합니다. 날짜·주·보기 방식을 바꾸면 frontend/app.ts의 navigate 함수가 메뉴와 보조기기의 읽기 영역을 갱신합니다. navigate 함수는 날짜 버튼과 날짜 선택창 등 화면에 남아 있는 선택 도구의 키보드 초점을 유지하며, 별도의 스크롤 명령을 실행하지 않습니다. 주간 화면의 “하루 보기” 버튼은 일간 화면으로 전환할 때 사라지므로, 이 경우에만 navigate 함수가 날짜 선택창으로 키보드 초점을 옮깁니다. 이 초점 이동에도 preventScroll 옵션을 사용하여 화면 위치를 유지합니다. 오늘 날짜와 메뉴 최신성은 Berlin 기준이며, 화면은 공개 범위·수집 시각·지난 식단 여부를 표시합니다.

## 호스트와 게시 운영의 소유자

| 대상 | 준비/관리 책임 |
| --- | --- |
| checkout | 유지보수자가 검토 코드·입력 JSON·glossary를 제공합니다. directory는 읽기, GitHub는 전용 main 쓰기가 필요합니다. |
| private state | 서비스 사용자가 0700 state의 큐/최근 결과/부분 번역 이력/저널을 관리합니다. 상위 경로와 일관된 백업은 운영자가 준비합니다. |
| managed inference | 운영자가 실행 파일/모델 가중치/비공개 runtime·models·log 권한을 준비하고 워커가 필요한 실행 그룹만 소유합니다. 자동 다운로드는 없습니다. |
| external inference | endpoint 운영자가 가동/정지/인증을 관리합니다. 현재 config CLI의 key는 빈 값입니다. |
| directory public root | 운영자가 HTML/assets 전체 빌드와 기존 data 소유권/서버 읽기 권한을 준비합니다. 워커는 data 포인터/쌍만 갱신합니다. |
| GitHub publication | 운영자가 origin/repository/workflow와 CLI 인증을 준비합니다. worker는 데이터 SHA와 run 확인, Actions는 검증/빌드/Pages 게시를 담당합니다. |
| 스케줄러 | 처리 기회를 제공합니다. 일별 갱신·미수집 날짜 합침·backoff·완료 기록은 큐가 소유합니다. |

Linux 예제는 `/opt/mensa/worker-checkout`, `/etc/mensa/worker.toml`, `/var/lib/mensa/private`, `/srv/www/mensa`를 사용합니다. service는 사용자 mensa, oneshot, state 0700, restrictive umask, private state와 public data만 쓰기 허용, 종료 30초/control-group을 선언합니다. timer는 부팅 2분 뒤와 작업 종료 15분 뒤에 기회를 제공합니다. 템플릿은 실제 host 준비나 모델 적합성 증명이 아닙니다. proc/cgroup-v2와 affinity의 보이는 제한을 반영하지만 경쟁 GPU·숨은 상위 제한은 운영자가 확인합니다.

Mac `--base`는 전용 checkout/runtime/ollama/models/logs와 고정 gemma4:31b/GitHub 대상 정책입니다. memory 32 GiB, 1분 평균 부하(load) max(2, 0.6×logical CPUs), AC/배터리 없음 증거를 요구합니다. installer의 user agent는 로그인 시/900초 간격이며 모델 설치나 강제 깨우기를 하지 않습니다.

## 실패와 복구를 다룰 때

운영자는 `--status` 출력에서 대기 작업의 period/phase/attempts/next_attempt_at과 commit_sha/run_id/dispatch_requested_at, 최근 실행 결과를 함께 확인해 주세요. `translation-checkpoint.json`은 `--config` 경로에서 저장한 완료 항목 데이터이며 부분 캐시일 수 있습니다. Mac 기본 `--base` 경로에는 이 파일이 없습니다. 운영자는 `waiting`이나 종료 코드 0만으로 새 게시가 성공했다고 판단하지 말고, 완료 수집 시각과 공개 데이터의 수집 시각을 대조해 주세요. 운영자가 복구 기록을 백업할 때는 자동 실행과 현재 작업을 중지하고 worker.lock 잠금 안에서 큐/최근 결과/번역 이력/저널/TOML을 일관된 비공개 사본으로 보존해 주세요. Git 게시 경로를 복구하는 운영자는 HEAD/main/upstream과 `refs/mensa/superseded/`의 커밋도 보존해 주세요.

먼저 원인을 수정합니다. 영구 generation 실패의 명시적 해제는 `mensa.queue.request_retry`를 사용하며 전용 CLI 옵션은 없습니다. 운영 문서의 락/파일 검사 snippet을 따라 period/phase/attempts/게시 IDs를 보존해 주세요. queue/checkpoint/journal 삭제나 임의 ID 초기화는 하지 않습니다. GitHub의 알려진 run을 먼저 조회하고 응답이 사라진 dispatch는 같은 SHA로 찾습니다. 대체 커밋과 원래 실행의 충돌·만료 후보·unknown 변경은 운영자 검토 대상입니다.

로그는 모델 시작 시 최신 실행으로 덮어쓰며 byte cap/과거 보존이 없습니다. 로그 회전과 디스크 감시는 운영자가 준비합니다. 롤백은 검증된 전체 정적 빌드와 같은 release의 JSON 쌍을 단위로 합니다. 비공개 상태 복구는 현재 게시 SHA/run과 백업의 period/IDs/저널을 대조한 뒤 같은 락 아래에서 수행합니다.

## 개발 확인과 반영 준비

유지보수자는 Python 3.12/시간대 데이터와 잠금 파일의 TypeScript 7.0.2를 준비한 뒤 Python 회귀 검사, `npm test`, 저장된 후보 validate-only를 실행해 주세요. 유지보수자는 변경된 기능에 맞는 검사를 선택하고 새 빌드 경로에서 실제 일간/주간·세 언어/가격·원문/상세·날짜 이동·작은 화면을 확인해 주세요. Python 빌더는 컴파일러를 실행하지 않으므로 compile 또는 npm build를 사용해 주세요.

유지보수자는 가격 연결·날짜 보존·번역 재사용·실패 시 게시본 보존처럼 현재 동작의 오류를 검출하는 테스트를 유지해 주세요. 함수 이름·import 목록·내부 호출 순서처럼 개발 당시 구현 모양만 확인하는 검사는 상시 회귀 검사에 남기지 않습니다. 유지보수자는 새 검사를 추가할 때 어떤 실제 오류를 막는지, 기존 검사와 같은 결과를 반복 확인하는지 먼저 판단해 주세요. 같은 규칙에 대한 불필요한 입력 변형을 늘리거나 테스트 개수를 품질 목표로 삼지 않습니다.

정적 출력에는 도달 가능한 해시 경로의 JS/CSS, 선언된 HTML/favicon, 선택된 공개 JSON 쌍만 포함됩니다. 원본 TypeScript/비공개 상태/모델/로그를 게시하지 않습니다. `site/`를 직접 서비스하지 마세요. 저장된 후보 계약 검사와 현재 날짜 coverage 검사는 구분합니다.

실제 반영은 검토된 변경을 보존하고 정확한 main SHA를 지정하는 release 절차입니다. GitHub workflow는 수동 dispatch만 받으며 main 소속 확인 → 테스트 → 후보/현재 coverage 검증 → 빌드 → Pages 게시를 수행합니다. 구체적인 순서와 완료 확인은 [사이트 배포 안내](deployment.md)를 따라 주세요.
