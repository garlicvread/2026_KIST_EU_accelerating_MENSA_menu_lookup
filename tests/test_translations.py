import copy
import hashlib
import json
from pathlib import Path
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from scripts.translations import build_translations, validate_cache, model_config, source_for
from scripts import notices, translations
from mensa import translation_contract
from mensa import translation_service


@contextmanager
def provider(content):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            choice = {"message": {"content": content}, "finish_reason": "stop"}
            body = json.dumps({"choices": [choice]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions", "model": "fixture-gemma", "key": "fixture-key"}, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def ollama_provider(response):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append({"path": self.path, "headers": dict(self.headers),
                          "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            body = response if isinstance(response, bytes) else json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"provider": "ollama", "url": f"http://127.0.0.1:{server.server_port}/api/chat",
               "model": "gemma4:e4b", "key": ""}, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def sample_meal(name="Schupfnudeln", sides=("Gemüse",)):
    source = {"name_de": name, "components": list(sides)}
    key = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"translation_key": key, "name_de": name,
            "components": [{"name_de": s, "notices": []} for s in sides],
            "prices": {"student": 310, "staff": 560, "guest": 780}}


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.meal = sample_meal()
        self.menu = {"days": [{"meals": [self.meal]}]}
        self.phrases = {"Schupfnudeln": {"en": "German potato dumplings", "ko": "독일식 감자 경단"},
                        "Gemüse": {"en": "Vegetables", "ko": "채소"}}

    def test_no_provider_keeps_unknown_as_original(self):
        result = build_translations(self.menu, {"schema_version": 1, "entries": {}}, {}, None)
        self.assertEqual(result["entries"], {})
        self.assertEqual(self.meal["prices"]["student"], 310)

    def test_editorial_translation_covers_name_and_components(self):
        cache = build_translations(self.menu, {}, self.phrases, None)
        entry = cache["entries"][self.meal["translation_key"]]
        self.assertEqual(entry["ko"], {"name": "독일식 감자 경단", "components": ["채소"]})
        validate_cache(cache)

    def test_notices_are_translated_even_when_dish_translation_is_cached(self):
        old = build_translations(self.menu, {}, self.phrases, None)
        old.pop("notices", None)  # 이 테스트는 안내문 번역이 도입되기 전에 게시한 schema_version 1 번역 파일을 재현하도록 old에서 notices 항목을 제거합니다.
        self.meal["notices"] = ["vegan", "Weizen"]
        self.meal["components"][0]["notices"] = ["Milch und Laktose", "Weizen"]
        original = copy.deepcopy(self.menu)
        calls = []
        cache = build_translations(self.menu, old, {}, None, translate=lambda *args: calls.append(args))
        self.assertEqual(set(cache.get("notices", {})), {"vegan", "Weizen", "Milch und Laktose"})
        self.assertEqual(cache["notices"]["Weizen"], {"en": "Wheat", "ko": "밀"})
        self.assertEqual(cache["entries"], old["entries"])
        self.assertEqual(self.menu, original)
        self.assertEqual(calls, [])
        self.assertNotIn("notices", old)

    def test_unknown_notice_fails_before_any_model_request_and_keeps_old_cache(self):
        old = build_translations(self.menu, {}, self.phrases, None)
        before = copy.deepcopy(old)
        self.meal["components"][0]["notices"] = ["New unreviewed source label"]
        calls = []
        with self.assertRaisesRegex(ValueError, "New unreviewed source label.*notice-translations.json"):
            build_translations(self.menu, old, {}, {"model": "fixture"},
                               translate=lambda *args: calls.append(args))
        self.assertEqual(old, before)
        self.assertEqual(calls, [])

    def test_malformed_notice_cache_is_rejected_but_legacy_omission_is_valid(self):
        validate_cache({"schema_version": 1, "entries": {}})
        for notices in (None, [], {"Weizen": {"en": "Wheat"}},
                        {"Weizen": {"en": "Wheat", "ko": ""}},
                        {"Weizen": {"en": "Wheat", "ko": "밀", "safe": True}},
                        {"": {"en": "Wheat", "ko": "밀"}},
                        {"Weizen": {"en": 12, "ko": "밀"}}):
            with self.subTest(notices=notices), self.assertRaisesRegex(ValueError, "[Nn]otice"):
                validate_cache({"schema_version": 1, "entries": {}, "notices": notices})

    def test_unused_notice_cache_keys_survive_for_interrupted_snapshot_compatibility(self):
        self.meal["notices"] = ["Weizen"]
        old = build_translations(self.menu, {}, self.phrases, None)
        self.meal["notices"] = ["vegan"]
        cache = build_translations(self.menu, old, {}, None)
        self.assertEqual(set(cache.get("notices", {})), {"Weizen", "vegan"})

    def test_glossary_covers_every_notice_in_the_published_source_snapshot(self):
        root = Path(__file__).resolve().parents[1]
        menu = json.loads((root / "site/data/menu.json").read_text())
        previous = json.loads((root / "site/data/translations.json").read_text())
        original = copy.deepcopy(menu)
        labels = {label for day in menu["days"] for meal in day["meals"]
                  for record in [meal, *meal["components"]] for label in record["notices"]}
        cache = build_translations(menu, previous, {}, None)
        self.assertLessEqual(labels, cache["notices"].keys())
        self.assertEqual(cache["entries"], previous["entries"])
        self.assertEqual(menu, original)

    def test_notice_source_keys_are_exact_and_not_normalized(self):
        for label in ("weizen", "Weizen ", "biologisches Essen"):
            with self.subTest(label=label):
                self.meal["notices"] = [label]
                with self.assertRaisesRegex(ValueError, "Unknown notice"):
                    build_translations(self.menu, {}, self.phrases, None)

    def test_source_change_does_not_reuse_old_translation(self):
        old = build_translations(self.menu, {}, self.phrases, None)
        changed = sample_meal(sides=("Fleisch",))
        result = build_translations({"days": [{"meals": [changed]}]}, old, {}, None)
        self.assertNotIn(changed["translation_key"], result["entries"])

    def test_partial_editorial_phrase_set_cannot_fake_full_translation(self):
        for incomplete in (
            {"Schupfnudeln": self.phrases["Schupfnudeln"]},
            {**self.phrases, "Schupfnudeln": {"en": "Potato dumplings"}},
            {**self.phrases, "Gemüse": {"en": "Vegetables", "ko": " "}},
        ):
            with self.subTest(phrases=incomplete):
                result = build_translations(self.menu, {}, incomplete, None)
                self.assertNotIn(self.meal["translation_key"], result["entries"])

    def test_changed_source_or_missing_component_is_rejected(self):
        valid = build_translations(self.menu, {}, self.phrases, None)
        for mutation in (lambda e: e["source"].update(name_de="Another dish"),
                         lambda e: e["ko"].update(components=[]),
                         lambda e: e["en"].update(prices={"student": 1})):
            invalid = copy.deepcopy(valid)
            mutation(invalid["entries"][self.meal["translation_key"]])
            with self.assertRaises(ValueError):
                validate_cache(invalid)

    def test_provider_configuration_is_explicit_and_secure(self):
        self.assertIsNone(model_config({}))
        with self.assertRaises(ValueError):
            model_config({"MENU_TRANSLATION_URL": "https://example.org/chat/completions"})
        with self.assertRaises(ValueError):
            model_config({"MENU_TRANSLATION_URL": "http://example.org/chat/completions", "MENU_TRANSLATION_MODEL": "gemma"})
        config = model_config({"MENU_TRANSLATION_URL": "http://127.0.0.1:11434/v1/chat/completions", "MENU_TRANSLATION_MODEL": "gemma4:e4b"})
        self.assertEqual(config["model"], "gemma4:e4b")

    def test_real_http_protocol_and_cached_reuse(self):
        result = {"en": {"name": "Potato dumplings", "components": ["Vegetables"]},
                  "ko": {"name": "감자 경단", "components": ["채소"]}}
        with provider(json.dumps(result)) as (config, calls):
            cache = build_translations(self.menu, {}, {}, config)
            build_translations(self.menu, cache, {}, config)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "fixture-gemma")
        supplied = json.loads(calls[0]["messages"][1]["content"])
        self.assertNotIn("prices", supplied)
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["ko"], result["ko"])

    def test_invalid_model_response_rejects_entire_candidate(self):
        before = build_translations(self.menu, {}, self.phrases, None)
        untouched = copy.deepcopy(before)
        changed = sample_meal(name="New dish")
        with provider('{"en":{"name":"Dish","components":[]}}') as (config, _):
            with self.assertRaises(ValueError):
                build_translations({"days": [{"meals": [changed]}]}, before, {}, config)
        self.assertEqual(before, untouched)

    def test_ollama_configuration_is_explicit_and_loopback_only(self):
        env = {"MENU_TRANSLATION_PROVIDER": "ollama", "MENU_TRANSLATION_URL": "http://127.0.0.1:11434/api/chat",
               "MENU_TRANSLATION_MODEL": "gemma4:e4b"}
        self.assertEqual(model_config(env).get("provider"), "ollama")
        for url in ("https://example.org/api/chat", "http://192.168.1.8:11434/api/chat",
                    "http://127.0.0.1:11434/v1/chat/completions"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                model_config({**env, "MENU_TRANSLATION_URL": url})
        for provider_name in ("unknown",):
            with self.assertRaises(ValueError):
                model_config({**env, "MENU_TRANSLATION_PROVIDER": provider_name})

    def test_native_ollama_http_request_disables_thinking_and_constrains_the_schema(self):
        result = {"en": {"name": "Potato dumplings", "components": ["Vegetables"]},
                  "ko": {"name": "감자 경단", "components": ["채소"]}}
        response = {"done": True, "done_reason": "stop", "message": {"content": json.dumps(result)}}
        with ollama_provider(response) as (config, calls):
            try:
                cache = build_translations(self.menu, {}, {}, config)
            except ValueError as exc:
                self.fail(f"Valid native response must be accepted: {exc}")
            build_translations(self.menu, cache, {}, config)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], "/api/chat")
        payload = calls[0]["body"]
        self.assertEqual(set(payload), {"model", "messages", "stream", "think", "format", "options", "keep_alive"})
        self.assertEqual(payload["model"], "gemma4:e4b")
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"], {"temperature": 0, "num_ctx": 8192, "num_predict": 4096})
        self.assertEqual(payload["keep_alive"], "5m")
        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
        self.assertEqual(json.loads(payload["messages"][1]["content"]), source_for(self.meal))
        self.assertNotIn("Authorization", calls[0]["headers"])
        text_schema = {"type": "string", "minLength": 1, "maxLength": 1000}
        part_schema = {"type": "object", "additionalProperties": False, "required": ["name", "components"],
                       "properties": {"name": text_schema, "components": {
                           "type": "array", "items": text_schema, "minItems": 1, "maxItems": 1}}}
        self.assertEqual(payload["format"], {"type": "object", "additionalProperties": False,
                         "required": ["en", "ko"], "properties": {"en": part_schema, "ko": part_schema}})
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["ko"], result["ko"])

class GenerationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.meal = sample_meal()
        self.meal["notices"] = ["Weizen"]
        self.menu = {"days": [{"meals": [self.meal]}]}
        self.config = {"model": "fixture-model", "url": "http://127.0.0.1:11434/api/chat", "key": "fixture-key"}
        self.glossary = {"Weizen": {"en": "Wheat", "ko": "밀"}, "vegan": {"en": "Vegan", "ko": "비건"}}
        self.original = {"en": {"name": "Original title", "components": ["Original side"]},
                         "ko": {"name": "기존 이름", "components": ["기존 곁들임"]}}
        self.generated = {"en": {"name": "Refreshed title", "components": ["Refreshed side"]},
                          "ko": {"name": "새 이름", "components": ["새 곁들임"]}}

    def api(self, name):
        function = getattr(translation_contract, name, None)
        self.assertTrue(callable(function), f"Pure {name} API is required")
        return function

    def build(self, previous, config, result, *, glossary=None, phrases=None):
        with patch.object(translations, "load_glossary", return_value=self.glossary if glossary is None else glossary, create=True):
            return build_translations(self.menu, previous, phrases or {}, config,
                                      lambda *args: copy.deepcopy(result))

    def legacy(self, origin="model"):
        return {"schema_version": 1, "entries": {self.meal["translation_key"]: {
            "source": source_for(self.meal), **copy.deepcopy(self.original),
            "origin": origin, "model": self.config["model"], "prompt_version": "menu-v4"}}}

    def test_generated_identity_is_recorded_and_matching_output_is_reused(self):
        previous = self.build({}, self.config, self.original)
        before = copy.deepcopy(previous)
        entry = previous["entries"][self.meal["translation_key"]]
        self.assertIn("generation_identity", entry)
        identity = entry["generation_identity"]
        self.assertEqual(set(identity), {"schema_version", "policy", "glossary", "backend"})
        self.assertIs(type(identity["schema_version"]), int)
        self.assertEqual(identity["schema_version"], 1)
        for field in ("policy", "glossary", "backend"):
            self.assertRegex(identity[field], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(identity, self.api("generation_identity")(self.config, notice_glossary=self.glossary))
        reused = self.build(previous, self.config, self.generated)
        self.assertEqual(reused, previous)
        self.assertEqual(previous, before)

    def test_changed_backend_regenerates_model_output(self):
        previous = self.build({}, self.config, self.original)
        before = copy.deepcopy(previous)
        self.assertEqual(self.build(previous, self.config, self.generated), previous)
        unsupported = copy.deepcopy(previous)
        unsupported["entries"][self.meal["translation_key"]]["prompt_version"] = "menu-v2"
        variants = [("backend", previous, {**self.config, **change}) for change in
                    ({"provider": "ollama"}, {"model": "different-model"},
                     {"url": "http://127.0.0.1:11435/api/chat"})]
        variants.append(("stored-prompt", unsupported, self.config))
        for reason, stored, config in variants:
            with self.subTest(reason=reason, config=config):
                inputs = copy.deepcopy((stored, config))
                cache = self.build(stored, config, self.generated)
                entry = cache["entries"][self.meal["translation_key"]]
                self.assertEqual(entry["en"], self.generated["en"])
                self.assertEqual(entry["ko"], self.generated["ko"])
                self.assertEqual(entry["origin"], "model")
                self.assertEqual(entry["prompt_version"], translation_contract.PROMPT_VERSION)
                self.assertIn("generation_identity", entry)
                if reason == "backend":
                    self.assertNotEqual(entry["generation_identity"]["backend"], previous["entries"][self.meal["translation_key"]]["generation_identity"]["backend"])
                else:
                    self.assertEqual(entry["generation_identity"], previous["entries"][self.meal["translation_key"]]["generation_identity"])
                self.assertEqual((stored, config), inputs)
                self.assertEqual(previous, before)

    def test_changed_policy_versions_and_exact_mappings_regenerate(self):
        for config in (self.config, {**self.config, "backend_revision": "immutable-fixture-v1"}):
            with self.subTest(config=config):
                previous = self.build({}, config, self.original)
                before = copy.deepcopy((previous, config, self.glossary))
                changes = [(field, "test-policy-v2") for field in
                           ("PROMPT_VERSION", "NAME_POLICY_VERSION", "COMPONENT_POLICY_VERSION")]
                for field in ("REVIEWED_DISH_NAMES", "REVIEWED_COMPONENT_NAMES"):
                    mapping = copy.deepcopy(getattr(translation_contract, field))
                    mapping[next(iter(mapping))]["en"] += " revised"
                    changes.append((field, mapping))
                for field, value in changes:
                    with self.subTest(policy=field), patch.object(translation_contract, field, value):
                        cache = self.build(previous, config, self.generated)
                        entry = cache["entries"][self.meal["translation_key"]]
                        self.assertEqual(entry["en"], self.generated["en"])
                        self.assertEqual(entry["ko"], self.generated["ko"])
                        self.assertIn("generation_identity", entry)
                        self.assertNotEqual(entry["generation_identity"]["policy"], previous["entries"][self.meal["translation_key"]]["generation_identity"]["policy"])
                self.assertEqual((previous, config, self.glossary), before)

    def test_changed_complete_glossary_regenerates_even_for_unused_label(self):
        for config in (self.config, {**self.config, "backend_revision": "immutable-fixture-v1"}):
            with self.subTest(config=config):
                previous = self.build({}, config, self.original)
                before = copy.deepcopy((previous, config, self.glossary))
                for label in ("Weizen", "vegan"):
                    with self.subTest(label=label):
                        glossary = copy.deepcopy(self.glossary)
                        glossary[label]["ko"] += " 수정"
                        cache = self.build(previous, config, self.generated, glossary=glossary)
                        entry = cache["entries"][self.meal["translation_key"]]
                        self.assertEqual(entry["en"], self.generated["en"])
                        self.assertEqual(entry["ko"], self.generated["ko"])
                        self.assertEqual(cache["notices"], {"Weizen": glossary["Weizen"]})
                        self.assertIn("generation_identity", entry)
                        self.assertNotEqual(entry["generation_identity"]["glossary"], previous["entries"][self.meal["translation_key"]]["generation_identity"]["glossary"])
                self.assertEqual((previous, config, self.glossary), before)

    def test_mapping_order_key_rotation_and_unrelated_config_preserve_reuse(self):
        previous = self.build({}, self.config, self.original)
        reordered = {key: dict(reversed(tuple(value.items()))) for key, value in reversed(tuple(self.glossary.items()))}
        dishes = dict(reversed(tuple(translation_contract.REVIEWED_DISH_NAMES.items())))
        components = {key: dict(reversed(tuple(value.items()))) for key, value in translation_contract.REVIEWED_COMPONENT_NAMES.items()}
        config = {**self.config, "provider": "openai", "key": "rotated-fixture-key", "timeout": 99}
        with patch.object(translation_contract, "REVIEWED_DISH_NAMES", dishes), patch.object(translation_contract, "REVIEWED_COMPONENT_NAMES", components):
            reused = self.build(previous, config, self.generated, glossary=reordered)
        self.assertEqual(reused, previous)
        self.assertIn("generation_identity", reused["entries"][self.meal["translation_key"]])

    def test_configured_legacy_model_regenerates_offline_legacy_is_not_stamped(self):
        previous = self.legacy()
        before = copy.deepcopy(previous)
        with patch.object(translation_service, "generation_identity", side_effect=lambda *args, **kwargs: self.fail("Offline retention cannot compute current model provenance")):
            retained = self.build(previous, None, self.generated)
        self.assertEqual(retained["entries"], previous["entries"])
        self.assertNotIn("generation_identity", retained["entries"][self.meal["translation_key"]])
        for config in (self.config, {**self.config, "backend_revision": "immutable-fixture-v1"}):
            with self.subTest(config=config):
                inputs = copy.deepcopy(config)
                regenerated = self.build(previous, config, self.generated)
                entry = regenerated["entries"][self.meal["translation_key"]]
                self.assertEqual(entry["en"], self.generated["en"])
                self.assertEqual(entry["ko"], self.generated["ko"])
                self.assertIn("generation_identity", entry)
                self.assertEqual(config, inputs)
                self.assertEqual(previous, before)
                self.assertNotIn("generation_identity", previous["entries"][self.meal["translation_key"]])

    def test_explicit_identity_only_changes_model_reuse_and_preserves_old_api(self):
        identity = {"schema_version": 1, "policy": "a" * 64, "glossary": "b" * 64, "backend": "c" * 64}
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            for stored in (None, identity, {**identity, "backend": "d" * 64}):
                with self.subTest(origin=origin, stored=stored):
                    entry = self.legacy(origin)["entries"][self.meal["translation_key"]]
                    if stored is not None:
                        entry["generation_identity"] = copy.deepcopy(stored)
                    self.assertTrue(translation_contract.reusable_translation(entry, self.config["model"]))
                    try:
                        reusable = translation_contract.reusable_translation(entry, self.config["model"], identity=identity)
                    except TypeError as exc:
                        self.fail(f"Identity-aware reuse API is required: {exc}")
                    self.assertEqual(reusable, origin != "model" or stored == identity)
                    entry["prompt_version"] = "unsupported"
                    self.assertFalse(translation_contract.reusable_translation(entry, self.config["model"], identity=identity))
        entry = self.legacy()["entries"][self.meal["translation_key"]]
        entry["generation_identity"] = identity
        try:
            reusable = translation_contract.reusable_translation(entry, "different-model", identity=identity)
        except TypeError as exc:
            self.fail(f"Identity-aware reuse API is required: {exc}")
        self.assertFalse(reusable)

    def test_identity_api_defaults_and_glossary_validation(self):
        make_identity = self.api("generation_identity")
        minimal = {"model": "fixture"}
        identity = make_identity(minimal, notice_glossary=self.glossary)
        self.assertEqual(identity, make_identity({**minimal, "provider": "openai", "url": "", "key": "ignored"}, notice_glossary=self.glossary))
        backend = {"provider": "openai", "model": "fixture", "url": ""}
        digest = hashlib.sha256(json.dumps(backend, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(identity["backend"], digest)
        for malformed in (None, [], {"Weizen": {"en": "Wheat"}}, {"Weizen ": {"en": "Wheat", "ko": ""}}):
            with self.subTest(glossary=malformed), self.assertRaises(ValueError):
                make_identity(minimal, notice_glossary=malformed)

    def test_backend_revision_reuses_model_output_across_endpoint_and_key_changes(self):
        config = {**self.config, "backend_revision": "immutable-fixture-v1"}
        previous = self.build({}, config, self.original)
        before = copy.deepcopy(previous)
        relocated = {**config, "url": "http://127.0.0.1:39217/api/chat", "key": "rotated-fixture-key"}
        reused = self.build(previous, relocated, self.generated)
        self.assertEqual(reused, previous)
        self.assertEqual(previous, before)
        self.assertEqual(set(reused["entries"][self.meal["translation_key"]]["generation_identity"]),
                         {"schema_version", "policy", "glossary", "backend"})
        self.assertNotIn(config["backend_revision"], json.dumps(reused))
        self.assertNotIn(config["key"], json.dumps(reused))
        self.assertNotIn(relocated["key"], json.dumps(reused))

    def test_changed_declared_backend_revision_provider_or_model_regenerates(self):
        config = {**self.config, "backend_revision": "immutable-fixture-v1"}
        previous = self.build({}, config, self.original)
        before = copy.deepcopy(previous)
        for change in ({"backend_revision": "immutable-fixture-v2"},
                       {"provider": "ollama"}, {"model": "different-model"}):
            with self.subTest(change=change):
                cache = self.build(previous, {**config, **change}, self.generated)
                entry = cache["entries"][self.meal["translation_key"]]
                self.assertEqual(entry["en"], self.generated["en"])
                self.assertEqual(entry["ko"], self.generated["ko"])
                self.assertNotEqual(entry["generation_identity"]["backend"],
                                    previous["entries"][self.meal["translation_key"]]["generation_identity"]["backend"])
                self.assertEqual(previous, before)

    def test_declared_backend_revision_preserves_exact_string_and_excludes_url_and_secrets(self):
        config = {**self.config, "provider": "ollama", "backend_revision": "  fixture-불변-v1\t"}
        before = copy.deepcopy(config)
        identity = translation_contract.generation_identity(config, notice_glossary=self.glossary)
        backend = {"provider": "ollama", "model": config["model"],
                   "backend_revision": config["backend_revision"]}
        expected = hashlib.sha256(json.dumps(backend, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(identity["backend"], expected)
        stripped = translation_contract.generation_identity(
            {**config, "backend_revision": config["backend_revision"].strip()}, notice_glossary=self.glossary)
        self.assertNotEqual(identity["backend"], stripped["backend"])
        self.assertEqual(config, before)

    def test_malformed_backend_revision_is_rejected_in_identity_and_builder(self):
        for revision in (None, True, False, 0, 1.5, [], {}, "", " ", "\t\n"):
            with self.subTest(revision=revision):
                config = {**self.config, "backend_revision": revision}
                before = copy.deepcopy(config)
                with self.assertRaisesRegex(ValueError, "backend_revision"):
                    translation_contract.generation_identity(config, notice_glossary=self.glossary)
                with self.assertRaisesRegex(ValueError, "backend_revision"):
                    self.build({}, config, self.generated)
                self.assertEqual(config, before)

    def test_absent_backend_revision_preserves_exact_legacy_backend_digest(self):
        for config in ({"model": "fixture"}, self.config,
                       {**self.config, "provider": "ollama", "model": "모델", "url": "http://127.0.0.1:39217/api/chat"}):
            with self.subTest(config=config):
                backend = {"provider": config.get("provider", "openai"),
                           "model": config["model"], "url": config.get("url", "")}
                expected = hashlib.sha256(json.dumps(backend, ensure_ascii=False, sort_keys=True,
                                                     separators=(",", ":")).encode("utf-8")).hexdigest()
                identity = translation_contract.generation_identity(config, notice_glossary=self.glossary)
                self.assertEqual(identity["backend"], expected)

    def test_malformed_identity_is_rejected_for_every_origin_without_input_mutation(self):
        valid = {"schema_version": 1, "policy": "a" * 64, "glossary": "b" * 64, "backend": "c" * 64}
        malformed = [None, [], {}, {**valid, "extra": "field"},
                     {key: value for key, value in valid.items() if key != "backend"}]
        malformed += [{**valid, "schema_version": value} for value in (True, 2, "1", 1.0)]
        malformed += [{**valid, field: value} for field in ("policy", "glossary", "backend")
                      for value in (7, "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n")]
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            for value in malformed:
                with self.subTest(origin=origin, identity=value):
                    previous = self.legacy(origin)
                    previous["entries"][self.meal["translation_key"]]["generation_identity"] = value
                    before = copy.deepcopy(previous)
                    with self.assertRaises(ValueError):
                        validate_cache(previous)
                    with self.assertRaises(ValueError):
                        self.build(previous, self.config, self.generated)
                    self.assertEqual(previous, before)

    def test_valid_optional_identity_is_accepted_for_every_origin(self):
        validate_identity = self.api("validate_generation_identity")
        identity = {"schema_version": 1, "policy": "a" * 64, "glossary": "b" * 64, "backend": "c" * 64}
        validate_identity(identity)
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            with self.subTest(origin=origin):
                previous = self.legacy(origin)
                previous["entries"][self.meal["translation_key"]]["generation_identity"] = copy.deepcopy(identity)
                validate_cache(previous)

    def test_builder_uses_one_glossary_snapshot_for_notices_and_model_identity(self):
        make_identity = self.api("generation_identity")
        other = copy.deepcopy(self.glossary)
        other["Weizen"]["ko"] = "다른 밀"
        with (patch.object(translations, "load_glossary", side_effect=[self.glossary, other], create=True) as load,
              patch.object(translation_service, "translate_notice_labels", wraps=translation_service.translate_notice_labels) as translate_notices,
              patch.object(translation_service, "generation_identity", wraps=make_identity) as identity_call):
            cache = build_translations(self.menu, {}, {}, self.config,
                                       translate=lambda *args: copy.deepcopy(self.generated))
        self.assertEqual(cache["notices"], {"Weizen": self.glossary["Weizen"]})
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["generation_identity"],
                         make_identity(self.config, notice_glossary=self.glossary))
        self.assertEqual(load.call_count, 1)
        self.assertIs(translate_notices.call_args.args[1], self.glossary)
        self.assertIs(identity_call.call_args.kwargs["notice_glossary"], self.glossary)

    def test_notice_adapter_validates_explicit_mapping_and_keeps_exact_label_errors(self):
        try:
            selected = notices.translated_notices(self.menu, glossary=self.glossary)
        except TypeError as exc:
            self.fail(f"Notice adapter must accept a validated snapshot: {exc}")
        self.assertEqual(selected, {"Weizen": self.glossary["Weizen"]})
        selected["Weizen"]["en"] = "Edited copy"
        self.assertEqual(self.glossary["Weizen"]["en"], "Wheat")
        for malformed in ([], {"Weizen": {"en": "Wheat"}}):
            with self.subTest(glossary=malformed), self.assertRaises(ValueError):
                notices.translated_notices(self.menu, glossary=malformed)
        with self.assertRaisesRegex(ValueError, "Unknown notice labels: 'Weizen'.*notice-translations.json before retrying"):
            notices.translated_notices(self.menu, glossary={})

    def test_complete_editorial_phrases_replace_model_identity_without_acquiring_it(self):
        previous = self.build({}, self.config, self.original)
        phrases = {"Schupfnudeln": {"en": "Editorial title", "ko": "편집 이름"},
                   "Gemüse": {"en": "Editorial side", "ko": "편집 곁들임"}}
        cache = self.build(previous, self.config, self.generated, phrases=phrases)
        entry = cache["entries"][self.meal["translation_key"]]
        self.assertEqual(entry["en"], {"name": "Editorial title", "components": ["Editorial side"]})
        self.assertEqual(entry["ko"], {"name": "편집 이름", "components": ["편집 곁들임"]})
        self.assertEqual(entry["origin"], "editorial-draft")
        self.assertNotIn("generation_identity", entry)


class TranslationCheckpointTests(unittest.TestCase):
    class InjectedGenerationFailure(RuntimeError):
        pass

    class InjectedCheckpointFailure(RuntimeError):
        pass

    def setUp(self):
        self.a = sample_meal("First dish")
        self.b = sample_meal("Second dish")
        self.menu = {"days": [{"meals": [self.a, self.b]}]}
        self.config = {"model": "fixture-model", "url": "https://fixture.invalid/chat", "key": "fixture-key"}
        self.glossary = {"Weizen": {"en": "Wheat", "ko": "밀"},
                         "vegan": {"en": "Vegan", "ko": "비건"}}
        self.result = {"en": {"name": "Translated dish", "components": ["Vegetables"]},
                       "ko": {"name": "번역한 요리", "components": ["채소"]}}

    def build(self, menu, previous, phrases, config, translate=None, **kwargs):
        with patch.object(translations, "load_glossary", return_value=self.glossary):
            try:
                return build_translations(menu, previous, phrases, config,
                                          translate=translate, **kwargs)
            except TypeError as exc:
                if "unexpected keyword argument 'checkpoint'" in str(exc):
                    self.fail(f"Accepted-translation checkpoint API is required: {exc}")
                raise

    def generate(self, source, config):
        return copy.deepcopy(self.result)

    def test_partial_checkpoint_survives_failure_and_retry_requests_only_missing_dish(self):
        previous, phrases = {"schema_version": 1, "entries": {}}, {}
        inputs = copy.deepcopy((self.menu, previous, phrases, self.config))
        snapshots, calls = [], []
        failure = self.InjectedGenerationFailure("Second dish failed")

        def interrupted(source, config):
            calls.append(source)
            if source == source_for(self.b):
                raise failure
            return copy.deepcopy(self.result)

        with self.assertRaises(self.InjectedGenerationFailure) as raised:
            self.build(self.menu, previous, phrases, self.config, interrupted,
                       checkpoint=snapshots.append)
        self.assertIs(raised.exception, failure)
        self.assertEqual(calls, [source_for(self.a), source_for(self.b)])
        self.assertEqual(len(snapshots), 1)
        validate_cache(snapshots[0])
        self.assertEqual(set(snapshots[0]["entries"]), {self.a["translation_key"]})
        self.assertIn("generation_identity", snapshots[0]["entries"][self.a["translation_key"]])
        self.assertEqual((self.menu, previous, phrases, self.config), inputs)
        calls.clear()

        def resumed(source, config):
            calls.append(source)
            return copy.deepcopy(self.result)

        final = self.build(self.menu, snapshots[0], phrases, self.config, resumed)
        self.assertEqual(calls, [source_for(self.b)])
        self.assertEqual(set(final["entries"]), {self.a["translation_key"], self.b["translation_key"]})
        self.assertEqual(set(snapshots[0]["entries"]), {self.a["translation_key"]})

    def test_each_accepted_entry_has_an_independent_complete_cache_snapshot(self):
        snapshots = []
        final = self.build(self.menu, {}, {}, self.config, self.generate,
                           checkpoint=snapshots.append)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(set(snapshots[0]["entries"]), {self.a["translation_key"]})
        self.assertEqual(snapshots[1], final)
        for snapshot in snapshots:
            validate_cache(snapshot)
        final["entries"][self.a["translation_key"]]["en"]["components"][0] = "Changed returned cache"
        self.assertEqual(snapshots[1]["entries"][self.a["translation_key"]]["en"]["components"], ["Vegetables"])

    def test_invalid_schema_or_semantic_rejection_does_not_checkpoint_rejected_entry(self):
        invalid_schema = copy.deepcopy(self.result)
        invalid_schema["ko"]["components"] = []
        invalid_policy = copy.deepcopy(self.result)
        invalid_policy["en"]["name"] = "Vegan curry (mensaVital)"
        for rejected in (invalid_schema, invalid_policy):
            with self.subTest(rejected=rejected):
                snapshots = []

                def generate(source, config):
                    return copy.deepcopy(self.result if source == source_for(self.a) else rejected)

                with self.assertRaises(ValueError):
                    self.build(self.menu, {}, {}, self.config, generate,
                               checkpoint=snapshots.append)
                self.assertEqual(len(snapshots), 1)
                validate_cache(snapshots[0])
                self.assertEqual(set(snapshots[0]["entries"]), {self.a["translation_key"]})

    def test_invalid_provenance_is_rejected_before_callback(self):
        snapshots = []
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.build(self.menu, {}, {}, {**self.config, "model": " "}, self.generate,
                       checkpoint=snapshots.append)
        self.assertEqual(snapshots, [])

    def test_checkpoint_failure_propagates_and_aborts_without_mutating_inputs(self):
        previous, phrases = {"schema_version": 1, "entries": {}}, {}
        inputs = copy.deepcopy((self.menu, previous, phrases, self.config))
        calls, snapshots = [], []
        failure = self.InjectedCheckpointFailure("Private cache write failed")

        def generate(source, config):
            calls.append(source)
            return copy.deepcopy(self.result)

        def checkpoint(cache):
            validate_cache(cache)
            snapshots.append(cache)
            cache["entries"].clear()
            raise failure

        with self.assertRaises(self.InjectedCheckpointFailure) as raised:
            self.build(self.menu, previous, phrases, self.config, generate,
                       checkpoint=checkpoint)
        self.assertIs(raised.exception, failure)
        self.assertEqual(calls, [source_for(self.a)])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual((self.menu, previous, phrases, self.config), inputs)

    def test_callback_mutation_cannot_change_builder_cache_or_later_snapshot(self):
        snapshots = []

        def mutate(cache):
            snapshots.append(copy.deepcopy(cache))
            cache["entries"][self.a["translation_key"]]["en"]["components"][0] = "Callback edit"
            cache["entries"][self.a["translation_key"]]["generation_identity"]["policy"] = "0" * 64
            cache["entries"].clear()
            cache["notices"]["Weizen"]["en"] = "Callback notice edit"

        self.a["notices"] = ["Weizen"]
        final = self.build(self.menu, {}, {}, self.config, self.generate, checkpoint=mutate)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[-1], final)
        self.assertEqual(final["entries"][self.a["translation_key"]]["en"]["components"], ["Vegetables"])
        self.assertEqual(final["notices"]["Weizen"], self.glossary["Weizen"])
        validate_cache(final)

    def test_editorial_replacement_checkpoints_after_policies_with_full_priority(self):
        meal = sample_meal("Wikingertopf", sides=("Peperoni",))
        menu = {"days": [{"meals": [meal]}]}
        previous = self.build(menu, {}, {}, self.config, self.generate)
        phrases = {"Wikingertopf": {"en": "Wikingertopf", "ko": "비킹어토프"},
                   "Peperoni": {"en": "Peperoni", "ko": "페페로니"}}
        inputs = copy.deepcopy((previous, phrases, self.config))
        snapshots = []
        final = self.build(menu, previous, phrases, self.config,
                           lambda *args: self.fail("Complete editorial phrases must avoid inference"),
                           checkpoint=snapshots.append)
        self.assertEqual(snapshots, [final])
        entry = snapshots[0]["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"], {"name": "Meatball stew", "components": ["Chili peppers"]})
        self.assertEqual(entry["ko"], {"name": "고기완자 스튜", "components": ["고추"]})
        self.assertEqual(entry["origin"], "editorial-draft")
        self.assertNotIn("generation_identity", entry)
        self.assertEqual((previous, phrases, self.config), inputs)
        validate_cache(snapshots[0])

    def test_checkpoint_retains_historical_entries_notices_and_recorded_identity(self):
        historical = sample_meal("Historical dish")
        historical["notices"] = ["vegan"]
        previous = self.build({"days": [{"meals": [historical]}]}, {}, {},
                              self.config, self.generate)
        before = copy.deepcopy(previous)
        self.a["notices"] = ["Weizen"]
        snapshots = []
        final = self.build(self.menu, previous, {}, self.config, self.generate,
                           checkpoint=snapshots.append)
        for snapshot in snapshots:
            self.assertEqual(snapshot["entries"][historical["translation_key"]],
                             previous["entries"][historical["translation_key"]])
            self.assertEqual(snapshot["notices"], self.glossary)
            validate_cache(snapshot)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[-1], final)
        self.assertEqual(previous, before)

    def test_all_reused_entries_do_not_checkpoint_with_incomplete_editorial_phrases(self):
        previous = self.build(self.menu, {}, {}, self.config, self.generate)
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            with self.subTest(origin=origin):
                retained = copy.deepcopy(previous)
                for entry in retained["entries"].values():
                    entry["origin"] = origin
                snapshots = []
                final = self.build(self.menu, retained, {"First dish": {"en": "Incomplete"}},
                                   self.config, lambda *args: self.fail("Compatible entries must be reused"),
                                   checkpoint=snapshots.append)
                self.assertEqual(final, retained)
                self.assertEqual(snapshots, [])

    def test_omitted_or_none_checkpoint_preserves_default_behavior(self):
        default = self.build(self.menu, {}, {}, self.config, self.generate)
        explicit = self.build(self.menu, {}, {}, self.config, self.generate, checkpoint=None)
        self.assertEqual(explicit, default)
        unknown = self.build(self.menu, {}, {}, None,
                             lambda *args: self.fail("Unconfigured builds must avoid inference"))
        self.assertEqual(unknown["entries"], {})
        validate_cache(default)

    def test_noncallable_checkpoint_is_rejected_before_translation(self):
        for checkpoint in (False, 0, "callback", [], {}):
            with self.subTest(checkpoint=checkpoint):
                inputs = copy.deepcopy((self.menu, self.config))
                with self.assertRaisesRegex(TypeError, "checkpoint.*callable"):
                    self.build(self.menu, {}, {}, self.config,
                               lambda *args: self.fail("Invalid checkpoint must fail before inference"),
                               checkpoint=checkpoint)
                self.assertEqual((self.menu, self.config), inputs)

    def test_provider_result_is_owned_before_deterministic_policy_correction(self):
        meal = sample_meal("Wikingertopf", sides=("Peperoni",))
        supplied = {"en": {"name": "Wikingertopf", "components": ["Peperoni"]},
                    "ko": {"name": "비킹어토프", "components": ["페페로니"]}}
        original = copy.deepcopy(supplied)
        snapshots = []
        final = self.build({"days": [{"meals": [meal]}]}, {}, {}, self.config,
                           lambda *args: supplied, checkpoint=snapshots.append)
        self.assertEqual(supplied, original)
        self.assertEqual(snapshots, [final])
        entry = final["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"], {"name": "Meatball stew", "components": ["Chili peppers"]})
        self.assertEqual(entry["ko"], {"name": "고기완자 스튜", "components": ["고추"]})
        supplied["ko"]["components"][0] = "Later provider edit"
        self.assertEqual(entry["ko"]["components"], ["고추"])


class SemanticNamePolicyTests(unittest.TestCase):
    def fixture_identity(self, config):
        """fixture_identity는 현재 번역 규칙과 용어집으로 생성한 모델 번역 예시를 식별하도록 해당 식별 정보를 반환합니다."""
        return translation_contract.generation_identity(config, notice_glossary=notices.load_glossary())

    def legacy_cache(self, meal, en_name, ko_name, origin="model"):
        return {"schema_version": 1, "entries": {meal["translation_key"]: {
            "source": source_for(meal),
            "en": {"name": en_name, "components": ["Original side"] * len(meal["components"])},
            "ko": {"name": ko_name, "components": ["기존 곁들임"] * len(meal["components"])},
            "origin": origin, "model": "original-generator", "prompt_version": "menu-v3"}}}

    def test_cached_reviewed_dishes_migrate_without_model_or_component_changes(self):
        for source_name, old_en, old_ko, new_en, new_ko in (
            ("Wikingertopf", "Wikingertopf (stew)", "Wikingertopf (스튜)", "Meatball stew", "고기완자 스튜"),
            ("Köttbullar", "Köttbullar (Swedish meatballs)", "Köttbullar (스웨덴식 미트볼)",
             "Swedish meatballs", "스웨덴식 미트볼"),
            ("Kaisergemüse mit gebackenem Tofu und Vollkornpasta",
             "Imperial vegetables with cooked tofu and whole grain pasta",
             "조리한 두부와 통곡물 파스타를 곁들인 임페리얼 야채",
             "Mixed vegetables with cooked tofu and whole-grain pasta",
             "조리한 두부와 통곡물 파스타를 곁들인 모둠 채소"),
            ("Veganer Cornflakes Taler Chicken Style",
             "Vegan Cornflakes-crusted Chicken-style Patty", "비건 콘플렉스 치킨 스타일 패티",
             "Vegan cornflake-crusted chicken-style patty", "콘플레이크를 입힌 비건 치킨 스타일 패티"),
        ):
            with self.subTest(source=source_name):
                meal = sample_meal(source_name)
                meal["notices"] = ["Weizen"]
                meal["components"][0]["notices"] = ["Milch und Laktose"]
                menu = {"days": [{"meals": [meal]}]}
                previous = self.legacy_cache(meal, old_en, old_ko)
                original_menu, original_cache = copy.deepcopy(menu), copy.deepcopy(previous)
                cache = build_translations(menu, previous, {}, None,
                                           translate=lambda *args: self.fail("Migration must not call a model"))
                expected = copy.deepcopy(previous["entries"][meal["translation_key"]])
                expected["en"]["name"], expected["ko"]["name"] = new_en, new_ko
                expected["name_policy_version"] = "semantic-names-v1"
                self.assertEqual(cache["entries"][meal["translation_key"]], expected)
                self.assertEqual(menu, original_menu)
                self.assertEqual(previous, original_cache)
                validate_cache(cache)

    def test_reviewed_component_migrates_cached_source_without_retranslating_other_foods(self):
        meal = sample_meal("Salatbuffet", sides=("Gurken", "Peperoni", "Pepperoni-Salami"))
        meal["components"][1]["notices"] = ["Senf"]
        menu = {"days": [{"meals": [meal]}]}
        previous = self.legacy_cache(meal, "Salad buffet", "샐러드 뷔페")
        entry = previous["entries"][meal["translation_key"]]
        entry["en"]["components"] = ["Cucumber", "Peperoni", "Pepperoni sausage"]
        entry["ko"]["components"] = ["오이", "페페로니", "페퍼로니 소시지"]
        original_menu, original_cache = copy.deepcopy(menu), copy.deepcopy(previous)
        cache = build_translations(menu, previous, {}, None,
                                   translate=lambda *args: self.fail("Review must not run inference"))
        expected = copy.deepcopy(entry)
        expected["en"]["components"][1] = "Chili peppers"
        expected["ko"]["components"][1] = "고추"
        expected["component_policy_version"] = "semantic-components-v1"
        self.assertEqual(cache["entries"][meal["translation_key"]], expected)
        self.assertEqual(menu, original_menu)
        self.assertEqual(previous, original_cache)
        self.assertEqual(build_translations(menu, cache, {}, None), cache)
        validate_cache(cache)

    def test_reviewed_component_applies_to_new_model_output(self):
        meal = sample_meal("Salatbuffet", sides=("Peperoni",))
        result = {"en": {"name": "Salad buffet", "components": ["Peperoni"]},
                  "ko": {"name": "샐러드 뷔페", "components": ["페페로니"]}}
        cache = build_translations({"days": [{"meals": [meal]}]}, {}, {}, {"model": "fixture"},
                                   translate=lambda *args: copy.deepcopy(result))
        entry = cache["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"]["components"], ["Chili peppers"])
        self.assertEqual(entry["ko"]["components"], ["고추"])
        self.assertEqual(entry["origin"], "model")
        self.assertEqual(entry["model"], "fixture")
        validate_cache(cache)

    def test_publication_rejects_unreviewed_component_wording(self):
        meal = sample_meal("Salatbuffet", sides=("Peperoni",))
        previous = self.legacy_cache(meal, "Salad buffet", "샐러드 뷔페")
        with self.assertRaisesRegex(ValueError, "[Cc]omponent.*reviewed"):
            validate_cache(previous)

    def test_cached_brand_chains_are_removed_while_food_and_dietary_terms_survive(self):
        meal = sample_meal("KlimaTeller: Vegan: Curry")
        previous = self.legacy_cache(meal, "  MeNsAvItAl: KlimaTeller: Vegan curry",
                                     "KLIMATELLER: mensavital: 비건 커리", origin="reviewed-draft")
        cache = build_translations({"days": [{"meals": [meal]}]}, previous, {}, None)
        entry = cache["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"]["name"], "Vegan curry")
        self.assertEqual(entry["ko"]["name"], "비건 커리")
        self.assertEqual(entry["source"], source_for(meal))
        self.assertEqual(entry["origin"], "reviewed-draft")
        self.assertEqual(entry["prompt_version"], "menu-v3")
        self.assertEqual(entry["name_policy_version"], "semantic-names-v1")

    def test_unused_legacy_entries_are_normalized_for_snapshot_compatibility(self):
        meal = sample_meal("Wikingertopf")
        previous = self.legacy_cache(meal, "Wikingertopf (stew)", "Wikingertopf (스튜)")
        cache = build_translations({"days": []}, previous, {}, None)
        self.assertEqual(cache["entries"][meal["translation_key"]]["en"]["name"], "Meatball stew")
        validate_cache(cache)

    def test_complete_editorial_phrases_replace_reusable_cache_of_every_origin(self):
        meal = sample_meal(sides=("Gemüse", "Reis"))
        meal["notices"] = ["Weizen"]
        meal["components"][0]["notices"] = ["Milch und Laktose"]
        meal["components"][1]["notices"] = ["Senf"]
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Schupfnudeln": {"en": "Corrected potato dumplings", "ko": "수정한 감자 경단"},
                   "Gemüse": {"en": "Corrected vegetables", "ko": "수정한 채소"},
                   "Reis": {"en": "Corrected rice", "ko": "수정한 밥"}}
        historical = sample_meal("Previous dish")
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            for config in (None, {"model": "original-generator"}):
                with self.subTest(origin=origin, config=config):
                    previous = self.legacy_cache(meal, "Potato dumplings", "감자 경단", origin)
                    historical_entry = self.legacy_cache(historical, "Previous dish", "이전 요리")["entries"]
                    previous["entries"].update(historical_entry)
                    previous["notices"] = {"vegan": {"en": "Vegan", "ko": "비건"}}
                    inputs = copy.deepcopy((menu, previous, phrases, config))
                    cache = build_translations(menu, previous, phrases, config,
                                               translate=lambda *args: self.fail("Complete editorial phrases must avoid inference"))
                    self.assertEqual(cache["entries"][meal["translation_key"]], {
                        "source": source_for(meal),
                        "en": {"name": "Corrected potato dumplings", "components": ["Corrected vegetables", "Corrected rice"]},
                        "ko": {"name": "수정한 감자 경단", "components": ["수정한 채소", "수정한 밥"]},
                        "origin": "editorial-draft", "model": "assistant-draft", "prompt_version": "menu-v4"})
                    self.assertEqual(cache["entries"][historical["translation_key"]], historical_entry[historical["translation_key"]])
                    self.assertEqual(cache["notices"], {
                        "vegan": {"en": "Vegan", "ko": "비건"},
                        "Weizen": {"en": "Wheat", "ko": "밀"},
                        "Milch und Laktose": {"en": "Milk and lactose", "ko": "우유 및 유당"},
                        "Senf": {"en": "Mustard", "ko": "겨자"}})
                    self.assertEqual((menu, previous, phrases, config), inputs)

    def test_configured_editorial_correction_replaces_migrated_v3_components_and_applies_policies(self):
        meal = sample_meal("Wikingertopf", sides=("Gemüse", "Peperoni"))
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Wikingertopf": {"en": "Wikingertopf", "ko": "비킹어토프"},
                   "Gemüse": {"en": "Corrected editorial side", "ko": "수정한 편집 곁들임"},
                   "Peperoni": {"en": "Peperoni", "ko": "페페로니"}}
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            with self.subTest(origin=origin):
                previous = self.legacy_cache(meal, "Wikingertopf (stew)", "Wikingertopf (스튜)", origin)
                migrated = build_translations(menu, previous, {}, None)
                cache = build_translations(menu, migrated, phrases, {"model": "original-generator"},
                                           translate=lambda *args: self.fail("Complete editorial phrases must avoid inference"))
                self.assertEqual(cache["entries"][meal["translation_key"]], {
                    "source": source_for(meal),
                    "en": {"name": "Meatball stew", "components": ["Corrected editorial side", "Chili peppers"]},
                    "ko": {"name": "고기완자 스튜", "components": ["수정한 편집 곁들임", "고추"]},
                    "origin": "editorial-draft", "model": "assistant-draft", "prompt_version": "menu-v4",
                    "name_policy_version": "semantic-names-v1", "component_policy_version": "semantic-components-v1"})

    def test_changed_complete_phrases_replace_previous_editorial_cache(self):
        meal = sample_meal()
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Schupfnudeln": {"en": "Potato dumplings", "ko": "감자 경단"},
                   "Gemüse": {"en": "Vegetables", "ko": "채소"}}
        previous = build_translations(menu, {}, phrases, None)
        before = copy.deepcopy(previous)
        phrases["Schupfnudeln"] = {"en": "German potato dumplings", "ko": "독일식 감자 경단"}
        phrases["Gemüse"] = {"en": "Vegetable side", "ko": "채소 곁들임"}
        cache = build_translations(menu, previous, phrases, None)
        entry = cache["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"], {"name": "German potato dumplings", "components": ["Vegetable side"]})
        self.assertEqual(entry["ko"], {"name": "독일식 감자 경단", "components": ["채소 곁들임"]})
        self.assertEqual((entry["origin"], entry["model"], entry["prompt_version"]),
                         ("editorial-draft", "assistant-draft", "menu-v4"))
        self.assertEqual(previous, before)

    def test_incomplete_editorial_phrases_reuse_compatible_cache_without_partial_overlay(self):
        meal = sample_meal(sides=("Gemüse", "Reis"))
        menu = {"days": [{"meals": [meal]}]}
        complete = {"Schupfnudeln": {"en": "Corrected title", "ko": "수정한 이름"},
                    "Gemüse": {"en": "Corrected side", "ko": "수정한 곁들임"},
                    "Reis": {"en": "Rice", "ko": "밥"}}
        for incomplete in (
            {text: value for text, value in complete.items() if text != "Reis"},
            {**complete, "Schupfnudeln": {"en": "Corrected title"}},
            {**complete, "Reis": {"en": "Rice", "ko": " "}},
        ):
            for origin in ("model", "editorial-draft", "reviewed-draft"):
                for config in (None, {"model": "original-generator"}):
                    with self.subTest(phrases=incomplete, origin=origin, config=config):
                        previous = self.legacy_cache(meal, "Cached title", "기존 이름", origin)
                        if origin == "model" and config:
                            previous["entries"][meal["translation_key"]]["generation_identity"] = self.fixture_identity(config)
                        cache = build_translations(menu, previous, incomplete, config,
                                                   translate=lambda *args: self.fail("Compatible cache must avoid inference"))
                        self.assertEqual(cache["entries"][meal["translation_key"]], previous["entries"][meal["translation_key"]])

    def test_incomplete_editorial_phrases_use_configured_generator_without_partial_overlay(self):
        meal = sample_meal()
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Schupfnudeln": {"en": "Editorial title", "ko": "편집 이름"},
                   "Gemüse": {"en": "Editorial side"}}
        generated = {"en": {"name": "Generated title", "components": ["Generated side"]},
                     "ko": {"name": "생성한 이름", "components": ["생성한 곁들임"]}}
        for cached in (False, True):
            with self.subTest(incompatible_cache=cached):
                previous = self.legacy_cache(meal, "Old title", "이전 이름") if cached else {}
                config = {"model": "new-generator"}
                inputs = copy.deepcopy((menu, previous, phrases, config))
                def translate(source, selected_config):
                    self.assertEqual(source, source_for(meal))
                    self.assertEqual(selected_config, config)
                    return copy.deepcopy(generated)
                cache = build_translations(menu, previous, phrases, config, translate=translate)
                self.assertEqual(cache["entries"][meal["translation_key"]], {
                    "source": source_for(meal), **generated,
                    "origin": "model", "model": "new-generator", "prompt_version": "menu-v4",
                    "generation_identity": self.fixture_identity(config)})
                self.assertEqual((menu, previous, phrases, config), inputs)

    def test_configured_build_reuses_v3_model_cache_without_editorial_phrases(self):
        meal = sample_meal()
        previous = self.legacy_cache(meal, "Potato dumplings", "감자 경단")
        config = {"model": "original-generator"}
        previous["entries"][meal["translation_key"]]["generation_identity"] = self.fixture_identity(config)
        cache = build_translations({"days": [{"meals": [meal]}]}, previous, {}, config,
                                   translate=lambda *args: self.fail("Compatible cache must not call a model"))
        self.assertEqual(cache["entries"], previous["entries"])

    def test_malformed_editorial_entries_follow_incomplete_phrase_fallback(self):
        meal = sample_meal()
        menu = {"days": [{"meals": [meal]}]}
        complete = {"Schupfnudeln": {"en": "Editorial title", "ko": "편집 이름"},
                    "Gemüse": {"en": "Editorial side", "ko": "편집 곁들임"}}
        generated = {"en": {"name": "Generated title", "components": ["Generated side"]},
                     "ko": {"name": "생성한 이름", "components": ["생성한 곁들임"]}}
        for text in complete:
            for malformed in (None, [], "invalid entry", 7):
                for fallback in ("cached-unconfigured", "cached-configured", "absent", "generated"):
                    with self.subTest(text=text, entry=malformed, fallback=fallback):
                        phrases = {**complete, text: malformed}
                        previous = self.legacy_cache(meal, "Cached title", "기존 이름") if fallback != "absent" else {}
                        config = {"cached-unconfigured": None, "cached-configured": {"model": "original-generator"},
                                  "absent": None, "generated": {"model": "new-generator"}}[fallback]
                        if fallback == "cached-configured":
                            previous["entries"][meal["translation_key"]]["generation_identity"] = self.fixture_identity(config)
                        inputs = copy.deepcopy((menu, previous, phrases, config))
                        try:
                            cache = build_translations(menu, previous, phrases, config,
                                                       translate=lambda *args: copy.deepcopy(generated))
                        except AttributeError as exc:
                            self.fail(f"Malformed phrase entry must follow incomplete fallback: {exc}")
                        if fallback == "absent":
                            self.assertNotIn(meal["translation_key"], cache["entries"])
                        elif fallback == "generated":
                            self.assertEqual(cache["entries"][meal["translation_key"]], {
                                "source": source_for(meal), **generated,
                                "origin": "model", "model": "new-generator", "prompt_version": "menu-v4",
                                "generation_identity": self.fixture_identity(config)})
                        else:
                            self.assertEqual(cache["entries"][meal["translation_key"]], previous["entries"][meal["translation_key"]])
                        self.assertEqual((menu, previous, phrases, config), inputs)

    def test_editorial_names_use_the_same_reviewed_names_and_brand_policy(self):
        for source_name, en, ko, expected_en, expected_ko in (
            ("mensaVital: Vegan curry", "mensaVital: KlimaTeller: Vegan curry",
             "mensaVital: KlimaTeller: 비건 커리", "Vegan curry", "비건 커리"),
            ("Wikingertopf", "Wikingertopf", "비킹어토프", "Meatball stew", "고기완자 스튜"),
            ("Köttbullar", "Köttbullar", "쾨트불라르", "Swedish meatballs", "스웨덴식 미트볼"),
        ):
            with self.subTest(source=source_name):
                meal = sample_meal(source_name)
                phrases = {source_name: {"en": en, "ko": ko}, "Gemüse": {"en": "Vegetables", "ko": "채소"}}
                cache = build_translations({"days": [{"meals": [meal]}]}, {}, phrases, None)
                entry = cache["entries"][meal["translation_key"]]
                self.assertEqual(entry["en"], {"name": expected_en, "components": ["Vegetables"]})
                self.assertEqual(entry["ko"], {"name": expected_ko, "components": ["채소"]})
                self.assertEqual(entry["origin"], "editorial-draft")
                self.assertEqual(entry["name_policy_version"], "semantic-names-v1")

    def test_generated_names_are_normalized_before_publication_with_truthful_provenance(self):
        meal = sample_meal("KlimaTeller: Vegan curry")
        result = {"en": {"name": "mensaVital: KlimaTeller: Vegan curry", "components": ["Vegetables"]},
                  "ko": {"name": "KLIMATELLER: MENsaVital: 비건 커리", "components": ["채소"]}}
        with provider(json.dumps(result)) as (config, calls):
            cache = build_translations({"days": [{"meals": [meal]}]}, {}, {}, config)
        entry = cache["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"], {"name": "Vegan curry", "components": ["Vegetables"]})
        self.assertEqual(entry["ko"], {"name": "비건 커리", "components": ["채소"]})
        self.assertEqual(entry["model"], "fixture-gemma")
        self.assertEqual(entry["prompt_version"], "menu-v4")
        self.assertEqual(entry["name_policy_version"], "semantic-names-v1")
        validate_cache(cache)

    def test_empty_or_residual_opaque_brand_names_are_rejected(self):
        meal = sample_meal("Unreviewed dish")
        for name in ("mensaVital: KlimaTeller:", "Vegan curry (mensaVital)", "KlimaTeller Curry"):
            with self.subTest(name=name):
                phrases = {meal["name_de"]: {"en": name, "ko": "요리"},
                           "Gemüse": {"en": "Vegetables", "ko": "채소"}}
                with self.assertRaisesRegex(ValueError, "[Nn]ame|[Tt]itle"):
                    build_translations({"days": [{"meals": [meal]}]}, {}, phrases, None)

    def test_unreviewed_source_variants_do_not_lose_dietary_or_food_identity(self):
        meal = sample_meal("Vegan Wikingertopf")
        previous = self.legacy_cache(meal, "Vegan Wikingertopf stew", "비건 Wikingertopf 스튜")
        with self.assertRaisesRegex(ValueError, "[Nn]ame|[Tt]itle"):
            build_translations({"days": [{"meals": [meal]}]}, previous, {}, None)

    def test_strict_cache_validation_rejects_bypassing_the_title_policy(self):
        for source_name, en, ko in (
            ("Wikingertopf", "Wikingertopf (stew)", "Wikingertopf (스튜)"),
            ("Köttbullar", "Köttbullar (Swedish meatballs)", "스웨덴식 미트볼"),
            ("Curry", "KlimaTeller: Curry", "커리"),
            ("Curry", "Curry", "커리 (mensaVital)"),
        ):
            with self.subTest(source=source_name, name=en):
                cache = self.legacy_cache(sample_meal(source_name), en, ko)
                with self.assertRaisesRegex(ValueError, "[Nn]ame|[Tt]itle"):
                    validate_cache(cache)


if __name__ == "__main__":
    unittest.main()
