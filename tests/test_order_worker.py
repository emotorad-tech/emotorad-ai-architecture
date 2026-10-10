"""The order worker (spec 2026-10-10 replacement orders, section 4), against
the fake OMS of tests/test_oms_afs.py."""

import unittest
from datetime import datetime, timedelta, timezone

from emotorad_ai import oms_afs
from emotorad_ai.fulfilment import ReplacementOrders, open_key
from emotorad_ai.order_worker import MAX_PASSES, OrderWorker
from tests.test_oms_afs import FakeOMS, client

START = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)


def queued(ledger, at=START, frame="EMXP0001"):
    ref = ledger.next_reference()
    order, _ = ledger.insert({
        "_id": ref, "open_key": open_key(frame, "battery"), "frame_number": frame, "part": "battery",
        "product_id": "uuid-1", "product_name": "EMX Plus",
        "customer": {"name": "Test Rider", "email": "t@example.com", "mobile": "9876543210",
                     "address": {"line1": "A1102", "line2": "Sector 49", "pincode": "122018"}},
        "delivery_address": "x", "phone": "+919876543210", "conversation_id": "c1", "ticket_reference": None,
        "status": "queued",
        "oms": {"intent_at": None, "order_code": None, "order_id": None, "passes": 0, "last_error": None},
        "next_attempt_at": at.isoformat(), "lease_until": None, "created_at": at.isoformat(),
        "updated_at": at.isoformat()})
    return order


def worker(ledger, oms, now, logged=None, env=None):
    return OrderWorker(ledger, client(oms, env=env), pin_codes=lambda pincode: "pin-1",
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


class EnvironmentTests(unittest.TestCase):
    """Finding 4: the ticket number carries the deployment's environment."""

    def test_an_order_is_sent_and_found_under_its_environments_ticket_number(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(fail_add_after_create=True), [START]
        order = queued(ledger)
        worker(ledger, oms, now, env="stage").run_once()
        self.assertEqual(oms.sent[0]["ticket_number"], order["_id"] + "/stage")
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")
        self.assertEqual(len(oms.orders), 1)

    def test_another_environments_order_does_not_stop_ours(self):
        ledger, now = ReplacementOrders(), [START]
        order = queued(ledger)
        oms = FakeOMS(orders=[{"id": "o-7", "order_code": "AFS/X/7", "ticket_id": order["_id"]}])
        worker(ledger, oms, now, env="stage").run_once()
        self.assertEqual(len(oms.sent), 1)
        self.assertNotEqual(ledger.get(order["_id"])["oms"]["order_code"], "AFS/X/7")

    def test_a_row_for_another_frame_is_not_our_order(self):
        ledger, now = ReplacementOrders(), [START]
        order = queued(ledger)
        oms = FakeOMS(orders=[{"id": "o-7", "order_code": "AFS/X/7", "ticket_id": order["_id"],
                               "frame_number": "EMXP9999"}])
        worker(ledger, oms, now).run_once()
        self.assertEqual(len(oms.sent), 1)


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

    def test_after_eight_passes_with_oms_looking_empty_it_fails_and_says_so(self):
        logged = []
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        afs = OrderWorker(ledger, client(oms), pin_codes=lambda pincode: None,
                          log=lambda event, fields: logged.append((event, fields)), clock=lambda: now[0])
        for _ in range(MAX_PASSES):
            afs.run_once()
            now[0] = datetime.fromisoformat(ledger.get(order["_id"])["next_attempt_at"]) \
                if ledger.get(order["_id"])["status"] == "queued" else now[0]
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["open_key"], open_key("EMXP0001", "battery"))
        self.assertIn(("replacement_order_failed", {"reference": order["_id"], "error": "pin_code_unknown"}), logged)

    def test_after_eight_passes_with_oms_down_it_is_look_only_not_failed(self):
        logged = []
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now, logged)
        for _ in range(MAX_PASSES):
            afs.run_once()
            now[0] = datetime.fromisoformat(ledger.get(order["_id"])["next_attempt_at"])
        saved = ledger.get(order["_id"])
        self.assertEqual((saved["status"], saved["oms"]["look_only"]), ("queued", True))
        self.assertEqual(saved["open_key"], open_key("EMXP0001", "battery"))
        self.assertEqual([event for event, _ in logged], [])

    def test_a_day_old_order_fails_on_its_next_failure(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START + timedelta(hours=25)]
        order = queued(ledger)
        OrderWorker(ledger, client(oms), pin_codes=lambda pincode: None, clock=lambda: now[0]).run_once()
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


class LookFailsAfterSend:
    """The real client, except that a look answers an error while `looking_fails` is set."""

    def __init__(self, real):
        self._real = real
        self.looking_fails = False

    def find(self, reference, frame_number=None):
        if self.looking_fails:
            raise oms_afs.OMSCallError("look failed")
        return self._real.find(reference, frame_number=frame_number)

    def place(self, request, client_ref):
        try:
            return self._real.place(request, client_ref)
        finally:
            self.looking_fails = True  # the send is over; the look-again that follows fails

    def sale_type_id(self, name):
        return self._real.sale_type_id(name)


class NeverFailedWhileOMSMayHoldItTests(unittest.TestCase):
    def test_a_final_pass_whose_look_errors_is_look_only_not_failed(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(fail_add_after_create=True), [START]
        order = queued(ledger)
        order["oms"]["passes"] = MAX_PASSES - 1
        ledger.save(order)
        stub = LookFailsAfterSend(client(oms))
        afs = OrderWorker(ledger, stub, pin_codes=lambda pincode: "pin-1", clock=lambda: now[0])
        afs.run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertTrue(saved["oms"]["look_only"])
        self.assertEqual(saved["next_attempt_at"], (START + timedelta(minutes=60)).isoformat())
        self.assertEqual(len(oms.orders), 1)
        # OMS is reachable again: a look-only pass finds the order and sends nothing.
        stub.looking_fails = False
        now[0] = START + timedelta(minutes=61)
        afs.run_once()
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")
        self.assertEqual(len(oms.sent), 1)

    def test_a_final_pass_whose_first_look_errors_is_look_only(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        order["oms"]["passes"] = MAX_PASSES - 1
        ledger.save(order)
        worker(ledger, oms, now).run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertTrue(saved["oms"]["look_only"])

    def test_a_look_only_order_is_never_sent(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(), [START]
        order = queued(ledger)
        order["oms"]["look_only"] = True
        ledger.save(order)
        worker(ledger, oms, now).run_once()
        self.assertEqual(oms.sent, [])
        self.assertEqual(ledger.get(order["_id"])["status"], "failed")

    def test_a_look_only_order_that_looks_and_errors_waits_an_hour(self):
        ledger, oms, now = ReplacementOrders(), FakeOMS(down=True), [START]
        order = queued(ledger)
        order["oms"]["look_only"] = True
        ledger.save(order)
        worker(ledger, oms, now).run_once()
        saved = ledger.get(order["_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["next_attempt_at"], (START + timedelta(minutes=60)).isoformat())


class ClaimLeaseTests(unittest.TestCase):
    def test_each_claim_has_its_own_lease_from_its_own_time(self):
        claims = []

        class Recording(ReplacementOrders):
            def claim(self, reference, now_iso, lease_until_iso):
                claims.append((now_iso, lease_until_iso))
                return super().claim(reference, now_iso, lease_until_iso)

        ledger, oms = Recording(), FakeOMS()
        queued(ledger)
        queued(ledger, frame="EMXP0002")
        ticks = [0]

        def clock():
            ticks[0] += 1
            return START + timedelta(seconds=200 * ticks[0])

        afs = OrderWorker(ledger, client(oms), pin_codes=lambda pincode: "pin-1", clock=clock)
        self.assertEqual(afs.run_once(), 2)
        self.assertEqual(len(claims), 2)
        for claimed_at, lease_until in claims:
            self.assertEqual(datetime.fromisoformat(lease_until) - datetime.fromisoformat(claimed_at),
                             timedelta(seconds=300))
        self.assertGreater(claims[1][0], claims[0][0])


class OneBadOrderTests(unittest.TestCase):
    def _two(self, ledger):
        first = queued(ledger, START)
        first["customer"]["address"]["pincode"] = "000000"
        ledger.save(first)
        second = queued(ledger, START + timedelta(seconds=1), frame="EMXP0002")
        return first, second

    def _worker(self, ledger, oms, now, logged):
        def pins(pincode):
            if pincode == "000000":
                raise RuntimeError("pin service down")
            return "pin-1"

        return OrderWorker(ledger, client(oms), pin_codes=pins,
                           log=lambda event, fields: logged.append((event, fields)), clock=lambda: now[0])

    def test_one_bad_order_never_stops_the_pass(self):
        ledger, oms, now, logged = ReplacementOrders(), FakeOMS(), [START + timedelta(minutes=1)], []
        first, second = self._two(ledger)
        self._worker(ledger, oms, now, logged).run_once()
        self.assertIn(("replacement_order_pass_failed", {"reference": first["_id"], "error": "RuntimeError"}), logged)
        self.assertEqual(ledger.get(second["_id"])["status"], "sent")
        saved = ledger.get(first["_id"])
        self.assertEqual((saved["status"], saved["oms"]["passes"]), ("queued", 1))
        self.assertIsNone(saved["lease_until"])

    def test_an_order_that_always_raises_does_not_stay_queued_forever(self):
        ledger, oms, now, logged = ReplacementOrders(), FakeOMS(), [START + timedelta(minutes=1)], []
        first, _ = self._two(ledger)
        afs = self._worker(ledger, oms, now, logged)
        for _ in range(MAX_PASSES):
            afs.run_once()
            now[0] = datetime.fromisoformat(ledger.get(first["_id"])["next_attempt_at"])
        saved = ledger.get(first["_id"])
        self.assertTrue(saved["status"] == "failed" or saved["oms"].get("look_only"))
        afs.run_once()
        self.assertEqual(ledger.get(first["_id"])["status"], "failed")
        self.assertNotIn(first["_id"], [sent["ticket_number"] for sent in oms.sent])


class ProcessesThenRefuses(FakeOMS):
    """OMS acts on afs_order_add sent on the old token, then answers unauthorized."""

    def answer(self, token, frame):
        if frame["url"] == "afs_order_add" and token == "tok-old":
            self.frames.append(frame["url"])
            request = frame.get("request") or {}
            self.sent.append(request)
            self.orders.append({"id": "o-%d" % (len(self.orders) + 1), "order_code": "AFS/X/%d" % (len(self.orders) + 1),
                                "ticket_id": request.get("ticket_number")})
            return {"transmit": "single", "url": "unauthorized"}
        return super().answer(token, frame)


class RefusesAddOnOldToken(FakeOMS):
    """afs_order_add refuses the old token and makes nothing; the other calls accept it."""

    def answer(self, token, frame):
        if frame["url"] == "afs_order_add" and token == "tok-old":
            self.frames.append(frame["url"])
            return {"transmit": "single", "url": "unauthorized"}
        return super().answer(token, frame)


class RefusedTokenOnSendTests(unittest.TestCase):
    """Finding 2: a token refused on the send never resends in the same pass."""

    def test_an_order_made_on_a_refused_token_is_found_not_sent_again(self):
        ledger, oms, now = ReplacementOrders(), ProcessesThenRefuses(), [START]
        order = queued(ledger)
        worker(ledger, oms, now).run_once()
        self.assertEqual(oms.frames.count("afs_order_add"), 1)
        self.assertEqual(len(oms.orders), 1)
        self.assertEqual(oms.logins, 1)
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")

    def test_a_refused_send_that_made_nothing_waits_for_a_later_pass(self):
        ledger, oms, now = ReplacementOrders(), RefusesAddOnOldToken(), [START]
        order = queued(ledger)
        afs = worker(ledger, oms, now)
        afs.run_once()
        self.assertEqual(oms.frames.count("afs_order_add"), 1)
        saved = ledger.get(order["_id"])
        self.assertEqual((saved["status"], saved["oms"]["last_error"]), ("queued", "OMSAuthError"))
        now[0] = datetime.fromisoformat(saved["next_attempt_at"])
        afs.run_once()
        self.assertEqual(oms.frames.count("afs_order_add"), 2)
        self.assertEqual(len(oms.orders), 1)
        self.assertEqual(ledger.get(order["_id"])["status"], "sent")


class IntentTests(unittest.TestCase):
    def test_nothing_is_sent_when_the_intent_cannot_be_saved(self):
        class Unsaving(ReplacementOrders):
            def save(self, order):
                raise OSError("store down")

        ledger, oms, now, logged = Unsaving(), FakeOMS(), [START], []
        order = queued(ledger)
        worker(ledger, oms, now, logged).run_once()
        self.assertEqual(oms.sent, [])
        self.assertIn(("replacement_order_intent_unsaved", {"reference": order["_id"], "error": "OSError"}), logged)


if __name__ == "__main__":
    unittest.main()
