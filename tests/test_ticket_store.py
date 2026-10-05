"""The ticket record and both ticket stores (plan Task 2)."""

import threading
import unittest

import mongomock
from pymongo.errors import ServerSelectionTimeoutError

from emotorad_ai.config import Settings
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.stores.mongo import COUNTERS, INDEXES, TICKETS, MongoTicketStore, ensure_indexes
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.wiring import build_stores
from tests.ticket_store_contract import PHONE, RUN, T0, TicketStoreContract, at, record_for

RECORD_KEYS = {
    "_id", "chat_reference", "source_key", "mode", "kind", "urgent", "conversation_id", "cluster_id", "started_at",
    "ended_at", "channel", "created_at", "phone", "identity", "category", "ai_severity", "summary", "claims", "bike",
    "coverage", "customer_name", "notes", "zoho", "posted_turns", "posted_media", "posted_notes", "state",
    "due_since", "wake", "attempts", "next_attempt_at", "lease_until", "lease_token", "intent", "last_error",
}


def fields(**overrides):
    base = dict(reference="EM-1000001", chat_reference="stage:EM-1000001",
                source_key="c1:%s:create_support_ticket:k1" % RUN, mode="test", kind="support",
                conversation_id="c1", started_at=RUN, cluster_id="cluster-1", channel="whatsapp", phone=PHONE,
                identity="verified", category="battery_charging", ai_severity="normal", summary="LED stays off.",
                claims={}, bike=None, coverage="computed", customer_name=None, created_at=T0)
    base.update(overrides)
    return base


class NewRecordTests(unittest.TestCase):
    def test_the_document_has_exactly_the_spec_fields_and_starts_waiting(self):
        record = new_record(**fields())
        self.assertEqual(set(record), RECORD_KEYS)
        self.assertEqual(record["_id"], "EM-1000001")
        self.assertEqual((record["state"], record["due_since"], record["next_attempt_at"]), ("waiting", T0, at(120)))
        self.assertEqual((record["wake"], record["attempts"], record["ended_at"], record["lease_until"],
                          record["lease_token"], record["intent"], record["last_error"]),
                         (0, 0, None, None, None, None, None))
        self.assertEqual((record["notes"], record["posted_turns"], record["posted_media"], record["posted_notes"]),
                         ([], [], [], []))
        self.assertEqual(record["zoho"], {"contact_id": None, "ticket_id": None, "ticket_number": None,
                                          "web_url": None, "comment_ids": [], "attachment_ids": []})

    def test_urgency_comes_from_the_kind_and_the_category(self):
        self.assertFalse(new_record(**fields())["urgent"])
        self.assertTrue(new_record(**fields(category="battery_safety"))["urgent"])
        self.assertTrue(new_record(**fields(kind="safety", category=None))["urgent"])

    def test_the_callers_claims_and_bike_are_copied(self):
        claims, bike = {"stated_name": "Test"}, {"model": "EMX Plus", "frame_number": "EMXP2026001234"}
        record = new_record(**fields(claims=claims, bike=bike))
        claims["stated_name"], bike["model"] = "changed", "changed"
        self.assertEqual((record["claims"]["stated_name"], record["bike"]["model"]), ("Test", "EMX Plus"))

    def test_a_bad_kind_mode_identity_or_missing_key_is_refused(self):
        for bad in ({"kind": "complaint"}, {"mode": "staging"}, {"identity": "asserted"}, {"source_key": ""},
                    {"conversation_id": ""}):
            with self.subTest(bad), self.assertRaises(ValueError):
                new_record(**fields(**bad))


class InMemoryTicketStoreTests(TicketStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryTicketStore()

    def test_references_from_many_threads_are_all_different(self):
        store, seen, lock = InMemoryTicketStore(), [], threading.Lock()

        def take():
            mine = [store.next_reference() for _ in range(25)]
            with lock:
                seen.extend(mine)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(seen)), 200)

    def test_one_source_key_from_many_threads_is_one_record(self):
        store, results, lock = InMemoryTicketStore(), [], threading.Lock()
        barrier = threading.Barrier(8)
        key = "c1:%s:create_support_ticket:k1" % RUN

        def insert():
            barrier.wait()
            record = record_for(store, source_key=key)
            with lock:
                results.append(record["_id"])

        threads = [threading.Thread(target=insert) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(store.listing("test")), 1)

    def test_a_returned_record_is_a_copy(self):
        store = InMemoryTicketStore()
        record = record_for(store)
        record["notes"].append({"text": "changed outside", "at": T0})
        self.assertEqual(store.get(record["_id"])["notes"], [])


class MongoTicketStoreTests(TicketStoreContract, unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def make_store(self):
        return MongoTicketStore(self.db)

    def test_two_servers_share_one_counter_and_one_record_per_source_key(self):
        one, two = MongoTicketStore(self.db), MongoTicketStore(self.db)
        self.assertEqual([one.next_reference(), two.next_reference(), one.next_reference()],
                         ["EM-1000001", "EM-1000002", "EM-1000003"])
        key = "c1:%s:create_support_ticket:k1" % RUN
        first = record_for(one, source_key=key)
        again = record_for(two, source_key=key)
        self.assertEqual(again["_id"], first["_id"])
        self.assertEqual(self.db[TICKETS].count_documents({"source_key": key}), 1)

    def test_a_second_worker_finds_nothing_while_the_first_holds_the_lease(self):
        one, two = MongoTicketStore(self.db), MongoTicketStore(self.db)
        record_for(one)
        self.assertIsNotNone(one.take_due(at(120), "test", 300, "worker-1"))
        self.assertIsNone(two.take_due(at(121), "test", 300, "worker-2"))

    def test_a_save_after_the_record_was_removed_writes_nothing_back(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.db[TICKETS].delete_one({"_id": record["_id"]})  # removed by a person meanwhile
        self.assertFalse(store.save(record["_id"], "t1", {"state": "sent"}, add_to_set={"posted_turns": [1]}))
        self.assertEqual(self.db[TICKETS].count_documents({}), 0)

    def test_without_setup_the_unique_index_is_reported_missing(self):
        self.assertFalse(MongoTicketStore(mongomock.MongoClient()["emotorad_ai"]).has_unique_source_key())

    def test_a_source_key_index_that_is_not_unique_does_not_count(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        db[TICKETS].create_index([("source_key", 1)], name="source_key")
        self.assertFalse(MongoTicketStore(db).has_unique_source_key())

    def test_an_unreachable_cluster_is_store_unavailable(self):
        class Unreachable:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise ServerSelectionTimeoutError("no servers found")
                return fail

        store = MongoTicketStore({TICKETS: Unreachable(), COUNTERS: Unreachable()})
        for call in (store.next_reference, lambda: store.get("EM-1000001"),
                     lambda: store.take_due(T0, "test", 300, "t1"), lambda: store.mark_stuck("EM-1000001"),
                     store.has_unique_source_key):
            with self.assertRaises(StoreUnavailable):
                call()


class TicketIndexTests(unittest.TestCase):
    def test_tickets_get_their_four_indexes_and_counters_none(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        report = ensure_indexes(db)
        self.assertEqual(report[TICKETS], ["_id_", "conversation", "due", "phone", "source_key"])
        self.assertEqual(report[COUNTERS], ["_id_"])
        info = db[TICKETS].index_information()
        self.assertTrue(info["source_key"]["unique"])
        self.assertEqual(info["due"]["key"], [("state", 1), ("next_attempt_at", 1)])

    def test_neither_collection_ever_expires(self):
        for collection in (TICKETS, COUNTERS):
            for _, options in INDEXES[collection]:
                self.assertNotIn("expireAfterSeconds", options)


class StoresWiringTests(unittest.TestCase):
    def test_the_memory_stores_include_an_in_memory_ticket_store(self):
        self.assertIsInstance(build_stores(Settings(store="memory")).tickets, InMemoryTicketStore)

    def test_mongodb_puts_tickets_on_the_same_database(self):
        client, settings = mongomock.MongoClient(), Settings(store="mongodb")
        stores = build_stores(settings, client=client)
        self.assertIsInstance(stores.tickets, MongoTicketStore)
        stores.tickets.next_reference()
        self.assertEqual(client[settings.mongo_db][COUNTERS].count_documents({}), 1)


if __name__ == "__main__":
    unittest.main()
