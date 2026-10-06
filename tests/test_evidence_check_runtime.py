"""The evidence check in a conversation, through runtime.handle() (the person's
brief, 6 October 2026).

The checker runs at ingest (api._inbound_attachments) and its verdict reaches
the turn in `entry_metadata["evidence_verdict"]`, the way `photos_unchecked`
does, so these tests fake it there. With the switch on, a fault chat raises a
support ticket or records a handover ticket only after a pass; three asks with
nothing that passes end with the customer care contact and no ticket. Safety,
and every chat that is not about a bike fault, behave exactly as before.
"""

import unittest
from datetime import date

from emotorad_ai.agents.base import HANDOVER_TEXT
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationState, InMemoryConversationStore
from emotorad_ai.evidence_check import (
    EVIDENCE_HANDOVER_TEXT,
    EVIDENCE_HANDOVER_TEXT_HI,
    fail_text,
    final_text,
    is_fault_chat,
)
from emotorad_ai.guardrails import HANDOFF_MESSAGE, HANDOVER_RECORDED_MESSAGE
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import TURN_FACT_FIELDS, Runtime
from emotorad_ai.tickets.kinds import FIRST_DESK_NUMBER
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, MockTicketSystem, build_registry
from emotorad_ai.zoho.payload import description
from tests.test_evidence_only_what_was_seen import Store

TODAY = date(2026, 10, 6)
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus (fixtures)
RIDER = Identity(strength=VERIFIED, phone=ONE_BIKE, em_aid="aid-1")
CONTACT = "1800 000 0000"
SEEN = "The charger light stays red while the battery is plugged in."
MISSING = "The charger plugged in, with its light, for a few seconds."
PASS = {"passed": True, "shows_part": True, "fault_visible": True, "matches_complaint": True, "seen": SEEN,
        "missing": ""}
FAIL = {"passed": False, "shows_part": True, "fault_visible": False, "matches_complaint": False,
        "seen": "The battery pack on the frame.", "missing": MISSING}
ERROR = {"error": "unavailable"}
PHOTO = {"kind": "image", "url": "s3://customers/cl/c1/images/x.jpg", "mime_type": "image/jpeg"}
VIDEO = {"kind": "video", "url": "s3://customers/cl/c1/videos/x.mp4", "mime_type": "video/mp4",
         "summary": "The charger is plugged into the battery and its light stays red throughout."}
TICKET = {"category": "battery_charging", "severity": "normal", "description": "Light stays red; tried two sockets.",
          "idempotency_key": "k1"}
VIDEO_ASK = "Could you send a short video of the charger plugged in, showing its light?"
EVIDENCE_LINE = "Evidence checked by Gemini: %s Shows the problem and matches the complaint." % SEEN


def ticket_turn(n=1, then="I've raised that for you."):
    """The model tries the support ticket, then says something."""
    return [call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_%d" % n), say(then)]


class EvidenceChat:
    """One customer with the evidence check on (or off) and Zoho on (or off)."""

    def __init__(self, replies=(), switch=True, zoho=True, contact=CONTACT, routed="battery_support"):
        self.tickets = InMemoryTicketStore()
        self.mock = MockTicketSystem()
        system = TicketRouter(DeskTicketSystem(self.tickets, "test", "stage"), self.mock) if zoho else self.mock
        self.registry = build_registry(today=TODAY, ticket_system=system)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            media_store=Store(), evidence_check=switch, customer_care_contact=contact,
        )
        if routed:
            self.conversations.get("c1").route_to(routed)

    def say(self, text, verdict=None, attachments=(), identity=RIDER, pill=None):
        meta = {"evidence_verdict": dict(verdict)} if verdict is not None else {}
        if pill:
            meta["pill_clicked"] = pill
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=identity, attachments=[Attachment(**a) for a in attachments], entry_metadata=meta,
        ))

    def state(self):
        return self.conversations.peek("c1")

    def records(self):
        found = (self.tickets.get("EM-%d" % n) for n in range(FIRST_DESK_NUMBER, FIRST_DESK_NUMBER + 50))
        return [record for record in found if record is not None]

    def no_ticket_anywhere(self):
        return self.records() == [] and self.mock.tickets == {}

    def guardrails(self, name):
        return [e for e in self.log.events if e["event"] == "guardrail_triggered" and e["guardrail"] == name]


class SupportTicketTests(unittest.TestCase):
    def test_a_photo_that_passes_then_the_ticket_with_the_evidence_line(self):
        chat = EvidenceChat(ticket_turn())
        reply = chat.say("here is the charger light", PASS, [PHOTO])
        self.assertEqual(reply.handled_by, "battery_support")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["evidence_check"]), ("support", SEEN))
        self.assertIn(EVIDENCE_LINE, description(record))
        self.assertEqual(chat.state().evidence_verdict["passed"], True)

    def test_a_fail_then_a_better_video_that_passes_then_the_ticket(self):
        chat = EvidenceChat(ticket_turn(1, "Shall I raise it?") + ticket_turn(2))
        first = chat.say("here is the battery", FAIL, [PHOTO])
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertEqual(first.handled_by, "guardrail:evidence_check")
        self.assertIn(fail_text(MISSING, hindi=False), first.text)
        self.assertNotIn("Shall I raise it?", first.text)
        self.assertFalse(first.escalated)
        self.assertEqual(chat.state().evidence_asks, 1)
        second = chat.say("here is a video of it", PASS, [VIDEO])
        (record,) = chat.records()
        self.assertEqual(record["evidence_check"], SEEN)
        self.assertEqual(second.handled_by, "battery_support")
        self.assertEqual(chat.state().evidence_asks, 0)

    def test_three_fails_no_ticket_and_the_customer_care_contact(self):
        chat = EvidenceChat([say(VIDEO_ASK)] + ticket_turn(1) + ticket_turn(2) + ticket_turn(3))
        self.assertIn(VIDEO_ASK, chat.say("my battery won't charge").text)
        for text in ("here it is", "another one", "and again"):
            reply = chat.say(text, FAIL, [PHOTO])
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertIn(final_text(CONTACT, hindi=False), reply.text)
        self.assertEqual(reply.handled_by, "guardrail:evidence_not_accepted")
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        (logged,) = chat.guardrails("evidence_not_accepted")
        self.assertEqual(logged["triggered_by"], {"asks": 3, "errors": 0, "verdict": "failed"})
        self.assertNotIn(MISSING, repr(logged))

    def test_without_the_setting_the_final_text_has_no_number(self):
        chat = EvidenceChat([say(VIDEO_ASK)] + ticket_turn(1) + ticket_turn(2) + ticket_turn(3), contact=None)
        chat.say("my battery won't charge")
        for text in ("here it is", "another one", "and again"):
            reply = chat.say(text, FAIL, [PHOTO])
        self.assertIn(final_text(None, hindi=False), reply.text)
        self.assertIn("Please contact EMotorad customer care.", reply.text)
        self.assertTrue(chat.no_ticket_anywhere())

    def test_the_chat_stays_open_after_the_final_text(self):
        chat = EvidenceChat([say(VIDEO_ASK)] * 4 + [say("The light pattern you describe is normal.")])
        for text in ("my battery won't charge", "hmm", "ok", "what now"):
            reply = chat.say(text)
        self.assertIn(final_text(CONTACT, hindi=False), reply.text)
        after = chat.say("what does a red light mean?")
        self.assertIn("The light pattern you describe is normal.", after.text)

    def test_the_old_hand_over_at_the_fourth_ask_becomes_the_final_text_with_no_ticket(self):
        chat = EvidenceChat([say(VIDEO_ASK)] * 4)
        for text in ("my battery won't charge", "hmm", "ok"):
            chat.say(text)
        fourth = chat.say("what now")
        self.assertNotIn(HANDOVER_TEXT, fourth.text)
        self.assertIn(final_text(CONTACT, hindi=False), fourth.text)
        self.assertFalse(fourth.escalated)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertEqual([e for e in chat.log.events if e["event"] == "escalation"], [])

    def test_a_failed_photo_does_not_start_the_count_again_and_a_pass_does(self):
        chat = EvidenceChat([say(VIDEO_ASK)] * 3 + [say("Thanks, I can see the red light.")])
        chat.say("my battery won't charge")
        chat.say("hmm")
        chat.say("here", FAIL, [PHOTO])
        self.assertEqual(chat.state().evidence_asks, 3)
        chat.say("here is a video", PASS, [VIDEO])
        self.assertEqual(chat.state().evidence_asks, 0)

    def test_a_pass_stays_when_a_later_photo_fails(self):
        chat = EvidenceChat([say("Thanks.")] + [say("Thanks.")] + ticket_turn(1))
        chat.say("video", PASS, [VIDEO])
        chat.say("and a photo of the label", FAIL, [PHOTO])
        self.assertTrue(chat.state().evidence_verdict["passed"])
        chat.say("please raise it")
        (record,) = chat.records()
        self.assertEqual(record["evidence_check"], SEEN)

    def test_a_different_bike_needs_its_own_evidence(self):
        state = ConversationState(conversation_id="c1", selected_frame="F1")
        state.evidence_verdict = {"passed": True, "seen": SEEN, "missing": "", "error": None, "at": "T"}
        state.select_bike("F1")
        self.assertIsNotNone(state.evidence_verdict)
        state.select_bike("F2")
        self.assertIsNone(state.evidence_verdict)
        state.evidence_verdict = {"passed": True}
        state.forget_bike()
        self.assertIsNone(state.evidence_verdict)

    def test_the_ticket_tool_was_refused_with_the_new_code(self):
        chat = EvidenceChat(ticket_turn(1))
        chat.say("here is the battery", FAIL, [PHOTO])
        (call,) = [e for e in chat.log.events if e["event"] == "tool_call" and e["tool"] == CREATE_SUPPORT_TICKET]
        self.assertFalse(call["ok"])
        self.assertEqual(chat.llm.requests[1]["messages"][-1]["content"][0]["is_error"], True)
        self.assertIn("evidence_not_accepted", chat.llm.requests[1]["messages"][-1]["content"][0]["content"])

    def test_a_stop_instruction_is_never_replaced_by_the_ask(self):
        hazard = ("Please stop using the bike and stop charging the battery now; a bulge in the casing can be a "
                  "hazard. Could you send a photo of the side of the pack?")
        chat = EvidenceChat(ticket_turn(1, hazard))
        reply = chat.say("the case looks uneven", FAIL, [PHOTO])
        self.assertIn("stop charging", reply.text)
        self.assertNotIn("Thanks for sending that", reply.text)

    def test_with_no_media_ever_the_model_asks_in_its_own_words(self):
        # Nothing was sent, so "Thanks for sending that" would be untrue.
        chat = EvidenceChat(ticket_turn(1, VIDEO_ASK))
        reply = chat.say("please raise a ticket")
        self.assertIn(VIDEO_ASK, reply.text)
        self.assertNotIn("Thanks for sending that", reply.text)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertEqual(chat.state().evidence_asks, 1)


class CheckErrorTests(unittest.TestCase):
    def test_an_error_never_raises_a_ticket(self):
        chat = EvidenceChat(ticket_turn(1))
        reply = chat.say("here it is", ERROR, [PHOTO])
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertIn(fail_text("", hindi=False), reply.text)
        self.assertEqual(chat.state().evidence_check_errors, 1)

    def test_one_extra_ask_then_the_failure_path(self):
        chat = EvidenceChat([say(VIDEO_ASK)] + ticket_turn(1) + ticket_turn(2) + ticket_turn(3) + ticket_turn(4))
        chat.say("my battery won't charge")
        chat.say("here", FAIL, [PHOTO])
        chat.say("again", FAIL, [PHOTO])
        self.assertEqual(chat.state().evidence_asks, 3)
        extra = chat.say("one more", ERROR, [PHOTO])
        self.assertIn(fail_text("", hindi=False), extra.text)
        self.assertEqual(chat.state().evidence_asks, 4)
        final = chat.say("and again", ERROR, [PHOTO])
        self.assertIn(final_text(CONTACT, hindi=False), final.text)
        self.assertTrue(chat.no_ticket_anywhere())
        (logged,) = chat.guardrails("evidence_not_accepted")
        self.assertEqual(logged["triggered_by"], {"asks": 4, "errors": 2, "verdict": "error:unavailable"})


class HandoverTests(unittest.TestCase):
    def test_talk_to_a_person_in_a_fault_chat_with_no_evidence_records_nothing(self):
        chat = EvidenceChat()
        reply = chat.say("I want to talk to a person")
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertEqual(reply.metadata["handover"], "evidence_needed")
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.state().evidence_asks, 1)
        self.assertIsNone(chat.state().awaiting_callback)

    def test_after_a_passing_video_talk_to_a_person_records_the_handover_ticket(self):
        chat = EvidenceChat([say("I can see the light stays red.")])
        chat.say("here is a video", PASS, [VIDEO])
        reply = chat.say("I want to talk to a person")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["evidence_check"]), ("handover", SEEN))
        self.assertIn(EVIDENCE_LINE, description(record))
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=record["_id"]), reply.text)
        self.assertTrue(reply.escalated)
        self.assertEqual(len(chat.llm.requests), 1)

    def test_a_video_that_passes_with_the_request_records_it_at_once(self):
        chat = EvidenceChat()
        reply = chat.say("here is the video, now I want to talk to a person", PASS, [VIDEO])
        (record,) = chat.records()
        self.assertEqual(record["evidence_check"], SEEN)
        self.assertTrue(reply.escalated)
        self.assertEqual(chat.llm.requests, [])

    def test_a_number_typed_with_the_request_is_kept_hidden(self):
        chat = EvidenceChat()
        reply = chat.say("call me on 9999999999")
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertNotIn("9999999999", repr(chat.state().history))
        self.assertEqual(chat.state().typed_number, "9999999999")

    def test_asking_for_a_person_counts_towards_the_final_text(self):
        chat = EvidenceChat()
        for _ in range(3):
            self.assertIn(EVIDENCE_HANDOVER_TEXT, chat.say("I want to talk to a person").text)
        fourth = chat.say("I want to talk to a person")
        self.assertIn(final_text(CONTACT, hindi=False), fourth.text)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertEqual(chat.llm.requests, [])

    def test_talk_to_a_person_about_an_order_is_recorded_as_today(self):
        chat = EvidenceChat(routed=None)
        reply = chat.say("I want to talk to a person about my order")
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual(record["kind"], "handover")
        self.assertNotIn("evidence_check", record)
        self.assertTrue(reply.escalated)
        self.assertFalse(is_fault_chat(chat.state()))

    def test_a_first_message_that_only_asks_for_a_person_is_not_a_fault_chat(self):
        chat = EvidenceChat(routed=None)
        reply = chat.say("talk to a person")
        (record,) = chat.records()
        self.assertEqual(record["kind"], "handover")
        self.assertTrue(reply.escalated)
        self.assertEqual(chat.llm.requests, [])

    def test_a_late_registration_chat_is_not_a_fault_chat(self):
        chat = EvidenceChat(routed="late_warranty")
        chat.say("I want to talk to a person")
        (record,) = chat.records()
        self.assertEqual(record["kind"], "handover")

    def test_a_ticket_the_run_holds_gets_the_note_as_today(self):
        chat = EvidenceChat()
        safety = chat.say("my battery is smoking")
        reply = chat.say("I want to talk to a person")
        self.assertEqual(reply.ticket_id, safety.ticket_id)
        (record,) = chat.records()
        self.assertEqual(record["kind"], "safety")
        self.assertEqual([note["text"] for note in record["notes"]], ["Customer asked for a person."])
        self.assertEqual(chat.llm.requests, [])

    def test_with_zoho_off_a_fault_chat_still_waits_for_evidence(self):
        chat = EvidenceChat(zoho=False)
        reply = chat.say("I want to talk to a person")
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertNotIn(HANDOFF_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertEqual(chat.llm.requests, [])

    def test_with_zoho_off_and_a_pass_it_is_todays_handover(self):
        chat = EvidenceChat(zoho=False)
        reply = chat.say("here is the video, now I want to talk to a person", PASS, [VIDEO])
        self.assertIn(HANDOFF_MESSAGE, reply.text)
        self.assertTrue(reply.escalated)


class SafetyTests(unittest.TestCase):
    def test_a_safety_report_is_as_today_and_the_model_is_never_called(self):
        chat = EvidenceChat()
        reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        (record,) = chat.records()
        self.assertEqual(record["kind"], "safety")
        self.assertNotIn("evidence_check", record)
        self.assertEqual(chat.state().evidence_asks, 0)

    def test_a_safety_report_with_a_failed_photo_is_still_as_today(self):
        chat = EvidenceChat()
        reply = chat.say("my battery is swollen", FAIL, [PHOTO])
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertEqual(len(chat.records()), 1)


class HindiTests(unittest.TestCase):
    def test_a_customer_writing_in_devanagari_gets_the_hindi_fail_text(self):
        chat = EvidenceChat(ticket_turn(1))
        reply = chat.say("यह बैटरी की फ़ोटो है", FAIL, [PHOTO])
        self.assertIn(fail_text(MISSING, hindi=True), reply.text)
        self.assertNotIn("Thanks for sending that", reply.text)

    def test_the_hindi_handover_and_final_texts(self):
        chat = EvidenceChat()
        for _ in range(3):
            self.assertIn(EVIDENCE_HANDOVER_TEXT_HI, chat.say("मुझे customer care से बात करनी है").text)
        self.assertIn(final_text(CONTACT, hindi=True), chat.say("मुझे customer care से बात करनी है").text)

    def test_the_english_texts_never_say_colleague(self):
        chat = EvidenceChat(ticket_turn(1))
        texts = [chat.say("here", FAIL, [PHOTO]).text, chat.say("I want to talk to a person").text]
        for text in texts:
            self.assertNotIn("colleague", text.lower())


class SwitchOffTests(unittest.TestCase):
    def test_with_the_switch_off_a_verdict_is_ignored_and_tickets_are_as_today(self):
        chat = EvidenceChat(ticket_turn(1), switch=False)
        reply = chat.say("here is the battery", FAIL, [PHOTO])
        (record,) = chat.records()
        self.assertEqual(record["kind"], "support")
        self.assertNotIn("evidence_check", record)
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertIsNone(chat.state().evidence_verdict)

    def test_with_the_switch_off_the_old_evidence_required_rule_applies(self):
        chat = EvidenceChat(ticket_turn(1, VIDEO_ASK), switch=False)
        chat.say("please raise a ticket")
        (call,) = [e for e in chat.log.events if e["event"] == "tool_call" and e["tool"] == CREATE_SUPPORT_TICKET]
        self.assertIn("evidence_required", repr(chat.llm.requests[1]["messages"][-1]["content"][0]["content"]))
        self.assertTrue(chat.no_ticket_anywhere())

    def test_with_the_switch_off_talk_to_a_person_in_a_fault_chat_is_as_today(self):
        chat = EvidenceChat(switch=False)
        reply = chat.say("I want to talk to a person")
        (record,) = chat.records()
        self.assertEqual(record["kind"], "handover")
        self.assertTrue(reply.escalated)

    def test_with_the_switch_off_the_fourth_ask_is_todays_hand_over(self):
        chat = EvidenceChat([say(VIDEO_ASK)] * 4, switch=False)
        for text in ("my battery won't charge", "hmm", "ok"):
            chat.say(text)
        fourth = chat.say("what now")
        self.assertIn(HANDOVER_TEXT, fourth.text)
        self.assertTrue(fourth.escalated)


class MergeTests(unittest.TestCase):
    def test_the_verdict_and_errors_are_turn_facts(self):
        self.assertIn("evidence_verdict", TURN_FACT_FIELDS)
        self.assertIn("evidence_check_errors", TURN_FACT_FIELDS)

    def test_a_pass_the_other_server_saved_stands_against_this_turns_fail(self):
        chat = EvidenceChat()
        fresh = chat.conversations.get("c1")
        fresh.evidence_verdict = {"passed": True, "seen": SEEN, "missing": "", "error": None, "at": "T1"}
        chat.conversations.save(fresh)
        ours = ConversationState.from_json(fresh.to_json())
        ours.evidence_verdict = {"passed": False, "seen": "x", "missing": MISSING, "error": None, "at": "T2"}
        loaded = {name: None for name in TURN_FACT_FIELDS}
        merged = chat.runtime._merge_onto_fresh(
            ours, [], Reply(conversation_id="c1", text="ok", handled_by="battery_support"), loaded=loaded)
        self.assertTrue(merged.evidence_verdict["passed"])

    def test_this_turns_pass_stands_against_the_other_servers_fail(self):
        chat = EvidenceChat()
        fresh = chat.conversations.get("c1")
        fresh.evidence_verdict = {"passed": False, "seen": "x", "missing": MISSING, "error": None, "at": "T1"}
        chat.conversations.save(fresh)
        ours = ConversationState.from_json(fresh.to_json())
        ours.evidence_verdict = {"passed": True, "seen": SEEN, "missing": "", "error": None, "at": "T2"}
        loaded = {name: getattr(fresh, name) for name in TURN_FACT_FIELDS}
        merged = chat.runtime._merge_onto_fresh(
            ours, [], Reply(conversation_id="c1", text="ok", handled_by="battery_support"), loaded=loaded)
        self.assertTrue(merged.evidence_verdict["passed"])


class FaultChatForTheRunTests(unittest.TestCase):
    """A fault chat stays one for the run (the review of 6 October 2026): the
    navigation that clears the agent and the topic does not open the handover."""

    def test_start_over_then_talk_to_a_person_still_waits_for_evidence(self):
        chat = EvidenceChat()
        chat.say("start over")
        self.assertIsNone(chat.state().agent)
        reply = chat.say("I want to talk to a person")
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertEqual(chat.llm.requests, [])

    def test_a_first_message_naming_the_fault_and_asking_for_a_person_waits(self):
        chat = EvidenceChat(routed=None)
        reply = chat.say("my battery won't charge, I want to talk to a person")
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(is_fault_chat(chat.state()))
        passed = chat.say("here is the video, now I want to talk to a person", PASS, [VIDEO])
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["evidence_check"]), ("handover", SEEN))
        self.assertTrue(passed.escalated)

    def test_a_pill_naming_the_fault_with_a_request_for_a_person_waits(self):
        chat = EvidenceChat(routed=None)
        reply = chat.say("talk to a person", pill="battery_issue")
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertTrue(chat.no_ticket_anywhere())

    def test_with_zoho_off_a_first_message_naming_the_fault_waits_too(self):
        chat = EvidenceChat(routed=None, zoho=False)
        reply = chat.say("the motor makes a grinding noise, connect me to a person")
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertFalse(reply.escalated)

    def test_a_number_typed_for_a_waiting_handover_records_nothing_without_evidence(self):
        chat = EvidenceChat()
        chat.state().awaiting_callback = "handover"
        reply = chat.say("9999999999", identity=Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertTrue(chat.no_ticket_anywhere())
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.state().typed_number, "9999999999")
        self.assertNotIn("9999999999", repr(chat.state().history))

    def test_a_number_typed_with_the_fault_after_a_plain_request_records_nothing(self):
        chat = EvidenceChat(routed=None)
        anonymous = Identity(strength=ANONYMOUS, em_aid="aid-1")
        chat.say("talk to a person", identity=anonymous)
        self.assertEqual(chat.state().awaiting_callback, "handover")
        reply = chat.say("9999999999, my battery won't charge", identity=anonymous)
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertTrue(chat.no_ticket_anywhere())

    def test_a_registration_chat_with_a_battery_topic_records_the_handover_as_today(self):
        chat = EvidenceChat(routed="late_warranty")
        chat.state().pending_topic = "battery"
        reply = chat.say("I want to talk to a person")
        (record,) = chat.records()
        self.assertEqual(record["kind"], "handover")
        self.assertTrue(reply.escalated)
        self.assertNotEqual(reply.metadata.get("handover"), "evidence_needed")


class AnonymousTests(unittest.TestCase):
    def test_an_anonymous_handover_in_a_fault_chat_asks_for_evidence_not_a_number(self):
        chat = EvidenceChat()
        reply = chat.say("I want to talk to a person", identity=Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertIn(EVIDENCE_HANDOVER_TEXT, reply.text)
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertTrue(chat.no_ticket_anywhere())


if __name__ == "__main__":
    unittest.main()
