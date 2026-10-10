import unittest
from datetime import datetime, timedelta, timezone

import mongomock

from emotorad_ai.stores.mongo import MongoIdempotencyStore, ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, MockTicketSystem, build_registry
from emotorad_ai.tools.registry import IdempotencyStore, ToolContext, ToolRegistry, is_error, ok
from tests.clock import mongomock_clock_at

TICKET = {"category": "battery_charging", "severity": "normal", "description": "LED off.", "idempotency_key": "k1"}
CTX = ToolContext(conversation_id="c1", phone="+919876543210")


class ClaimContract:
    def make(self):
        raise NotImplementedError

    def test_a_key_is_claimed_once_then_returns_the_stored_result(self):
        store = self.make()
        self.assertIsNone(store.claim("k"))
        pending = store.claim("k")
        self.assertEqual(pending["error"]["code"], "write_in_progress")
        store.put("k", ok({"ticket_id": "EM-1"}))
        self.assertEqual(store.claim("k"), ok({"ticket_id": "EM-1"}))

    def test_a_released_claim_can_be_claimed_again(self):
        store = self.make()
        store.claim("k")
        store.release("k")
        self.assertIsNone(store.claim("k"))


class InMemoryClaimTests(ClaimContract, unittest.TestCase):
    def make(self):
        return IdempotencyStore()


class MongoClaimTests(ClaimContract, unittest.TestCase):
    NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)

    def setUp(self):
        # Receipts expire NOW + 7 days, so mongomock's TTL clock runs on NOW too.
        clock = mongomock_clock_at(self.NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def make(self):
        return MongoIdempotencyStore(self.db, now=lambda: self.NOW)

    def test_two_registries_on_one_database_raise_exactly_one_ticket(self):
        # Separate store instances, as two servers would have, on one database.
        tickets = MockTicketSystem()
        first = build_registry(ticket_system=tickets, idempotency=self.make())
        second = build_registry(ticket_system=tickets, idempotency=self.make())
        a = first.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        b = second.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        self.assertEqual(a, b)
        self.assertEqual(len(tickets.tickets), 1)

    def test_a_receipt_expires_after_seven_days(self):
        self.make().claim("c1:x:k")
        doc = self.db["idempotency_keys"].find_one({"_id": "c1:x:k"})
        self.assertEqual(doc["expires_at"].replace(tzinfo=timezone.utc), self.NOW + timedelta(days=7))


class SharedStoreTests(unittest.TestCase):
    def test_two_registries_sharing_one_store_raise_exactly_one_ticket(self):
        # Two servers in miniature: a durable store only has to pass this with
        # its own instance in place of the shared in-memory one.
        shared, tickets = IdempotencyStore(), MockTicketSystem()
        first = build_registry(ticket_system=tickets, idempotency=shared)
        second = build_registry(ticket_system=tickets, idempotency=shared)
        a = first.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        b = second.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        self.assertEqual(a, b)
        self.assertEqual(len(tickets.tickets), 1)


class RegistryReleasesOnFailureTests(unittest.TestCase):
    def test_a_write_tool_that_raises_can_be_retried(self):
        registry = ToolRegistry()
        calls = []

        @registry.register("flaky_write", "d", parameters={"idempotency_key": {"type": "string"}},
                           required=("idempotency_key",), write=True)
        def flaky_write(idempotency_key):
            calls.append(idempotency_key)
            if len(calls) == 1:
                raise RuntimeError("upstream timed out")
            return {"done": True}

        first = registry.call("flaky_write", {"idempotency_key": "k"}, CTX)
        second = registry.call("flaky_write", {"idempotency_key": "k"}, CTX)
        self.assertTrue(is_error(first))
        self.assertEqual(second, ok({"done": True}))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
