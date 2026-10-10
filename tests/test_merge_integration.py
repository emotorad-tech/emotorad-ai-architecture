"""Behaviour the merge of feat/live-eval into feat/integration created.

Neither side had these on its own: they come from putting feat/integration's
attachments, history window and web-chat API together with our durable store,
graph runtime and permanent transcript.
"""

import unittest
from dataclasses import replace
from datetime import date

import mongomock

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import Attachment
from emotorad_ai.conversation import HISTORY_TURNS, InMemoryConversationStore, _is_customer_turn, recorded_url
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import OpenRouterUnavailable
from emotorad_ai.runtime import Runtime
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes
from emotorad_ai.tools.mocks import build_registry
from tests.store_contract import inbound, reply

TODAY = date(2026, 7, 28)
NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}


def mongo_store():
    db = mongomock.MongoClient()["emotorad_ai"]
    ensure_indexes(db)
    return db, MongoConversationStore(db)


def runtime(store, replies=(), jev=None, narrow_llm=None):
    registry = build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(replies)),
                   log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=store,
                   jev=jev, narrow_llm=narrow_llm)


def message(rt, text, cid="c1", **meta):
    m = WebsiteChatAdapter(rt.resolver).to_message({"conversation_id": cid, "session_token": "sess-ananya", "text": text})
    return replace(m, entry_metadata=dict(m.entry_metadata, **meta)) if meta else m


class Raising:
    model = "anthropic/claude-haiku-4.5"

    def create(self, system, messages, tools):
        raise OpenRouterUnavailable("down")


class PeekTests(unittest.TestCase):
    def test_peek_never_creates_a_conversation(self):
        for store in (InMemoryConversationStore(), mongo_store()[1]):
            with self.subTest(type(store).__name__):
                self.assertIsNone(store.peek("c1"))
                state = store.get("c1")
                state.turns = 1
                store.save(state)
                self.assertEqual(store.peek("c1").turns, 1)
                self.assertIsNone(store.peek("c2"))


class PermanentRecordTests(unittest.TestCase):
    def test_no_inline_photo_or_signed_link_is_kept_in_the_transcript(self):
        self.assertEqual(recorded_url("data:image/jpeg;base64,/9j/AAAA"), "data:(inline, not kept)")
        self.assertEqual(recorded_url("https://x.s3.amazonaws.com/k.jpg?X-Amz-Signature=abc"), "https://x.s3.amazonaws.com/k.jpg")
        for store in (InMemoryConversationStore(), mongo_store()[1]):
            with self.subTest(type(store).__name__):
                state = store.get("c1")
                state.turns = 1
                photo = Attachment(kind="image", url="data:image/jpeg;base64," + "A" * 5000)
                store.record_turn(state, inbound("here", attachments=[photo]), reply("Thanks."))
                [customer, _] = store.transcript("c1")
                self.assertEqual(customer.attachments, ({"kind": "image", "url": "data:(inline, not kept)"},))

    def test_the_mongodb_trim_counts_a_photo_message_as_a_turn(self):
        photo_turn = {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image", "source": {}}]}
        tool_results = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "{}"}]}
        self.assertTrue(_is_customer_turn(photo_turn))
        self.assertFalse(_is_customer_turn(tool_results))
        db, store = mongo_store()
        store._max_state_bytes = 2_000
        state = store.get("c1")
        state.history = [photo_turn, {"role": "assistant", "content": [{"type": "text", "text": "x" * 1500}]},
                         photo_turn, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
        store.save(state)
        self.assertEqual(store.get("c1").history[0], photo_turn)  # cut at the second photo turn, not left whole
        self.assertEqual(len(store.get("c1").history), 2)


class WebChatStateTests(unittest.TestCase):
    def test_the_cluster_and_a_pinned_agent_survive_to_the_next_turn_on_mongodb(self):
        db, store = mongo_store()
        rt = runtime(store, [say("Checking the motor."), say("Still the motor.")])
        rt.handle(message(rt, "hello", cluster_id="cl-1", pinned_agent="motor_support"))
        saved = MongoConversationStore(db).get("c1")
        self.assertEqual((saved.cluster_id, saved.agent), ("cl-1", "motor_support"))
        second = runtime(MongoConversationStore(db), [say("Still the motor.")]).handle(message(rt, "and now?"))
        self.assertEqual(second.handled_by, "motor_support")

    def test_an_unknown_pin_is_cleared_rather_than_followed(self):
        rt = runtime(InMemoryConversationStore(), [say("Ok.")])
        rt.handle(message(rt, "my battery won't charge", pinned_agent="no_such_agent"))
        self.assertTrue(any(e["event"] == "unknown_agent_cleared" for e in rt.log.events))


class HistoryWindowTests(unittest.TestCase):
    def test_a_narrow_fallback_past_the_window_shows_the_message_once(self):
        store = InMemoryConversationStore()
        state = store.get("c1")
        for i in range(HISTORY_TURNS + 2):
            state.history += [{"role": "user", "content": "earlier %d" % i},
                              {"role": "assistant", "content": [{"type": "text", "text": "reply %d" % i}]}]
        state.turns = HISTORY_TURNS + 2
        state.route_to("battery_support")
        rt = runtime(store, [say("From the full agent.")], jev=ScriptedJev([JevDecision(answers=NARROW)]), narrow_llm=Raising())
        answer = rt.handle(message(rt, "my battery won't charge"))
        self.assertEqual(answer.handled_by, "battery_support")
        history = store.get("c1").history
        sent = [e for e in history if e.get("role") == "user" and "my battery won't charge" in str(e.get("content"))]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sum(1 for e in history if _is_customer_turn(e)), HISTORY_TURNS)


class JevInputTests(unittest.TestCase):
    def test_jev_reads_the_customers_words_not_the_video_models(self):
        from emotorad_ai.attachments import VIDEO_DESCRIPTION_LABEL
        from emotorad_ai.decisions import build_state
        history = [{"role": "user", "content": [
            {"type": "text", "text": "it makes this noise"},
            {"type": "text", "text": VIDEO_DESCRIPTION_LABEL + ", written by a video model]: a grinding sound near the rear hub"},
        ]}]
        state = build_state("what is it?", history, "website")
        self.assertIn("it makes this noise", str(state))
        self.assertNotIn("grinding sound near the rear hub", str(state))


class NarrowIdentityTests(unittest.TestCase):
    def test_the_narrow_agent_can_verify_a_web_visitor_like_the_others(self):
        from emotorad_ai.tools.verification import REQUEST_IDENTITY_VERIFICATION, VerificationStore
        registry = build_registry(today=TODAY, verification=VerificationStore())
        narrow = ScriptedClaude([say("Let me check.")])
        rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([]), log=EventLog(path=None),
                     resolver=IdentityResolver(registry), conversations=InMemoryConversationStore(),
                     jev=ScriptedJev([JevDecision(answers=NARROW)]), narrow_llm=narrow, self_service_identity=True)
        rt.handle(message(rt, "my battery won't charge"))
        offered = [tool["name"] for tool in narrow.requests[0]["tools"]]
        self.assertIn(REQUEST_IDENTITY_VERIFICATION, offered)


class ConflictMergeTests(unittest.TestCase):
    def test_the_merge_after_a_conflict_keeps_orders_codes_and_the_lookup(self):
        store = InMemoryConversationStore()
        rt = runtime(store)
        fresh = store.get("c1")
        fresh.placed_order_ids, fresh.consumed_codes = ["RO-1000001"], ["123456"]
        ours = replace(fresh, placed_order_ids=["RO-1000002"], consumed_codes=["654321"],
                       coverage_result={"data": {"in_warranty": True}}, cluster_id="cl-1", history=[], transitions=[])
        # This turn made its own lookup, so it is the newer one; the stale
        # case is in tests/test_edge_cases_after_merge.py.
        merged = rt._merge_onto_fresh(ours, [], reply("Placed.", ticket_id=None), looked_up=True)
        self.assertEqual(merged.placed_order_ids, ["RO-1000001", "RO-1000002"])
        self.assertEqual(merged.consumed_codes, ["123456", "654321"])
        self.assertEqual(merged.coverage_result, {"data": {"in_warranty": True}})
        self.assertEqual(merged.cluster_id, "cl-1")


if __name__ == "__main__":
    unittest.main()
