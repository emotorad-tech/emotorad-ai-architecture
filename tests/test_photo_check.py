"""The safety look at every photo (photo_check.py, spec 2026-10-01)."""

import base64
import unittest

from emotorad_ai.openrouter import CHAT_PATH, OpenRouterRateLimited
from emotorad_ai.photo_check import (INLINE_LIMIT, OPENROUTER_PHOTO_MODEL, PROMPT, TIMEOUT_SECONDS,
                                     OpenRouterPhotoChecker, PhotoCheckError, photo_checker_from_env)


class FakeTransport:
    def __init__(self, text="A battery pack on a table. No smoke visible.", error=None):
        self.text, self.error, self.posts = text, error, []

    def post(self, path, body, timeout=None):
        self.posts.append({"path": path, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return {"model": OPENROUTER_PHOTO_MODEL,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": self.text}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 60}}


class CheckerTests(unittest.TestCase):
    def test_the_photo_goes_inline_once_with_the_prompt_and_no_retention(self):
        transport = FakeTransport()
        text = OpenRouterPhotoChecker(transport).check(b"\xff\xd8jpeg", "image/jpeg")
        self.assertEqual(text, "A battery pack on a table. No smoke visible.")
        [post] = transport.posts
        self.assertEqual((post["path"], post["timeout"]), (CHAT_PATH, TIMEOUT_SECONDS))
        body = post["body"]
        self.assertEqual(body["model"], "google/gemini-3.8-flash")
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": PROMPT})
        self.assertEqual(content[1]["image_url"]["url"],
                         "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode())

    def test_the_prompt_asks_about_every_hazard_and_how_to_say_none(self):
        for word in ("smoke", "flames", "scorch", "swelling", "melting", "leaking", "sparks", "no smoke visible"):
            self.assertIn(word, PROMPT)

    def test_a_photo_too_large_is_refused_before_any_request(self):
        transport = FakeTransport()
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(transport).check(b"x" * (INLINE_LIMIT + 1), "image/jpeg")
        self.assertEqual(str(raised.exception), "too_large")
        self.assertEqual(transport.posts, [])

    def test_a_provider_error_gives_its_code_only(self):
        error = OpenRouterRateLimited("429 with the request echoed")
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(FakeTransport(error=error)).check(b"jpeg", "image/jpeg")
        self.assertEqual(str(raised.exception), "rate_limited")

    def test_an_empty_answer_is_an_error(self):
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(FakeTransport(text="  ")).check(b"jpeg", "image/jpeg")
        self.assertEqual(str(raised.exception), "empty")


class FromEnvTests(unittest.TestCase):
    def test_no_key_no_checker(self):
        self.assertIsNone(photo_checker_from_env({}))

    def test_a_key_gives_the_openrouter_checker(self):
        checker = photo_checker_from_env({"OPENROUTER_API_KEY": "sk-or-test"})
        self.assertEqual(checker.provider, "openrouter")
        self.assertTrue(checker.zdr)
