"""The unlisted bike through the rest of the conversation
(spec 2026-10-01-unlisted-bike-design.md)."""

import unittest
from datetime import date

from emotorad_ai.llm import call_tool, say
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, UNLISTED_SOURCE, build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.triage import ASK_FOR_UNLISTED_BIKE
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

BIKE = {"frame_number": "EMXP2026009999", "model": "T-Rex Air"}


def unlisted(chat, phone=RIDER, not_mine="its not one of these 2"):
    chat.verify(phone, first="hi")
    assert ASK_FOR_UNLISTED_BIKE in chat.say(not_mine).text
    asked = chat.say("EMXP2026009999, T-Rex Air").text
    assert "Just to confirm: your bike is the T-Rex Air, frame EMXP2026009999. Is that right?" in asked, asked
    return chat.say("yes")


class StagingReplayTests(unittest.TestCase):
    def test_the_staging_chat(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        confirmed = unlisted(chat)
        self.assertIn("Thanks: T-Rex Air, frame EMXP2026009999. What is happening with the bike?", confirmed.text)
        reply = chat.say("battery isnt charging")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertNotIn("Which bike", reply.text)
        self.assertEqual(chat.state().unlisted_bike, BIKE)
        self.assertIn("not registered on their number: T-Rex Air, frame EMXP2026009999, as they read it",
                      chat.llm.requests[-1]["system"])

    def test_one_bike_listed_and_no(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        unlisted(chat, phone=ONE_BIKE, not_mine="no")
        self.assertEqual(chat.say("battery isnt charging").handled_by, "battery_support")
        self.assertEqual(chat.state().unlisted_bike, BIKE)


class CoverageTests(unittest.TestCase):
    def test_a_coverage_claim_about_the_unlisted_bike_is_blocked(self):
        # Ananya's listed EMX Plus is covered; the bike she is asking about is
        # not on her number, so the lookup says nothing about it.
        chat = Chat(replies=[call_tool(LOOKUP_WARRANTY_RECORD, {}, "toolu_1"),
                             say("Good news, it's covered under warranty.")])
        unlisted(chat, phone=ONE_BIKE, not_mine="no")
        reply = chat.say("battery isnt charging, is it under warranty?")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertNotIn("covered under warranty", reply.text)


class TicketTests(unittest.TestCase):
    def ticket(self, arguments, late):
        registry = build_registry(today=date(2026, 10, 1))
        envelope = registry.call(CREATE_SUPPORT_TICKET, dict({
            "category": "battery_charging", "description": "Not charging.", "severity": "normal",
            "idempotency_key": "k1"}, **arguments),
            ToolContext(conversation_id="c1", phone=RIDER, late=dict({"evidence_seen": lambda: True}, **late)))
        return registry.tickets.tickets[envelope["data"]["ticket_id"]]

    def test_a_ticket_for_the_unlisted_bike_carries_its_frame(self):
        ticket = self.ticket({}, {"unlisted_bike": lambda: dict(BIKE)})
        self.assertEqual((ticket["frame_number"], ticket["bike_model"], ticket["frame_number_source"]),
                         ("EMXP2026009999", "T-Rex Air", UNLISTED_SOURCE))

    def test_naming_the_unlisted_frame_is_accepted(self):
        ticket = self.ticket({"frame_number": "EMXP2026009999"}, {"unlisted_bike": lambda: dict(BIKE)})
        self.assertEqual(ticket["frame_number_source"], UNLISTED_SOURCE)

    def test_a_safety_ticket_carries_the_unlisted_frame(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        unlisted(chat)
        reply = chat.say("my battery is smoking")
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        ticket = chat.registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["frame_number"], ticket["frame_number_source"]), ("EMXP2026009999", UNLISTED_SOURCE))


class FinalReviewRuntimeTests(unittest.TestCase):
    """The final review (2026-10-01)."""

    def test_no_replacement_is_ordered_for_the_unlisted_bike(self):
        from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders, load_parts_table
        from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER

        registry = build_registry(today=date(2026, 10, 1), replacement_orders=ReplacementOrders(),
                                  item_codes=ItemCodes(), approval_mode="reasonable")
        part = next(p for p, r in load_parts_table().items() if not r.technician and not r.ask)
        envelope = registry.call(
            PLACE_REPLACEMENT_ORDER, {"part": part, "use_record_address": True, "idempotency_key": "o1"},
            ToolContext(conversation_id="c1", phone=ONE_BIKE, late={
                "evidence_seen": lambda: True, "coverage_result": lambda: {}, "customer_messages": lambda: [],
                "unlisted_bike": lambda: dict(BIKE)}))
        self.assertEqual(envelope["error"]["code"], "unlisted_bike")
        self.assertEqual(envelope["error"]["remedy"], "human_handoff")

    def test_the_agent_is_told_the_bike_is_none_of_the_listed_ones(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        unlisted(chat)
        chat.say("battery isnt charging")
        system = chat.llm.requests[-1]["system"]
        self.assertNotIn("Do NOT assume which one", system)
        self.assertNotIn("EMXP2026001234", system)
        self.assertIn("T-Rex Air frame EMXP2026009999", system)
        self.assertIn("Coverage: NO RECORD ON THIS NUMBER", system)

    def test_verifying_again_and_choosing_a_listed_bike_drops_the_unlisted_one(self):
        now = [0.0]
        chat = Chat(replies=[say("Is the charger light on?")] * 4, clock=lambda: now[0])
        unlisted(chat)
        now[0] += 13 * 60 * 60  # the verification lapses; the conversation does not
        chat.verify(RIDER, first="my battery isn't charging")
        chat.say("1")
        self.assertIsNone(chat.state().unlisted_bike)
        self.assertEqual(chat.state().selected_frame, "EMXP2026001234")

    def test_the_unlisted_frame_never_reaches_jev(self):
        from emotorad_ai.conversation import ConversationState
        from emotorad_ai.runtime import Runtime
        from tests.test_triage import BIKES, resolved

        state = ConversationState("c1")
        state.unlisted_bike = dict(BIKE)
        self.assertIn("EMXP2026009999", Runtime._redaction_terms(resolved(BIKES), state))

    def test_a_safety_report_while_collecting_is_not_put_on_the_rejected_bike(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        chat.verify(ONE_BIKE, first="hi")
        chat.say("no")
        reply = chat.say("my battery is smoking")
        ticket = chat.registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["frame_number"], ticket["frame_number_source"]), (None, None))
