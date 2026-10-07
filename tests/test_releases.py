"""이 테스트 모듈은 실제 파일로 저장한 메뉴와 번역 게시본 쌍을 검사합니다. 저장한 게시본을 바꾸지 않고 현재 게시본을 한 번에 선택하는지 확인합니다."""

import copy
import json
import os
from pathlib import Path
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
            pointer = root / 'data/current.json'
            first_pointer = pointer.read_bytes()
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


    def test_stored_notices_remain_readable_and_new_publication_uses_copied_glossary(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'; old = self.publisher(root)
            old.publish(self.menu, self.cache)
            changed = copy.deepcopy(self.glossary); changed['vegan']['en'] = 'Reviewed vegan wording'
            publisher = self.publisher(root, changed)
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

    def test_corrupt_selected_pair_is_refused_without_overwrite(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            publisher = self.publisher(root)
            release_id = publisher.publish(self.menu, self.cache)
            pointer = root / 'data/current.json'
            payload = root / 'data/releases' / release_id / 'translations.json'
            payload.write_text('{}')
            original = pointer.read_bytes()
            with self.assertRaises(ValueError):
                publisher.load_current()
            with self.assertRaises(ValueError):
                publisher.publish(self.menu, self.cache)
            self.assertEqual(pointer.read_bytes(), original)
            self.assertEqual(payload.read_text(), '{}')


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
