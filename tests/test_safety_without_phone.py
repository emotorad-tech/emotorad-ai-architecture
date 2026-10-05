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
from emotorad_ai.conversation import ConversationState, InMemoryConversationStore, StoreUnavailable
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
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.media_records import media_record
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

    def assert_model_never_called(self, since=0):
        """No model call after the first `since`: a gate, the safety branch or
        the verify step answered, as the definition of done asks of every
        blocked path (the final review, safety-flow Minor 10)."""
        made = len(self.llm.requests) - since
        if made:
            raise AssertionError("the model was called %d time(s) on a path that must never call it" % made)

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


def in_window(record, at):
    """Whether a time falls inside a ticket record's run, by the worker's rule
    (zoho/worker.ZohoWorker._in_run): at or after its start, before its end."""
    moment = parse(at)
    if moment < parse(record["started_at"]):
        return False
    return record["ended_at"] is None or moment < parse(record["ended_at"])


class DeskChatTests(unittest.TestCase):
    def test_the_model_check_fails_once_the_model_was_called(self):
        chat = DeskChat(replies=[say("Is the charger light on?")])
        chat.assert_model_never_called()
        chat.say("my battery won't charge", identity=RIDER)
        self.assertEqual(len(chat.llm.requests), 1)
        with self.assertRaises(AssertionError):
            chat.assert_model_never_called()
        chat.assert_model_never_called(since=1)


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
                chat.assert_model_never_called()
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
        chat.assert_model_never_called()
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

    def test_a_known_phones_ticket_store_that_cannot_be_read_is_logged_and_the_tool_decides(self):
        # Review of Task 13, Important 3: the look-up of the run's safety
        # tickets fails, then the store answers again for the tool. The ticket
        # is raised, and the log says why no earlier one was looked for.
        chat = DeskChat()
        read = chat.tickets.by_source_key
        calls = []

        def down_once(key):
            calls.append(key)
            if len(calls) == 1:
                raise StoreUnavailable("down")
            return read(key)

        with mock.patch.object(chat.tickets, "by_source_key", side_effect=down_once):
            reply = chat.say("my battery is swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        (event,) = chat.events("ticket_read_failed")
        self.assertEqual((event["error"], event["kind"]), ("StoreUnavailable", "safety"))
        self.assertTrue(is_desk_reference(reply.ticket_id))
        self.assertIn(SAFETY_MESSAGE, reply.text)
        self.assertEqual(len(chat.records()), 1)

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
        chat.assert_model_never_called()
        self.assertEqual(chat.runtime.safety_not_recorded, 2)
        # Zoho off with no number is the expected reply, not a failure.
        off = DeskChat(zoho=False)
        off.say("my battery is smoking")
        off.assert_model_never_called()
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
        self.assertEqual(runtime.llm.requests, [])
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)

    def test_any_other_message_while_the_store_is_down_still_hands_over(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery won't charge")
        self.assertEqual(runtime.llm.requests, [])
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
        chat.assert_model_never_called()
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

    @staticmethod
    def stranger_with_a_ticket(chat):
        """The run's own person reports a swollen pack; then someone else on
        the same browser, with no number known, reports smoke and types a
        number. Both safety replies."""
        owner = chat.say("my battery is swollen", identity=RIDER)
        stranger = chat.say("smoke from my battery, my number is 9999999999")
        return owner, stranger

    def test_the_runs_ticket_stays_the_first_persons_while_a_stranger_writes(self):
        # Review of Task 13, Important 1: a stranger's ticket is kept apart, so
        # the first person's next turn never wakes it and their history never
        # names it.
        chat = DeskChat()
        owner, stranger = self.stranger_with_a_ticket(chat)
        self.assertEqual(chat.llm.requests, [])
        state = chat.state()
        self.assertEqual(state.ticket_id, owner.ticket_id)
        self.assertEqual(state.newcomer_ticket_id, stranger.ticket_id)
        self.assertEqual(state.newcomer_started_at, chat.tickets.get(stranger.ticket_id)["started_at"])
        (summary,) = chat.conversations.recent_summaries("PHONE#" + ONE_BIKE)
        self.assertEqual(summary.ticket_id, owner.ticket_id)

    def test_the_first_person_coming_back_ends_the_strangers_ticket(self):
        # Review of Task 13, Important 1 ("and the reverse"): the run's own
        # person proves the same number again, with no restart_for. None of
        # their turns may reach the stranger's ticket.
        chat = DeskChat()
        owner, stranger = self.stranger_with_a_ticket(chat)
        woken = chat.tickets.get(stranger.ticket_id)["wake"]
        back = chat.say("I am back, the battery is still swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        theirs = chat.tickets.get(stranger.ticket_id)
        reported = customer_turn(chat, "smoke from my battery, my number is [phone]")
        returning = customer_turn(chat, "I am back, the battery is still swollen")
        # The stranger's run ends after their last turn and before the first
        # person's: the worker posts the one and never the other.
        self.assertIsNotNone(theirs["ended_at"])
        self.assertLess(parse(reported.at), parse(theirs["ended_at"]))
        self.assertLessEqual(parse(theirs["ended_at"]), parse(returning.at))
        self.assertEqual(theirs["wake"], woken)
        self.assertEqual(theirs["notes"], [])
        # The run's own person has a stretch of their own from here (the
        # final review, safety-flow Important 1): one safety ticket per
        # stretch, as restart_for gives one per run. Their report records a
        # ticket whose run starts where the stranger's ended, so it holds
        # their words and none of the stranger's. The stranger's is not named.
        self.assertTrue(is_desk_reference(back.ticket_id))
        self.assertNotIn(back.ticket_id, (owner.ticket_id, stranger.ticket_id))
        self.assertNotIn(stranger.ticket_id, back.text)
        ours = chat.tickets.get(back.ticket_id)
        self.assertEqual(ours["started_at"], theirs["ended_at"])
        self.assertIsNone(ours["ended_at"])
        self.assertTrue(in_window(ours, returning.at))
        self.assertFalse(in_window(ours, reported.at))
        self.assertEqual(chat.tickets.get(owner.ticket_id)["notes"], [])
        state = chat.state()
        self.assertEqual(state.ticket_id, back.ticket_id)
        self.assertEqual(state.owner_started_at, ours["started_at"])
        self.assertEqual((state.newcomer_started_at, state.newcomer_ticket_id), (None, None))
        (summary,) = chat.conversations.recent_summaries("PHONE#" + ONE_BIKE)
        self.assertEqual(summary.ticket_id, back.ticket_id)

    def test_the_owners_report_on_coming_back_is_in_their_ticket_and_the_strangers_is_not(self):
        # The final review's probe (safety-flow Important 1): the run's own
        # person says hello, someone else on the browser reports smoke with a
        # number, then the run's own person reports a swollen pack. Before the
        # fix their ticket took the run's start and was ended in the same
        # turn, so it held the stranger's smoke and not their own report.
        chat = DeskChat()
        chat.say("hello", identity=RIDER)
        stranger = chat.say("smoke from my battery, my number is 9999999999")
        back = chat.say("my battery is swollen", identity=RIDER)
        chat.assert_model_never_called()
        self.assertTrue(is_desk_reference(back.ticket_id))
        self.assertNotEqual(back.ticket_id, stranger.ticket_id)
        ours, theirs = chat.tickets.get(back.ticket_id), chat.tickets.get(stranger.ticket_id)
        smoke = customer_turn(chat, "smoke from my battery, my number is [phone]")
        swollen = customer_turn(chat, "my battery is swollen")
        self.assertTrue(in_window(ours, swollen.at))
        self.assertFalse(in_window(ours, smoke.at))
        self.assertIsNone(ours["ended_at"])
        self.assertTrue(in_window(theirs, smoke.at))
        self.assertFalse(in_window(theirs, swollen.at))
        self.assertEqual(theirs["ended_at"], ours["started_at"])

    def test_an_owners_ticket_on_a_later_turn_takes_none_of_the_strangers_turns_or_photos(self):
        chat = DeskChat(replies=[say("Thank you. Please keep it outside.")] * 4)
        chat.say("hello", identity=RIDER)
        key = "customers/clu_1/c1/photos/smoke.jpg"
        photo = media_record("media-bucket", key, "image", "image/jpeg", 2048, "c1", "clu_1", "inline", now_iso())
        chat.conversations.record_media(photo)  # stored before its turn, as api._persist_media does
        chat.say("smoke from my battery, my number is 9999999999",
                 attachments=[Attachment("image", "s3://media-bucket/" + key, "image/jpeg")])
        chat.say("I have moved the bike outside", identity=RIDER)
        before = len(chat.llm.requests)
        later = chat.say("my battery is swollen", identity=RIDER)
        chat.assert_model_never_called(since=before)
        record = chat.tickets.get(later.ticket_id)
        smoke = customer_turn(chat, "smoke from my battery, my number is [phone]")
        self.assertFalse(in_window(record, smoke.at))
        self.assertFalse(in_window(record, photo["stored_at"]))
        for text in ("I have moved the bike outside", "my battery is swollen"):
            self.assertTrue(in_window(record, customer_turn(chat, text).at), text)
        self.assertIsNone(record["ended_at"])
        self.assertEqual(record["started_at"], chat.state().owner_started_at)

    def test_a_ticket_an_agent_raises_on_the_owners_return_takes_their_stretch(self):
        # The agent's tools read the run from the facts (Runtime._run), so a
        # ticket the model raises on the turn the run's own person comes back
        # takes their stretch too, and is not ended by that same turn.
        chat = DeskChat(replies=[
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "t1"),
            say("I have raised a ticket for the team."),
        ])
        chat.say("hello", identity=RIDER)
        chat.say("smoke from my battery, my number is 9999999999")
        chat.assert_model_never_called()
        state = chat.conversations.get("c1")
        state.evidence_seen = True  # a fault ticket needs a photo (test_evidence_before_ticket)
        chat.conversations.save(state)
        back = chat.say("the charger light stays off", identity=RIDER)
        record = chat.tickets.get(back.ticket_id)
        self.assertEqual(record["kind"], "support")
        self.assertEqual(record["started_at"], chat.state().owner_started_at)
        self.assertIsNone(record["ended_at"])
        self.assertTrue(in_window(record, customer_turn(chat, "the charger light stays off").at))
        self.assertFalse(in_window(record, customer_turn(chat, "smoke from my battery, my number is [phone]").at))

    def test_a_save_conflict_on_the_owners_return_keeps_their_stretchs_start(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        chat.say("hello", identity=RIDER)
        chat.say("smoke from my battery, my number is 9999999999")
        store.conflicts = 1
        back = chat.say("my battery is swollen", identity=RIDER)
        chat.assert_model_never_called()
        self.assertEqual(len(chat.events("conversation_merged")), 1)
        state = chat.state()
        self.assertEqual(state.owner_started_at, chat.tickets.get(back.ticket_id)["started_at"])
        self.assertEqual(state.ticket_id, back.ticket_id)
        self.assertIsNone(state.newcomer_started_at)

    def test_a_strangers_wait_for_a_number_ends_with_their_stretch(self):
        # The ask was the stranger's: the run's own person's next message is
        # never read as the number to call about the stranger's report.
        chat = DeskChat(replies=[say("Thank you. Please keep it outside.")] * 4)
        chat.say("my battery is swollen", identity=RIDER)
        chat.say("my battery is smoking")
        chat.assert_model_never_called()  # both safety turns; the next one is the model's
        self.assertEqual(chat.state().awaiting_callback, "safety")
        chat.say("I have moved the bike outside", identity=RIDER)
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), (None, 0))
        self.assertIsNone(chat.state().newcomer_started_at)

    def test_a_strangers_later_reports_stay_on_their_own_ticket(self):
        # Review of Task 13, Important 2: one safety ticket per run holds for
        # the stranger too (decision 11). Their start does not move, so their
        # keys repeat, and their own next turn never ends their ticket.
        chat = DeskChat()
        owner, stranger = self.stranger_with_a_ticket(chat)
        again = chat.say("it is still smoking")
        third = chat.say("smoke again, call me on 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual((again.ticket_id, third.ticket_id), (stranger.ticket_id, stranger.ticket_id))
        self.assertIn(SAFETY_ADDED_MESSAGE.format(reference=stranger.ticket_id), again.text)
        self.assertNotIn("mobile number", again.text)
        self.assertNotIn(owner.ticket_id, again.text + third.text)
        self.assertEqual(len(chat.records()), 2)
        theirs, first = chat.tickets.get(stranger.ticket_id), chat.tickets.get(owner.ticket_id)
        self.assertIsNone(theirs["ended_at"])
        self.assertEqual(len(theirs["notes"]), 2)
        self.assertEqual(first["notes"], [])
        self.assertEqual(first["ended_at"], theirs["started_at"])
        for text in ("it is still smoking", "smoke again, call me on [phone]"):
            self.assertLessEqual(parse(theirs["started_at"]), parse(customer_turn(chat, text).at))
        self.assertEqual(chat.state().ticket_id, owner.ticket_id)

    def test_a_strangers_later_turn_wakes_their_ticket_and_never_the_runs(self):
        chat = DeskChat()
        owner, stranger = self.stranger_with_a_ticket(chat)
        theirs, first = chat.tickets.get(stranger.ticket_id)["wake"], chat.tickets.get(owner.ticket_id)["wake"]
        chat.say("hello? is anyone there")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(chat.tickets.get(stranger.ticket_id)["wake"], theirs + 1)
        self.assertEqual(chat.tickets.get(owner.ticket_id)["wake"], first)

    def test_a_strangers_ticket_on_a_later_turn_takes_in_their_first_report(self):
        # The start is set on the stranger's first turn, not on the turn that
        # records: the report that asked for a number is in their run.
        chat = DeskChat()
        chat.say("my battery is swollen", identity=RIDER)
        asked = chat.say("my battery is smoking")
        recorded = chat.say("the smoke is getting worse, call me on 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, asked.text)
        record = chat.tickets.get(recorded.ticket_id)
        self.assertLessEqual(parse(record["started_at"]), parse(customer_turn(chat, "my battery is smoking").at))
        self.assertLess(parse(customer_turn(chat, "my battery is swollen").at), parse(record["started_at"]))

    def test_a_save_conflict_on_a_strangers_turn_keeps_their_ticket_apart(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        owner = chat.say("my battery is swollen", identity=RIDER)
        store.conflicts = 1
        stranger = chat.say("smoke from my battery, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(len(chat.events("conversation_merged")), 1)
        state = chat.state()
        self.assertEqual(state.ticket_id, owner.ticket_id)
        self.assertEqual(state.newcomer_ticket_id, stranger.ticket_id)
        self.assertEqual(state.newcomer_started_at, chat.tickets.get(stranger.ticket_id)["started_at"])

    def test_the_strangers_stretch_is_kept_with_the_state_and_goes_with_restart_for(self):
        state = ConversationState("c1", turns=3, user_key="PHONE#" + ONE_BIKE,
                                  newcomer_started_at="2026-10-05T10:00:00.000000+00:00",
                                  newcomer_ticket_id="EM-1000002")
        self.assertEqual(ConversationState.from_json(state.to_json()), state)
        old = ConversationState.from_json('{"conversation_id": "c1"}')
        self.assertEqual((old.newcomer_started_at, old.newcomer_ticket_id), (None, None))
        state.restart_for("PHONE#" + TWO_BIKES, "2026-10-05T11:00:00.000000+00:00")
        self.assertEqual((state.newcomer_started_at, state.newcomer_ticket_id), (None, None))

    def test_the_owners_stretch_start_is_kept_with_the_state_and_goes_with_restart_for(self):
        state = ConversationState("c1", turns=3, user_key="PHONE#" + ONE_BIKE,
                                  owner_started_at="2026-10-05T10:05:00.000000+00:00")
        self.assertEqual(ConversationState.from_json(state.to_json()), state)
        self.assertIsNone(ConversationState.from_json('{"conversation_id": "c1"}').owner_started_at)
        state.restart_for("PHONE#" + TWO_BIKES, "2026-10-05T11:00:00.000000+00:00")
        self.assertIsNone(state.owner_started_at)


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
