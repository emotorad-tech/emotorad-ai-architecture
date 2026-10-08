"""invoice_readings: kept, updated, and erased with the person or the
conversation (spec 2026-10-08, section 3)."""

import unittest

import mongomock

from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.stores.mongo import INDEXES, MongoConversationStore, ensure_indexes


def a_reading(conversation_id, key, user_key="cluster-1", read_at="2026-10-08T10:00:00+00:00"):
    return {"_id": key, "conversation_id": conversation_id, "user_key": user_key, "frame_number": "EMXP2025004417",
            "source": "oms", "found": {"is_invoice": True}, "confident": True, "purchase_date": "2025-03-12",
            "read_at": read_at, "ticket_id": None, "told": False}


class Contract:
    def test_a_reading_is_kept_once_per_id_and_in_order(self):
        store = self.make_store()
        store.add_invoice_reading(a_reading("c1", "k2", read_at="2026-10-08T11:00:00+00:00"))
        store.add_invoice_reading(a_reading("c1", "k1"))
        store.add_invoice_reading(a_reading("c1", "k1"))
        self.assertEqual([r["_id"] for r in store.invoice_readings_of("c1")], ["k1", "k2"])

    def test_a_reading_is_updated(self):
        store = self.make_store()
        store.add_invoice_reading(a_reading("c1", "k1"))
        store.update_invoice_reading("c1", "k1", {"told": True, "ticket_id": "EM-1000001"})
        [reading] = store.invoice_readings_of("c1")
        self.assertEqual((reading["told"], reading["ticket_id"]), (True, "EM-1000001"))

    def test_readings_are_erased_with_the_conversation(self):
        store = self.make_store()
        store.add_invoice_reading(a_reading("c1", "k1"))
        store.add_invoice_reading(a_reading("c2", "k2"))
        self.assertEqual(store.delete_conversation("c1")["invoice_readings"], 1)
        self.assertEqual(store.invoice_readings_of("c1"), [])
        self.assertEqual(len(store.invoice_readings_of("c2")), 1)

    def test_readings_are_erased_with_the_person(self):
        store = self.make_store()
        store.add_invoice_reading(a_reading("c1", "k1"))
        store.add_invoice_reading(a_reading("c1", "k2"))
        store.add_invoice_reading(a_reading("c9", "k9", user_key="someone-else"))
        self.assertIn("c1", store.conversations_of("cluster-1"))
        self.assertEqual(store.delete_person("cluster-1", dry_run=True)["invoice_readings"], 2)
        self.assertEqual(len(store.invoice_readings_of("c1")), 2)
        self.assertEqual(store.delete_person("cluster-1")["invoice_readings"], 2)
        self.assertEqual(store.invoice_readings_of("c1"), [])
        self.assertEqual(len(store.invoice_readings_of("c9")), 1)


class InMemoryTests(Contract, unittest.TestCase):
    def make_store(self):
        return InMemoryConversationStore()


class MongoTests(Contract, unittest.TestCase):
    def make_store(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        return MongoConversationStore(db)

    def test_its_indexes_and_no_expiry(self):
        self.assertIn("invoice_readings", INDEXES)
        for _, options in INDEXES["invoice_readings"]:
            self.assertNotIn("expireAfterSeconds", options)


if __name__ == "__main__":
    unittest.main()
