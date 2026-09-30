"""The final review's findings on the Amigo branch, each pinned by a test.

C1 the VIN in the lookup result, C2 the safety ticket when Amigo drops,
I1 an IMEI bike that is also in the OMS, I2 registration the bot cannot do,
I3 a frame typed for the only frame-less bike, I4 what a rider may read off
the sticker, I5 an outage read on every call."""

import logging
import unittest
from datetime import date
from unittest import mock

from emotorad_ai.llm import call_tool, say
from emotorad_ai.agents import battery_support
from emotorad_ai.enrichment import ContextEnricher
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import AmigoReader, AmigoUnavailable, merged_source
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, _coverage, _owned_bike
from emotorad_ai.tools.registry import ToolContext, ToolError
from emotorad_ai.triage import which_bike_text
from tests.amigo_fake import RIDER_A, RIDER_B, FakeAmigo
from tests.test_amigo_flow import SECRET_BITS, Chat
from tests.test_amigo_reader import DSN, FakeServer

_bikes_block = ContextEnricher()._bikes_block
OMS_BASE = dict(fixtures.WARRANTY_RECORDS["+919876543210"][0])


def oms_bike(frame, product="T-Rex Smart"):
    return dict(OMS_BASE, frame_number=frame, product_name=product, mobile=RIDER_B)


class VinStaysOutOfTheModelTests(unittest.TestCase):
    def test_the_lookup_result_carries_no_vin(self):
        chat = Chat()
        chat.verify(RIDER_B)
        envelope = chat.registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=RIDER_B))
        self.assertNotIn("error", envelope)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, repr(envelope))

    def test_the_reference_is_stable_between_reads(self):
        chat = Chat()
        first = chat.registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=RIDER_B))
        again = chat.registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c2", phone=RIDER_B))
        ref = lambda env: env["data"]["bikes"][0]["bike_ref"]  # noqa: E731
        self.assertEqual(ref(first), ref(again))

    def test_a_lookup_in_the_conversation_keeps_the_vin_from_the_model(self):
        chat = Chat(replies=[call_tool(LOOKUP_WARRANTY_RECORD, {}, "t1"), say("Is the charger light on?")])
        chat.verify(RIDER_B)
        chat.say("yes")
        requests = repr(chat.llm.requests)
        self.assertIn(LOOKUP_WARRANTY_RECORD, requests)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, requests)


class SafetyTicketWhenAmigoDropsTests(unittest.TestCase):
    def test_a_safety_report_raises_a_ticket_after_amigo_goes_down(self):
        # Rider A also has one OMS bike, so the app-only Doodle Pro vanishes
        # from the list when Amigo stops answering part-way through.
        amigo = FakeAmigo()
        with mock.patch.dict(fixtures.WARRANTY_RECORDS, {RIDER_A: [dict(OMS_BASE, frame_number="EMXP2026777777",
                                                                          mobile=RIDER_A)]}):
            chat = Chat(replies=[say("ok")] * 5, amigo=amigo)
            listed = chat.verify(RIDER_A)
            self.assertIn("3. Doodle Pro", listed.text)
            chat.say("3")
            amigo.down = True
            with self.assertLogs("emotorad_ai", level="WARNING"):
                reply = chat.say("there is smoke coming from the battery")
        self.assertIsNotNone(reply.ticket_id)
        (ticket,) = chat.registry.tickets.tickets.values()
        self.assertIn("Doodle Pro", ticket["description"])


class ImeiBikeAlsoInTheOmsTests(unittest.TestCase):
    def test_it_folds_into_the_one_oms_bike_of_its_model(self):
        source = merged_source(lambda phone: [oms_bike("TRSM2026009911")], FakeAmigo())
        (bike,) = source(RIDER_B)
        self.assertEqual(bike["frame_number"], "TRSM2026009911")
        self.assertTrue(bike["in_app"])
        self.assertNotEqual(bike.get("warranty_on_record"), False)

    def test_with_two_oms_bikes_of_its_model_it_is_not_called_unregistered(self):
        source = merged_source(lambda phone: [oms_bike("TRSM2026009911"), oms_bike("TRSM2026009922")],
                               FakeAmigo())
        bikes = source(RIDER_B)
        self.assertEqual(len(bikes), 3)
        app = _coverage(bikes[2], date(2026, 9, 30))
        self.assertEqual(app["coverage_status"], "warranty_unknown")
        self.assertNotIn("register", app["note"])
        self.assertNotIn("remedy", app)

    def test_an_oms_bike_of_another_model_leaves_it_unregistered(self):
        source = merged_source(lambda phone: [oms_bike("EMXP2026777777", "EMX Plus")], FakeAmigo())
        bikes = source(RIDER_B)
        self.assertEqual(len(bikes), 2)
        self.assertEqual(_coverage(bikes[1], date(2026, 9, 30))["coverage_status"], "not_registered")


class NoPromiseOfRegistrationTests(unittest.TestCase):
    def app_only(self):
        (bike,) = merged_source(lambda phone: None, FakeAmigo())(RIDER_B)
        return _coverage(bike, date(2026, 9, 30))

    def test_no_text_the_model_reads_offers_to_register(self):
        bike = self.app_only()
        texts = [bike["note"], _bikes_block([bike]), battery_support._coverage_line(bike)]
        for text in texts:
            self.assertNotIn("offer to register", text.lower())
            self.assertIn("ticket", text.lower())


class FrameForTheOnlyFramelessBikeTests(unittest.TestCase):
    def test_the_question_does_not_ask_for_a_frame_number_we_cannot_match(self):
        (bike,) = merged_source(lambda phone: None, FakeAmigo())(RIDER_B)
        self.assertNotIn("frame number of the bike you mean", which_bike_text([_coverage(bike, None)]))

    def test_a_frame_typed_for_it_selects_it_and_keeps_the_issue(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        chat.verify(RIDER_B)
        chat.say("TRSM2026009911")
        state = chat.conversations.peek("c1")
        self.assertIsNotNone(state.selected_frame)
        self.assertEqual(state.agent, battery_support.AGENT_NAME)


class RiderReadFrameTests(unittest.TestCase):
    def bikes_on(self, phone):
        return [dict(_coverage(b, date(2026, 9, 30)))
                for b in merged_source(lambda p: [oms_bike("EMXP2026777777", "EMX Plus")], FakeAmigo())(phone)]

    def app_ref(self):
        return self.bikes_on(RIDER_B)[1]["bike_ref"]

    def test_a_near_miss_of_a_frame_on_record_is_refused(self):
        with self.assertRaises(ToolError) as raised:
            _owned_bike(RIDER_B, "EMXP2026777778", self.bikes_on, allow_rider_read=True, selected=self.app_ref())
        self.assertEqual(raised.exception.code, "frame_number_not_owned")

    def test_a_reply_that_is_not_frame_shaped_is_refused(self):
        with self.assertRaises(ToolError):
            _owned_bike(RIDER_B, "TRSM2026OO9911", self.bikes_on, allow_rider_read=True, selected=self.app_ref())

    def test_it_is_accepted_only_for_the_chosen_frameless_bike(self):
        with self.assertRaises(ToolError):
            _owned_bike(RIDER_B, "TRSM2026009911", self.bikes_on, allow_rider_read=True, selected="EMXP2026777777")
        bike = _owned_bike(RIDER_B, "TRSM2026009911", self.bikes_on, allow_rider_read=True, selected=self.app_ref())
        self.assertEqual(bike["frame_number"], "TRSM2026009911")
        self.assertEqual(bike["frame_number_source"], "read by the rider")

    def test_the_model_is_told_to_ask_for_the_sticker(self):
        bike = self.bikes_on(RIDER_B)[1]
        self.assertIn("sticker", _bikes_block([bike]))
        chat = Chat()
        description = chat.registry.specs[CREATE_SUPPORT_TICKET].parameters["frame_number"]["description"]
        self.assertIn("not on record", description)


class OutageReadsTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.server = FakeServer()
        self.reader = AmigoReader(DSN, connect=self.server.connect, clock=lambda: self.now[0])

    def test_a_failed_read_returns_the_last_good_list(self):
        good = self.reader.bikes(RIDER_A)
        self.now[0] += 600
        self.server.fail = OSError("host unreachable")
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING"):
            self.assertEqual(self.reader.bikes(RIDER_A), good)

    def test_a_failure_is_remembered_for_a_short_while(self):
        self.server.fail = OSError("host unreachable")
        with self.assertRaises(AmigoUnavailable):
            self.reader.bikes(RIDER_A)
        with self.assertRaises(AmigoUnavailable):
            self.reader.bikes(RIDER_B)
        self.assertEqual(len(self.server.connects), 1)
        self.now[0] += 61
        with self.assertRaises(AmigoUnavailable):
            self.reader.bikes(RIDER_A)
        self.assertEqual(len(self.server.connects), 2)

    def test_service_status_reads_on_one_connection(self):
        self.reader.bikes(RIDER_A)
        before = len(self.server.connects)
        self.reader.service_status(RIDER_A)
        self.assertEqual(len(self.server.connects) - before, 1)


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
