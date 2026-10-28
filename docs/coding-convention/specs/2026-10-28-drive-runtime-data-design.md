# Drive 기반 식단 데이터 게시 설계

목표는 일상적인 메뉴·번역 갱신이 데이터 커밋·PR을 만들지 않도록 소스와 운영 데이터를 분리하는 것입니다. 운영비는 추가되지 않아야 하며 Cloudflare 등 새 서비스·결제 설정·브라우저용 API 키를 도입하지 않습니다. 운영용 컴퓨터의 워커가 Drive를 갱신하고 GitHub Actions 실행기가 공개 묶음을 받아 화면·JSON을 Pages에 배포합니다. 웹브라우저는 같은 Pages 사이트의 JSON을 읽습니다. 운영 문서도 이 구조와 인계 절차에 맞춰 갱신합니다.

2026-10-28 사용자가 승인한 보관 정책을 함께 적용합니다. 메뉴 지난30일·원본 미래 날짜, 참조 번역만 공개, 운영 워커 캐시 미사용180일·5,000항목·16MiB, 성공 복구본10개와 진행 자료 보호, 로그14일·10MiB, 등록 전환 백업 정책과 생성 자료256MiB 한도를 사용합니다. 이 정책이 이 문서의 이전 무기한 이력 보존 설명을 대체하며 정확한 동작은 [운영 문서](../../operations.md#데이터-보관-한도와-정리)에 있습니다.

## 확정한 경계

- 현재 설치는 사용자가 제공한 Drive 폴더 `1IvVl551VYwDOURMtdVQDTlr_xeH662P8`를 사용합니다. 기존 읽기 전용 `gdrive:` 대신 사용자 승인으로 인증한 쓰기 연결 `mensa-drive:`를 사용합니다. 코드에는 설치별 폴더 ID·파일 ID·연결 이름을 하드코딩하지 않습니다.
- 사용자의 지시에 따라 폴더와 파일의 공유 권한을 변경하지 않습니다. 후임 운영자가 자기 Drive 연결과 공유 설정을 준비하고 설정 파일에 입력합니다.
- Drive의 기존 파일을 삭제하거나 동기화 삭제하지 않습니다. 예약된 파일 이름은 `current.json` 한 개이며, 이 파일은 검증된 공개 식단·번역 묶음입니다.
- 모델·비공개 번역 체크포인트·큐·복구 기록은 기존 로컬 상태 경로에 보관합니다. Drive에 인증 정보나 비공개 작업 기록을 업로드하지 않습니다.
- 기존 Git 이력은 다시 쓰지 않습니다. 운영 JSON을 앞으로 소스에서 추적하지 않는 코드 변경만 수행합니다.

## 공개 데이터 계약

Drive `current.json`의 JSON 구조는 다음과 같습니다.

```json
{"schema_version":1,"release_id":"<64 lowercase hex>","menu":{},"translations":{}}
```

`menu`와 `translations`는 현재의 검증된 스키마를 그대로 사용합니다. `release_id`는 기존 `mensa.releases._release_id(_json_bytes(menu), _json_bytes(translations))` 규칙으로 계산합니다. 기존 가격·원문·주의 표시·번역 검사와 지난 날짜 보존을 유지합니다. 빌더는 한 Drive 묶음을 검증해 같은 ID의 두 JSON과 포인터를 배포합니다.

공개 다운로드 주소는 `https://drive.usercontent.google.com/download?id=<FILE_ID>&export=download&authuser=0&confirm=t`입니다. 빌드 실행기가 API 키 없이 서버 측 HTTP 요청으로 읽습니다. 브라우저가 Drive를 직접 읽는 방식에서 발생한 CORS 의존성은 제거합니다. 로그인 화면·HTML·손상된 게시본·16MiB 초과 응답은 배포하지 않습니다.

브라우저의 `DataClient` 인터페이스는 `loadManifest/loadMenu/loadTranslations`를 유지합니다. `createDataClient(fetchPort)`는 같은 사이트의 포인터와 그 ID에 고정된 두 JSON만 읽습니다. 기존 지연 번역·독일어 표시·오류 처리와 오래된 요청 무시 동작을 유지합니다. Drive 파일 ID·API 키 메타 태그는 사용하지 않습니다.

## 게시와 복구

`DrivePublisher(remote, folder_id, *, file_id=None, command=None)`는 rclone만으로 지정 폴더의 `current.json`을 읽고 갱신합니다. `publish(menu, cache, *, previous=None, today=None)`는 먼저 전체 게시 검사를 수행하고 `DrivePublication(release_id, file_id)`를 반환합니다. 같은 내용은 다시 업로드하지 않습니다. 업로드 후 내용·파일 ID를 확인합니다. 설정된 파일 ID와 실제 파일 ID가 다르거나 같은 이름의 파일이 여럿이면 중단합니다. 원격 기존 파일이 유효한 게시 묶음이 아니면 덮어쓰지 않습니다. `load_current()`는 검증된 이전 묶음을 반환하거나 파일이 없으면 None을 반환합니다. Git·gh·공유 권한 변경 명령은 이 경로에서 금지합니다.

`DriveRefreshJob`은 기존 JobRunner의 큐 잠금을 사용합니다. 새 수집의 이전 메뉴·번역은 소스 JSON이 아닌 현재 공개 Drive 묶음에서 읽어 교정과 이력을 보존합니다. 로컬 데이터 경로는 완성된 준비본과 재시도용 보존을 담당합니다. 새 묶음을 로컬에 먼저 준비하고 `drive-publication.json`에 수집 주기·릴리스 ID·이전 공개 릴리스 ID를 저장한 다음 업로드합니다. 업로드나 완료 기록이 중단되면 같은 준비된 묶음을 재사용하되, 다른 유효한 원격 교정이 생기면 최신 공개본에서 다시 준비합니다. 기존 큐·모델·체크포인트·가격 검사는 초기화하지 않습니다. 업로드 실패는 기존 큐의 실패/재시도 정책으로 전달합니다.

설정은 기존 WorkerConfig에 `publication.mode="drive"`, `remote`, `folder_id`, `file_id`를 추가합니다. `paths.public_dir`는 Git 체크아웃·비공개 상태 경로와 분리된 로컬 준비 경로입니다. 현재 Mac은 `--base` 실행에서 base/worker.toml을 읽어 Drive 경로로 동작하도록 전환합니다. 기존 네이티브 번역의 캐시 재사용 및 모델 실행 경로는 재사용하고, 다른 서버는 `--config`로 모델·자원·Drive 설정을 명시합니다. 이후 설정 누락으로 이전 자동 Git 게시가 다시 활성화되어서는 안 됩니다.

## 화면 배포와 데이터 갱신

화면 빌드는 `--drive-file-id` 또는 명시적인 로컬 `--data-dir`를 받습니다. Drive 모드에서는 공개 JSON을 내려받아 검증하고 결과물에만 포함합니다. GitHub Actions는 저장소 변수 `MENSA_DRIVE_FILE_ID`를 사용하며 일상 데이터 갱신에서 이 변수나 Git 소스를 수정하지 않습니다. main push·수동 실행 외에 30분 간격으로 새 게시본을 확인하고 같은 ID이면 예약 배포를 생략합니다. 예약 실행 시각은 보장하지 않습니다. 배포용 첨부파일은 1일 보관합니다.

자동 검사에는 작고 고정된 `tests/fixtures/menu-data`의 메뉴·번역을 사용합니다. `site/data/menu.json`과 `site/data/translations.json`은 추적 대상에서 제거합니다. 실제 운영 데이터는 전환 전에 백업하며 공개 데이터에는 승인한 보관 기간을 적용합니다. 기간 안의 원문·가격·번역 연결과 보호 자료를 보존합니다.

## 검증과 전환 순서

1. 빌더의 API 키 없는 다운로드·검증·배포물 생성과 브라우저의 같은 사이트 읽기를 회귀 검사합니다. 전체 Python/브라우저 검사와 실제 Drive 빌드를 수행합니다.
2. 기존 검증된 게시 데이터를 Drive current.json으로 복사하고 공개 다운로드를 실제로 검사합니다. 설정과 운영 문서를 준비합니다.
3. 큐에 진행 중 작업이 없는지 확인하고 자동 워커를 잠시 멈춘 뒤 같은 worker.lock 안에서 기존 데이터를 보존하고 새 설정·운영 코드를 반영합니다. GitHub Pages의 파일 ID를 연결합니다.
4. 코드 변경 한 건을 검사·검토·게시합니다. 실제 Pages 포인터와 Drive 묶음의 게시본 ID가 같고 브라우저가 Pages JSON을 읽는지 확인합니다.
5. 자동 워커를 복구하고 같은 식단을 갱신해도 소스 작업 트리와 Git HEAD가 변하지 않는지 확인합니다. 실패 시 기존 데이터와 코드를 보존하고 해당 단계만 복구합니다.

Google Drive의 기존 계정 저장 공간·다운로드 제한을 따릅니다. 추가 용량 구매, 새 결제 연결, 유료 서비스 전환은 수행하지 않습니다. rclone 인증은 각 운영자의 환경 설정이며 문서에 설정·갱신 방법과 현재 공유 client_id의 종료 공지를 함께 설명합니다.
