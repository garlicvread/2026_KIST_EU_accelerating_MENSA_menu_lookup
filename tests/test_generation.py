"""이 테스트 모듈은 번역 요청을 보내는 generation 코드가 정해진 종류의 GenerationError 예외를 발생시키는지 확인합니다. 실제 외부 제공자에 요청하지 않도록 루프백 주소의 테스트 서버만 사용합니다."""

from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import Message
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

    def assert_safe(self, error):
        rendered = str(error) + repr(error) + "".join(traceback.format_exception(error))
        for sensitive in (SECRET, REMOTE, BODY_SECRET):
            self.assertNotIn(sensitive, rendered)
        self.assertIsNone(error.__cause__)

    def failure(self, config, code, retryable, status=None, retry_after=None):
        try:
            generation.request_translation(SOURCE, config)
        except ValueError as error:
            self.assertIsInstance(error, generation.GenerationError)
            self.assertEqual(error.code, code)
            self.assertIs(error.retryable, retryable)
            self.assertEqual(error.status_code, status)
            self.assertEqual(error.retry_after, retry_after)
            self.assert_safe(error)
            return error
        self.fail("Expected a typed generation failure")


    def test_http_failure_classification_without_adapter_retries(self):
        for status in (401, 400, 429, 503):
            retryable = status in (429, 503)
            code = "authentication" if status == 401 else ("http_retryable" if retryable else "http_error")
            with self.subTest(status=status):
                with fixture(BODY_SECRET.encode(), status=status, headers={"Retry-After": "99"}) as (config, calls):
                    self.failure(config, code, retryable, status, 99 if retryable else None)
                self.assertEqual(len(calls), 1)


    def test_retry_after_http_date_uses_utc_adapter_clock(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        with fixture(status=503, headers={"Retry-After": "Wed, 30 Sep 2026 12:01:00 GMT"}) as (config, _):
            with patch.object(generation, "datetime") as clock:
                clock.now.return_value = now
                self.failure(config, "http_retryable", True, 503, 60)


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


    def test_redirect_is_permanent_and_never_followed(self):
        with fixture(status=307, headers={"Location": REMOTE}) as (config, calls):
            self.failure(config, "redirect", False, 307)
        self.assertEqual(len(calls), 1)

    def test_timeout_and_network_interruptions_are_retryable_and_safe(self):
        for raw, code in ((TimeoutError(SECRET + REMOTE), "timeout"),
                          (urllib.error.URLError(SECRET + REMOTE), "network")):
            with self.subTest(code=code):
                with patch.object(generation.urllib.request.OpenerDirector, "open", side_effect=raw) as opening:
                    self.failure({"url": "http://127.0.0.1/fixture", "model": "fixture", "key": SECRET}, code, True)
                self.assertEqual(opening.call_count, 1)


    def test_malformed_json_and_unicode_are_permanent_response_failures(self):
        for body in (b'{"truncated":', b'\xff'):
            with self.subTest(body=body):
                with fixture(body) as (config, _):
                    self.failure(config, "invalid_json", False)


    def test_incomplete_provider_generations_are_permanent(self):
        for provider in ("openai", "ollama"):
            body = envelope(provider)
            part = body if provider == "ollama" else body["choices"][0]
            field = "done_reason" if provider == "ollama" else "finish_reason"
            part[field] = "length"
            with self.subTest(provider=provider):
                with fixture(body, provider=provider) as (config, _):
                    self.failure(config, "incomplete_generation", False)

    def test_translation_with_lost_components_is_permanent(self):
        result = {**RESULT, "ko": {"name": "요리", "components": []}}
        with fixture(envelope(content=json.dumps(result))) as (config, _):
            self.failure(config, "invalid_result", False)


    def test_native_endpoint_refuses_nonlocal_address_with_safe_message(self):
        error = self.failure({"provider": "ollama", "url": "https://example.invalid/api/chat", "model": "fixture"},
                             "invalid_ollama_endpoint", False)
        self.assertEqual(str(error), "Native Ollama requires a plain loopback /api/chat URL")

    def test_provider_round_trips_preserve_source_and_authorization(self):
        for provider in ("openai", "ollama"):
            with self.subTest(provider=provider):
                with fixture(provider=provider) as (config, calls):
                    self.assertEqual(generation.request_translation(SOURCE, config), RESULT)
                self.assertEqual(len(calls), 1)
                payload = calls[0]["payload"]
                self.assertEqual(json.loads(payload["messages"][1]["content"]), SOURCE)
                if provider == "ollama":
                    self.assertFalse(payload["stream"])
                    self.assertNotIn("Authorization", calls[0]["headers"])
                else:
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer " + SECRET)
