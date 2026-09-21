import hashlib
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


class AssetReferences(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        url = attrs.get("src") if tag == "script" else attrs.get("href")
        if url and urlsplit(url).path in {"app.js", "styles.css"}:
            self.urls[urlsplit(url).path] = url


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "index.html").write_text(
            '<link rel="stylesheet" href="styles.css">'
            '<script type="module" src="app.js"></script>', encoding="utf-8")
        (self.source / "app.js").write_bytes(b"console.log('menu');\n")
        (self.source / "styles.css").write_bytes(b"body { color: green; }\n")
        (self.source / "data").mkdir()
        (self.source / "data" / "menu.json").write_bytes(b'{"days": []}\n')

    def build(self, output):
        return subprocess.run(
            [sys.executable, "-m", "scripts.build_site", "--source", str(self.source),
             "--output", str(output)], capture_output=True, text=True, check=False)

    def test_generated_references_version_exact_copied_bytes_without_changing_source(self):
        before = {path.relative_to(self.source): path.read_bytes()
                  for path in self.source.rglob("*") if path.is_file()}
        output = self.root / "pages"
        result = self.build(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        urls = AssetReferences((output / "index.html").read_text()).urls
        self.assertEqual(set(urls), {"app.js", "styles.css"})
        for name, url in urls.items():
            digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
            self.assertEqual(parse_qs(urlsplit(url).query), {"v": [digest]})
            self.assertEqual((output / name).read_bytes(), before[Path(name)])
        self.assertEqual((output / "data" / "menu.json").read_bytes(), before[Path("data/menu.json")])
        self.assertEqual(before, {path.relative_to(self.source): path.read_bytes()
                                 for path in self.source.rglob("*") if path.is_file()})

    def test_changed_asset_content_changes_only_its_own_reference(self):
        previous = None
        for index, changed in enumerate([None, "app.js", "styles.css", None]):
            if changed:
                with (self.source / changed).open("ab") as stream:
                    stream.write(b"/* updated */\n")
            output = self.root / f"pages-{index}"
            result = self.build(output)
            self.assertEqual(result.returncode, 0, result.stderr)
            urls = AssetReferences((output / "index.html").read_text()).urls
            if previous is not None:
                for name in urls:
                    if name == changed:
                        self.assertNotEqual(urls[name], previous[name])
                    else:
                        self.assertEqual(urls[name], previous[name])
            previous = urls

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "pages"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("existing file")
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(sentinel.read_text(), "existing file")

    def test_output_inside_source_is_rejected(self):
        output = self.source / "pages"
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside", result.stderr)
        self.assertFalse(output.exists())

    def test_missing_asset_reference_stops_build_before_writing_output(self):
        (self.source / "index.html").write_text('<link href="styles.css">')
        output = self.root / "pages"
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app.js reference", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
