import copy
import hashlib
import json
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


if __name__ == "__main__":
    unittest.main()
