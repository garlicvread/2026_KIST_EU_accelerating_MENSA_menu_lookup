"""게시 검사가 유효한 미번역 메뉴와 손상된 번역을 구별하는지 확인합니다."""

import copy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mensa import publication
from mensa.drive_publication import DrivePublisher
from mensa.notice_contract import source_notices
from mensa.releases import DirectoryPublisher, _json_bytes, _release_id
from mensa.retention import public_translations
from mensa.translation_contract import source_for, source_key
from scripts.build_site import build_site
from scripts.menu_source import parse_menu
from test_drive_publication import FakeRclone, REMOTE, FOLDER_ID, FILE_ID, envelope
from test_menu_source import page, meal
from test_publication import translation_entry


class SparsePublicationTests(unittest.TestCase):
    def setUp(self):
        self.menu = parse_menu(page(meal() + meal(name='Untranslated second dish')),
                               '2026-09-21T10:00:00Z')
        first, second = self.menu['days'][0]['meals']
        second['notices'].append('Neue Quellenwarnung')
        self.key, self.missing_key = first['translation_key'], second['translation_key']
        self.glossary = {'vegan': {'en': 'Vegan', 'ko': '비건'}}
        self.cache = {'schema_version': 1,
                      'entries': {self.key: translation_entry(source_for(first))},
                      'notices': copy.deepcopy(self.glossary)}

    def test_valid_sparse_pair_preserves_source_prices_and_raw_notices(self):
        original = copy.deepcopy((self.menu, self.cache))
        publication.validate_publication(self.menu, self.cache, notice_glossary=self.glossary,
                                         today='2026-09-21')
        self.assertEqual((self.menu, self.cache), original)
        self.assertEqual(self.menu['days'][0]['meals'][1]['prices']['student'], 350)
        self.assertIn('Neue Quellenwarnung', self.menu['days'][0]['meals'][1]['notices'])
        publication.validate_publication(self.menu, {'schema_version': 1, 'entries': {}},
                                         notice_glossary=self.glossary)

    def test_translation_gaps_are_sorted_unique_and_track_completion_without_mutation(self):
        before = copy.deepcopy((self.menu, self.cache))
        self.assertEqual(publication.translation_gaps(self.menu, self.cache), {
            'entries': (self.missing_key,),
            'notices': ('Milch und Laktose', 'Neue Quellenwarnung', 'Sellerie', 'Weizen'),
        })
        self.assertEqual((self.menu, self.cache), before)
        second = self.menu['days'][0]['meals'][1]
        self.cache['entries'][self.missing_key] = translation_entry(source_for(second))
        for label in publication.translation_gaps(self.menu, self.cache)['notices']:
            self.cache['notices'][label] = {'en': 'Notice', 'ko': '안내'}
        self.assertEqual(publication.translation_gaps(self.menu, self.cache),
                         {'entries': (), 'notices': ()})

    def test_projection_keeps_only_present_references_and_independent_copies(self):
        unused_source = {'name_de': 'Unused dish', 'components': ['Reis', 'Gemüse']}
        unused = source_key(unused_source)
        self.cache['entries'][unused] = translation_entry(unused_source)
        self.cache['notices']['Unused notice'] = {'en': 'Unused', 'ko': '미사용'}
        self.cache['private_usage'] = {'private': True}
        before = copy.deepcopy((self.menu, self.cache))
        result = public_translations(self.menu, self.cache)
        self.assertEqual(result, {'schema_version': 1, 'entries': {self.key: self.cache['entries'][self.key]},
                                  'notices': self.glossary})
        result['entries'][self.key]['en']['name'] = 'Changed copy'
        result['notices']['vegan']['en'] = 'Changed copy'
        self.assertEqual((self.menu, self.cache), before)

    def test_projection_validates_unreferenced_cache_data_before_discarding_it(self):
        second = self.menu['days'][0]['meals'][1]
        self.cache['entries'][self.missing_key] = translation_entry(source_for(second))
        for label in source_notices(self.menu):
            self.cache['notices'].setdefault(label, {'en': 'Notice', 'ko': '안내'})
        malformed_entry = copy.deepcopy(self.cache)
        malformed_entry['entries']['a' * 64] = {'source': {'name_de': 'Invalid', 'components': []}}
        malformed_notice = copy.deepcopy(self.cache)
        malformed_notice['notices']['Unused notice'] = {'en': 'Missing Korean'}
        for invalid in (malformed_entry, malformed_notice, {'schema_version': 1, 'entries': []}):
            with self.subTest(cache=invalid), self.assertRaises(ValueError):
                public_translations(self.menu, invalid)

    def test_sparse_cache_does_not_hide_invalid_supplied_entries_or_price_associations(self):
        def entry(cache):
            return cache['entries'][self.key]
        def missing_ko(menu, cache):
            del entry(cache)['ko']
        def wrong_source(menu, cache):
            entry(cache)['source']['name_de'] = 'Wrong source'
        def wrong_key(menu, cache):
            cache['entries']['b' * 64] = cache['entries'].pop(self.key)
        def components(menu, cache):
            entry(cache)['en']['components'].pop()
        def byte_tokens(menu, cache):
            entry(cache)['ko']['name'] = '<0xEB><0xA0><0x9D>'
        def reviewed_notice(menu, cache):
            cache['notices']['vegan']['ko'] = '변조'
        def incomplete_notice(menu, cache):
            del cache['notices']['vegan']['ko']
        def price_association(menu, cache):
            menu['days'][0]['meals'][1]['price_source']['name'] = 'Other dish'
        for mutate in (missing_ko, wrong_source, wrong_key, components, byte_tokens,
                       reviewed_notice, incomplete_notice, price_association):
            with self.subTest(mutation=mutate.__name__):
                menu, cache = copy.deepcopy((self.menu, self.cache))
                mutate(menu, cache)
                with self.assertRaises(ValueError):
                    publication.validate_publication(menu, cache, notice_glossary=self.glossary)

    def test_sparse_pair_roundtrips_through_drive_and_directory_with_unchanged_identity(self):
        command = FakeRclone()
        drive = DrivePublisher(REMOTE, FOLDER_ID, command=command)
        expected_id = _release_id(_json_bytes(self.menu), _json_bytes(self.cache))
        published = drive.publish(self.menu, self.cache, today='2026-09-21')
        self.assertEqual((published.release_id, published.file_id), (expected_id, FILE_ID))
        self.assertEqual(drive.load_current(), (self.menu, self.cache))
        with TemporaryDirectory() as folder:
            directory = DirectoryPublisher(Path(folder) / 'public', notice_glossary=self.glossary)
            self.assertEqual(directory.publish(*drive.load_current(), today='2026-09-21'), expected_id)
            self.assertEqual(directory.load_release(expected_id), (self.menu, self.cache))
            self.assertEqual(directory.load_current(), (self.menu, self.cache))

    def test_sparse_local_and_drive_builds_keep_the_same_published_pair(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source, compiled = root / 'site', root / 'compiled'
            (source / 'data').mkdir(parents=True)
            compiled.mkdir()
            (source / 'index.html').write_text('<link rel="stylesheet" href="styles.css"><script type="module" src="app.js"></script>')
            (source / 'styles.css').write_text('body {}')
            (source / 'favicon.svg').write_text('<svg/>')
            (compiled / 'main.js').write_text('export const ready = true;')
            (source / 'data/menu.json').write_bytes(_json_bytes(self.menu))
            (source / 'data/translations.json').write_bytes(_json_bytes(self.cache))
            (root / 'reviewed').mkdir()
            glossary = root / 'reviewed/notices.json'
            glossary.write_bytes(_json_bytes({'schema_version': 1, 'notices': self.glossary}))
            for mode in ('local', 'drive'):
                with self.subTest(mode=mode):
                    output = root / mode
                    with patch('urllib.request.urlopen', return_value=BytesIO(_json_bytes(envelope(self.menu, self.cache)))):
                        build_site(source, output, compiled=compiled, glossary=glossary,
                                   drive_file_id='fixture-public-file-id' if mode == 'drive' else None)
                    self.assertEqual(DirectoryPublisher(output, notice_glossary=self.glossary).load_current(),
                                     (self.menu, self.cache))


if __name__ == '__main__':
    unittest.main()
