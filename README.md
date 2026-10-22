# Saarbrücken Mensa

이 웹사이트는 Saarbrücken 멘자의 독일어 식단을 한국어·영어·독일어로 보여 줍니다. 이용자는 하루/주간 보기를 선택하고 학생·교직원·방문객 가격, 독일어 원문, 곁들임 음식과 주의 표시를 확인할 수 있습니다.

[공개 식단](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/) · [원본 식단](https://www.stw-saarland.de/gastro/mensa-saarbruecken/)

## 프로그램이 식단을 준비하고 보여 주는 방식

식단을 주기적으로 준비하는 Python 프로그램을 워커라고 부릅니다. 실행 명령은 [`scripts/local_refresh.py`](scripts/local_refresh.py)에 있습니다. 이 프로그램은 원본 웹페이지를 읽고 메뉴·가격을 검사한 뒤 요리 이름과 곁들임 음식을 번역합니다. 검사를 통과한 메뉴와 번역은 JSON 파일로 저장합니다.

이용자의 브라우저는 미리 준비한 HTML/CSS/JavaScript와 JSON 파일을 읽습니다. 브라우저 요청에 맞춰 식단을 생성하는 웹 백엔드나 데이터베이스 엔진은 없습니다. Python 프로그램은 수집·게시 진행 상태와 완성된 번역을 JSON 파일로 저장합니다. `--config` 실행은 검사가 끝난 중간 번역도 별도 파일에 저장하지만, Mac 기본 `--base` 실행은 새 번역을 만드는 동안 메모리에서 보관합니다. 운영자는 이 진행 기록을 웹서버에 공개하지 않습니다.

처음 프로젝트를 맡은 유지보수자는 [시작 안내](docs/getting-started.md)와 [유지보수 설명서](docs/maintenance.md)를 읽어 주세요. 웹사이트에 변경을 반영하는 운영자는 [사이트 배포 안내](docs/deployment.md)를 확인해 주세요.

코드를 수정하는 유지보수자는 [구조 문서](docs/architecture.md)에서 담당 함수와 파일을 먼저 찾고, [데이터 설명](docs/data-contract.md)에서 메뉴·번역 파일의 필드와 검사 규칙을 확인해 주세요. 서버 계정·경로·자동 실행을 준비하는 운영자는 [운영 문서](docs/operations.md)의 설정과 실행 절차를 따라 주세요.

## 현재 운영 환경

현재 Mac에서는 `~/mensa`의 코드를 사용해 식단을 갱신하고 GitHub Pages에 게시합니다. 작업 기록·모델·로그는 `~/Library/Application Support/Mensa`에 있으며, 그 안의 `checkout`은 `~/mensa`를 가리킵니다. Python·Ollama·`gemma4:31b` 모델과 로그인 사용자의 LaunchAgent는 이미 설치되어 있습니다. 이 환경을 이어받는 유지보수자는 기존 설정과 파일을 그대로 사용합니다. 경로와 상태 확인 명령은 [현재 운영 환경](docs/operations.md#현재-운영-환경)에 정리되어 있습니다.

갱신 날짜는 프로그램이 `Europe/Berlin` 기준으로 계산합니다. 기존 환경에서 시간대 데이터를 별도로 준비하거나 시스템 시간대를 다시 설정할 작업은 없습니다. 새로운 컴퓨터에 설치하거나 게시 방식을 바꿀 때의 작업은 [새 환경 설치](docs/operations.md#새-환경에-설치할-때)를 참고해 주세요.

## 로컬에서 코드와 화면 확인하기

코드 수정과 검사는 실제 워커 체크아웃과 분리된 개발 체크아웃에서 진행해 주세요. 현재 Mac의 `~/mensa`는 새 워커 프로세스가 직접 읽는 운영 코드 위치입니다. Git이 무시하는 `.tmp/`, `dist/`, `node_modules/` 산출물은 게시 검사에서 보존하며 허용합니다. 구체적인 허용 범위와 충돌 보호는 [운영 문서](docs/operations.md#개발-산출물과-체크아웃-검사)에 설명되어 있습니다.

개발 명령은 Python 3.12 이상과 Node.js를 사용합니다. 현재 Mac의 워커는 `/opt/homebrew/opt/python@3.14/bin/python3.14`, Pages 검사는 Python 3.12와 Node.js 24를 사용합니다. 브라우저용 컴파일러는 [package.json](package.json)과 잠금 파일로 설치합니다. 새 개발 체크아웃이거나 잠금 파일이 바뀐 경우에만 다음 설치 명령을 실행해 주세요. 설치된 개발 환경을 그대로 사용할 때는 아래 검사에서 시작합니다.

```sh
npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
```

README와 package.json이 있는 프로젝트 최상위 폴더에서 필요한 검사를 실행합니다. `python3`는 위 버전 조건을 충족하는 개발용 Python을 뜻합니다.

```sh
python3 -m unittest discover -s tests -v
npm test
python3 -m scripts.update_menu --validate-only
```

`npm test`는 TypeScript를 컴파일한 뒤 브라우저 코드의 테스트를 실행합니다. `npm run typecheck`는 파일을 만들지 않고 타입을 검사하며 `npm run compile`은 `dist/frontend/`에 JavaScript를 만듭니다. 마지막 Python 명령은 저장된 `site/data/menu.json`과 `translations.json`의 형식·원문 연결·번역 완전성을 검사합니다. 이 명령은 원본 웹페이지를 가져오지 않으며 식단 날짜가 오늘까지 유효한지는 별도로 검사해야 합니다.

웹서버가 제공할 폴더는 사이트 생성 프로그램 [`scripts/build_site.py`](scripts/build_site.py)가 만듭니다. 다음 예제의 `.tmp/build-example`은 아직 없는 폴더 이름을 골라 사용해 주세요. 프로그램은 기존 파일을 보호하기 위해 출력 폴더가 이미 있으면 중단합니다. 기존 폴더를 지우지 말고 `.tmp/build-example-2`처럼 다른 이름을 선택해 주세요.

```sh
npm run build -- --output .tmp/build-example
python3 -m http.server 8000 --bind 127.0.0.1 --directory .tmp/build-example
```

`npm run build`는 컴파일과 사이트 생성을 차례로 실행합니다. 사이트 생성 프로그램은 `site/`의 HTML/CSS/favicon·메뉴/번역과 `dist/frontend/`의 JavaScript를 읽고 `.tmp/build-example/`에 게시용 파일을 만듭니다. 개발용 모델·로그·작업 기록은 이 폴더에 넣지 않습니다. 브라우저로 확인할 때는 `site/` 입력 폴더가 아니라 이 명령이 만든 출력 폴더를 제공해 주세요.

사이트 생성 명령의 기본 출력 폴더는 `.tmp/pages`입니다. Python 명령 `python3 -m scripts.build_site`를 직접 사용하면 컴파일은 실행되지 않으므로 먼저 `npm run compile`을 실행해 주세요. 모든 입력/출력 옵션은 [운영 문서](docs/operations.md#명령별-옵션과-기본값)에 설명되어 있습니다.

## 실제 식단 갱신과 상태 확인하기

현재 Mac의 로그인 사용자는 기존 코드 폴더에서 설치된 워커의 상태를 확인합니다.

```sh
cd "$HOME/mensa"
/opt/homebrew/opt/python@3.14/bin/python3.14 -m scripts.local_refresh --base "$HOME/Library/Application Support/Mensa" --status
```

`--status`는 큐와 마지막 결과만 읽습니다. 수동으로 대기 작업을 처리할 필요가 있을 때는 같은 명령에서 `--status`를 빼며, 재시도 대기와 자원 조건은 그대로 적용됩니다. 모델·LaunchAgent 재설치는 일상적인 갱신 절차에 포함되지 않습니다.

다른 서버를 새로 구성할 때는 설정 파일 위치(`--config`) 또는 Mac 전용 작업 폴더(`--base`) 중 하나를 지정합니다. 설정 파일은 코드·작업 기록·공개 폴더와 모델·자원·게시 방법을 지정합니다. [상대 경로 예제](config/worker.example.toml)와 [Linux 예제](deploy/worker.example.toml)는 새 환경에 맞춰 작성할 때 사용합니다.

Linux 자동 실행 예제 [`deploy/mensa-refresh.service`](deploy/mensa-refresh.service)는 프로그램을 OS 계정 `mensa`로 실행하도록 지정합니다. 운영자가 같은 프로그램을 수동으로 실행할 때도 이 계정을 사용해 주세요. 다른 계정으로 작업 기록 파일을 만들면 `mensa`가 다음 자동 실행에서 파일을 읽거나 고치지 못할 수 있습니다. 대상 계정의 프로젝트 폴더에서 사용할 명령은 다음과 같습니다.

```sh
python3 -m scripts.local_refresh --config /etc/mensa/worker.toml --status
python3 -m scripts.local_refresh --config /etc/mensa/worker.toml
```

`--status`를 받은 프로그램은 설정의 작업 기록 폴더에서 `queue.json`의 대기 수집 시각 작업과 `last-result.json`의 마지막 실행 결과를 출력합니다. 프로그램은 파일을 수정하지 않고, 첫 실행 전에도 작업 기록 폴더를 만들지 않습니다. `--status` 없이 실행하면 대기 작업을 처리하되 매일 09:00·10:00·11:00 Berlin 시간, 실패 후 대기 시간과 메모리/CPU 기준을 지킵니다. 수동 실행도 강제 갱신은 아닙니다. 원본 페이지가 주중에 추가하는 가격과 구성을 반영하기 위해 프로그램은 하루 세 번 수집하며, 메뉴 이름과 구성품이 같으면 저장된 번역을 재사용합니다. 웹사이트는 새 메뉴·번역으로 최신 식단을 교체하며, 수집할 때마다 같은 메뉴를 추가하지 않습니다. `queue.json`에는 중복 실행을 막기 위한 완료 수집 시각과 대기 작업만 기록합니다.

현재 Mac의 LaunchAgent는 로그인 시와 900초 간격으로 워커를 호출합니다. Mac이 깨어 있고 해당 사용자의 로그인 세션이 유지되어야 실행됩니다. 화면 잠금·로그아웃·시스템 잠자기와 실제 복구 방법은 [Mac 운영 조건](docs/operations.md#mac의-로그인화면-잠금잠자기)을 확인해 주세요. 다른 Mac의 최초 설치나 손상된 설치 복구에만 [`scripts/install_local_worker.py`](scripts/install_local_worker.py)를 사용합니다.

## 원본 데이터와 공개 파일을 다룰 때의 기준

원본 수집 함수는 같은 메뉴가 반복되어도 각 발생에 다른 ID를 붙여 `menu.json`에 모두 남깁니다. 화면의 메뉴 선택 함수는 같은 날짜 안에서 ID 이외의 모든 내용이 같은 항목만 한 번 보여 줍니다. 일간·주간 화면 모두 이 규칙을 사용합니다.

원본이 유효하면 메뉴 추가·삭제·교체, 중복 항목 수 변화와 빈 식단을 정상적으로 갱신합니다. 이전 식단과 메뉴 개수를 비교해 차단하지 않습니다. 계속 존재하는 동일 메뉴의 가격이 모두 사라진 경우와 원문·가격 연결 오류는 계속 검출합니다.

가격 검사 함수는 같은 날짜·분류·독일어 이름의 원본 S/M/G 금액과 학생/교직원/방문객의 정수 센트 가격을 대조합니다. 번역 모델은 가격이나 원문에 없는 알레르기 표시를 만들지 않습니다. 주의 표시는 `data/notice-translations.json`의 번역을 우선 사용하고, 사전에 없는 문구는 모델로 번역해 캐시에 저장합니다. 새 식단은 모든 주의 표시의 번역이 완성된 뒤 게시합니다. 번역한 주의 표시에도 독일어 원문을 함께 표시합니다.

미등록 주의 표시만 새로 생겨도 워커는 번역 모델을 실행합니다. 호출 오류는 갱신 성공으로 처리하지 않고 기존 작업 큐에 전달합니다. 일시적인 통신 오류는 큐의 재시도 정책으로 다시 실행합니다. 이전 코드의 `Unknown notice labels`로 대기 중인 작업은 수정본 반영 후 기존 기록을 보존해 재개합니다. [오류 복구](docs/operations.md#실패-확인과-재시도)를 참고해 주세요.

완성된 웹사이트에는 같은 식단의 `menu.json`과 `translations.json`을 함께 넣습니다. `data/current.json`은 이용자에게 보여 줄 두 파일의 폴더를 지정합니다. 실패 후 이미 완료한 번역이나 GitHub 게시 번호를 잊지 않도록 운영자는 작업 기록을 삭제하지 말고 [복구 절차](docs/operations.md#실패-확인과-재시도)를 따라 주세요.

GitHub의 [`update-and-deploy.yml`](.github/workflows/update-and-deploy.yml)은 운영자가 지정한 main 커밋을 검사하고 사이트를 만들어 Pages에 게시합니다. 이 파일에는 push/주간 자동 실행 일정이 없습니다. Python 프로그램이 하루 세 번의 식단 갱신 작업을 판단하며 Linux timer나 Mac LaunchAgent는 그 프로그램을 실행할 기회만 제공합니다. 저장소 라이선스는 아직 결정되지 않았습니다.
