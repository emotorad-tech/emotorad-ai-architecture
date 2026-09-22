# tests/test_agent_attachments.py
"""An evidence photo sent over the API reaches the model as an image block, and
an S3 key is fetched through the store the runtime was given."""

import base64
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry

PNG = base64.b64encode(b"\x89PNG fake").decode()


class _Store:
    def __init__(self):
        self.fetched = []

    def get_bytes(self, key):
        self.fetched.append(key)
        return b"\x89PNG fake"


def make_runtime(script, store=None):
    registry = build_registry(today=date(2026, 8, 6))
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None), registry=registry, llm=llm,
        log=EventLog(path=None, to_stdout=False), resolver=IdentityResolver(registry), media_store=store,
    )
    return runtime, llm


def send(runtime, attachments, text="what is this light"):
    adapter = WebsiteChatAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"conversation_id": "c1", "session_token": "sess-ananya", "text": text, "pill": "battery_issue", "attachments": attachments}))


class AttachmentTests(unittest.TestCase):
    def test_a_data_url_reaches_the_model_as_an_image_block(self):
        runtime, llm = make_runtime([say("That is the charge indicator.")])
        send(runtime, [{"kind": "image", "url": "data:image/png;base64," + PNG, "mime_type": "image/png"}])
        first_user_turn = llm.requests[0]["messages"][0]["content"]
        self.assertEqual(first_user_turn[0]["type"], "image")
        self.assertEqual(first_user_turn[-1], {"type": "text", "text": "what is this light"})

    def test_an_s3_key_is_fetched_through_the_runtime_store(self):
        store = _Store()
        runtime, llm = make_runtime([say("ok")], store=store)
        send(runtime, [{"kind": "image", "url": "s3://customers/clu_1/c1/images/upl_1.png", "mime_type": "image/png"}])
        self.assertEqual(store.fetched, ["customers/clu_1/c1/images/upl_1.png"])
        self.assertEqual(llm.requests[0]["messages"][0]["content"][0]["type"], "image")

    def test_text_only_history_is_unchanged(self):
        runtime, llm = make_runtime([say("ok")])
        send(runtime, [], text="battery won't charge")
        self.assertEqual(llm.requests[0]["messages"][0]["content"], "battery won't charge")


if __name__ == "__main__":
    unittest.main()


def _assert_no_empty_content(case, turn):
    """The Anthropic API rejects a message whose content is empty *and* a text
    block whose text is empty, so both shapes are checked."""
    content = turn["content"]
    if isinstance(content, str):
        case.assertTrue(content.strip(), "blank turn: %r" % turn)
        return
    case.assertTrue(content, "empty content list: %r" % turn)
    for block in content:
        if block.get("type") == "text":
            case.assertTrue(block["text"].strip(), "empty text block in turn: %r" % turn)


class SafetyStopThenPhotoTests(unittest.TestCase):
    """Staging, 2026-09-22, conversation 8279e535: a clip with no typed text tripped the
    battery-safety stop; the customer's next photo then got the model-unavailable
    fallback because the transcript held a user turn with empty content, which the
    Anthropic API rejects with a 400."""

    def _safety_stop_on_video(self, runtime):
        clip = {
            "kind": "video", "url": "s3://customers/clu_1/c1/videos/upl_1.mp4",
            "mime_type": "video/mp4", "summary": "White smoke rises from the battery pack.",
        }
        reply = send(runtime, [clip], text="")
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")

    def test_a_video_only_safety_stop_leaves_no_empty_user_turn_in_history(self):
        runtime, llm = make_runtime([say("Please keep away from the battery.")])
        self._safety_stop_on_video(runtime)
        self.assertEqual(llm.requests, [], "the safety stop must not call the model")
        state = runtime.conversations.get("c1")
        for turn in state.history:
            _assert_no_empty_content(self, turn)

    def test_the_next_photo_after_a_video_safety_stop_reaches_the_model(self):
        runtime, llm = make_runtime([say("That is the pack; please keep it outdoors.")])
        self._safety_stop_on_video(runtime)
        reply = send(runtime, [{"kind": "image", "url": "data:image/png;base64," + PNG, "mime_type": "image/png"}], text="")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertFalse(reply.escalated)
        # Every turn the model receives must have non-empty content, block by block.
        for turn in llm.requests[0]["messages"]:
            _assert_no_empty_content(self, turn)
