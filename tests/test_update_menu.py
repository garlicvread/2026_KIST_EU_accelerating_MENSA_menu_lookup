import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.update_menu import write_snapshot, require_current_coverage, update, main
from scripts.local_refresh import RefreshJob
from scripts.translations import build_translations
from scripts.menu_source import parse_menu
from test_menu_source import page, meal


class SnapshotTests(unittest.TestCase):
    def test_unknown_notice_during_update_preserves_both_published_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / "site" / "data"
            previous = parse_menu(page(date="21.09.2099"))
            cache = build_translations(previous, {}, {}, None)
            write_snapshot(directory, previous, cache)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            changed = page(date="21.09.2099").replace("Weizen", "New unreviewed source label")
            with self.assertRaisesRegex(ValueError, "New unreviewed source label"):
                update(root, html=changed)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_validate_only_requires_current_notice_coverage(self):
        menu = parse_menu(page())
        with patch("scripts.update_menu.read_json", side_effect=[menu, {"schema_version": 1, "entries": {}}]), \
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

    def test_worker_rejects_notice_wording_that_bypasses_the_reviewed_glossary(self):
        menu = parse_menu(page())
        phrases = {name: {"en": "Dish", "ko": "요리"} for name in
                   ["Vegan: Ägyptisches Kushari", "Reis", "Soße & Gemüse"]}
        cache = build_translations(menu, {}, phrases, None)
        RefreshJob.validate_complete(menu, cache)
        cache["notices"]["Weizen"]["en"] = "Wheat-free"
        with self.assertRaisesRegex(ValueError, "[Nn]otice.*glossary"):
            RefreshJob.validate_complete(menu, cache)

    def test_price_loss_during_refresh_preserves_published_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / "site" / "data"
            previous = parse_menu(page())
            cache = {"schema_version": 1, "entries": {}}
            write_snapshot(directory, previous, cache)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            with self.assertRaisesRegex(ValueError, "price|Price"):
                update(root, html=page(meal(prices="")))
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_second_replace_failure_restores_both_snapshot_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            menu = parse_menu(page())
            cache = {"schema_version": 1, "entries": {}}
            write_snapshot(directory, menu, cache)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
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
                    write_snapshot(directory, menu, cache)
            self.assertEqual(len(calls), 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_failed_validation_leaves_both_files_byte_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "menu.json").write_text('{"old": "menu"}')
            (root / "translations.json").write_text('{"old": "translations"}')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with self.assertRaises(ValueError):
                write_snapshot(root, {"days": []}, {"entries": {}}, previous=None)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_expired_source_cannot_be_published_as_a_fresh_fetch(self):
        with self.assertRaises(ValueError):
            require_current_coverage({"coverage": {"start": "2026-09-14", "end": "2026-09-18"}}, "2026-09-21")
        require_current_coverage({"coverage": {"start": "2026-09-21", "end": "2026-10-02"}}, "2026-09-21")


if __name__ == "__main__":
    unittest.main()
