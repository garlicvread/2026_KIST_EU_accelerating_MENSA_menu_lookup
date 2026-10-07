import os
from pathlib import Path
import tempfile
import unittest

from mensa.config import load_worker_paths


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

    def write_config(self, values=None):
        values = self.values if values is None else values
        lines = ["[paths]"]
        for key, value in values.items():
            # 이 테스트의 경로 예시는 TOML에서 이스케이프 처리가 필요한 문자를 포함하지 않으므로 경로를 그대로 설정 문자열에 넣습니다.
            lines.append(f'{key} = "{value}"')
        self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def load_valid(self):
        return load_worker_paths(self.config_path)

    def assert_invalid(self, message):
        with self.assertRaisesRegex(ValueError, message):
            load_worker_paths(self.config_path)


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


    def test_unknown_path_keys_are_rejected(self):
        self.write_config({**self.values, "cache_dir": "../cache"})
        self.assert_invalid("cache_dir")


    def test_state_directory_cannot_contain_public_output(self):
        self.write_config({**self.values, "state_dir": "../shared", "public_dir": "../shared/child"})
        self.assert_invalid("state_dir.*public_dir.*overlap")


    def test_state_directory_rejects_existing_regular_file(self):
        file_path = self.root / "file"
        file_path.write_text("existing file", encoding="utf-8")
        self.write_config({**self.values, "state_dir": str(file_path)})
        self.assert_invalid("state_dir.*(directory|file)")


if __name__ == "__main__":
    unittest.main()
