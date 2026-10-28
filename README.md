# Saarbrücken Mensa

Saarbrücken 멘자의 독일어 식단을 한국어·영어·독일어로 보여 드립니다. 하루/주간 보기에서 학생·교직원·방문객 가격, 독일어 원문, 곁들임 음식과 주의 표시를 확인하실 수 있습니다.

[공개 식단](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/) · [원본 식단](https://www.stw-saarland.de/gastro/mensa-saarbruecken/)

## 식단과 화면을 게시하는 방식

운영용 Mac의 Python 워커가 원본 식단을 수집하고 가격·원문·번역을 검사한 뒤 Google Drive의 `current.json` 한 개를 갱신합니다. GitHub Actions 실행기가 이 묶음을 내려받아 검증하고 메뉴·번역 JSON을 화면 코드와 함께 GitHub Pages에 배포합니다. 웹브라우저는 같은 Pages 주소의 화면과 JSON을 읽습니다. 웹 백엔드나 데이터베이스 엔진은 없습니다.

일상적인 식단 갱신은 데이터 커밋·PR을 만들지 않습니다. Actions는 30분 간격으로 Drive 데이터를 확인하고 공개 Pages의 게시본 ID가 달라졌을 때 배포합니다. main 코드 변경이나 수동 요청도 배포를 실행합니다. 예약 작업은 지연되거나 비활성화될 수 있어 정확한 반영 시각을 보장하지 않습니다. 운영 JSON은 Git 추적 대상이 아니라 배포물에만 포함하며, 테스트에는 `tests/fixtures/menu-data`의 고정 예제를 사용합니다.

Drive 게시에는 기존 계정의 저장 공간과 다운로드 한도가 적용됩니다. 새 유료 서비스·Cloudflare·결제 설정을 도입하지 않습니다. 저장 공간이나 다운로드 한도 때문에 실패하면 원인을 확인하며, 프로그램이 용량을 구매하지 않습니다.

이 문서는 Drive 방식으로 운영하는 절차를 설명합니다. 코드·설정 준비만으로 실제 전환이 완료되지는 않습니다. 현재 설치의 전환 여부는 [활성화 절차](docs/deployment.md#기존-mac을-drive-방식으로-전환하기)의 Drive 공개 읽기, Pages 배포, 워커 재개 확인으로 판단해 주세요.

## 문서 읽기

- 처음 맡으시면 [시작 안내](docs/getting-started.md)와 [유지보수 설명서](docs/maintenance.md)를 읽어 주세요.
- 설치·실행 계정·Drive 연결·오류 복구·후임자 인계는 [운영 문서](docs/operations.md)에 있습니다.
- 코드와 실제 설치에 수정본을 반영하는 순서는 [배포 안내](docs/deployment.md)를 사용해 주세요.
- 함수와 파일 위치는 [구조 문서](docs/architecture.md), JSON 필드·가격·해시 규칙은 [데이터 설명](docs/data-contract.md)에 있습니다.

## 로컬에서 검사하고 화면 확인하기

운영 워커의 `~/mensa`와 분리된 개발 체크아웃에서 작업해 주세요. Python 3.12 이상과 Node.js 24를 사용합니다. 새 체크아웃이거나 잠금 파일이 바뀌었을 때 컴파일러를 설치합니다.

```sh
npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
python3 -m unittest discover -s tests -v
npm test
python3 -m scripts.update_menu --validate-only --data-dir tests/fixtures/menu-data
```

마지막 명령은 저장된 예제의 형식·가격·번역 연결을 검사합니다. 원본 수집이나 모델 실행을 하지 않으며 오늘 날짜까지의 식단을 보증하지 않습니다. 고정된 과거 테스트 자료에 현재 날짜 검사를 적용하지 않습니다.

아직 없는 출력 폴더를 선택하여 예제로 화면을 만듭니다. 이미 있는 출력 폴더는 빌더가 덮어쓰지 않으므로 다른 이름을 사용해 주세요.

```sh
npm run build -- --data-dir tests/fixtures/menu-data --output .tmp/build-example
python3 -m http.server 8000 --bind 127.0.0.1 --directory .tmp/build-example
```

실제 Drive를 읽는 화면은 공개 `current.json`의 파일 ID를 지정합니다.

```sh
npm run build -- --drive-file-id FILE_ID --output .tmp/build-drive
```

`FILE_ID`를 실제 설치의 값으로 바꿔 주세요. 이 모드는 빌드를 실행하는 컴퓨터가 공개 Drive JSON을 내려받아 게시 계약·해시·검토 사전을 검사하고 메뉴·번역을 결과물에 포함합니다. 브라우저용 API 키나 Drive 인증 설정은 필요하지 않습니다. `--drive-file-id`와 `--data-dir`는 함께 사용할 수 없습니다. `site/` 입력 폴더 대신 생성한 출력 폴더를 웹서버에 연결해 주세요. Python 빌더를 직접 실행할 때는 먼저 `npm run compile`을 실행합니다.

## 워커 실행과 현재 Mac의 경로

현재 Mac의 코드 위치는 `~/mensa`, 비공개 상태·모델·로그 위치는 `~/Library/Application Support/Mensa`입니다. 기존 LaunchAgent는 같은 로그인 사용자로 `scripts.local_refresh --base`를 로그인 시와 900초 간격에 호출합니다. 이 코드는 `BASE/worker.toml`을 읽으며 설정이 없으면 자동 Git 게시로 되돌아가지 않습니다. Drive 전환 때도 기존 모델·큐·로그와 LaunchAgent를 보존합니다.

```sh
cd "$HOME/mensa"
/opt/homebrew/opt/python@3.14/bin/python3.14 -m scripts.local_refresh --base "$HOME/Library/Application Support/Mensa" --status
```

`--status`는 큐와 마지막 결과를 읽습니다. 이를 뺀 실행은 기존 일정·재시도 대기·자원 기준에 따라 대기 작업을 처리합니다. 수동 실행도 강제 갱신은 아닙니다. 수집 일정은 `Europe/Berlin` 기준 매일 09:00·10:00·11:00입니다. 화면을 잠가도 로그인과 깨어 있는 상태가 유지되면 실행할 수 있지만 로그아웃·시스템 잠자기 동안에는 실행되지 않습니다.

새 설치와 후임자는 [Drive 설정 예제](config/worker.drive.example.toml)를 자신의 계정·경로·모델·rclone remote·폴더 ID·파일 ID에 맞춥니다. `local_refresh --config /path/to/worker.toml`도 지원합니다. `paths.public_dir`는 코드와 비공개 상태에서 분리한 로컬 준비 폴더이며, Drive 모드에서 정적 웹사이트 루트를 뜻하지 않습니다. 현재 Mac의 예는 `~/Library/Application Support/Mensa Data`입니다.

## 데이터와 복구 기록 보존

Drive 워커는 새 원본의 날짜를 최신 값으로 교체하고 지난 30일·오늘·원본의 미래 메뉴를 공개합니다. 공개 JSON에는 그 메뉴가 참조하는 번역만 담습니다. 원본 발생·검증한 가격·주의 표시와 번역 연결을 유지하며, 수집 구조·날짜·가격 검사를 통과한 뒤 보관 기간을 적용합니다.

새 번역이 필요할 때만 준비된 모델을 사용합니다. 비공개 재사용 캐시는 미사용 180일·5,000항목·16MiB, 로컬 성공 게시본은 최근 10개를 기본으로 보관합니다. 현재본·재시도 자료·사람이 검토한 번역은 보호합니다. 실패하면 기존 공개 묶음을 유지하고 큐의 재시도 정책을 사용합니다. `queue.json`, `translation-state.json`, `drive-publication.json`과 복구 기록은 비공개이며 임의로 지우지 않습니다. [정리 기준과 사전 점검](docs/operations.md#데이터-보관-한도와-정리)을 참고해 주세요.

Drive의 공유 설정은 해당 설치 운영자가 관리합니다. 이번 전환은 사용자가 제공한 폴더의 기존 공유 설정을 그대로 사용합니다. 후임자는 자신의 연결과 공개 읽기 설정을 준비하고 파일 ID를 설정에 넣습니다. OAuth token/client secret은 rclone의 비공개 설정에만 보관하며 소스·워커 TOML·사이트에 넣지 않습니다. rclone 공용 OAuth client의 종료 공지와 자체 client 준비는 [인계 안내](docs/operations.md#drive-연결과-후임자-인계)를 확인해 주세요.

저장소 라이선스는 아직 결정되지 않았습니다.
