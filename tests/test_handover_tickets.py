"""Talk to a person with Zoho on (spec 2026-10-05, section 6, part 4).

A customer's request records a handover ticket, or asks for the number to
record it with. A dealer's request records nothing. Zoho off is today's
handover. Every test goes through runtime.handle() and checks that the model
was never called.
"""

import unittest
from unittest import mock

from emotorad_ai.adapters import DealerWhatsAppAdapter
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.guardrails import (
    HANDOFF_MESSAGE,
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_GONE_MESSAGE,
    HANDOVER_NOT_RECORDED_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
)
from tests.test_safety_without_phone import CALL_BACK, FAKE, ONE_BIKE, PROMISES, RIDER, DeskChat
from tests.test_verify_first import ConflictedStore

HEALTHY_DEALER = "919000000001"  # Royal Cycle Stores (fixtures)


class WithAPhoneTests(unittest.TestCase):
    def test_a_verified_customer_gets_a_handover_ticket_and_its_reference(self):
        chat = DeskChat()
        reply = chat.say("I want to talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("handover", "verified", ONE_BIKE))
        self.assertEqual(record["source_key"], "c1:%s:handover" % chat.state().started_at)
        self.assertIn("EMXP2025004417", repr(record["bike"]))
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=record["_id"]), reply.text)
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertTrue(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)

    def test_a_verified_handover_ticket_carries_the_cover_and_the_oms_name(self):
        # Amendment A, finding 30: what a ticket the agent raises would carry.
        chat = DeskChat()
        chat.say("I want to talk to a person", identity=RIDER)
        chat.assert_model_never_called()
        (record,) = chat.records()
        self.assertEqual(record["coverage"], "computed")
        self.assertEqual(record["customer_name"], "Ananya Rao")
        self.assertEqual(record["bike"], {"model": "EMX Plus", "frame_number": "EMXP2025004417",
                                          "frame_number_source": None})

    def test_a_number_in_the_first_message_is_recorded(self):
        for text, typed in (("call me on 99999 99999", "99999 99999"), ("call me on ९९९९९९९९९९", "९९९९९९९९९९")):
            with self.subTest(text=text):
                chat = DeskChat()
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["identity"], record["phone"]),
                                 ("handover", "unverified", FAKE))
                self.assertFalse(record["bike"])
                self.assertIsNone(record["coverage"])
                self.assertIsNone(record["customer_name"])
                self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=record["_id"]), reply.text)
                self.assertTrue(reply.escalated)
                history = repr(chat.state().history)
                self.assertIn("[phone]", history)
                self.assertNotIn(typed, history)
                said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
                self.assertIn("[phone]", said)
                self.assertNotIn(typed, said)
                self.assertFalse([e for e in chat.events("tool_call")
                                  if e["tool"] == "request_identity_verification"])

    def test_a_verified_customer_who_types_another_number_is_called_on_it_unverified(self):
        chat = DeskChat()
        chat.say("please call me on 9999999999", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual((record["phone"], record["identity"]), (FAKE, "unverified"))
        self.assertFalse(record["bike"])
        self.assertIsNone(record["coverage"])
        self.assertIsNone(record["customer_name"])


class WithoutANumberTests(unittest.TestCase):
    def test_the_number_is_asked_for_and_nothing_is_escalated_yet(self):
        chat = DeskChat()
        reply = chat.say("I want to talk to a person")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertEqual(chat.state().awaiting_callback, "handover")
        self.assertEqual(chat.events("escalation"), [])

    def test_a_record_that_fails_promises_nothing_and_keeps_reading_the_number(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("call me on 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(HANDOVER_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(len(chat.events("handover_ticket_not_recorded")), 1)
        self.assertEqual(chat.state().awaiting_callback, "handover")
        self.assertEqual(chat.say("9999999999").handled_by, "guardrail:callback:recorded")

    def test_a_known_phones_record_that_fails_promises_nothing_and_waits_for_nothing(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("I want to talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(HANDOVER_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)


class OneTicketPerRunTests(unittest.TestCase):
    def test_a_second_request_adds_a_note_to_the_runs_ticket(self):
        chat = DeskChat()
        first = chat.say("call me on 9999999999")
        second = chat.say("I want to talk to a human")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(second.ticket_id, first.ticket_id)
        (record,) = chat.records()
        self.assertEqual([note["text"] for note in record["notes"]], ["Customer asked for a person."])
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=first.ticket_id), second.text)
        self.assertTrue(second.escalated)

    def test_a_safety_ticket_in_the_run_gets_the_note(self):
        chat = DeskChat()
        safety = chat.say("my battery is swollen", identity=RIDER)
        handover = chat.say("talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(handover.ticket_id, safety.ticket_id)
        (record,) = chat.records()
        self.assertEqual(len(record["notes"]), 1)

    def test_a_ticket_gone_from_desk_is_not_quoted(self):
        chat = DeskChat()
        first = chat.say("call me on 9999999999")
        (record,) = chat.records()
        with mock.patch.object(chat.tickets, "get", return_value=dict(record, state="gone")):
            reply = chat.say("I want to talk to a human")
        self.assertEqual(chat.llm.requests, [])
        self.assertNotIn(first.ticket_id, reply.text)
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, reply.text)

    def test_a_conflict_after_a_note_merges_and_adds_no_second_note(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        first = chat.say("my battery is swollen", identity=RIDER)
        store.conflicts = 1
        second = chat.say("talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(second.ticket_id, first.ticket_id)
        self.assertIn("merged_after_conflict", chat.state().transitions)
        (record,) = chat.records()
        self.assertEqual(len(record["notes"]), 1)


class GoneHandoverTests(unittest.TestCase):
    """The run's handover ticket deleted or merged in Desk (the review of
    Task 14, Important 2). Its key returns that record for the rest of the
    run, so no number can record another. Nothing is asked for and no wait is
    set, and each later request gets the same reply, which promises nothing
    and does not ask the customer to try again."""

    def assert_gone_reply(self, chat, reply, gone):
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertIn(HANDOVER_GONE_MESSAGE, reply.text)
        self.assertNotIn(gone, reply.text)
        self.assertNotIn(HANDOVER_ASK_NUMBER_MESSAGE, reply.text)
        self.assertNotIn(HANDOVER_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertIsNone(chat.state().awaiting_callback)

    def test_a_typed_numbers_later_requests_are_not_a_refusal_loop(self):
        chat = DeskChat()
        first = chat.say("call me on 9999999999")
        chat.mark_gone(first.ticket_id)
        for text in ("I want to talk to a person", "call me on 9999999999", "talk to a human"):
            with self.subTest(text=text):
                self.assert_gone_reply(chat, chat.say(text), first.ticket_id)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual([e["why"] for e in chat.events("handover_ticket_not_recorded")], ["ticket_gone"] * 3)
        self.assertEqual(len(chat.records()), 1)
        self.assertEqual(chat.tickets.get(first.ticket_id)["notes"], [])

    def test_a_known_phones_later_request_is_not_told_to_try_again(self):
        chat = DeskChat()
        first = chat.say("I want to talk to a person", identity=RIDER)
        chat.mark_gone(first.ticket_id)
        reply = chat.say("I want to talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assert_gone_reply(chat, reply, first.ticket_id)
        self.assertEqual([e["why"] for e in chat.events("handover_ticket_not_recorded")], ["ticket_gone"])
        self.assertEqual(len(chat.records()), 1)

    def test_a_wait_saved_before_its_ticket_went_ends_on_the_number(self):
        # A wait that outlived its ticket (saved by an earlier version, or a
        # second tab): the number ends it, and the customer is not refused
        # again on every number they send.
        chat = DeskChat()
        first = chat.say("call me on 9999999999")
        chat.mark_gone(first.ticket_id)
        state = chat.state()
        state.awaiting_callback, state.callback_asks = "handover", 0
        chat.conversations.save(state)
        reply = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:callback:not_recorded")
        self.assertEqual(reply.text, HANDOVER_GONE_MESSAGE)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual((event["purpose"], event["why"]), ("handover", "ticket_gone"))
        self.assertNotIn(CALL_BACK, repr(chat.state().history))
        self.assertEqual(len(chat.records()), 1)

    def test_the_owners_gone_ticket_never_stops_someone_elses_request(self):
        # Keys are per stretch of the run (Task 13): a second person on the
        # browser is asked for their number as usual.
        chat = DeskChat()
        owner = chat.say("I want to talk to a person", identity=RIDER)
        chat.mark_gone(owner.ticket_id)
        asked = chat.say("I want to talk to a person")
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, asked.text)
        self.assertEqual(chat.state().awaiting_callback, "handover")
        recorded = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(recorded.handled_by, "guardrail:callback:recorded")
        self.assertNotEqual(recorded.ticket_id, owner.ticket_id)
        self.assertEqual(chat.events("handover_ticket_not_recorded"), [])

    def test_the_gone_text_promises_nothing(self):
        for promise in PROMISES + ("try again",):
            self.assertNotIn(promise, HANDOVER_GONE_MESSAGE.lower())
        self.assertNotIn("—", HANDOVER_GONE_MESSAGE)  # no em dash


class SomeoneElsesRunTests(unittest.TestCase):
    """A second person on the same browser before restart_for (Task 13's
    stretch). Their request never notes or quotes the run's own ticket, and
    their ticket has a run of its own."""

    def test_a_strangers_request_never_takes_the_runs_ticket(self):
        chat = DeskChat()
        owner = chat.say("my battery is swollen", identity=RIDER)
        asked = chat.say("I want to talk to a person")
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, asked.text)
        self.assertNotIn(owner.ticket_id, asked.text)
        self.assertIsNone(asked.ticket_id)
        recorded = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(recorded.text, NUMBER_RECEIVED_MESSAGE.format(reference=recorded.ticket_id))
        self.assertNotEqual(recorded.ticket_id, owner.ticket_id)
        theirs, ours = chat.tickets.get(recorded.ticket_id), chat.tickets.get(owner.ticket_id)
        self.assertEqual(ours["notes"], [])
        self.assertEqual((theirs["kind"], theirs["phone"]), ("handover", FAKE))
        self.assertEqual(theirs["started_at"], chat.state().newcomer_started_at)
        self.assertNotEqual(theirs["started_at"], chat.state().started_at)
        self.assertEqual(theirs["source_key"], "c1:%s:handover" % theirs["started_at"])
        self.assertEqual(chat.state().ticket_id, owner.ticket_id)
        self.assertEqual(chat.state().newcomer_ticket_id, recorded.ticket_id)

    def test_a_strangers_second_request_notes_their_own_ticket(self):
        chat = DeskChat()
        owner = chat.say("my battery is swollen", identity=RIDER)
        first = chat.say("call me on 9999999999")
        second = chat.say("talk to a person")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(second.ticket_id, first.ticket_id)
        self.assertNotEqual(first.ticket_id, owner.ticket_id)
        self.assertEqual(len(chat.tickets.get(first.ticket_id)["notes"]), 1)
        self.assertEqual(chat.tickets.get(owner.ticket_id)["notes"], [])


class DealerTests(unittest.TestCase):
    def test_a_dealers_request_records_nothing(self):
        chat = DeskChat()
        message = DealerWhatsAppAdapter(chat.runtime.resolver).to_message(
            {"from": HEALTHY_DEALER, "text": "please connect me to my account manager", "conversation_id": "d1"})
        reply = chat.runtime.handle(message)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertIn(HANDOFF_MESSAGE, reply.text)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertEqual(chat.mock.tickets, {})
        self.assertIsNone(chat.conversations.peek("d1").awaiting_callback)


class ZohoOffTests(unittest.TestCase):
    def test_zoho_off_is_todays_handover(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("call me on 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertIn(HANDOFF_MESSAGE, reply.text)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.mock.tickets, {})
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.say(CALL_BACK).handled_by, "verify_first:code_sent")

    def test_zoho_off_with_a_known_phone_is_todays_handover(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("I want to talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(HANDOFF_MESSAGE, reply.text)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.mock.tickets, {})


if __name__ == "__main__":
    unittest.main()
