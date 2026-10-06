"""Both receipt stores take over a claim at the same moment.

Found by the mutation audit on 2026-09-29: both docstrings say a claim
"older than" the lease is taken over, but at exactly the lease the in-memory
store took it over (`>=`) and MongoDB refused it (`$lt`). Strictly older is
what the words say and what MongoDB does, so the in-memory store now agrees.
"""

import unittest
from datetime import datetime, timedelta, timezone

import mongomock

from emotorad_ai.stores.mongo import MongoIdempotencyStore, ensure_indexes
from emotorad_ai.tools.registry import CLAIM_LEASE_SECONDS, IdempotencyStore
from tests.clock import mongomock_clock_at

START = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)

# Receipts expire START + 7 days, so mongomock's TTL clock runs on START too.
_MONGOMOCK_CLOCK = mongomock_clock_at(START)


def setUpModule():
    _MONGOMOCK_CLOCK.start()


def tearDownModule():
    _MONGOMOCK_CLOCK.stop()


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def utc(self):
        return START + timedelta(seconds=self.seconds)


def stores(clock):
    db = mongomock.MongoClient()["emotorad_ai"]
    ensure_indexes(db)
    return {
        "memory": IdempotencyStore(clock=clock.monotonic),
        "mongodb": MongoIdempotencyStore(db, now=clock.utc),
    }


class LeaseBoundaryTests(unittest.TestCase):
    def test_at_exactly_the_lease_the_claim_is_still_held(self):
        clock = Clock()
        for name, store in stores(clock).items():
            with self.subTest(store=name):
                clock.seconds = 0.0
                self.assertIsNone(store.claim("k"))
                clock.seconds = float(CLAIM_LEASE_SECONDS)
                self.assertEqual(store.claim("k")["error"]["code"], "write_in_progress")

    def test_a_second_past_the_lease_it_is_taken_over(self):
        clock = Clock()
        for name, store in stores(clock).items():
            with self.subTest(store=name):
                clock.seconds = 0.0
                self.assertIsNone(store.claim("k"))
                clock.seconds = CLAIM_LEASE_SECONDS + 1.0
                self.assertIsNone(store.claim("k"))


if __name__ == "__main__":
    unittest.main()
