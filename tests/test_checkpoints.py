"""이 테스트 모듈은 번역 재개 기록이 비공개 파일에 저장되는지 확인합니다. 실제 작업 파일을 건드리지 않도록 모든 파일 변경은 테스트 임시 디렉터리에 한정합니다."""

import copy
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from mensa.translation_contract import source_key

try:
    checkpoints = importlib.import_module("mensa.checkpoints")
except ModuleNotFoundError as error:
    if error.name != "mensa.checkpoints":
        raise
    checkpoints = None


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
        self.assertIsNotNone(checkpoints, "TranslationCheckpointStore must provide private persistence")
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.parent = Path(self.scratch.name)
        self.state_dir = self.parent / "private-state"
        self.path = self.state_dir / "translation-checkpoint.json"
        self.store = checkpoints.TranslationCheckpointStore(self.state_dir)

    def make_state_dir(self):
        self.state_dir.mkdir(mode=0o700)

    def write_raw(self, raw):
        self.make_state_dir()
        self.path.write_bytes(raw)

    def test_missing_checkpoint_returns_none_without_creating_directory(self):
        self.assertIsNone(self.store.load())
        self.assertFalse(self.state_dir.exists())

    def test_missing_checkpoint_in_private_directory_returns_none(self):
        self.make_state_dir()
        self.assertIsNone(self.store.load())
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_partial_history_roundtrips_as_strict_utf8_json(self):
        cache = partial_cache()
        self.assertIsNone(self.store.save(cache))
        self.assertEqual(self.store.load(), cache)
        raw = self.path.read_bytes()
        self.assertIn("감자 수프".encode("utf-8"), raw)
        self.assertEqual(json.loads(raw.decode("utf-8")), cache)
        self.assertEqual([p.name for p in self.state_dir.iterdir()], [self.path.name])

    def test_empty_partial_cache_roundtrips(self):
        cache = {"schema_version": 1, "entries": {}}
        self.store.save(cache)
        self.assertEqual(self.store.load(), cache)

    def test_constructor_requires_explicit_absolute_path_without_filesystem_effects(self):
        for invalid in (Path("relative"), Path(""), "relative", None):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "absolute.*Path"):
                    checkpoints.TranslationCheckpointStore(invalid)
        with patch.object(Path, "mkdir", side_effect=AssertionError("constructor touched filesystem")), \
             patch.object(Path, "lstat", side_effect=AssertionError("constructor inspected filesystem")):
            checkpoints.TranslationCheckpointStore(self.state_dir)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_invalid_cache_does_not_create_state_directory(self):
        with self.assertRaisesRegex(ValueError, "cache"):
            self.store.save({"schema_version": 1, "entries": {"bad": {}}})
        self.assertFalse(self.state_dir.exists())

    def test_serialization_failure_does_not_create_state_directory(self):
        for extra in (float("nan"), float("inf"), -float("inf"), object(), "\ud800"):
            with self.subTest(extra=repr(extra)):
                cache = partial_cache()
                cache["history"] = extra
                with self.assertRaisesRegex(ValueError, "serializ|UTF-8|JSON"):
                    self.store.save(cache)
                self.assertFalse(self.state_dir.exists())

    def test_invalid_save_preserves_previous_bytes(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.save({})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])

    def test_save_keeps_caller_cache_unchanged(self):
        cache = partial_cache()
        before = copy.deepcopy(cache)
        self.store.save(cache)
        self.assertEqual(cache, before)
        cache["entries"].clear()
        self.assertEqual(self.store.load(), before)

    def test_load_returns_independent_objects(self):
        self.store.save(partial_cache())
        first = self.store.load()
        second = self.store.load()
        first["entries"].clear()
        self.assertEqual(second, partial_cache())
        self.assertEqual(self.store.load(), partial_cache())

    def test_corrupt_encoding_json_or_cache_has_actionable_error(self):
        self.make_state_dir()
        for raw in (b"\xff", b"{", b"{}", b"[]", b' {"schema_version":1,"entries":{},"history":NaN}',
                    b' {"schema_version":1,"entries":{},"history":Infinity}',
                    b' {"schema_version":1,"entries":{},"history":-Infinity}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "checkpoint.*translation-checkpoint.json"):
                    self.store.load()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_loaded_structurally_invalid_entry_is_rejected(self):
        cache = partial_cache()
        next(iter(cache["entries"].values()))["ko"]["components"] = []
        self.write_raw(json.dumps(cache).encode("utf-8"))
        with self.assertRaisesRegex(ValueError, "checkpoint.*translation-checkpoint.json"):
            self.store.load()

    def test_load_decoder_recursion_error_is_actionable_and_preserves_files(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        marker = self.state_dir / "unrelated"
        marker.write_bytes(b"keep")
        failure = RecursionError("fixture decoder nesting limit")
        with patch.object(checkpoints.json, "loads", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "checkpoint.*translation-checkpoint.json") as raised:
                try:
                    self.store.load()
                except RecursionError as error:
                    self.fail(f"load leaked decoder RecursionError: {error}")
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(set(self.state_dir.iterdir()), {self.path, marker})

    def test_load_serializer_recursion_error_is_actionable_and_preserves_files(self):
        self.store.save({"schema_version": 1, "entries": {}, "history": {"extra": "metadata"}})
        old = self.path.read_bytes()
        marker = self.state_dir / "unrelated"
        marker.write_bytes(b"keep")
        failure = RecursionError("fixture serializer nesting limit")
        with patch.object(checkpoints.json, "dumps", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "checkpoint.*translation-checkpoint.json") as raised:
                try:
                    self.store.load()
                except RecursionError as error:
                    self.fail(f"load leaked serializer RecursionError: {error}")
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(set(self.state_dir.iterdir()), {self.path, marker})

    def test_save_recursion_error_does_not_create_state_directory(self):
        marker = self.parent / "unrelated"
        marker.write_bytes(b"keep")
        cache = {"schema_version": 1, "entries": {}, "history": {"extra": "metadata"}}
        failure = RecursionError("fixture serializer nesting limit")
        with patch.object(checkpoints.json, "dumps", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "checkpoint cache.*UTF-8 JSON") as raised:
                try:
                    self.store.save(cache)
                except RecursionError as error:
                    self.fail(f"save leaked serializer RecursionError: {error}")
        self.assertIs(raised.exception.__cause__, failure)
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(list(self.parent.iterdir()), [marker])

    def test_save_recursion_error_preserves_existing_files(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        marker = self.state_dir / "unrelated"
        marker.write_bytes(b"keep")
        cache = {"schema_version": 1, "entries": {}, "history": {"extra": "metadata"}}
        failure = RecursionError("fixture serializer nesting limit")
        with patch.object(checkpoints.json, "dumps", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "checkpoint cache.*UTF-8 JSON") as raised:
                try:
                    self.store.save(cache)
                except RecursionError as error:
                    self.fail(f"save leaked serializer RecursionError: {error}")
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(set(self.state_dir.iterdir()), {self.path, marker})

    def test_creates_private_leaf_and_private_checkpoint_modes(self):
        with patch.object(checkpoints.os, "replace", wraps=os.replace) as replace:
            self.store.save(partial_cache())
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        temporary, target = replace.call_args.args
        self.assertEqual(Path(temporary).parent, self.state_dir)
        self.assertEqual(Path(target), self.path)

    def test_parent_must_exist_without_creating_ancestors(self):
        missing_parent = self.parent / "absent"
        store = checkpoints.TranslationCheckpointStore(missing_parent / "state")
        with self.assertRaises(FileNotFoundError):
            store.save(partial_cache())
        self.assertFalse(missing_parent.exists())

    def test_existing_accessible_state_directory_is_refused_without_chmod(self):
        self.make_state_dir()
        for mode in (0o710, 0o701, 0o750, 0o755, 0o777):
            with self.subTest(mode=oct(mode)):
                self.state_dir.chmod(mode)
                for operation in (self.store.load, lambda: self.store.save(partial_cache())):
                    with self.assertRaisesRegex(ValueError, "private|permission"):
                        operation()
                self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), mode)
                self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_existing_state_symlink_is_refused_and_target_preserved(self):
        target = self.parent / "target"
        target.mkdir(mode=0o700)
        marker = target / "keep"
        marker.write_bytes(b"unchanged")
        self.state_dir.symlink_to(target, target_is_directory=True)
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "directory|symlink"):
                operation()
        self.assertEqual(marker.read_bytes(), b"unchanged")
        self.assertEqual(list(target.iterdir()), [marker])
        self.assertTrue(self.state_dir.is_symlink())

    def test_existing_state_file_is_refused_and_preserved(self):
        self.state_dir.write_bytes(b"keep")
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "directory"):
                operation()
        self.assertEqual(self.state_dir.read_bytes(), b"keep")

    def test_checkpoint_symlink_is_refused_without_touching_target(self):
        self.make_state_dir()
        target = self.parent / "target"
        target.write_bytes(b"keep")
        self.path.symlink_to(target)
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "regular|symlink"):
                operation()
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])

    def test_dangling_checkpoint_symlink_is_refused(self):
        self.make_state_dir()
        target = self.parent / "absent"
        self.path.symlink_to(target)
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "regular|symlink"):
                operation()
        self.assertFalse(target.exists())
        self.assertTrue(self.path.is_symlink())

    def test_checkpoint_directory_and_fifo_are_refused(self):
        self.make_state_dir()
        self.path.mkdir()
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "regular"):
                operation()
        self.path.rmdir()
        os.mkfifo(self.path)
        for operation in (self.store.load, lambda: self.store.save(partial_cache())):
            with self.assertRaisesRegex(ValueError, "regular"):
                operation()
        self.assertTrue(stat.S_ISFIFO(self.path.lstat().st_mode))

    def test_load_filesystem_error_is_not_silent_empty_cache(self):
        self.store.save(partial_cache())
        with patch.object(Path, "read_bytes", side_effect=PermissionError("fixture read denial")):
            with self.assertRaisesRegex(PermissionError, "fixture read denial"):
                self.store.load()

    def test_write_failure_preserves_old_checkpoint_and_cleans_temporary(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        real_fdopen = os.fdopen

        class FailingWrite:
            def __init__(self, descriptor, mode):
                self.file = real_fdopen(descriptor, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def fileno(self):
                return self.file.fileno()

            def write(self, data):
                self.file.write(data[:5])
                raise OSError("fixture write failure")

        with patch.object(checkpoints.os, "fdopen", side_effect=FailingWrite):
            with self.assertRaisesRegex(OSError, "fixture write failure"):
                self.store.save({"schema_version": 1, "entries": {}})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])

    def test_fdopen_failure_closes_descriptor_and_preserves_old_checkpoint(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        descriptors = []

        def fail_open(descriptor, mode):
            descriptors.append(descriptor)
            raise OSError("fixture fdopen failure")

        with patch.object(checkpoints.os, "fdopen", side_effect=fail_open):
            with self.assertRaisesRegex(OSError, "fixture fdopen failure"):
                self.store.save({"schema_version": 1, "entries": {}})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])
        try:
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
        finally:
            try:
                os.close(descriptors[0])
            except OSError:
                pass

    def test_fsync_failure_preserves_old_checkpoint_and_cleans_temporary(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        with patch.object(checkpoints.os, "fsync", side_effect=OSError("fixture fsync failure")):
            with self.assertRaisesRegex(OSError, "fixture fsync failure"):
                self.store.save({"schema_version": 1, "entries": {}})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])

    def test_replace_failure_preserves_old_checkpoint_and_cleans_temporary(self):
        self.store.save(partial_cache())
        old = self.path.read_bytes()
        with patch.object(checkpoints.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaisesRegex(OSError, "fixture replace failure"):
                self.store.save({"schema_version": 1, "entries": {}})
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(self.state_dir.iterdir()), [self.path])

    def test_unrelated_files_and_parent_mode_are_preserved(self):
        self.make_state_dir()
        private_marker = self.state_dir / "unrelated"
        parent_marker = self.parent / "unrelated"
        private_marker.write_bytes(b"private keep")
        parent_marker.write_bytes(b"parent keep")
        parent_mode = stat.S_IMODE(self.parent.stat().st_mode)
        self.store.save(partial_cache())
        self.store.save({"schema_version": 1, "entries": {}})
        self.assertEqual(private_marker.read_bytes(), b"private keep")
        self.assertEqual(parent_marker.read_bytes(), b"parent keep")
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), parent_mode)
        self.assertEqual(set(self.state_dir.iterdir()), {private_marker, self.path})


if __name__ == "__main__":
    unittest.main()
