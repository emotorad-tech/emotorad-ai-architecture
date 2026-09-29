"""The bot offers a picture only when it can send one.

The person's rule, 2026-09-29: "if it doesn't even have the picture, I don't
even want it to offer that help." The model offered "I can show you where it
is" because every picture in the catalogue reached it, whether or not this
server could deliver any: the tool listed them all, the narrow prompt listed a
record's media (by its file path, which the tool does not even accept), and
the knowledge search returned them. Now the server works out at startup which
pictures it can send (media.sendable), and only those reach the model, by the
key the tool takes. With none, the tool is not offered and every customer agent
is told that no picture can be sent.
"""

import unittest
from datetime import date

from emotorad_ai import media
from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import GUIDE_MEDIA_RULE, NO_PICTURES_RULE
from emotorad_ai.agents.narrow_support import build_narrow_definition
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.storage.s3 import StorageError
from emotorad_ai.tools.mocks import SEARCH_BATTERY_KNOWLEDGE, SEND_GUIDE_MEDIA, build_registry
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 7, 28)
SWITCH_ID = "afs/battery/photos/battery-onoff-switch.png"
CATALOGUE = {
    "battery_onoff_switch": {"id": SWITCH_ID, "kind": "image", "caption": "The battery On/Off switch"},
    "hosted_elsewhere": {"url": "https://cdn.example/guide.jpg", "kind": "image", "caption": "Hosted"},
}


class CannotSign:
    def presign_get(self, key):
        raise StorageError("a local folder cannot sign a link for %r" % key)


class Signs:
    def presign_get(self, key):
        return "https://bucket.example/" + key + "?X-Amz-Signature=abc"


class SendableTests(unittest.TestCase):
    def test_a_picture_whose_link_cannot_be_signed_is_left_out_with_the_reason(self):
        kept, dropped = media.sendable(CATALOGUE, store=CannotSign())
        self.assertEqual(sorted(kept), ["hosted_elsewhere"])
        self.assertIn("StorageError", dropped["battery_onoff_switch"])

    def test_a_picture_that_can_be_signed_is_kept(self):
        kept, dropped = media.sendable(CATALOGUE, store=Signs())
        self.assertEqual(sorted(kept), ["battery_onoff_switch", "hosted_elsewhere"])
        self.assertEqual(dropped, {})
        self.assertEqual(kept["battery_onoff_switch"], CATALOGUE["battery_onoff_switch"])  # the item, not a URL


class NoPicturesTests(unittest.TestCase):
    def runtime(self, guide_media):
        registry = build_registry(today=TODAY, guide_media=guide_media, sent_media={})
        rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([say("Is the key set to ON?")]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=InMemoryConversationStore())
        rt.conversations.get("c1").route_to("battery_support")
        rt.handle(WebsiteChatAdapter(rt.resolver).to_message(
            {"conversation_id": "c1", "session_token": "sess-ananya", "text": "my battery won't charge"}))
        return rt

    def test_with_none_sendable_the_tool_is_not_offered_and_the_agent_is_told(self):
        rt = self.runtime(guide_media={})
        request = rt.llm.requests[0]
        self.assertNotIn(SEND_GUIDE_MEDIA, [tool["name"] for tool in request["tools"]])
        self.assertIn(NO_PICTURES_RULE, request["system"])
        self.assertNotIn(GUIDE_MEDIA_RULE, request["system"])

    def test_with_some_sendable_it_is_the_picture_rule_instead(self):
        rt = self.runtime(guide_media={"battery_onoff_switch": CATALOGUE["battery_onoff_switch"]})
        request = rt.llm.requests[0]
        self.assertIn(SEND_GUIDE_MEDIA, [tool["name"] for tool in request["tools"]])
        self.assertIn(GUIDE_MEDIA_RULE, request["system"])
        self.assertNotIn(NO_PICTURES_RULE, request["system"])

    def test_the_no_pictures_rule_says_never_to_offer_one(self):
        self.assertIn("never offer", NO_PICTURES_RULE.lower())


class NarrowListingTests(unittest.TestCase):
    def setUp(self):
        self.record = next(r for r in KnowledgeBase().records if r.id == "battery-onoff-switch-dead")
        self.assertTrue(any(item.get("id") == SWITCH_ID for item in self.record.media))

    def prompt(self, sendable):
        from emotorad_ai.contract import InboundMessage, Identity
        from emotorad_ai.identity import ResolvedIdentity

        definition = build_narrow_definition(self.record, [], sendable=sendable)
        message = InboundMessage(conversation_id="c1", persona="customer", identity=Identity(), channel="website_chat",
                                 message_text="the switch")
        return definition.build_system_prompt(message, ResolvedIdentity(persona="customer", method="anonymous"), "")

    def test_a_sendable_picture_is_listed_by_the_key_the_tool_takes(self):
        text = self.prompt(sendable={"battery_onoff_switch": CATALOGUE["battery_onoff_switch"]})
        self.assertIn("- battery_onoff_switch:", text)
        self.assertNotIn("- " + SWITCH_ID, text)

    def test_with_nothing_sendable_no_picture_is_listed(self):
        text = self.prompt(sendable={})
        self.assertNotIn("Guide media you can send", text)
        self.assertNotIn(SWITCH_ID, text)


class SearchResultTests(unittest.TestCase):
    def search(self, guide_media):
        registry = build_registry(today=TODAY, guide_media=guide_media, sent_media={})
        envelope = registry.call(SEARCH_BATTERY_KNOWLEDGE, {"query": "battery on off switch not working"},
                                 ToolContext(conversation_id="c1", phone="+919876543210"))
        return envelope["data"]["passages"]

    def test_the_search_names_only_pictures_that_can_be_sent_by_their_key(self):
        passages = self.search({"battery_onoff_switch": CATALOGUE["battery_onoff_switch"]})
        with_media = [p for p in passages if p.get("media")]
        self.assertTrue(with_media)
        for passage in with_media:
            for item in passage["media"]:
                self.assertEqual(set(item), {"key", "kind", "caption"})
                self.assertEqual(item["key"], "battery_onoff_switch")

    def test_with_nothing_sendable_the_search_names_no_picture(self):
        passages = self.search({})
        self.assertTrue(passages)
        self.assertFalse([p for p in passages if p.get("media")])


if __name__ == "__main__":
    unittest.main()
