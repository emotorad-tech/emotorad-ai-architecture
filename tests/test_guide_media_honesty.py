"""The bot never claims to show a picture it did not send.

Found by the person on 2026-09-29, testing the chat page: "I can show you where
it is", then, on "yes show me", "That's the battery On/Off switch" with nothing
on the screen. The event log: send_guide_media failed (the store could not sign
a link for the asset), the failure reached the model as a bare
`tool_exception: StorageError ...` without the tool's usual instruction not to
claim a picture, and the model described the picture anyway.

Three parts: `media.resolve` reports a link it could not sign as unresolved
(its docstring always promised it never raises), so the tool gives its honest
`guide_media_unavailable`; every agent that can send pictures is given the
picture rule in its prompt; and when a picture failed and nothing went out, a
reply that does not already say so gets one plain sentence saying it.
"""

import unittest
from datetime import date

from emotorad_ai import media
from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import GUIDE_MEDIA_RULE
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import MEDIA_NOT_SENT_TEXT, Runtime
from emotorad_ai.storage.s3 import StorageError
from emotorad_ai.tools.mocks import SEND_GUIDE_MEDIA, build_registry
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 7, 28)
SWITCH = {"battery_onoff_switch": {"id": "afs/battery/photos/battery-onoff-switch.png", "kind": "image",
                                   "caption": "The battery On/Off switch: set it to ON before charging"}}
CLAIM = "That's the battery On/Off switch. Make sure it's set to ON before charging."


class CannotSign:
    def presign_get(self, key):
        raise StorageError("a local folder cannot sign a link for %r" % key)


class Signs:
    def presign_get(self, key):
        return "https://bucket.example/" + key + "?X-Amz-Signature=abc"


class StoreSwap(unittest.TestCase):
    """media.resolve reads a process-wide store; swap it for the test."""

    def use_store(self, store):
        saved = (media._store, media._store_loaded)
        media._store, media._store_loaded = store, True
        self.addCleanup(lambda: setattr(media, "_store", saved[0]) or setattr(media, "_store_loaded", saved[1]))


class ResolveTests(StoreSwap):
    def test_a_link_that_cannot_be_signed_is_unresolved_not_raised(self):
        found = media.resolve(SWITCH["battery_onoff_switch"], store=CannotSign())
        self.assertTrue(found["unresolved"])
        self.assertIn("StorageError", found["reason"])
        self.assertIsNone(found["url"])

    def test_the_tool_then_says_nothing_was_sent_and_not_to_claim_it(self):
        self.use_store(CannotSign())
        registry = build_registry(today=TODAY, guide_media=SWITCH, sent_media={})
        envelope = registry.call(SEND_GUIDE_MEDIA, {"key": "battery_onoff_switch"}, ToolContext(conversation_id="c1"))
        self.assertEqual(envelope["error"]["code"], "guide_media_unavailable")
        self.assertIn("do not tell the customer you have sent a picture", envelope["error"]["message"])


def runtime(replies):
    registry = build_registry(today=TODAY, guide_media=SWITCH, sent_media={})
    rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(replies)),
                 log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=InMemoryConversationStore())
    rt.conversations.get("c1").route_to("battery_support")
    return rt


def send(rt, text):
    return rt.handle(WebsiteChatAdapter(rt.resolver).to_message(
        {"conversation_id": "c1", "session_token": "sess-ananya", "text": text}))


def assistant_texts(rt):
    out = []
    for entry in rt.conversations.peek("c1").history:
        if entry["role"] == "assistant" and isinstance(entry["content"], list):
            out.extend(b.get("text", "") for b in entry["content"] if b.get("type") == "text")
    return out


class PromptRuleTests(StoreSwap):
    def test_an_agent_that_can_send_pictures_is_given_the_rule(self):
        self.use_store(Signs())
        rt = runtime([say("Is the battery key set to ON?")])
        send(rt, "my battery won't charge")
        self.assertIn(GUIDE_MEDIA_RULE, rt.llm.requests[0]["system"])

    def test_the_rule_says_what_to_do_when_sending_fails(self):
        self.assertIn("only after", GUIDE_MEDIA_RULE)
        self.assertIn("cannot show", GUIDE_MEDIA_RULE)

    def test_an_agent_without_the_tool_is_not(self):
        registry = build_registry(today=TODAY)  # no guide media: the tool is not registered
        rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([say("Hello.")]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=InMemoryConversationStore())
        rt.conversations.get("c1").route_to("battery_support")
        send(rt, "my battery won't charge")
        self.assertNotIn(GUIDE_MEDIA_RULE, rt.llm.requests[0]["system"])


class BackstopTests(StoreSwap):
    def test_a_picture_claimed_after_it_failed_gets_the_plain_admission(self):
        self.use_store(CannotSign())
        rt = runtime([call_tool(SEND_GUIDE_MEDIA, {"key": "battery_onoff_switch"}, "toolu_1"), say(CLAIM)])
        answer = send(rt, "yes show me")
        self.assertTrue(answer.text.endswith(MEDIA_NOT_SENT_TEXT))
        self.assertEqual(answer.attachments, [])
        self.assertTrue(assistant_texts(rt)[-1].endswith(MEDIA_NOT_SENT_TEXT))
        [event] = [e for e in rt.log.events if e["event"] == "guide_media_not_sent"]
        self.assertEqual(event["keys"], ["battery_onoff_switch"])

    def test_a_picture_that_went_out_needs_no_admission(self):
        # The pair to the test above: only the store differs.
        self.use_store(Signs())
        rt = runtime([call_tool(SEND_GUIDE_MEDIA, {"key": "battery_onoff_switch"}, "toolu_1"), say(CLAIM)])
        answer = send(rt, "yes show me")
        self.assertNotIn(MEDIA_NOT_SENT_TEXT, answer.text)
        self.assertEqual(len(answer.attachments), 1)

    def test_a_reply_that_already_says_it_cannot_show_it_is_left_alone(self):
        self.use_store(CannotSign())
        honest = "I can't show you the picture right now. The switch is on the side of the battery, near the key."
        rt = runtime([call_tool(SEND_GUIDE_MEDIA, {"key": "battery_onoff_switch"}, "toolu_1"), say(honest)])
        answer = send(rt, "yes show me")
        self.assertNotIn(MEDIA_NOT_SENT_TEXT, answer.text)
        self.assertEqual([e for e in rt.log.events if e["event"] == "guide_media_not_sent"], [])


if __name__ == "__main__":
    unittest.main()
