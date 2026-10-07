import copy
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.update_menu import write_snapshot, require_current_coverage, update, main
from scripts.local_refresh import RefreshJob
from scripts.translations import build_translations
from scripts.menu_source import parse_menu
from test_menu_source import page, meal


def complete_cache(menu):
    texts = {text for day in menu["days"] for record in day["meals"]
             for text in [record["name_de"], *[c["name_de"] for c in record["components"]]]}
    phrases = {text: {"en": "Dish", "ko": "요리"} for text in texts}
    return build_translations(menu, {}, phrases, None)


class SnapshotTests(unittest.TestCase):

    def test_update_keeps_published_earlier_days_across_successive_collections(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / 'site/data'
            previous = parse_menu(page(date='21.09.2099'))
            write_snapshot(directory, previous, complete_cache(previous))
            for source_date, expected_dates in (
                ('22.09.2099', ['2099-09-21', '2099-09-22']),
                ('23.09.2099', ['2099-09-21', '2099-09-22', '2099-09-23']),
            ):
                report = update(root, html=page(date=source_date))
                stored = json.loads((directory / 'menu.json').read_text())
                self.assertEqual([day['date'] for day in stored['days']], expected_dates)
                self.assertEqual(stored['days'][0], previous['days'][0])
                self.assertEqual(report['days'], len(expected_dates))

    def test_validate_only_rejects_missing_second_current_meal_translation(self):
        menu = parse_menu(page(meal() + meal(name="Second dish")))
        cache = complete_cache(menu)
        second = menu["days"][0]["meals"][1]
        del cache["entries"][second["translation_key"]]
        with patch("scripts.update_menu.read_json", side_effect=[menu, cache]), \
                patch("sys.argv", ["update_menu", "--validate-only"]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "Missing meal translation.*Second dish"):
                main()

    def test_incomplete_meal_cache_preserves_both_files_without_staging(self):
        menu = parse_menu(page())
        cache = complete_cache(menu)
        cache["entries"].clear()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "menu.json").write_text('{"old": "menu"}')
            (directory / "translations.json").write_text('{"old": "translations"}')
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            with self.assertRaisesRegex(ValueError, "Missing meal translation"):
                write_snapshot(directory, menu, cache)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
            absent = directory / "uncreated"
            with self.assertRaisesRegex(ValueError, "Missing meal translation"):
                write_snapshot(absent, menu, cache)
            self.assertFalse(absent.exists())

    def test_unknown_notice_during_update_preserves_both_published_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / "site" / "data"
            previous = parse_menu(page(date="21.09.2099"))
            cache = complete_cache(previous)
            write_snapshot(directory, previous, cache)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            changed = page(date="21.09.2099").replace("Weizen", "New unreviewed source label")
            with self.assertRaisesRegex(ValueError, "New unreviewed source label"):
                update(root, html=changed)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_validate_only_requires_current_notice_coverage(self):
        menu = parse_menu(page())
        cache = complete_cache(menu)
        cache["notices"].pop("Weizen")
        with patch("scripts.update_menu.read_json", side_effect=[menu, cache]), \
                patch("sys.argv", ["update_menu", "--validate-only"]):
            with self.assertRaisesRegex(SystemExit, "[Nn]otice.*[Ww]eizen|[Nn]otice.*[Ss]ellerie"):
                main()

    def test_worker_requires_current_notice_coverage_independent_of_dish_cache(self):
        menu = parse_menu(page())
        phrases = {name: {"en": "Dish", "ko": "요리"} for name in
                   ["Vegan: Ägyptisches Kushari", "Reis", "Soße & Gemüse"]}
        cache = build_translations(menu, {}, phrases, None)
        cache.pop("notices", None)
        with self.assertRaisesRegex(ValueError, "[Nn]otice"):
            RefreshJob.validate_complete(menu, cache)


    def test_price_loss_during_refresh_preserves_published_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / "site" / "data"
            previous = parse_menu(page())
            cache = complete_cache(previous)
            write_snapshot(directory, previous, cache)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            with self.assertRaisesRegex(ValueError, "price|Price"):
                update(root, html=page(meal(prices="")))
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_second_replace_failure_restores_both_snapshot_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            menu = parse_menu(page())
            cache = complete_cache(menu)
            write_snapshot(directory, menu, cache)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            # 이 테스트가 복구 누락을 확인하려면 앞서 교체한 번역 내용도 달라야 합니다.
            next_menu = copy.deepcopy(menu)
            next_menu["source"]["fetched_at"] = "2026-10-07T12:00:00Z"
            next_cache = copy.deepcopy(cache)
            next(iter(next_cache["entries"].values()))["en"]["name"] += " (updated)"
            import os
            replace = os.replace
            calls = []

            def fail_second(source, target):
                calls.append(target)
                if len(calls) == 2:
                    raise OSError("injected storage failure")
                return replace(source, target)

            with patch("scripts.update_menu.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    write_snapshot(directory, next_menu, next_cache)
            self.assertEqual(len(calls), 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})


    def test_expired_source_cannot_be_published_as_a_fresh_fetch(self):
        with self.assertRaises(ValueError):
            require_current_coverage({"coverage": {"start": "2026-09-14", "end": "2026-09-18"}}, "2026-09-21")
        require_current_coverage({"coverage": {"start": "2026-09-21", "end": "2026-10-02"}}, "2026-09-21")


if __name__ == "__main__":
    unittest.main()
