import copy
import json
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from scripts.translations import build_translations, validate_cache, source_for
from scripts import notices, translations
from mensa import translation_contract


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


def sample_meal(name="Schupfnudeln", sides=("Gemüse",)):
    source = {"name_de": name, "components": list(sides)}
    key = translation_contract.source_key(source)
    return {"translation_key": key, "name_de": name,
            "components": [{"name_de": s, "notices": []} for s in sides],
            "prices": {"student": 310, "staff": 560, "guest": 780}}


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.meal = sample_meal()
        self.menu = {"days": [{"meals": [self.meal]}]}
        self.phrases = {"Schupfnudeln": {"en": "German potato dumplings", "ko": "독일식 감자 경단"},
                        "Gemüse": {"en": "Vegetables", "ko": "채소"}}


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


    def test_real_http_protocol_and_cached_reuse(self):
        result = {"en": {"name": "Potato dumplings", "components": ["Vegetables"]},
                  "ko": {"name": "감자 경단", "components": ["채소"]}}
        with provider(json.dumps(result)) as (config, calls):
            cache = build_translations(self.menu, {}, {}, config)
            incomplete = {"Schupfnudeln": {"en": "Incomplete editorial title"}}
            reused = build_translations(self.menu, cache, incomplete, config)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "fixture-gemma")
        supplied = json.loads(calls[0]["messages"][1]["content"])
        self.assertNotIn("prices", supplied)
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["ko"], result["ko"])
        self.assertEqual(reused["entries"], cache["entries"])
        self.assertIn("generation_identity", reused["entries"][self.meal["translation_key"]])

    def test_invalid_model_response_rejects_entire_candidate(self):
        before = build_translations(self.menu, {}, self.phrases, None)
        untouched = copy.deepcopy(before)
        changed = sample_meal(name="New dish")
        with provider('{"en":{"name":"Dish","components":[]}}') as (config, _):
            with self.assertRaises(ValueError):
                build_translations({"days": [{"meals": [changed]}]}, before, {}, config)
        self.assertEqual(before, untouched)


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

    def build(self, previous, config, result, *, glossary=None, phrases=None):
        with patch.object(translations, "load_glossary", return_value=self.glossary if glossary is None else glossary):
            return build_translations(self.menu, previous, phrases or {}, config,
                                      lambda *args: copy.deepcopy(result))

    def legacy(self):
        return {"schema_version": 1, "entries": {self.meal["translation_key"]: {
            "source": source_for(self.meal), **copy.deepcopy(self.original),
            "origin": "model", "model": self.config["model"], "prompt_version": "menu-v4"}}}


    def test_changed_backend_regenerates_model_output(self):
        previous = self.build({}, self.config, self.original)
        for change in ({"provider": "ollama"}, {"model": "different-model"},
                       {"url": "http://127.0.0.1:11435/api/chat"}):
            config = {**self.config, **change}
            with self.subTest(config=config):
                inputs = copy.deepcopy((previous, config))
                cache = self.build(previous, config, self.generated)
                entry = cache["entries"][self.meal["translation_key"]]
                self.assertEqual(entry["en"], self.generated["en"])
                self.assertEqual(entry["ko"], self.generated["ko"])
                self.assertNotEqual(entry["generation_identity"]["backend"], previous["entries"][self.meal["translation_key"]]["generation_identity"]["backend"])
                self.assertEqual((previous, config), inputs)

    def test_changed_policy_versions_and_exact_mappings_regenerate(self):
        config = {**self.config, "backend_revision": "immutable-fixture-v1"}
        previous = self.build({}, config, self.original)
        before = copy.deepcopy((previous, config, self.glossary))
        mapping = copy.deepcopy(translation_contract.REVIEWED_DISH_NAMES)
        mapping[next(iter(mapping))]["en"] += " revised"
        for field, value in (("PROMPT_VERSION", "test-policy-v2"), ("REVIEWED_DISH_NAMES", mapping)):
            with self.subTest(policy=field), patch.object(translation_contract, field, value):
                cache = self.build(previous, config, self.generated)
                entry = cache["entries"][self.meal["translation_key"]]
                self.assertEqual(entry["en"], self.generated["en"])
                self.assertEqual(entry["ko"], self.generated["ko"])
                self.assertNotEqual(entry["generation_identity"]["policy"], previous["entries"][self.meal["translation_key"]]["generation_identity"]["policy"])
        self.assertEqual((previous, config, self.glossary), before)

    def test_changed_complete_glossary_regenerates_even_for_unused_label(self):
        config = {**self.config, "backend_revision": "immutable-fixture-v1"}
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
                self.assertNotEqual(entry["generation_identity"]["glossary"], previous["entries"][self.meal["translation_key"]]["generation_identity"]["glossary"])
        self.assertEqual((previous, config, self.glossary), before)


    def test_configured_legacy_model_regenerates_offline_legacy_is_not_stamped(self):
        previous = self.legacy()
        before = copy.deepcopy(previous)
        retained = self.build(previous, None, self.generated)
        self.assertEqual(retained["entries"], previous["entries"])
        self.assertNotIn("generation_identity", retained["entries"][self.meal["translation_key"]])
        config = {**self.config, "backend_revision": "immutable-fixture-v1"}
        inputs = copy.deepcopy(config)
        regenerated = self.build(previous, config, self.generated)
        entry = regenerated["entries"][self.meal["translation_key"]]
        self.assertEqual(entry["en"], self.generated["en"])
        self.assertEqual(entry["ko"], self.generated["ko"])
        self.assertIn("generation_identity", entry)
        self.assertEqual(config, inputs)
        self.assertEqual(previous, before)


    def test_malformed_cached_identity_is_rejected_without_input_mutation(self):
        previous = self.legacy()
        previous["entries"][self.meal["translation_key"]]["generation_identity"] = {
            "schema_version": 1, "policy": "a" * 64, "glossary": "b" * 64}
        before = copy.deepcopy(previous)
        with self.assertRaises(ValueError):
            self.build(previous, self.config, self.generated)
        self.assertEqual(previous, before)


class TranslationCheckpointTests(unittest.TestCase):
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
            return build_translations(menu, previous, phrases, config,
                                      translate=translate, **kwargs)


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


    def test_configured_editorial_correction_replaces_migrated_v3_components_and_applies_policies(self):
        meal = sample_meal("Wikingertopf", sides=("Gemüse", "Peperoni"))
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Wikingertopf": {"en": "Wikingertopf", "ko": "비킹어토프"},
                   "Gemüse": {"en": "Corrected editorial side", "ko": "수정한 편집 곁들임"},
                   "Peperoni": {"en": "Peperoni", "ko": "페페로니"}}
        previous = self.legacy_cache(meal, "Wikingertopf (stew)", "Wikingertopf (스튜)")
        migrated = build_translations(menu, previous, {}, None)
        cache = build_translations(menu, migrated, phrases, {"model": "original-generator"},
                                   translate=lambda *args: self.fail("Complete editorial phrases must avoid inference"))
        self.assertEqual(cache["entries"][meal["translation_key"]], {
            "source": source_for(meal),
            "en": {"name": "Meatball stew", "components": ["Corrected editorial side", "Chili peppers"]},
            "ko": {"name": "고기완자 스튜", "components": ["수정한 편집 곁들임", "고추"]},
            "origin": "editorial-draft", "model": "assistant-draft", "prompt_version": "menu-v4",
            "name_policy_version": "semantic-names-v1", "component_policy_version": "semantic-components-v1"})


    def test_incomplete_editorial_phrases_use_configured_generator_without_partial_overlay(self):
        meal = sample_meal()
        menu = {"days": [{"meals": [meal]}]}
        phrases = {"Schupfnudeln": {"en": "Editorial title", "ko": "편집 이름"},
                   "Gemüse": {"en": "Editorial side"}}
        generated = {"en": {"name": "Generated title", "components": ["Generated side"]},
                     "ko": {"name": "생성한 이름", "components": ["생성한 곁들임"]}}
        previous = self.legacy_cache(meal, "Old title", "이전 이름")
        config = {"model": "new-generator"}
        inputs = copy.deepcopy((menu, previous, phrases, config))
        cache = build_translations(menu, previous, phrases, config,
                                   translate=lambda *args: copy.deepcopy(generated))
        self.assertEqual(cache["entries"][meal["translation_key"]], {
            "source": source_for(meal), **generated,
            "origin": "model", "model": "new-generator", "prompt_version": "menu-v4",
            "generation_identity": self.fixture_identity(config)})
        self.assertEqual((menu, previous, phrases, config), inputs)


    def test_generated_names_are_normalized_before_publication_with_truthful_provenance(self):
        meal = sample_meal("KlimaTeller: Vegan curry", sides=("Peperoni",))
        result = {"en": {"name": "mensaVital: KlimaTeller: Vegan curry", "components": ["Peperoni"]},
                  "ko": {"name": "KLIMATELLER: MENsaVital: 비건 커리", "components": ["페페로니"]}}
        with provider(json.dumps(result)) as (config, calls):
            cache = build_translations({"days": [{"meals": [meal]}]}, {}, {}, config)
        entry = cache["entries"][meal["translation_key"]]
        self.assertEqual(entry["en"], {"name": "Vegan curry", "components": ["Chili peppers"]})
        self.assertEqual(entry["ko"], {"name": "비건 커리", "components": ["고추"]})
        self.assertEqual(entry["model"], "fixture-gemma")
        self.assertEqual(entry["prompt_version"], "menu-v4")
        self.assertEqual(entry["name_policy_version"], "semantic-names-v1")
        validate_cache(cache)


    def test_unreviewed_source_variants_do_not_lose_dietary_or_food_identity(self):
        meal = sample_meal("Vegan Wikingertopf")
        previous = self.legacy_cache(meal, "Vegan Wikingertopf stew", "비건 Wikingertopf 스튜")
        with self.assertRaisesRegex(ValueError, "[Nn]ame|[Tt]itle"):
            build_translations({"days": [{"meals": [meal]}]}, previous, {}, None)

    def test_strict_cache_validation_rejects_bypassing_the_title_policy(self):
        cache = self.legacy_cache(sample_meal("Wikingertopf"), "Wikingertopf (stew)", "Wikingertopf (스튜)")
        with self.assertRaisesRegex(ValueError, "[Nn]ame|[Tt]itle"):
            validate_cache(cache)


if __name__ == "__main__":
    unittest.main()
