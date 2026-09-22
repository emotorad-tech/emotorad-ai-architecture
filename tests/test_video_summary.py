"""Gemini turns a customer's clip into text, once, at ingest.

The SDK is never imported here and the real API is never called: a fake client
records what the summariser asks of it. What matters is the shape of the calls
— inline bytes under the ceiling, the Files API above it, the upload deleted
from Google whatever happens — and that an SDK failure never leaks request
content through its message.
"""

import unittest
from types import SimpleNamespace

from emotorad_ai.video_summary import (
    DEFAULT_MODEL,
    INLINE_LIMIT,
    PROMPT,
    GeminiVideoSummariser,
    VideoSummaryError,
    summariser_from_env,
)


class _Models:
    def __init__(self, client, text="a video", error=None):
        self._client = client
        self.text = text
        self.error = error

    def generate_content(self, *, model, contents, config=None):
        self._client.calls.append(("generate", model, contents))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


class _Files:
    def __init__(self, client, states=("PROCESSING", "ACTIVE")):
        self._client = client
        self._states = list(states)

    def upload(self, *, file, config=None):
        self._client.calls.append(("upload", config))
        return SimpleNamespace(name="files/abc", state="PROCESSING")

    def get(self, *, name):
        self._client.calls.append(("get", name))
        state = self._states.pop(0) if self._states else "ACTIVE"
        return SimpleNamespace(name=name, state=state, error=None)

    def delete(self, *, name):
        self._client.calls.append(("delete", name))
        return SimpleNamespace()


class _Client:
    def __init__(self, text="a video", error=None, states=("PROCESSING", "ACTIVE")):
        self.calls = []
        self.models = _Models(self, text=text, error=error)
        self.files = _Files(self, states=states)


class _Clock:
    """A monotonic clock the test advances, so the poll loop's deadline is
    exercised without sleeping."""

    def __init__(self, step=0.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def _summariser(client, **kw):
    kw.setdefault("clock", _Clock())
    return GeminiVideoSummariser("key", client=client, sleep=lambda _s: None, **kw)


class InlinePathTests(unittest.TestCase):
    def test_a_small_clip_goes_inline_with_the_prompt(self):
        client = _Client(text="  The clip shows a battery pack.  ")
        text = _summariser(client).summarise(b"\x00" * 100, "video/mp4", name="z.mp4")
        self.assertEqual(text, "The clip shows a battery pack.")
        (kind, model, contents), = client.calls
        self.assertEqual(kind, "generate")
        self.assertEqual(model, DEFAULT_MODEL)
        self.assertEqual(DEFAULT_MODEL, "gemini-3.8-flash")
        part, prompt = contents
        self.assertEqual(prompt, PROMPT)
        # The PartDict shape, which is what `types.Part.from_bytes` produces
        # (`model_dump`), so the fake sees exactly what the SDK would send.
        self.assertEqual(part, {"inline_data": {"data": b"\x00" * 100, "mime_type": "video/mp4"}})

    def test_no_files_api_calls_for_a_small_clip(self):
        client = _Client()
        _summariser(client).summarise(b"x", "video/mp4")
        self.assertEqual([c[0] for c in client.calls], ["generate"])


class FilesPathTests(unittest.TestCase):
    def setUp(self):
        self.big = b"\x00" * (INLINE_LIMIT + 1)

    def test_a_large_clip_is_uploaded_polled_then_deleted(self):
        client = _Client(text="big clip")
        text = _summariser(client).summarise(self.big, "video/mp4", name="big.mp4")
        self.assertEqual(text, "big clip")
        kinds = [c[0] for c in client.calls]
        self.assertEqual(kinds[0], "upload")
        self.assertIn("get", kinds)
        self.assertEqual(kinds[-2:], ["generate", "delete"])
        self.assertLess(kinds.index("get"), kinds.index("generate"))
        self.assertEqual(client.calls[0][1], {"mime_type": "video/mp4"})
        self.assertEqual(client.calls[-1], ("delete", "files/abc"))
        # The file object, not the bytes, is what goes to the model.
        _, _, contents = [c for c in client.calls if c[0] == "generate"][0]
        self.assertEqual(contents[0].name, "files/abc")
        self.assertEqual(contents[1], PROMPT)

    def test_the_upload_is_deleted_even_when_generate_raises(self):
        client = _Client(error=RuntimeError("quota"))
        with self.assertRaises(VideoSummaryError):
            _summariser(client).summarise(self.big, "video/mp4")
        self.assertEqual(client.calls[-1], ("delete", "files/abc"))

    def test_a_file_that_never_becomes_active_times_out_and_is_deleted(self):
        client = _Client(states=["PROCESSING"] * 1000)
        with self.assertRaises(VideoSummaryError) as ctx:
            _summariser(client, clock=_Clock(step=10.0)).summarise(self.big, "video/mp4")
        self.assertEqual(str(ctx.exception), "timeout")
        self.assertEqual(client.calls[-1], ("delete", "files/abc"))
        self.assertNotIn("generate", [c[0] for c in client.calls])

    def test_a_failed_file_state_raises_and_is_deleted(self):
        client = _Client(states=["FAILED"])
        with self.assertRaises(VideoSummaryError):
            _summariser(client).summarise(self.big, "video/mp4")
        self.assertEqual(client.calls[-1], ("delete", "files/abc"))


class FailureTests(unittest.TestCase):
    def test_empty_text_raises(self):
        with self.assertRaises(VideoSummaryError) as ctx:
            _summariser(_Client(text="   ")).summarise(b"x", "video/mp4")
        self.assertEqual(str(ctx.exception), "empty summary")

    def test_none_text_raises(self):
        with self.assertRaises(VideoSummaryError):
            _summariser(_Client(text=None)).summarise(b"x", "video/mp4")

    def test_an_sdk_error_carries_only_the_class_name(self):
        class QuotaExhausted(Exception):
            pass

        client = _Client(error=QuotaExhausted("prompt was: customer said battery on fire"))
        with self.assertRaises(VideoSummaryError) as ctx:
            _summariser(client).summarise(b"x", "video/mp4")
        self.assertEqual(str(ctx.exception), "QuotaExhausted")


class FromEnvTests(unittest.TestCase):
    def test_no_key_means_no_summariser(self):
        self.assertIsNone(summariser_from_env({}))
        self.assertIsNone(summariser_from_env({"GEMINI_API_KEY": "   "}))

    def test_a_key_builds_one(self):
        client = _Client()
        summariser = summariser_from_env({"GEMINI_API_KEY": "k"}, client=client)
        self.assertIsInstance(summariser, GeminiVideoSummariser)
        self.assertEqual(summariser.model, DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
