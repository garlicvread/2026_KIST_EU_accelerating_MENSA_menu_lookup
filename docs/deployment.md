# 화면 배포와 수집기 코드 반영

수집기는 수집 호스트에서 원본 식단·가격과 사용할 수 있는 번역을 Google Drive의 `current.json`에 게시합니다. GitHub Actions 배포 실행기는 이 공개 묶음을 검증해 화면 코드와 함께 Pages에 배포합니다. 웹브라우저는 Pages의 JSON을 읽습니다. 데이터 갱신은 데이터 커밋·PR을 만들지 않으며 브라우저용 API 키나 추가 유료 서비스가 필요하지 않습니다. 이 문서는 평소 코드 반영·복구 절차와 기존 Git 게시에서 한 번 수행하는 전환 절차를 설명합니다.

[공개 식단](https://garlicvread.github.io/2026_KIST_EU_accelerating_MENSA_menu_lookup/) · [설정과 복구](operations.md) · [데이터 계약](data-contract.md)

## 코드 배포 준비

운영 체크아웃과 분리된 개발 폴더에서 기존 변경을 확인하고 보존해 주세요. Python 3.12 이상과 Node.js 24로 다음 검사를 실행합니다. 의존성 잠금 파일이 바뀌었거나 새 환경일 때만 먼저 `npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org`를 실행합니다.

```sh
python3 -m unittest discover -s tests -v
npm test
python3 -m scripts.update_menu --validate-only --data-dir tests/fixtures/menu-data
npm run build -- --drive-file-id FILE_ID --output .tmp/deploy-example
```

`FILE_ID`는 해당 설치의 공개 `current.json` 파일 ID로 바꿉니다. 출력 경로는 아직 없는 폴더여야 합니다. Drive 빌드는 실행기가 JSON을 내려받아 해시·게시 계약·검토 사전을 검사하고 정적 JSON을 결과물에 포함합니다. 네트워크 없이 화면을 확인하려면 별도 출력에 `--data-dir tests/fixtures/menu-data`를 사용합니다. 두 데이터 옵션은 동시에 지정할 수 없습니다. 두 모드 모두 공개 JSON과 화면을 빌드하며 비공개 재사용 캐시·큐·모델·로그는 포함하지 않습니다.

일간/주간·세 언어·가격·독일어 원문·열린 상세·날짜 이동·작은 화면을 확인합니다. 고정 예제 검사와 성공한 Drive 빌드만으로 식단 최신성이 확인되지는 않습니다. 실제 묶음의 `menu.source.fetched_at`와 날짜 범위를 별도로 확인합니다.

## GitHub Pages에 화면 코드 게시하기

저장소의 Actions 변수 `MENSA_DRIVE_FILE_ID`에 공개 `current.json`의 파일 ID를 넣습니다. 저장소 Settings → Secrets and variables → Actions → Variables에서 관리합니다. 값은 공개 데이터의 식별자이며 OAuth token이나 client secret을 넣는 자리가 아닙니다. 설치를 처음 연결하거나 파일 ID를 바꿀 때만 설정합니다. 매일 식단을 올릴 때 이 변수나 저장소를 변경하지 않습니다.

[`ci.yml`](../.github/workflows/ci.yml)은 코드 PR·main push·수동 요청에서 Python/브라우저 검사, 고정 예제 검증과 예제 화면 빌드를 실행합니다. [`update-and-deploy.yml`](../.github/workflows/update-and-deploy.yml)은 main push·수동 `workflow_dispatch`·매시 17분과 47분 예약 실행에서 Drive 데이터를 포함해 빌드합니다. 예약 실행은 현재 Pages와 게시본이 같으면 업로드·배포를 생략합니다. 코드 변경·수동 요청에는 전체 회귀 검사를 실행하며 예약 실행은 main 코드를 컴파일하고 데이터를 검사합니다. 예약은 지연되거나 비활성화될 수 있어 30분 이내 반영을 보장하지 않습니다. 수동 요청은 다음과 같습니다.

```sh
gh workflow run update-and-deploy.yml --ref main --repo garlicvread/2026_KIST_EU_accelerating_MENSA_menu_lookup
```

배포 작업은 Python 3.12·Node.js 24·잠금 파일의 컴파일러를 사용하고 검사 후 `MENSA_DRIVE_FILE_ID`로 빌드합니다. `.tmp/pages`를 기존 GitHub Pages 주소에 배포하며 `mensa-pages` 그룹의 진행 중인 배포를 취소하지 않습니다. 실패하면 해당 코드 실행을 확인하여 수정·재실행합니다. Drive 수집기 큐에서 Pages 실행 번호를 찾거나 일일 게시를 다시 요청하지 않습니다.

성공한 실행의 코드 SHA와 공개 주소를 확인합니다. Pages의 `data/current.json`과 Drive 묶음의 `release_id`가 같아야 합니다. 웹브라우저 Network에서는 같은 Pages 사이트의 `data/current.json`, `data/releases/<ID>/menu.json`, `translations.json`을 읽는지 확인합니다. 실제 원본 수집 시각과 날짜 범위도 대조합니다. 번역만 보완해도 릴리스 ID가 바뀔 수 있으며 Pages 재배포는 `source.fetched_at`를 새로 만들지 않습니다.

## 평소 실행 코드 반영과 장애 복구

수집기 코드·검토 번역 사전을 바꿀 때는 [중지 절차](operations.md#실패-확인과-재시도)에 따라 자동·수동 실행을 멈춥니다. 기존 변경을 보존하고 같은 `worker.lock`의 OS 잠금 안에서 검토한 커밋을 코드 체크아웃에 fast-forward한 뒤 재개합니다. 큐·체크포인트·Drive 파일 ID를 유지합니다. 화면 코드를 바꾼 경우에는 위 Pages 배포도 확인합니다.

`python3 -m scripts.local_refresh --config /path/to/worker.toml --status`에서 수집 완료와 번역 대기·실패를 각각 확인합니다. 수집·번역 분리 버전은 기존 모델 실패를 별도 번역 상태로 인계하고 새 수집을 계속합니다. `translation-work.json`은 정상 실행이 같은 잠금 아래 준비하므로 상태 파일을 수동으로 만들거나 이전 큐를 지우지 않습니다. 손상된 개별 번역은 재생성 전까지 원문으로 표시합니다. 모델 문구는 유효한 새 표현으로 교체할 수 있으며 기존 문장과의 동일성을 배포 조건으로 삼지 않습니다. 원문·가격·날짜·해시 검증과 새 내용의 새 릴리스 ID는 유지합니다.

화면 롤백은 같은 사이트의 JSON을 지원하는 검증한 코드와 해당 데이터 배포물을 단위로 진행합니다. 식단 복구는 로컬 보존 릴리스의 메뉴·번역을 같은 쌍으로 읽고 현재 날짜·원문·가격·번역 조건을 검증한 뒤 같은 Drive 파일에 게시하고 Actions로 Pages에 반영합니다. 오래된 묶음을 최신 식단인 것처럼 올리거나 메뉴와 번역을 서로 다른 릴리스에서 가져오지 않습니다. 복구 작업도 자동 실행 중지와 같은 큐 잠금 아래에서 수행해 주세요.

업로드 결과가 불명확하면 원격 파일과 `drive-publication.json`이 가리키는 로컬 릴리스를 확인합니다. 같은 준비본을 재사용하는 정상 재시도를 우선하며 큐·저널·번역 기록을 삭제하지 않습니다. 이전 Git 게시 기록은 역사 자료로 보존하고, 기존 명시적 Git 설치를 조사할 때만 해당 코드를 확인합니다. Drive 운영에서 Git 커밋 복구나 Pages 재배포로 일일 업로드 실패를 해결하지 않습니다.

## 기존 Git 게시에서 Drive로 전환하기

이 절차는 기존 Git 게시 설치를 Drive로 옮길 때 한 번 수행합니다. 설치된 코드·모델·큐·로그와 자동 실행 정의를 보존합니다. 기존 데이터에는 지난 공개 날짜와 검증된 가격·번역이 있으므로 추적 JSON을 제거하는 코드 반영 전에 별도 보존본을 준비합니다. Drive 폴더·파일의 공유 설정은 전환 명령이 변경하지 않습니다.

1. 분리된 개발 체크아웃에서 코드 검사를 완료하고, 현재 큐에 진행 중인 작업이 없는지 기존 수집기의 `--status`로 확인합니다. Git 게시 단계나 게시 번호가 남아 있으면 기존 작업을 먼저 확인합니다. 전환 명령은 활성 대기 작업을 버리지 않습니다.
2. [중지 절차](operations.md#실패-확인과-재시도)로 자동 실행과 수동 작업을 멈춥니다. 같은 `worker.lock`을 확보하여 기존 큐·결과·저널·모델 설정과 메뉴/번역의 일관된 비공개 백업을 만듭니다. 기존 소스의 `site/data` 쌍을 이용한다면 코드 fast-forward 전에 보존합니다. 잠금을 풀고 다른 작성자가 시작되지 않게 유지합니다.
3. 검증한 새 코드가 있는 폴더에서 아래 전환 명령을 실행합니다. 명령은 같은 큐 잠금을 직접 확보하며 검증된 쌍을 로컬 게시 준비 폴더와 Drive에 보존하고 `BASE/worker.toml`을 작성합니다. 외부 잠금 안에서 명령을 중첩 실행하지 않습니다.
4. 반환된 `config_path`, `file_id`, `release_id`, `data_dir`를 기록합니다. 익명 HTTP 성공·JSON 본문과 보관 기간의 메뉴·필요한 번역을 확인합니다. 원본 가격·교정 번역과 비공개 재사용 캐시를 보존합니다. 생성 설정의 모델·revision·자원·플랫폼을 실제 호스트에 맞춥니다. main 반영 전에 `MENSA_DRIVE_FILE_ID`를 반환된 파일 ID로 설정합니다.
5. 다시 같은 `worker.lock`을 확보하여 수집 호스트의 코드 체크아웃에서 브랜치·origin·기존 변경을 확인하고 검토한 main으로 fast-forward합니다. 다른 변경은 보존하며 reset/clean/force push로 해결하지 않습니다. 기존 코드 연결과 모델·큐 위치를 유지합니다.
6. main 코드의 Pages 배포를 완료하고 공개 화면과 Drive의 게시본 ID가 같은지 확인한 뒤 기존 자동 실행을 재개합니다. 호출 경로와 설정이 유지된다면 자동 실행 정의를 재설치할 필요가 없습니다.
7. 재개한 수집기의 `queue.completed_period`, Drive의 `menu.source.fetched_at`와 릴리스 ID를 대조합니다. 전자는 완료한 예약 슬롯이고 후자는 실제 수집 시각입니다. 일상 갱신 전후 Git HEAD·추적 파일이 바뀌지 않고 데이터 커밋·PR이 생기지 않는지 확인합니다. Pages의 ID가 Drive와 일치하는지 확인한 뒤 전환 완료를 기록합니다.

```sh
python3 -m scripts.configure_drive \
  --base /path/to/private-state \
  --checkout /path/to/checkout \
  --data-dir /path/to/publication \
  --remote gdrive: \
  --folder-id FOLDER_ID \
  --source-data OLD_PAIR_DIR
```

`FOLDER_ID`는 실제 폴더 ID, `OLD_PAIR_DIR`는 보존한 `menu.json`·`translations.json` 폴더로 바꿉니다. 이미 검증된 Drive 묶음에서 인계받으면 `--source-data`를 생략합니다. 명령은 기존 유효한 원격 파일을 함부로 덮어쓰거나 중복 `current.json`을 선택하지 않습니다. 전환 명령의 상세 조건은 [운영 문서](operations.md#최초-데이터-이관과-설정-준비)를 참고해 주세요.

생성 설정은 managed Ollama `gemma4:31b`와 macOS 자원 기본값을 사용합니다. Linux 또는 다른 모델의 설정은 [플랫폼별 설치](operations.md#새-환경에-설치할-때)에 따라 활성화 전에 조정합니다. `[retention]`과 모델의 실제 digest도 확인합니다. 전환 후 `python3 -m scripts.retention --config /path/to/worker.toml`로 정리 후보·보호 사유를 보고 보관 정책을 적용합니다. 전환 백업은 기록한 소유권과 검증 완료일에 따라 관리하며 미완료 전환의 입력을 먼저 삭제하지 않습니다.
