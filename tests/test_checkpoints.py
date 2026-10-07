"""이 테스트 모듈은 번역 재개 기록이 비공개 파일에 저장되는지 확인합니다. 실제 작업 파일을 건드리지 않도록 모든 파일 변경은 테스트 임시 디렉터리에 한정합니다."""

from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from mensa import checkpoints
from mensa.translation_contract import source_key


def partial_cache():
    source = {"name_de": "Kartoffelsuppe", "components": ["Brot"]}
    entry = {
        "source": source,
        "en": {"name": "Potato soup", "components": ["Bread"]},
        "ko": {"name": "감자 수프", "components": ["빵"]},
        "origin": "model",
        "model": "fixture-model",
        "prompt_version": "menu-v4",
    }
    return {"schema_version": 1, "entries": {source_key(source): entry}}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.parent = Path(self.scratch.name)
        self.state_dir = self.parent / "private-state"
        self.path = self.state_dir / "translation-checkpoint.json"
        self.store = checkpoints.TranslationCheckpointStore(self.state_dir)

    def make_state_dir(self):
        self.state_dir.mkdir(mode=0o700)


    def test_invalid_save_preserves_previous_bytes(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.save({})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])


    def test_corrupt_json_or_cache_has_actionable_error(self):
        self.make_state_dir()
        for raw in (b"{", b"{}"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "checkpoint.*translation-checkpoint.json"):
                    self.store.load()
                self.assertEqual(self.path.read_bytes(), raw)


    def test_creates_private_leaf_and_private_checkpoint_modes(self):
        self.store.save(partial_cache())
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


    def test_existing_publicly_accessible_state_directory_is_refused_without_chmod(self):
        self.make_state_dir()
        self.state_dir.chmod(0o755)
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "private|permission"):
                operation()
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o755)
        self.assertEqual(list(self.state_dir.iterdir()), [])


    def test_replace_failure_preserves_old_checkpoint_and_cleans_temporary(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        with patch.object(checkpoints.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaisesRegex(OSError, "fixture replace failure"):
                self.store.save({"schema_version": 1, "entries": {}})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
