"""One safety ticket per run of a conversation, not per conversation id.

Found by the mutation audit, decided by the person on 2026-09-29. The key was
`safety:<conversation_id>`, and write receipts live 7 days while a
conversation's working state expires after 48 hours. A WhatsApp thread keeps
its id, so a hazard reported in a new run of the thread within the week was
handed the old ticket's reference and raised nothing. The key now carries the
run's start (the same "run" a conversation summary is keyed by), so repeats
inside one run still share a ticket and a new run raises its own.
"""

import itertools
import unittest
from datetime import date, datetime, timezone

import mongomock

from emotorad_ai.stores.mongo import MongoConversationStore, MongoIdempotencyStore, ensure_indexes
from emotorad_ai.tools.mocks import build_registry
from tests.test_runtime_persistence import runtime_on, send

TODAY = date(2026, 7, 28)


def ticking_clock():
    """A new second on every call, so two runs never share a start time."""
    seconds = itertools.count()
    return lambda: "2026-09-29T10:%02d:%02dZ" % divmod(next(seconds) % 3600, 60)


class SafetyTicketPerRunTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        receipts = MongoIdempotencyStore(self.db, now=lambda: datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc))
        self.registry = build_registry(today=TODAY, idempotency=receipts)
        self.store = MongoConversationStore(self.db, clock=ticking_clock())

    def hazard(self, text):
        return send(runtime_on(self.store, [], registry=self.registry), text, cid="wa-thread")

    def test_a_new_run_of_the_thread_raises_its_own_safety_ticket(self):
        first = self.hazard("my battery is swollen")
        self.db["conversations"].delete_one({"_id": "wa-thread"})  # 48 h on: the working state expires
        second = self.hazard("my battery is smoking now")
        self.assertTrue(first.ticket_id)
        self.assertNotEqual(second.ticket_id, first.ticket_id)
        self.assertEqual(len(self.registry.tickets.tickets), 2)

    def test_a_repeat_inside_one_run_shares_its_ticket(self):
        # The pair to the test above: the same thread, the same run.
        first = self.hazard("my battery is swollen")
        second = self.hazard("it is still swollen and hot")
        self.assertEqual(second.ticket_id, first.ticket_id)
        self.assertEqual(len(self.registry.tickets.tickets), 1)


if __name__ == "__main__":
    unittest.main()
