"""The customer's clip described through OpenRouter (the person's choice on
2026-09-29), beside the Gemini-direct summariser it replaces by default.

No network: a fake transport records the request and answers it.
"""

import unittest

from emotorad_ai.openrouter import CHAT_PATH, OpenRouterUnavailable
from emotorad_ai.video_summary import (
    INLINE_LIMIT,
    OPENROUTER_VIDEO_MODEL,
    PROMPT,
    TIMEOUT_SECONDS,
    GeminiVideoSummariser,
    OpenRouterVideoSummariser,
    VideoSummaryError,
    summariser_from_env,
)


class FakeTransport:
    def __init__(self, text="A grinding sound from the rear hub at 0:04.", error=None, cost=0.004):
        self.text, self.error, self.cost, self.posts = text, error, cost, []

    def post(self, path, body, timeout=None):
        self.posts.append({"path": path, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return {
            "model": OPENROUTER_VIDEO_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": self.text}}],
            "usage": {"prompt_tokens": 9000, "completion_tokens": 120, "cost": self.cost},
        }


class OpenRouterSummariserTests(unittest.TestCase):
    def test_a_clip_is_sent_inline_once_with_the_fixed_prompt_and_no_retention(self):
        transport = FakeTransport()
        summary = OpenRouterVideoSummariser(transport).summarise(b"\x00\x01", "video/mp4", name="clip.mp4")
        self.assertEqual(summary, "A grinding sound from the rear hub at 0:04.")
        [post] = transport.posts
        self.assertEqual((post["path"], post["timeout"]), (CHAT_PATH, TIMEOUT_SECONDS))
        body = post["body"]
        self.assertEqual(body["model"], "google/gemini-3.8-flash")
        [message] = body["messages"]
        self.assertEqual(message["content"][0], {"type": "text", "text": PROMPT})
        self.assertEqual(message["content"][1], {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAE="}})
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["usage"], {"include": True})

    def test_the_billed_cost_is_kept_for_the_log(self):
        summariser = OpenRouterVideoSummariser(FakeTransport(cost=0.0042))
        summariser.summarise(b"\x00", "video/quicktime")
        self.assertEqual(summariser.last_usage["cost"], 0.0042)

    def test_a_clip_too_large_to_send_inline_is_refused_before_any_request(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(VideoSummaryError, "too large"):
            OpenRouterVideoSummariser(transport).summarise(b"\x00" * (INLINE_LIMIT + 1), "video/mp4")
        self.assertEqual(transport.posts, [])

    def test_a_provider_failure_is_named_and_never_carries_provider_text(self):
        transport = FakeTransport(error=OpenRouterUnavailable("upstream echoed: the customer's words"))
        with self.assertRaises(VideoSummaryError) as caught:
            OpenRouterVideoSummariser(transport).summarise(b"\x00", "video/mp4")
        self.assertEqual(str(caught.exception), "unavailable")

    def test_an_empty_answer_is_an_error_not_a_blank_description(self):
        with self.assertRaisesRegex(VideoSummaryError, "empty summary"):
            OpenRouterVideoSummariser(FakeTransport(text="   ")).summarise(b"\x00", "video/mp4")


class ChoiceTests(unittest.TestCase):
    def test_openrouter_describes_videos_whenever_its_key_is_set(self):
        chosen = summariser_from_env({"OPENROUTER_API_KEY": "sk-or-test", "GEMINI_API_KEY": "g-test"})
        self.assertIsInstance(chosen, OpenRouterVideoSummariser)
        self.assertEqual(chosen.provider, "openrouter")
        self.assertTrue(chosen.zdr)

    def test_gemini_direct_runs_when_asked_for_or_when_it_is_the_only_key(self):
        client = object()
        asked = summariser_from_env({"OPENROUTER_API_KEY": "sk-or-test", "GEMINI_API_KEY": "g-test",
                                     "EMOTORAD_VIDEO_SUMMARY": "gemini"}, client=client)
        only = summariser_from_env({"GEMINI_API_KEY": "g-test"}, client=client)
        for chosen in (asked, only):
            self.assertIsInstance(chosen, GeminiVideoSummariser)
            self.assertEqual(chosen.provider, "gemini")

    def test_no_key_means_the_frames_fallback(self):
        self.assertIsNone(summariser_from_env({}))
        self.assertIsNone(summariser_from_env({"OPENROUTER_API_KEY": "  ", "GEMINI_API_KEY": ""}))


if __name__ == "__main__":
    unittest.main()
