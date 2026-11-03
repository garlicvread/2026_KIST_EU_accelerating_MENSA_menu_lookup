"""운영용 복구 읽기가 파생 번역만 격리하고 원래 게시 ID와 메뉴 검사를 보존하는지 확인합니다."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mensa.drive_publication import DrivePublisher, read_publication
from mensa.releases import DirectoryPublisher, _json_bytes, _release_id
from scripts.menu_source import parse_menu
from test_drive_publication import FakeRclone, REMOTE, FOLDER_ID, FILE_ID, envelope
from test_menu_source import page
from test_publication import translation_entry
from mensa.translation_contract import source_for


class RecoveryReaderTests(unittest.TestCase):
    def setUp(self):
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.key = record['translation_key']
        self.cache = {'schema_version': 1,
                      'entries': {self.key: translation_entry(source_for(record))}, 'notices': {}}

    def damaged(self, kind):
        cache = copy.deepcopy(self.cache)
        if kind == 'missing-language':
            del cache['entries'][self.key]['ko']
        elif kind == 'wrong-source':
            cache['entries'][self.key]['source']['name_de'] = 'Unrelated source'
        elif kind == 'notice':
            cache['notices']['Weizen'] = {'en': 'Wheat'}
        else:
            raise AssertionError(kind)
        return cache

    def seed_directory(self, root, menu, cache):
        payloads = (_json_bytes(menu), _json_bytes(cache))
        identifier = _release_id(*payloads)
        directory = root / 'data/releases' / identifier
        directory.mkdir(parents=True)
        for name, payload in zip(('menu.json', 'translations.json'), payloads):
            (directory / name).write_bytes(payload)
        (root / 'data/current.json').write_bytes(_json_bytes({'schema_version': 1, 'release_id': identifier}))
        return identifier

    def test_opt_in_envelope_reader_returns_raw_pair_without_aliasing_its_identity(self):
        for kind in ('missing-language', 'wrong-source', 'notice'):
            with self.subTest(kind=kind):
                cache = self.damaged(kind)
                payload = envelope(self.menu, cache)
                before = copy.deepcopy(payload)
                with self.assertRaises(ValueError):
                    read_publication(_json_bytes(payload))
                pair = read_publication(_json_bytes(payload), recover_translations=True)
                self.assertEqual(pair, (self.menu, cache))
                self.assertEqual(_release_id(*(_json_bytes(value) for value in pair)), payload['release_id'])
                self.assertNotEqual(payload['release_id'], envelope(self.menu, {'schema_version': 1, 'entries': {}, 'notices': {}})['release_id'])
                self.assertEqual(payload, before)

    def test_opt_in_drive_reader_can_replace_damaged_existing_cache_with_valid_candidate(self):
        for kind in ('missing-language', 'wrong-source', 'notice'):
            with self.subTest(kind=kind):
                command = FakeRclone()
                cache = self.damaged(kind)
                command.add(envelope(self.menu, cache))
                with self.assertRaises(ValueError):
                    DrivePublisher(REMOTE, FOLDER_ID, file_id=FILE_ID, command=command).load_current()
                publisher = DrivePublisher(REMOTE, FOLDER_ID, file_id=FILE_ID, command=command,
                                           recover_translations=True)
                self.assertEqual(publisher.load_current(), (self.menu, cache))
                published = publisher.publish(self.menu, self.cache, previous=self.menu, today='2026-09-21')
                self.assertEqual(published.release_id, envelope(self.menu, self.cache)['release_id'])
                self.assertEqual(len(command.uploads), 1)
                self.assertEqual(DrivePublisher(REMOTE, FOLDER_ID, file_id=FILE_ID, command=command).load_current(),
                                 (self.menu, self.cache))

    def test_opt_in_directory_reader_can_replace_damaged_existing_cache_with_valid_candidate(self):
        for kind in ('missing-language', 'wrong-source', 'notice'):
            with self.subTest(kind=kind), TemporaryDirectory() as folder:
                root = Path(folder) / 'public'
                cache = self.damaged(kind)
                old = self.seed_directory(root, self.menu, cache)
                with self.assertRaises(ValueError):
                    DirectoryPublisher(root, notice_glossary={}).load_current()
                publisher = DirectoryPublisher(root, notice_glossary={}, recover_translations=True)
                self.assertEqual(publisher.load_current(), (self.menu, cache))
                self.assertEqual(publisher.load_release(old), (self.menu, cache))
                new = publisher.publish(self.menu, self.cache, previous=self.menu, today='2026-09-21')
                self.assertNotEqual(old, new)
                self.assertEqual(DirectoryPublisher(root, notice_glossary={}).load_current(), (self.menu, self.cache))
                self.assertEqual(publisher.load_release(old), (self.menu, cache))

    def test_opt_in_reading_does_not_allow_malformed_outgoing_candidates(self):
        command = FakeRclone()
        cache = self.damaged('missing-language')
        command.add(envelope(self.menu, cache))
        drive = DrivePublisher(REMOTE, FOLDER_ID, command=command, recover_translations=True)
        with self.assertRaises(ValueError):
            drive.publish(self.menu, cache)
        self.assertEqual(command.commands, [])
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            old = self.seed_directory(root, self.menu, cache)
            directory = DirectoryPublisher(root, notice_glossary={}, recover_translations=True)
            before = (root / 'data/current.json').read_bytes()
            with self.assertRaises(ValueError):
                directory.publish(self.menu, cache)
            self.assertEqual((root / 'data/current.json').read_bytes(), before)
            self.assertEqual([path.name for path in (root / 'data/releases').iterdir()], [old])

    def test_raw_hash_is_checked_before_translation_recovery(self):
        payload = envelope(self.menu, self.damaged('missing-language'))
        payload['release_id'] = 'a' * 64
        with patch('mensa.translation_contract.recover_cache', create=True,
                   side_effect=AssertionError('Recovery must follow raw identity validation')):
            with self.assertRaisesRegex(ValueError, 'identity'):
                read_publication(_json_bytes(payload), recover_translations=True)
            with TemporaryDirectory() as folder:
                root = Path(folder) / 'public'
                old = self.seed_directory(root, self.menu, self.damaged('missing-language'))
                path = root / 'data/releases' / old / 'translations.json'
                path.write_bytes(_json_bytes(self.cache))
                with self.assertRaisesRegex(ValueError, 'identity'):
                    DirectoryPublisher(root, notice_glossary={}, recover_translations=True).load_current()

    def test_recovery_rejects_invalid_menu_even_when_raw_identity_matches(self):
        menu = copy.deepcopy(self.menu)
        menu['days'][0]['meals'][0]['price_source']['name'] = 'Unrelated dish'
        with self.assertRaisesRegex(ValueError, 'menu|provenance'):
            read_publication(_json_bytes(envelope(menu, self.damaged('missing-language'))),
                             recover_translations=True)
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            self.seed_directory(root, menu, self.damaged('missing-language'))
            with self.assertRaisesRegex(ValueError, 'menu|provenance'):
                DirectoryPublisher(root, notice_glossary={}, recover_translations=True).load_current()

    def test_directory_recovery_rejects_noncanonical_pair_before_recovery(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'public'
            payloads = (json.dumps(self.menu, indent=2).encode(), _json_bytes(self.damaged('missing-language')))
            identifier = _release_id(*payloads)
            directory = root / 'data/releases' / identifier
            directory.mkdir(parents=True)
            for name, payload in zip(('menu.json', 'translations.json'), payloads):
                (directory / name).write_bytes(payload)
            (root / 'data/current.json').write_bytes(_json_bytes({'schema_version': 1, 'release_id': identifier}))
            with patch('mensa.translation_contract.recover_cache', create=True,
                       side_effect=AssertionError('Recovery must follow canonical identity validation')):
                with self.assertRaisesRegex(ValueError, 'canonical'):
                    DirectoryPublisher(root, notice_glossary={}, recover_translations=True).load_current()


if __name__ == '__main__':
    unittest.main()
