"""A verified chat stays verified across a server restart (6 October 2026).

On staging a customer who had proved their number in the web chat was asked
for it again mid-conversation, and then for their bike again. The
conversation was in MongoDB, but the proof lived only in the process's
VerificationStore, so every deploy made every chat in progress anonymous.
Proved numbers are now saved (`verification_sessions`, twelve hours) and read
back when this process has no proof of its own. Codes in transit stay in
memory.
"""

import importlib.util
import io
import logging
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import mongomock
from pymongo.errors import ServerSelectionTimeoutError

from emotorad_ai.agents import battery_support
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.llm import say
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import (
    INDEXES,
    VERIFICATION_SESSIONS,
    MongoConversationStore,
    MongoVerifiedSessions,
    ensure_indexes,
)
from emotorad_ai.tools.verification import VERIFIED_TTL_SECONDS, InMemoryVerifiedSessions, VerificationStore
from emotorad_ai.wiring import build_stores
from tests.test_api_health import fresh_api
from tests.test_verify_first import ONE_BIKE, Chat

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
PHONE = "+919700000033"
OTHER = "+919700000044"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# mongomock expires TTL documents against the real clock; these tests run on NOW.
_MONGOMOCK_CLOCK = mock.patch("mongomock.utcnow", lambda: NOW.replace(tzinfo=None))


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


# -- VerificationStore over a saved-session backend ---------------------------


class SurvivesARestartTests(unittest.TestCase):
    def setUp(self):
        self.clocks = Clocks()
        self.sessions = self.clocks.sessions()
        self.first = self.clocks.store(self.sessions)

    def restarted(self):
        """A new process: nothing in memory, the same saved sessions."""
        return self.clocks.store(self.sessions)

    def test_a_new_store_sharing_the_sessions_knows_the_proved_number(self):
        verify(self.first)
        second = self.restarted()
        self.assertEqual(second.verified_phone("c1"), PHONE)
        self.assertEqual(second.verified_on("c1"), NOW.isoformat())
        self.assertIsNone(second.verified_phone("c2"))

    def test_without_shared_sessions_a_new_store_knows_nothing(self):
        verify(self.first)
        self.assertIsNone(VerificationStore().verified_phone("c1"))

    def test_the_saved_session_lasts_twelve_hours_and_no_longer(self):
        verify(self.first)
        self.clocks.advance(VERIFIED_TTL_SECONDS - 1)
        self.assertEqual(self.restarted().verified_phone("c1"), PHONE)
        self.clocks.advance(2)
        second = self.restarted()
        self.assertIsNone(second.verified_phone("c1"))
        self.assertIsNone(second.verified_on("c1"))

    def test_a_new_code_ends_the_saved_session(self):
        verify(self.first)
        self.first.issue("c1", OTHER, "654321")
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.sessions.get("c1"))
        self.assertIsNone(self.restarted().verified_phone("c1"))

    def test_a_new_code_after_a_restart_ends_the_saved_session(self):
        verify(self.first)
        second = self.restarted()
        second.issue("c1", OTHER, "654321")
        self.assertIsNone(second.verified_phone("c1"))
        self.assertIsNone(self.restarted().verified_phone("c1"))

    def test_reset_ends_the_saved_session(self):
        verify(self.first)
        self.first.reset("c1")
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.restarted().verified_phone("c1"))

    def test_reset_after_a_restart_ends_the_saved_session(self):
        # The runtime's "wrong number" after a deploy: the proof is read from
        # the saved session, then reset must remove it there.
        verify(self.first)
        second = self.restarted()
        self.assertEqual(second.verified_phone("c1"), PHONE)
        second.reset("c1")
        self.assertIsNone(second.verified_phone("c1"))
        self.assertIsNone(self.restarted().verified_phone("c1"))

    def test_cancelling_a_code_leaves_a_proved_number_alone_as_in_memory(self):
        verify(self.first)
        self.first.cancel_code("c1")
        self.assertEqual(self.first.verified_phone("c1"), PHONE)
        self.assertEqual(self.restarted().verified_phone("c1"), PHONE)

    def test_a_session_memory_has_let_lapse_is_never_read_back(self):
        # The monotonic clock says twelve hours have gone; the wall clock was
        # set back meanwhile and says they have not. Memory decides, and once
        # it sweeps the entry the saved session goes with it.
        verify(self.first)
        self.clocks.advance(VERIFIED_TTL_SECONDS + 1, wall=False)
        self.assertIsNone(self.first.verified_phone("c1"))
        self.first.issue("c2", OTHER, "111111")  # the sweep runs on issue
        self.assertIsNone(self.first.verified_phone("c1"))
        self.assertIsNone(self.restarted().verified_phone("c1"))

    def test_a_code_in_transit_is_not_saved(self):
        self.first.issue("c1", PHONE, "123456")
        second = self.restarted()
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


class BackendFailureTests(unittest.TestCase):
    def setUp(self):
        self.log = EventLog(path=None)

    def failures(self):
        return [e for e in self.log.events if e["event"] == "verification_session_unavailable"]

    def test_a_failing_read_is_not_verified_and_never_raises(self):
        store = VerificationStore(sessions=Broken(), log=self.log)
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING") as logged:
            self.assertIsNone(store.verified_phone("c1"))
            self.assertIsNone(store.verified_on("c1"))
        [first, _] = self.failures()
        self.assertEqual((first["operation"], first["error"]), ("get", "StoreUnavailable"))
        self.assertIn("verification_session_unavailable", "\n".join(logged.output))

    def test_any_backend_error_is_not_verified(self):
        class Raises:
            def get(self, cid):
                raise ServerSelectionTimeoutError("no servers found")

        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING"):
            self.assertIsNone(VerificationStore(sessions=Raises()).verified_phone("c1"))

    def test_memory_answers_first_so_a_failing_read_does_not_matter_after_a_code(self):
        store = VerificationStore(sessions=Broken(fail=("get",)), log=self.log)
        verify(store)
        self.assertEqual(store.verified_phone("c1"), PHONE)
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
        self.assertEqual(self.failures()[0]["operation"], "delete")

    def test_a_new_proof_after_a_failed_delete_is_used_again(self):
        backend = Broken(fail=("delete",))
        store = VerificationStore(sessions=backend, log=self.log)
        verify(store)
        with self.assertLogs("emotorad_ai.tools.verification", level="WARNING"):
            store.reset("c1")
            verify(store, phone=OTHER)
        self.assertEqual(store.verified_phone("c1"), OTHER)
        self.assertEqual(VerificationStore(sessions=backend).verified_phone("c1"), OTHER)


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
        self.assertEqual(clocks.store(MongoVerifiedSessions(self.db, now=lambda: clocks.wall)).verified_phone("c1"),
                         PHONE)


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
    def test_memory_gives_in_memory_sessions(self):
        self.assertIsInstance(build_stores(Settings(store="memory")).verified_sessions, InMemoryVerifiedSessions)

    def test_mongodb_gives_mongodb_sessions_on_the_same_database(self):
        client = mongomock.MongoClient()
        stores = build_stores(Settings(store="mongodb"), client=client)
        self.assertIsInstance(stores.verified_sessions, MongoVerifiedSessions)
        stores.verified_sessions.save("c1", PHONE, NOW.isoformat(), NOW + timedelta(hours=12))
        self.assertEqual(client["emotorad_ai"][VERIFICATION_SESSIONS].count_documents({}), 1)

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


if __name__ == "__main__":
    unittest.main()
