"""Four small defects found by the review of the merge (2026-09-29).

- One customer turn is one `inbound` and one `outcome` event, whatever path it
  takes: a retry after a save conflict, the busy reply, a storage outage. The
  metrics count turns by `inbound` and Langfuse closes a trace on `outcome`.
- A tester's pinned agent is followed; Jev does not route around it.
- One name for one list of modes.
- The in-memory store is bounded, so a long-running process does not grow
  without limit.
"""

import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai import config, llm
from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import (
    ConversationConflict,
    ConversationState,
    InMemoryConversationStore,
    StoreUnavailable,
)
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from tests.store_contract import inbound, reply, summary

TODAY = date(2026, 7, 28)
MOTOR_NARROW = {Q_CATEGORY: choose("motor", 0.95), Q_SUB_CATEGORY: choose("motor-noise", 0.9)}


def runtime(store, replies=(), jev=None, narrow_llm=None):
    registry = build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(replies)),
                   log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=store,
                   jev=jev, narrow_llm=narrow_llm)


def send(rt, text, cid="c1", **meta):
    message = WebsiteChatAdapter(rt.resolver).to_message({"conversation_id": cid, "session_token": "sess-ananya", "text": text})
    if meta:
        message = replace(message, entry_metadata=dict(message.entry_metadata, **meta))
    return rt.handle(message)


def events(rt, name, cid="c1"):
    return [e for e in rt.log.events if e["event"] == name and e["conversation_id"] == cid]


class Conflicting(InMemoryConversationStore):
    def __init__(self, conflicts):
        super().__init__()
        self.conflicts = conflicts

    def get(self, conversation_id):
        return ConversationState.from_json(super().get(conversation_id).to_json())

    def save(self, state):
        if self.conflicts:
            self.conflicts -= 1
            raise ConversationConflict("someone else saved")
        super().save(state)


class OneTurnOneTraceTests(unittest.TestCase):
    def test_a_retried_turn_is_one_inbound_and_one_outcome(self):
        rt = runtime(Conflicting(conflicts=1), [say("First."), say("Second.")])
        answer = send(rt, "my battery won't charge")
        self.assertEqual((len(events(rt, "inbound")), len(events(rt, "outcome"))), (1, 1))
        [outcome] = events(rt, "outcome")
        self.assertEqual(outcome["handled_by"], answer.handled_by)
        self.assertIsInstance(outcome["duration_ms"], int)

    def test_the_busy_reply_closes_the_turn(self):
        rt = runtime(Conflicting(conflicts=2), [say("First."), say("Second.")])
        send(rt, "my battery won't charge")
        [outcome] = events(rt, "outcome")
        self.assertEqual((outcome["handled_by"], outcome["escalated"]), ("conversation_busy", False))
        self.assertEqual(len(events(rt, "inbound")), 1)

    def test_a_storage_outage_closes_the_turn(self):
        class Down(InMemoryConversationStore):
            def get(self, conversation_id):
                raise StoreUnavailable("MongoDB find_one failed")
        rt = runtime(Down())
        send(rt, "my battery won't charge")
        self.assertEqual(len(events(rt, "inbound")), 1)
        [outcome] = events(rt, "outcome")
        self.assertEqual((outcome["handled_by"], outcome["escalated"]), ("store_unavailable", True))

    def test_an_ordinary_turn_still_logs_one_of_each(self):
        rt = runtime(InMemoryConversationStore(), [say("Try another socket.")])
        send(rt, "my battery won't charge")
        self.assertEqual((len(events(rt, "inbound")), len(events(rt, "outcome"))), (1, 1))


class PinnedAgentTests(unittest.TestCase):
    def test_a_pinned_agent_is_followed_and_jev_is_not_asked(self):
        jev = ScriptedJev([JevDecision(answers=MOTOR_NARROW)])
        narrow = ScriptedClaude([say("From the narrow model.")])
        rt = runtime(InMemoryConversationStore(), [say("From the battery agent.")], jev=jev, narrow_llm=narrow)
        answer = send(rt, "the motor makes a grinding noise", pinned_agent="battery_support")
        self.assertEqual(answer.handled_by, "battery_support")
        self.assertEqual((jev.calls, narrow.requests), ([], []))
        self.assertEqual([e["path"] for e in events(rt, "turn_path")], ["full"])


class ModeNameTests(unittest.TestCase):
    def test_one_name_for_one_list(self):
        self.assertFalse(hasattr(llm, "MODES"))
        self.assertEqual(llm.SINGLE_MODEL_MODES, ("offline", "anthropic", "bedrock"))
        self.assertTrue(set(llm.SINGLE_MODEL_MODES) < set(config.MODES))


class BoundedMemoryStoreTests(unittest.TestCase):
    def fill(self, store, cid):
        state = store.get(cid)
        state.user_key, state.turns = "PHONE#+919876543210", 1
        store.save(state)
        store.record_turn(state, inbound("x", cid), reply("y", cid), summary(cid, started_at=state.started_at))

    def test_the_least_recently_used_conversation_goes_first_and_whole(self):
        store = InMemoryConversationStore(max_conversations=2)
        self.fill(store, "a")
        self.fill(store, "b")
        store.get("a")  # a is now the most recently used
        self.fill(store, "c")
        self.assertEqual(len(store), 2)
        self.assertEqual(store.transcript("b"), [])
        self.assertNotIn("b", [s.conversation_id for s in store.recent_summaries("PHONE#+919876543210")])
        self.assertEqual(len(store.transcript("a")), 2)
        self.assertEqual(len(store.transcript("c")), 2)

    def test_the_default_bound_is_generous(self):
        self.assertGreaterEqual(InMemoryConversationStore().max_conversations, 10_000)


if __name__ == "__main__":
    unittest.main()
