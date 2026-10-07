"""이 테스트 모듈은 실제 파일로 저장한 메뉴와 번역 게시본 쌍을 검사합니다. 저장한 게시본을 바꾸지 않고 현재 게시본을 한 번에 선택하는지 확인합니다."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mensa.translation_contract import source_for
from scripts.menu_source import parse_menu
from test_menu_source import page
from test_publication import translation_entry

from mensa import releases as releases


class DirectoryPublisherTests(unittest.TestCase):
    def setUp(self):
        self.publisher_type = releases.DirectoryPublisher
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.key = record['translation_key']
        self.glossary = {
            'vegan': {'en': 'Vegan', 'ko': '비건'},
            'Weizen': {'en': 'Wheat', 'ko': '밀'},
            'Sellerie': {'en': 'Celery', 'ko': '셀러리'},
            'Milch und Laktose': {'en': 'Milk and lactose', 'ko': '우유 및 유당'},
        }
        self.cache = {'schema_version': 1, 'entries': {self.key: translation_entry(source_for(record))},
                      'notices': copy.deepcopy(self.glossary)}

    def publisher(self, root, glossary=None):
        return self.publisher_type(root, notice_glossary=self.glossary if glossary is None else glossary)

    def changed_pair(self):
        menu, cache = copy.deepcopy(self.menu), copy.deepcopy(self.cache)
        menu['source']['fetched_at'] = '2026-09-21T11:00:00Z'
        cache['entries'][self.key]['en']['name'] = 'New reviewed dish'
        cache['entries'][self.key]['ko']['name'] = '새 검토 요리'
        return menu, cache

    def test_complete_pair_and_reader_remain_pinned_across_pointer_switch(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            private = Path(folder) / 'private'
            private.mkdir(); (private / 'checkpoint.json').write_text('Private history')
            publisher = self.publisher(root)
            expected = (copy.deepcopy(self.menu), copy.deepcopy(self.cache))
            first = publisher.publish(*expected)
            self.assertRegex(first, r'^[0-9a-f]{64}$')
            pointer = root / 'data/current.json'
            first_pointer = pointer.read_bytes()
            self.assertEqual(json.loads(first_pointer), {'schema_version': 1, 'release_id': first})
            pair_dir = root / 'data/releases' / first
            self.assertEqual(json.loads((pair_dir / 'menu.json').read_bytes()), expected[0])
            self.assertEqual(json.loads((pair_dir / 'translations.json').read_bytes()), expected[1])
            self.assertEqual(sorted(path.name for path in pair_dir.iterdir()), ['menu.json', 'translations.json'])
            self.assertEqual(publisher.load_current(), expected)
            changed = self.changed_pair()
            second = publisher.publish(*changed)
            second_pointer = pointer.read_bytes()
            pointer.write_bytes(first_pointer)
            actual_read = Path.read_bytes
            def switch_selection(path):
                if path == pair_dir / 'menu.json':
                    pointer.write_bytes(second_pointer)
                return actual_read(path)
            with patch.object(Path, 'read_bytes', switch_selection):
                self.assertEqual(publisher.load_current(), expected)
            self.assertEqual(publisher.load_current(), changed)
            self.assertNotEqual(first, second)
            self.assertEqual((private / 'checkpoint.json').read_text(), 'Private history')
            self.assertEqual(sorted(path.name for path in (root / 'data').iterdir()), ['current.json', 'releases'])

    def test_identical_retry_and_dictionary_order_preserve_selection_and_older_releases(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'; publisher = self.publisher(root)
            first = publisher.publish(self.menu, self.cache)
            pointer = root / 'data/current.json'
            before = (pointer.read_bytes(), pointer.stat().st_ino, pointer.stat().st_mtime_ns)
            def reordered(value):
                if isinstance(value, dict): return {key: reordered(value[key]) for key in reversed(value)}
                if isinstance(value, list): return [reordered(item) for item in value]
                return value
            self.assertEqual(publisher.publish(reordered(self.menu), reordered(self.cache)), first)
            self.assertEqual((pointer.read_bytes(), pointer.stat().st_ino, pointer.stat().st_mtime_ns), before)
            second = publisher.publish(*self.changed_pair())
            self.assertNotEqual(first, second)
            self.assertTrue((root / 'data/releases' / first / 'translations.json').is_file())
            loaded = publisher.load_current(); loaded[1]['entries'].clear()
            self.assertEqual(publisher.load_current(), self.changed_pair())

    def test_stored_notices_remain_readable_and_new_publication_uses_copied_glossary(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'; old = self.publisher(root)
            old.publish(self.menu, self.cache)
            changed = copy.deepcopy(self.glossary); changed['vegan']['en'] = 'Reviewed vegan wording'
            publisher = self.publisher(root, changed)
            changed['vegan']['en'] = 'Caller mutation'
            self.assertEqual(publisher.load_current(), (self.menu, self.cache))
            with self.assertRaises(ValueError): publisher.publish(self.menu, self.cache)
            updated = copy.deepcopy(self.cache); updated['notices']['vegan']['en'] = 'Reviewed vegan wording'
            publisher.publish(self.menu, updated)
            self.assertEqual(publisher.load_current(), (self.menu, updated))
            self.assertEqual(old.load_current(), (self.menu, updated))

    def test_interrupted_pair_or_selection_keeps_old_pair_and_retries_without_temps(self):
        for boundary in ('pair', 'pointer'):
            with self.subTest(boundary=boundary), TemporaryDirectory() as folder:
                root = Path(folder) / 'public'; publisher = self.publisher(root)
                first = publisher.publish(self.menu, self.cache)
                pointer = root / 'data/current.json'; original = pointer.read_bytes()
                changed = self.changed_pair()
                actual_open, actual_replace = Path.open, os.replace
                def fail_pair(path, mode='r', *args, **kwargs):
                    if path.name == 'translations.json' and ('w' in mode or 'x' in mode):
                        raise OSError('Interrupted pair preparation')
                    return actual_open(path, mode, *args, **kwargs)
                def fail_pointer(source, target):
                    if Path(target) == pointer: raise OSError('Interrupted current selection')
                    return actual_replace(source, target)
                port = patch.object(Path, 'open', fail_pair) if boundary == 'pair' else patch.object(os, 'replace', fail_pointer)
                with port, self.assertRaisesRegex(OSError, 'Interrupted'):
                    publisher.publish(*changed)
                self.assertEqual(pointer.read_bytes(), original)
                self.assertEqual(publisher.load_current(), (self.menu, self.cache))
                ready = {path.name for path in (root / 'data/releases').iterdir()}
                self.assertEqual(len(ready), 1 if boundary == 'pair' else 2)
                self.assertIn(first, ready)
                self.assertTrue(all(len(name) == 64 for name in ready))
                self.assertEqual(sorted(path.name for path in (root / 'data').iterdir()), ['current.json', 'releases'])
                accepted = publisher.publish(*changed)
                self.assertEqual(publisher.load_current(), changed)
                self.assertEqual({path.name for path in (root / 'data/releases').iterdir()}, {first, accepted})

    def test_incomplete_or_stale_input_fails_before_output_creation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'; publisher = self.publisher(root)
            incomplete = copy.deepcopy(self.cache); incomplete['entries'].clear()
            with self.assertRaises(ValueError): publisher.publish(self.menu, incomplete)
            self.assertFalse(root.exists())
            with self.assertRaises(ValueError): publisher.publish(self.menu, self.cache, today='2026-09-22')
            self.assertFalse(root.exists())
            publisher.publish(self.menu, self.cache, today='2026-09-21')
            self.assertEqual(publisher.load_current(), (self.menu, self.cache))

    def test_selected_corruption_and_pointer_paths_fail_without_overwrite(self):
        for bad in ('digest', 'schema', 'pointer-symlink', 'release-symlink', 'data-symlink', 'missing', 'corrupt', 'hashed-incomplete'):
            with self.subTest(bad=bad), TemporaryDirectory() as folder:
                root = Path(folder) / 'public'; publisher = self.publisher(root)
                release_id = publisher.publish(self.menu, self.cache)
                pointer = root / 'data/current.json'; selected = root / 'data/releases' / release_id
                payload = selected / 'translations.json'
                if bad == 'digest': pointer.write_text(json.dumps({'schema_version': 1, 'release_id': '../private'}))
                elif bad == 'schema': pointer.write_text(json.dumps({'schema_version': True, 'release_id': release_id}))
                elif bad == 'pointer-symlink':
                    saved = Path(folder) / 'saved-pointer'; saved.write_bytes(pointer.read_bytes()); pointer.unlink(); pointer.symlink_to(saved)
                elif bad == 'release-symlink':
                    saved = Path(folder) / 'saved-release'; selected.rename(saved); selected.symlink_to(saved, target_is_directory=True)
                elif bad == 'data-symlink':
                    saved = Path(folder) / 'saved-cache'; saved.write_bytes(payload.read_bytes()); payload.unlink(); payload.symlink_to(saved)
                elif bad == 'missing': payload.unlink()
                elif bad == 'corrupt': payload.write_text('{}')
                else:
                    incomplete = copy.deepcopy(self.cache); incomplete['entries'].clear()
                    menu_bytes = (selected / 'menu.json').read_bytes()
                    cache_bytes = json.dumps(incomplete, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
                    # 이 테스트는 내부 해시가 맞아도 내용이 불완전한 게시본을 거부하는지 확인하도록 해당 외부 게시본 파일을 만듭니다.
                    digest = hashlib.sha256(len(menu_bytes).to_bytes(8, 'big') + menu_bytes + len(cache_bytes).to_bytes(8, 'big') + cache_bytes).hexdigest()
                    forged = root / 'data/releases' / digest; forged.mkdir()
                    (forged / 'menu.json').write_bytes(menu_bytes); (forged / 'translations.json').write_bytes(cache_bytes)
                    pointer.write_text(json.dumps({'schema_version': 1, 'release_id': digest}))
                original = pointer.read_bytes()
                with self.assertRaises(ValueError): publisher.load_current()
                with self.assertRaises(ValueError): publisher.publish(self.menu, self.cache)
                self.assertEqual(pointer.read_bytes(), original)
                if bad == 'corrupt': self.assertEqual(payload.read_text(), '{}')

    def test_existing_directory_shapes_and_same_id_corruption_are_not_repaired(self):
        for leaf in ('root', 'data', 'releases', 'selected', 'pair-directory'):
            with self.subTest(leaf=leaf), TemporaryDirectory() as folder:
                root = Path(folder) / 'public'; publisher = self.publisher(root)
                if leaf in ('selected', 'pair-directory'):
                    release_id = publisher.publish(self.menu, self.cache)
                    selected = root / 'data/releases' / release_id
                    if leaf == 'selected':
                        for child in selected.iterdir(): child.unlink()
                        selected.rmdir(); selected.write_text('Unknown directory replacement')
                        bad = selected
                    else:
                        bad = selected / 'translations.json'; bad.unlink(); bad.mkdir()
                else:
                    bad = {'root': root, 'data': root / 'data', 'releases': root / 'data/releases'}[leaf]
                    bad.parent.mkdir(parents=True, exist_ok=True); bad.write_text('Unknown path')
                before = bad.read_bytes() if bad.is_file() else sorted(p.name for p in bad.iterdir())
                with self.assertRaises(ValueError): publisher.publish(self.menu, self.cache)
                with self.assertRaises(ValueError): publisher.load_current()
                self.assertEqual(bad.read_bytes() if bad.is_file() else sorted(p.name for p in bad.iterdir()), before)

    def test_constructor_and_absent_reader_do_not_create_paths(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            with (patch.object(Path, 'lstat', side_effect=AssertionError('Constructor filesystem lookup')),
                  patch.object(Path, 'mkdir', side_effect=AssertionError('Constructor creation')),
                  patch.object(Path, 'read_bytes', side_effect=AssertionError('Constructor read'))):
                publisher = self.publisher(root)
                for invalid in (Path('relative'), str(root)):
                    with self.assertRaises(ValueError): self.publisher(invalid)
                with self.assertRaises(ValueError): self.publisher(root, {'invalid': {'en': 'Missing Korean'}})
            self.assertIsNone(publisher.load_current())
            self.assertFalse(root.exists())
            missing_parent = Path(folder) / 'absent/public'
            with self.assertRaises(ValueError): self.publisher(missing_parent).publish(self.menu, self.cache)
            self.assertFalse(missing_parent.parent.exists())

    def test_new_public_paths_are_readable_under_restrictive_umask(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            before = os.umask(0o077)
            try: self.publisher(root).publish(self.menu, self.cache)
            finally: os.umask(before)
            for path in (root, *root.rglob('*')):
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertEqual(mode & (0o555 if path.is_dir() else 0o444), 0o555 if path.is_dir() else 0o444)
            self.assertEqual(self.publisher(root).load_current(), (self.menu, self.cache))

    def test_real_permission_failure_preserves_old_selection(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'; publisher = self.publisher(root)
            publisher.publish(self.menu, self.cache)
            pointer = root / 'data/current.json'; original = pointer.read_bytes()
            directory = root / 'data/releases'; directory.chmod(0o555)
            try:
                if os.access(directory, os.W_OK): self.skipTest('Current account bypasses directory write permissions')
                with self.assertRaises(PermissionError): publisher.publish(*self.changed_pair())
                self.assertEqual(pointer.read_bytes(), original)
                self.assertEqual(publisher.load_current(), (self.menu, self.cache))
            finally: directory.chmod(0o755)


if __name__ == '__main__':
    unittest.main()
