import unittest
from datetime import date

import mongomock

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import (
    ConversationConflict,
    ConversationState,
    InMemoryConversationStore,
    StoreUnavailable,
)
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import BUSY_MESSAGE, Runtime
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes
from emotorad_ai.tools.mocks import build_registry

TODAY = date(2026, 7, 28)


def runtime_on(store, responses, registry=None):
    registry = registry or build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(responses)),
                   log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=store)


def send(runtime, text, cid="conv-1", session="sess-ananya"):
    adapter = WebsiteChatAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"conversation_id": cid, "session_token": session, "text": text}))


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def store(self):
        return MongoConversationStore(self.db)

    def test_a_conversation_continues_after_a_restart(self):
        send(runtime_on(self.store(), [say("Try another socket.")]), "my battery won't charge")
        restarted = runtime_on(self.store(), [say("Then it is the charger.")])
        reply = send(restarted, "still nothing")
        self.assertEqual(reply.handled_by, "battery_support")  # routed state survived: no triage again
        self.assertEqual(self.store().get("conv-1").turns, 2)
        self.assertEqual([t.role for t in self.store().transcript("conv-1")], ["customer", "bot", "customer", "bot"])

    def test_a_verified_customer_gets_a_summary_and_an_anonymous_one_does_not(self):
        send(runtime_on(self.store(), [say("Ok.")]), "my battery won't charge")
        [item] = self.store().recent_summaries("PHONE#+919876543210")
        self.assertEqual((item.conversation_id, item.title, item.product_name), ("conv-1", "Battery issue", "EMX Plus"))
        send(runtime_on(self.store(), []), "hello", cid="anon", session="no-such-session")
        self.assertIsNone(self.store().get("anon").user_key)


class ConflictingStore(InMemoryConversationStore):
    """Hands out a fresh copy on every load, as a real database does, so a
    retry cannot quietly reuse the losing attempt's mutated state."""

    def __init__(self, conflicts):
        super().__init__()
        self.conflicts, self.gets = conflicts, 0

    def get(self, conversation_id):
        self.gets += 1
        return ConversationState.from_json(super().get(conversation_id).to_json())

    def save(self, state):
        if self.conflicts:
            self.conflicts -= 1
            raise ConversationConflict("someone else saved")
        super().save(state)


class ConflictTests(unittest.TestCase):
    def test_one_conflict_is_retried_from_fresh_state(self):
        store = ConflictingStore(conflicts=1)
        reply = send(runtime_on(store, [say("First."), say("Second.")]), "my battery won't charge")
        self.assertIn("Second.", reply.text)
        self.assertEqual(store.gets, 2)
        saved = store.get("conv-1")
        self.assertEqual(saved.turns, 1)
        self.assertEqual([m for m in saved.history if m.get("content") == "my battery won't charge"],
                         [{"role": "user", "content": "my battery won't charge"}])

    def test_two_conflicts_answer_busy_without_escalating(self):
        runtime = runtime_on(ConflictingStore(conflicts=2), [say("First."), say("Second.")])
        reply = send(runtime, "my battery won't charge")
        self.assertIn(BUSY_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertTrue(any(e["event"] == "conversation_busy" for e in runtime.log.events))


class OutageTests(unittest.TestCase):
    class DownStore(InMemoryConversationStore):
        def get(self, conversation_id):
            raise StoreUnavailable("MongoDB find_one failed")

    def test_a_store_outage_hands_over_instead_of_starting_blank(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery won't charge")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "store_unavailable")
        self.assertTrue(any(e["event"] == "store_unavailable" for e in runtime.log.events))

    def test_a_failed_transcript_write_is_logged_and_the_customer_still_gets_the_reply(self):
        class TranscriptDown(InMemoryConversationStore):
            def record_turn(self, *a, **k):
                raise StoreUnavailable("MongoDB replace_one failed")
        runtime = runtime_on(TranscriptDown(), [say("Try another socket.")])
        reply = send(runtime, "my battery won't charge")
        self.assertIn("Try another socket.", reply.text)
        self.assertTrue(any(e["event"] == "transcript_write_failed" for e in runtime.log.events))


if __name__ == "__main__":
    unittest.main()
