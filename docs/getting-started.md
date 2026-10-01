# 유지보수 시작 안내

이 문서는 코드와 번역 문구를 수정할 유지보수자를 위한 읽기 순서입니다. 프로젝트 최상위 폴더에는 README.md, package.json, mensa/, scripts/, frontend/가 있습니다. 아래 문서를 읽으면 식단을 준비하는 Python 프로그램과 이용자의 브라우저 코드, 운영자가 관리하는 자동 실행 파일을 구별할 수 있습니다.

## 먼저 읽을 문서

1. [유지보수 설명서](maintenance.md)에서 Python이 식단을 준비하고 브라우저가 JSON을 표시하는 흐름을 읽어 주세요. 문서의 파일 지도에서 수정할 기능의 담당 코드를 찾습니다.
2. [README](../README.md)에서 개발 명령을 확인해 주세요. 사이트 생성 프로그램이 읽는 site/ 입력과 새로 만드는 출력 폴더를 구별합니다.
3. [코드 구조](architecture.md)에서 실제 담당 함수/클래스를 읽어 주세요. 식단 처리 순서를 호출하는 RefreshService와 Linux 자동 실행 설정 mensa-refresh.service는 다른 대상입니다.
4. [메뉴/번역 파일 설명](data-contract.md)에서 menu.json의 원본·가격·반복 발생 ID와 translations.json의 원문 연결을 확인해 주세요. current.json이 선택한 같은 묶음의 두 파일을 브라우저가 읽는 방식도 확인합니다.
5. [설정과 운영](operations.md)에서 변경할 설정 키의 의미와 파일 기준 경로를 읽어 주세요. 실제 프로그램을 수동 실행할 운영자는 Linux User=mensa 또는 Mac 로그인 사용자와 작업 기록 파일의 소유권을 확인합니다.
6. [사이트 반영 절차](deployment.md)는 검토한 코드와 JSON을 실제 웹사이트에 게시할 운영자가 사용합니다.

## 코드를 수정하는 순서

유지보수자는 프로젝트 최상위 폴더에서 `git status --short`로 현재 변경 파일을 먼저 확인해 주세요. 이미 있는 미커밋/미추적 파일을 보존하고 본인이 수정할 기능의 파일을 유지보수 설명서의 파일 지도에서 찾습니다. 원본 HTML 추출은 scripts/menu_source.py, 가격/형식 검사는 mensa/menu_contract.py, 번역 문구와 재사용 판단은 mensa/translation_contract.py, 화면 메뉴 선택은 frontend/selectors.ts에서 시작합니다.

메뉴 필드를 추가하거나 원문/가격을 바꾸는 유지보수자는 Python 검사와 frontend/contracts.ts의 브라우저 검사도 함께 수정합니다. 화면 중복 선택 함수 visibleMeals(day)는 ID 이외의 모든 메뉴 내용을 비교하므로 새 필드의 비교 여부도 검토합니다. 화면 표현만 바꾸는 경우에도 원본 반복 항목과 가격·주의 표시를 menu.json에서 지우지 않습니다.

번역을 바꾸는 유지보수자는 원문 이름과 구성품 순서가 번역 key에 반영되는지 확인합니다. 모델을 바꾸는 운영자는 TOML provider/model/revision을 실제 모델에 맞추고, 유지보수자는 완료 번역을 다시 사용하는 판단이 그 변경을 반영하는지 확인합니다. translation-checkpoint.json에 저장된 완료 번역과 queue.json의 수집 시각/게시 번호는 삭제하지 않습니다. 삭제하면 다음 실행이 이미 수행한 작업을 잊을 수 있습니다.

유지보수자는 수정한 기능에 해당하는 검사를 실행한 뒤 `npm run build -- --output .tmp/build-example`처럼 아직 사용하지 않은 폴더에 사이트를 만들어 확인해 주세요. 사이트 생성 프로그램은 기존 파일을 보호하려고 출력 폴더가 있으면 중단합니다. 기존 결과를 지우지 말고 다른 폴더 이름을 선택합니다. 로컬 웹서버는 site/ 입력이 아니라 이 출력 폴더를 제공해야 합니다.

유지보수자는 `update_menu --validate-only`가 저장된 메뉴/번역의 검사라는 점을 확인해 주세요. 게시 담당 운영자는 require_current_coverage로 Berlin 오늘까지의 식단 날짜도 검사합니다. 원본 웹페이지를 가져오거나 모델에 요청하거나 GitHub에 게시하는 명령은 실제 데이터를 바꾸므로 운영 목적과 실행 계정을 확인한 뒤 사용합니다.

## 실패한 작업을 다룰 때

운영자는 --status로 작업 기록 폴더의 queue.json과 last-result.json을 먼저 읽습니다. 파일 수정/백업 전에 Linux timer와 실행 작업 또는 Mac 로그인 자동 실행과 수동 작업을 멈추고, 수정 코드에서 worker.lock의 파일 잠금을 사용합니다. 설정·모델·검토 문구의 실제 원인을 고친 뒤 운영 문서의 request_retry 예제를 따라 자동 재시도 중지만 해제합니다. 대기 수집 시각·시도 수·게시 번호·완료 번역·Git 복구 기록을 보존해야 같은 작업을 이어갈 수 있습니다.
