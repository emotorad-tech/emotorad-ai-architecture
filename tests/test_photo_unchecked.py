"""A photo the safety check could not answer, and the agents' photo safety
rule (spec 2026-10-02)."""

import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents import late_warranty
from emotorad_ai.agents.base import PHOTO_SAFETY_RULE
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.decisions import Q_LANGUAGE, Q_STANDARD
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, choose
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.photo_check import UNCHECKED_NOTE
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from tests.test_jev_runtime import build
from tests.test_verify_first import Chat, jpeg

TODAY = date(2026, 10, 2)
SWITCH = {"id": "afs/battery/photos/battery-onoff-switch.png", "kind": "image",
          "caption": "The battery On/Off switch"}


class UncheckedPhotoTests(unittest.TestCase):
    def test_the_agent_is_told_on_that_turn_only(self):
        chat = Chat(replies=[say("Is the charger light on?"), say("Thanks for the photo.")])
        chat.verify()
        chat.say("1")
        reply = chat.say("this is what I see", photo=True, photos_unchecked=1)
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertIn(UNCHECKED_NOTE, chat.llm.requests[-1]["system"])
        self.assertNotIn(UNCHECKED_NOTE, chat.llm.requests[0]["system"])

    def test_jev_sends_it_to_a_model_not_a_standard_reply(self):
        runtime, adapter, jev, narrow, fallback = build(
            [JevDecision(answers={Q_STANDARD: choose("std-thanks", 0.97), Q_LANGUAGE: choose("english", 0.95)})],
            fallback=[say("I can see the photo.")],
        )
        message = adapter.to_message({"conversation_id": "conv-1", "session_token": "sess-ananya",
                                      "text": "my battery won't charge, see this",
                                      "attachments": [{"kind": "image", "url": jpeg()}]})
        reply = runtime.handle(replace(message, entry_metadata=dict(message.entry_metadata, photos_unchecked=1)))
        self.assertNotEqual(reply.handled_by, "standard:std-thanks")
        self.assertEqual(jev.calls, [])
        self.assertIn(UNCHECKED_NOTE, fallback.requests[-1]["system"])


class PromptRuleTests(unittest.TestCase):
    def system_for(self, agent, guide_media=None):
        registry = build_registry(today=TODAY, guide_media=guide_media or {}, sent_media={})
        rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([say("Thanks.")]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry),
                     conversations=InMemoryConversationStore())
        rt.conversations.get("c1").route_to(agent)
        rt.handle(WebsiteChatAdapter(rt.resolver).to_message(
            {"conversation_id": "c1", "session_token": "sess-ananya", "text": "here is my bike"}))
        return rt.llm.requests[0]["system"]

    def test_the_registration_agent_gets_the_safety_rule_and_the_new_pictures_wording(self):
        system = self.system_for(late_warranty.AGENT_NAME)
        self.assertIn(PHOTO_SAFETY_RULE, system)
        self.assertIn("You cannot send pictures or videos to the customer in this chat. "
                      "The customer can send you photos and videos.", system)
        self.assertNotIn("no pictures or videos can be sent in this chat", system)

    def test_an_agent_with_guide_pictures_gets_it_too(self):
        self.assertIn(PHOTO_SAFETY_RULE, self.system_for("battery_support", {"battery_onoff_switch": SWITCH}))

    def test_the_rule_names_each_live_hazard(self):
        for word in ("smoke", "flames", "swelling", "leaking fluid", "sparks", "stop using and charging"):
            self.assertIn(word, PHOTO_SAFETY_RULE)
