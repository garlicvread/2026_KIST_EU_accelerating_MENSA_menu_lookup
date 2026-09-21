import copy
import hashlib
import json
from pathlib import Path
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from scripts.translations import build_translations, validate_cache, model_config, request_translation, source_for


@contextmanager
def provider(content, redirect=False, completion="stop"):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if redirect:
                self.send_response(307)
                self.send_header("Location", "/other")
                self.end_headers()
                return
            choice = {"message": {"content": content}}
            if completion != "missing":
                choice["finish_reason"] = completion
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
def ollama_provider(response, redirect=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append({"path": self.path, "headers": dict(self.headers),
                          "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if redirect:
                self.send_response(307)
                self.send_header("Location", "/other")
                self.end_headers()
                return
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
        old.pop("notices", None)  # The published schema-1 cache predates notice translations.
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
        result = build_translations(self.menu, {}, {"Schupfnudeln": self.phrases["Schupfnudeln"]}, None)
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

    def test_redirect_cannot_forward_api_credentials(self):
        with provider("unused", redirect=True) as (config, calls):
            with self.assertRaises(ValueError):
                request_translation(source_for(self.meal), config)
        self.assertEqual(len(calls), 1)

    def test_provider_must_explicitly_confirm_complete_generation(self):
        result = {"en": {"name": "Potato dumplings", "components": ["Vegetables"]},
                  "ko": {"name": "감자 경단", "components": ["채소"]}}
        for completion in (None, "missing", "length"):
            with self.subTest(completion=completion):
                with provider(json.dumps(result), completion=completion) as (config, _):
                    with self.assertRaises(ValueError):
                        request_translation(source_for(self.meal), config)

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

    def test_native_ollama_requires_explicit_normal_completion(self):
        content = json.dumps({"en": {"name": "Dish", "components": ["Side"]},
                              "ko": {"name": "요리", "components": ["곁들임"]}})
        for completion in ({"done": False, "done_reason": "stop"}, {"done": 1, "done_reason": "stop"},
                           {"done": True, "done_reason": "length"}, {"done": True}, {"done_reason": "stop"}):
            with self.subTest(completion=completion):
                with ollama_provider({**completion, "message": {"content": content}}) as (config, _):
                    with self.assertRaises(ValueError):
                        request_translation(source_for(self.meal), config)

    def test_native_ollama_rejects_truncated_json_and_schema_mismatch(self):
        responses = [b'{"done":true,"message":',
                     {"done": True, "done_reason": "stop", "message": {"content": '{"en":'}},
                     {"done": True, "done_reason": "stop", "message": {"content": json.dumps({
                         "en": {"name": "Dish", "components": []},
                         "ko": {"name": "요리", "components": []}})}},
                     {"done": True, "done_reason": "stop", "message": {"content": json.dumps({
                         "en": {"name": "Dish", "components": ["Side"], "prices": {"student": 100}},
                         "ko": {"name": "요리", "components": ["곁들임"]}})}}]
        for response in responses:
            with self.subTest(response=response):
                with ollama_provider(response) as (config, _):
                    with self.assertRaises(ValueError):
                        request_translation(source_for(self.meal), config)

    def test_native_ollama_never_follows_redirects(self):
        with ollama_provider({}, redirect=True) as (config, calls):
            with self.assertRaises(ValueError):
                request_translation(source_for(self.meal), config)
        self.assertEqual(len(calls), 1)

    def test_native_ollama_checks_loopback_at_request_boundary(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            request_translation(source_for(self.meal), {
                "provider": "ollama", "url": "https://example.invalid/api/chat", "model": "gemma4:e4b", "key": ""})


class SemanticNamePolicyTests(unittest.TestCase):
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

    def test_unrelated_legacy_translations_are_reused_even_with_available_editorial_phrases(self):
        meal = sample_meal()
        previous = self.legacy_cache(meal, "Potato dumplings", "감자 경단")
        phrases = {"Schupfnudeln": {"en": "Different title", "ko": "다른 이름"},
                   "Gemüse": {"en": "Different side", "ko": "다른 곁들임"}}
        cache = build_translations({"days": [{"meals": [meal]}]}, previous, phrases, None)
        self.assertEqual(cache["entries"], previous["entries"])
        self.assertNotIn("name_policy_version", cache["entries"][meal["translation_key"]])

    def test_configured_build_preserves_migrated_v3_components_with_editorial_available(self):
        meal = sample_meal("Wikingertopf")
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Wikingertopf": {"en": "Meatball stew", "ko": "고기완자 스튜"},
                   "Gemüse": {"en": "Different editorial side", "ko": "다른 편집 곁들임"}}
        for origin in ("model", "editorial-draft", "reviewed-draft"):
            with self.subTest(origin=origin):
                previous = self.legacy_cache(meal, "Wikingertopf (stew)", "Wikingertopf (스튜)", origin)
                migrated = build_translations(menu, previous, {}, None)
                cache = build_translations(menu, migrated, phrases, {"model": "original-generator"},
                                           translate=lambda *args: self.fail("Compatible cache must not call a model"))
                self.assertEqual(cache, migrated)
                self.assertEqual(cache["entries"][meal["translation_key"]]["prompt_version"], "menu-v3")

    def test_configured_build_reuses_v3_model_cache_without_editorial_phrases(self):
        meal = sample_meal()
        previous = self.legacy_cache(meal, "Potato dumplings", "감자 경단")
        cache = build_translations({"days": [{"meals": [meal]}]}, previous, {}, {"model": "original-generator"},
                                   translate=lambda *args: self.fail("Compatible cache must not call a model"))
        self.assertEqual(cache["entries"], previous["entries"])

    def test_configured_build_refreshes_changed_model_unsupported_prompt_and_missing_source(self):
        meal = sample_meal()
        result = {"en": {"name": "Potato dumplings", "components": ["New side"]},
                  "ko": {"name": "감자 경단", "components": ["새 곁들임"]}}
        for reason in ("changed-model", "unsupported-prompt", "missing-source"):
            with self.subTest(reason=reason):
                previous = self.legacy_cache(meal, "Potato dumplings", "감자 경단")
                entry = previous["entries"][meal["translation_key"]]
                if reason == "changed-model":
                    entry["model"] = "older-model"
                elif reason == "unsupported-prompt":
                    entry["prompt_version"] = "menu-v2"
                else:
                    previous["entries"] = {}
                calls = []
                def translate(source, config):
                    calls.append(source)
                    return copy.deepcopy(result)
                cache = build_translations({"days": [{"meals": [meal]}]}, previous, {},
                                           {"model": "original-generator"}, translate=translate)
                self.assertEqual(calls, [source_for(meal)])
                self.assertEqual(cache["entries"][meal["translation_key"]]["en"], result["en"])
                self.assertEqual(cache["entries"][meal["translation_key"]]["prompt_version"], "menu-v4")

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
        instructions = calls[0]["messages"][0]["content"]
        self.assertNotIn("retain the dish name with a brief type", instructions)
        self.assertIn("descriptive", instructions)
        self.assertIn("mensaVital", instructions)
        self.assertIn("KlimaTeller", instructions)
        self.assertIn("original German", instructions)
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
