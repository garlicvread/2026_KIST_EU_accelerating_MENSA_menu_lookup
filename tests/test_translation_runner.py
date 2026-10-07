"""이 테스트 모듈은 게시한 번역과 비공개 재개 기록을 합칠 때 검토한 번역과 최신 번역을 올바르게 선택하는지 확인합니다."""

import copy
import unittest

from mensa.translation_runner import resume_translations
from mensa.translation_contract import PROMPT_VERSION, generation_identity, source_key


class TranslationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = {"model": "fixture", "provider": "ollama", "url": "http://127.0.0.1/api/chat",
                       "backend_revision": "immutable-r1"}
        self.glossary = {"Weizen": {"en": "Wheat", "ko": "밀"}}
        self.sources = [{"name_de": name, "components": ["Brot"]} for name in ("Suppe A", "Suppe B")]
        self.menu = {"days": [{"meals": [self.meal(source) for source in self.sources]}]}
        self.generated = []


    def meal(self, source):
        return {"name_de": source["name_de"], "components": [{"name_de": name, "notices": ["Weizen"]}
                for name in source["components"]], "translation_key": source_key(source)}

    def result(self, name="Generated soup"):
        return {"en": {"name": name, "components": ["Bread"]},
                "ko": {"name": "수프", "components": ["빵"]}}

    def entry(self, source=None, name="Cached soup", origin="model", config=None):
        source = self.sources[0] if source is None else source
        entry = {"source": copy.deepcopy(source), **self.result(name), "origin": origin,
                 "model": "fixture" if origin == "model" else "assistant-draft", "prompt_version": PROMPT_VERSION}
        if origin == "model":
            entry["generation_identity"] = generation_identity(self.config if config is None else config,
                                                               notice_glossary=self.glossary)
        return entry

    def cache(self, *entries, notices=None):
        cache = {"schema_version": 1, "entries": {source_key(entry["source"]): entry for entry in entries}}
        if notices is not None:
            cache["notices"] = notices
        return cache

    def generate(self, source, config):
        self.generated.append(source["name_de"])
        return self.result("Generated " + source["name_de"])

    def run_resume(self, published=None, private=None):
        return resume_translations(self.menu, published, {}, self.config,
                                   glossary_provider=lambda: self.glossary, translate=self.generate,
                                   load_checkpoint=lambda: private, save_checkpoint=lambda candidate: None)


    def test_private_editorial_never_replaces_public_reviewed(self):
        final = self.run_resume(self.cache(self.entry(name="Public reviewed", origin="reviewed-draft")),
                                self.cache(self.entry(name="Private stale", origin="editorial-draft")))
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Public reviewed")
        self.assertEqual(self.generated, ["Suppe B"])


    def test_incompatible_public_model_uses_current_compatible_private_model(self):
        old = {**self.config, "backend_revision": "obsolete"}
        final = self.run_resume(self.cache(self.entry(name="Public obsolete", config=old)),
                                self.cache(self.entry(name="Private current")))
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Private current")
        self.assertEqual(self.generated, ["Suppe B"])


    def test_private_model_history_fills_missing_public_entries_and_merges_notices(self):
        history = {"name_de": "Historisch", "components": ["Brot"]}
        public = self.cache(self.entry(history, name="Public history"), notices={
            "Alt": {"en": "Public old", "ko": "공개 이전"},
            "Weizen": {"en": "Old wheat", "ko": "이전 밀"}})
        private = self.cache(self.entry(), notices={
            "Privat": {"en": "Private old", "ko": "비공개 이전"},
            "Alt": {"en": "Private stale", "ko": "비공개 오래됨"}})
        final = self.run_resume(public, private)
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Cached soup")
        self.assertEqual(final["entries"][source_key(history)]["en"]["name"], "Public history")
        self.assertEqual(final["notices"]["Alt"], public["notices"]["Alt"])
        self.assertEqual(final["notices"]["Privat"], private["notices"]["Privat"])
        self.assertEqual(final["notices"]["Weizen"], self.glossary["Weizen"])
        self.assertEqual(self.generated, ["Suppe B"])


if __name__ == "__main__":
    unittest.main()
