"""Manual erasure: the admin command (spec 2026-10-01-manual-erasure-design.md)."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from emotorad_ai import erasure
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.erasure_admin import Admin, run
from emotorad_ai.storage.s3 import StorageError
from tests.store_contract import inbound, reply, summary

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
ME, THEM = "PHONE#+919700000031", "PHONE#+919812345678"
PHOTO = "customers/a/mine/images/upl_1.jpg"
OTP = {"method": "otp", "verified_at": "2026-10-01T08:58:00+00:00"}


class FakeMedia:
    def __init__(self, fail_on=None, store=None):
        self.hidden, self.fail_on, self.store = [], fail_on, store
        self.records_left_when_hiding = []

    def hide(self, key):
        if self.store is not None:
            self.records_left_when_hiding.append(len(self.store.transcript("mine")))
        if key == self.fail_on:
            raise StorageError("hide %r failed: AccessDenied" % key)
        self.hidden.append(key)

    def presign_get(self, key):
        return "https://media.example/%s?X-Amz-Expires=900" % key


class Desk:
    """One person at the terminal: what they type, and what they see."""

    def __init__(self):
        self.at = ["2026-10-01T08:44:00+00:00"]
        self.store = InMemoryConversationStore(clock=lambda: self.at[0])
        self.media = FakeMedia()
        self.now = NOW

    def seed(self, proof=OTP):
        """ME: one chat with a safety hand-over, a ticket, a photo and where it
        came from. THEM: one chat that must never show or go."""
        mine = self.store.get("mine")
        mine.user_key, mine.turns = ME, 1
        self.store.save(mine)
        self.store.record_turn(
            mine, inbound("my battery is smoking", cid="mine"),
            reply("Please stop using it and move it outside.", cid="mine", handled_by="guardrail:battery_safety",
                  ticket_id="EM-00001"),
            summary=summary("mine", user_key=ME, started_at="2026-10-01T08:44:00+00:00", title="Battery smoking",
                            outcome="escalated", ticket_id="EM-00001"),
        )
        self.store.record_media({"_id": PHOTO, "key": PHOTO, "conversation_id": "mine", "kind": "image",
                                 "mime_type": "image/jpeg", "size_bytes": 220168,
                                 "stored_at": "2026-10-01T08:44:00+00:00"})
        self.store.record_origin({"_id": "mine#1", "conversation_id": "mine", "started_at": "2026-10-01T08:44:00+00:00",
                                  "channel": "website_chat", "country": "IN", "region": "Maharashtra",
                                  "city": "Pune", "source": "ip", "user_key": ME})
        theirs = self.store.get("theirs")
        theirs.user_key, theirs.turns = THEM, 1
        self.store.save(theirs)
        self.store.record_turn(theirs, inbound("my brakes squeal", cid="theirs"),
                               reply("Let's check the pads.", cid="theirs"))
        return self.store.request_erasure(ME, "website_chat", "mine", "2026-10-01T09:00:00+00:00", proof=proof)

    def quiet_person(self):
        """ME with one ordinary chat, no file, no ticket, signed in to the app."""
        state = self.store.get("quiet")
        state.user_key, state.turns = ME, 1
        self.store.save(state)
        self.store.record_turn(state, inbound("hello", cid="quiet"), reply("Hi!", cid="quiet"))
        return self.store.request_erasure(ME, "amiigo_app", None, "2026-10-01T09:00:00+00:00",
                                          proof={"method": "app_sign_in"})

    def run(self, *argv, answers=()):
        queue, lines = list(answers), []
        admin = Admin(self.store, self.media, lambda: self.now, lambda prompt: queue.pop(0), lines.append)
        return run(list(argv), admin), "\n".join(lines)


class ListTests(unittest.TestCase):
    def test_references_ages_and_counts_only(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("list")
        self.assertEqual(code, 0)
        self.assertRegex(text, reference + r"\s+2 days\s+website_chat\s+pending\s+chats=1 turns=2 files=1")
        self.assertIn("open erasure requests: 1", text)
        for private in ("+919700000031", "smoking", "Pune", PHOTO):
            self.assertNotIn(private, text)


class ShowTests(unittest.TestCase):
    def setUp(self):
        self.desk = Desk()
        self.reference = self.desk.seed()

    def test_everything_that_will_go_and_the_review_is_recorded(self):
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        for expected in ("ticket EM-00001 raised in chat mine",
                         "safety hand-over in chat mine",
                         "phone: +919700000031",
                         "channel: website_chat",
                         "proof: OTP, verified at 2026-10-01T08:58:00+00:00",
                         "asked from conversation: mine",
                         "earlier requests: none",
                         "came from: ip, IN, Maharashtra, Pune (website_chat)",
                         "summary: Battery smoking",
                         "customer: my battery is smoking",
                         "bot (guardrail:battery_safety): Please stop using it and move it outside.",
                         "open for 15 minutes: https://media.example/" + PHOTO,
                         "transcript_turns: 2",
                         "s3_objects: 1",
                         "Review recorded: Asha"):
            self.assertIn(expected, text)
        self.assertNotIn("my brakes squeal", text)
        (review,) = self.desk.store.erasure_record(self.reference)["reviews"]
        self.assertEqual((review["by"], review["at"]), ("Asha", NOW.isoformat()))
        self.assertEqual((review["totals"]["transcript_turns"], review["totals"]["s3_objects"]), (2, 1))

    def test_no_flags_for_an_ordinary_old_chat(self):
        desk = Desk()
        reference = desk.quiet_person()
        code, text = desk.run("show", reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        self.assertIn("No flags", text)
        self.assertIn("proof: app sign-in", text)
        self.assertIn("No files", text)

    def test_a_chat_used_in_the_last_48_hours_is_flagged(self):
        self.desk.at[0] = "2026-10-03T08:00:00+00:00"
        mine = self.desk.store.get("mine")
        mine.turns = 2
        self.desk.store.record_turn(mine, inbound("is it safe now?", cid="mine"),
                                    reply("Please keep it outside.", cid="mine"))
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertIn("chat mine was used in the last 48 hours", text)

    def test_an_unrecorded_proof_and_an_earlier_request(self):
        desk = Desk()
        desk.store.request_erasure(ME, "amiigo_app", None, "2026-09-01T10:00:00+00:00")
        earlier = desk.store.cancel_erasure(ME, "2026-09-01T11:00:00+00:00")
        reference = desk.seed(proof=None)
        code, text = desk.run("show", reference, answers=["Asha"])
        self.assertIn("proof: not recorded", text)
        self.assertIn("earlier request %s: cancelled, asked 2026-09-01T10:00:00+00:00, "
                      "closed 2026-09-01T11:00:00+00:00" % earlier, text)

    def test_a_chat_whose_working_copy_expired_is_still_shown(self):
        # MongoDB drops the working copy 48 hours after the last message; the
        # transcript, summary and origin stay (Review Focus 3).
        self.desk.store._states.pop("mine")
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        self.assertIn("customer: my battery is smoking", text)
        self.assertIn("conversations: 0", text)

    def test_a_name_is_needed(self):
        code, text = self.desk.run("show", self.reference, answers=["  "])
        self.assertEqual(code, 1)
        self.assertIn("Your name is needed", text)
        self.assertNotIn("my battery is smoking", text)
        self.assertNotIn("reviews", self.desk.store.erasure_record(self.reference))

    def test_an_unknown_or_closed_request(self):
        self.assertEqual(self.desk.run("show", "DEL-ZZZZZZ", answers=["Asha"]), (1, "No erasure request DEL-ZZZZZZ."))
        self.desk.store.cancel_erasure(ME, "2026-10-02T10:00:00+00:00")
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 1)
        self.assertIn("is cancelled, not pending", text)


class HoldTests(unittest.TestCase):
    def test_held_stays_pending_with_the_note(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("hold", reference, answers=["Asha", "safety case EM-00001 open"])
        self.assertEqual(code, 0, text)
        record = desk.store.erasure_record(reference)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["held"], {"by": "Asha", "at": NOW.isoformat(), "note": "safety case EM-00001 open"})
        code, listed = desk.run("list")
        self.assertRegex(listed, reference + r"\s+2 days\s+website_chat\s+held")
        self.assertNotIn("safety case", listed)

    def test_a_note_is_needed(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("hold", reference, answers=["Asha", ""])
        self.assertEqual(code, 1)
        self.assertIn("A note is needed", text)
        self.assertNotIn("held", desk.store.erasure_record(reference))


class RunTests(unittest.TestCase):
    def test_usage(self):
        desk = Desk()
        for argv in ((), ("erase",), ("show",), ("list", "DEL-AAAAAA")):
            code, text = desk.run(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage:", text)

    def test_a_store_failure_stops_with_the_class_only(self):
        desk = Desk()
        with mock.patch.object(desk.store, "pending_erasures",
                               side_effect=StoreUnavailable("mongodb://user:secret@host")):
            self.assertEqual(desk.run("list"), (1, "Stopped on an error (StoreUnavailable)."))
