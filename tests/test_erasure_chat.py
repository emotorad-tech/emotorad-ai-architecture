"""Delete my data, in the chat (spec 2026-10-01)."""

import unittest
from unittest import mock

from emotorad_ai import erasure
from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.llm import say
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

APP_RIDER = Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1")


class AppChat(Chat):
    """A verified rider, as the Amiigo app sends them."""

    def ask(self, text):
        return self.say(text, identity=APP_RIDER)


def new_chat():
    return AppChat(replies=[say("Is the charger light on?")] * 6)


class VerifiedRiderTests(unittest.TestCase):
    def test_ask_confirm_and_get_a_reference(self):
        chat = new_chat()
        offered = chat.ask("delete my data")
        self.assertEqual(offered.handled_by, "erasure:confirm")
        self.assertIn(erasure.ERASURE_CONFIRM, offered.text)
        done = chat.ask("DELETE")
        self.assertEqual(done.handled_by, "erasure:requested")
        pending = chat.conversations.pending_erasure_of("PHONE#" + RIDER)
        self.assertEqual(done.text, erasure.ERASURE_REQUESTED.format(reference=pending["_id"]))
        events = [e for e in chat.log.events if e["event"] == "erasure_requested"]
        self.assertEqual(events[0]["reference"], pending["_id"])
        self.assertNotIn(RIDER, repr(events))

    def test_lower_case_delete_confirms(self):
        chat = new_chat()
        chat.ask("delete my data")
        self.assertEqual(chat.ask("delete").handled_by, "erasure:requested")

    def test_any_other_answer_keeps_the_data(self):
        chat = new_chat()
        chat.ask("delete my data")
        kept = chat.ask("no wait")
        self.assertEqual((kept.handled_by, kept.text), ("erasure:kept", erasure.ERASURE_KEPT))
        self.assertIsNone(chat.conversations.pending_erasure_of("PHONE#" + RIDER))
        self.assertNotEqual(chat.ask("DELETE").handled_by, "erasure:requested")

    def test_delete_without_being_asked_is_an_ordinary_message(self):
        chat = new_chat()
        self.assertFalse(chat.ask("DELETE").handled_by.startswith("erasure:"))

    def test_asking_again_gives_the_same_reference(self):
        chat = new_chat()
        chat.ask("delete my data")
        chat.ask("DELETE")
        reference = chat.conversations.pending_erasure_of("PHONE#" + RIDER)["_id"]
        again = chat.ask("please delete my account")
        self.assertEqual((again.handled_by, again.text),
                         ("erasure:existing", erasure.ERASURE_EXISTING.format(reference=reference)))

    def test_cancel_then_nothing_to_cancel(self):
        chat = new_chat()
        chat.ask("delete my data")
        chat.ask("DELETE")
        reference = chat.conversations.pending_erasure_of("PHONE#" + RIDER)["_id"]
        cancelled = chat.ask("cancel my deletion")
        self.assertEqual(cancelled.text, erasure.ERASURE_CANCELLED.format(reference=reference))
        self.assertEqual(chat.conversations.erasure_record(reference)["status"], "cancelled")
        self.assertEqual(chat.ask("cancel my deletion").text, erasure.ERASURE_NOTHING_TO_CANCEL)

    def test_a_store_failure_asks_the_rider_to_try_again(self):
        chat = new_chat()
        chat.ask("delete my data")
        with mock.patch.object(chat.conversations, "request_erasure", side_effect=StoreUnavailable("down")):
            failed = chat.ask("DELETE")
        self.assertEqual((failed.handled_by, failed.text), ("erasure:failed", erasure.ERASURE_FAILED))
        (event,) = [e for e in chat.log.events if e["event"] == "erasure_request_failed"]
        self.assertEqual(event["error"], "StoreUnavailable")

    def test_safety_and_handoff_come_first(self):
        chat = new_chat()
        self.assertEqual(chat.ask("my battery is smoking, delete my data").handled_by, "guardrail:battery_safety")
        chat = new_chat()
        self.assertEqual(chat.ask("talk to a person and delete my data").handled_by, "guardrail:human_handoff")


class WebsiteVisitorTests(unittest.TestCase):
    def test_verify_then_the_confirmation_not_the_bike_list(self):
        chat = new_chat()
        self.assertEqual(chat.say("delete my data").handled_by, "verify_first:ask_number")
        chat.say(ONE_BIKE[3:])
        verified = chat.say(chat.code())
        self.assertTrue(verified.handled_by.startswith("verify_first:verified"), verified.handled_by)
        self.assertIn("Thanks, that's confirmed.", verified.text)
        self.assertIn(erasure.ERASURE_CONFIRM, verified.text)
        self.assertNotIn("I found", verified.text)
        self.assertEqual(chat.say("DELETE").handled_by, "erasure:requested")
        self.assertIsNotNone(chat.conversations.pending_erasure_of("PHONE#" + ONE_BIKE))

    def test_a_deletion_phrase_while_a_code_is_awaited(self):
        chat = new_chat()
        chat.say("my battery isn't charging")
        chat.say(ONE_BIKE[3:])
        self.assertEqual(chat.say("actually, delete my data").handled_by, "verify_first:ask_code")
        verified = chat.say(chat.code())
        self.assertIn(erasure.ERASURE_CONFIRM, verified.text)

    def test_cancel_after_verifying(self):
        chat = new_chat()
        reference = chat.conversations.request_erasure("PHONE#" + ONE_BIKE, "amiigo_app", None,
                                                       "2026-10-01T09:00:00+00:00")
        chat.say("cancel my deletion")
        chat.say(ONE_BIKE[3:])
        verified = chat.say(chat.code())
        self.assertIn(erasure.ERASURE_CANCELLED.format(reference=reference), verified.text)
        self.assertEqual(chat.conversations.erasure_record(reference)["status"], "cancelled")


class ProofTests(unittest.TestCase):
    """The final review (2026-10-01): only this turn's proven identity may ask
    or cancel, and DELETE confirms only as the very next message."""

    def test_a_lapsed_verification_cannot_ask_for_the_first_persons_deletion(self):
        now = [0.0]
        chat = AppChat(replies=[say("Is the charger light on?")] * 6, clock=lambda: now[0])
        chat.verify(ONE_BIKE)
        now[0] += 12 * 60 * 60 + 1  # the verification lapses; the saved owner does not
        asked = chat.say("delete my data")
        self.assertEqual(asked.handled_by, "verify_first:ask_number")
        self.assertIsNone(chat.conversations.pending_erasure_of("PHONE#" + ONE_BIKE))

    def test_a_lapsed_verification_cannot_confirm_either(self):
        now = [0.0]
        chat = AppChat(replies=[say("Is the charger light on?")] * 6, clock=lambda: now[0])
        chat.verify(ONE_BIKE)
        self.assertEqual(chat.say("delete my data").handled_by, "erasure:confirm")
        now[0] += 12 * 60 * 60 + 1
        self.assertNotEqual(chat.say("DELETE").handled_by, "erasure:requested")
        self.assertIsNone(chat.conversations.pending_erasure_of("PHONE#" + ONE_BIKE))

    def test_delete_confirms_only_as_the_next_message(self):
        chat = new_chat()
        chat.ask("delete my data")
        self.assertEqual(chat.ask("talk to a person").handled_by, "guardrail:human_handoff")
        self.assertNotEqual(chat.ask("delete").handled_by, "erasure:requested")
        self.assertIsNone(chat.conversations.pending_erasure_of("PHONE#" + RIDER))
