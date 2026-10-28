# 식단 갱신 프로그램의 설정과 운영

워커는 원본 식단을 수집·번역·검사하고 Drive `current.json`을 갱신합니다. 실행 진입점은 [`scripts/local_refresh.py`](../scripts/local_refresh.py)입니다. 웹 백엔드나 데이터베이스 엔진은 없으며 큐·완료 번역·준비한 게시본·마지막 결과를 파일로 보관합니다. 화면 HTML/CSS/JavaScript는 별도의 Pages 코드 배포가 담당합니다.

## 현재 운영 환경

기존 Mac 설치의 경로와 Drive 전환 후 사용할 설정은 다음과 같습니다. 전환 준비와 실제 활성화는 구별합니다. 공개 데이터 읽기·Pages 배포·워커 재개·Git 무변경 확인까지 끝나야 [전환 완료](deployment.md#기존-mac을-drive-방식으로-전환하기)로 기록합니다.

| 대상 | 경로 또는 설정 |
| --- | --- |
| 운영 코드 | `~/mensa`, 검토한 main 코드 |
| 비공개 상태 BASE | `~/Library/Application Support/Mensa` |
| 코드 연결 | `BASE/checkout`이 운영 코드 위치를 가리킵니다. |
| 설정 | `BASE/worker.toml`; `--base`도 이 파일을 읽습니다. |
| 로컬 데이터 준비 | 예: `~/Library/Application Support/Mensa Data`; 코드·BASE와 겹치지 않습니다. |
| Python | `/opt/homebrew/opt/python@3.14/bin/python3.14` |
| 네이티브 모델 | `BASE/runtime/ollama`, `BASE/models`, `gemma4:31b`; 로그는 `BASE/logs` |
| 실행 자원 | 기존 Mac 정책의 가용 메모리 32 GiB, CPU별 부하 계수 0.6과 전원 조건 |
| 자동 실행 | 로그인 사용자의 `io.github.garlicvread.mensa-refresh`; 로그인 시·900초 간격 |
| 데이터 목적지 | 설정한 rclone remote·폴더 ID·안정된 `current.json` 파일 ID |
| 현재 폴더 이름 | `saarbruecken_mensa_data_storage`; 이름 변경은 폴더 ID를 바꾸지 않습니다. |
| 화면 | 기존 GitHub Pages 주소, 저장소 변수 `MENSA_DRIVE_FILE_ID` |
| 수집 시각 | `Europe/Berlin` 기준 매일 09:00·10:00·11:00 |

현재 설치의 모델·큐·로그·LaunchAgent를 다시 설치하지 않습니다. 새 코드에서 BASE 설정이 없으면 자동 Git 게시로 되돌아가지 않습니다. 베를린 날짜 계산도 기존 Python의 시간대 정보를 사용하므로 시스템 시간대를 매번 바꾸지 않습니다.

```sh
cd "$HOME/mensa"
/opt/homebrew/opt/python@3.14/bin/python3.14 -m scripts.local_refresh --base "$HOME/Library/Application Support/Mensa" --status
```

`--status`는 큐와 마지막 결과를 읽고 수집·모델·게시를 실행하지 않습니다. 이를 뺀 명령은 대기 작업을 처리하되 일정·실패 후 대기·자원 기준을 따릅니다. 수동 실행도 강제 갱신은 아닙니다. `idle`은 할 작업 없음, `waiting`은 재시도 시각 전, `deferred`는 자원 조건 미충족, `busy`는 다른 실행이 잠금 보유, `completed`는 완료입니다. `failed`·`blocked`는 실패·자동 재시도 중지이며 CLI가 exit 1로 끝납니다. 종료 코드 0만으로 새 데이터 게시를 판단하지 마세요.

## Drive 연결과 후임자 인계

운영 계정에 rclone을 준비하고 `rclone config`로 자신의 Google Drive 연결을 만듭니다. 현재 설치의 쓰기 연결 이름은 `mensa-drive:`입니다. `publication.remote`는 rclone remote 이름이며 Git remote나 다운로드 URL이 아닙니다. 읽기 전용 토큰으로는 게시할 수 없습니다. 이 설치는 `drive.readonly,drive.file` 범위로 기존 파일을 읽고 rclone에서 사용하는 파일을 작성하도록 인증했습니다. 후임자는 자신의 계정으로 인증하고 remote·폴더 ID·파일 ID를 설정합니다. 다른 운영자의 OAuth token을 문서나 소스에 복사하지 않습니다.

현재 연결의 rclone 공용 `client_id`에는 2026년 중 사용 종료 공지가 있습니다. 현재 인증 성공이 계속된 사용을 보장하지는 않습니다. 후임자는 [rclone 공식 자체 client ID 안내](https://rclone.org/drive/#making-your-own-client-id)에 따라 Google 프로젝트·Drive API·OAuth 동의·데스크톱 client를 준비하고 rclone의 비공개 설정에 client ID/client secret을 넣어 재인증해 주세요. 이 절차에 결제 활성화나 유료 서비스 추가를 포함하지 않습니다. 인증 값과 token은 rclone 설정에서만 관리하고 워커 TOML·Git·로그·사이트에 넣지 않습니다.

이번 설치는 사용자가 제공한 폴더의 기존 공유 설정을 그대로 사용합니다. 후임자는 자신의 공유·공개 읽기 설정을 준비한 뒤 아래 주소가 로그인 없이 JSON을 반환하는지 확인합니다. 프로그램과 전환 절차는 폴더·파일의 공유 설정을 변경하지 않습니다.

```text
https://drive.usercontent.google.com/download?id=FILE_ID&export=download&authuser=0&confirm=t
```

`FILE_ID`는 `current.json`의 파일 ID이며 폴더 ID와 다릅니다. 브라우저 Network 또는 익명 HTTP 요청에서 성공 상태·실제 JSON 본문·`Access-Control-Allow-Origin: *`를 확인합니다. Drive 로그인 화면이나 HTML 다운로드 확인 페이지는 성공한 데이터 응답이 아닙니다. 일반 보기 링크나 API key가 필요한 Drive API 주소를 앱 설정으로 사용하지 않습니다.

폴더 안의 예약 이름은 `current.json` 한 개입니다. 워커는 기존 파일의 ID와 내용을 확인하고 같은 파일을 갱신합니다. 이름이 중복되거나 ID가 달라지거나 기존 파일이 유효한 공개 묶음이 아니면 중단하므로 임의의 파일을 같은 이름으로 바꾸지 않습니다. 폴더의 다른 파일을 삭제·동기화하지 않습니다. 인계 기록에는 코드 버전, BASE·로컬 준비 경로, remote 이름, 폴더/파일 ID, 모델/revision, 마지막 검증·완료 시각을 남기며 인증 비밀은 제외합니다.

Drive는 기존 계정의 저장 공간과 다운로드 한도를 사용합니다. 추가 운영비를 만들지 않으며 Cloudflare·새 유료 호스팅·용량 구매·결제 계정 연결을 도입하지 않습니다. 한도·인증·통신 오류가 나면 기존 공개 묶음을 보존하고 원인을 해결합니다.

## 최초 데이터 이관과 설정 준비

[`scripts/configure_drive.py`](../scripts/configure_drive.py)는 기존 메뉴·번역 또는 이미 있는 Drive 묶음을 검증하여 로컬 준비 데이터와 BASE 설정을 준비합니다. 검증된 새 코드를 사용할 폴더에서 같은 운영 계정으로 실행합니다. 원래 큐가 대기 작업 없이 idle 상태여야 하며 명령이 기존 `worker.lock`을 확보합니다. 먼저 자동 실행과 수동 작업을 멈추고 [배포 순서](deployment.md#기존-mac을-drive-방식으로-전환하기)에 따라 백업해 주세요.

```sh
python3 -m scripts.configure_drive \
  --base "$HOME/Library/Application Support/Mensa" \
  --checkout "$HOME/mensa" \
  --data-dir "$HOME/Library/Application Support/Mensa Data" \
  --remote gdrive: \
  --folder-id FOLDER_ID \
  --source-data OLD_PAIR_DIR
```

`FOLDER_ID`는 목적지 폴더 ID로, `OLD_PAIR_DIR`는 `menu.json`·`translations.json`을 보존한 폴더로 바꿉니다. 구버전 체크아웃에 있던 `site/data`를 사용하려면 그 두 파일이 제거되는 코드 반영 전에 별도로 보존합니다. 이미 유효한 Drive `current.json`에서 인계받으면 `--source-data`를 생략합니다. 원격 파일이 없는 신규 목적지에는 명시한 검증 쌍이 필요합니다.

명령은 기존 큐·모델·로그를 비우지 않고 `BASE/worker.toml`을 준비하며 `config_path`, `file_id`, `release_id`, `data_dir`를 반환합니다. 기존 번역을 워커용 재사용 캐시에 이관하고 30일 메뉴·참조 번역만 공개합니다. 기본 모델은 `gemma4:31b`이며 revision은 설치된 Ollama manifest의 SHA256 digest를 읽습니다. 다른 위치의 모델은 `--model-revision`에 `/api/tags`의 정확한 digest를 지정합니다. 모델을 실행하거나 내려받지는 않습니다. managed 실행은 실제 보고된 digest와 설정을 비교합니다. 기존 준비 데이터·설정과 충돌하면 중단합니다. 원격 묶음이 입력 또는 보관 정책 적용 결과와 다르면 덮어쓰지 않습니다. 실패하면 입력과 실제 원격 결과를 보존하여 같은 대상에 재개합니다.

설정 준비 후 익명 공개 읽기를 확인하고 저장소 변수 `MENSA_DRIVE_FILE_ID`를 반환된 파일 ID로 한 번 설정합니다. 검토한 운영 코드·Pages를 반영한 뒤 워커를 재개합니다. 설정 파일을 만들었다는 사실만으로 활성화 완료를 기록하지 않습니다. 상세 순서는 [배포 안내](deployment.md)에 있습니다.

전환 명령은 운영용 Mac의 소유한 `logs` 폴더를0700, 기존 `model.log`를0600으로 맞춰 모델 실행 검사를 준비합니다. 로그 내용과 inode·다른 파일은 보존하고 symlink·FIFO·hardlink·다른 소유자의 경로는 원격 작업 전에 거절합니다. 기존 설정에 같은 명령을 재실행해도 이 권한 준비를 수행합니다. Google Drive 폴더의 공유 권한을 바꾸는 작업이 아닙니다.

## 설정 파일의 경로·모델·게시 값

[`config/worker.drive.example.toml`](../config/worker.drive.example.toml)은 상대 경로의 Drive 예제입니다. `load_worker_config`는 `[paths]`, `[inference]`, `[resources]`, `[publication]`과 선택적인 `[retention]`을 받습니다. 보관 설정 생략 시 아래 기본값을 적용하며 알 수 없는 키·0 이하 한도·잘못된 형식을 거절합니다. 상대 경로는 TOML 위치 기준이며 symlink의 실제 경로도 확인합니다. `~`·환경변수는 확장하지 않습니다.

| 키 | Drive 운영에서의 의미 |
| --- | --- |
| `paths.checkout_dir` | 코드와 검토 번역 사전을 읽는 체크아웃입니다. 운영 JSON을 쓰지 않습니다. |
| `paths.state_dir` | 0700 비공개 큐·결과·중간 번역·준비 저널 폴더입니다. |
| `paths.public_dir` | 로컬 메뉴·번역 릴리스 준비 폴더입니다. 웹사이트 루트가 아닙니다. |
| `inference.mode` | `managed`는 준비된 Ollama를 시작/종료하고 `external`은 실행 중인 모델을 사용합니다. |
| `inference.provider` | managed는 `ollama`, external은 `ollama` 또는 `openai`입니다. |
| `inference.model`, `revision` | 실제 모델 이름과 버전입니다. managed Ollama는 실제 보고되는 digest와 revision이 일치해야 합니다. |
| `resources.platform` | 실제 호스트의 `macos` 또는 `linux`입니다. |
| `resources.min_available_bytes` | 최소 가용 메모리의 양의 정수 바이트입니다. |
| `resources.max_load_per_cpu` | 가용 CPU 용량에 곱할 양의 유한 부하 계수입니다. |
| `publication.mode` | Drive 설치는 `drive`입니다. |
| `publication.remote` | 인증된 rclone remote 이름이며 `:`로 끝납니다. |
| `publication.folder_id`, `file_id` | 목적지 폴더와 `current.json`의 ID입니다. 영문자·숫자·밑줄·하이픈 10~200자입니다. |

checkout/state/public 세 경로는 같거나 서로 포함할 수 없습니다. managed의 `binary`, `models_dir`, `log_dir`도 명시해야 하며 모델/로그는 checkout·데이터 준비 폴더와 겹칠 수 없습니다. binary는 데이터 준비 폴더 안에 둘 수 없습니다. 경로는 아직 없어도 선언할 수 있지만 기존 경로의 파일/폴더 종류가 다르면 거절합니다. 설정 읽기는 모델·Drive 인증·호스트 적합성을 확인하거나 워커를 활성화하지 않습니다.

external에는 `url`만 추가하고 managed 전용 키를 섞지 않습니다. Ollama의 실제 요청은 loopback `/api/chat`을 사용하며 query를 허용하지 않습니다. OpenAI-compatible은 실제 요청 endpoint 전체를 지정합니다. 설정 기반 CLI에는 API key TOML 필드가 없으며 현재 빈 key를 전달하므로 인증이 필요한 endpoint는 별도의 검토된 연결이 필요합니다.

모델 번역 재사용은 provider/model/revision과 번역 정책·주의 표시 대응표의 지문으로 판단합니다. 일시적인 URL·포트·API key는 설정 기반 재사용 정체성에 넣지 않습니다. revision은 운영자 선언이며 가중치 검증 결과는 아닙니다. 기존 Mac `--base`는 생성된 기본 설정의 코드·모델·로그 경로, model/revision, 자원 값이 모두 일치할 때만 네이티브 번역·캐시와 가용 메모리 32 GiB, 부하 `max(2, 0.6 × logical CPUs)`, 전원 정책을 재사용합니다. 설정을 바꿨으면 선언한 모델·자원 정책을 적용합니다. 다른 서버의 새 설치는 명시적인 `--config` 실행을 사용해 주세요.

## 명령별 옵션과 기본값

프로젝트 최상위에서 실행하며 실제 지원 옵션은 각 명령의 `--help`로 확인합니다.

| 명령 | 주요 옵션과 효과 |
| --- | --- |
| `python3 -m scripts.local_refresh` | `--base BASE` 또는 `--config PATH` 중 하나; `--status`는 읽기 전용 조회입니다. |
| `python3 -m scripts.configure_drive` | `--base`, `--checkout`, `--data-dir`, `--remote`, `--folder-id` 필수; `--source-data`, `--model-revision` 선택. 초기 데이터와 보관 한도·설정을 준비합니다. |
| `python3 -m scripts.retention` | `--config PATH`로 정리 후보를 읽기 전용 JSON으로 출력합니다. `--apply`는 같은 큐 잠금에서 현재 원격·준비 참조를 확인하고 적용합니다. |
| `python3 -m scripts.update_menu` | `--data-dir DIR`로 메뉴/번역 폴더를 선택합니다. `--validate-only`는 저장된 쌍만 검사합니다. 이를 빼면 수집·번역 후 그 로컬 쌍을 갱신하며 Drive 게시를 대신하지 않습니다. |
| `python3 -m scripts.build_site` | `--drive-file-id ID`는 UI만 출력하고 `--data-dir DIR`는 명시한 로컬 쌍을 포함합니다. 두 옵션은 상호 배타적입니다. |
| `python3 -m scripts.install_local_worker` | 새 Mac 또는 손상된 자동 실행을 준비할 때만 사용합니다. `--write-only`는 plist를 쓰되 등록·중지는 하지 않습니다. |

빌더의 `--source` 기본은 `site`, `--output`은 `.tmp/pages`, `--compiled`는 `dist/frontend`, 로컬 모드의 `--glossary`는 `data/notice-translations.json`이며 코드 루트 기준입니다. 출력은 새 폴더여야 합니다. Python 빌더는 TypeScript를 컴파일하지 않으므로 먼저 `npm run compile`을 실행하거나 `npm run build -- --drive-file-id FILE_ID --output NEW_DIR`를 사용합니다.

예제 검사는 `python3 -m scripts.update_menu --validate-only --data-dir tests/fixtures/menu-data`입니다. 이 명령은 최신 날짜 검사가 아닙니다. 실제 게시 경로는 Berlin 오늘까지의 coverage와 전체 공개 조건을 검사합니다. 기존 호출 호환을 위한 기본 `site/data` 경로는 남아 있지만 운영 JSON을 소스에서 추적하거나 Drive의 대체 입력으로 사용하지 않습니다.

## 새 환경에 설치할 때

Python 3.12 이상, rclone, 선택한 모델의 실행 파일·가중치와 실행 계정을 준비합니다. Drive 인증은 실제 워커 사용자로 설정합니다. 코드는 읽는 경로, 비공개 상태, 로컬 데이터 준비를 분리하고 Drive 예제의 모델·revision·자원·remote·ID를 실제 값으로 채웁니다. 모델을 자동으로 다운로드하거나 파일럿 예제를 실행 가능 증거로 취급하지 않습니다.

Linux에서는 같은 서비스 계정으로 `python3 -m scripts.local_refresh --config /etc/mensa/worker.toml --status`와 실제 실행을 사용합니다. Drive 예제를 복사하여 실제 모델·revision·Linux 자원을 명시합니다. `configure_drive`가 생성하는 설정은 Mac 기본값이므로 다른 호스트에서 사용하려면 활성화 전에 해당 설정을 조정합니다. 기존 [`deploy/mensa-refresh.service`](../deploy/mensa-refresh.service)·[timer](../deploy/mensa-refresh.timer)는 directory 방식 예제이므로 Drive 설치에는 실제 체크아웃·설정·상태·준비 폴더·rclone 인증 경로를 반영합니다. 쓰기 허용 경로도 로컬 준비 폴더와 상태·모델 경로에 맞춥니다. `User=mensa`, restrictive umask와 프로세스 그룹 종료 조건을 실제 호스트에서 확인해 주세요. Linux 자원 검사는 보이는 proc/cgroup-v2·CPU affinity를 반영하지만 다른 GPU 작업이나 숨은 상위 제한까지 증명하지 않습니다.

새 Mac은 BASE의 `checkout` 연결, `runtime/ollama`, `models`와 비공개 상태를 준비하고 데이터/worker.toml을 먼저 검증합니다. 이후 같은 사용자로 `python3 -m scripts.install_local_worker --base BASE`를 실행하여 자동 실행을 한 번 등록합니다. 기존 Mac의 Drive 전환에는 이 등록을 반복하지 않습니다.

### Mac의 로그인·화면 잠금·잠자기

화면 잠금은 로그인 세션을 유지하므로 Mac이 깨어 있고 자원·전원 조건을 통과하면 실행할 수 있습니다. 로그아웃·시스템 잠자기·전원 꺼짐 동안에는 실행하지 않습니다. 깨어난 후 다음 기회에 큐가 최신 미수집 시각을 판단하며 이미 준비한 게시와 재시도 대기는 유지합니다. LaunchAgent는 강제 깨우기를 하지 않습니다.

현재 Mac의 전원 연결 시 자동 잠자기 설정은 그대로 사용합니다. 실제 변경이나 새 설치 때만 `pmset -g custom`과 [Apple 잠자기 안내](https://support.apple.com/guide/mac-help/set-sleep-and-wake-settings-mchle41a6ccd/mac)를 확인합니다. `sleep 0`도 수동 잠자기·덮개 닫기·전원 꺼짐을 막지는 않습니다. FileVault 재부팅 후 첫 잠금 해제는 사용자가 수행합니다. 워커는 로그인 암호나 디스크 암호화를 바꾸지 않습니다.

### 개발 산출물과 체크아웃 검사

Drive 워커는 소스 체크아웃을 읽기만 합니다. 개발·검사는 운영 체크아웃과 분리하고, 검토 코드의 fast-forward는 자동 실행을 멈춘 뒤 같은 `worker.lock` 아래에서 진행합니다. 기존 미커밋/미추적 파일·큐·모델은 보존합니다. Git이 무시하는 개발 결과와 운영 데이터는 서로 다른 수명으로 관리하며, 코드 정리 때문에 로컬 데이터 준비 폴더를 삭제하지 않습니다.

## 실패 확인과 재시도

먼저 `--status`에서 `pending.period`, `phase`, `attempts`, `next_attempt_at`, `last_error`와 마지막 결과를 읽습니다. `completed_period`, 공개 `menu.source.fetched_at`, Drive의 릴리스 ID를 대조하여 실제 완료를 판단합니다. 웹사이트의 오래된 자료 안내는 워커·인증·잠자기 원인을 판별하는 감시 장치는 아닙니다.

| 보존할 대상 | 의미 |
| --- | --- |
| `queue.json`, `last-result.json` | 완료/대기 수집 시각·시도·대기·오류와 최근 결과입니다. |
| `translation-state.json` | 검증한 중간/재사용 번역과 마지막 사용일·보호 여부를 함께 저장합니다. 공개 파일이 아닙니다. |
| `translation-checkpoint.json` | 전환 전 중간 번역입니다. 새 번역 상태가 생기기 전 한 번만 읽고 이후 재병합하지 않습니다. |
| `retention-releases.json` | 로컬 준비본과 원격 성공본을 구별하는 소유권·성공 기록입니다. |
| `drive-publication.json` | 준비한 수집 시각·릴리스 ID·목적지와 이전 공개 릴리스 ID입니다. 업로드 결과가 불명확해도 같은 준비본으로 재개하는 입력입니다. |
| `worker.toml` | 경로·모델/revision·자원·Drive 목적지입니다. OAuth 비밀을 넣지 않습니다. |
| 로컬 준비 폴더의 `data/current.json`, `data/releases/` | 완성된 로컬 묶음입니다. 현재 포인터가 가리키는 준비본이 원격에서 성공 확인된 게시본과 항상 같지는 않습니다. |

업로드가 실패하거나 응답을 잃어도 준비한 로컬 묶음과 저널을 삭제하지 않습니다. 다음 실행은 원격을 검사하여 이미 같은 내용이면 업로드를 생략하고 완료를 이어갑니다. 같은 수집 시각의 목적지가 바뀌면 중단하므로 장애 복구 중 파일 ID를 임의로 새로 만들지 않습니다. 원격 중복 파일·ID 불일치·손상된 envelope는 내용을 보존하여 조사합니다.

새 수집은 Drive의 현재 공개 묶음을 기준으로 삼아 공개본의 교정과 이력을 유지합니다. 재시도 중 원격이 원래 기준이나 준비본과 다른 유효한 묶음으로 바뀌었으면 그 공개본에서 다시 준비합니다. 수집 도중 다시 바뀌면 덮어쓰지 않고 다음 재시도로 넘깁니다. 로컬 준비 포인터만 보고 공개본의 교정을 되돌리지 않습니다.

파일 수정·백업·코드 반영 전에는 자동 실행과 현재 작업을 멈춥니다. Mac은 아래 `bootout`, Linux는 `sudo systemctl stop mensa-refresh.timer mensa-refresh.service`를 사용하며 별도 수동 작업도 중지합니다. 수정 코드에서도 같은 `worker.lock`의 OS 잠금을 확보합니다. BASE·준비 폴더·설정·저널을 일관된 비공개 백업으로 보존하고 코드 이력을 다시 쓰지 않습니다.

```sh
launchctl bootout "gui/$(id -u)/io.github.garlicvread.mensa-refresh"
```

원인인 인증·통신·자원·모델·원문·번역 문제를 먼저 고칩니다. 미등록 주의 표시는 검토 사전·캐시 또는 모델로 번역하며 원문을 삭제해 오류를 숨기지 않습니다. 자동 재시도가 중지된 모델 실패만 `mensa.queue.request_retry`로 해제할 수 있습니다. 전용 CLI 옵션은 없습니다. 다음 예는 자동 실행과 같은 사용자로, 중지 후 실제 `state_dir`를 인자로 받아 기존 기록을 보존합니다.

```sh
cd "$HOME/mensa"
python3 - "$HOME/Library/Application Support/Mensa" <<'PYTHON'
from pathlib import Path
import os
import stat
import sys
from mensa.queue import exclusive_lock, load_state, request_retry, save_state

state_dir = Path(sys.argv[1]).expanduser().absolute()
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

상태에서 수집 시각·단계·시도와 준비 저널이 보존됐는지 확인한 뒤 기존 자동 실행을 재개합니다. Mac은 같은 사용자로 다음 명령을 사용하고 Linux는 기존 timer를 재개합니다.

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.github.garlicvread.mensa-refresh.plist"
launchctl kickstart "gui/$(id -u)/io.github.garlicvread.mensa-refresh"
```

기존 Git 게시의 `snapshot-journal.json`·commit/run 번호·복구 참조는 전환 전 작업 확인과 역사 보존 대상입니다. 이를 Drive 복구 절차에 섞거나 삭제하여 전환을 강행하지 않습니다. 현재 `directory`·`github` 예제는 명시적인 기존 설치 호환용이며 Drive의 기본 설정으로 복사하지 않습니다.

## 수집 일정·날짜 보존과 운영 한계

큐는 아직 수집하지 않은 지난 수집 시각만 최신 시각으로 합치고 이미 준비한 게시·서버 요청 대기는 보존합니다. 일반 실패의 대기는 15 → 30 → 60 → 120 → 240 → 360분이며 이후 360분 상한입니다. 모델의 network/timeout/HTTP 408·429·5xx 오류는 재시도하고 유효한 Retry-After와 기존 대기 중 큰 값을 사용합니다. 자동 재시도 불가 모델 실패는 날짜가 바뀌어도 운영자 해제 전까지 유지합니다.

새 원본의 날짜는 최신 검증 값으로 교체하고 지난 날짜에는 아래 보관 한도를 적용합니다. 정상적인 메뉴 개수·중복 수 변화와 빈 식단은 허용하지만 원문 구조·날짜 연결·가격 출처·계속 존재하는 메뉴의 실제 가격 소실은 계속 검사합니다. 새 원본 자체를 먼저 검사하므로 보관 기간 적용으로 수집 누락을 숨기지 않습니다.

번역은 같은 원문·구성품 key에서 재사용하거나 교체하며 공개용 데이터와 워커용 재사용 캐시의 수명을 분리합니다. 과거 메뉴를 단순 순회·복사한 것만으로 미사용 항목의 마지막 사용일을 갱신하지 않습니다. 현재 메뉴·진행 작업·보호된 교정에 필요한 번역은 임의로 제거하지 않습니다.

같은 준비 폴더와 Drive 목적지에 한 워커만 연결합니다. 업로드 후 읽기 확인은 동시 원격 작성·전원 손실을 원자적으로 되돌린다는 보장이 아닙니다. 원격 오류가 나면 관찰한 결과와 보존한 준비본을 대조해 해당 단계만 복구해 주세요.

## 데이터 보관 한도와 정리

| `[retention]` 키 | 기본값·동작 |
| --- | --- |
| `history_days` | 30. Berlin 기준 `today - 30 days` 이후의 메뉴와 원본의 미래 날짜를 공개합니다. |
| `cache_unused_days` | 180. 미사용 비참조 모델 번역·주의 표시를 정리합니다. |
| `cache_max_entries`, `cache_max_bytes` | 5,000항목·16MiB. 식단/주의 표시와 사용 기록을 함께 제한하고 오래 사용하지 않은 항목부터 정리합니다. |
| `keep_releases` | 현재본 포함 최근 성공10개. 현재 포인터·원격 현재본·준비 저널의 참조를 추가 보호합니다. |
| `log_days`, `log_max_bytes` | 14일·총10MiB. 알려진 로그만 처리하며 열린 stdout/stderr 파일의 inode를 유지합니다. |
| `backup_days` | 전환 검증 후14일. 등록한 전환 백업 중 최근1개를 보호하고 그 외에는 이 기간 뒤 정리합니다. |
| `max_bundle_bytes` | 공개 envelope 전체8MiB. 필요한 원본·번역을 잘라 크기를 맞추지 않습니다. |
| `max_storage_bytes` | 생성 자료256MiB. 모델·소스·Git을 제외하고 캐시·릴리스·로그·등록 백업을 포함합니다. |

현재 메뉴·진행 작업에 필요한 항목과 `editorial-draft`·`reviewed-draft`는 캐시 퇴출에서 보호합니다. 명시적인 사용 기록의 `pinned`도 보호하며 model 출처로 남은 수동 교정은 자동 식별하지 못합니다. 보호 항목만으로 한도를 넘으면 삭제 대신 중단합니다. 사용 기록은 `translation-state.json`에서 캐시와 함께 원자적으로 바뀌고 제거한 항목의 기록도 정리합니다.

다음 첫 명령으로 정리 후보와 보호 사유·전후 바이트를 확인한 뒤 두 번째 명령으로 적용합니다. 적용은 idle 큐와 같은 `worker.lock`을 요구하며 실제 원격·로컬 포인터와 준비 저널의 릴리스를 보호합니다.

```sh
python3 -m scripts.retention --config "$HOME/Library/Application Support/Mensa/worker.toml"
python3 -m scripts.retention --config "$HOME/Library/Application Support/Mensa/worker.toml" --apply
```

원격 업로드·읽기 확인 후 성공 기록을 남기고 오래된 등록 릴리스를 정리합니다. 로컬 포인터만으로 성공을 판단하지 않습니다. 로그는 유휴·실패 상태에도 잠금 안에서 한도를 적용합니다. 미등록 파일·알 수 없는 symlink·인증·큐·모델·다른 Drive 파일은 삭제하지 않습니다. 전환 백업은 명시적으로 소유권을 기록하고 검증 완료일을 남긴 경로만 자동 관리합니다.

Drive 내부 이전 버전은 현재 파일 크기와 별개입니다. 영구 고정을 사용하지 않으며 [Google의 blob 버전 정리 정책](https://developers.google.com/workspace/drive/api/guides/manage-revisions)에 맡깁니다. 일반적인30일 보관/100개 버전 기준은 Google의 정책이며 이 프로그램이 보장하는 Drive 전체 바이트 상한은 아닙니다. 폴더 전체 purge/cleanup으로 정리하지 않습니다.
