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
