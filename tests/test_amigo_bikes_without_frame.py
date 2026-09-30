"""A bike whose frame number is not on record (an app bike registered by
IMEI): chosen by its internal reference, shown as "frame number not on
record", never showing its IMEI or VIN (spec section 2)."""

import unittest
from datetime import date

from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, ConversationState
from emotorad_ai.enrichment import ContextEnricher
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import merged_source
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.triage import TriageAgent, bike_ref, describe_bike, which_bike_text
from tests.amigo_fake import RIDER_A, RIDER_B, FakeAmigo

TODAY = date(2026, 9, 30)
SECRET_BITS = ("860000000000032", "FRPVINTEST", "vin:")


def registry(**kwargs):
    return build_registry(today=TODAY, warranty_source=merged_source(fixtures.WARRANTY_RECORDS.get, FakeAmigo()),
                          **kwargs)


def resolved_for(phone, reg):
    message = InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="hi",
                             identity=Identity(strength=VERIFIED, phone=phone, em_aid="a"))
    return IdentityResolver(reg).hydrate(message)


class ShownTests(unittest.TestCase):
    def setUp(self):
        self.reg = registry()
        self.b = resolved_for(RIDER_B, self.reg)

    def test_the_list_says_frame_number_not_on_record(self):
        self.assertEqual(describe_bike(self.b.bikes[0]), "T-Rex Smart (Grey), frame number not on record")
        text = which_bike_text(self.b.bikes)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, text)

    def test_the_context_never_shows_the_imei_or_vin(self):
        block = ContextEnricher().build(self.b).render()
        self.assertIn("frame number not on record", block)
        self.assertIn("warranty not on record", block)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, block)


class ChosenTests(unittest.TestCase):
    def test_a_bike_without_a_frame_is_selected_by_its_reference(self):
        reg = registry()
        who = resolved_for(RIDER_B, reg)
        state = ConversationState("c1")
        state.move_to(AWAITING_BIKE_SELECTION, "verified")
        state.pending_topic = "battery"
        outcome = TriageAgent({"battery": "battery_support"}, unlisted_agent="late").handle(
            InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="yes",
                           identity=Identity(strength=VERIFIED, phone=RIDER_B, em_aid="a")), who, state)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.selected_frame, "vin:FRPVINTEST0000000000000b")
        self.assertEqual(bike_ref(who.bikes[0]), state.selected_frame)


class TicketTests(unittest.TestCase):
    def call(self, reg, name, arguments, phone=RIDER_B):
        return reg.call(name, arguments, ToolContext(conversation_id="c1", phone=phone))

    def test_a_ticket_takes_the_frame_the_rider_reads_out_and_marks_it(self):
        reg = registry()
        envelope = self.call(reg, CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "description": "Battery not charging.", "severity": "normal",
            "idempotency_key": "k1", "frame_number": "TRSM2026009911"})
        self.assertNotIn("error", envelope, envelope)
        ticket = reg.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertEqual(ticket["frame_number"], "TRSM2026009911")
        self.assertEqual(ticket["frame_number_source"], "read by the rider")

    def test_a_ticket_without_a_frame_is_raised_without_one(self):
        reg = registry()
        envelope = self.call(reg, CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "description": "Battery not charging.", "severity": "normal",
            "idempotency_key": "k2"})
        ticket = reg.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertIsNone(ticket["frame_number"])

    def test_a_listed_bikes_frame_is_still_checked(self):
        reg = registry()
        envelope = self.call(reg, CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "description": "x", "severity": "normal", "idempotency_key": "k3",
            "frame_number": "NOTMINE123"}, phone=RIDER_A)
        self.assertEqual(envelope["error"]["code"], "frame_number_not_owned")

    def test_a_replacement_for_a_bike_without_a_frame_is_refused(self):
        from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders, load_parts_table

        reg = registry(replacement_orders=ReplacementOrders(), item_codes=ItemCodes(), approval_mode="reasonable")
        part = next(p for p, r in load_parts_table().items() if not r.technician and not r.ask)
        envelope = reg.call(
            PLACE_REPLACEMENT_ORDER, {"part": part, "use_record_address": True, "idempotency_key": "o1"},
            ToolContext(conversation_id="c1", phone=RIDER_B, late={
                "evidence_seen": lambda: True, "coverage_result": lambda: {}, "customer_messages": lambda: []}))
        self.assertEqual(envelope["error"]["code"], "frame_number_not_on_record")


if __name__ == "__main__":
    unittest.main()
