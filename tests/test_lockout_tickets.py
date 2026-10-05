"""Lock-out tickets and the caps on unverified tickets (spec 2026-10-05,
section 6, part 4).

The lock-out (five wrong codes, or a fourth code asked for) records one
`lockout` ticket per run. It uses the first number found in this message, the
pending number, or the last number a code went to. Unverified tickets that
are not urgent are capped at two per number and fifty in all per calendar day
in IST. Urgent tickets are never capped.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.guardrails import (
    CAP_OVERALL_MESSAGE,
    CAP_PER_NUMBER_MESSAGE,
    HANDOVER_NOT_RECORDED_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    REFERENCE_SUFFIX,
)
from emotorad_ai.tickets.caps import (
    CAP_TEXTS,
    OVERALL,
    OVERALL_CAP,
    PER_NUMBER,
    PER_NUMBER_CAP,
    cap_reached,
    ist_day_start,
)
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tools.mocks import RAISE_INTAKE_TICKET
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.verify_first import LOCKED, LOCKED_WHY, PASSING_ON, TOO_MANY_CODES, TOO_MANY_WHY
from tests.test_safety_without_phone import CALL_BACK, FAKE, ONE_BIKE, PROMISES, RIDER, DeskChat

ROOT = Path(__file__).resolve().parents[1]

NOW = "2026-10-05T07:00:00.000000+00:00"  # 12:30 IST, 5 October
EARLIER = "2026-10-05T01:00:00.000000+00:00"  # 06:30 IST, the same day
NEXT_DAY = "2026-10-05T19:00:00.000000+00:00"  # 00:30 IST, 6 October
PENDING = "+919700000010"  # the fixture rider's number, where the codes go


def at(now):
    """The runtime's clock for the caps, held at `now`."""
    return mock.patch("emotorad_ai.runtime.now_iso", return_value=now)


def seed(chat, phone, count=1, created_at=EARLIER, kind="handover", identity="unverified"):
    """Records from earlier chats, written straight to the store."""
    for _ in range(count):
        reference = chat.tickets.next_reference()
        chat.tickets.insert(new_record(
            reference=reference, chat_reference="stage:" + reference, source_key="seed:" + reference,
            mode="test", kind=kind, conversation_id="older-chat", started_at=created_at, cluster_id=None,
            channel="website_chat", phone=phone, identity=identity, category=None, ai_severity=None,
            summary="", claims={}, bike=None, coverage=None, customer_name=None, created_at=created_at,
        ))


def wrong(chat):
    return "000000" if chat.store.pending_code("c1") != "000000" else "111111"


def lock_out(chat):
    chat.say("hi")
    chat.say("9700000010")
    for _ in range(4):
        chat.say(wrong(chat))
    return chat.say(wrong(chat))


class LockoutTests(unittest.TestCase):
    def test_five_wrong_codes_record_one_lockout_ticket_on_the_pending_number(self):
        chat = DeskChat()
        locked = lock_out(chat)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(locked.handled_by, "verify_first:locked")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("lockout", "unverified", PENDING))
        self.assertEqual(record["source_key"], "c1:%s:lockout" % chat.state().started_at)
        self.assertFalse(record["bike"])
        self.assertEqual(locked.text, LOCKED + REFERENCE_SUFFIX.format(reference=record["_id"]))
        self.assertEqual(locked.ticket_id, record["_id"])
        self.assertTrue(locked.escalated)
        # One per run: every later locked message gets the same reference.
        again = chat.say("hello?")
        self.assertEqual(again.ticket_id, record["_id"])
        self.assertEqual(len(chat.records()), 1)

    def test_a_number_typed_while_locked_is_kept_as_phone(self):
        chat = DeskChat()
        lock_out(chat)
        chat.say("9876543210")
        self.assertNotIn("9876543210", repr(chat.state().history))

    def test_a_fourth_code_asked_for_records_the_number_in_the_message(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("9876543210")
        chat.say("9700000009")
        reply = chat.say("9999999999")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["phone"]), ("lockout", FAKE))
        self.assertEqual(reply.text, TOO_MANY_CODES + REFERENCE_SUFFIX.format(reference=record["_id"]))
        self.assertEqual(reply.ticket_id, record["_id"])

    def test_a_fourth_code_asked_for_with_no_number_uses_the_pending_one(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("resend")
        chat.say("resend")
        reply = chat.say("resend")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        (record,) = chat.records()
        self.assertEqual(record["phone"], PENDING)

    def test_with_the_code_cancelled_the_last_number_a_code_went_to_is_used(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("resend")
        chat.say("resend")
        chat.say("wrong number")  # cancels the pending code
        self.assertEqual(chat.state().last_code_phone, PENDING)
        reply = chat.say("my order number is EMO-100234")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        (record,) = chat.records()
        self.assertEqual(record["phone"], PENDING)

    def test_the_last_number_a_code_went_to_follows_each_code(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        self.assertEqual(chat.state().last_code_phone, PENDING)
        chat.say("sorry, wrong number, it's 9876543210")
        self.assertEqual(chat.state().last_code_phone, "+919876543210")

    def test_zoho_off_locks_out_as_today(self):
        chat = DeskChat(zoho=False)
        locked = lock_out(chat)
        self.assertEqual(locked.text, LOCKED)
        self.assertIsNone(locked.ticket_id)
        self.assertTrue(locked.escalated)
        self.assertEqual(chat.mock.tickets, {})

    def test_the_texts_keep_their_wording_when_put_together_from_their_parts(self):
        self.assertEqual(LOCKED, LOCKED_WHY + " " + PASSING_ON)
        self.assertEqual(TOO_MANY_CODES, TOO_MANY_WHY + " " + PASSING_ON)
        self.assertEqual(
            LOCKED,
            "That's too many wrong codes, so I can't confirm it's you here. I'm passing you to our support "
            "team, who can verify you another way.",
        )
        self.assertEqual(
            TOO_MANY_CODES,
            "I can't send any more codes in this chat. I'm passing you to our support team, who can verify "
            "you another way.",
        )

    def assert_nothing_promised(self, chat, reply, why_text):
        self.assertEqual(reply.text, why_text + " " + HANDOVER_NOT_RECORDED_MESSAGE)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertNotIn(PASSING_ON, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.events("escalation"), [])
        self.assertEqual(chat.records(), [])

    def test_a_lockout_whose_record_fails_promises_nothing(self):
        # The final review, safety-flow Important 2: with Zoho on, a lock-out
        # that recorded nothing said "I'm passing you to our support team"
        # and escalated.
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            locked = lock_out(chat)
        chat.assert_model_never_called()
        self.assertEqual(locked.handled_by, "verify_first:locked")
        self.assert_nothing_promised(chat, locked, LOCKED_WHY)
        (event,) = chat.events("lockout_ticket_not_recorded")
        self.assertEqual(event["why"], "not_recorded")
        self.assertEqual([e["error"] for e in chat.events("ticket_record_failed")], ["StoreUnavailable"])
        # The next locked message tries again, and promises only once recorded.
        again = chat.say("hello?")
        self.assertEqual(again.ticket_id, chat.records()[0]["_id"])
        self.assertTrue(again.escalated)

    def test_a_lockout_with_no_number_to_call_promises_nothing(self):
        # No number in the message, none pending and none a code went to.
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("resend")
        chat.say("resend")
        state = chat.conversations.get("c1")
        state.last_code_phone = None
        chat.conversations.save(state)
        with mock.patch.object(chat.store, "pending_phone", return_value=None):
            reply = chat.say("resend")
        chat.assert_model_never_called()
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        self.assertIsNone(chat.state().last_code_phone)
        self.assert_nothing_promised(chat, reply, TOO_MANY_WHY)
        (event,) = chat.events("lockout_ticket_not_recorded")
        self.assertEqual(event["why"], "no_number")

    def test_a_lockout_with_no_number_to_call_records_nothing_and_is_logged(self):
        chat = DeskChat()
        chat.say("hi")
        message = InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text="hello",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
        )
        recorded = chat.runtime._record_lockout(message, chat.state(), None, "locked")
        self.assertEqual(tuple(recorded), (None, None))
        self.assertEqual(chat.records(), [])
        (event,) = chat.events("lockout_ticket_not_recorded")
        self.assertEqual(event["why"], "no_number")


class CapHelperTests(unittest.TestCase):
    def test_the_day_starts_at_midnight_in_ist(self):
        self.assertEqual(ist_day_start(NOW), "2026-10-04T18:30:00.000000+00:00")
        self.assertEqual(ist_day_start("2026-10-05T18:29:59.999999+00:00"), "2026-10-04T18:30:00.000000+00:00")
        self.assertEqual(ist_day_start("2026-10-05T18:30:00.000000+00:00"), "2026-10-05T18:30:00.000000+00:00")

    def test_no_store_never_caps(self):
        self.assertIsNone(cap_reached(None, phone=FAKE, source_key="c1:s:handover", now=NOW))

    def test_a_recorded_source_key_is_never_capped(self):
        chat = DeskChat()
        seed(chat, FAKE, count=PER_NUMBER)
        self.assertIsNone(cap_reached(chat.tickets, phone=FAKE, source_key="seed:EM-1000001", now=NOW))
        self.assertEqual(cap_reached(chat.tickets, phone=FAKE, source_key="c1:s:handover", now=NOW), "per_number")

    def test_with_no_number_only_the_overall_cap_applies(self):
        chat = DeskChat()
        seed(chat, FAKE, count=PER_NUMBER)
        self.assertIsNone(cap_reached(chat.tickets, phone=None, source_key="c1:s:handover", now=NOW))
        seed(chat, "+919000000001", count=OVERALL - PER_NUMBER)
        self.assertEqual(cap_reached(chat.tickets, phone=None, source_key="c1:s:handover", now=NOW), OVERALL_CAP)

    def test_every_cap_has_the_text_the_customer_is_told(self):
        self.assertEqual(CAP_TEXTS, {PER_NUMBER_CAP: CAP_PER_NUMBER_MESSAGE, OVERALL_CAP: CAP_OVERALL_MESSAGE})

    def test_only_unverified_tickets_that_are_not_urgent_count(self):
        chat = DeskChat()
        seed(chat, FAKE, count=PER_NUMBER, identity="verified")
        seed(chat, FAKE, count=PER_NUMBER, kind="safety")
        self.assertIsNone(cap_reached(chat.tickets, phone=FAKE, source_key="c1:s:handover", now=NOW))


class CapTests(unittest.TestCase):
    def test_a_third_unverified_ticket_for_one_number_in_a_day_is_refused(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        with at(NOW):
            reply = chat.say("call me on 9999999999")
            other = chat.say("call me on 9876543210", cid="c2")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(CAP_PER_NUMBER_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual((event["cap"], event["kind"], event["level"]), ("per_number", "handover", "error"))
        # Another number the same day is recorded.
        self.assertTrue(is_desk_reference(other.ticket_id))
        self.assertEqual(len(chat.records()), PER_NUMBER + 1)

    def test_the_callback_number_is_capped_too(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        with at(NOW):
            chat.say("talk to a person")
            reply = chat.say(CALL_BACK)
        self.assertEqual(reply.handled_by, "guardrail:callback:capped")
        self.assertEqual(reply.text, CAP_PER_NUMBER_MESSAGE)
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(len(chat.records()), PER_NUMBER)

    def test_the_day_is_the_calendar_day_in_ist(self):
        chat = DeskChat(ticket_clock=lambda: NEXT_DAY)
        seed(chat, FAKE, count=PER_NUMBER, created_at=EARLIER)
        with at(NEXT_DAY):
            reply = chat.say("call me on 9999999999")
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=reply.ticket_id), reply.text)

    def test_fifty_unverified_tickets_in_a_day_refuse_any_more(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, None, count=OVERALL)
        with at(NOW):
            reply = chat.say("call me on 9999999999")
        self.assertIn(CAP_OVERALL_MESSAGE, reply.text)
        self.assertIsNone(reply.ticket_id)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual(event["cap"], "overall")

    def test_urgent_tickets_are_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        seed(chat, None, count=OVERALL)
        with at(NOW):
            reply = chat.say("my battery is smoking, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(is_desk_reference(reply.ticket_id))
        self.assertEqual(chat.tickets.get(reply.ticket_id)["kind"], "safety")
        self.assertEqual(chat.events("unverified_ticket_capped"), [])

    def test_a_verified_customers_handover_is_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, ONE_BIKE, count=PER_NUMBER)
        seed(chat, None, count=OVERALL)
        with at(NOW):
            reply = chat.say("I want to talk to a person", identity=RIDER)
        self.assertTrue(is_desk_reference(reply.ticket_id))
        self.assertEqual(chat.tickets.get(reply.ticket_id)["identity"], "verified")
        self.assertEqual(chat.events("unverified_ticket_capped"), [])

    def test_a_locked_out_number_over_its_cap_is_told_so_and_nothing_is_promised(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, PENDING, count=PER_NUMBER)
        with at(NOW):
            locked = lock_out(chat)
        self.assertEqual(locked.text, LOCKED_WHY + " " + CAP_PER_NUMBER_MESSAGE)
        self.assertNotIn(PASSING_ON, locked.text)
        self.assertFalse(locked.escalated)
        self.assertIsNone(locked.ticket_id)
        self.assertEqual(len(chat.records()), PER_NUMBER)

    def test_a_capped_lockout_is_alarmed_once_and_not_tried_again_on_every_message(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, PENDING, count=PER_NUMBER)
        with at(NOW):
            locked = lock_out(chat)
            later = chat.say("hello?")
            more = chat.say("is anyone there?")
        for reply in (locked, later, more):
            self.assertEqual(reply.text, LOCKED_WHY + " " + CAP_PER_NUMBER_MESSAGE)
            self.assertFalse(reply.escalated)
            self.assertIsNone(reply.ticket_id)
        self.assertEqual(len(chat.events("unverified_ticket_capped")), 1)
        self.assertEqual(len(chat.records()), PER_NUMBER)
        self.assertIn("lockout_capped:per_number", chat.state().transitions)
        self.assertEqual(chat.llm.requests, [])

    def test_a_lockout_over_the_overall_cap_keeps_saying_that_cap(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, None, count=OVERALL)
        with at(NOW):
            locked = lock_out(chat)
            later = chat.say("hello?")
        self.assertEqual(locked.text, LOCKED_WHY + " " + CAP_OVERALL_MESSAGE)
        self.assertEqual(later.text, LOCKED_WHY + " " + CAP_OVERALL_MESSAGE)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual((event["cap"], event["kind"]), ("overall", "lockout"))

    def test_a_retry_of_a_recorded_ticket_is_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        with at(NOW):
            locked = lock_out(chat)
            seed(chat, PENDING, count=PER_NUMBER)  # the number is now over its cap
            again = chat.say("hello?")
        self.assertEqual(again.ticket_id, locked.ticket_id)
        self.assertEqual(chat.events("unverified_ticket_capped"), [])

    def test_a_store_that_cannot_count_does_not_cap_and_says_so(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        with at(NOW), mock.patch.object(chat.tickets, "unverified_since", side_effect=StoreUnavailable("down")):
            reply = chat.say("call me on 9999999999")
        self.assertTrue(is_desk_reference(reply.ticket_id))
        (event,) = chat.events("ticket_cap_unchecked")
        self.assertEqual((event["kind"], event["error"]), ("handover", "StoreUnavailable"))
        self.assertEqual(chat.events("unverified_ticket_capped"), [])

    def test_with_zoho_off_nothing_is_capped_or_counted(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("call me on 9999999999")
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.events("unverified_ticket_capped"), [])
        self.assertEqual(chat.events("ticket_cap_unchecked"), [])


class IntakeCapTests(unittest.TestCase):
    def call(self, chat, typed, strength=ANONYMOUS, phone=None, key="intake-1"):
        context = ToolContext(
            conversation_id="c1", persona="customer", started_at=NOW, phone=phone,
            late={"typed_number": lambda: typed, "identity_strength": lambda: strength,
                  "channel": lambda: "website_chat"},
        )
        with mock.patch("emotorad_ai.tools.mocks.now_iso", return_value=NOW):
            return chat.registry.call(
                RAISE_INTAKE_TICKET, {"summary": "Charger not working.", "idempotency_key": key}, context)

    def test_the_intake_tool_uses_the_same_caps(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        envelope = self.call(chat, CALL_BACK)
        self.assertEqual(envelope["error"]["code"], "unverified_ticket_capped")
        self.assertIn(CAP_PER_NUMBER_MESSAGE, envelope["error"]["message"])
        self.assertEqual(len(chat.records()), PER_NUMBER)

    def test_the_overall_cap_refuses_an_intake_too(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, None, count=OVERALL)
        envelope = self.call(chat, CALL_BACK)
        self.assertEqual(envelope["error"]["code"], "unverified_ticket_capped")
        self.assertIn(CAP_OVERALL_MESSAGE, envelope["error"]["message"])

    def test_another_number_is_not_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        envelope = self.call(chat, "9876543210")
        self.assertTrue(is_desk_reference(envelope["data"]["ticket_id"]))

    def test_a_verified_customers_intake_is_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, ONE_BIKE, count=PER_NUMBER)
        seed(chat, None, count=OVERALL)
        envelope = self.call(chat, None, strength=VERIFIED, phone=ONE_BIKE)
        self.assertTrue(is_desk_reference(envelope["data"]["ticket_id"]))
        self.assertEqual(envelope["data"]["identity"], "verified")

    def test_a_retry_of_a_recorded_intake_is_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        first = self.call(chat, CALL_BACK)
        seed(chat, FAKE, count=PER_NUMBER)  # the number is now over its cap
        second = self.call(chat, CALL_BACK)
        self.assertEqual(second["data"]["ticket_id"], first["data"]["ticket_id"])

    def test_a_capped_intake_is_logged(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        envelope = self.call(chat, CALL_BACK)
        chat.runtime._remember_lookup(chat.conversations.get("c1"), RAISE_INTAKE_TICKET, {}, envelope)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual((event["kind"], event["level"]), ("intake", "error"))

    def test_another_refusal_is_not_logged_as_a_cap(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        envelope = self.call(chat, None)
        self.assertEqual(envelope["error"]["code"], "contact_number_required")
        chat.runtime._remember_lookup(chat.conversations.get("c1"), RAISE_INTAKE_TICKET, {}, envelope)
        self.assertEqual(chat.events("unverified_ticket_capped"), [])

    def test_with_zoho_off_the_mock_is_never_capped(self):
        chat = DeskChat(zoho=False)
        first = self.call(chat, CALL_BACK)
        self.assertEqual(first["data"]["ticket_id"], "EM-00001")


class DocsTests(unittest.TestCase):
    """The edge case register and the app contract say what the code does."""

    def setUp(self):
        self.register = (ROOT / "docs" / "Emotorad_Edge_Case_Register.md").read_text(encoding="utf-8")
        self.contract = (ROOT / "docs" / "contracts" / "amiigo-support-chat.md").read_text(encoding="utf-8")
        self.source = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "src" / "emotorad_ai").rglob("*.py"))

    def section(self, heading):
        start = self.register.index(heading)
        found = re.search(r"\n## ", self.register[start + len(heading):])
        return self.register[start:start + len(heading) + found.start()] if found else self.register[start:]

    def test_the_register_has_a_row_for_each_handover_that_records_nothing(self):
        text = self.section("## 7. Handovers that record no Zoho ticket")
        rows = re.findall(r"^\| (7\.[0-9]+) \|", text, re.MULTILINE)
        self.assertEqual(rows, ["7.%d" % n for n in range(1, 11)])
        for line in text.splitlines():
            if re.match(r"\| 7\.[0-9]+ \|", line):
                self.assertIn("**CAPTURE**", line)

    def test_the_register_names_only_events_the_code_has(self):
        text = self.section("## 7. Handovers that record no Zoho ticket")
        for name in ("evidence_not_forthcoming", "coverage_claim_blocked", "order_claim_blocked",
                     "coverage_post_check", "order_post_check", "ticket_promise_unbacked", "llm_error",
                     "empty_reply", "stuck_agent", "agent_requested_handover", "llm_error_after_write",
                     "store_unavailable", "callback_number_invalid", "safety_ticket_not_recorded",
                     "safety_backstop_failed", "unsupported_topic"):
            with self.subTest(name=name):
                self.assertIn(name, text)
                self.assertRegex(self.source, r"[\"']%s" % name)

    def test_the_register_keeps_the_log_and_reference_gaps_found_while_building(self):
        text = self.section("## 8. Ticket references and log redaction gaps")
        for needle in ("EM-0", "48 h", "mobile:9876543210pls", "phone=9876543210pls", "0034"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        for line in text.splitlines():
            if re.match(r"\| 8\.[0-9]+ \|", line):
                self.assertIn("**CAPTURE**", line)

    def test_the_contract_gives_the_handover_wording_and_points_at_the_right_heading(self):
        row = next(line for line in self.contract.splitlines() if line.startswith("| Handover wording |"))
        self.assertIn("I've passed this conversation to our support team", row)
        self.assertIn("What mobile number can they reach you on?", row)
        self.assertIn("`escalated: false`", row)
        self.assertNotIn("comes with the next part", self.contract)
        self.assertNotIn('see "What changes with Zoho")', self.contract)
        self.assertIn('(see "What changes with the Zoho integration")', self.contract)


if __name__ == "__main__":
    unittest.main()
