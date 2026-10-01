"""Verify first, through the whole turn (spec 2026-09-30).

The model in these tests fails the test if it is called before the bike is
chosen: ScriptedClaude raises when it runs out of replies, and only the turns
after the choice are given one.
"""

import base64
import io
import unittest
from datetime import date

from PIL import Image

from emotorad_ai.agents import battery_support, late_warranty
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.triage import ASK_FOR_UNLISTED_BIKE
from emotorad_ai.verify_first import PHOTO_SAFETY

TODAY = date(2026, 9, 30)
RIDER = fixtures.PHONE_AMIIGO_TEST_RIDER  # two bikes
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus
NO_BIKE = fixtures.PHONE_WITH_NO_RECORD


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (60, 60, 60)).save(out, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


class Chat:
    """One website visitor, turn by turn."""

    def __init__(self, replies=(), verify_first=True, account_finder=fixtures.find_account_by_order_code, clock=None,
                 conversations=None):
        self.store = VerificationStore(clock=clock) if clock else VerificationStore()
        self.registry = build_registry(verification=self.store, today=TODAY, account_finder=account_finder)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = conversations if conversations is not None else InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            self_service_identity=True, phone_resolver=self.store.verified_phone,
            otp_verified_at=self.store.verified_on, verify_first=verify_first,
        )

    def say(self, text, photo=False, identity=None, **metadata):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=identity or Identity(strength=ANONYMOUS, em_aid="aid-1"),
            attachments=[Attachment(kind="image", url=jpeg())] if photo else [], entry_metadata=metadata,
        ))

    def code(self):
        return self.store.pending_code("c1")

    def state(self):
        return self.conversations.peek("c1")

    def verify(self, phone=RIDER, first="my battery isn't charging"):
        self.say(first)
        self.say(phone[3:])
        return self.say(self.code())


class WholeFlowTests(unittest.TestCase):
    def test_number_code_bike_then_the_issue(self):
        chat = Chat(replies=[say("Let's check the charger first. Is its light on?")])
        first = chat.say("my battery isn't charging")
        self.assertEqual(first.handled_by, "verify_first:ask_number")
        sent = chat.say("97000 00010")
        self.assertEqual(sent.handled_by, "verify_first:code_sent")
        self.assertIn("•••••••010", sent.text)
        listed = chat.say(chat.code())
        self.assertEqual(listed.handled_by, "verify_first:verified")
        self.assertIn("EMXP2026001234", listed.text)
        self.assertIn("DDL32023045678", listed.text)
        self.assertEqual(chat.state().phase, AWAITING_BIKE_SELECTION)
        self.assertEqual(chat.llm.requests, [], "no model before the bike is chosen")

        answer = chat.say("2")
        self.assertEqual(chat.state().selected_frame, "DDL32023045678")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertIn("charger", answer.text)

    def test_the_model_never_sees_the_number_or_the_code(self):
        chat = Chat(replies=[say("Let's check the charger first.")])
        chat.say("my battery isn't charging")
        chat.say("97000 00010")
        code = chat.code()
        chat.say(code)
        chat.say("2")
        seen = repr(chat.llm.requests[0]["messages"])
        for secret in ("97000 00010", "9700000010", code):
            self.assertNotIn(secret, seen)
        self.assertIn("[phone]", seen)
        self.assertIn("[code]", seen)

    def test_nor_does_the_transcript(self):
        chat = Chat()
        chat.say("hi")
        chat.say("97000 00010")
        code = chat.code()
        chat.say("the code is " + code)
        # The turns' text only: a timestamp's microseconds could hold six digits.
        said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
        self.assertNotIn("97000 00010", said)
        self.assertNotIn(code, said)

    def test_the_conversation_is_the_riders_once_verified(self):
        chat = Chat()
        chat.verify()
        self.assertEqual(chat.state().user_key, "PHONE#" + RIDER)

    def test_a_number_and_an_issue_in_one_message(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        reply = chat.say("battery dead, my number is 9700000010")
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        chat.say(chat.code())
        chat.say("the EMX Plus")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)

    def test_a_number_inside_hindi_text(self):
        chat = Chat()
        chat.say("namaste")
        self.assertEqual(chat.say("मेरा नंबर 9700000010 है").handled_by, "verify_first:code_sent")

    def test_the_code_tools_are_logged_so_a_conflict_retry_cannot_resend(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say(chat.code())
        tools = [e["tool"] for e in chat.log.events if e["event"] == "tool_call"]
        self.assertIn("request_identity_verification", tools)
        self.assertIn("verify_identity", tools)


class OneBikeTests(unittest.TestCase):
    def test_yes_selects_it(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        listed = chat.verify(phone=ONE_BIKE)
        self.assertIn("Is this the bike that needs help?", listed.text)
        self.assertIn("EMXP2025004417", listed.text)
        chat.say("yes")
        self.assertEqual(chat.state().selected_frame, "EMXP2025004417")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)

    def test_no_asks_for_the_frame_number_and_model(self):
        # The bike not listed (spec 2026-10-01, unlisted bike): asked for, no
        # route to late registration, and no model call for it.
        chat = Chat(replies=[say("Let's register it. What's the frame number?")])
        chat.verify(phone=ONE_BIKE)
        reply = chat.say("no")
        self.assertIn(ASK_FOR_UNLISTED_BIKE, reply.text)
        self.assertIsNone(chat.state().agent)
        self.assertEqual(len(chat.llm.requests), 0)


class NoBikeTests(unittest.TestCase):
    def test_registration_is_offered_then_the_agent_takes_over(self):
        chat = Chat(replies=[say("Great, what's the frame number?")])
        listed = chat.verify(phone=NO_BIKE)
        self.assertEqual(listed.handled_by, "verify_first:verified_no_bikes")
        self.assertIn("register it now", listed.text)
        chat.say("yes please")
        self.assertEqual(chat.state().agent, late_warranty.AGENT_NAME)


class CodeTests(unittest.TestCase):
    def wrong(self, chat):
        return "000000" if chat.code() != "000000" else "111111"

    def test_a_wrong_code_says_how_many_tries_are_left(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say(self.wrong(chat))
        self.assertEqual(reply.handled_by, "verify_first:wrong_code")
        self.assertIn("4 tries left", reply.text)

    def test_five_wrong_codes_hand_over(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        for left in ("4 tries", "3 tries", "2 tries", "1 try"):
            self.assertIn(left + " left", chat.say(self.wrong(chat)).text)
        locked = chat.say(self.wrong(chat))
        self.assertEqual(locked.handled_by, "verify_first:locked")
        self.assertTrue(locked.escalated)

    def test_resend(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say("I didn't get it, please resend")
        self.assertEqual(reply.handled_by, "verify_first:code_resent")
        self.assertIn("•••••••010", reply.text)
        self.assertEqual(chat.say(chat.code()).handled_by, "verify_first:verified")

    def test_a_different_number_while_waiting_for_the_code(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say("sorry, wrong number, it's 9876543210")
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        self.assertIn("•••••••210", reply.text)
        self.assertIn("EMXP2025004417", chat.say(chat.code()).text)

    def test_anything_else_asks_for_the_code_again(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        reply = chat.say("what?")
        self.assertEqual(reply.handled_by, "verify_first:ask_code")
        self.assertIn("•••••••010", reply.text)

    def test_an_expired_code_says_so(self):
        now = [0.0]
        chat = Chat(clock=lambda: now[0])
        chat.say("hi")
        chat.say("9700000010")
        code = chat.code()
        now[0] += 601
        self.assertEqual(chat.say(code).handled_by, "verify_first:code_expired")


class OrderNumberTests(unittest.TestCase):
    def test_an_unhelpful_reply_offers_the_order_number(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        reply = chat.say("I don't remember it")
        self.assertEqual(reply.handled_by, "verify_first:ask_number_again")
        self.assertIn("order or invoice number", reply.text)

    def test_an_order_number_sends_the_code_to_the_number_on_it(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        chat.say("I don't remember it")
        reply = chat.say("my order number is EMO-100234")
        self.assertEqual(reply.handled_by, "verify_first:order_code_sent")
        self.assertIn("•••••••010", reply.text)
        self.assertIn("DDL32023045678", chat.say(chat.code()).text)

    def test_an_unknown_order_number_says_so(self):
        chat = Chat()
        chat.say("hi")
        chat.say("I can't recall")
        self.assertEqual(chat.say("EMO-999999").handled_by, "verify_first:order_not_found")

    def test_without_the_order_lookup_it_offers_a_person(self):
        chat = Chat(account_finder=None)
        chat.say("hi")
        self.assertIn("talk to a person", chat.say("I don't remember it").text)

    def test_a_number_that_is_not_a_mobile_is_refused(self):
        chat = Chat()
        chat.say("hi")
        self.assertEqual(chat.say("1234567890").handled_by, "verify_first:invalid_number")

    def test_a_stale_code_while_waiting_for_the_number_asks_for_the_number(self):
        chat = Chat()
        chat.say("hi")
        self.assertEqual(chat.say("482913").handled_by, "verify_first:ask_number_again")


class StillFirstTests(unittest.TestCase):
    def test_a_safety_report_comes_before_verification(self):
        chat = Chat()
        self.assertEqual(chat.say("there is smoke coming out of my battery").handled_by, "guardrail:battery_safety")

    def test_asking_for_a_person_comes_before_verification(self):
        chat = Chat()
        self.assertEqual(chat.say("I want to talk to a person").handled_by, "guardrail:human_handoff")

    def test_a_photo_on_the_first_message_adds_the_safety_line(self):
        chat = Chat()
        reply = chat.say("is this normal?", photo=True)
        self.assertEqual(reply.handled_by, "verify_first:ask_number")
        self.assertIn(PHOTO_SAFETY, reply.text)

    def test_no_photo_no_safety_line(self):
        self.assertNotIn(PHOTO_SAFETY, Chat().say("hi").text)


class SkippedTests(unittest.TestCase):
    def test_a_signed_in_rider_is_not_asked(self):
        chat = Chat()
        reply = chat.say("my battery isn't charging", identity=Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1"))
        self.assertEqual(reply.handled_by, "triage")
        self.assertIn("EMXP2026001234", reply.text)

    def test_without_verify_first_nothing_changes(self):
        chat = Chat(verify_first=False)
        reply = chat.say("hi")
        self.assertFalse(reply.handled_by.startswith("verify_first"), reply.handled_by)


class PinnedAgentTests(unittest.TestCase):
    def test_a_pinned_agent_still_gets_the_bike_choice(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        pin = {"pinned_agent": battery_support.AGENT_NAME}
        chat.say("my battery isn't charging", **pin)
        chat.say("9700000010", **pin)
        self.assertEqual(chat.say(chat.code(), **pin).handled_by, "verify_first:verified")
        chat.say("2", **pin)
        self.assertEqual(chat.state().selected_frame, "DDL32023045678")


class ExpiredSessionTests(unittest.TestCase):
    def test_an_expired_session_starts_the_step_again(self):
        now = [0.0]
        chat = Chat(replies=[say("Is the charger light on?")], clock=lambda: now[0])
        chat.verify()
        chat.say("2")
        now[0] += 12 * 60 * 60 + 1
        self.assertEqual(chat.say("still not charging").handled_by, "verify_first:ask_number")


# -- the final review's findings (2026-09-30) ---------------------------------


class DifferentPersonAfterExpiryTests(unittest.TestCase):
    """Finding 1: after the first person's session expires, a different person
    who verifies in the same conversation starts a run of their own and
    inherits nothing of the first person's: not their memory, their records,
    their summary, or their words in the model's history."""

    def test_nothing_of_the_first_person_carries_over(self):
        now = [0.0]
        chat = Chat(replies=[say("Is the charger light on?"), say("Is the charger light on?")], clock=lambda: now[0])
        a_key, b_key = "PHONE#" + RIDER, "PHONE#" + ONE_BIKE
        chat.verify()
        chat.say("2")
        first_started = chat.state().started_at
        self.assertEqual(chat.state().user_key, a_key)

        now[0] += 12 * 60 * 60 + 1
        chat.say("hello again")
        chat.say(ONE_BIKE[3:])
        self.assertEqual(chat.say(chat.code()).handled_by, "verify_first:verified")
        state = chat.state()
        self.assertEqual(state.user_key, b_key)
        self.assertNotEqual(state.started_at, first_started)
        self.assertIsNone(state.coverage_result)

        # B's own run kept no topic from A, so the bike choice is followed by
        # the issue question; B's issue is what reaches the model.
        self.assertEqual(chat.say("yes").handled_by, "triage")
        chat.say("battery dead")
        self.assertEqual(len(chat.llm.requests), 2, "B's turn reached the model")
        seen = repr(chat.llm.requests[-1])
        self.assertNotIn("DDL32023045678", seen)
        self.assertNotIn("my battery isn't charging", seen)
        [a_summary] = chat.conversations.recent_summaries(a_key)
        self.assertEqual(a_summary.frame_number, "DDL32023045678")
        [b_summary] = chat.conversations.recent_summaries(b_key)
        self.assertEqual(b_summary.frame_number, "EMXP2025004417")

    def test_the_same_person_again_keeps_their_conversation(self):
        now = [0.0]
        chat = Chat(replies=[say("Is the charger light on?")], clock=lambda: now[0])
        chat.verify()
        chat.say("2")
        started = chat.state().started_at
        now[0] += 12 * 60 * 60 + 1
        chat.say("hello again")
        chat.say(RIDER[3:])
        chat.say(chat.code())
        self.assertEqual(chat.state().started_at, started)


class ConflictedStore(InMemoryConversationStore):
    """Hands out a fresh copy on every load, as a real database does, and
    refuses the next `conflicts` saves (as tests/test_runtime_persistence.py)."""

    def __init__(self):
        super().__init__()
        self.conflicts = 0

    def get(self, conversation_id):
        from emotorad_ai.conversation import ConversationState

        return ConversationState.from_json(super().get(conversation_id).to_json())

    def save(self, state):
        from emotorad_ai.conversation import ConversationConflict

        if self.conflicts:
            self.conflicts -= 1
            raise ConversationConflict("someone else saved")
        super().save(state)


class SaveConflictTests(unittest.TestCase):
    """Finding 2: a step turn that lost the save race keeps its progress."""

    def test_a_conflict_on_the_code_turn_keeps_the_step(self):
        store = ConflictedStore()
        chat = Chat(conversations=store)
        chat.say("hi")
        store.conflicts = 1
        self.assertEqual(chat.say("9700000010").handled_by, "verify_first:code_sent")
        self.assertEqual(chat.say(chat.code()).handled_by, "verify_first:verified")

    def test_a_conflict_on_the_verified_turn_keeps_the_bike_choice(self):
        store = ConflictedStore()
        chat = Chat(replies=[say("Is the charger light on?")], conversations=store)
        chat.say("my battery isn't charging")
        chat.say("9700000010")
        store.conflicts = 1
        self.assertEqual(chat.say(chat.code()).handled_by, "verify_first:verified")
        state = chat.state()
        self.assertEqual(state.phase, AWAITING_BIKE_SELECTION)
        self.assertIsNone(state.context_block)
        chat.say("2")
        self.assertEqual(chat.state().selected_frame, "DDL32023045678")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)


class LockoutAndCapTests(unittest.TestCase):
    """Finding 3: once locked, the step only repeats the handover; and no more
    than three codes are sent in one conversation."""

    def codes_sent(self, chat):
        return sum(1 for e in chat.log.events if e["event"] == "tool_call" and e["tool"] == "request_identity_verification")

    def test_after_the_lockout_nothing_more_is_sent_or_tried(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        wrong = "000000" if chat.code() != "000000" else "111111"
        for _ in range(5):
            chat.say(wrong)
        sent = self.codes_sent(chat)
        for text in ("ok", "resend", "9876543210"):
            reply = chat.say(text)
            self.assertEqual(reply.handled_by, "verify_first:locked", text)
            self.assertTrue(reply.escalated)
        self.assertEqual(self.codes_sent(chat), sent)

    def test_no_more_than_three_codes_in_a_conversation(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("resend")
        chat.say("resend")
        self.assertEqual(self.codes_sent(chat), 3)
        reply = chat.say("resend")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        self.assertTrue(reply.escalated)
        self.assertEqual(chat.say("9876543210").handled_by, "verify_first:too_many_codes")
        self.assertEqual(self.codes_sent(chat), 3)


DEVANAGARI = "०१२३४५६७८९"


def in_devanagari(digits):
    return "".join(DEVANAGARI[int(d)] for d in digits)


class DevanagariDigitsTests(unittest.TestCase):
    """Finding 6: digits typed in Devanagari are read as the same digits."""

    def test_a_number_and_a_code_in_devanagari(self):
        chat = Chat()
        chat.say("namaste")
        self.assertEqual(chat.say(in_devanagari("9700000010")).handled_by, "verify_first:code_sent")
        self.assertEqual(chat.say(in_devanagari(chat.code())).handled_by, "verify_first:verified")


class PhotoAtEveryStepTests(unittest.TestCase):
    """Finding 7: a photo at any step gets the stop-using line, not only the first."""

    def test_a_photo_while_waiting_for_the_number(self):
        chat = Chat()
        chat.say("hi")
        self.assertIn(PHOTO_SAFETY, chat.say("here", photo=True).text)

    def test_a_photo_while_waiting_for_the_code(self):
        chat = Chat()
        chat.say("hi")
        chat.say("9700000010")
        self.assertIn(PHOTO_SAFETY, chat.say("look", photo=True).text)


if __name__ == "__main__":
    unittest.main()
