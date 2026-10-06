"""A verified chat stays verified across a server restart (6 October 2026).

On staging a customer who had proved their number in the web chat was asked
for it again mid-conversation, and then for their bike again. The
conversation was in MongoDB, but the proof lived only in the process's
VerificationStore, so every deploy made every chat in progress anonymous.
Proved numbers are now saved (`verification_sessions`, twelve hours) and taken
back after a restart, but only for the person the saved conversation says
proved its number (`VerificationStore.restore`). Codes in transit stay in
memory.
"""

import importlib.util
import io
import logging
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import mongomock
from pymongo.errors import ServerSelectionTimeoutError

from emotorad_ai.agents import battery_support
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.conversation import ConversationState, InMemoryConversationStore, StoreUnavailable
from emotorad_ai.llm import say
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import (
    INDEXES,
    VERIFICATION_SESSIONS,
    MongoConversationStore,
    MongoVerifiedSessions,
    ensure_indexes,
)
from emotorad_ai.tools.verification import (
    VERIFIED_TTL_SECONDS,
    InMemoryVerifiedSessions,
    VerificationStore,
    apply_verified_identity,
    proved_owner,
)
from emotorad_ai.wiring import SESSIONS_INDEX_UNREADABLE, SESSIONS_TTL_MISSING, build_stores
from tests.clock import mongomock_clock_at
from tests.test_api_health import fresh_api
from tests.test_verify_first import ONE_BIKE, Chat

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
PHONE = "+919700000033"
OTHER = "+919700000044"
# Who the saved conversation says proved its number (proved_owner).
OWNER = "PHONE#" + PHONE
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# mongomock expires TTL documents against the real clock; these tests run on NOW.
_MONGOMOCK_CLOCK = mongomock_clock_at(NOW)


def setUpModule():
    _MONGOMOCK_CLOCK.start()


def tearDownModule():
    _MONGOMOCK_CLOCK.stop()


class Clocks:
    """A monotonic clock and a wall clock, wound by hand together or apart."""

    def __init__(self):
        self.mono = 1000.0
        self.wall = NOW

    def advance(self, seconds, wall=True, mono=True):
        if mono:
            self.mono += seconds
        if wall:
            self.wall += timedelta(seconds=seconds)

    def store(self, sessions, **kw):
        return VerificationStore(clock=lambda: self.mono, wall_clock=lambda: self.wall.isoformat(),
                                 sessions=sessions, **kw)

    def sessions(self):
        return InMemoryVerifiedSessions(now=lambda: self.wall)


def verify(store, cid="c1", phone=PHONE):
    store.issue(cid, phone, "123456")
    assert store.check(cid, "123456")


class Broken:
    """A backend whose every call fails, as MongoDB does when it is unreachable."""

    def __init__(self, fail=("save", "get", "delete")):
        self.fail = set(fail)
        self.inner = InMemoryVerifiedSessions(now=lambda: NOW)

    def _call(self, name, *args):
        if name in self.fail:
            raise StoreUnavailable("MongoDB %s failed (ServerSelectionTimeoutError)" % name)
        return getattr(self.inner, name)(*args)

    def save(self, *args):
        return self._call("save", *args)

    def get(self, *args):
        return self._call("get", *args)

    def delete(self, *args):
        return self._call("delete", *args)


class Counting(InMemoryVerifiedSessions):
    """Real saved sessions that count their reads."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gets = 0

    def get(self, conversation_id):
        self.gets += 1
        return super().get(conversation_id)


class DeleteFails(InMemoryVerifiedSessions):
    """Saves and reads, but every delete fails, as on a MongoDB blip."""

    def delete(self, conversation_id):
        raise StoreUnavailable("MongoDB delete_one failed (AutoReconnect)")


class BlockingDelete(InMemoryVerifiedSessions):
    """A delete that, once `block` is set, waits for `release`: one MongoDB
    round trip, held open so another request can run inside it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.block = False
        self.deleting = threading.Event()
        self.release = threading.Event()

    def delete(self, conversation_id):
        if self.block:
            self.deleting.set()
            assert self.release.wait(5), "the test never released the delete"
        super().delete(conversation_id)


# -- VerificationStore over a saved-session backend ---------------------------


class SurvivesARestartTests(unittest.TestCase):
    def setUp(self):
        self.clocks = Clocks()
        self.sessions = self.clocks.sessions()
        self.first = self.clocks.store(self.sessions)

    def restarted(self):
        """A new process: nothing in memory, the same saved sessions."""
        return self.clocks.store(self.sessions)

    def test_a_restarted_store_takes_the_proof_back_for_its_owner(self):
        verify(self.first)
        second = self.restarted()
        # Nothing in memory until the saved conversation's owner is known.
        self.assertIsNone(second.verified_phone("c1"))
        self.assertEqual(second.restore("c1", OWNER), PHONE)
        self.assertEqual(second.verified_phone("c1"), PHONE)
        self.assertEqual(second.verified_on("c1"), NOW.isoformat())
        self.assertIsNone(second.restore("c2", OWNER))
        self.assertIsNone(second.verified_phone("c2"))

    def test_without_shared_sessions_a_new_store_knows_nothing(self):
        verify(self.first)
        store = VerificationStore()
        self.assertIsNone(store.restore("c1", OWNER))
        self.assertIsNone(store.verified_phone("c1"))

    def test_the_saved_session_lasts_twelve_hours_and_no_longer(self):
        verify(self.first)
        self.clocks.advance(VERIFIED_TTL_SECONDS - 1)
        second = self.restarted()
        self.assertEqual(second.restore("c1", OWNER), PHONE)
        self.clocks.advance(2)
        # The proof taken back lapses when the first one would have.
        self.assertIsNone(second.verified_phone("c1"))
        self.assertIsNone(second.verified_on("c1"))
        third = self.restarted()
        self.assertIsNone(third.restore("c1", OWNER))
        self.assertIsNone(third.verified_on("c1"))

    def test_a_proof_taken_back_halfway_keeps_its_first_deadline(self):
        verify(self.first)
        self.clocks.advance(6 * 60 * 60)
        second = self.restarted()
        self.assertEqual(second.restore("c1", OWNER), PHONE)
        self.clocks.advance(6 * 60 * 60 - 1)
        self.assertEqual(second.verified_phone("c1"), PHONE)
        self.clocks.advance(2)
        self.assertIsNone(second.verified_phone("c1"))

    def test_a_proof_taken_back_has_no_code_to_type(self):
        verify(self.first)
        second = self.restarted()
        second.restore("c1", OWNER)
        for typed in ("", " ", "123456"):
            self.assertFalse(second.check("c1", typed), repr(typed))
        self.assertEqual(second.verified_phone("c1"), PHONE)
        self.assertIsNone(second.pending_code("c1"))

    def test_a_new_code_ends_the_saved_session(self):
        verify(self.first)
        self.first.issue("c1", OTHER, "654321")
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.sessions.get("c1"))
        self.assertIsNone(self.restarted().restore("c1", OWNER))

    def test_a_new_code_after_a_restart_ends_the_saved_session(self):
        verify(self.first)
        second = self.restarted()
        self.assertEqual(second.restore("c1", OWNER), PHONE)
        second.issue("c1", OTHER, "654321")
        self.assertIsNone(second.verified_phone("c1"))
        self.assertIsNone(self.restarted().restore("c1", OWNER))

    def test_reset_ends_the_saved_session(self):
        verify(self.first)
        self.first.reset("c1")
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.restarted().restore("c1", OWNER))

    def test_reset_after_a_restart_ends_the_saved_session(self):
        # The runtime's "wrong number" after a deploy: the proof is taken back
        # from the saved session, then reset must remove it there.
        verify(self.first)
        second = self.restarted()
        self.assertEqual(second.restore("c1", OWNER), PHONE)
        second.reset("c1")
        self.assertIsNone(second.verified_phone("c1"))
        self.assertIsNone(second.restore("c1", OWNER))
        self.assertIsNone(self.restarted().restore("c1", OWNER))

    def test_cancelling_a_code_leaves_a_proved_number_alone_as_in_memory(self):
        verify(self.first)
        self.first.cancel_code("c1")
        self.assertEqual(self.first.verified_phone("c1"), PHONE)
        self.assertEqual(self.restarted().restore("c1", OWNER), PHONE)

    def test_a_session_memory_has_let_lapse_is_never_read_back(self):
        # The monotonic clock says twelve hours have gone; the wall clock was
        # set back meanwhile and says they have not. Memory decides, and once
        # it sweeps the entry the saved session goes with it.
        verify(self.first)
        self.clocks.advance(VERIFIED_TTL_SECONDS + 1, wall=False)
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.first.restore("c1", OWNER))
        self.first.issue("c2", OTHER, "111111")  # the sweep runs on issue
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.restarted().restore("c1", OWNER))

    def test_a_code_in_transit_is_not_saved(self):
        self.first.issue("c1", PHONE, "123456")
        second = self.restarted()
        self.assertIsNone(second.restore("c1", OWNER))
        self.assertIsNone(second.pending_code("c1"))
        self.assertFalse(second.check("c1", "123456"))
        self.assertIsNone(self.sessions.get("c1"))

    def test_no_backend_is_memory_only_as_before(self):
        clocks = Clocks()
        store = clocks.store(None)
        verify(store)
        self.assertEqual(store.verified_phone("c1"), PHONE)
        clocks.advance(VERIFIED_TTL_SECONDS + 1)
        self.assertIsNone(store.verified_phone("c1"))


class StateAgreementTests(unittest.TestCase):
    """A saved session is honoured only when the saved conversation agrees
    with it (the review of the fix, minor 3): on a shared browser, a proof
    that this process ended but MongoDB kept must not bring the first
    person's number, bikes and coverage back after a restart."""

    def setUp(self):
        self.clocks = Clocks()
        self.sessions = Counting(now=lambda: self.clocks.wall)
        verify(self.clocks.store(self.sessions))

    def test_only_for_the_owner_the_saved_conversation_names(self):
        store = self.clocks.store(self.sessions)
        for owner in (None, "", "PHONE#" + OTHER, "AID#aid-1", PHONE):
            self.assertIsNone(store.restore("c1", owner), owner)
            self.assertIsNone(store.verified_phone("c1"), owner)
        self.assertEqual(store.restore("c1", OWNER), PHONE)

    def test_no_owner_reads_nothing(self):
        store = self.clocks.store(self.sessions)
        store.restore("c1", None)
        store.restore("c1", "AID#aid-1")
        self.assertEqual(self.sessions.gets, 0)

    def test_memory_answers_before_the_saved_session(self):
        store = self.clocks.store(self.sessions)
        verify(store, phone=OTHER)
        reads = self.sessions.gets
        self.assertEqual(store.restore("c1", OWNER), OTHER)
        self.assertEqual(self.sessions.gets, reads, "memory held a proof: nothing read")

    def test_the_owner_is_whoever_proved_it_and_finished_verifying(self):
        self.assertEqual(proved_owner(ConversationState(conversation_id="c1", user_key=OWNER)), OWNER)
        for step in ("number", "code"):
            state = ConversationState(conversation_id="c1", user_key=OWNER, verify_step=step)
            self.assertIsNone(proved_owner(state), step)
        self.assertIsNone(proved_owner(ConversationState(conversation_id="c1")))
        self.assertIsNone(proved_owner(None))

    def test_a_saved_proof_not_taken_back_is_never_stamped_on_a_message(self):
        # api.post_message stamps the proof before the turn loads the saved
        # conversation: it must not stamp one the conversation may disown.
        store = self.clocks.store(self.sessions)
        message = InboundMessage(conversation_id="c1", persona="customer", channel="website_chat",
                                 message_text="hi", identity=Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertIsNone(apply_verified_identity(message, store).identity.phone)
        store.restore("c1", OWNER)
        self.assertEqual(apply_verified_identity(message, store).identity.phone, PHONE)


class ConcurrencyTests(unittest.TestCase):
    """Two requests on one conversation (a double send next to "wrong
    number"): while the saved session is being deleted, the other request
    must not see the proof memory has just ended (the review, minor 2)."""

    def setUp(self):
        self.clocks = Clocks()
        self.sessions = BlockingDelete(now=lambda: self.clocks.wall)
        verify(self.clocks.store(self.sessions))
        self.store = self.clocks.store(self.sessions)  # restarted
        self.assertEqual(self.store.restore("c1", OWNER), PHONE)

    def ending(self, end):
        self.sessions.block = True
        thread = threading.Thread(target=end)
        thread.start()
        self.assertTrue(self.sessions.deleting.wait(5))
        return thread

    def assert_never_verified_while_deleting(self, end):
        thread = self.ending(end)
        restored = []
        reader = threading.Thread(target=lambda: restored.append(self.store.restore("c1", OWNER)))
        try:
            self.assertIsNone(self.store.verified_phone("c1"))
            self.assertIsNone(self.store.verified_on("c1"))
            reader.start()
            self.assertIsNone(self.store.verified_phone("c1"))
        finally:
            self.sessions.release.set()
            thread.join(5)
            reader.join(5)
        self.assertEqual(restored, [None])
        self.assertIsNone(self.store.verified_phone("c1"))
        self.assertIsNone(self.sessions.get("c1"))

    def test_while_reset_deletes(self):
        self.assert_never_verified_while_deleting(lambda: self.store.reset("c1"))

    def test_while_a_new_code_deletes(self):
        self.assert_never_verified_while_deleting(lambda: self.store.issue("c1", OTHER, "654321"))

    def test_a_reset_during_a_read_is_not_undone_by_it(self):
        # The other order: the saved session is read first, held open, and a
        # "wrong number" arrives meanwhile. The proof must not land in memory
        # after the reset has finished.
        class SlowRead(InMemoryVerifiedSessions):
            reading, release = threading.Event(), threading.Event()

            def get(self, conversation_id):
                found = super().get(conversation_id)
                self.reading.set()
                assert self.release.wait(5), "the test never released the read"
                return found

        sessions = SlowRead(now=lambda: self.clocks.wall)
        verify(self.clocks.store(sessions))
        store = self.clocks.store(sessions)  # restarted
        reader = threading.Thread(target=store.restore, args=("c1", OWNER))
        reader.start()
        self.assertTrue(SlowRead.reading.wait(5))
        resetting = threading.Thread(target=store.reset, args=("c1",))
        resetting.start()
        resetting.join(0.2)
        SlowRead.release.set()
        reader.join(5)
        resetting.join(5)
        self.assertIsNone(store.verified_phone("c1"))
        self.assertIsNone(store.restore("c1", OWNER))
        self.assertIsNone(sessions.get("c1"))


class BackendFailureTests(unittest.TestCase):
    def setUp(self):
        self.log = EventLog(path=None)

    def failures(self):
        return [e for e in self.log.events if e["event"] == "verification_session_unavailable"]

    def test_a_failing_read_is_not_verified_and_never_raises(self):
        store = VerificationStore(sessions=Broken(), log=self.log)
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING") as logged:
            self.assertIsNone(store.restore("c1", OWNER))
            self.assertIsNone(store.verified_phone("c1"))
            self.assertIsNone(store.verified_on("c1"))
        [first] = self.failures()
        self.assertEqual((first["operation"], first["error"]), ("get", "StoreUnavailable"))
        self.assertIn("verification_session_unavailable", "\n".join(logged.output))

    def test_any_backend_error_is_not_verified(self):
        class Raises:
            def get(self, cid):
                raise ServerSelectionTimeoutError("no servers found")

        store = VerificationStore(sessions=Raises())
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING"):
            self.assertIsNone(store.restore("c1", OWNER))
        self.assertIsNone(store.verified_phone("c1"))

    def test_memory_answers_first_so_a_failing_read_does_not_matter_after_a_code(self):
        store = VerificationStore(sessions=Broken(fail=("get",)), log=self.log)
        verify(store)
        self.assertEqual(store.verified_phone("c1"), PHONE)
        self.assertEqual(store.restore("c1", OWNER), PHONE)
        self.assertEqual(self.failures(), [])

    def test_a_failing_save_still_verifies_in_this_process_and_never_logs_the_phone(self):
        store = VerificationStore(sessions=Broken(fail=("save",)), log=self.log)
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING") as logged:
            verify(store)
        self.assertEqual(store.verified_phone("c1"), PHONE)
        [failure] = self.failures()
        self.assertEqual((failure["operation"], failure["error"]), ("save", "StoreUnavailable"))
        for digits in ("9700000033", "033"):
            self.assertNotIn(digits, repr(self.log.events))
            self.assertNotIn(digits, "\n".join(logged.output))

    def test_a_failed_delete_never_lets_the_old_session_back_in_this_process(self):
        backend = Broken(fail=("delete",))
        store = VerificationStore(sessions=backend, log=self.log)
        verify(store)
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING"):
            store.reset("c1")
        self.assertIsNotNone(backend.inner.get("c1"), "the delete really failed")
        self.assertIsNone(store.verified_phone("c1"))
        self.assertIsNone(store.restore("c1", OWNER))
        self.assertEqual(self.failures()[0]["operation"], "delete")

    def test_a_new_proof_after_a_failed_delete_is_used_again(self):
        backend = Broken(fail=("delete",))
        store = VerificationStore(sessions=backend, log=self.log)
        verify(store)
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING"):
            store.reset("c1")
            verify(store, phone=OTHER)
        self.assertEqual(store.verified_phone("c1"), OTHER)
        self.assertEqual(VerificationStore(sessions=backend).restore("c1", "PHONE#" + OTHER), OTHER)


class InMemorySessionsTests(unittest.TestCase):
    def test_save_get_delete_and_expiry(self):
        now = [NOW]
        sessions = InMemoryVerifiedSessions(now=lambda: now[0])
        sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(sessions.get("c1"), (PHONE, NOW.isoformat()))
        now[0] = NOW + timedelta(hours=12)
        self.assertIsNone(sessions.get("c1"))
        sessions.save("c2", PHONE, NOW.isoformat(), now[0] + timedelta(hours=12))
        sessions.delete("c2")
        sessions.delete("never-saved")
        self.assertIsNone(sessions.get("c2"))


# -- MongoDB --------------------------------------------------------------------


class MongoSessionsTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.now = [NOW]
        self.sessions = MongoVerifiedSessions(self.db, now=lambda: self.now[0])

    def test_save_get_delete(self):
        self.sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(self.sessions.get("c1"), (PHONE, NOW.isoformat()))
        doc = self.db[VERIFICATION_SESSIONS].find_one({"_id": "c1"})
        self.assertEqual((doc["phone"], doc["user_key"], doc["verified_on"]),
                         (PHONE, "PHONE#" + PHONE, NOW.isoformat()))
        self.assertEqual(doc["expires_at"].replace(tzinfo=timezone.utc), NOW + timedelta(hours=12))
        self.sessions.delete("c1")
        self.sessions.delete("never-saved")
        self.assertIsNone(self.sessions.get("c1"))

    def test_saving_again_replaces_the_session(self):
        self.sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.sessions.save("c1", OTHER, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(self.sessions.get("c1")[0], OTHER)
        self.assertEqual(self.db[VERIFICATION_SESSIONS].count_documents({}), 1)

    def test_an_expired_document_is_absent_before_the_ttl_monitor_removes_it(self):
        self.sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.now[0] = NOW + timedelta(hours=12)
        # Still stored (mongomock's TTL clock is pinned to NOW), so the store's own check is what hides it.
        self.assertIsNotNone(self.db[VERIFICATION_SESSIONS].find_one({"_id": "c1"}))
        self.assertIsNone(self.sessions.get("c1"))

    def test_the_ttl_index_and_the_person_index_are_there_after_setup(self):
        info = self.db[VERIFICATION_SESSIONS].index_information()
        self.assertEqual(info["expires_at_ttl"]["expireAfterSeconds"], 0)
        self.assertEqual(info["expires_at_ttl"]["key"], [("expires_at", 1)])
        self.assertIn("user_key", info)
        self.assertIn(VERIFICATION_SESSIONS, INDEXES)

    def test_an_unreachable_cluster_is_store_unavailable(self):
        class Unreachable:
            def find_one(self, *a, **k):
                raise ServerSelectionTimeoutError("no servers found")

        with self.assertRaises(StoreUnavailable):
            MongoVerifiedSessions({VERIFICATION_SESSIONS: Unreachable()}).get("c1")

    def test_a_verification_store_on_mongodb_survives_a_restart(self):
        clocks = Clocks()
        sessions = MongoVerifiedSessions(self.db, now=lambda: clocks.wall)
        verify(clocks.store(sessions))
        restarted = clocks.store(MongoVerifiedSessions(self.db, now=lambda: clocks.wall))
        self.assertEqual(restarted.restore("c1", OWNER), PHONE)


class TtlIndexTests(unittest.TestCase):
    """Only mongo_setup.py makes the TTL index, and a deploy never runs it. A
    collection created by the first write has none, and every proved number
    would stay for ever (the review of the fix, important 1)."""

    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]

    def test_there_after_setup(self):
        ensure_indexes(self.db)
        self.assertTrue(MongoVerifiedSessions(self.db).has_ttl_index())

    def test_not_there_on_a_collection_the_first_write_made(self):
        MongoVerifiedSessions(self.db).save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertFalse(MongoVerifiedSessions(self.db).has_ttl_index())

    def test_an_index_that_does_not_expire_on_the_date_does_not_count(self):
        for options in ({}, {"expireAfterSeconds": 3600}):
            db = mongomock.MongoClient()["emotorad_ai"]
            db[VERIFICATION_SESSIONS].create_index([("expires_at", 1)], name="expires_at_ttl", **options)
            self.assertFalse(MongoVerifiedSessions(db).has_ttl_index(), options)

    def test_an_unreadable_index_list_is_store_unavailable(self):
        class Unreachable:
            def index_information(self):
                raise ServerSelectionTimeoutError("no servers found")

        with self.assertRaises(StoreUnavailable):
            MongoVerifiedSessions({VERIFICATION_SESSIONS: Unreachable()}).has_ttl_index()


class ErasureTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.sessions = MongoVerifiedSessions(self.db, now=lambda: NOW)
        self.store = MongoConversationStore(self.db, now=lambda: NOW)

    def test_delete_person_removes_their_sessions(self):
        self.sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.sessions.save("c2", OTHER, NOW.isoformat(), NOW + timedelta(hours=12))
        key = "PHONE#" + PHONE
        self.assertEqual(self.store.conversations_of(key), ["c1"])
        self.assertEqual(self.store.delete_person(key, dry_run=True)[VERIFICATION_SESSIONS], 1)
        self.assertIsNotNone(self.sessions.get("c1"))
        self.assertEqual(self.store.delete_person(key)[VERIFICATION_SESSIONS], 1)
        self.assertIsNone(self.sessions.get("c1"))
        self.assertIsNotNone(self.sessions.get("c2"), "only theirs")

    def test_a_session_in_one_of_their_conversations_goes_whoever_proved_it(self):
        # The conversation is theirs by its transcript; the session in it was
        # proved by another number later. The whole conversation goes.
        self.db["transcript_turns"].insert_one({"_id": "c1#00001", "conversation_id": "c1", "n": 1,
                                                "user_key": "PHONE#" + PHONE})
        self.sessions.save("c1", OTHER, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(self.store.delete_person("PHONE#" + PHONE)[VERIFICATION_SESSIONS], 1)
        self.assertIsNone(self.sessions.get("c1"))

    def test_delete_conversation_removes_its_session(self):
        self.sessions.save("anon", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(self.store.delete_conversation("anon")[VERIFICATION_SESSIONS], 1)
        self.assertIsNone(self.sessions.get("anon"))


class ErasureAdminTests(unittest.TestCase):
    """erasure_admin reviews and deletes through delete_person, so a session
    is counted and goes with the rest; it expires on its own, so it is not
    part of what a review is compared on."""

    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.store = MongoConversationStore(self.db, now=lambda: NOW)
        self.key = "PHONE#" + PHONE
        self.db["transcript_turns"].insert_one({"_id": "c1#00001", "conversation_id": "c1", "n": 1,
                                                "user_key": self.key, "role": "customer", "text": "hi",
                                                "at": "2026-10-01T08:44:00+00:00"})
        MongoVerifiedSessions(self.db, now=lambda: NOW).save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.reference = self.store.request_erasure(self.key, "website_chat", "c1", "2026-10-01T09:00:00+00:00")

    def run_admin(self, *argv, answers=()):
        from emotorad_ai.erasure_admin import Admin, run

        queue, lines = list(answers), []
        admin = Admin(self.store, None, lambda: NOW, lambda prompt: queue.pop(0), lines.append)
        return run(list(argv), admin), "\n".join(lines)

    def delete(self):
        return self.run_admin("delete", self.reference, answers=["Asha", "customer asked", self.reference])

    def test_show_counts_the_session_and_delete_removes_it(self):
        code, text = self.run_admin("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        self.assertIn("verification_sessions: 1", text)
        code, text = self.delete()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.db[VERIFICATION_SESSIONS].count_documents({}), 0)

    def test_a_session_that_lapses_after_the_review_does_not_block_delete(self):
        self.run_admin("show", self.reference, answers=["Asha"])
        self.db[VERIFICATION_SESSIONS].delete_many({})  # the TTL monitor, twelve hours on
        code, text = self.delete()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.db["transcript_turns"].count_documents({}), 0)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptTests(unittest.TestCase):
    def test_delete_person_script_removes_the_session(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        MongoVerifiedSessions(db, now=lambda: NOW).save("c1", "+919876543210", NOW.isoformat(),
                                                        NOW + timedelta(hours=12))
        module = load_script("delete_person")
        with mock.patch.object(module, "connect", lambda db_name: client[db_name]):
            for argv, remaining in ((["--phone", "98765 43210"], 1),
                                    (["--phone", "98765 43210", "--yes", "--reason", "customer email"], 0)):
                out = io.StringIO()
                with mock.patch.object(sys, "argv", ["delete_person.py"] + argv), redirect_stdout(out):
                    module.main()
                self.assertRegex(out.getvalue(), r"verification_sessions\s+1\b")
                self.assertEqual(db[VERIFICATION_SESSIONS].count_documents({}), remaining, out.getvalue())

    def test_mongo_setup_lists_the_collection_as_one_that_expires(self):
        client = mongomock.MongoClient()
        module = load_script("mongo_setup")
        out = io.StringIO()
        env = {"EMOTORAD_MONGO_URI": "mongodb+srv://emotorad-ai-dev:SECRET@emotorad.example.mongodb.net/"}
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "connect", lambda db_name: client[db_name]), \
                redirect_stdout(out):
            code = module.main()
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"verification_sessions\s+expires\s+.*expires_at_ttl \(TTL 0s\)")


# -- wiring ---------------------------------------------------------------------


class WiringTests(unittest.TestCase):
    def missing(self, log):
        return [e for e in log.events if e["event"] == "verification_sessions_ttl_missing"]

    def test_memory_gives_in_memory_sessions(self):
        stores = build_stores(Settings(store="memory"))
        self.assertIsInstance(stores.verified_sessions, InMemoryVerifiedSessions)
        self.assertEqual(stores.verified_sessions_status, "memory")

    def test_mongodb_gives_mongodb_sessions_on_the_same_database(self):
        client = mongomock.MongoClient()
        ensure_indexes(client["emotorad_ai"])
        log = EventLog(path=None)
        stores = build_stores(Settings(store="mongodb"), log=log, client=client)
        self.assertIsInstance(stores.verified_sessions, MongoVerifiedSessions)
        self.assertEqual(stores.verified_sessions_status, "mongodb")
        self.assertEqual(self.missing(log), [])
        stores.verified_sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(client["emotorad_ai"][VERIFICATION_SESSIONS].count_documents({}), 1)

    def test_without_the_ttl_index_proofs_stay_in_memory_and_it_says_so(self):
        client = mongomock.MongoClient()
        log = EventLog(path=None)
        with self.assertLogs("emotorad_ai.wiring", level="ERROR") as logged:
            stores = build_stores(Settings(store="mongodb"), log=log, client=client)
        self.assertIsInstance(stores.verified_sessions, InMemoryVerifiedSessions)
        self.assertEqual(stores.verified_sessions_status, SESSIONS_TTL_MISSING)
        [event] = self.missing(log)
        self.assertEqual((event["level"], event["reason"]), ("error", SESSIONS_TTL_MISSING))
        self.assertIn("verification_sessions_ttl_missing", "\n".join(logged.output))
        self.assertNotIn(VERIFICATION_SESSIONS, client["emotorad_ai"].list_collection_names(),
                         "nothing is written where it would never expire")

    def test_an_unreadable_index_list_keeps_proofs_in_memory_too(self):
        client = mongomock.MongoClient()
        ensure_indexes(client["emotorad_ai"])
        log = EventLog(path=None)
        unreachable = ServerSelectionTimeoutError("no servers found")
        with mock.patch.object(mongomock.collection.Collection, "index_information", side_effect=unreachable), \
                self.assertLogs("emotorad_ai.wiring", level="ERROR"):
            stores = build_stores(Settings(store="mongodb"), log=log, client=client)
        self.assertIsInstance(stores.verified_sessions, InMemoryVerifiedSessions)
        self.assertEqual(stores.verified_sessions_status, SESSIONS_INDEX_UNREADABLE)
        [event] = self.missing(log)
        self.assertEqual((event["reason"], event["error"]), (SESSIONS_INDEX_UNREADABLE, "StoreUnavailable"))

    def test_health_says_where_proofs_are_kept(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.assertEqual(api.health()["verification_sessions"], "memory")
        with mock.patch.object(api.stores, "verified_sessions_status", SESSIONS_TTL_MISSING):
            self.assertEqual(api.health()["verification_sessions"], SESSIONS_TTL_MISSING)

    def test_the_api_store_saves_to_the_built_sessions_and_logs_to_the_event_log(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.assertIs(api.verification_store.sessions, api.stores.verified_sessions)
        self.assertIs(api.verification_store.log, api.log)
        api.verification_store.issue("web-restart", PHONE, "123456")
        self.assertTrue(api.verification_store.check("web-restart", "123456"))
        self.assertEqual(api.stores.verified_sessions.get("web-restart")[0], PHONE)


# -- the reproduction, through the whole turn -----------------------------------


class RestartMidConversationTests(unittest.TestCase):
    """The staging transcript: verified, bike chosen, diagnosis under way, a
    deploy, then "I am blind"."""

    def start(self, sessions):
        conversations = InMemoryConversationStore()
        chat = Chat(replies=[say("Is the charger light on?")], conversations=conversations, sessions=sessions)
        chat.verify(phone=ONE_BIKE)
        chat.say("yes")
        self.assertEqual(chat.state().selected_frame, "EMXP2025004417")
        self.assertEqual(chat.state().agent, battery_support.AGENT_NAME)
        return conversations

    def test_after_a_restart_the_agent_answers_and_the_bike_is_still_chosen(self):
        sessions = InMemoryVerifiedSessions()
        conversations = self.start(sessions)
        restarted = Chat(replies=[say("I can describe each step in words.")], conversations=conversations,
                         sessions=sessions)
        reply = restarted.say("I am blind")
        self.assertEqual(reply.handled_by, battery_support.AGENT_NAME)
        self.assertEqual(reply.text.count("I can describe each step in words."), 1)
        self.assertEqual(restarted.state().selected_frame, "EMXP2025004417")
        self.assertEqual(restarted.state().user_key, "PHONE#" + ONE_BIKE)
        self.assertEqual(len(restarted.llm.requests), 1)

    def test_control_without_saved_sessions_the_restart_asks_for_the_number(self):
        conversations = self.start(None)
        restarted = Chat(conversations=conversations)
        self.assertEqual(restarted.say("I am blind").handled_by, "verify_first:ask_number")

    def test_a_change_of_number_whose_delete_failed_stays_changed_after_a_restart(self):
        # The review, minor 3: "wrong mobile number" ended the proof in memory
        # but MongoDB kept it, and the turn was saved. On a shared browser a
        # restart must not hand the first person's bike to whoever is typing.
        sessions = DeleteFails()
        chat = Chat(replies=[say("Is the charger light on?")], sessions=sessions)
        chat.verify(phone=ONE_BIKE)
        chat.say("yes")
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING"):
            self.assertEqual(chat.say("wrong mobile number").handled_by, "verify_first:change_number")
        self.assertIsNotNone(sessions.get("c1"), "the delete really failed")
        self.assertEqual(chat.state().verify_step, "number")
        restarted = Chat(conversations=chat.conversations, sessions=sessions)  # no model reply to give
        reply = restarted.say("I am blind")
        self.assertTrue(reply.handled_by.startswith("verify_first:ask_number"), reply.handled_by)
        self.assertIsNone(restarted.store.verified_phone("c1"))
        self.assertEqual(restarted.llm.requests, [])

    def test_a_saved_session_for_someone_else_is_not_taken_back(self):
        # The saved conversation is one person's; the saved session names
        # another (a save that lost its race). Neither is guessed at.
        sessions = InMemoryVerifiedSessions()
        conversations = self.start(sessions)
        sessions.save("c1", "+919700000099", datetime.now(timezone.utc).isoformat(),
                      datetime.now(timezone.utc) + timedelta(hours=1))
        restarted = Chat(conversations=conversations, sessions=sessions)
        self.assertTrue(restarted.say("I am blind").handled_by.startswith("verify_first:"))
        self.assertIsNone(restarted.store.verified_phone("c1"))


if __name__ == "__main__":
    unittest.main()
