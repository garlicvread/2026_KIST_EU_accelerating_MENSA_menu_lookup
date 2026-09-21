"""Collect, validate and replace static snapshots only after all checks pass."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from scripts.menu_source import fetch_html, parse_menu, validate_menu
from scripts.notices import require_notice_coverage
from scripts.translations import build_translations, model_config, validate_cache

ROOT = Path(__file__).resolve().parents[1]


def read_json(path, fallback=None):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def require_current_coverage(menu, today=None):
    today = today or datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    if menu["coverage"]["end"] < today:
        raise ValueError("Source contains only past menus; refusing to mark them freshly updated")


def write_snapshot(directory, menu, translations, previous=None):
    validate_menu(menu, previous=previous)
    validate_cache(translations)
    # Validate and serialize EVERYTHING before touching either published file.
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
        # New caches retain old keys, so even a process interruption between replaces
        # leaves the old menu compatible with the expanded cache.
        for name in payloads:
            os.replace(staged[name], directory / name)
            replaced.append(name)
    except OSError:
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
    meals = [meal for day in menu["days"] for meal in day["meals"]]
    return {"days": len(menu["days"]), "meals": len(meals),
            "verified_prices": sum(m["price_status"] == "verified" for m in meals),
            "source_pending_prices": sum(m["price_status"] == "source_pending" for m in meals),
            "translated_meals": sum(m["translation_key"] in translations["entries"] for m in meals),
            "coverage": menu["coverage"], "fetched_at": menu["source"]["fetched_at"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.validate_only:
            menu = read_json(ROOT / "site/data/menu.json")
            cache = read_json(ROOT / "site/data/translations.json")
            validate_menu(menu)
            validate_cache(cache)
            require_notice_coverage(menu, cache)
            report = summarize(menu, cache)
        else:
            report = update()
    except (ValueError, OSError, TypeError, KeyError) as exc:
        # Deliberately no partial write/publish. CI stops here and retains live Pages.
        raise SystemExit(f"Menu update rejected: {exc}") from None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("### Validated menu snapshot\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
