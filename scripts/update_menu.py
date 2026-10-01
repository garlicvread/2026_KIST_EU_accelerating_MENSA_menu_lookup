"""update가 원본 식단과 번역을 검증하여 site/data의 메뉴·번역 JSON을 갱신합니다.

update가 수집·기존 메뉴 비교·날짜 검사·번역을 진행하고, write_snapshot이 공개 조건을
검사한 뒤 파일을 교체합니다. 메뉴 규칙과 번역 완결성은 mensa의 계약 검증 함수가
담당합니다. 두 파일은 각각 교체하므로 함께 바뀌는 하나의 원자적 저장은 아닙니다.
사이트 빌드와 배포는 이 모듈의 호출자가 담당합니다.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from mensa.publication import validate_publication
from scripts.menu_source import fetch_html, parse_menu, validate_menu
from scripts.notices import load_glossary
from scripts.translations import build_translations, model_config

ROOT = Path(__file__).resolve().parents[1]


def read_json(path, fallback=None):
    """path의 UTF-8 JSON을 읽어 해석한 값을 반환하며 파일이 없으면 fallback을 그대로 반환합니다.

    JSON 구조를 검증하거나 파일을 만들지는 않습니다. 읽기·해석 오류는 호출자에게
    전달하므로 누락된 파일에만 기본값을 적용하고 손상된 파일을 기본값으로 숨기지 않습니다.
    """
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def require_current_coverage(menu, today=None):
    """메뉴의 마지막 공개 날짜가 today보다 이전이면 ValueError를 발생시킵니다.

    today가 없으면 현재 Europe/Berlin 날짜를 사용합니다. 범위 끝이 오늘 또는 그 이후인지만
    확인하며 오늘의 메뉴가 존재하는지나 수집 시각이 최근인지는 확인하지 않습니다.
    """
    today = today or datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    if menu["coverage"]["end"] < today:
        raise ValueError("Source contains only past menus; refusing to mark them freshly updated")


def write_snapshot(directory, menu, translations, previous=None):
    """공개 조건을 검증한 메뉴·번역을 directory의 두 JSON 파일에 차례로 교체 저장합니다.

    기본 안내 용어집을 읽고 previous가 있으면 이전 메뉴의 손실도 검사합니다. 모든
    검증·직렬화를 끝낸 뒤 디렉터리를 준비하고, .pending-* 임시 파일을 쓰고 동기화하여
    translations.json 다음 menu.json 순서로 각각 교체합니다. 반환값은 없습니다.

    교체 중 OSError가 발생하면 이미 바꾼 파일의 복구를 시도하지만, 복구 자체도 실패할
    수 있으며 프로세스 중단은 복구 절차를 건너뛸 수 있습니다. 두 파일이 함께 원자적으로
    바뀐다고 가정하지 마십시오. finally는 남은 임시 파일을 정리합니다.
    """
    validate_publication(menu, translations, notice_glossary=load_glossary(), previous=previous)
    # write_snapshot은 공개 파일을 하나라도 수정하기 전에 메뉴·번역 전체를 검증하고 직렬화합니다.
    payloads = {
        "translations.json": (json.dumps(translations, ensure_ascii=False, indent=2) + "\n").encode(),
        "menu.json": (json.dumps(menu, ensure_ascii=False, indent=2) + "\n").encode(),
    }
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    staged = {}
    originals = {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in payloads}
    replaced = []
    try:
        for name, data in payloads.items():
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".pending-", delete=False) as temp:
                staged[name] = Path(temp.name)
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
        # write_snapshot은 번역 파일부터 교체합니다. 호출자가 기존 메뉴의 키를 보존한 캐시를
        # 전달하면 메뉴 파일 교체 전에도 기존 메뉴가 새 캐시에서 필요한 번역을 찾을 수 있습니다.
        for name in payloads:
            os.replace(staged[name], directory / name)
            replaced.append(name)
    except OSError:
        # 저장해 둔 바이트로 이미 교체한 파일부터 되돌립니다. 이 복구는 일반 쓰기·삭제를
        # 사용하므로 실패할 수 있으며, 두 파일의 일관성을 보장하는 트랜잭션은 아닙니다.
        for name in reversed(replaced):
            target = directory / name
            if originals[name] is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(originals[name])
        raise
    finally:
        for pending in staged.values():
            pending.unlink(missing_ok=True)


def update(root=ROOT, html=None):
    """root의 기존 메뉴·캐시·편집 문구를 읽어 갱신하고 저장된 메뉴의 집계 사전을 반환합니다.

    html을 주면 그 문자열을 해석하고 생략하면 원본 URL을 요청합니다. 메뉴 구조·이전
    메뉴 손실·날짜 범위를 먼저 검사한 뒤 환경 변수의 설정으로 필요한 번역을 구성합니다.
    모델 요청은 번역 구성 함수가 수행하며 공개 파일 저장은 write_snapshot에 위임합니다.
    입력 검사나 번역 구성에 실패하면 공개 파일 교체 단계로 진행하지 않습니다.
    """
    root = Path(root)
    directory = root / "site" / "data"
    previous = read_json(directory / "menu.json")
    cache = read_json(directory / "translations.json", {"schema_version": 1, "entries": {}})
    editorial = read_json(root / "data" / "editorial-translations.json", {"phrases": {}})
    menu = parse_menu(fetch_html() if html is None else html)
    validate_menu(menu, previous=previous)
    require_current_coverage(menu)
    translations = build_translations(menu, cache, editorial["phrases"], model_config())
    write_snapshot(directory, menu, translations, previous)
    return summarize(menu, translations)


def summarize(menu, translations):
    """메뉴 날짜·항목·가격 상태·번역 키 존재 수와 공개 범위·수집 시각을 집계하여 반환합니다.

    translated_meals는 각 메뉴 항목의 키가 entries에 있는지 세므로 여러 요리가 같은
    번역 키를 공유해도 항목별로 셉니다. 번역의 완결성 검사는 이 함수가 아닌
    validate_publication이 수행합니다. 파일을 읽거나 수정하지 않습니다.
    """
    meals = [meal for day in menu["days"] for meal in day["meals"]]
    return {"days": len(menu["days"]), "meals": len(meals),
            "verified_prices": sum(m["price_status"] == "verified" for m in meals),
            "source_pending_prices": sum(m["price_status"] == "source_pending" for m in meals),
            "translated_meals": sum(m["translation_key"] in translations["entries"] for m in meals),
            "coverage": menu["coverage"], "fetched_at": menu["source"]["fetched_at"]}


def main():
    """갱신 또는 --validate-only 검사를 실행하고 집계 결과를 JSON으로 출력합니다.

    --validate-only는 기존 메뉴·캐시·용어집만 검사하며 원본 요청·모델 호출·스냅샷 교체를
    하지 않습니다. 두 경로 모두 GITHUB_STEP_SUMMARY가 설정되어 있으면 지정된 파일에
    결과를 덧붙이므로 검사 전용 실행에도 이 파일 쓰기는 발생할 수 있습니다.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.validate_only:
            menu = read_json(ROOT / "site/data/menu.json")
            cache = read_json(ROOT / "site/data/translations.json")
            validate_publication(menu, cache, notice_glossary=load_glossary())
            report = summarize(menu, cache)
        else:
            report = update()
    except (ValueError, OSError, TypeError, KeyError) as exc:
        # main은 갱신 오류를 실패 종료 상태로 알립니다. CI는 실패 시 중단되어 현재 공개된 Pages를 유지합니다.
        raise SystemExit(f"Menu update rejected: {exc}") from None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("### Validated menu snapshot\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
