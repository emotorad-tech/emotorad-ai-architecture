"""The OpenRouter key as the environment hands it over, stray line endings and all.

Found by the mutation audit on 2026-09-29: a key ending in CR or LF (easy to
store by accident in Secrets Manager or a pasted value) made http.client raise
ValueError("Invalid header value b'Bearer sk-or-v1-...\\r'"). That is not an
error the transport catches, so it escaped with the key in its text, and the
runtime, which catches only OpenRouter and Jev errors, crashed the turn and
printed the key in the server's traceback. The Anthropic path already strips
its key (llm.select_llm); this one did not.
"""

import contextlib
import http.client
import os
import socket
import unittest
from unittest import mock

from emotorad_ai.config import Settings
from emotorad_ai.openrouter import API_KEY_ENV, OpenRouterConfigError, OpenRouterError, OpenRouterTransport
from emotorad_ai.wiring import build_models

KEY = "sk-or-v1-FAKEKEY0123456789"


class RecordingOpener:
    """Stands in for urlopen and keeps the request it was given."""

    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        raise OSError("no network in tests")


@contextlib.contextmanager
def no_network():
    refuse = AssertionError("a test tried to open a connection")
    with mock.patch.object(http.client.HTTPConnection, "connect", side_effect=refuse), \
            mock.patch.object(socket, "create_connection", side_effect=refuse):
        yield


class StrayWhitespaceTests(unittest.TestCase):
    def test_a_key_with_a_line_ending_or_spaces_is_sent_without_them(self):
        for stray in ("\r", "\n", "\r\n", "  ", "\t\n", " \r\n"):
            with self.subTest(stray=repr(stray)):
                opener = RecordingOpener()
                transport = OpenRouterTransport(api_key=" " + KEY + stray, opener=opener)
                with self.assertRaises(OpenRouterError):
                    transport.post("/v1/chat/completions", {"model": "x"})
                self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + KEY)

    def test_through_the_real_http_client_the_error_is_ours_and_holds_no_key(self):
        # The path that leaked: urllib's own header check, before any connection.
        transport = OpenRouterTransport(api_key=KEY + "\r\n", base_url="http://127.0.0.1:9", timeout=2)
        with self.assertRaises(OpenRouterError) as caught:
            transport.post("/v1/chat/completions", {"model": "x"})
        self.assertNotIn("FAKEKEY", str(caught.exception))

    def test_a_key_read_from_the_environment_is_stripped_too(self):
        opener = RecordingOpener()
        with mock.patch.dict(os.environ, {API_KEY_ENV: KEY + "\n"}):
            transport = OpenRouterTransport(opener=opener)
        with self.assertRaises(OpenRouterError):
            transport.post("/v1/chat/completions", {"model": "x"})
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + KEY)

    def test_a_key_that_is_only_whitespace_is_not_set(self):
        with self.assertRaises(OpenRouterConfigError) as caught:
            OpenRouterTransport(api_key=" \r\n")
        self.assertIn("is not set", str(caught.exception))


class DamagedKeyTests(unittest.TestCase):
    def test_a_control_character_inside_the_key_is_refused_at_start_without_the_key(self):
        for damaged in (KEY[:10] + "\n" + KEY[10:], KEY[:10] + "\x00" + KEY[10:], KEY[:10] + " " + KEY[10:]):
            with self.subTest(damaged=repr(damaged[8:12])):
                with self.assertRaises(OpenRouterConfigError) as caught:
                    OpenRouterTransport(api_key=damaged)
                message = str(caught.exception)
                self.assertIn(API_KEY_ENV, message)
                self.assertNotIn("FAKEKEY", message)
                self.assertNotIn(KEY[:10], message)

    def test_the_server_refuses_to_start_on_one_rather_than_failing_every_turn(self):
        # build_models runs at import in api.py, so this fails the deploy's
        # health check instead of the first customer message.
        with mock.patch.dict(os.environ, {API_KEY_ENV: KEY[:10] + "\n" + KEY[10:]}), no_network():
            with self.assertRaises(OpenRouterConfigError):
                build_models(Settings(mode="openrouter", log_path=""))

    def test_the_server_starts_on_a_key_with_only_a_trailing_line_ending(self):
        with mock.patch.dict(os.environ, {API_KEY_ENV: KEY + "\r\n"}), no_network():
            models = build_models(Settings(mode="openrouter", log_path=""))
        self.assertIsNotNone(models.jev)


if __name__ == "__main__":
    unittest.main()
