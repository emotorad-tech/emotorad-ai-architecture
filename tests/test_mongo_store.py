import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import mongomock
from pymongo.errors import ServerSelectionTimeoutError

from emotorad_ai.conversation import ConversationConflict, ConversationState, StoreUnavailable
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import INDEXES, MONGO_URI_ENV, MongoConversationStore, connect, ensure_indexes
from tests.store_contract import StoreContract, inbound, reply, summary

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def fresh_db():
    return mongomock.MongoClient()["emotorad_ai"]


class MongoStoreTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        ensure_indexes(self.db)

    def make_store(self, **kw):
        return MongoConversationStore(self.db, now=lambda: NOW, **kw)

    def test_a_stale_save_is_refused(self):
        store = self.make_store()
        state = store.get("c1")
        store.save(state)                 # version 1 stored
        stale = store.get("c1")
        store.save(store.get("c1"))       # someone else saves: version 2
        with self.assertRaises(ConversationConflict):
            store.save(stale)

    def test_two_first_saves_of_one_conversation_conflict(self):
        store = self.make_store()
        first, second = store.get("c1"), store.get("c1")
        store.save(first)
        with self.assertRaises(ConversationConflict):
            store.save(second)

    def test_working_state_expires_after_48_hours_and_records_never_do(self):
        store = self.make_store()
        state = store.get("c1")
        state.user_key, state.turns = "PHONE#+919876543210", 1
        store.save(state)
        store.record_turn(state, inbound("hi"), reply("Hello."), summary("c1"))
        saved = self.db["conversations"].find_one({"_id": "c1"})
        self.assertEqual(saved["expires_at"].replace(tzinfo=timezone.utc), NOW + timedelta(hours=48))
        for turn in self.db["transcript_turns"].find():
            self.assertNotIn("expires_at", turn)
        self.assertNotIn("expires_at", self.db["conversation_summaries"].find_one({"conversation_id": "c1"}))

    def test_transcript_turns_carry_the_person_for_deletion(self):
        store = self.make_store()
        state = store.get("c1")
        state.user_key, state.turns = "PHONE#+919876543210", 1
        store.record_turn(state, inbound("hi"), reply("Hello."))
        self.assertEqual({t["user_key"] for t in self.db["transcript_turns"].find()}, {"PHONE#+919876543210"})

    def test_a_huge_history_is_trimmed_by_whole_turns_and_logged(self):
        log = EventLog(path=None)
        store = self.make_store(log=log, max_state_bytes=20_000)
        state = store.get("c1")
        for i in range(40):
            state.history.append({"role": "user", "content": "turn %d" % i})
            state.history.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": "x", "input": {}}]})
            state.history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": "x" * 1000}]})
            state.history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        store.save(state)
        kept = store.get("c1").history
        self.assertLess(len(json.dumps(kept)), 20_000)
        self.assertIsInstance(kept[0]["content"], str)  # starts at a turn boundary
        self.assertEqual(kept[-4]["content"], "turn 39")  # and the oldest turns are the ones dropped
        uses = {b["id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_use"}
        results = {b["tool_use_id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"}
        self.assertEqual(uses, results)
        self.assertTrue(any(e["event"] == "history_trimmed" for e in log.events))

    def test_state_with_awkward_keys_round_trips(self):
        # Tool inputs can hold keys MongoDB refuses as field names; the state is a string.
        store = self.make_store()
        state = store.get("c1")
        state.history.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {"$where": "1", "a.b": 2}}]})
        store.save(state)
        self.assertEqual(store.get("c1").history, state.history)

    def test_an_unreachable_cluster_is_store_unavailable_not_an_empty_state(self):
        class Unreachable:
            def find_one(self, *a, **k):
                raise ServerSelectionTimeoutError("no servers found")
        broken = {"conversations": Unreachable()}
        with self.assertRaises(StoreUnavailable):
            MongoConversationStore(broken).get("c1")


class IndexTests(unittest.TestCase):
    def test_every_collection_gets_exactly_its_indexes_and_only_two_expire(self):
        db = fresh_db()
        report = ensure_indexes(db)
        self.assertEqual(set(report), {"conversations", "transcript_turns", "conversation_summaries", "idempotency_keys", "media"})
        ttl = {}
        for collection in report:
            for index, info in db[collection].index_information().items():
                if "expireAfterSeconds" in info:
                    ttl["%s.%s" % (collection, index)] = info["expireAfterSeconds"]
        self.assertEqual(ttl, {"conversations.expires_at_ttl": 0, "idempotency_keys.expires_at_ttl": 0})
        self.assertTrue(db["transcript_turns"].index_information()["conversation_turn"]["unique"])
        self.assertIn("user_recent", db["conversation_summaries"].index_information())

    def test_running_it_twice_changes_nothing(self):
        db = fresh_db()
        self.assertEqual(ensure_indexes(db), ensure_indexes(db))

    def test_the_index_table_has_no_ttl_on_the_permanent_record(self):
        for collection in ("transcript_turns", "conversation_summaries"):
            for _, options in INDEXES[collection]:
                self.assertNotIn("expireAfterSeconds", options)


class ConnectTests(unittest.TestCase):
    def test_a_missing_connection_string_is_store_unavailable(self):
        with mock.patch.dict(os.environ, {MONGO_URI_ENV: ""}):
            with self.assertRaises(StoreUnavailable):
                connect()

    def test_a_given_client_is_used_as_is(self):
        client = mongomock.MongoClient()
        self.assertEqual(connect(client=client, db_name="emotorad_ai").name, "emotorad_ai")


if __name__ == "__main__":
    unittest.main()
