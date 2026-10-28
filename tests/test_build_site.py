import copy
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mensa.releases import DirectoryPublisher
from mensa.translation_contract import source_for
from scripts import build_site as builder
from scripts.menu_source import parse_menu
from test_menu_source import page
from test_publication import translation_entry


class AssetReferences(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type') == 'module':
            self.urls['app.js'] = attrs.get('src')
        elif tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.urls['styles.css'] = attrs.get('href')


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.menu = parse_menu(page(), '2026-09-21T10:00:00Z')
        record = self.menu['days'][0]['meals'][0]
        self.glossary = {
            'vegan': {'en': 'Vegan', 'ko': '비건'},
            'Weizen': {'en': 'Wheat', 'ko': '밀'},
            'Sellerie': {'en': 'Celery', 'ko': '셀러리'},
            'Milch und Laktose': {'en': 'Milk and lactose', 'ko': '우유 및 유당'},
        }
        self.cache = {'schema_version': 1, 'entries': {record['translation_key']: translation_entry(source_for(record))},
                      'notices': copy.deepcopy(self.glossary)}
        self.source, self.compiled, self.glossary_path = self.fixture(self.root)

    def fixture(self, root):
        source, compiled, reviewed = (root / name for name in ('source', 'compiled', 'reviewed'))
        for directory in (source / 'data', compiled / 'nested', reviewed):
            directory.mkdir(parents=True)
        (source / 'index.html').write_text('<meta name="mensa-drive-file-id" content=""><link rel="stylesheet" href="styles.css"><script type="module" src="app.js"></script>', encoding='utf-8')
        (source / 'styles.css').write_bytes(b'body { color: green; }\n')
        (source / 'favicon.svg').write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n')
        (source / 'data/menu.json').write_text(json.dumps(self.menu, ensure_ascii=False), encoding='utf-8')
        (source / 'data/translations.json').write_text(json.dumps(self.cache, ensure_ascii=False), encoding='utf-8')
        (reviewed / 'notice-translations.json').write_text(json.dumps({'schema_version': 1, 'notices': self.glossary}), encoding='utf-8')
        (compiled / 'main.js').write_bytes(b"import { value } from './nested/view.js';\nconsole.log(value);\n")
        (compiled / 'nested/view.js').write_bytes(b"export { value } from '../data.js';\n")
        (compiled / 'data.js').write_bytes(b"export const value = 'menu';\n")
        return source, compiled, reviewed / 'notice-translations.json'

    def build(self, output, *, source=None, compiled=None, glossary=None, extra=()):
        return subprocess.run([sys.executable, '-m', 'scripts.build_site', '--source', str(source or self.source),
                               '--output', str(output), '--compiled', str(compiled or self.compiled),
                               '--glossary', str(glossary or self.glossary_path), *extra],
                              capture_output=True, text=True, check=False)

    def source_bytes(self):
        return {path.relative_to(self.source): path.read_bytes() for path in self.source.rglob('*') if path.is_file()}

    def test_drive_build_needs_only_ui_assets_and_emits_configured_meta_without_json(self):
        for path in (self.source / 'data').iterdir():
            path.unlink()
        (self.source / 'data').rmdir()
        self.glossary_path.unlink()
        output = self.root / 'drive-pages'; before = self.source_bytes()
        result = self.build(output, extra=['--drive-file-id', 'fixture-public-file-id'])
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (output / 'index.html').read_text()
        self.assertIn('<meta name="mensa-drive-file-id" content="fixture-public-file-id">', html)
        self.assertFalse((output / 'data').exists())
        self.assertEqual(list(output.rglob('*.json')), [])
        urls = AssetReferences(html).urls
        self.assertEqual({path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_file()},
                         {'index.html', 'app.js', 'styles.css', 'favicon.svg', urls['styles.css']} |
                         {(Path(urls['app.js']).parent / name).as_posix() for name in ['main.js', 'nested/view.js', 'data.js']})
        self.assertEqual(before, self.source_bytes())

    def test_explicit_fixture_directory_is_used_without_source_runtime_data(self):
        for path in (self.source / 'data').iterdir():
            path.unlink()
        fixture = builder.ROOT / 'tests/fixtures/menu-data'
        output = self.root / 'fixture-pages'
        result = self.build(output, glossary=builder.ROOT / 'data/notice-translations.json', extra=['--data-dir', str(fixture)])
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = tuple(json.loads((fixture / name).read_text()) for name in ['menu.json', 'translations.json'])
        self.assertEqual(DirectoryPublisher(output, notice_glossary={}).load_current(), expected)
        self.assertIn('<meta name="mensa-drive-file-id" content="">', (output / 'index.html').read_text())

    def test_invalid_or_ambiguous_drive_configuration_stops_before_output_creation(self):
        for index, file_id in enumerate(['', 'short', '../file', 'file?download=1', 'a' * 201]):
            output = self.root / f'invalid-drive-{index}'
            result = self.build(output, extra=['--drive-file-id', file_id])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('file ID', result.stderr)
            self.assertFalse(output.exists())
        output = self.root / 'ambiguous-drive'
        result = self.build(output, extra=['--drive-file-id', 'fixture-public-file-id', '--data-dir', str(self.source / 'data')])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_output_cannot_overlap_explicit_data_tree(self):
        fixture = self.root / 'fixtures'; fixture.mkdir()
        for name in ['menu.json', 'translations.json']:
            (fixture / name).write_bytes((self.source / 'data' / name).read_bytes())
        result = self.build(fixture / 'pages', extra=['--data-dir', str(fixture)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('overlap', result.stderr)
        self.assertFalse((fixture / 'pages').exists())

    def test_drive_requires_one_meta_setting_and_python_modes_are_unambiguous(self):
        original = (self.source / 'index.html').read_text()
        meta = '<meta name="mensa-drive-file-id" content="">'
        for index, replacement in enumerate(['', meta + meta]):
            (self.source / 'index.html').write_text(original.replace(meta, replacement))
            output = self.root / f'missing-or-duplicate-meta-{index}'
            result = self.build(output, extra=['--drive-file-id', 'fixture-public-file-id'])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('meta tag', result.stderr)
            self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, 'not both'):
            builder.build_site(self.source, self.root / 'ambiguous-api', compiled=self.compiled,
                               glossary=self.glossary_path, data_dir=self.source / 'data',
                               drive_file_id='fixture-public-file-id')

    def test_generated_references_version_exact_copied_bytes_without_changing_source(self):
        before = self.source_bytes()
        output = self.root / 'pages'
        result = self.build(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        urls = AssetReferences((output / 'index.html').read_text()).urls
        self.assertEqual(set(urls), {'app.js', 'styles.css'})
        self.assertRegex(urls['app.js'], r'^assets/[0-9a-f]{64}/main\.js$')
        self.assertRegex(urls['styles.css'], r'^assets/styles-[0-9a-f]{64}\.css$')
        graph_dir = (output / urls['app.js']).parent
        self.assertEqual({p.relative_to(graph_dir) for p in graph_dir.rglob('*.js')},
                         {Path(name) for name in ['main.js', 'nested/view.js', 'data.js']})
        for name in ['main.js', 'nested/view.js', 'data.js']:
            self.assertEqual((graph_dir / name).read_bytes(), (self.compiled / name).read_bytes())
        self.assertEqual((output / urls['styles.css']).read_bytes(), before[Path('styles.css')])
        self.assertEqual((output / 'styles.css').read_bytes(), before[Path('styles.css')])
        self.assertEqual((output / 'app.js').read_text(), f"import './{urls['app.js']}';\n")
        self.assertEqual((output / 'favicon.svg').read_bytes(), before[Path('favicon.svg')])
        self.assertEqual(DirectoryPublisher(output, notice_glossary=self.glossary).load_current(), (self.menu, self.cache))
        self.assertEqual(before, self.source_bytes())

    def test_changed_asset_content_changes_only_its_own_reference(self):
        previous = None
        for index, changed in enumerate([None, 'data.js', 'styles.css', None]):
            if changed:
                path = self.source / changed if changed == 'styles.css' else self.compiled / changed
                with path.open('ab') as stream:
                    stream.write(b'/* updated */\n')
            main = (self.compiled / 'main.js').read_bytes()
            output = self.root / f'pages-{index}'
            result = self.build(output)
            self.assertEqual(result.returncode, 0, result.stderr)
            urls = AssetReferences((output / 'index.html').read_text()).urls
            if previous is not None:
                for name in urls:
                    expected_change = name == ('styles.css' if changed == 'styles.css' else 'app.js') if changed else False
                    if expected_change:
                        self.assertNotEqual(urls[name], previous[name])
                    else:
                        self.assertEqual(urls[name], previous[name])
                if changed == 'data.js':
                    self.assertEqual(main, previous_main, 'Dependency-only edit leaves main bytes unchanged')
            previous, previous_main = urls, main

    def test_existing_output_is_not_overwritten(self):
        output = self.root / 'pages'
        output.mkdir()
        sentinel = output / 'keep.txt'
        sentinel.write_text('existing file')
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(sentinel.read_text(), 'existing file')

    def test_output_inside_source_is_rejected(self):
        before = self.source_bytes()
        output = self.source / 'pages'
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('overlap', result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(before, self.source_bytes())

    def test_missing_asset_reference_stops_build_before_writing_output(self):
        (self.source / 'index.html').write_text('<link rel="stylesheet" href="styles.css">')
        output = self.root / 'pages'
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reference', result.stderr)
        self.assertFalse(output.exists())

    def test_selected_pair_privacy_and_public_modes_under_restrictive_umask(self):
        for path in [self.source / 'data/translation-history.json', self.compiled / 'private.js']:
            path.parent.mkdir(exist_ok=True); path.write_text('PRIVATE_SENTINEL')
        output = self.root / 'new-parent/pages'; before = self.source_bytes()
        old_umask = os.umask(0o077)
        try:
            builder.build_site(self.source, output, compiled=self.compiled, glossary=self.glossary_path)
        finally:
            os.umask(old_umask)
        self.assertEqual(DirectoryPublisher(output, notice_glossary={}).load_current(), (self.menu, self.cache))
        self.assertFalse((output / 'data/menu.json').exists()); self.assertFalse((output / 'data/translations.json').exists())
        self.assertEqual(before, self.source_bytes())
        for path in [output.parent, output, *output.rglob('*')]:
            with self.subTest(path=path.relative_to(self.root)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755 if path.is_dir() else 0o644)
                if path.is_file():
                    self.assertNotIn(b'PRIVATE_SENTINEL', path.read_bytes())
        public_files = {path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_file()}
        pointer = json.loads((output / 'data/current.json').read_text())
        urls = AssetReferences((output / 'index.html').read_text()).urls
        graph = Path(urls['app.js']).parent
        self.assertEqual(public_files, {'index.html', 'app.js', 'styles.css', 'favicon.svg', urls['styles.css'],
                                        'data/current.json', f"data/releases/{pointer['release_id']}/menu.json",
                                        f"data/releases/{pointer['release_id']}/translations.json"} |
                         {(graph / name).as_posix() for name in ['main.js', 'nested/view.js', 'data.js']})

    def test_invalid_data_or_reachable_graph_stops_before_output_creation(self):
        cases = ['incomplete-cache', "import './missing.js';", "import('./data.js');", "import './private.js';"]
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                folder = self.root / f'bad-{index}'
                source, compiled, glossary = self.fixture(folder)
                if case == 'incomplete-cache':
                    (source / 'data/translations.json').write_text('{"schema_version":1,"entries":{},"notices":{}}')
                else:
                    if case == "import './private.js';":
                        (compiled / 'private.js').write_text('PRIVATE_SENTINEL')
                    (compiled / 'main.js').write_text(case + '\n')
                output = folder / 'uncreated-parent/pages'
                result = self.build(output, source=source, compiled=compiled, glossary=glossary)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(output.parent.exists(), 'Invalid input must not create the output parent')

    def test_partial_write_failure_removes_only_this_calls_output(self):
        output = self.root / 'pages'; sentinel = self.root / 'keep.txt'; sentinel.write_text('outside')
        before = self.source_bytes(); original = Path.open; wrote_assets = []
        def fail_after_assets(path, *args, **kwargs):
            if path == output / 'styles.css' and args == ('xb',):
                wrote_assets.extend((output / 'assets').rglob('*.js'))
                self.assertTrue(wrote_assets, 'Inject failure only after actual graph files were written')
                path.mkdir()  # fail_after_assets는 파일 쓰기가 실제로 실패하도록 styles.css 경로에 디렉터리를 만듭니다.
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_after_assets), self.assertRaises(FileExistsError):
            builder.build_site(self.source, output, compiled=self.compiled, glossary=self.glossary_path)
        self.assertFalse(output.exists()); self.assertEqual(sentinel.read_text(), 'outside')
        self.assertEqual(before, self.source_bytes())


if __name__ == '__main__':
    unittest.main()
