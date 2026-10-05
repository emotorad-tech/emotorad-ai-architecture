"""Safety without a known phone, and a safety record that cannot be written
(spec 2026-10-05-zoho-desk-tickets-design.md, sections 6 and 7, part 4).

Every test goes through runtime.handle() and checks that the model was never
called. ScriptedClaude gets no replies, so any model call would raise. The rule
under test: a reply promises a call only when a ticket is behind it.

DeskChat is the helper the part 4 tests share (test_callback_gate.py,
test_handover_tickets.py, test_lockout_tickets.py). It is a web chat with Zoho
on, wired as api.py wires it, over an in-memory ticket store.
"""

import unittest
from datetime import date
from unittest import mock

from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.guardrails import (
    CAP_OVERALL_MESSAGE,
    CAP_PER_NUMBER_MESSAGE,
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
    REFERENCE_SUFFIX,
    SAFETY_ADDED_MESSAGE,
    SAFETY_EMERGENCY,
    SAFETY_MESSAGE,
    SAFETY_NO_CONTACT_MESSAGE,
    SAFETY_NOT_RECORDED_MESSAGE,
    SAFETY_STEPS,
)
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import HANDOVER_TEXT, Runtime, read_number
from emotorad_ai.tickets.clock import now_iso, parse, plus
from emotorad_ai.tickets.kinds import FIRST_DESK_NUMBER, is_desk_reference
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, MockTicketSystem, build_registry, ticket_source_key
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.tools.verification import VerificationStore
from tests.test_reply_claims import STAGING_REPLY
from tests.test_runtime_persistence import runtime_on, send
from tests.test_verify_first import ConflictedStore

TODAY = date(2026, 10, 5)
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus (fixtures)
TWO_BIKES = fixtures.PHONE_AMIIGO_TEST_RIDER  # two bikes (fixtures)
CALL_BACK = "9999999999"  # the fake number, as a customer types it
FAKE = "+91" + CALL_BACK
RIDER = Identity(strength=VERIFIED, phone=ONE_BIKE, em_aid="aid-1")
# The words a promise of a call is made of. A reply that recorded nothing
# must contain none of them.
PROMISES = ("call you", "in touch", "linked to your account", "reference", "passed this on", "pass you")
# A safety report with a number, as customers write it.
HAZARD_WITH_NUMBER = {
    "english": "my battery is smoking, call me on 99999 99999",
    "hinglish": "battery se dhuan aa raha hai, mera number 9999999999 hai",
    "hindi": "battery smoking, मेरा नंबर ९९९९९९९९९९ है",
}
ORIGINAL_SAFETY_MESSAGE = (
    "Please stop using and stop charging the battery right now, and move it away from "
    "anything flammable and away from people. Do not try to open, repair or charge it "
    "again, and do not put it in water.\n\n"
    "What you have described is a safety issue rather than a normal support question, so "
    "I am handing this to our safety team immediately rather than troubleshooting it here. "
    "They will call you on the number linked to your account.\n\n"
    "If you can see smoke or flames right now, move away from the bike and call emergency "
    "services on 112."
)


class DeskChat:
    """One website visitor with Zoho on. The registry holds a TicketRouter, as
    api.py wires it, over an in-memory ticket store the test can read. With
    zoho=False it holds only the mock, as today."""

    def __init__(self, replies=(), zoho=True, verify_first=True, conversations=None, ticket_clock=None):
        self.store = VerificationStore()
        self.tickets = InMemoryTicketStore()
        self.mock = MockTicketSystem()
        system = (TicketRouter(DeskTicketSystem(self.tickets, "test", "stage", clock=ticket_clock or now_iso),
                               self.mock)
                  if zoho else self.mock)
        self.registry = build_registry(verification=self.store, today=TODAY, ticket_system=system,
                                       account_finder=fixtures.find_account_by_order_code)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = conversations if conversations is not None else InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            self_service_identity=True, phone_resolver=self.store.verified_phone,
            otp_verified_at=self.store.verified_on, verify_first=verify_first,
        )

    def say(self, text, identity=None, cid="c1", attachments=()):
        return self.runtime.handle(InboundMessage(
            conversation_id=cid, persona="customer", channel="website_chat", message_text=text,
            identity=identity or Identity(strength=ANONYMOUS, em_aid="aid-1"), attachments=list(attachments),
        ))

    def state(self, cid="c1"):
        return self.conversations.peek(cid)

    def records(self):
        """Every Desk record, in reference order."""
        found = (self.tickets.get("EM-%d" % n) for n in range(FIRST_DESK_NUMBER, FIRST_DESK_NUMBER + 100))
        return [record for record in found if record is not None]

    def events(self, name):
        return [e for e in self.log.events if e["event"] == name]

    def mark_gone(self, reference):
        """Deleted or merged in Desk, as the worker records it: taken under a
        lease, then saved as gone."""
        token = "gone-test"
        taken = self.tickets.take_due(plus(now_iso(), 3600), "test", 300, token)
        if taken is None or taken["_id"] != reference:
            raise AssertionError("expected %s to be due, got %r" % (reference, taken))
        self.tickets.save(reference, token, {"state": "gone"})


def customer_turn(chat, text, cid="c1"):
    return next(turn for turn in chat.conversations.transcript(cid) if turn.role == "customer" and turn.text == text)


class ReadNumberTests(unittest.TestCase):
    def test_a_number_in_each_language_and_shape(self):
        for text, shown in (
            ("call me on 99999 99999", "call me on [phone]"),
            ("mera number 9999999999 hai", "mera number [phone] hai"),
            ("मेरा नंबर ९९९९९९९९९९ है", "मेरा नंबर [phone] है"),
            ("नंबर 9999999999पर", "नंबर [phone]पर"),
            ("+91 99999 99999", "[phone]"),
        ):
            with self.subTest(text=text):
                typed = read_number(text)
                self.assertEqual((typed.number, typed.shown, typed.attempted), (CALL_BACK, shown, False))

    def test_a_quoted_reference_is_neither_a_number_nor_a_try_at_one(self):
        for text in ("my ticket is EM-1000001", "booking BK-0001234 and order ro-1234567"):
            with self.subTest(text=text):
                self.assertEqual(tuple(read_number(text)), (None, text, False))
        # The same digits without a prefix are a try at a number.
        self.assertTrue(read_number("my ticket is 1000001").attempted)

    def test_a_number_that_is_not_an_indian_mobile_is_a_try(self):
        for text, shown in (("12345678", "[number]"), ("call +34 612 345 678", "call [number]")):
            with self.subTest(text=text):
                typed = read_number(text)
                self.assertEqual((typed.number, typed.shown, typed.attempted), (None, shown, True))

    def test_a_reference_beside_a_number_leaves_the_number_to_be_read(self):
        typed = read_number("EM-1000001, call 9999999999")
        self.assertEqual((typed.number, typed.shown), (CALL_BACK, "EM-1000001, call [phone]"))

    def test_a_reference_in_devanagari_digits_is_set_aside_too(self):
        self.assertEqual(read_number("मेरा टिकट EM-१०००००१ है").number, None)
        self.assertFalse(read_number("मेरा टिकट EM-१०००००१ है").attempted)

    def test_a_message_with_no_number_is_kept_as_typed(self):
        self.assertEqual(tuple(read_number("बैटरी से धुआं")), (None, "बैटरी से धुआं", False))


class AskForANumberTests(unittest.TestCase):
    def test_a_hazard_with_no_number_asks_for_one_and_promises_no_call(self):
        chat = DeskChat()
        reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, reply.text)
        for promise in ("call you", "in touch", "linked to your account", "reference"):
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), ("safety", 0))
        self.assertEqual(len(chat.events("callback_asked")), 1)
        self.assertEqual(chat.events("escalation"), [])

    def test_the_ask_for_a_report_in_each_language_the_gate_reads(self):
        # Devanagari hazard words are not safety terms today (guardrails.py),
        # so the Hindi report carries the English word, as HAZARD_WITH_NUMBER's.
        for text in ("my battery is smoking", "battery se dhuan aa raha hai", "battery smoking, मदद करो"):
            with self.subTest(text=text):
                chat = DeskChat()
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                self.assertIn(SAFETY_NO_CONTACT_MESSAGE, reply.text)
                self.assertEqual(chat.records(), [])
                self.assertEqual(chat.state().awaiting_callback, "safety")

    def test_a_number_that_is_not_an_indian_mobile_asks_and_is_kept_as_a_placeholder(self):
        chat = DeskChat()
        reply = chat.say("my battery is smoking, call +34 612 345 678")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, reply.text)
        self.assertEqual(chat.records(), [])
        self.assertIn("[number]", repr(chat.state().history))
        self.assertNotIn("612 345 678", repr(chat.state().history))


class NumberInTheSameMessageTests(unittest.TestCase):
    def test_the_hazard_and_a_number_record_an_urgent_unverified_ticket(self):
        for language, text in HAZARD_WITH_NUMBER.items():
            with self.subTest(language=language):
                chat = DeskChat()
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["urgent"], record["identity"]), ("safety", True, "unverified"))
                self.assertEqual(record["phone"], FAKE)
                self.assertEqual(record["source_key"], "c1:%s:safety_callback" % chat.state().started_at)
                self.assertEqual(record["started_at"], chat.state().started_at)
                self.assertFalse(record["bike"])
                self.assertNotIn(CALL_BACK, record["summary"])
                self.assertEqual(reply.ticket_id, record["_id"])
                self.assertTrue(is_desk_reference(reply.ticket_id))
                self.assertTrue(reply.escalated)
                self.assertEqual(reply.handled_by, "guardrail:battery_safety")
                self.assertIn(SAFETY_STEPS, reply.text)
                self.assertIn(NUMBER_RECEIVED_MESSAGE.format(reference=reply.ticket_id), reply.text)
                self.assertIn(SAFETY_EMERGENCY, reply.text)
                self.assertIsNone(chat.state().awaiting_callback)
                self.assertEqual(chat.state().typed_number, CALL_BACK)
                (event,) = chat.events("ticket_recorded")
                self.assertEqual((event["ticket_id"], event["kind"], event["urgent"]), (record["_id"], "safety", True))

    def test_the_number_is_phone_in_history_and_transcript(self):
        for language, text in HAZARD_WITH_NUMBER.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say(text)
                history = repr(chat.state().history)
                self.assertIn("[phone]", history)
                for form in (CALL_BACK, "99999 99999", "९९९९९९९९९९"):
                    self.assertNotIn(form, history)
                said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
                self.assertIn("[phone]", said)
                self.assertNotIn("९९९९९९९९९९", said)
                self.assertNotIn(CALL_BACK, said)

    def test_a_typed_number_that_owns_two_bikes_looks_up_no_bike(self):
        chat = DeskChat()
        reply = chat.say("there is smoke from the battery, my number is 9700000010")
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual(record["phone"], TWO_BIKES)
        self.assertFalse(record["bike"])
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertFalse([e for e in chat.events("tool_call") if e["tool"] == "create_support_ticket"])

    def test_a_save_conflict_records_one_ticket(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        store.conflicts = 1
        reply = chat.say("my battery is smoking, my number is 9999999999")
        (record,) = chat.records()
        self.assertEqual(reply.ticket_id, record["_id"])
        # The recording is a side effect: the turn is merged, never run again,
        # so no second report is noted on the ticket it just recorded.
        self.assertEqual(record["notes"], [])
        self.assertEqual(len(chat.events("conversation_merged")), 1)
        self.assertIn(NUMBER_RECEIVED_MESSAGE.format(reference=record["_id"]), reply.text)
        self.assertEqual(chat.state().ticket_id, record["_id"])
        self.assertEqual(chat.llm.requests, [])


class OneSafetyTicketPerRunTests(unittest.TestCase):
    def test_a_second_report_adds_a_note_to_the_typed_numbers_ticket(self):
        chat = DeskChat()
        first = chat.say("smoke from my battery, my number is 9999999999")
        second = chat.say("it is still smoking")
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual(second.ticket_id, first.ticket_id)
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn("again", record["notes"][0]["text"])
        self.assertIn(SAFETY_ADDED_MESSAGE.format(reference=first.ticket_id), second.text)
        self.assertNotIn("mobile number", second.text)
        self.assertTrue(second.escalated)

    def test_a_second_report_with_a_known_phone_adds_a_note(self):
        chat = DeskChat()
        first = chat.say("my battery is swollen", identity=RIDER)
        second = chat.say("it is still swollen and hot", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(is_desk_reference(first.ticket_id))
        self.assertIn("I have raised this as a priority safety case, reference %s." % first.ticket_id, first.text)
        self.assertEqual(second.ticket_id, first.ticket_id)
        (record,) = chat.records()
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn(SAFETY_MESSAGE, second.text)

    def test_a_typed_numbers_ticket_takes_a_later_report_once_the_phone_is_known(self):
        # Amendment A, finding 11: an anonymous visitor records through a typed
        # number, then is known in the same run (no restart_for: the run had
        # no owner). The later report goes to the same ticket, not a second.
        chat = DeskChat()
        first = chat.say("smoke from my battery, my number is 9999999999")
        second = chat.say("it is still swollen and hot", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual(second.ticket_id, first.ticket_id)
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn(SAFETY_MESSAGE, second.text)
        self.assertIn("reference %s." % first.ticket_id, second.text)
        self.assertFalse([e for e in chat.events("tool_call") if e["tool"] == CREATE_SUPPORT_TICKET])

    def test_the_safety_branchs_ticket_takes_a_later_report_with_no_number_known(self):
        # The other key of finding 11: the run's safety ticket was raised by
        # the safety branch's tool, and the next report comes with no phone.
        chat = DeskChat()
        state = chat.conversations.get("c1")
        started = state.started_at
        key = ticket_source_key("c1", started, CREATE_SUPPORT_TICKET, "safety:c1:%s" % started)
        held = chat.registry.tickets.create(source_key=key, persona="customer", kind="safety", conversation_id="c1",
                                            started_at=started, phone=FAKE, identity="unverified",
                                            category="battery_safety", description="Smoke from the pack.")
        reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.ticket_id, held["ticket_id"])
        (record,) = chat.records()
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn(SAFETY_ADDED_MESSAGE.format(reference=held["ticket_id"]), reply.text)
        self.assertNotIn("mobile number", reply.text)
        self.assertIsNone(chat.state().awaiting_callback)


class SeveralBikesTests(unittest.TestCase):
    """The safety branch fires before a bike is chosen. On a number with
    several bikes its ticket is raised with no bike, never refused for want of
    a frame number, so the promise in its reply has a ticket behind it. Before
    5 October 2026 it was refused (frame_number_required) and the reply still
    promised a call."""

    def test_a_known_phone_with_several_bikes_and_none_chosen_records_its_safety_ticket(self):
        for zoho in (True, False):
            with self.subTest(zoho=zoho):
                chat = DeskChat(zoho=zoho)
                reply = chat.say("there is smoke coming from the battery",
                                 identity=Identity(strength=VERIFIED, phone=TWO_BIKES, em_aid="aid-1"))
                self.assertEqual(chat.llm.requests, [])
                self.assertTrue(reply.ticket_id)
                self.assertIn(SAFETY_MESSAGE, reply.text)
                self.assertIn("reference %s." % reply.ticket_id, reply.text)
                self.assertTrue(reply.escalated)
                self.assertEqual(chat.events("safety_ticket_not_recorded"), [])
                if zoho:
                    (record,) = chat.records()
                    self.assertEqual((record["kind"], record["identity"], record["bike"]), ("safety", "verified", None))
                else:
                    self.assertIsNone(chat.mock.tickets[reply.ticket_id]["frame_number"])

    def test_a_ticket_the_model_raises_still_names_the_bike(self):
        # Only the safety branch's kind, which code sets, is let through: the
        # model's call on a number with several bikes is refused as before,
        # whatever category it chose.
        registry = build_registry(today=TODAY)
        envelope = registry.call(
            CREATE_SUPPORT_TICKET,
            {"category": "battery_safety", "severity": "critical", "description": "Smoke from the pack.",
             "idempotency_key": "k1"},
            ToolContext(conversation_id="c1", phone=TWO_BIKES, persona="customer", started_at=now_iso()),
        )
        self.assertEqual(envelope["error"]["code"], "frame_number_required")
        self.assertEqual(registry.tickets.tickets, {})


class GoneTicketTests(unittest.TestCase):
    def test_a_runs_safety_ticket_gone_in_desk_is_never_quoted(self):
        chat = DeskChat()
        first = chat.say("smoke from my battery, my number is 9999999999")
        chat.mark_gone(first.ticket_id)
        reply = chat.say("it is still smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn(first.ticket_id, reply.text)
        self.assertIsNone(reply.ticket_id)
        self.assertFalse(reply.escalated)
        self.assertEqual([e["why"] for e in chat.events("safety_ticket_not_recorded")], ["ticket_gone"])

    def test_a_record_that_comes_back_gone_is_not_quoted(self):
        # Amendment A, finding 18: the same source key returns the record
        # even when it is gone, so _record_ticket reads it after create.
        chat = DeskChat()
        first = chat.say("smoke from my battery, my number is 9999999999")
        chat.mark_gone(first.ticket_id)
        with mock.patch.object(chat.tickets, "by_source_key", return_value=None):
            reply = chat.say("there is still smoke, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertNotIn(first.ticket_id, reply.text)
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual([e["error"] for e in chat.events("ticket_record_failed")], ["gone"])
        self.assertEqual(len(chat.records()), 1)


class NotRecordedTests(unittest.TestCase):
    def assert_no_promise(self, reply):
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)

    def test_a_write_that_fails_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("smoke from my battery, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual(event["level"], "error")
        self.assertEqual(chat.records(), [])
        self.assertNotIn(CALL_BACK, repr(chat.state().history))

    def test_a_seam_that_returns_nothing_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", return_value=None):
            reply = chat.say("smoke from my battery, my number is 9999999999")
        self.assert_no_promise(reply)
        self.assertEqual(len(chat.events("safety_ticket_not_recorded")), 1)

    def test_a_ticket_store_that_cannot_be_read_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.tickets, "by_source_key", side_effect=StoreUnavailable("down")):
            reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)

    def test_a_known_phones_ticket_that_cannot_be_raised_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.runtime, "_raise_safety_ticket", return_value=None):
            reply = chat.say("my battery is swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)
        self.assertEqual(len(chat.events("safety_ticket_not_recorded")), 1)

    def test_a_known_phones_ticket_that_raises_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.runtime, "_raise_safety_ticket", side_effect=RuntimeError("boom")):
            reply = chat.say("my battery is swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)
        self.assertEqual([e["error"] for e in chat.events("safety_ticket_failed")], ["RuntimeError"])

    def test_each_one_is_counted_for_health(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            chat.say("smoke from my battery, my number is 9999999999")
            chat.say("smoke from my battery, my number is 9999999999", cid="c2")
        self.assertEqual(chat.runtime.safety_not_recorded, 2)
        # Zoho off with no number is the expected reply, not a failure.
        off = DeskChat(zoho=False)
        off.say("my battery is smoking")
        self.assertEqual(off.runtime.safety_not_recorded, 0)


class StoreDownTests(unittest.TestCase):
    class DownStore(InMemoryConversationStore):
        def get(self, conversation_id):
            raise StoreUnavailable("MongoDB find_one failed")

    def test_a_safety_report_while_the_store_is_down_gets_the_steps_and_no_promise(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery is swollen")
        self.assertEqual(runtime.llm.requests, [])
        self.assertEqual(reply.handled_by, "store_unavailable")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn(HANDOVER_TEXT, reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        (event,) = [e for e in runtime.log.events if e["event"] == "safety_ticket_not_recorded"]
        self.assertEqual((event["why"], event["level"]), ("store_unavailable", "error"))
        self.assertEqual(runtime.safety_not_recorded, 1)

    def test_smoke_seen_in_a_clip_while_the_store_is_down_is_a_safety_report(self):
        runtime = runtime_on(self.DownStore(), [])
        clip = Attachment("video", "s3://customers/clu_1/c1/videos/upl_1.mp4", "video/mp4",
                          summary="White smoke rises from the battery pack.")
        reply = runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text="",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), attachments=[clip],
        ))
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)

    def test_any_other_message_while_the_store_is_down_still_hands_over(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery won't charge")
        self.assertIn(HANDOVER_TEXT, reply.text)
        self.assertTrue(reply.escalated)
        self.assertEqual(runtime.safety_not_recorded, 0)


class ZohoOffTests(unittest.TestCase):
    def test_no_phone_gets_the_steps_without_a_question_or_a_promise(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn("mobile number", reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.mock.tickets, {})
        self.assertEqual(len(chat.events("safety_without_contact")), 1)

    def test_a_number_in_the_message_records_nothing(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("smoke from the battery, call me on 9999999999")
        self.assertEqual(chat.mock.tickets, {})
        self.assertIsNone(reply.ticket_id)
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)

    def test_a_known_phone_still_raises_its_ticket_as_before(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("my battery is swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(SAFETY_MESSAGE, reply.text)
        self.assertIn("reference %s." % reply.ticket_id, reply.text)
        self.assertIn(reply.ticket_id, chat.mock.tickets)
        self.assertTrue(reply.escalated)


class BackstopWithoutAPhoneTests(unittest.TestCase):
    """Amendment A, finding 13: a model reply claims a ticket about a live
    hazard, and no number is known. The model is called once, for that
    reply, and never again; what replaces it promises nothing."""

    def anonymous(self, chat):
        return chat.say("my battery isn't charging")

    def test_with_zoho_on_it_asks_for_a_number_instead_of_a_handover(self):
        chat = DeskChat(replies=[say(STAGING_REPLY)], verify_first=False)
        reply = self.anonymous(chat)
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, reply.text)
        self.assertNotIn(HANDOVER_TEXT, reply.text)
        self.assertNotIn("I'm raising", reply.text)
        for promise in ("call you", "in touch", "linked to your account", "reference", "pass you"):
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), ("safety", 0))
        # What the model is shown next turn is what the customer was sent.
        self.assertNotIn("I'm raising", repr(chat.state().history))

    def test_with_zoho_off_it_gives_the_steps_and_no_promise(self):
        chat = DeskChat(replies=[say(STAGING_REPLY)], zoho=False, verify_first=False)
        reply = self.anonymous(chat)
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn(HANDOVER_TEXT, reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertEqual(chat.mock.tickets, {})
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual((event["why"], event["level"]), ("backstop_no_contact", "error"))
        self.assertEqual(chat.runtime.safety_not_recorded, 1)


class SomeoneElsesRunTests(unittest.TestCase):
    """A second person on the same browser before restart_for: the run's own
    person was known by the channel, and the next message comes with no
    number known. Their ticket has a run of its own, never the first person's
    (the controller's ruling for Task 13)."""

    def test_a_strangers_report_records_a_ticket_of_its_own_run(self):
        chat = DeskChat()
        owner = chat.say("my battery is swollen", identity=RIDER)
        stranger = chat.say("smoke from my battery, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(is_desk_reference(owner.ticket_id) and is_desk_reference(stranger.ticket_id))
        self.assertNotEqual(stranger.ticket_id, owner.ticket_id)
        self.assertNotIn(owner.ticket_id, stranger.text)
        theirs, ours = chat.tickets.get(owner.ticket_id), chat.tickets.get(stranger.ticket_id)
        started = chat.state().started_at
        self.assertEqual(theirs["started_at"], started)
        self.assertEqual(theirs["notes"], [])
        self.assertNotEqual(ours["started_at"], started)
        self.assertEqual(ours["source_key"], "c1:%s:safety_callback" % ours["started_at"])
        self.assertEqual(ours["phone"], FAKE)
        # The first person's run ends where the stranger's begins: after the
        # first person's last turn, at or before the stranger's.
        self.assertEqual(theirs["ended_at"], ours["started_at"])
        swollen = customer_turn(chat, "my battery is swollen")
        theirs_now = customer_turn(chat, "smoke from my battery, my number is [phone]")
        self.assertLess(parse(swollen.at), parse(ours["started_at"]))
        self.assertLessEqual(parse(ours["started_at"]), parse(theirs_now.at))
        self.assertIsNone(ours["ended_at"])
        # The stranger's ticket is woken at the end of their turn; the first
        # person's only by their own.
        self.assertEqual((theirs["wake"], ours["wake"]), (1, 1))

    def test_a_strangers_report_never_takes_the_first_persons_ticket(self):
        chat = DeskChat()
        owner = chat.say("my battery is swollen", identity=RIDER)
        stranger = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, stranger.text)
        self.assertNotIn(owner.ticket_id, stranger.text)
        self.assertIsNone(stranger.ticket_id)
        (record,) = chat.records()
        self.assertEqual(record["notes"], [])


class TextsTests(unittest.TestCase):
    def test_the_known_phone_text_is_unchanged(self):
        self.assertEqual(SAFETY_MESSAGE, ORIGINAL_SAFETY_MESSAGE)

    def test_the_texts_without_a_record_promise_no_call(self):
        for text in (SAFETY_NOT_RECORDED_MESSAGE, SAFETY_NO_CONTACT_MESSAGE):
            for promise in ("call you", "in touch", "linked to your account", "reference"):
                self.assertNotIn(promise, text)
            self.assertTrue(text.startswith(SAFETY_STEPS))
            self.assertTrue(text.endswith(SAFETY_EMERGENCY))
        self.assertIn("112", SAFETY_NOT_RECORDED_MESSAGE)
        self.assertIn("mobile number", SAFETY_NO_CONTACT_MESSAGE)

    def test_the_new_texts_are_plain(self):
        texts = (SAFETY_NO_CONTACT_MESSAGE, SAFETY_NOT_RECORDED_MESSAGE, SAFETY_ADDED_MESSAGE,
                 NUMBER_RECEIVED_MESSAGE, HANDOVER_RECORDED_MESSAGE, HANDOVER_ASK_NUMBER_MESSAGE,
                 REFERENCE_SUFFIX, CAP_PER_NUMBER_MESSAGE, CAP_OVERALL_MESSAGE)
        for text in texts:
            self.assertNotIn("—", text)  # no em dash
        for text in (SAFETY_ADDED_MESSAGE, NUMBER_RECEIVED_MESSAGE, HANDOVER_RECORDED_MESSAGE, REFERENCE_SUFFIX):
            self.assertIn("EM-1000001", text.format(reference="EM-1000001"))


if __name__ == "__main__":
    unittest.main()
