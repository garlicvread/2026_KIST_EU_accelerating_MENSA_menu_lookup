"""이 테스트 모듈은 중단한 번역을 올바르게 재개하는지 확인하기 위해 실제 TranslationService와 비공개 번역 재개 기록 저장 코드를 함께 실행합니다."""

import copy
import importlib
import inspect
from pathlib import Path
import tempfile
import unittest

from mensa.checkpoints import TranslationCheckpointStore
from mensa.errors import GenerationError
from mensa.translation_contract import PROMPT_VERSION, generation_identity, source_key


class TranslationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = {"model": "fixture", "provider": "ollama", "url": "http://127.0.0.1/api/chat",
                       "backend_revision": "immutable-r1"}
        self.glossary = {"Weizen": {"en": "Wheat", "ko": "밀"}}
        self.sources = [{"name_de": name, "components": ["Brot"]} for name in ("Suppe A", "Suppe B")]
        self.menu = {"days": [{"meals": [self.meal(source) for source in self.sources]}]}
        self.saved = []
        self.generated = []
        self.runner()

    def runner(self):
        path = Path(__file__).resolve().parents[1] / "mensa" / "translation_runner.py"
        self.assertTrue(path.is_file(), "resume_translations must provide explicit private resume orchestration")
        runner = getattr(importlib.import_module("mensa.translation_runner"), "resume_translations", None)
        self.assertTrue(callable(runner), "resume_translations must be callable")
        return runner

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

    def run_resume(self, published=None, private=None, phrases=None, config="default", **ports):
        supplied = {"glossary_provider": lambda: self.glossary, "translate": self.generate,
                    "load_checkpoint": lambda: private, "save_checkpoint": self.saved.append}
        supplied.update(ports)
        return self.runner()(self.menu, published, {} if phrases is None else phrases,
                             self.config if config == "default" else config, **supplied)

    def test_resume_signature_requires_explicit_keyword_ports(self):
        runner = self.runner()
        signature = inspect.signature(runner)
        positional = ["menu", "published", "phrases", "config"]
        ports = {"glossary_provider", "translate", "load_checkpoint", "save_checkpoint"}
        self.assertEqual(list(signature.parameters)[:4], positional)
        self.assertEqual(set(signature.parameters), set(positional) | ports)
        for name in positional:
            parameter = signature.parameters[name]
            self.assertEqual(parameter.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            self.assertIs(parameter.default, inspect.Parameter.empty)
        for name in ports:
            parameter = signature.parameters[name]
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_every_noncallable_port_rejected_before_any_port_invocation(self):
        calls = []
        for name in ("glossary_provider", "translate", "load_checkpoint", "save_checkpoint"):
            for invalid in (None, False, 1, [], {}):
                supplied = {port: lambda *args: calls.append("called") for port in
                            ("glossary_provider", "translate", "load_checkpoint", "save_checkpoint")}
                supplied[name] = invalid
                with self.subTest(port=name, invalid=invalid), self.assertRaisesRegex(TypeError, name):
                    self.run_resume(**supplied)
                self.assertEqual(calls, [])

    def test_malformed_published_rejected_before_loading_or_other_ports(self):
        calls = []
        supplied = {port: lambda *args: calls.append(port) for port in
                    ("glossary_provider", "translate", "load_checkpoint", "save_checkpoint")}
        for published in ([], False, "", 0, {"schema_version": 2, "entries": {}},
                          {"schema_version": 1, "entries": {"bad": {}}}):
            with self.subTest(published=published), self.assertRaises(ValueError):
                self.run_resume(published, **supplied)
            self.assertEqual(calls, [])

    def test_absent_published_and_checkpoint_generate_and_persist_final(self):
        for published in (None, {}):
            with self.subTest(published=published):
                self.saved.clear()
                self.generated.clear()
                cache = self.run_resume(published)
                self.assertEqual(self.generated, ["Suppe A", "Suppe B"])
                self.assertEqual(len(self.saved), 3)
                self.assertEqual(self.saved[-1], cache)
                self.assertIsNot(self.saved[-1], cache)

    def test_corrupt_checkpoint_rejected_before_glossary_generation_or_save(self):
        calls = []
        for private in ({}, [], False, {"schema_version": 2, "entries": {}},
                        {"schema_version": 1, "entries": {"bad": {}}}):
            with self.subTest(private=private), self.assertRaises(ValueError):
                self.run_resume(private=private, glossary_provider=lambda: calls.append("glossary"))
            self.assertEqual(calls, [])
            self.assertEqual(self.generated, [])
            self.assertEqual(self.saved, [])

    def test_interruption_and_recreated_store_resume_only_missing_entry_after_url_change(self):
        published = self.cache(self.entry({"name_de": "Historisch", "components": ["Brot"]}, name="History"))
        before = copy.deepcopy(published)
        with tempfile.TemporaryDirectory() as scratch:
            state_dir = Path(scratch) / "private"
            store = TranslationCheckpointStore(state_dir)
            attempted = []
            failure = GenerationError("timeout")

            def interrupted(source, config):
                attempted.append(source["name_de"])
                if source["name_de"] == "Suppe B":
                    raise failure
                return self.result("Accepted A")

            with self.assertRaises(GenerationError) as raised:
                self.run_resume(published, translate=interrupted, load_checkpoint=store.load,
                                save_checkpoint=store.save)
            self.assertIs(raised.exception, failure)
            self.assertEqual(attempted, ["Suppe A", "Suppe B"])
            checkpoint = store.load()
            self.assertEqual(set(checkpoint["entries"]), {source_key(self.sources[0]), *published["entries"]})
            changed_url = {**self.config, "url": "http://127.0.0.2/api/chat"}
            recreated = TranslationCheckpointStore(state_dir)
            final = self.run_resume(published, config=changed_url, load_checkpoint=recreated.load,
                                    save_checkpoint=recreated.save)
            self.assertEqual(self.generated, ["Suppe B"])
            self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Accepted A")
            self.assertEqual(recreated.load(), final)
            self.assertEqual(published, before)

    def test_changed_backend_revision_regenerates_private_model_entries(self):
        private = self.cache(*(self.entry(source) for source in self.sources))
        final = self.run_resume(private=private, config={**self.config, "backend_revision": "immutable-r2"})
        self.assertEqual(self.generated, ["Suppe A", "Suppe B"])
        self.assertNotEqual(final["entries"][source_key(self.sources[0])]["generation_identity"],
                            private["entries"][source_key(self.sources[0])]["generation_identity"])

    def test_current_editorial_phrases_override_stale_public_and_private_entries(self):
        public = self.cache(self.entry(name="Old public", origin="reviewed-draft"))
        private = self.cache(self.entry(name="Old private"))
        phrases = {"Suppe A": {"en": "Corrected soup", "ko": "수정 수프"},
                   "Brot": {"en": "Corrected bread", "ko": "수정 빵"}}
        final = self.run_resume(public, private, phrases)
        entry = final["entries"][source_key(self.sources[0])]
        self.assertEqual(entry["en"], {"name": "Corrected soup", "components": ["Corrected bread"]})
        self.assertEqual(entry["origin"], "editorial-draft")
        self.assertEqual(self.generated, ["Suppe B"])

    def test_private_editorial_never_replaces_public_reviewed(self):
        final = self.run_resume(self.cache(self.entry(name="Public reviewed", origin="reviewed-draft")),
                                self.cache(self.entry(name="Private stale", origin="editorial-draft")))
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Public reviewed")
        self.assertEqual(self.generated, ["Suppe B"])

    def test_public_editorial_wins_over_private_compatible_model(self):
        final = self.run_resume(self.cache(self.entry(name="Public editorial", origin="editorial-draft")),
                                self.cache(self.entry(name="Private model")))
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Public editorial")

    def test_both_compatible_models_keep_public(self):
        final = self.run_resume(self.cache(self.entry(name="Public model")),
                                self.cache(self.entry(name="Private model")))
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Public model")
        self.assertEqual(self.generated, ["Suppe B"])

    def test_incompatible_public_model_uses_current_compatible_private_model(self):
        old = {**self.config, "backend_revision": "obsolete"}
        final = self.run_resume(self.cache(self.entry(name="Public obsolete", config=old)),
                                self.cache(self.entry(name="Private current")))
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Private current")
        self.assertEqual(self.generated, ["Suppe B"])

    def test_private_editorial_cannot_replace_incompatible_public_model(self):
        old = {**self.config, "backend_revision": "obsolete"}
        final = self.run_resume(self.cache(self.entry(name="Public obsolete", config=old)),
                                self.cache(self.entry(name="Private editorial", origin="editorial-draft")))
        self.assertEqual(self.generated, ["Suppe A", "Suppe B"])
        self.assertEqual(final["entries"][source_key(self.sources[0])]["origin"], "model")

    def test_incompatible_private_model_cannot_replace_incompatible_public_model_offline(self):
        old = {**self.config, "backend_revision": "obsolete"}
        public = self.cache(self.entry(name="Public offline", config=old))
        final = self.run_resume(public, self.cache(self.entry(name="Private current")), config=None)
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Public offline")
        self.assertEqual(self.generated, [])
        self.assertEqual(self.saved, [final])

    def test_missing_public_entries_use_private_history_of_all_origins_and_keep_notices(self):
        history = {"name_de": "Historisch", "components": ["Brot"]}
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            with self.subTest(origin=origin):
                self.saved.clear()
                self.generated.clear()
                public = self.cache(self.entry(history, name="Public history"), notices={
                    "Alt": {"en": "Public old", "ko": "공개 이전"},
                    "Weizen": {"en": "Old wheat", "ko": "이전 밀"}})
                private = self.cache(self.entry(origin=origin), notices={
                    "Privat": {"en": "Private old", "ko": "비공개 이전"},
                    "Alt": {"en": "Private stale", "ko": "비공개 오래됨"}})
                before = copy.deepcopy((public, private))
                final = self.run_resume(public, private)
                self.assertEqual(final["entries"][source_key(self.sources[0])]["origin"], origin)
                self.assertEqual(final["entries"][source_key(history)]["en"]["name"], "Public history")
                self.assertEqual(final["notices"]["Alt"], public["notices"]["Alt"])
                self.assertEqual(final["notices"]["Privat"], private["notices"]["Privat"])
                self.assertEqual(final["notices"]["Weizen"], self.glossary["Weizen"])
                self.assertEqual((public, private), before)
                self.assertEqual(self.generated, ["Suppe B"])

    def test_offline_empty_success_still_persists_final(self):
        final = self.run_resume(config=None)
        self.assertEqual(final, self.cache(notices=self.glossary))
        self.assertEqual(self.saved, [final])
        self.assertEqual(self.generated, [])

    def test_reused_only_success_persists_final_without_per_entry_saves(self):
        private = self.cache(*(self.entry(source) for source in self.sources))
        final = self.run_resume(private=private)
        self.assertEqual(self.saved, [final])
        self.assertEqual(self.generated, [])

    def test_legacy_history_migrations_are_delegated_to_service(self):
        source = {"name_de": "Wikingertopf", "components": ["Peperoni"]}
        legacy = self.entry(source, name="Wikingertopf")
        legacy["en"]["components"] = ["Pepperoni"]
        legacy["ko"]["components"] = ["페퍼로니"]
        legacy["prompt_version"] = "menu-v3"
        for public, private in ((self.cache(legacy), None), (None, self.cache(legacy))):
            with self.subTest(public=public):
                final = self.run_resume(public, private, config=None)
                migrated = final["entries"][source_key(source)]
                self.assertEqual(migrated["en"], {"name": "Meatball stew", "components": ["Chili peppers"]})
                self.assertEqual(legacy["en"]["name"], "Wikingertopf")

    def test_one_copied_glossary_snapshot_drives_merge_and_generation_identity(self):
        public = self.cache(self.entry(config={**self.config, "backend_revision": "obsolete"}))
        private = self.cache(self.entry(name="Private snapshot"))
        original = copy.deepcopy(self.glossary)
        calls = []

        def glossary():
            calls.append("glossary")
            return self.glossary

        def generate(source, config):
            self.glossary["Weizen"]["en"] = "Mutated externally"
            return self.generate(source, config)

        final = self.run_resume(public, private, glossary_provider=glossary, translate=generate)
        self.assertEqual(calls, ["glossary"])
        self.assertEqual(self.generated, ["Suppe B"])
        self.assertEqual(final["entries"][source_key(self.sources[0])]["en"]["name"], "Private snapshot")
        self.assertEqual(final["notices"], original)
        self.assertEqual(final["entries"][source_key(self.sources[1])]["generation_identity"],
                         generation_identity(self.config, notice_glossary=original))

    def test_callback_mutations_cannot_change_final_or_other_saved_snapshots(self):
        retained = []
        pristine = []

        def save(cache):
            pristine.append(copy.deepcopy(cache))
            retained.append(cache)
            cache["entries"].clear()
            cache["notices"]["Weizen"]["en"] = "Callback mutation"

        final = self.run_resume(save_checkpoint=save)
        self.assertEqual(len(final["entries"]), 2)
        self.assertEqual(len(pristine), 3)
        self.assertEqual([len(cache["entries"]) for cache in pristine], [1, 2, 2])
        self.assertEqual(final, pristine[-1])
        final["entries"].clear()
        self.assertEqual(len(pristine[-1]["entries"]), 2)
        self.assertTrue(all(cache["notices"]["Weizen"]["en"] == "Callback mutation" for cache in retained))

    def test_port_failures_propagate_without_partial_merge_or_checkpoint_clear(self):
        public = self.cache(self.entry(name="Public obsolete", config={**self.config, "backend_revision": "old"}))
        private = self.cache(self.entry(name="Private current"))
        before = copy.deepcopy((public, private))
        for port in ("load_checkpoint", "glossary_provider", "translate"):
            failure = OSError("fixture " + port)

            def fail(*args):
                raise failure

            self.saved.clear()
            with self.subTest(port=port), self.assertRaises(OSError) as raised:
                self.run_resume(public, private, **{port: fail})
            self.assertIs(raised.exception, failure)
            self.assertEqual(self.saved, [])
            self.assertEqual((public, private), before)

    def test_checkpoint_save_failure_stops_build_before_next_entry(self):
        failure = OSError("fixture save")
        saves = []

        def save(cache):
            saves.append(copy.deepcopy(cache))
            raise failure

        with self.assertRaises(OSError) as raised:
            self.run_resume(save_checkpoint=save)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.generated, ["Suppe A"])
        self.assertEqual(len(saves), 1)
        self.assertEqual(set(saves[0]["entries"]), {source_key(self.sources[0])})

    def test_final_save_failure_propagates_for_reused_only_success(self):
        private = self.cache(*(self.entry(source) for source in self.sources))
        failure = OSError("fixture final save")
        calls = []

        def save(cache):
            calls.append(cache)
            raise failure

        with self.assertRaises(OSError) as raised:
            self.run_resume(private=private, save_checkpoint=save)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.generated, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(private["entries"], calls[0]["entries"])


if __name__ == "__main__":
    unittest.main()
