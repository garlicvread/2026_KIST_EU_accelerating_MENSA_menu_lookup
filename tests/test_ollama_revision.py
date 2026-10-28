"""Installed Ollama revision discovery reads bounded manifests without starting a model."""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class OllamaRevisionTests(unittest.TestCase):
    def test_supported_model_references_resolve_the_exact_manifest_digest(self):
        from mensa.ollama_revision import installed_ollama_revision
        for model, relative in (
                ('gemma4:31b', 'registry.ollama.ai/library/gemma4/31b'),
                ('gemma4', 'registry.ollama.ai/library/gemma4/latest'),
                ('team/gemma4:31b', 'registry.ollama.ai/team/gemma4/31b'),
                ('registry.example:5000/team/gemma4:31b', 'registry.example:5000/team/gemma4/31b')):
            with self.subTest(model=model), TemporaryDirectory() as folder:
                models = Path(folder)
                manifest = models / 'manifests' / relative
                manifest.parent.mkdir(parents=True)
                raw = b'{"schemaVersion":2,"config":{},"layers":[]}\n'
                manifest.write_bytes(raw)
                self.assertEqual(installed_ollama_revision(models, model), hashlib.sha256(raw).hexdigest())

    def test_missing_invalid_oversized_or_symlinked_manifests_are_refused(self):
        from mensa.ollama_revision import installed_ollama_revision, MAX_MANIFEST_BYTES
        with TemporaryDirectory() as folder:
            models = Path(folder)
            manifest = models / 'manifests/registry.ollama.ai/library/gemma4/31b'
            with self.assertRaises(ValueError):
                installed_ollama_revision(models, 'gemma4:31b')
            manifest.parent.mkdir(parents=True)
            for raw in (b'not-json', b'[]', b'{}', b'x' * (MAX_MANIFEST_BYTES + 1)):
                manifest.write_bytes(raw)
                with self.subTest(raw_size=len(raw)), self.assertRaises(ValueError):
                    installed_ollama_revision(models, 'gemma4:31b')
            manifest.unlink()
            outside = models / 'outside'
            outside.write_bytes(b'{"schemaVersion":2,"config":{},"layers":[]}')
            manifest.symlink_to(outside)
            with self.assertRaises(ValueError):
                installed_ollama_revision(models, 'gemma4:31b')
            manifest.unlink()
            manifest.parent.rmdir()
            manifest.parent.symlink_to(models, target_is_directory=True)
            with self.assertRaises(ValueError):
                installed_ollama_revision(models, 'gemma4:31b')

    def test_model_reference_cannot_escape_the_manifest_directory(self):
        from mensa.ollama_revision import installed_ollama_revision
        with TemporaryDirectory() as folder:
            for model in ('../gemma4:31b', 'team/../gemma4:31b', '/gemma4:31b',
                          'gemma4:../31b', 'http://host/team/gemma4', 'gemma4@sha256:abc', ''):
                with self.subTest(model=model), self.assertRaises(ValueError):
                    installed_ollama_revision(Path(folder), model)


if __name__ == '__main__':
    unittest.main()
