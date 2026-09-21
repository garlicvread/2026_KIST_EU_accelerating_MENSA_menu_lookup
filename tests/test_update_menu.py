import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.update_menu import write_snapshot, require_current_coverage, update
from scripts.menu_source import parse_menu
from test_menu_source import page, meal


class SnapshotTests(unittest.TestCase):
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
