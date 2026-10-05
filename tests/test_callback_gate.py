"""The callback-number gate (spec 2026-10-05, section 6, part 4).

While a handover or a safety report waits for a number, code reads the next
message for one. The gate runs after safety and before going back, the
handover, erasure and the verify step. Every test goes through
runtime.handle() with a model that raises if called, except where the turn is
meant to reach an agent.
"""

import unittest
from unittest import mock

from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.graph import NODE_NAMES
from emotorad_ai.guardrails import (
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_NO_NUMBER_MESSAGE,
    HANDOVER_NOT_RECORDED_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
    SAFETY_ASK_AGAIN_MESSAGE,
    SAFETY_NO_CONTACT_MESSAGE,
    SAFETY_NOT_RECORDED_MESSAGE,
)
from emotorad_ai.llm import say
from emotorad_ai.tools.verification import MAX_ATTEMPTS
from emotorad_ai.verify_first import INVALID_NUMBER
from tests.test_safety_without_phone import CALL_BACK, FAKE, ONE_BIKE, PROMISES, RIDER, DeskChat
from tests.test_verify_first import ConflictedStore

# The number, as customers write it.
GOLDEN = {
    "english": "my number is 9999999999",
    "hinglish": "mera number 9999999999 hai",
    "hindi": "मेरा नंबर ९९९९९९९९९९ है",
}


def codes_sent(chat):
    return [e for e in chat.events("tool_call") if e["tool"] == "request_identity_verification"]


def devanagari(digits):
    """ASCII digits written in Devanagari (०-९)."""
    return "".join(chr(0x0966 + int(digit)) for digit in digits)


class OrderTests(unittest.TestCase):
    def test_the_gate_comes_straight_after_safety_and_before_the_verify_step(self):
        self.assertEqual(NODE_NAMES.index("callback_gate"), NODE_NAMES.index("safety_gate") + 1)
        self.assertLess(NODE_NAMES.index("callback_gate"), NODE_NAMES.index("navigation_gate"))
        self.assertLess(NODE_NAMES.index("callback_gate"), NODE_NAMES.index("verify_gate"))

    def test_a_number_typed_while_a_handover_waits_sends_no_code(self):
        chat = DeskChat()
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, chat.say("I want to talk to a person").text)
        reply = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(codes_sent(chat), [])
        self.assertIsNone(chat.store.pending_code("c1"))
        self.assertEqual(reply.handled_by, "guardrail:callback:recorded")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("handover", "unverified", FAKE))
        self.assertEqual(record["source_key"], "c1:%s:handover" % chat.state().started_at)
        self.assertFalse(record["bike"])
        self.assertEqual(reply.text, NUMBER_RECEIVED_MESSAGE.format(reference=record["_id"]))
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertTrue(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)

    def test_a_number_typed_while_a_safety_report_waits_is_a_ticket_not_a_code(self):
        chat = DeskChat()
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, chat.say("my battery is smoking").text)
        reply = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(codes_sent(chat), [])
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["urgent"], record["phone"]), ("safety", True, FAKE))
        self.assertEqual(record["source_key"], "c1:%s:safety_callback" % chat.state().started_at)
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertTrue(reply.escalated)


class GoldenPhraseTests(unittest.TestCase):
    def test_a_number_in_each_language_is_recorded_for_a_handover(self):
        for language, text in GOLDEN.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say("talk to a person")
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["phone"]), ("handover", FAKE))
                self.assertEqual(reply.ticket_id, record["_id"])
                self.assertEqual(chat.state().typed_number, CALL_BACK)

    def test_a_number_in_each_language_is_recorded_for_a_safety_report(self):
        for language, text in GOLDEN.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say("my battery is swollen")
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["urgent"], record["phone"]), ("safety", True, FAKE))
                self.assertIn(NUMBER_RECEIVED_MESSAGE.format(reference=record["_id"]), reply.text)

    def test_the_number_is_phone_in_history_and_transcript(self):
        for language, text in GOLDEN.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say("talk to a person")
                chat.say(text)
                chat.assert_model_never_called()
                history = repr(chat.state().history)
                self.assertIn("[phone]", history)
                for form in (CALL_BACK, "९९९९९९९९९९"):
                    self.assertNotIn(form, history)
                said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
                self.assertIn("[phone]", said)
                self.assertNotIn("९९९९९९९९९९", said)
                self.assertNotIn(CALL_BACK, said)


class InvalidNumberTests(unittest.TestCase):
    def test_a_number_that_is_not_an_indian_mobile_is_refused_and_the_wait_goes_on(self):
        for text, raw in (("12345678", "12345678"), ("+34 612 345 678", "612 345 678")):
            with self.subTest(text=text):
                chat = DeskChat()
                chat.say("talk to a person")
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual(reply.handled_by, "guardrail:callback:invalid_number")
                self.assertEqual(reply.text, INVALID_NUMBER)
                self.assertFalse(reply.escalated)
                self.assertEqual(chat.records(), [])
                self.assertEqual(chat.state().awaiting_callback, "handover")
                self.assertIn("[number]", repr(chat.state().history))
                self.assertNotIn(raw, repr(chat.state().history))
                self.assertEqual(len(chat.events("callback_number_invalid")), 1)
                self.assertEqual(codes_sent(chat), [])
                self.assertEqual(chat.say("9999999999").handled_by, "guardrail:callback:recorded")


class QuotedReferenceTests(unittest.TestCase):
    def test_a_quoted_reference_is_not_read_as_a_bad_number(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("my old ticket is EM-1000001")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "guardrail:callback:no_number")
        self.assertEqual(chat.events("callback_number_invalid"), [])
        self.assertEqual(chat.records(), [])

    def test_a_reference_and_a_number_records_the_number(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("ticket EM-1000001, call 9999999999")
        chat.assert_model_never_called()
        (record,) = chat.records()
        self.assertEqual(record["phone"], FAKE)
        self.assertEqual(reply.ticket_id, record["_id"])


class NoNumberTests(unittest.TestCase):
    def test_a_handover_wait_ends_with_the_other_ways_line(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("I'd rather not")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:callback:no_number")
        self.assertEqual(reply.text, HANDOVER_NO_NUMBER_MESSAGE)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual((event["purpose"], event["why"]), ("handover", "no_number"))
        self.assertTrue(chat.say("my battery isn't charging").handled_by.startswith("verify_first:"))

    def test_a_safety_wait_asks_once_more_then_logs_not_recorded(self):
        chat = DeskChat()
        chat.say("my battery is smoking")
        again = chat.say("what?")
        self.assertEqual(again.handled_by, "guardrail:callback:ask_again")
        self.assertEqual(again.text, SAFETY_ASK_AGAIN_MESSAGE)
        self.assertFalse(again.escalated)
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), ("safety", 1))
        last = chat.say("no")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(last.handled_by, "guardrail:callback:no_number")
        self.assertEqual(last.text, SAFETY_NOT_RECORDED_MESSAGE)
        for promise in PROMISES:
            self.assertNotIn(promise, last.text)
        self.assertFalse(last.escalated)
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual((event["why"], event["level"]), ("no_number", "error"))
        # Counted on /health, as every safety report with no ticket is.
        self.assertEqual(chat.runtime.safety_not_recorded, 1)
        self.assertEqual(chat.records(), [])

    def test_the_ask_again_text_promises_no_call(self):
        for promise in PROMISES:
            self.assertNotIn(promise, SAFETY_ASK_AGAIN_MESSAGE)
            self.assertNotIn(promise, HANDOVER_NO_NUMBER_MESSAGE)
        self.assertIn("112", SAFETY_ASK_AGAIN_MESSAGE)
        for text in (SAFETY_ASK_AGAIN_MESSAGE, HANDOVER_NO_NUMBER_MESSAGE, HANDOVER_NOT_RECORDED_MESSAGE):
            self.assertNotIn("—", text)  # no em dash


class NotRecordedTests(unittest.TestCase):
    def test_a_safety_number_whose_record_fails_promises_nothing_and_the_wait_goes_on(self):
        chat = DeskChat()
        chat.say("my battery is smoking")
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:callback:not_recorded")
        self.assertEqual(reply.text, SAFETY_NOT_RECORDED_MESSAGE)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual((event["why"], event["level"]), ("not_recorded", "error"))
        self.assertEqual(chat.runtime.safety_not_recorded, 1)
        self.assertEqual(chat.state().awaiting_callback, "safety")
        self.assertNotIn(CALL_BACK, repr(chat.state().history))
        # The number sent again is still read here, never by the verify step.
        again = chat.say("9999999999")
        self.assertEqual(again.handled_by, "guardrail:callback:recorded")
        self.assertEqual(codes_sent(chat), [])

    def test_a_handover_number_whose_record_fails_promises_nothing_and_the_wait_goes_on(self):
        chat = DeskChat()
        chat.say("talk to a person")
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("9999999999")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "guardrail:callback:not_recorded")
        self.assertEqual(reply.text, HANDOVER_NOT_RECORDED_MESSAGE)
        self.assertFalse(reply.escalated)
        self.assertEqual(len(chat.events("handover_ticket_not_recorded")), 1)
        self.assertEqual(chat.state().awaiting_callback, "handover")
        self.assertEqual(chat.say("9999999999").handled_by, "guardrail:callback:recorded")


class StartOverTests(unittest.TestCase):
    def test_start_over_ends_the_wait(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("start over")
        chat.assert_model_never_called()
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.records(), [])
        self.assertTrue(reply.handled_by.startswith("verify_first:"), reply.handled_by)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual((event["purpose"], event["why"]), ("handover", "start_over"))


class VerificationCodeTests(unittest.TestCase):
    """A visitor whose code was sent, then a wait for a number to call, then
    the code typed (the review of Task 14, Important 1). The verify step takes
    the code and hides it as [code], so the model never sees it, and the wait
    ends."""

    def code_sent_then(self, text):
        chat = DeskChat()
        self.assertEqual(chat.say(ONE_BIKE[3:]).handled_by, "verify_first:code_sent")
        code = chat.store.pending_code("c1")
        self.assertIsNotNone(code)
        chat.say(text)
        return chat, code

    def assert_code_hidden(self, chat, code):
        history = repr(chat.state().history)
        self.assertIn("[code]", history)
        for form in (code, devanagari(code)):
            self.assertNotIn(form, history)
        said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
        self.assertNotIn(code, said)

    def test_a_code_typed_while_a_handover_waits_goes_to_the_verify_step(self):
        for form in (str, devanagari):
            with self.subTest(form=form.__name__):
                chat, code = self.code_sent_then("I want to talk to a person")
                self.assertEqual(chat.state().awaiting_callback, "handover")
                reply = chat.say("the code is " + form(code))
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual(reply.handled_by, "verify_first:verified")
                self.assertIsNone(chat.store.pending_code("c1"))
                self.assertFalse(reply.escalated)
                self.assertIsNone(chat.state().awaiting_callback)
                (event,) = chat.events("callback_wait_ended")
                self.assertEqual((event["purpose"], event["why"]), ("handover", "code"))
                self.assert_code_hidden(chat, code)
                self.assertEqual(chat.records(), [])

    def test_a_code_typed_while_a_safety_report_waits_goes_to_the_verify_step_and_is_counted(self):
        for form in (str, devanagari):
            with self.subTest(form=form.__name__):
                chat, code = self.code_sent_then("my battery is smoking")
                self.assertEqual(chat.state().awaiting_callback, "safety")
                reply = chat.say(form(code))
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual(reply.handled_by, "verify_first:verified")
                self.assertIsNone(chat.store.pending_code("c1"))
                self.assertIsNone(chat.state().awaiting_callback)
                (event,) = chat.events("callback_wait_ended")
                self.assertEqual((event["purpose"], event["why"]), ("safety", "code"))
                # The report has no ticket: alarmed and counted on /health, as
                # every safety report with no ticket is.
                (missed,) = chat.events("safety_ticket_not_recorded")
                self.assertEqual((missed["why"], missed["level"]), ("code", "error"))
                self.assertEqual(chat.runtime.safety_not_recorded, 1)
                self.assert_code_hidden(chat, code)
                self.assertEqual(chat.records(), [])

    def test_a_code_typed_with_a_handover_request_is_kept_as_code(self):
        # The final review, safety-flow Minor 11: the handover gate answers
        # before the verify step, so the code is not spent, and it must not
        # stay in what the model is shown on a later turn.
        for form in (str, devanagari):
            with self.subTest(form=form.__name__):
                chat = DeskChat()
                chat.say(ONE_BIKE[3:])
                code = chat.store.pending_code("c1")
                reply = chat.say("talk to a person, my code is " + form(code))
                chat.assert_model_never_called()
                self.assertEqual(reply.handled_by, "guardrail:human_handoff")
                self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, reply.text)
                self.assertEqual(chat.state().awaiting_callback, "handover")
                self.assert_code_hidden(chat, code)
                self.assertIn("talk to a person, my code is [code]", repr(chat.state().history))

    def test_a_code_and_a_number_typed_with_a_handover_request_are_both_hidden(self):
        chat = DeskChat()
        chat.say(ONE_BIKE[3:])
        code = chat.store.pending_code("c1")
        reply = chat.say("talk to a person, call me on 9999999999, code %s" % code)
        chat.assert_model_never_called()
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["phone"]), ("handover", FAKE))
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assert_code_hidden(chat, code)
        said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
        self.assertIn("talk to a person, call me on [phone], code [code]", said)
        self.assertNotIn(CALL_BACK, said)

    def test_six_digits_with_no_code_pending_are_no_number(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("123456")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "guardrail:callback:no_number")
        self.assertEqual(codes_sent(chat), [])

    def test_a_foreign_number_typed_while_a_code_is_pending_spends_no_try(self):
        # "+34 612 345 678" holds six digits a code could be read from. It is
        # a try at a number to call, so the wait goes on and no code is tried.
        chat, _ = self.code_sent_then("talk to a person")
        reply = chat.say("+34 612 345 678")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "guardrail:callback:invalid_number")
        self.assertEqual(chat.state().awaiting_callback, "handover")
        self.assertEqual(chat.store.attempts_left("c1"), MAX_ATTEMPTS)


class KnownPhoneTests(unittest.TestCase):
    def test_the_runs_own_person_back_after_someone_elses_ask_is_never_asked_for_a_number(self):
        # The wait was the stranger's (Task 13's stretch). The run's own person
        # has a number we know, so the gate does not read their message as the
        # number to call about the stranger's report.
        chat = DeskChat(replies=[say("Thank you. Please keep it outside.")] * 4)
        chat.say("my battery is swollen", identity=RIDER)
        chat.say("my battery is smoking")
        chat.assert_model_never_called()  # both safety turns; the next one is the model's
        self.assertEqual(chat.state().awaiting_callback, "safety")
        reply = chat.say("I have moved the bike outside", identity=RIDER)
        self.assertFalse(reply.handled_by.startswith("guardrail:callback"), reply.handled_by)
        self.assertNotIn(SAFETY_ASK_AGAIN_MESSAGE, reply.text)
        self.assertNotIn("mobile number", reply.text)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual((event["purpose"], event["why"]), ("safety", "known_phone"))
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), (None, 0))
        self.assertEqual(len(chat.records()), 1)


class SafetyWaitEndsTests(unittest.TestCase):
    """A safety report's wait for a number that ends with no ticket is
    alarmed and counted on /health, however it ends (the final review,
    safety-flow Important 4)."""

    def assert_counted(self, chat, why):
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual((event["why"], event["level"]), (why, "error"))
        self.assertEqual(chat.runtime.safety_not_recorded, 1)
        self.assertEqual(chat.records(), [])
        self.assertIsNone(chat.state().awaiting_callback)

    def test_a_safety_wait_ended_by_a_known_phone_is_counted(self):
        # The run's own person back after someone else's report asked for a
        # number: that report is left with no ticket.
        chat = DeskChat(replies=[say("Thank you. Please keep it outside.")] * 4)
        chat.say("hello", identity=RIDER)
        chat.say("my battery is smoking")
        chat.assert_model_never_called()
        self.assertEqual(chat.state().awaiting_callback, "safety")
        chat.say("I have moved the bike outside", identity=RIDER)
        (ended,) = chat.events("callback_wait_ended")
        self.assertEqual((ended["purpose"], ended["why"]), ("safety", "known_phone"))
        self.assert_counted(chat, "known_phone")

    def test_a_safety_wait_ended_by_start_over_is_counted(self):
        chat = DeskChat()
        chat.say("my battery is smoking")
        reply = chat.say("start over")
        chat.assert_model_never_called()
        self.assertTrue(reply.handled_by.startswith("verify_first:"), reply.handled_by)
        (ended,) = chat.events("callback_wait_ended")
        self.assertEqual((ended["purpose"], ended["why"]), ("safety", "start_over"))
        self.assert_counted(chat, "start_over")

    def test_a_safety_wait_left_when_zoho_went_off_is_counted(self):
        chat = DeskChat()
        chat.say("my battery is smoking")
        chat.registry.tickets = chat.mock  # Zoho switched off between turns
        reply = chat.say("9999999999")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        (ended,) = chat.events("callback_wait_ended")
        self.assertEqual((ended["purpose"], ended["why"]), ("safety", "not_recordable"))
        self.assert_counted(chat, "not_recordable")

    def test_a_handover_wait_that_ends_is_not_a_safety_report(self):
        chat = DeskChat()
        chat.say("talk to a person")
        chat.say("start over")
        chat.assert_model_never_called()
        self.assertEqual(chat.events("safety_ticket_not_recorded"), [])
        self.assertEqual(chat.runtime.safety_not_recorded, 0)


class ConflictTests(unittest.TestCase):
    def test_a_conflict_after_a_recording_merges_and_does_not_run_again(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        chat.say("talk to a person")
        store.conflicts = 1
        reply = chat.say("9999999999")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "guardrail:callback:recorded")
        (record,) = chat.records()
        saved = chat.state()
        self.assertIn("merged_after_conflict", saved.transitions)
        self.assertIsNone(saved.awaiting_callback)
        self.assertEqual(saved.typed_number, CALL_BACK)
        self.assertEqual(saved.ticket_id, record["_id"])
        self.assertEqual(len(chat.events("ticket_recorded")), 1)


class ZohoOffTests(unittest.TestCase):
    def test_a_wait_left_when_zoho_went_off_is_dropped(self):
        chat = DeskChat()
        chat.say("talk to a person")
        chat.registry.tickets = chat.mock  # Zoho switched off between turns
        reply = chat.say("9999999999")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual(event["why"], "not_recordable")


if __name__ == "__main__":
    unittest.main()
