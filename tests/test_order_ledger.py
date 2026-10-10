"""The replacement order ledger (spec 2026-10-10 replacement orders, section 3)."""

import unittest

import mongomock

from emotorad_ai.fulfilment import ProductIds, ReplacementOrders, open_key, order_reference
from emotorad_ai.stores.mongo import MongoOrderLedger, ensure_indexes


def order(ledger, frame="EMXP0001", part="battery", status="queued"):
    ref = ledger.next_reference()
    return {"_id": ref, "open_key": open_key(frame, part), "frame_number": frame, "part": part,
            "product_id": "p1", "product_name": "EMX Plus", "customer": {}, "delivery_address": "x",
            "phone": "+919876543210", "conversation_id": "c1", "ticket_reference": None, "status": status,
            "oms": {"intent_at": None, "order_code": None, "order_id": None, "passes": 0, "last_error": None},
            "next_attempt_at": "2026-10-10T00:00:00+00:00", "lease_until": None,
            "created_at": "2026-10-10T00:00:00+00:00", "updated_at": "2026-10-10T00:00:00+00:00"}


def ledgers():
    db = mongomock.MongoClient().db
    ensure_indexes(db)
    return {"memory": ReplacementOrders(), "mongodb": MongoOrderLedger(db)}


class ReferenceTests(unittest.TestCase):
    def test_references_are_ro_and_seven_digits_from_one_million_and_one(self):
        self.assertEqual(order_reference(1000001), "RO-1000001")
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                self.assertEqual([ledger.next_reference(), ledger.next_reference()], ["RO-1000001", "RO-1000002"])

    def test_the_open_key_ignores_case_and_spaces(self):
        self.assertEqual(open_key("emx p0001 ", "battery"), "EMXP0001|battery")


class OneOpenOrderTests(unittest.TestCase):
    def test_a_second_open_order_for_the_bike_and_part_returns_the_first(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, created = ledger.insert(order(ledger))
                self.assertTrue(created)
                again, created = ledger.insert(order(ledger))
                self.assertFalse(created)
                self.assertEqual(again["_id"], first["_id"])
                self.assertEqual(ledger.open_order("emxp0001", "battery")["_id"], first["_id"])

    def test_another_part_is_another_order(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                ledger.insert(order(ledger))
                self.assertTrue(ledger.insert(order(ledger, part="charger"))[1])

    def test_after_a_cancellation_a_new_order_is_allowed(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger))
                ledger.cancel(first["_id"])
                self.assertIsNone(ledger.open_order("EMXP0001", "battery"))
                self.assertTrue(ledger.insert(order(ledger))[1])

    def test_a_failed_order_still_blocks_a_new_one(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger, status="failed"))
                self.assertFalse(ledger.insert(order(ledger))[1])

    def test_two_ledgers_on_one_database_make_one_order(self):
        db = mongomock.MongoClient().db
        ensure_indexes(db)
        one, two = MongoOrderLedger(db), MongoOrderLedger(db)
        first, _ = one.insert(order(one))
        again, created = two.insert(order(two))
        self.assertFalse(created)
        self.assertEqual(again["_id"], first["_id"])


class WorkQueueTests(unittest.TestCase):
    def test_due_and_claim_take_a_queued_order_once(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger))
                ledger.insert(order(ledger, frame="EMXP0002", status="recorded"))
                due = ledger.due("2026-10-10T00:01:00+00:00")
                self.assertEqual([o["_id"] for o in due], [first["_id"]])
                self.assertIsNotNone(ledger.claim(first["_id"], "2026-10-10T00:01:00+00:00",
                                                  "2026-10-10T00:06:00+00:00"))
                self.assertIsNone(ledger.claim(first["_id"], "2026-10-10T00:02:00+00:00",
                                               "2026-10-10T00:07:00+00:00"))
                self.assertIsNotNone(ledger.claim(first["_id"], "2026-10-10T00:06:01+00:00",
                                                  "2026-10-10T00:11:01+00:00"))

    def test_save_keeps_what_the_worker_wrote(self):
        for name, ledger in ledgers().items():
            with self.subTest(ledger=name):
                first, _ = ledger.insert(order(ledger))
                first["status"], first["oms"]["order_code"] = "sent", "AFS/26-27/EC/1"
                ledger.save(first)
                self.assertEqual(ledger.get(first["_id"])["oms"]["order_code"], "AFS/26-27/EC/1")


class ProductIdTests(unittest.TestCase):
    def test_empty_until_agreed(self):
        self.assertIsNone(ProductIds().resolve("EMX Plus", "battery"))
        self.assertFalse(ProductIds().has_part(["motor", "controller"]))

    def test_resolves_by_model_and_part(self):
        ids = ProductIds({"EMX Plus": {"battery": "uuid-1"}})
        self.assertEqual(ids.resolve("EMX Plus Black-XX01/EM02", "battery"), "uuid-1")
        self.assertTrue(ids.has_part(["battery"]))


class IndexTests(unittest.TestCase):
    def test_the_index_is_reported(self):
        db = mongomock.MongoClient().db
        self.assertFalse(MongoOrderLedger(db).index_ready())
        ensure_indexes(db)
        self.assertTrue(MongoOrderLedger(db).index_ready())


if __name__ == "__main__":
    unittest.main()
