"""Manual erasure: the admin command (spec 2026-10-01-manual-erasure-design.md)."""

import importlib.util
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


class DeleteTests(unittest.TestCase):
    REASON = "asked in the chat; no open case"

    def setUp(self):
        self.desk = Desk()
        self.reference = self.desk.seed()

    def reviewed(self, name="Asha"):
        self.assertEqual(self.desk.run("show", self.reference, answers=[name])[0], 0)

    def delete(self, name="Asha", reason=None, typed=None):
        return self.desk.run("delete", self.reference,
                             answers=[name, self.REASON if reason is None else reason, typed or self.reference])

    def assert_nothing_deleted(self):
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.media.hidden, [])
        self.assertEqual(self.desk.store.erasure_log, [])
        self.assertEqual(self.desk.store.erasure_record(self.reference)["status"], "pending")

    def test_after_a_review_everything_goes_files_first(self):
        self.desk.media = FakeMedia(store=self.desk.store)
        self.reviewed()
        code, text = self.delete()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.desk.media.hidden, [PHOTO])
        self.assertEqual(self.desk.media.records_left_when_hiding, [2])  # files first, records after
        self.assertEqual(self.desk.store.conversations_of(ME), [])
        self.assertEqual(self.desk.store.conversations_of(THEM), ["theirs"])
        record = self.desk.store.erasure_record(self.reference)
        self.assertEqual((record["status"], record["by"]), ("done", "Asha"))
        self.assertNotIn("user_key", record)
        (audit,) = self.desk.store.erasure_log
        self.assertEqual(audit, erasure.audit_record(
            ME, "self-service request %s: %s" % (self.reference, self.REASON), "Asha", NOW,
            deleted=record["counts"], s3_objects=1))

    def test_a_held_request_can_still_be_deleted(self):
        self.desk.run("hold", self.reference, answers=["Asha", "safety case EM-00001 open"])
        self.reviewed()
        self.assertEqual(self.delete()[0], 0)

    def test_refused_without_a_review(self):
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Run show %s first" % self.reference, text)
        self.assert_nothing_deleted()

    def test_refused_when_someone_else_reviewed(self):
        self.reviewed("Ravi")
        code, text = self.delete("Asha")
        self.assertEqual(code, 1)
        self.assertIn("No review of %s by Asha" % self.reference, text)
        self.assert_nothing_deleted()

    def test_the_same_name_in_another_case_counts(self):
        self.reviewed("Asha")
        self.assertEqual(self.delete(" asha ")[0], 0)

    def test_refused_when_the_review_is_over_24_hours_old(self):
        self.reviewed()
        self.desk.now = NOW + timedelta(hours=24, minutes=1)
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("more than 24 hours old", text)
        self.assert_nothing_deleted()

    def test_refused_when_something_new_arrived_after_the_review(self):
        self.reviewed()
        mine = self.desk.store.get("mine")
        mine.turns = 2
        self.desk.store.record_turn(mine, inbound("one more thing", cid="mine"), reply("Thanks.", cid="mine"))
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("has changed since your review", text)
        self.assertEqual(self.desk.media.hidden, [])
        self.assertEqual(len(self.desk.store.transcript("mine")), 4)

    def test_refused_when_a_notice_arrived_after_the_review(self):
        # Ruling 21: a ticket closed in Zoho Desk after the review puts a
        # notice in the chat, which the review never saw.
        self.reviewed()
        self.desk.store.add_notice("mine", ME, "ticket_closed",
                                   "Your support request EM-00001 was closed by our support team.",
                                   "2026-10-03T08:30:00+00:00")
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("has changed since your review", text)
        self.assertEqual(self.desk.media.hidden, [])
        self.assertEqual(len(self.desk.store.notices_of("mine")), 1)

    def test_refused_when_the_wrong_reference_is_typed(self):
        self.reviewed()
        code, text = self.delete(typed="DEL-AAAAAA")
        self.assertEqual(code, 1)
        self.assertIn("Stopped. Nothing was deleted.", text)
        self.assert_nothing_deleted()

    def test_the_reference_in_lower_case_is_accepted(self):
        self.reviewed()
        code, text = self.desk.run("delete", self.reference.lower(),
                                   answers=["Asha", self.REASON, self.reference.lower()])
        self.assertEqual(code, 0, text)

    def test_refused_without_a_reason(self):
        self.reviewed()
        code, text = self.delete(reason=" ")
        self.assertEqual(code, 1)
        self.assertIn("A reason is needed", text)
        self.assert_nothing_deleted()

    def test_refused_once_the_customer_cancelled(self):
        self.reviewed()
        self.desk.store.cancel_erasure(ME, "2026-10-03T08:30:00+00:00")
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("is cancelled, not pending", text)
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.media.hidden, [])

    def test_a_cancel_while_the_person_types_stops_it(self):
        self.reviewed()
        answers = ["Asha", self.REASON]

        def ask(prompt):
            if prompt.startswith("Type "):
                self.desk.store.cancel_erasure(ME, "2026-10-03T09:00:00+00:00")
                return self.reference
            return answers.pop(0)

        lines = []
        code = run(["delete", self.reference], Admin(self.desk.store, self.desk.media, lambda: NOW, ask, lines.append))
        self.assertEqual(code, 1)
        self.assertIn("is cancelled, not pending", "\n".join(lines))
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.media.hidden, [])

    def test_a_file_that_cannot_be_hidden_keeps_every_record(self):
        self.desk.media = FakeMedia(fail_on=PHOTO)
        self.reviewed()
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Could not hide the files (StorageError). Nothing in the database was deleted.", text)
        record = self.desk.store.erasure_record(self.reference)
        self.assertEqual((record["status"], record["attempts"], record["last_error"]), ("pending", 1, "StorageError"))
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.store.erasure_log, [])
        self.desk.media.fail_on = None
        self.assertEqual(self.delete()[0], 0)

    def test_files_and_no_bucket_is_a_hide_failure(self):
        self.desk.media = None
        self.reviewed()
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Could not hide the files (StorageError)", text)
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])

    def test_a_person_with_no_files_needs_no_bucket(self):
        desk = Desk()
        reference = desk.quiet_person()
        desk.media = None
        self.assertEqual(desk.run("show", reference, answers=["Asha"])[0], 0)
        code, text = desk.run("delete", reference, answers=["Asha", self.REASON, reference])
        self.assertEqual(code, 0, text)
        self.assertEqual(desk.store.conversations_of(ME), [])


class CheckTests(unittest.TestCase):
    def test_quiet_below_25_days(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("check")
        self.assertEqual(code, 0, text)
        self.assertRegex(text, reference + r"\s+2 days\s+pending")
        self.assertIn("open erasure requests: 1", text)

    def test_red_at_25_days_held_included(self):
        desk = Desk()
        reference = desk.seed()
        desk.run("hold", reference, answers=["Asha", "safety case EM-00001 open"])
        desk.now = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
        code, text = desk.run("check")
        self.assertEqual(code, 1)
        self.assertRegex(text, reference + r"\s+25 days\s+held")
        self.assertIn("25 days old or more", text)
        for private in ("+919700000031", "safety case", "smoking", PHOTO, "chats="):
            self.assertNotIn(private, text)

    def test_red_when_the_store_cannot_be_read(self):
        desk = Desk()
        with mock.patch.object(desk.store, "pending_erasures", side_effect=StoreUnavailable("down")):
            self.assertEqual(desk.run("check"), (1, "Stopped on an error (StoreUnavailable)."))


class NothingDeletesOnItsOwnTests(unittest.TestCase):
    def test_the_nightly_job_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("emotorad_ai.erasure_job"))


class FinalReviewTests(unittest.TestCase):
    """The final review of the manual erasure branch (2026-10-01)."""

    REASON = "asked in the chat; no open case"

    def setUp(self):
        self.desk = Desk()
        self.reference = self.desk.seed()

    def delete(self):
        return self.desk.run("delete", self.reference, answers=["Asha", self.REASON, self.reference])

    def test_a_show_that_stops_part_way_is_not_a_review(self):
        with mock.patch.object(self.desk.store, "origins_of", side_effect=StoreUnavailable("down")):
            code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 1)
        self.assertNotIn("reviews", self.desk.store.erasure_record(self.reference))
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Run show %s first" % self.reference, text)
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])

    def test_a_failure_after_the_files_are_hidden_is_recorded_and_said(self):
        self.desk.run("show", self.reference, answers=["Asha"])
        with mock.patch.object(self.desk.store, "log_erasure", side_effect=StoreUnavailable("down")):
            code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Stopped at log_erasure (StoreUnavailable). All 1 files are hidden", text)
        record = self.desk.store.erasure_record(self.reference)
        self.assertEqual((record["status"], record["attempts"], record["last_error"]),
                         ("pending", 1, "log_erasure:StoreUnavailable"))
        # The person finishes it, and the audit record says it was a second go.
        self.desk.run("show", self.reference, answers=["Asha"])
        code, text = self.delete()
        self.assertEqual(code, 0, text)
        (audit,) = self.desk.store.erasure_log
        self.assertIn("finishing after 1 failed attempt(s), last: log_erasure:StoreUnavailable", audit["reason"])

    def test_ctrl_c_while_hiding_is_recorded(self):
        class Interrupted(FakeMedia):
            def hide(self, key):
                raise KeyboardInterrupt

        self.desk.media = Interrupted()
        self.desk.run("show", self.reference, answers=["Asha"])
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Could not hide the files (KeyboardInterrupt). Nothing in the database was deleted.", text)
        record = self.desk.store.erasure_record(self.reference)
        self.assertEqual((record["attempts"], record["last_error"]), (1, "KeyboardInterrupt"))
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])

    def test_records_that_expire_on_their_own_do_not_block_delete(self):
        self.desk.run("show", self.reference, answers=["Asha"])
        self.desk.store._states.pop("mine")  # MongoDB drops the working copy after 48 hours
        code, text = self.delete()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.desk.store.conversations_of(ME), [])
