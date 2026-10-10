"""The order worker (spec 2026-10-10 replacement orders, section 4), against
the fake OMS of tests/test_oms_afs.py."""

import unittest
from datetime import datetime, timedelta, timezone

from emotorad_ai import oms_afs
from emotorad_ai.fulfilment import ReplacementOrders, open_key
from emotorad_ai.order_worker import MAX_PASSES, OrderWorker
from tests.test_oms_afs import FakeOMS, client

START = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)


def queued(ledger, at=START):
    ref = ledger.next_reference()
    order, _ = ledger.insert({
        "_id": ref, "open_key": open_key("EMXP0001", "battery"), "frame_number": "EMXP0001", "part": "battery",
        "product_id": "uuid-1", "product_name": "EMX Plus",
        "customer": {"name": "Test Rider", "email": "t@example.com", "mobile": "9876543210",
                     "address": {"line1": "A1102", "line2": "Sector 49", "pincode": "122018"}},
        "delivery_address": "x", "phone": "+919876543210", "conversation_id": "c1", "ticket_reference": None,
        "status": "queued",
        "oms": {"intent_at": None, "order_code": None, "order_id": None, "passes": 0, "last_error": None},
        "next_attempt_at": at.isoformat(), "lease_until": None, "created_at": at.isoformat(),
        "updated_at": at.isoformat()})
    return order


def worker(ledger, oms, now, logged=None):
    return OrderWorker(ledger, client(oms), pin_codes=lambda pincode: "pin-1",
                       log=(lambda event, fields: logged.append((event, fields))) if logged is not None else None,
                       clock=lambda: now[0])


class SendTests(unittest.TestCase):
    def test_a_queued_order_is_sent_once(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        self.assertEqual(worker(ledger, oms, now).run_once(), 1)
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "sent")
        self.assertEqual(saved["oms"]["order_code"], "AFS/26-27/EC/1")
        self.assertEqual(len(oms.sent), 1)
        self.assertEqual(oms.sent[0]["ticket_number"], order["_id"])
        self.assertEqual(oms.sent[0]["items"][0]["rate"], 0)
        self.assertEqual(oms.sent[0]["sale_type_id"], "st-2")

    def test_a_found_order_is_never_sent(self):
        ledger, now = ReplacementOrders(), [START]
        order = queued(ledger)
        oms = FakeOMS(orders=[{"id": "o-7", "order_code": "AFS/X/7", "ticket_id": order["_id"]}])
        worker(ledger, oms, now).run_once()
        self.assertEqual(oms.sent, [])
        self.assertEqual(ledger.get(order["_id"])["oms"]["order_code"], "AFS/X/7")

    def test_an_error_after_the_order_was_made_ends_sent_with_one_order(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(fail_add_after_create=True), [START]
        order = queued(ledger)
        worker(ledger, oms, now).run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")
        self.assertEqual(len(oms.orders), 1)

    def test_a_pass_after_a_crash_finds_the_order_and_never_sends(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        # The last pass sent it and died before saving: OMS has it, the ledger says queued with an intent.
        oms.orders.append({"id": "o-1", "order_code": "AFS/26-27/EC/1", "ticket_id": order["_id"]})
        order["oms"]["intent_at"] = START.isoformat()
        ledger.save(order)
        worker(ledger, oms, now).run_once()
        self.assertEqual(oms.sent, [])
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")


class RetryTests(unittest.TestCase):
    def test_the_kill_drill_ends_in_exactly_one_order(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now)
        afs.run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["oms"]["last_error"], "OMSCallError")
        self.assertEqual(saved["next_attempt_at"], (START + timedelta(minutes=1)).isoformat())
        oms.down = False
        now[0] = START + timedelta(minutes=2)
        afs.run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")
        self.assertEqual(len(oms.orders), 1)

    def test_retries_wait_1_5_15_60_minutes_then_hourly(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now)
        waits = []
        for _ in range(6):
            afs.run_once()
            saved = ledger.get(order["_id"])
            due = datetime.fromisoformat(saved["next_attempt_at"])
            waits.append(int((due - now[0]).total_seconds() // 60))
            now[0] = due
        self.assertEqual(waits, [1, 5, 15, 60, 60, 60])

    def test_after_eight_passes_it_fails_and_says_so(self):
        logged = []
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now, logged)
        for _ in range(MAX_PASSES):
            afs.run_once()
            now[0] = datetime.fromisoformat(ledger.get(order["_id"])["next_attempt_at"])
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["open_key"], open_key("EMXP0001", "battery"))
        self.assertIn(("replacement_order_failed", {"reference": order["_id"], "error": "OMSCallError"}), logged)

    def test_a_day_old_order_fails_on_its_next_failure(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START + timedelta(hours=25)]
        order = queued(ledger)
        worker(ledger, oms, now).run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "failed")

    def test_an_unknown_pin_code_waits(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        OrderWorker(ledger, client(oms), pin_codes=lambda pincode: None, clock=lambda: now[0]).run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual((saved["status"], saved["oms"]["last_error"]), ("queued", "pin_code_unknown"))
        self.assertEqual(oms.sent, [])

    def test_a_leased_order_is_not_taken_twice(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        ledger.claim(order["_id"], START.isoformat(), (START + timedelta(minutes=5)).isoformat())
        self.assertEqual(worker(ledger, oms, now).run_once(), 0)
        self.assertEqual(oms.sent, [])


if __name__ == "__main__":
    unittest.main()
