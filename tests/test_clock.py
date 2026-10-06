"""The pin on mongomock's TTL clock (tests/clock.py) does what the
Mongo-backed tests rely on it for.

Every test whose store runs on a fixed date leans on this pin. If a mongomock
upgrade stopped reading `mongomock.utcnow` for its TTL indexes, each pin would
silently do nothing, and those tests would start failing on the calendar again
or pass without testing anything. These fail first.
"""

import unittest
from datetime import datetime, timedelta, timezone

import mongomock

from emotorad_ai.stores.mongo import IDEMPOTENCY_KEYS, ensure_indexes
from tests.clock import mongomock_clock_at

# Long past by the real clock, so only the pin can keep the document.
WRITTEN = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
EXPIRES = WRITTEN + timedelta(days=7)


class MongomockClockTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.db[IDEMPOTENCY_KEYS].insert_one({"_id": "k", "status": "pending", "expires_at": EXPIRES})

    def receipt(self):
        return self.db[IDEMPOTENCY_KEYS].find_one({"_id": "k"})

    def test_a_document_is_kept_until_the_pinned_clock_reaches_its_expiry(self):
        with mongomock_clock_at(EXPIRES - timedelta(seconds=1)):
            self.assertIsNotNone(self.receipt())

    def test_a_document_is_removed_once_the_pinned_clock_reaches_its_expiry(self):
        with mongomock_clock_at(EXPIRES):
            self.assertIsNone(self.receipt())

    def test_a_time_without_a_zone_is_refused(self):
        with self.assertRaises(ValueError):
            mongomock_clock_at(datetime(2026, 9, 29, 10, 0))


if __name__ == "__main__":
    unittest.main()
