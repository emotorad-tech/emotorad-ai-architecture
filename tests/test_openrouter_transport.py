import os
import unittest
from unittest import mock

from emotorad_ai.config import MODES, Settings
from emotorad_ai.openrouter import (
    API_KEY_ENV,
    OpenRouterAuthError,
    OpenRouterBadResponse,
    OpenRouterConfigError,
    OpenRouterPaymentRequired,
    OpenRouterRateLimited,
    OpenRouterRequestError,
    OpenRouterTransport,
    OpenRouterUnavailable,
)
from tests.fake_http import FakeServer

KEY = "sk-or-test-DO-NOT-LEAK"


class SettingsModeTests(unittest.TestCase):
    def test_the_modes_are_the_three_the_spec_names(self):
        self.assertEqual(MODES, ("offline", "bedrock", "openrouter"))
        self.assertEqual(Settings(mode="openrouter").mode, "openrouter")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            Settings(mode="openai")

    def test_model_defaults_match_the_spec(self):
        settings = Settings()
        self.assertEqual(settings.jev_model, "typesafe/jev-1.13")
        self.assertEqual(settings.narrow_model, "anthropic/claude-haiku-4.5")
        self.assertEqual(settings.fallback_model, "anthropic/claude-haiku-4.5")
        self.assertEqual(settings.openrouter_base_url, "https://openrouter.ai/api")

    def test_the_key_is_not_a_setting(self):
        self.assertFalse(any("key" in name for name in Settings.__dataclass_fields__))


class TransportTests(unittest.TestCase):
    def transport(self, server, timeout=5.0):
        return OpenRouterTransport(api_key=KEY, base_url=server.url, timeout=timeout)

    def test_posts_json_with_the_bearer_key_and_returns_the_body(self):
        with FakeServer() as server:
            server.queue(200, {"choices": [{"message": {"content": "hi"}}]})
            body = self.transport(server).post("/v1/chat/completions", {"model": "m"})
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(request["body"], {"model": "m"})

    def test_status_codes_map_to_typed_errors(self):
        cases = [
            (401, OpenRouterAuthError), (403, OpenRouterAuthError), (402, OpenRouterPaymentRequired),
            (429, OpenRouterRateLimited), (500, OpenRouterUnavailable), (529, OpenRouterUnavailable),
            (408, OpenRouterUnavailable), (400, OpenRouterRequestError), (422, OpenRouterRequestError),
        ]
        for status, error in cases:
            with self.subTest(status=status), FakeServer() as server:
                server.queue(status, {"error": {"code": status, "message": "nope"}})
                with self.assertRaises(error):
                    self.transport(server).post("/x", {})

    def test_a_200_carrying_an_error_body_is_an_error_not_an_answer(self):
        # OpenRouter reports an upstream provider failure this way.
        with FakeServer() as server:
            server.queue(200, {"error": {"code": 502, "message": "provider returned error"}})
            with self.assertRaises(OpenRouterUnavailable):
                self.transport(server).post("/x", {})

    def test_a_body_that_is_not_json_is_a_bad_response(self):
        with FakeServer() as server:
            server.queue(200, b"<html>gateway</html>")
            with self.assertRaises(OpenRouterBadResponse):
                self.transport(server).post("/x", {})

    def test_a_slow_answer_is_unavailable(self):
        with FakeServer() as server:
            server.queue(200, {"choices": []}, delay=1.0)
            with self.assertRaises(OpenRouterUnavailable):
                self.transport(server, timeout=0.2).post("/x", {})

    def test_a_per_call_timeout_overrides_the_default(self):
        with FakeServer() as server:
            server.queue(200, {"answers": {}}, delay=1.0)
            with self.assertRaises(OpenRouterUnavailable):
                self.transport(server, timeout=10.0).post("/x", {}, timeout=0.2)

    def test_the_key_never_appears_in_an_error_or_the_repr(self):
        with FakeServer() as server:
            server.queue(401, {"error": {"code": 401, "message": "bad key " + KEY}})
            transport = self.transport(server)
            with self.assertRaises(OpenRouterAuthError) as caught:
                transport.post("/x", {})
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(transport))

    def test_a_missing_key_fails_when_the_transport_is_built(self):
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}):
            with self.assertRaises(OpenRouterConfigError):
                OpenRouterTransport()


if __name__ == "__main__":
    unittest.main()
