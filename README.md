# Saarbrücken Mensa

Saarbrücken 멘자의 독일어 식단을 한국어·영어·독일어로 보여 드립니다. 하루/주간 보기에서 학생·교직원·방문객 가격, 독일어 원문, 곁들임 음식과 주의 표시를 확인하실 수 있습니다.

[공개 식단](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/) · [원본 식단](https://www.stw-saarland.de/gastro/mensa-saarbruecken/)

## 식단과 화면을 게시하는 방식

수집 호스트의 Python 수집기(worker)가 원본 식단을 수집하고 검증한 메뉴·가격과 사용할 수 있는 번역을 Google Drive의 `current.json`에 게시합니다. GitHub Actions 배포 실행기는 이 묶음을 내려받아 검증하고 메뉴·번역 JSON을 화면 코드와 함께 GitHub Pages에 배포합니다. 웹브라우저는 같은 Pages 주소의 화면과 JSON을 읽습니다. 웹 백엔드나 데이터베이스 엔진은 없습니다.

원본 수집은 모델을 실행하지 않습니다. 번역이 없거나 손상된 항목도 새 메뉴·가격과 함께 게시하며, 화면에는 독일어 원문을 표시합니다. 별도 번역 작업이 필요한 항목을 보완하므로 모델 자원 부족이나 번역 재시도 대기가 다음 수집을 막지 않습니다.

일상적인 식단 갱신은 데이터 커밋·PR을 만들지 않습니다. Actions는 30분 간격으로 Drive 데이터를 확인하고 공개 Pages의 게시본 정보가 달라졌을 때 배포합니다. `main` 코드 변경이나 수동 요청도 배포를 실행합니다. 예약 작업은 지연되거나 비활성화될 수 있어 정확한 반영 시각을 보장하지 않습니다. 실제 식단 JSON은 배포물에 포함하며, 저장소의 테스트에는 `tests/fixtures/menu-data`의 고정 예제를 사용합니다.

Drive 게시에는 연결한 계정의 저장 공간과 다운로드 한도가 적용됩니다. 현재 구조는 Drive·GitHub Actions·GitHub Pages를 사용하며 추가 유료 서비스의 도입을 요구하지 않습니다. 프로그램이 저장 공간을 자동으로 구매하지는 않습니다.

이 문서는 Drive 방식의 운영 구조를 설명합니다. 설치를 활성화하거나 기존 Git 게시에서 전환할 때는 [배포 안내](docs/deployment.md#기존-git-게시에서-drive로-전환하기)에 따라 Drive 공개 읽기, Pages 배포, 자동 수집 실행을 확인해 주세요.

## 문서 읽기

- 처음 맡으시면 [시작 안내](docs/getting-started.md)와 [유지보수 설명서](docs/maintenance.md)를 읽어 주세요.
- 설치·실행 계정·Drive 연결·오류 복구·후임자 인계는 [운영 문서](docs/operations.md)에 있습니다.
- 코드와 실제 설치에 수정본을 반영하는 순서는 [배포 안내](docs/deployment.md)를 사용해 주세요.
- 함수와 파일 위치는 [구조 문서](docs/architecture.md), JSON 필드·가격·해시 규칙은 [데이터 설명](docs/data-contract.md)에 있습니다.

## 로컬에서 검사하고 화면 확인하기

수집기가 사용하는 코드 폴더와 분리된 개발 체크아웃에서 작업해 주세요. 저장소의 CI는 Python 3.12와 Node.js 24를 사용합니다. 새 체크아웃이거나 잠금 파일이 바뀌었을 때 `npm ci`로 프런트엔드 컴파일러를 설치합니다.

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

실제 게시 데이터를 확인하려면 공개 `current.json`의 Drive 파일 ID를 지정합니다.

```sh
npm run build -- --drive-file-id FILE_ID --output .tmp/build-drive
```

`FILE_ID`를 해당 설치의 값으로 바꿔 주세요. 빌드를 실행하는 컴퓨터가 공개 Drive JSON을 내려받아 게시 형식·원문 연결·해시와 검토 사전의 구조를 검사하고 메뉴·번역을 결과물에 포함합니다. 이 공개 읽기에는 API 키나 Drive 인증 설정이 필요하지 않습니다. `--drive-file-id`와 `--data-dir`는 함께 사용할 수 없습니다. 생성한 출력 폴더를 웹서버에 연결해 주세요. Python 빌더를 직접 실행할 때는 먼저 `npm run compile`을 실행합니다.

## 수집기 설정과 실행

수집 호스트는 수집기를 실행하는 컴퓨터나 서버입니다. 운영자는 실행 계정과 자동 실행 서비스를 정하고, 코드·비공개 상태·로컬 게시 준비 폴더, 모델과 Drive 연결을 `worker.toml`에 설정합니다. 자동 실행에도 같은 설정 파일을 사용합니다. 수집기의 자원 검사는 macOS와 Linux를 지원합니다. 플랫폼별 설치와 자동 실행은 [운영 문서](docs/operations.md#새-환경에-설치할-때)를 따라 주세요.

수집기 코드 폴더에서 실제 설정 파일의 절대 경로를 지정하여 상태를 확인합니다.

```sh
python3 -m scripts.local_refresh --config /path/to/worker.toml --status
```

`--status`는 수집 큐·번역 작업 상태·마지막 결과만 읽습니다. 이를 뺀 실행은 기존 일정과 재시도 대기에 따라 수집을 처리하고, 새 번역을 생성할 때 모델 자원 기준을 적용합니다. 수동 호출도 같은 일정을 따릅니다. 수집 예정 시각은 `Europe/Berlin` 기준 매일 09:00·10:00·11:00이며 실제 실행은 수집 호스트와 자동 실행 서비스의 가동 상태에 달려 있습니다.

`--base /path/to/base`는 `/path/to/base/worker.toml`을 읽는 편의 인자입니다. Drive 모드에서는 `--config`와 같은 설정·작업을 사용하며, 일반 실행에는 설정 파일이 필요합니다. 새 설치와 후임자는 [Drive 설정 예제](config/worker.drive.example.toml)를 자신의 계정·경로·모델·rclone remote·폴더 ID·파일 ID에 맞춥니다. 예제의 macOS 자원 정책도 해당 호스트에 맞게 조정합니다. TOML의 상대 경로는 설정 파일이 있는 폴더를 기준으로 해석합니다.

`paths.checkout_dir`, `paths.state_dir`, `paths.public_dir`는 서로 겹치지 않는 폴더로 설정합니다. Drive 모드의 `paths.public_dir`는 로컬 게시 준비 폴더입니다. 실제 경로와 실행 계정, 설정 파일 위치는 운영자가 기록하고 후임자에게 인계해 주세요.

## 데이터와 복구 기록 보존

Drive 수집기는 새 원본에 나온 날짜의 식단을 최신 값으로 교체하고 기본 보관 정책에 따라 지난 30일·오늘·원본의 미래 메뉴를 공개합니다. 공개 JSON에는 그 메뉴가 참조하는 정상 번역만 담습니다. 수집기는 원본의 반복 기록·검증한 가격·주의 표시와 번역 연결을 유지하며, 구조·날짜·가격 검사를 통과한 뒤 보관 기간을 적용합니다.

비공개 재사용 캐시의 기본 한도는 미사용 180일·5,000항목·16MiB이며, 로컬 성공 게시본은 최근 10개를 보관합니다. 현재본·재시도 자료·사람이 검토한 번역은 보호합니다. 수집 큐, 번역 상태, Drive 재시도 입력과 복구 기록을 임의로 지우지 마세요. 실패하면 먼저 `--status`로 수집과 번역 상태를 구분하고, 원인을 고친 뒤 [복구 절차](docs/operations.md#실패-확인과-재시도)를 따라 주세요. 번역 대기는 `--retry-translations`로 별도로 해제할 수 있습니다. 상세 한도는 [데이터 보관 기준](docs/operations.md#데이터-보관-한도와-정리)에 있습니다.

Drive의 공유 설정과 인증은 해당 설치 운영자가 관리합니다. 후임자는 자신의 연결과 공개 읽기 설정을 준비하고 파일 ID를 설정에 넣습니다. OAuth token/client secret은 rclone의 비공개 설정에만 보관하며 소스·수집기 TOML·사이트에 넣지 않습니다. 연결 준비와 인계 항목은 [Drive 인계 안내](docs/operations.md#drive-연결과-후임자-인계)를 확인해 주세요.

저장소 라이선스는 아직 명시되지 않았습니다.
