import itertools
import os
from pathlib import Path
import tempfile
import unittest

from mensa.config import WorkerPaths, load_worker_paths


class WorkerPathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.config_path = self.config_dir / "worker.toml"
        self.values = {
            "checkout_dir": "../checkout",
            "state_dir": "../state",
            "public_dir": "../public",
        }

    def write_config(self, values=None, extra=""):
        values = self.values if values is None else values
        lines = ["[paths]"]
        for key, value in values.items():
            # 이 테스트의 경로 예시는 TOML에서 이스케이프 처리가 필요한 문자를 포함하지 않으므로 경로를 그대로 설정 문자열에 넣습니다.
            lines.append(f'{key} = "{value}"')
        self.config_path.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")

    def load_valid(self):
        result = load_worker_paths(self.config_path)
        self.assertIsInstance(result, WorkerPaths)
        for key in self.values:
            self.assertIsInstance(getattr(result, key), Path)
        return result

    def assert_invalid(self, message):
        with self.assertRaisesRegex(ValueError, message):
            load_worker_paths(self.config_path)

    def test_relative_paths_are_resolved_from_config_directory(self):
        self.write_config()
        paths = self.load_valid()
        self.assertEqual(paths.checkout_dir, self.root / "checkout")
        self.assertEqual(paths.state_dir, self.root / "state")
        self.assertEqual(paths.public_dir, self.root / "public")

    def test_current_directory_does_not_change_resolved_paths(self):
        self.write_config()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        original_cwd = Path.cwd()
        try:
            os.chdir(elsewhere)
            paths = self.load_valid()
        finally:
            os.chdir(original_cwd)
        self.assertEqual(paths.checkout_dir, self.root / "checkout")
        self.assertEqual(paths.state_dir, self.root / "state")
        self.assertEqual(paths.public_dir, self.root / "public")

    def test_absolute_paths_are_preserved(self):
        self.write_config({key: str(self.root / key) for key in self.values})
        paths = self.load_valid()
        for key in self.values:
            self.assertEqual(getattr(paths, key), self.root / key)

    def test_loading_allows_absent_directories_without_creating_them(self):
        self.write_config()
        before = set(self.root.rglob("*"))
        paths = self.load_valid()
        for key in self.values:
            self.assertFalse(getattr(paths, key).exists())
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_existing_sibling_directories_are_allowed(self):
        for name in ("checkout", "state", "public"):
            (self.root / name).mkdir()
        self.write_config()
        self.load_valid()


    def test_missing_path_keys_are_rejected(self):
        for missing in self.values:
            with self.subTest(missing=missing):
                self.write_config({key: value for key, value in self.values.items() if key != missing})
                self.assert_invalid(missing)

    def test_unknown_path_keys_are_rejected(self):
        self.write_config({**self.values, "cache_dir": "../cache"})
        self.assert_invalid("cache_dir")

    def test_exactly_one_paths_table_is_required(self):
        cases = {
            "missing": "",
            "wrong_table": '[other]\ncheckout_dir = "../checkout"\n',
            "scalar": 'paths = "../checkout"\n',
            "array": "paths = []\n",
            "array_of_tables": '[[paths]]\ncheckout_dir = "../checkout"\n',
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.config_path.write_text(content, encoding="utf-8")
                self.assert_invalid("paths")

    def test_extra_root_keys_and_tables_are_rejected(self):
        cases = {
            "root_key": 'enabled = true\n',
            "table": '[worker]\nenabled = true\n',
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.write_config()
                original = self.config_path.read_text(encoding="utf-8")
                self.config_path.write_text(content + original, encoding="utf-8")
                self.assert_invalid("enabled|worker")

    def test_nested_paths_tables_are_rejected(self):
        self.write_config(extra='[paths.extra]\nlocation = "../extra"\n')
        self.assert_invalid("extra")

    def test_non_string_path_values_are_rejected(self):
        for key in self.values:
            for value in ("1", "1.5", "true", "[]", "{}", "2026-09-30"):
                with self.subTest(key=key, value=value):
                    self.write_config()
                    content = self.config_path.read_text(encoding="utf-8")
                    content = content.replace(f'{key} = "{self.values[key]}"', f"{key} = {value}")
                    self.config_path.write_text(content, encoding="utf-8")
                    self.assert_invalid(key + ".*string")

    def test_empty_and_whitespace_only_path_values_are_rejected(self):
        for key in self.values:
            for value in ("", "   "):
                with self.subTest(key=key, value=value):
                    self.write_config({**self.values, key: value})
                    self.assert_invalid(key + ".*(empty|nonempty|non-empty)")

    def test_invalid_toml_is_reported_with_config_context(self):
        for content in ("[paths", '[paths]\ncheckout_dir = "unterminated', "[paths]\n[paths]\n"):
            with self.subTest(content=content):
                self.config_path.write_text(content, encoding="utf-8")
                self.assert_invalid("worker.toml.*TOML")

    def test_every_parent_child_direction_is_rejected(self):
        for parent_key, child_key in itertools.permutations(self.values, 2):
            with self.subTest(parent=parent_key, child=child_key):
                self.write_config({**self.values, parent_key: "../shared", child_key: "../shared/child"})
                self.assert_invalid(f"({parent_key}.*{child_key}|{child_key}.*{parent_key}).*overlap")

    def test_equal_directory_paths_are_rejected_for_every_pair(self):
        for first, second in itertools.combinations(self.values, 2):
            with self.subTest(first=first, second=second):
                self.write_config({**self.values, first: "../shared", second: "../shared"})
                self.assert_invalid(f"{first}.*{second}.*overlap")

    def test_lexical_prefixes_are_not_directory_containment(self):
        self.write_config({"checkout_dir": "../tree", "state_dir": "../tree-state", "public_dir": "../tree-public"})
        self.load_valid()

    def test_existing_symlink_is_resolved_in_returned_path(self):
        target = self.root / "real-checkout"
        target.mkdir()
        (self.root / "checkout").symlink_to(target, target_is_directory=True)
        self.write_config()
        self.assertEqual(self.load_valid().checkout_dir, target)

    def test_symlink_equality_and_containment_are_rejected(self):
        target = self.root / "real"
        target.mkdir()
        (self.root / "alias").symlink_to(target, target_is_directory=True)
        for suffix in ("", "/absent-child"):
            with self.subTest(suffix=suffix):
                self.write_config({**self.values, "checkout_dir": "../real", "state_dir": "../alias" + suffix})
                self.assert_invalid("checkout_dir.*state_dir.*overlap")

    def test_existing_regular_files_are_rejected_for_every_path_key(self):
        file_path = self.root / "file"
        file_path.write_text("existing file", encoding="utf-8")
        for key in self.values:
            with self.subTest(key=key):
                self.write_config({**self.values, key: str(file_path)})
                self.assert_invalid(key + ".*(directory|file)")

    def test_symlink_to_regular_file_is_rejected(self):
        file_path = self.root / "file"
        file_path.write_text("existing file", encoding="utf-8")
        (self.root / "alias").symlink_to(file_path)
        self.write_config({**self.values, "checkout_dir": "../alias"})
        self.assert_invalid("checkout_dir.*(directory|file)")

    def test_home_and_environment_syntax_are_literal_path_components(self):
        self.write_config({"checkout_dir": "~/checkout", "state_dir": "$HOME/state", "public_dir": "${HOME}/public"})
        paths = self.load_valid()
        self.assertEqual(paths.checkout_dir, self.config_dir / "~" / "checkout")
        self.assertEqual(paths.state_dir, self.config_dir / "$HOME" / "state")
        self.assertEqual(paths.public_dir, self.config_dir / "${HOME}" / "public")


if __name__ == "__main__":
    unittest.main()
