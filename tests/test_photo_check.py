"""The safety look at every photo (photo_check.py, spec 2026-10-01, revised).

Yes or no per live hazard, in JSON; the text the safety gate scans is written by
code, only when a hazard is present. A prose description that lists absent
hazards tripped the gate (the final review, and the video prompt's lesson of
22 September)."""

import base64
import json
import unittest

from emotorad_ai.guardrails import check_safety_in_description
from emotorad_ai.openrouter import CHAT_PATH, OpenRouterRateLimited
from emotorad_ai.photo_check import (INLINE_LIMIT, LIVE_HAZARDS, OPENROUTER_PHOTO_MODEL, PROMPT, TIMEOUT_SECONDS,
                                     OpenRouterPhotoChecker, PhotoCheckError, describe, photo_checker_from_env)

NONE = {"smoke": False, "flames": False, "swelling": False, "leaking": False, "sparks": False}


class FakeTransport:
    def __init__(self, answer=None, text=None, error=None):
        self.text = text if text is not None else json.dumps(answer if answer is not None else NONE)
        self.error, self.posts = error, []

    def post(self, path, body, timeout=None):
        self.posts.append({"path": path, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return {"model": OPENROUTER_PHOTO_MODEL,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": self.text}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 30}}


class CheckerTests(unittest.TestCase):
    def test_the_photo_goes_inline_once_asking_for_json_with_no_retention(self):
        transport = FakeTransport()
        self.assertEqual(OpenRouterPhotoChecker(transport).check(b"\xff\xd8jpeg", "image/jpeg"), [])
        [post] = transport.posts
        self.assertEqual((post["path"], post["timeout"]), (CHAT_PATH, TIMEOUT_SECONDS))
        body = post["body"]
        self.assertEqual(body["model"], "google/gemini-3.8-flash")
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": PROMPT})
        self.assertEqual(content[1]["image_url"]["url"],
                         "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode())

    def test_the_live_hazards_come_back_in_a_fixed_order(self):
        answer = dict(NONE, swelling=True, smoke=True)
        self.assertEqual(OpenRouterPhotoChecker(FakeTransport(answer)).check(b"j", "image/jpeg"), ["smoke", "swelling"])
        self.assertEqual(LIVE_HAZARDS, ("smoke", "flames", "swelling", "leaking", "sparks"))

    def test_only_a_true_counts(self):
        answer = {"smoke": "yes", "flames": 1, "swelling": None}
        self.assertEqual(OpenRouterPhotoChecker(FakeTransport(answer)).check(b"j", "image/jpeg"), [])

    def test_json_in_a_code_fence_is_read(self):
        text = "```json\n" + json.dumps(dict(NONE, sparks=True)) + "\n```"
        self.assertEqual(OpenRouterPhotoChecker(FakeTransport(text=text)).check(b"j", "image/jpeg"), ["sparks"])

    def test_prose_is_bad_json(self):
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(FakeTransport(text="There is no smoke visible.")).check(b"j", "image/jpeg")
        self.assertEqual(str(raised.exception), "bad_json")

    def test_a_photo_too_large_is_refused_before_any_request(self):
        transport = FakeTransport()
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(transport).check(b"x" * (INLINE_LIMIT + 1), "image/jpeg")
        self.assertEqual((str(raised.exception), transport.posts), ("too_large", []))

    def test_a_provider_error_gives_its_code_only(self):
        error = OpenRouterRateLimited("429 with the request echoed")
        with self.assertRaises(PhotoCheckError) as raised:
            OpenRouterPhotoChecker(FakeTransport(error=error)).check(b"j", "image/jpeg")
        self.assertEqual(str(raised.exception), "rate_limited")

    def test_the_prompt_says_past_damage_is_not_a_live_hazard(self):
        for word in ("dents", "cracks", "punctures", "scorch", "melted", "JSON"):
            self.assertIn(word, PROMPT)


class DescribeTests(unittest.TestCase):
    def test_no_hazard_no_text(self):
        self.assertIsNone(describe([]))

    def test_the_text_names_what_was_seen(self):
        self.assertEqual(describe(["smoke"]), "The customer's photo shows smoke.")
        self.assertEqual(describe(["smoke", "swelling", "leaking"]),
                         "The customer's photo shows smoke, swelling and leaking fluid.")

    def test_every_live_hazard_trips_the_gate(self):
        for hazard in LIVE_HAZARDS:
            self.assertTrue(check_safety_in_description(describe([hazard])).triggered, hazard)


class FromEnvTests(unittest.TestCase):
    def test_no_key_no_checker(self):
        self.assertIsNone(photo_checker_from_env({}))

    def test_a_key_gives_the_openrouter_checker(self):
        checker = photo_checker_from_env({"OPENROUTER_API_KEY": "sk-or-test"})
        self.assertEqual((checker.provider, checker.zdr), ("openrouter", True))
