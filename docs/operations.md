# 식단 갱신 프로그램의 설정과 운영

식단을 주기적으로 수집·번역해 JSON 파일을 준비하는 Python 프로그램을 이 문서에서는 워커라고 부릅니다. 운영자는 [`scripts/local_refresh.py`](../scripts/local_refresh.py)의 실행 명령과 Linux/Mac 자동 실행 설정을 관리합니다. 이 프로그램에는 웹 백엔드나 데이터베이스 엔진이 없으며 대기 작업·완료 번역·마지막 실행 결과를 파일로 저장합니다.

운영자는 Python 3.12 이상과 `Europe/Berlin` 시간대 데이터를 준비해 주세요. 브라우저용 TypeScript 컴파일은 게시용 사이트를 만들 때 필요하며, 매일 식단 갱신 프로그램은 HTML/CSS/JavaScript를 만들지 않습니다.

## 설정 파일에 지정하는 폴더와 모델

설정을 읽는 `load_worker_config` 함수([`mensa/config.py`](../mensa/config.py))는 운영자가 `--config`로 지정한 TOML 파일을 읽습니다. 파일에는 정확히 `[paths]`, `[inference]`, `[resources]`, `[publication]` 네 부분이 필요합니다. 이 함수는 필수 값의 누락이나 알 수 없는 키를 오류로 처리하며 생략한 값을 기본값으로 채우지 않습니다. 문자열은 빈 값으로 둘 수 없습니다.

운영자는 먼저 코드 폴더, 웹에 공개하지 않을 작업 기록 폴더, 웹서버가 읽는 사이트 폴더를 나눠 지정해 주세요. 아래 경로는 Linux 예제의 값이며 실제 서버 경로에 맞춰 바꿀 수 있습니다.

| 필수 키 | 프로그램이 이 값을 사용하는 방법 | 예제값 |
| --- | --- | --- |
| `paths.checkout_dir` | 원본 입력 JSON과 사람이 검토한 번역 파일을 읽는 코드 폴더입니다. | `/opt/mensa/worker-checkout` |
| `paths.state_dir` | queue.json, 마지막 결과, 완료 번역과 복구 기록을 저장하는 비공개 작업 폴더입니다. | `/var/lib/mensa/private` |
| `paths.public_dir` | 웹서버에 제공할 HTML과 data 폴더가 있는 사이트 최상위 폴더입니다. | `/srv/www/mensa` |
| `inference.mode` | `managed`이면 프로그램이 Ollama를 시작/종료하고, `external`이면 이미 실행 중인 모델에 요청합니다. | `managed` |
| `inference.provider` | 모델 요청 형식을 고릅니다. managed는 `ollama`, external은 `ollama` 또는 `openai`입니다. | `ollama` |
| `inference.model` | 요청할 모델 이름입니다. 운영자가 실제 설치한 이름을 넣습니다. | `replace-with-provisioned-model`은 교체할 예시 문자열입니다. |
| `inference.revision` | 저장된 번역이 같은 모델 버전에서 만들어졌는지 구별하는 운영자 선언입니다. | `replace-with-provisioned-revision`은 교체할 예시 문자열입니다. |
| `resources.platform` | 메모리/CPU/전원을 읽을 운영체제를 고릅니다. | `linux` 또는 `macos`; 예제는 `linux`입니다. |
| `resources.min_available_bytes` | 실행을 허용할 최소 가용 메모리입니다. 양의 정수 바이트를 지정합니다. | `8589934592` = 8 GiB |
| `resources.max_load_per_cpu` | 가용 CPU 용량에 곱해 허용할 1분 평균 부하(load)를 정합니다. 양의 유한 숫자가 필요합니다. | `0.75` |
| `publication.mode` | `directory`이면 사이트 data에 파일을 쓰고, `github`이면 GitHub 저장소와 배포 작업을 통해 게시합니다. | `directory` |

설정을 읽는 함수는 선택한 방식에 필요한 추가 키만 허용합니다. 운영자가 managed와 external의 키를 한 부분에 함께 넣으면 설정을 읽는 함수는 ValueError 예외를 발생시켜 설정을 거절합니다.

| 선택한 방식 | 운영자가 추가할 필수 키 | 지정할 대상 |
| --- | --- | --- |
| `inference.mode = "managed"` | `binary`, `models_dir`, `log_dir` | Ollama 실행 파일, 이미 준비한 모델 파일 폴더, 공개하지 않을 모델 로그 폴더입니다. |
| `inference.mode = "external"` | `url` | 번역 요청을 받을 모델의 전체 HTTP/HTTPS 주소입니다. hostname이 필요하며 계정/암호·fragment·공백을 넣을 수 없습니다. |
| `publication.mode = "directory"` | 없음 | 이 부분에는 `mode`만 둡니다. |
| `publication.mode = "github"` | `repository`, `workflow`, `remote` | repository는 `owner/name`, workflow는 `.yml`/`.yaml` 파일 이름, remote는 Git origin에 사용할 절대 로컬 경로 또는 HTTPS 주소입니다. HTTPS remote에는 query/fragment/계정/암호를 넣지 않습니다. |

설정 함수는 상대 경로를 TOML 파일이 있는 폴더에 붙여 해석합니다. 예를 들어 `/etc/mensa/worker.toml`의 `state_dir = "../state"`는 `/etc/state`를 가리킵니다. 함수는 symlink의 실제 경로도 확인합니다. `~`나 `$HOME`을 확장하지 않으므로 운영자는 절대 경로나 일반 상대 경로를 적어 주세요.

설정 함수는 코드/작업 기록/사이트 세 폴더가 서로 같거나 다른 폴더 안에 들어가면 거절합니다. 이 분리는 작업 기록과 모델 파일이 웹사이트로 공개되는 일을 막습니다. managed의 models_dir/log_dir도 코드/사이트 폴더와 겹칠 수 없으며 binary는 사이트 폴더 안에 둘 수 없습니다. 운영자는 아직 없는 경로를 설정에 적을 수 있으나, 이미 있는 경로가 파일/폴더 중 잘못된 종류이면 함수가 거절합니다.

운영자는 [상대 경로 전체 설정 예제](../config/worker.example.toml) 또는 [Linux 절대 경로 전체 설정 예제](../deploy/worker.example.toml)를 복사해 실제 값을 채워 주세요. [개발 경로 예제](../config/development.example.toml)는 `load_worker_paths` 함수가 읽는 `[paths]` 전용 파일입니다. 네 부분을 요구하는 `local_refresh --config`에는 이 개발 파일을 사용할 수 없습니다.

설정 함수는 값을 읽고 검사할 뿐입니다. 함수가 성공했다고 폴더·서비스·모델이 설치된 것은 아니며 모델 주소 접속, 가중치나 GitHub 연결도 확인하지 않습니다. 운영자는 실행 계정과 모델 파일을 별도로 준비해야 합니다.

## 모델 주소와 저장된 번역의 재사용

번역 요청 함수 `request_translation`([`mensa/generation.py`](../mensa/generation.py))는 provider가 `ollama`이면 같은 컴퓨터를 가리키는 loopback hostname/IP의 `/api/chat` 주소만 허용하고 URL query도 거절합니다. provider가 `openai`이면 OpenAI-compatible 요청을 받을 전체 URL을 사용합니다. 운영자는 서버 최상위 주소가 아니라 실제 요청 주소를 url에 적어 주세요.

TOML을 사용하는 `local_refresh` 명령에는 API key 설정이나 CLI 옵션이 없으며 모델 요청에 빈 key를 전달합니다. API key 인증이 필요한 모델을 연결하려면 유지보수자가 key 전달 코드를 별도로 검토해 연결해야 합니다. 아래 수동 `update_menu` 명령의 환경변수와 같은 설정이 아닙니다.

번역 생성 조건을 기록하는 generation_identity 함수와 이전 번역의 재사용 여부를 판단하는 reusable_translation 함수는 `mensa/translation_contract.py`에 있습니다. generation_identity는 provider/model/선언된 revision과 번역 규칙·검토된 주의 표시 대응표를 SHA256 지문으로 기록합니다. reusable_translation은 모델이 만든 번역의 요청 규칙 버전·모델 이름·저장된 생성 조건을 확인해 다시 사용할 수 있는지 판단합니다. TOML 기반 번역에서는 이 기록에 임시 모델 포트·URL·API key를 넣지 않습니다. 운영자는 같은 주소 뒤의 모델 내용이 바뀌면 revision도 바꿔 주세요. revision은 운영자의 선언이며 프로그램이 가중치 파일을 증명한 값은 아닙니다.

## 명령별 옵션과 기본값

운영자나 유지보수자는 프로젝트 최상위 폴더에서 아래 명령을 실행합니다. 각 명령에 `--help`를 붙이면 지원 옵션을 볼 수 있습니다.

| 실행 명령 | 옵션과 기본값 | 명령이 읽거나 바꾸는 대상 |
| --- | --- | --- |
| `python3 -m scripts.local_refresh` | `--config PATH` / `--base PATH` 중 하나 필수; `--status` 선택 | 지정 위치의 대기 작업/최근 결과를 출력하거나 식단 갱신을 한 번 실행합니다. |
| `python3 -m scripts.install_local_worker` | `--base` 기본 `$HOME/Library/Application Support/Mensa`; `--write-only` 선택 | 로그인 사용자의 Mac LaunchAgent 파일을 작성합니다. 기본은 해당 자동 실행을 등록하며 write-only는 등록/중지를 생략합니다. |
| `python3 -m scripts.update_menu` | `--validate-only` 선택 | 선택 시 `site/data/menu.json`·`translations.json`을 검사합니다. 옵션이 없으면 원본 수집·번역 후 이 두 입력 파일을 교체합니다. |
| `python3 -m scripts.build_site` | `--source` 기본 `site`; `--output` 기본 `.tmp/pages`; `--compiled` 기본 `dist/frontend`; `--glossary` 기본 `data/notice-translations.json` | HTML/CSS/JSON·컴파일된 JavaScript·검토 대응표를 읽고 output 폴더에 게시용 사이트를 만듭니다. |

사이트 생성 프로그램 `scripts/build_site.py`의 기본 입력/출력 위치는 코드가 있는 프로젝트 최상위 폴더 기준입니다. 프로그램은 `--output` 폴더를 새로 만들며, 기존 파일을 보호하기 위해 그 폴더가 이미 있으면 중단합니다. 유지보수자는 아직 사용하지 않은 `.tmp/build-example`처럼 폴더 이름을 골라 주세요. 기존 결과 폴더를 삭제할 필요는 없습니다. Python 사이트 생성 명령은 컴파일러를 실행하지 않으므로 먼저 `npm run compile`을 실행하거나 컴파일까지 하는 `npm run build -- --output .tmp/build-example`을 사용해 주세요.

`update_menu --validate-only`는 저장된 두 JSON이 표시 가능한 형식인지 검사하며 식단의 마지막 날짜가 오늘보다 과거인지는 검사하지 않습니다. 게시 전에 날짜 검사 함수 `require_current_coverage`([`scripts/update_menu.py`](../scripts/update_menu.py))도 호출해야 합니다. 이 함수는 `menu.json`의 coverage.end가 Berlin 오늘보다 과거이면 거절합니다.

수동 입력 갱신 명령의 모델 설정 함수 `model_config`([`scripts/translations.py`](../scripts/translations.py))는 `MENU_TRANSLATION_PROVIDER`(기본 `openai`), `MENU_TRANSLATION_URL`, `MENU_TRANSLATION_MODEL`, `MENU_TRANSLATION_API_KEY` 환경변수를 읽습니다. 기본 provider에서 URL/model/key가 모두 없으면 모델을 요청하지 않고 검토된 구문과 저장된 번역을 사용합니다. 운영자가 모델을 설정하면 URL/model은 함께 필요하며 key는 선택입니다. 함수는 HTTPS 또는 loopback HTTP를 허용하고 URL 안의 계정/암호·query·fragment는 거절합니다.

## 상태 확인과 실제 실행을 맡는 OS 계정

Linux 자동 실행 설정 파일 [`deploy/mensa-refresh.service`](../deploy/mensa-refresh.service)의 `User=mensa`는 Python 프로그램을 OS 계정 `mensa`로 실행한다는 뜻입니다. 운영자가 작업 기록을 바꾸는 수동 실행이나 재시도를 할 때도 이 계정을 사용해 주세요. 다른 계정이 queue.json을 만들면 다음 자동 실행의 mensa 계정이 파일을 고치지 못할 수 있습니다. 관리자 계정에서 실행할 예는 다음과 같습니다.

```sh
cd /opt/mensa/worker-checkout
sudo -u mensa /usr/bin/python3.12 -m scripts.local_refresh --config /etc/mensa/worker.toml --status
sudo -u mensa /usr/bin/python3.12 -m scripts.local_refresh --config /etc/mensa/worker.toml
```

`--status`를 받은 local_refresh.main은 설정의 state_dir에서 queue.json의 대기 수집 시각 작업과 last-result.json의 마지막 실행 결과를 읽어 출력합니다. main은 파일을 수정하지 않고 첫 실행 전에도 state_dir 폴더를 만들지 않습니다. 메모리/CPU 측정, 원본 수집, 모델 요청과 게시도 실행하지 않습니다.

`--status` 없는 명령은 실제 갱신을 시도합니다. 프로그램은 수동 실행에서도 하루 세 번의 갱신 시각·실패 후 대기·자동 재시도 중지·메모리/CPU 기준을 따릅니다. `idle`은 대기 작업 없음, `waiting`은 재시도 시각 전, `deferred`는 컴퓨터 실행 조건 미충족, `busy`는 다른 실행이 파일 잠금 보유, `completed`는 완료, `failed`/`blocked`는 실패/자동 재시도 중지입니다. failed/blocked이면 명령은 exit 1로 끝납니다.

Mac `--base` 명령은 LaunchAgent를 등록한 로그인 사용자가 실행해 주세요. LaunchAgent는 그 사용자의 로그인 세션에서 프로그램을 자동 실행하는 macOS 기능입니다. 이 경로의 수동 실행을 다른 사용자로 수행하면 base 아래 작업 기록의 소유자가 달라질 수 있습니다.

```sh
python3 -m scripts.local_refresh --base "$HOME/Library/Application Support/Mensa" --status
python3 -m scripts.local_refresh --base "$HOME/Library/Application Support/Mensa"
```

Mac 코드 폴더는 base/checkout, Ollama 실행 파일은 base/runtime/ollama, 모델 파일은 base/models, 로그는 base/logs입니다. 프로그램은 base의 queue.json/last-result.json에 진행 기록을 저장하고 고정 모델 gemma4:31b와 `garlicvread/2026_KIST_EU_accelerating_MENSA_menu_lookup`의 `update-and-deploy.yml`을 사용합니다. 설치 프로그램 `scripts.install_local_worker.py`는 사용자 plist를 작성해 로그인 시와 900초 간격의 실행을 등록합니다. 사용자 로그인이 필요하며 컴퓨터 강제 깨우기나 모델 설치는 하지 않습니다.

## 설정으로 바꿀 값과 코드에 정해진 값

운영자는 TOML의 memory/load 기준을 컴퓨터에 맞춰 정하고, Linux timer/Mac LaunchAgent의 실행 간격을 관리합니다. 아래 값은 현재 CLI가 사용하는 코드의 값이며 TOML 옵션이 아닙니다. 유지보수자가 이 값을 바꾸면 관련 함수의 검사도 함께 검토해야 합니다.

| 담당 코드 | 그 코드가 적용하는 현재 동작/값 |
| --- | --- |
| `mensa/schedule.py`, `queue.py` | 매일 09:00·10:00·11:00 Europe/Berlin 이후 최신 수집 시각을 대기 작업으로 만듭니다. 아직 수집하지 않은 옛 작업만 최신 수집 시각으로 합칩니다. |
| `mensa/queue.py` | 일반 실패 후 15 → 30 → 60 → 120 → 240 → 360분 기다리며 이후 360분 상한을 사용합니다. |
| `mensa/errors.py`, `queue.py`, `generation.py` | network/timeout/HTTP 408·429·5xx 실패는 재시도합니다. HTTP Retry-After가 유효하면 위 대기 시간과 응답의 대기 시간 중 큰 값을 사용합니다. |
| `mensa/queue.py` | 설정·인증·결과 형태 등 자동 재시도 불가로 분류한 모델 실패는 운영자가 해제할 때까지 중지합니다. 날짜가 바뀌어도 중지를 지우지 않습니다. |
| `scripts/menu_source.py` | 원본 HTML을 최대 3회 요청하고 각 요청은 20초, 본문은 최대 5,000,000바이트로 제한합니다. |
| `mensa/model_runtime.py` | managed 모델 시작 예산은 30초, 준비 확인 간격은 0.25초입니다. 종료 요청 TERM 후 10초 기다리고 필요하면 KILL 후 5초 기다립니다. |
| `mensa/generation.py` | 번역 요청은 Ollama 180초/OpenAI-compatible 90초, 응답은 최대 100,000바이트입니다. |
| `mensa/generation.py` | Ollama 요청은 thinking/stream 꺼짐, temperature 0, num_ctx 8192, num_predict 4096, keep_alive 5m입니다. |
| `mensa/translation_contract.py` | 새 번역 요청 규칙은 menu-v4, 재사용 지원 버전은 menu-v3/menu-v4입니다. 변경한 표현에는 name_policy_version="semantic-names-v1", component_policy_version="semantic-components-v1"을 해당 항목에 기록합니다. |
| `scripts/local_refresh.py` | GitHub 게시 요청이 조회되지 않으면 30분 확인 대기를 지킵니다. 요청 직후 조회는 최대 12회×2초, 실행 진행 확인은 timeout 600초입니다. |
| `frontend/main.ts` | 브라우저 파일 요청과 JSON 읽기는 15초로 제한합니다. |

컴퓨터 여유를 측정하는 [`mensa/resources.py`](../mensa/resources.py)는 TOML platform에 맞춰 측정합니다. Linux에서는 읽을 수 있는 proc/cgroup-v2 제한과 CPU affinity를 반영합니다. Linux 코드의 전원 허용은 서버 운용 기준이며 AC 전원 측정 결과가 아닙니다. 운영자는 보이지 않는 상위 제한·다른 GPU 작업·모델 크기에 따른 실제 적합성을 별도로 확인해야 합니다. 측정이 실패하면 프로그램은 작업을 연기합니다.

Mac --base의 고정 기준은 가용 메모리 32 GiB, 1분 평균 부하 `max(2, 0.6 × logical CPUs)` 이하, AC 전원 또는 배터리 없음의 명시적인 측정 결과입니다. 측정값이 알 수 없으면 프로그램은 준비되었다고 판단하지 않습니다.

## 게시 방법별로 운영자가 준비할 것

사이트 폴더에 직접 쓰는 `directory` 방식에서는 운영자가 먼저 [README의 사이트 생성 절차](../README.md#로컬에서-코드와-화면-확인하기)로 HTML/CSS/JavaScript와 초기 메뉴/번역을 준비합니다. 예제의 `/srv/www/mensa`를 정적 웹서버가 읽게 하고 실제 `/srv/www/mensa/data` 폴더의 기존 내용까지 mensa 계정이 쓸 수 있게 준비해 주세요. Python 프로그램은 이 data와 작업 기록만 갱신합니다.

JSON을 게시하는 DirectoryPublisher([`mensa/releases.py`](../mensa/releases.py))는 같은 식단의 menu.json과 translations.json을 하나의 묶음으로 저장합니다. 클래스는 두 파일 내용에서 계산한 ID로 data/releases/<ID>/ 폴더를 만들고, data/current.json에 지금 사용할 ID를 적습니다. 브라우저는 이 선택 파일을 한 번 읽어 같은 ID 폴더의 두 JSON을 읽습니다. 예제 Linux 경로에서는 /srv/www/mensa/data/current.json이 /srv/www/mensa/data/releases/<ID>/의 파일을 선택합니다. DirectoryPublisher는 두 파일을 완성한 뒤 선택 파일을 교체하고 이전 묶음을 남깁니다.

DirectoryPublisher는 자신이 새로 만든 폴더에 0755, 파일에 0644 권한을 줍니다. 이 클래스는 이미 있는 폴더의 소유권/잘못된 권한을 고치지 않으므로 운영자가 준비해야 합니다. 운영자는 설정 파일·queue.json·번역 중간 기록·모델·로그를 사이트 폴더 아래에 두거나 웹서버에 연결하지 마세요.

GitHub를 거치는 `github` 방식에서는 운영자가 쓰기 가능한 전용 main 코드 폴더와 Git/GitHub CLI 인증을 준비합니다. 코드 폴더의 origin 주소는 설정의 remote와 정확히 같아야 합니다. repository는 GitHub 작업 대상이고 remote는 Git 연결 주소이며 설정 함수는 두 선언이 같은 저장소인지 접속해 확인하지 않습니다. 이 방식의 프로그램은 public_dir에 직접 쓰지 않고 저장소의 두 데이터 파일과 GitHub 배포 작업을 사용합니다. 읽기 전용 코드 폴더를 가정한 아래 directory용 Linux 설정 파일을 그대로 적용하면 안 됩니다.

작업 실행 클래스 JobRunner([`mensa/jobs.py`](../mensa/jobs.py))는 state_dir이 다른 사용자에게 열리지 않은 0700 폴더인지 검사합니다. 운영자는 이 폴더의 상위 디렉터리를 미리 준비하고 queue/result/lock 파일을 symlink가 아닌 정상 파일로 유지해 주세요. managed 모델 실행 함수도 준비된 실행 파일·모델 파일·비공개 models/log 폴더와 그 상위 경로를 요구합니다. 프로그램은 모델을 다운로드하지 않습니다. external 모델의 시작/종료/인증은 해당 모델 서버 운영자가 관리합니다.

## Linux 자동 실행 파일 설치

[`deploy/mensa-refresh.service`](../deploy/mensa-refresh.service)는 Linux의 systemd가 Python 프로그램을 실행할 방법을 적은 예제입니다. 식단 처리 순서를 호출하는 Python 클래스 RefreshService와 다릅니다. 운영자는 예제의 User=mensa, 작업 폴더 `/opt/mensa/worker-checkout`, Python `/usr/bin/python3.12`, 설정 `/etc/mensa/worker.toml`을 실제 환경에 맞춰 준비해 주세요.

이 파일의 Type=oneshot은 프로그램을 한 번 실행하고 끝낸다는 뜻입니다. 파일은 `PYTHONDONTWRITEBYTECODE=1`, UMask=0077, 작업 기록 폴더 0700, `ProtectSystem=strict`를 지정하고 `/var/lib/mensa/private`와 `/srv/www/mensa/data`만 쓰게 합니다. 여러 요리를 번역하므로 전체 시작 제한은 infinity이며 종료 제한은 30초입니다. `KillMode=control-group`은 systemd가 해당 실행 그룹을 정리하도록 지정합니다. 모델 종료 함수는 자신이 시작한 그룹에만 TERM/KILL을 보냅니다.

자동 실행 시각을 적은 [`deploy/mensa-refresh.timer`](../deploy/mensa-refresh.timer)는 부팅 2분 뒤, 작업 종료 15분 뒤에 Python 프로그램을 실행하도록 하며 accuracy는 1분입니다. queue.py가 하루 세 번의 식단 갱신 작업과 놓친 수집 시각을 판단하므로 timer에 날짜 판단을 넣지 않습니다.

운영자는 계정/권한·Python/시간대·모델 파일·GPU/메모리 측정과 실제 종료 동작을 대상 Linux에서 확인한 뒤 아래 파일을 설치해 주세요. 이 저장소의 예제 파일 자체는 실제 설치 결과가 아닙니다.

```sh
sudo cp deploy/mensa-refresh.service deploy/mensa-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mensa-refresh.timer
```

## 수집 일정과 최신 식단 교체

자동 갱신 프로그램은 `Europe/Berlin` 기준 매일 09:00·10:00·11:00에 도래한 수집 작업을 처리합니다. `period`와 `completed_period`는 `2026-10-01T10:00`처럼 수집 날짜와 예정 시각을 기록합니다. 이는 같은 시간대의 중복 실행을 막는 비공개 작업 기록이며, 웹사이트에는 최신 메뉴 한 묶음만 표시합니다. 프로그램은 날짜만 있는 기존 작업 기록도 읽습니다. 서버가 요청한 재시도 대기 시간과 이미 진행 중인 게시 작업은 다음 수집 시각이 와도 유지합니다. Linux timer와 Mac LaunchAgent가 실행 기회를 주므로 실제 시작은 예정 시각보다 늦을 수 있습니다.

원본 수집과 데이터 검사를 통과한 새 메뉴·번역은 현재 공개 식단을 대체합니다. 웹사이트는 이전 수집 결과를 별도 메뉴로 추가하지 않습니다. 원본에서 가격이나 구성품을 바꾸면 다음 수집에서 반영하고, 원문이 같은 번역은 저장된 결과를 다시 사용합니다. 공개 파일의 게시 ID별 폴더는 메뉴와 번역을 같은 묶음으로 읽기 위한 파일 보관 방식이며, 화면에서 과거 수집 내역을 나열하기 위한 기능이 아닙니다.

## 실패 확인과 재시도

운영자는 --status 출력과 실제 작업 기록 폴더의 아래 파일을 함께 확인해 주세요.

| 파일 | 프로그램이 저장하는 내용과 운영자가 확인할 것 |
| --- | --- |
| `queue.json` | 완료 수집 시각 completed_period와 대기 작업 pending입니다. pending에는 period(수집 날짜·시각), phase(수집 collect/게시 publish), attempts(시도 수), next_attempt_at, last_error와 Git commit_sha/run_id/dispatch_requested_at이 있습니다. |
| `last-result.json` | 마지막 실행의 시각과 완료/실패/연기 이유입니다. |
| `translation-checkpoint.json` | 검사를 통과해 이미 완료한 요리 번역입니다. 모든 요리가 완료된 파일이라는 뜻은 아닙니다. |
| `snapshot-journal.json` | GitHub 경로에서 프로그램이 만든 게시용 커밋을 확인하고 복구하기 위한 기록입니다. |

운영자가 파일을 수정하거나 백업하기 전에는 자동 실행과 현재 작업을 먼저 멈춰 주세요. Linux 예제에서는 `sudo systemctl stop mensa-refresh.timer mensa-refresh.service`, Mac에서는 `launchctl bootout "gui/$(id -u)/io.github.garlicvread.mensa-refresh"`를 사용합니다. 별도로 실행한 수동 작업도 멈춥니다. 파일을 수정하는 코드에서는 worker.lock에 같은 파일 잠금을 걸어 다른 실행과 queue.json이 경합하지 않게 해야 합니다.

운영자는 오류의 원인인 설정·모델 버전·주의 표시 번역 등을 먼저 고칩니다. 자동 재시도가 중지된 모델 실패는 `request_retry` 함수([`mensa/queue.py`](../mensa/queue.py))로 중지를 해제할 수 있습니다. 전용 CLI 옵션은 없습니다. 아래 코드는 지정 TOML의 작업 폴더와 정상 파일을 검사하고 잠금 안에서 queue.json을 저장합니다. 갱신 날짜·처리 단계·시도 수·GitHub 게시 번호는 유지하므로 이미 진행한 작업을 잊지 않습니다. 운영자는 자동 실행과 같은 OS 계정에서 이 코드를 실행해 주세요.

```sh
python3 - /etc/mensa/worker.toml <<'PYTHON'
from pathlib import Path
import os
import stat
import sys
from mensa.config import load_worker_config
from mensa.queue import exclusive_lock, load_state, request_retry, save_state

state_dir = load_worker_config(Path(sys.argv[1])).paths.state_dir
mode = state_dir.lstat().st_mode
if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
    raise SystemExit('Existing private state directory required')
for name in ('queue.json', 'worker.lock'):
    path = state_dir / name
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if name == 'worker.lock':
            continue
        raise
    if not stat.S_ISREG(mode):
        raise SystemExit('Queue and lock must be regular nonsymlink files')
os.umask(0o077)
with exclusive_lock(state_dir / 'worker.lock'):
    state = load_state(state_dir / 'queue.json')
    if state['pending'] is None:
        raise SystemExit('No pending work to retry')
    save_state(state_dir / 'queue.json', request_retry(state))
    print('Retry requested; period, publication IDs and attempts retained')
PYTHON
```

운영자는 status로 변경한 대기 작업을 확인한 뒤 자동 실행을 다시 등록/활성화해 주세요. queue.json·완료 번역·복구 기록이나 게시 번호를 삭제하면 프로그램이 이미 수행한 작업을 잊을 수 있으므로 초기화하지 않습니다.

Git 변경을 복구하는 SnapshotRepository(`mensa/git_repository.py`)는 워커가 두 데이터 JSON을 기록해 만든 커밋을 확인합니다. 이 커밋을 소유 커밋이라고 부릅니다. 이 클래스는 snapshot-journal.json의 부모 커밋·Git 트리·데이터 파일 내용을 대조하며 커밋 메시지만으로 소유 여부를 판단하지 않습니다. 게시 준비 중 원격 main이 앞서가면 SnapshotRepository의 plan_replacement/resume_replacement는 두 이력을 구별합니다. 원격 main 이력에 워커의 기존 커밋이 이미 들어 있으면 로컬 main을 원격 main 위치까지 앞으로 이동합니다(fast-forward). 기존 커밋이 main 이력에 남아 있으므로 이 경우에는 refs/mensa/superseded/<SHA>를 만들지 않습니다. 원격 main이 기존 커밋을 포함하지 않고 두 이력이 갈라졌다면, 클래스는 워커가 만든 커밋인지와 기록된 출발 커밋이 원격 main에 남아 있는지를 확인한 뒤 기존 커밋을 refs/mensa/superseded/<SHA>에 보존합니다. 프로그램은 최신 main에서 식단을 다시 수집하고 새 게시용 데이터 커밋을 준비합니다. 이 새 커밋을 대체 커밋이라고 부릅니다. 프로그램은 사람이 만든 변경·알 수 없는 커밋을 보존하며 운영자가 검토해야 합니다.

GitHub 게시 코드는 알려진 run_id를 먼저 조회하고 실패 실행을 같은 번호로 다시 요청합니다. 게시 요청 응답이 사라지면 같은 commit_sha의 실행을 조회합니다. 식단 날짜가 만료되었을 때 이미 성공한 실행이면 해당 갱신 날짜의 작업을 완료하고, 진행 중이면 기다리며, 완료되지 않은 만료 데이터는 새 수집으로 돌아갑니다. 운영자는 준비된 대체 커밋과 원래 게시 실행의 충돌·대체 식단의 만료·알 수 없는 변경을 보존하고 직접 검토해야 합니다.

## 로그 보존과 이전 게시물로 되돌리기

managed 모델을 시작하는 함수는 log_dir/model.log를 최신 실행으로 덮어씁니다. 코드에는 로그 크기 제한이나 과거 실행 보관 기능이 없으므로 운영자가 회전/보관과 디스크 여유 감시를 준비합니다. Mac의 worker.stdout.log/worker.stderr.log도 운영자가 별도로 관리합니다. 운영자는 이 파일을 웹서버에 공개하지 않습니다.

운영자는 프로그램 중지와 파일 잠금 확보 후 존재하는 queue.json·last-result.json·translation-checkpoint.json·snapshot-journal.json과 실제 TOML을 함께 백업합니다. 다른 사용자가 읽지 못하게 새 백업 폴더는 0700, 파일은 0600으로 준비합니다. GitHub 경로에서는 HEAD/main/origin/main과 보존한 `refs/mensa/superseded/`도 같이 남깁니다.

웹사이트를 되돌릴 때 운영자는 검증된 전체 HTML/CSS/JavaScript와 같은 ID의 메뉴/번역 두 파일을 함께 사용합니다. 별도 사이트 폴더에서 확인한 뒤 웹서버가 읽는 폴더를 바꾸고 TOML public_dir과 Linux 쓰기 허용 경로를 맞춥니다. 서로 다른 게시본의 메뉴와 번역을 섞으면 원문 연결이 맞지 않을 수 있습니다.

작업 기록을 백업으로 되돌리기 전에는 운영자가 현재 GitHub commit_sha/run_id와 백업의 갱신 날짜·게시 번호·복구 기록을 대조해야 합니다. 이미 완료한 게시 실행을 잊지 않기 위해서입니다. 운영자는 `git reset --hard`, `git clean`, force push나 queue.json 삭제로 알 수 없는 변경을 버리지 않습니다.
