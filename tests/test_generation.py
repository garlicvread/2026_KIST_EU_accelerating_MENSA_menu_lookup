"""이 테스트 모듈은 번역 요청을 보내는 generation 코드가 정해진 종류의 GenerationError 예외를 발생시키는지 확인합니다. 실제 외부 제공자에 요청하지 않도록 루프백 주소의 테스트 서버만 사용합니다."""

from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import Message
from http.client import IncompleteRead, InvalidURL, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from threading import Thread
import traceback
import unittest
from unittest.mock import patch
import urllib.error

from mensa import generation


SOURCE = {"name_de": "Kartoffeln", "components": ["Gemüse"]}
RESULT = {"en": {"name": "Potatoes", "components": ["Vegetables"]},
          "ko": {"name": "감자", "components": ["채소"]}}
SECRET = "fake-sensitive-provider-key"
REMOTE = "https://private-provider.invalid/secret-path"
BODY_SECRET = "private-response-body"


def envelope(provider="openai", content=None):
    content = json.dumps(RESULT) if content is None else content
    if provider == "ollama":
        return {"done": True, "done_reason": "stop", "message": {"content": content}}
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


@contextmanager
def fixture(body=None, *, status=200, headers=None, provider="openai"):
    calls = []
    body = envelope(provider) if body is None else body
    body = body if isinstance(body, bytes) else json.dumps(body).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append({"path": self.path, "headers": dict(self.headers),
                          "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        path = "/api/chat" if provider == "ollama" else "/v1/chat/completions"
        yield {"provider": provider, "url": f"http://127.0.0.1:{server.server_port}{path}",
               "model": "fixture-model", "key": SECRET}, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class GenerationFailureTests(unittest.TestCase):
    def error_type(self):
        self.assertTrue(hasattr(generation, "GenerationError"),
                        "GenerationError must be reexported by the adapter")
        error_type = generation.GenerationError
        self.assertTrue(issubclass(error_type, ValueError))
        return error_type

    def assert_safe(self, error):
        rendered = str(error) + repr(error) + "".join(traceback.format_exception(error))
        for sensitive in (SECRET, REMOTE, BODY_SECRET):
            self.assertNotIn(sensitive, rendered)
        self.assertIsNone(error.__cause__)
        self.assertFalse(hasattr(error, "response"))
        self.assertIs(type(error.retryable), bool)

    def failure(self, config, code, retryable, status=None, retry_after=None):
        error_type = self.error_type()
        try:
            generation.request_translation(SOURCE, config)
        except ValueError as error:
            self.assertIsInstance(error, error_type)
            self.assertEqual(error.code, code)
            self.assertIs(error.retryable, retryable)
            self.assertEqual(error.status_code, status)
            self.assertEqual(error.retry_after, retry_after)
            self.assert_safe(error)
            return error
        self.fail("Expected a typed generation failure")

    def test_error_api_lives_in_pure_module_and_retains_valueerror_compatibility(self):
        self.error_type()
        from mensa.errors import GenerationError
        error = GenerationError("timeout")
        self.assertIs(error.retryable, True)
        self.assertIsNone(error.status_code)
        self.assertIsNone(error.retry_after)
        self.assertEqual(str(error), "Translation request timed out")

    def test_http_failure_classification_without_adapter_retries(self):
        for status in (401, 403, 400, 404, 408, 429, 500, 503):
            retryable = status in (408, 429, 500, 503)
            code = "authentication" if status in (401, 403) else ("http_retryable" if retryable else "http_error")
            with self.subTest(status=status):
                with fixture(BODY_SECRET.encode(), status=status) as (config, calls):
                    self.failure(config, code, retryable, status)
                self.assertEqual(len(calls), 1)

    def test_retry_after_delta_invalid_and_cap_on_retryable_http(self):
        for value, expected in (("12", 12), ("0", 0), ("999999999999999999", 86400),
                                ("-1", None), ("1.5", None), ("NaN", None), ("inf", None), ("bogus", None)):
            with self.subTest(value=value):
                with fixture(status=429, headers={"Retry-After": value}) as (config, _):
                    self.failure(config, "http_retryable", True, 429, expected)

    def test_retry_after_http_date_uses_utc_adapter_clock(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        with fixture(status=503, headers={"Retry-After": "Wed, 30 Sep 2026 12:01:00 GMT"}) as (config, _):
            with patch.object(generation, "datetime", create=True) as clock:
                clock.now.return_value = now
                self.failure(config, "http_retryable", True, 503, 60)
                clock.now.assert_called_once_with(timezone.utc)

    def test_permanent_http_ignores_retry_after(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                with fixture(status=status, headers={"Retry-After": "99"}) as (config, _):
                    self.failure(config, "authentication" if status != 400 else "http_error", False, status)

    def test_http_error_body_is_never_read_and_response_is_closed(self):
        class Unreadable(io.BytesIO):
            def read(self, *args):
                raise AssertionError("HTTP failure body must never be read")
        body = Unreadable(BODY_SECRET.encode())
        headers = Message()
        headers["Retry-After"] = "2"
        raw = urllib.error.HTTPError(REMOTE, 429, SECRET, headers, body)
        with patch.object(generation.urllib.request.OpenerDirector, "open", side_effect=raw):
            self.failure({"url": "http://127.0.0.1/fixture", "model": "fixture", "key": SECRET},
                         "http_retryable", True, 429, 2)
        self.assertTrue(body.closed)

    def test_http_cleanup_failure_keeps_original_safe_classification(self):
        class BrokenClose(io.BytesIO):
            def read(self, *args):
                raise AssertionError("HTTP failure body must never be read")

            def close(self):
                super().close()
                raise OSError(SECRET + REMOTE + BODY_SECRET)

        body = BrokenClose()
        raw = urllib.error.HTTPError(REMOTE, 401, SECRET, Message(), body)
        with patch.object(generation.urllib.request.OpenerDirector, "open", side_effect=raw):
            try:
                generation.request_translation(SOURCE, {"url": "http://127.0.0.1/fixture", "model": "fixture"})
            except Exception as error:
                self.assertIsInstance(error, self.error_type())
                self.assertEqual(error.code, "authentication")
                self.assertEqual(error.status_code, 401)
                self.assertIs(error.retryable, False)
                self.assert_safe(error)
            else:
                self.fail("Expected authentication failure")
        self.assertTrue(body.closed)

    def test_redirect_cleanup_failure_keeps_original_safe_classification(self):
        class BrokenClose(io.BytesIO):
            def close(self):
                super().close()
                raise OSError(SECRET + REMOTE + BODY_SECRET)

        body = BrokenClose()
        try:
            generation._NoRedirect().redirect_request(None, body, 307, SECRET, Message(), REMOTE)
        except Exception as error:
            self.assertIsInstance(error, self.error_type())
            self.assertEqual(error.code, "redirect")
            self.assertEqual(error.status_code, 307)
            self.assertIs(error.retryable, False)
            self.assert_safe(error)
        else:
            self.fail("Expected redirect failure")
        self.assertTrue(body.closed)

    def test_redirect_is_permanent_and_never_followed(self):
        for provider in ("openai", "ollama"):
            with self.subTest(provider=provider):
                with fixture(status=307, headers={"Location": REMOTE}, provider=provider) as (config, calls):
                    self.failure(config, "redirect", False, 307)
                self.assertEqual(len(calls), 1)

    def test_timeout_and_network_interruptions_are_retryable_and_safe(self):
        for raw, code in ((TimeoutError(SECRET + REMOTE), "timeout"),
                          (urllib.error.URLError(SECRET + REMOTE), "network"),
                          (urllib.error.URLError(TimeoutError(SECRET + REMOTE)), "timeout"),
                          (ConnectionResetError(SECRET + REMOTE), "network"),
                          (RemoteDisconnected(SECRET + REMOTE), "network")):
            with self.subTest(raw=type(raw).__name__):
                with patch.object(generation.urllib.request.OpenerDirector, "open", side_effect=raw) as opening:
                    self.failure({"url": "http://127.0.0.1/fixture", "model": "fixture", "key": SECRET}, code, True)
                self.assertEqual(opening.call_count, 1)

    def test_interrupted_response_read_is_retryable_and_closed(self):
        class BrokenResponse(io.BytesIO):
            def read(self, *args):
                raise IncompleteRead(BODY_SECRET.encode(), 999)
        response = BrokenResponse()
        with patch.object(generation.urllib.request.OpenerDirector, "open", return_value=response):
            self.failure({"url": "http://127.0.0.1/fixture", "model": "fixture"}, "network", True)
        self.assertTrue(response.closed)

    def test_malformed_json_and_unicode_are_permanent_response_failures(self):
        for provider in ("openai", "ollama"):
            for body in (b'{"truncated":', b'\xff', envelope(provider, '{"en":')):
                with self.subTest(provider=provider, body_type=type(body).__name__):
                    with fixture(body, provider=provider) as (config, _):
                        self.failure(config, "invalid_json", False)

    def test_oversized_response_is_permanent(self):
        with fixture(b" " * (generation.MAX_RESPONSE_BYTES + 1)) as (config, _):
            self.failure(config, "response_too_large", False)

    def test_arbitrary_openai_envelope_choice_and_message_shapes_are_typed(self):
        shapes = [b"null", [], "remote", 1, {}, {"choices": []}, {"choices": {}}, {"choices": None},
                  {"choices": [None]}, {"choices": [[]]},
                  {"choices": [{"finish_reason": "stop"}]},
                  {"choices": [{"finish_reason": "stop", "message": []}]},
                  {"choices": [{"finish_reason": "stop", "message": {"content": 1}}]}]
        for body in shapes:
            with self.subTest(body=body):
                with fixture(body) as (config, _):
                    self.failure(config, "invalid_envelope", False)

    def test_deep_json_is_a_permanent_response_failure_at_any_decoder_limit(self):
        body = io.BytesIO(b"[" * 2000 + b"]" * 2000)
        with patch.object(generation.urllib.request.OpenerDirector, "open", return_value=body):
            try:
                generation.request_translation(SOURCE, {"url": "http://127.0.0.1/fixture", "model": "fixture"})
            except ValueError as error:
                self.assertIsInstance(error, self.error_type())
                self.assertIn(error.code, ("invalid_json", "invalid_envelope"))
                self.assertIs(error.retryable, False)
                self.assertIsNone(error.status_code)
                self.assertIsNone(error.retry_after)
                self.assert_safe(error)
            else:
                self.fail("Expected a permanent deep JSON response failure")
        self.assertTrue(body.closed)

    def test_injected_deep_json_decoder_limit_is_typed(self):
        body = io.BytesIO(b"[" * 2000 + b"]" * 2000)
        with patch.object(generation.urllib.request.OpenerDirector, "open", return_value=body):
            with patch.object(generation.json, "loads", side_effect=RecursionError("fixture decoder limit")):
                self.failure({"url": "http://127.0.0.1/fixture", "model": "fixture"},
                             "invalid_json", False)
        self.assertTrue(body.closed)

    def test_arbitrary_native_message_shapes_are_typed(self):
        for message in (None, [], {}, {"content": None}, {"content": {}}):
            with self.subTest(message=message):
                with fixture({"done": True, "done_reason": "stop", "message": message}, provider="ollama") as (config, _):
                    self.failure(config, "invalid_envelope", False)

    def test_missing_or_abnormal_completion_is_permanent(self):
        for provider in ("openai", "ollama"):
            for completion in (None, "length", "missing"):
                body = envelope(provider)
                part = body if provider == "ollama" else body["choices"][0]
                field = "done_reason" if provider == "ollama" else "finish_reason"
                if completion == "missing":
                    del part[field]
                else:
                    part[field] = completion
                with self.subTest(provider=provider, completion=completion):
                    with fixture(body, provider=provider) as (config, _):
                        self.failure(config, "incomplete_generation", False)
        for done in ("missing", False, 1):
            body = envelope("ollama")
            if done == "missing":
                del body["done"]
            else:
                body["done"] = done
            with self.subTest(provider="ollama", done=done):
                with fixture(body, provider="ollama") as (config, _):
                    self.failure(config, "incomplete_generation", False)

    def test_invalid_translation_result_uses_canonical_validator(self):
        invalid = [{}, [], {"en": RESULT["en"]},
                   {**RESULT, "ko": {"name": "요리", "components": []}},
                   {**RESULT, "en": {**RESULT["en"], "prices": 100}}]
        for provider in ("openai", "ollama"):
            for result in invalid:
                with self.subTest(provider=provider, result=result):
                    with fixture(envelope(provider, json.dumps(result)), provider=provider) as (config, _):
                        self.failure(config, "invalid_result", False)

    def test_invalid_provider_configuration_is_permanent_before_transport(self):
        configs = [None, [], {}, {"provider": "bad"}, {"url": None, "model": "fixture"},
                   {"url": "", "model": "fixture"}, {"url": "ftp://127.0.0.1/a", "model": "fixture"},
                   {"url": "http://[invalid", "model": "fixture"},
                   {"url": "http://127.0.0.1:invalid/a", "model": "fixture"},
                   {"url": "http://127.0.0.1/a"}, {"url": "http://127.0.0.1/a", "model": ""},
                   {"url": "http://127.0.0.1/a", "model": "fixture", "key": 123}]
        for config in configs:
            with self.subTest(config=config):
                with patch.object(generation.urllib.request.OpenerDirector, "open") as opening:
                    self.failure(config, "invalid_config", False)
                opening.assert_not_called()

    def test_raw_ascii_whitespace_controls_and_del_in_urls_are_invalid_configuration(self):
        for character in [chr(n) for n in range(33)] + [chr(127)]:
            urls = (f"http://bad{character}host.invalid/chat", f"http://127.0.0.1/path{character}piece")
            for url in urls:
                with self.subTest(url=url):
                    with patch.object(generation.urllib.request.OpenerDirector, "open",
                                      side_effect=InvalidURL(SECRET + REMOTE + BODY_SECRET)) as opening:
                        self.failure({"url": url, "model": "fixture"}, "invalid_config", False)
                    opening.assert_not_called()

    def test_invalidurl_transport_exception_is_permanent_and_safe(self):
        with patch.object(generation.urllib.request.OpenerDirector, "open",
                          side_effect=InvalidURL(SECRET + REMOTE + BODY_SECRET)) as opening:
            self.failure({"url": "http://127.0.0.1/fixture", "model": "fixture"}, "invalid_config", False)
        self.assertEqual(opening.call_count, 1)

    def test_openai_configuration_failure_has_generic_fixed_wording(self):
        error = self.failure({"provider": "openai", "url": "", "model": "fixture"}, "invalid_config", False)
        self.assertEqual(str(error), "Invalid translation configuration")
        self.assertNotIn("Ollama", str(error))
        self.assertNotIn("loopback", str(error))

    def test_percent_encoded_url_characters_remain_valid(self):
        with fixture() as (config, calls):
            config["url"] += "%20encoded%09%00%7F"
            self.assertEqual(generation.request_translation(SOURCE, config), RESULT)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["path"].endswith("%20encoded%09%00%7F"))

    def test_native_invalid_endpoints_keep_fixed_loopback_message(self):
        for url in ("https://example.invalid/api/chat", "http://127.0.0.1/other", "http://user:pass@localhost/api/chat",
                    "http://localhost/api/chat?secret=1", "http://localhost/api/chat#fragment"):
            with self.subTest(url=url):
                error = self.failure({"provider": "ollama", "url": url, "model": "fixture"}, "invalid_ollama_endpoint", False)
                self.assertEqual(str(error), "Native Ollama requires a plain loopback /api/chat URL")

    def test_success_result_payload_timeouts_and_proxy_policy_are_preserved(self):
        for provider, timeout in (("openai", 90), ("ollama", 180)):
            with self.subTest(provider=provider):
                with fixture(provider=provider) as (config, calls):
                    original = generation.urllib.request.OpenerDirector.open
                    observed = []
                    def opening(opener, request, **kwargs):
                        observed.append((kwargs["timeout"], opener))
                        return original(opener, request, **kwargs)
                    with patch.object(generation.urllib.request.OpenerDirector, "open", new=opening):
                        self.assertEqual(generation.request_translation(SOURCE, config), RESULT)
                self.assertEqual(len(calls), 1)
                self.assertEqual(observed[0][0], timeout)
                payload = calls[0]["payload"]
                self.assertEqual(json.loads(payload["messages"][1]["content"]), SOURCE)
                if provider == "ollama":
                    self.assertEqual(set(payload), {"model", "messages", "stream", "think", "format", "keep_alive", "options"})
                    self.assertFalse(payload["stream"])
                    self.assertFalse(payload["think"])
                    self.assertEqual(payload["options"], {"temperature": 0, "num_ctx": 8192, "num_predict": 4096})
                    self.assertNotIn("Authorization", calls[0]["headers"])
                    self.assertFalse(any(isinstance(h, generation.urllib.request.ProxyHandler) for h in observed[0][1].handlers))
                else:
                    self.assertEqual(set(payload), {"model", "messages", "max_tokens", "response_format"})
                    self.assertEqual(payload["max_tokens"], 4096)
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer " + SECRET)


class RetryAfterTests(unittest.TestCase):
    def parser(self):
        self.assertTrue(hasattr(generation, "parse_retry_after"), "Pure Retry-After parser must be available")
        return generation.parse_retry_after

    def test_delta_seconds_require_ascii_nonnegative_integers_and_are_bounded(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        for value, expected in (("0", 0), (" 12 ", 12), ("999999999999999", 86400),
                                ("-1", None), ("+1", None), ("1.5", None), ("١٢", None),
                                ("NaN", None), ("inf", None), (None, None), (123, None), ("", None)):
            with self.subTest(value=value):
                self.assertEqual(self.parser()(value, now), expected)

    def test_aware_http_dates_future_past_and_cap(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        for value, expected in (("Wed, 30 Sep 2026 12:00:42 GMT", 42),
                                ("Wed, 30 Sep 2026 11:59:00 GMT", 0),
                                ("Fri, 02 Oct 2026 12:00:00 GMT", 86400),
                                ("Wed, 30 Sep 2026 14:00:42 +0200", 42),
                                ("Wed, 30 Sep 2026 12:00:42", None), ("not a date", None)):
            with self.subTest(value=value):
                self.assertEqual(self.parser()(value, now), expected)

    def test_explicit_now_must_be_aware(self):
        with self.assertRaises(ValueError):
            self.parser()("12", datetime(2026, 9, 30))
