# 메뉴와 번역 JSON을 읽고 수정하는 방법

사이트를 만드는 Python 프로그램은 `site/data/menu.json`의 독일어 메뉴와 `site/data/translations.json`의 영어/한국어 번역을 읽습니다. 이 문서는 유지보수자가 두 파일의 필드를 바꾸거나 번역 규칙을 수정할 때 지켜야 할 검사 규칙을 설명합니다. 코드에서 이 규칙을 contract라고 부르며 해당 검사 함수는 `mensa/*_contract.py`에 있습니다.

식단 갱신 프로그램은 완료되지 않은 작업과 개별 번역을 웹에 공개하지 않는 폴더에 따로 저장합니다. 브라우저는 작업 기록이 아니라 완성된 메뉴/번역 파일만 읽습니다. 이 프로젝트는 JSON을 데이터베이스 엔진 없이 파일로 저장합니다.

## 날짜별 식단을 갱신하고 보존하는 규칙

식단 갱신 프로그램은 새 원본에 나온 날짜의 메뉴와 가격을 최신 값으로 교체합니다. 새 원본의 시작 날짜보다 앞선 날짜는 기존에 게시한 식단을 보존합니다. 따라서 원본 사이트가 지난 날짜를 제거해도 이용자는 우리 사이트에서 해당 날짜를 계속 선택할 수 있습니다. 같은 날짜의 오전 9시·10시·11시 수집 결과는 따로 쌓지 않고 마지막으로 검사를 통과한 값 하나만 남깁니다. 보존한 날짜를 자동으로 삭제하는 기간 설정은 없습니다.

프로그램은 날짜를 합치기 전에 새 원본 자체의 구조·메뉴 및 가격 손실·최신 날짜 범위를 검사합니다. 새 원본 범위 안에서 빠진 날짜를 과거 자료로 채워 수집 오류를 숨기지 않습니다. 새 원본의 마지막 날짜보다 뒤에 있던 기존 미래 날짜도 보존 대상이 아닙니다. 합친 메뉴 전체에 필요한 번역이 있는지 검사한 뒤 게시합니다.

`source.fetched_at`과 `source.sha256`은 마지막 원본 수집의 시각과 HTML 해시입니다. 보존한 지난 날짜를 다시 수집했다는 뜻은 아닙니다. `coverage`는 보존한 날짜까지 포함한 전체 조회 범위입니다. 화면은 지난 날짜의 메뉴를 선택하면 오늘 제공되는 식단이 아니라는 안내를 표시합니다.

## 브라우저가 같은 식단의 두 파일을 찾는 방법

메뉴와 번역을 함께 게시한 한 묶음을 코드에서는 release라고 부릅니다. 게시용 파일을 쓰는 `DirectoryPublisher` 클래스([`mensa/releases.py`](../mensa/releases.py))는 웹사이트 폴더 아래에 다음 구조를 만듭니다.

```text
data/
  current.json                    지금 보여 줄 묶음의 ID
  releases/
    <release_id>/
      menu.json                   그 묶음의 독일어 메뉴·가격·주의 표시
      translations.json           그 메뉴에 연결된 영어/한국어 번역
```

current.json은 정확히 `{"schema_version":1,"release_id":"<소문자 SHA256 64자리>"}`입니다. schema_version은 파일 형식의 버전이고 release_id는 두 파일 내용을 묶어 계산한 ID입니다. 예를 들어 current.json이 ID A를 지정하면 브라우저는 `data/releases/A/menu.json`과 `translations.json`을 읽습니다. 실제 ID는 A 한 글자가 아니라 소문자 SHA256 64자리입니다.

브라우저 파일 읽기 코드 [`frontend/data_client.ts`](../frontend/data_client.ts)는 current.json을 한 번 읽고 두 고정 경로를 동시에 요청합니다. 선택 파일이 중간에 바뀌어도 시작한 읽기는 같은 ID의 파일을 사용합니다. 형식 검사 코드 [`frontend/contracts.ts`](../frontend/contracts.ts)는 정해진 포인터 필드와 ID 형식만 허용하며 임의 경로를 읽지 않습니다. 메뉴/번역의 형식과 독일어 원문 연결을 검사하지만 두 파일의 SHA256을 다시 계산하지는 않습니다.

DirectoryPublisher는 메뉴와 번역이 모두 완성되었는지 검사하고 두 파일을 먼저 만든 뒤 current.json을 한 번에 교체합니다. 따라서 이용자가 아직 쓰는 중인 파일을 선택하지 않게 합니다. 이 클래스는 같은 ID에 다른 내용을 덮어쓰거나 이전 묶음을 삭제하지 않습니다. 같은 내용을 재게시하면 선택 파일을 불필요하게 다시 쓰지 않습니다.

게시 ID를 계산할 때 클래스는 JSON 키를 정렬하고 불필요한 공백을 없애며 UTF-8 바이트로 바꿉니다. 무한/NaN 숫자는 거절합니다. 계산 대상은 `u64be(menu byte length) + menu bytes + u64be(cache byte length) + cache bytes`입니다. u64be는 길이를 8바이트 big-endian 정수로 쓴 값이며 cache는 translations.json 내용입니다. 이 전체 바이트의 SHA256이 release_id입니다.

Python에서 기존 묶음을 읽는 `DirectoryPublisher.load_current()`는 선택 파일을 한 번 읽고 두 파일의 ID 계산과 표시 가능 여부를 검사합니다. 저장된 주의 표시 번역은 그 묶음을 게시했을 때 검토한 대응표로 검사합니다. 유지보수자가 현재 대응표를 편집했다고 과거의 완성된 묶음을 무효화하지 않으며, 새 게시에는 현재 대응표를 사용합니다.

## menu.json의 필드

원본 수집 함수 [`scripts/menu_source.py`](../scripts/menu_source.py)는 고정된 원본 페이지에서 날짜별 메뉴를 추출합니다. 메뉴 검사 함수 [`mensa/menu_contract.py`](../mensa/menu_contract.py)는 아래 필드와 원본 가격 연결을 확인합니다. 공개 메뉴의 schema_version은 1입니다.

| 필드 | menu.json에서 뜻하는 내용 |
| --- | --- |
| `source.url`, `source.fetched_at`, `source.sha256` | 읽은 원본 주소, 수집 시각, 원본 HTML 내용의 SHA256 |
| `coverage.start`, `coverage.end` | 이 파일에 들어 있는 식단 날짜 범위 |
| `days[].date`, `days[].meals` | ISO 날짜와 그 날짜의 원본 순서 메뉴 목록 |
| `meals[].id` | 원본에서 추출한 각 발생을 구별하는 ID |
| `translation_key` | 이 요리 이름과 순서가 있는 구성품 이름에 연결할 번역 key |
| `category`, `location`, `name_de` | 원본 분류, 위치, 독일어 요리 이름 |
| `components[].name_de`, `components[].notices` | 곁들임 음식 이름과 그 구성품의 원본 주의 표시 |
| `meals[].notices` | 요리 자체의 원본 주의 표시 |
| `prices`, `price_status`, `price_source` | 학생/교직원/방문객 가격과 검증 상태, 그 값을 확인한 원본 가격 기록 |

아래는 한 메뉴의 설명용 예제입니다. 실제 id와 translation_key는 코드가 원문으로 계산합니다.

```json
{
  "id": "<date-source-hash>",
  "translation_key": "<content-hash>",
  "category": "Wahlessen",
  "location": "Aufgang C",
  "name_de": "German dish",
  "components": [{"name_de": "German side", "notices": []}],
  "notices": [],
  "prices": {"student": 350, "staff": 465, "guest": 535},
  "price_status": "verified",
  "price_source": {
    "date": "2026-10-01", "category": "Wahlessen", "name": "German dish",
    "raw": "S: 3,50 | M: 4,65 | G: 5,35"
  }
}
```

가격 검사 함수는 원본의 S/M/G를 학생/교직원/방문객 가격과 각각 대조합니다. 저장 가격은 유로의 정수 센트이므로 예제의 350은 3.50유로입니다. 개별 가격표는 같은 날짜·분류·독일어 이름의 메뉴에 연결합니다. 일부 금액만 있는 잘못된 블록이나 같은 원문에 있던 가격의 소실은 거절합니다.

같은 판매대가 여러 메뉴를 나열하고 마지막 메뉴 뒤에 가격표를 한 번만 적으면, 수집 함수는 그 가격을 나열된 메뉴들의 공통 가격으로 적용합니다. 예를 들어 Wahlessen에 요리 A·요리 B·샐러드 뷔페가 있고 마지막에만 S/M/G 가격표가 있으면 세 메뉴가 같은 가격을 사용합니다. 판매대에 가격표가 여러 개 있거나 유일한 가격표 뒤에 다른 메뉴가 이어지면 수집 함수는 개별 가격을 유지합니다.

공통 가격을 적용받은 앞쪽 메뉴의 `price_source`에는 `scope:"counter"`를 추가합니다. 이 기록의 `name`은 가격표가 실제로 붙어 있던 마지막 메뉴의 독일어 이름이고, `raw`는 그 가격표의 원문입니다. Python과 브라우저 검사 함수는 같은 날짜·분류·위치에서 마지막 메뉴의 가격표가 하나뿐인지, 공통 가격과 원문이 그 메뉴의 값과 같은지 확인합니다. 따라서 유지보수자는 공통 가격을 개별 메뉴에 직접 적힌 가격처럼 기록하지 않아도 됩니다. 개별 가격표와 가격 미기재 기록에는 `scope` 필드가 없습니다.

원본이 같은 날짜·분류·위치의 판매대를 여러 구역으로 나누고 각 구역에 공통 가격을 붙이면 현재 JSON 검사 함수는 구역을 구별할 수 없어 게시를 거부합니다. 이 경우 유지보수자는 원본 구역을 구별하는 정보부터 메뉴 형식에 추가해 주세요. 다른 구역의 가격을 임의로 합치면 안 됩니다.

메뉴에 개별 가격표도 적용할 공통 가격표도 없으면 수집 함수는 prices:null, price_status:"source_pending", price_source.raw:null을 저장합니다. 화면은 원본 링크로 안내합니다. 유지보수자는 다른 판매대·날짜의 가격이나 PDF 값으로 이 항목을 채우지 마세요.

원본 수집 함수는 추출 결과와 별도로 HTML의 요리/구성품/주의 표시 요소, metadata 속성, 가격 표시와 S/M/G 표식을 종류별로 세어 대조합니다. 별도 HTML parser가 센 종류별 토큰 개수와 메뉴 추출 코드가 소비한 개수가 같아야 하므로 단순히 요리 개수만 맞추는 검사가 아닙니다. 정보성 안내는 요리 목록에서 제외합니다. 메뉴 검사 함수는 새 날짜 범위 안의 이전 날짜가 사라지거나 겹치는 날짜의 이전 메뉴가 1/4 넘게 사라지는 변화를 거절합니다. 유지보수자는 원본 형태가 바뀌었는지 검토해야 합니다.

게시 전 날짜 검사 함수 `require_current_coverage`([`scripts/update_menu.py`](../scripts/update_menu.py))는 coverage.end가 Berlin 기준 오늘보다 과거이면 거절합니다. `update_menu --validate-only`의 파일 형식/번역 검사는 이 날짜 검사를 대신하지 않습니다.

## 원본 발생 ID와 화면의 중복 제거가 다른 이유

메뉴 ID 계산 코드는 날짜·분류·독일어 이름을 정렬된 compact JSON으로 묶어 SHA256을 계산하고 ISO 날짜와 함께 사용합니다. 원본에서 같은 항목이 반복되면 수집 함수는 두 번째부터 -2, -3 같은 순번을 붙입니다. 서로 다른 발생에 같은 ID를 쓰지 않으며 반복된 원본을 모두 menu.json에 보관합니다.

화면에서 표시할 항목을 고르는 `visibleMeals(day)` 함수([`frontend/selectors.ts`](../frontend/selectors.ts))는 같은 날짜의 항목 중 ID를 제외한 모든 내용이 같은 첫 번째 항목만 반환합니다. 비교에는 분류·위치·독일어 이름·translation_key·순서가 있는 구성품/주의 표시·학생/교직원/방문객 가격 전부·가격 상태와 원문 기록이 들어갑니다. 일간/주간 화면을 그리는 코드는 두 보기에서 이 함수를 사용합니다.

화면 코드는 첫 번째 ID로 카드와 열린 상세를 유지하지만 JSON과 앱의 원본 메뉴 목록은 바꾸지 않습니다. 다른 날짜의 같은 요리, 공통 곁들임 음식, 이름만 같은 변형 메뉴는 유지합니다. 선택한 가격 한 개나 번역 제목만으로 항목을 합치지 않습니다. 유지보수자가 메뉴 필드를 추가하면 Python/브라우저 형식 검사와 이 전체 내용 비교도 함께 검토해 주세요.

## translations.json과 개별 완료 번역 파일

번역 파일은 schema_version 1이며 entries에 요리별 번역, notices에 주의 표시 대응표를 저장합니다. 원문으로 번역 key를 계산하는 `source_key` 함수([`mensa/translation_contract.py`](../mensa/translation_contract.py))는 `{"name_de": ..., "components": [...]}`의 키를 정렬하고 공백 없는 UTF-8 JSON으로 직렬화해 SHA256을 계산합니다. components는 원본 순서의 구성품 이름입니다. 원문이나 순서가 바뀌면 key도 바뀝니다.

```json
{
  "schema_version": 1,
  "entries": {
    "<content-hash>": {
      "source": {"name_de": "German dish", "components": ["German side"]},
      "en": {"name": "English dish", "components": ["English side"]},
      "ko": {"name": "한국어 요리", "components": ["한국어 곁들임"]},
      "origin": "reviewed-draft",
      "model": "assistant-draft",
      "prompt_version": "menu-v4"
    }
  },
  "notices": {"Weizen": {"en": "Wheat", "ko": "밀"}}
}
```

번역 검사 함수는 en/ko에 각각 name/components만 있고 구성품 개수가 원문과 같은지 검사합니다. source와 key도 맞아야 합니다. origin은 사람이 작성한 editorial-draft, 검토한 reviewed-draft 또는 model입니다. model과 prompt_version은 어떤 모델/번역 요청 규칙으로 만들었는지 기록합니다. 표현을 정리해도 코드는 원래 작성/생성 출처를 보존하며 사람의 초안을 모델 생성으로 바꾸지 않습니다.

번역 생성 조건을 기록하는 generation_identity 함수와 재사용을 판단하는 reusable_translation 함수는 `mensa/translation_contract.py`에 있습니다. generation_identity는 모델 종류(provider), 이름(model), 운영자가 선언한 버전(revision)과 번역 규칙·주의 표시 표의 SHA256 지문을 만듭니다. 번역 항목의 같은 이름인 generation_identity 필드에 이 결과가 저장됩니다. reusable_translation은 모델이 만든 항목의 요청 규칙 버전·모델 이름·저장된 생성 조건을 확인해 다시 사용할 수 있는지 판단합니다. TOML 경로에서는 임시 URL/포트/API key가 바뀌어도 같은 모델 버전이면 재사용 판단을 유지할 수 있습니다. 운영자가 모델 내용을 바꾸면 revision도 관리해야 합니다. 환경변수로 실행하는 별도 수동 번역 경로에는 revision 선언이 없으므로 URL이 이 비교 기록에 들어갑니다.

새 모델 번역의 요청 규칙 버전은 menu-v4이고 재사용 검사에서 지원하는 버전은 menu-v3/menu-v4입니다. 프로그램은 웹에 공개하지 않는 translation-checkpoint.json에도 검사를 통과한 항목을 저장합니다. 이 파일을 관리하는 `TranslationCheckpointStore`([`mensa/checkpoints.py`](../mensa/checkpoints.py))는 작업을 다시 시작할 때 완료 번역을 읽습니다. 일부 메뉴가 아직 없을 수 있으므로 운영자는 이 파일을 공개하지 마세요. 전체 식단의 두 언어·구성품·원문 연결·주의 표시 검사를 통과한 translations.json만 게시합니다.

브라우저는 생성 기록이 아니라 정확한 원문 연결과 표시값을 검사해 번역을 사용합니다. 번역이 원문에 맞지 않으면 독일어 이름/구성품을 유지합니다. 메뉴 파일이 먼저 도착하면 독일어로 화면을 만들고 번역 파일이 도착하면 보완합니다. 번역 요청 실패는 메뉴 파일 실패와 별도로 안내합니다.

## 사람이 검토하는 이름과 주의 표시

유지보수자는 [`data/editorial-translations.json`](../data/editorial-translations.json)에 검토한 구문을, [`data/notice-translations.json`](../data/notice-translations.json)에 정확한 독일어 주의 표시와 영어/한국어 문구를 적습니다. 두 번째 대응표를 코드에서 glossary라고 부릅니다. 번역 코드는 현재 메뉴의 식재료·식단·알레르기·첨가물 표시에 이 표를 사용합니다. 표에 없는 독일어 표시가 있으면 모델 요청과 파일 게시 전에 중단합니다.

번역 검사 코드는 오래된 schema 1 파일에서 top-level notices가 생략되면 빈 표로 읽을 수 있습니다. 그러나 새 게시 검사 함수는 현재 메뉴에 나온 모든 주의 표시의 검토 번역을 요구합니다. 유지보수자는 생략 호환성을 이용해 새 표시 검사를 피하면 안 됩니다.

이름 표현을 정리하는 [`mensa/translation_contract.py`](../mensa/translation_contract.py)는 mensaVital/KlimaTeller를 독일어 원문에 남기고 번역 제목을 설명형으로 정리합니다. 정확한 Wikingertopf는 “Meatball stew”/“고기완자 스튜”, Köttbullar는 “Swedish meatballs”/“스웨덴식 미트볼”, 구성품 Peperoni는 “Chili peppers”/“고추”입니다. 코드와 유지보수자는 vegan 등 식단 의미와 구성품 순서를 보존하고 원문에 없는 재료를 더하지 않습니다.

이름 표현을 정리하는 코드는 이름을 바꾼 항목에 name_policy_version:"semantic-names-v1", 구성품을 바꾼 항목에 component_policy_version:"semantic-components-v1"을 기록합니다. 번역 요청 함수는 모델이 가격·주의 표시를 생성하지 못하게 결과 필드를 제한합니다. Ollama를 선택한 요청은 loopback /api/chat을 사용하고 thinking을 끄며 JSON schema로 결과를 제한합니다. 요청 함수는 정상 완료와 구성품 수를 다시 확인합니다.

화면의 날짜/Today 계산은 Europe/Berlin 기준입니다. 화면 코드는 식단 날짜 범위와 수집 시각을 보여 주고 지난 식단/만료/오래된 자료를 안내합니다. 공개 파일 작성 코드는 한 작성 프로그램과 일반 파일시스템을 전제로 하며 전원 손실이나 적대적인 파일 경합까지 보장하지 않습니다. 운영자는 같은 사이트 data에 임의의 두 작성 프로그램을 동시에 연결하지 마세요.

가격 상태는 `source.fetched_at` 시점에 수집한 원본을 기준으로 합니다. `source_pending`은 그 시점에 적용할 개별 또는 공통 가격표가 없었다는 뜻입니다. 원본 사이트가 나중에 가격을 추가하면 다음 수집에서 가격을 반영합니다. 화면은 이 경우 “수집 당시 가격 미기재”라는 링크로 원본 식단을 안내합니다.
