"""Edge cases found by the review of the merge into feat/integration.

Each test reproduces one finding and failed before its fix. Numbers refer to
the review of merge commit dae0b7e (2026-09-29).
"""

import importlib
import os
import unittest
from datetime import date
from unittest import mock

import mongomock
from fastapi.testclient import TestClient

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import Attachment
from emotorad_ai.conversation import (
    HISTORY_TURNS,
    ConversationConflict,
    ConversationState,
    InMemoryConversationStore,
    StoreUnavailable,
    _is_customer_turn,
    render_transcript,
)
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import ScriptedClaude, call_tool, say, to_openai_messages
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import OpenRouterUnavailable
from emotorad_ai.runtime import Runtime
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, build_registry
from emotorad_ai.tools.verification import REQUEST_IDENTITY_VERIFICATION, VERIFY_IDENTITY, VerificationStore
from tests.store_contract import inbound, reply

TODAY = date(2026, 7, 28)
NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}
TICKET_CALL = call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                                "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1")
OLD_LOOKUP = {"data": {"frame_number": "A", "in_warranty": True}}
NEW_LOOKUP = {"data": {"frame_number": "B", "in_warranty": False}}


class Sequence:
    """A reply model that plays responses and raises errors, in order."""

    model = "anthropic/claude-haiku-4.5"

    def __init__(self, *steps):
        self.steps = list(steps)

    def create(self, system, messages, tools):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def runtime(store, llm, jev=None, narrow_llm=None, registry=None):
    registry = registry or build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path=""), registry=registry, llm=llm, log=EventLog(path=None),
                   resolver=IdentityResolver(registry), conversations=store, jev=jev, narrow_llm=narrow_llm)


def send(rt, text, cid="c1"):
    return rt.handle(WebsiteChatAdapter(rt.resolver).to_message({"conversation_id": cid, "session_token": "sess-ananya", "text": text}))


def long_history(state, turns=HISTORY_TURNS + 2):
    for i in range(turns):
        state.history += [{"role": "user", "content": "earlier %d" % i},
                          {"role": "assistant", "content": [{"type": "text", "text": "reply %d" % i}]}]
    state.turns = turns
    state.route_to("battery_support")


class CopyingStore(InMemoryConversationStore):
    """Hands out copies, as a database does; the first `conflicts` saves lose
    to another server, which meanwhile saved `other_coverage`."""

    def __init__(self, conflicts=0, other_coverage=None):
        super().__init__()
        self.conflicts, self.other_coverage = conflicts, other_coverage

    def get(self, conversation_id):
        return ConversationState.from_json(super().get(conversation_id).to_json())

    def save(self, state):
        if self.conflicts:
            self.conflicts -= 1
            if self.other_coverage is not None:
                super().get(state.conversation_id).coverage_result = self.other_coverage
            raise ConversationConflict("someone else saved")
        super().save(state)


class TicketThenOutageTests(unittest.TestCase):
    """Review issue 1: a ticket raised before the full agent's model failed."""

    def test_the_ticket_is_kept_and_quoted_on_the_handover(self):
        store = InMemoryConversationStore()
        rt = runtime(store, Sequence(TICKET_CALL, OpenRouterUnavailable("down")))
        answer = send(rt, "my battery won't charge")
        self.assertEqual((answer.handled_by, answer.escalated, answer.ticket_id), ("llm_error", True, "EM-00001"))
        self.assertIn("EM-00001", answer.text)
        self.assertEqual(store.get("c1").ticket_id, "EM-00001")
        self.assertIn("transcript", rt.registry.tickets.tickets["EM-00001"])


class ConflictMergeTests(unittest.TestCase):
    def test_a_newer_lookup_by_the_other_server_survives_when_this_turn_did_none(self):
        """Review issue 2: the stale lookup this turn loaded must not win."""
        store = CopyingStore(other_coverage=NEW_LOOKUP)
        state = store.get("c1")
        state.coverage_result = OLD_LOOKUP
        store.save(state)
        store.conflicts = 1
        send(runtime(store, ScriptedClaude([TICKET_CALL, say("Your reference is EM-00001.")])), "my battery won't charge")
        self.assertEqual(store.get("c1").coverage_result, NEW_LOOKUP)

    def test_this_turns_own_lookup_wins_when_it_did_one(self):
        rt = runtime(InMemoryConversationStore(), ScriptedClaude([]))
        fresh = rt.conversations.get("c1")
        fresh.coverage_result = OLD_LOOKUP
        ours = ConversationState(conversation_id="c1", coverage_result=NEW_LOOKUP)
        self.assertEqual(rt._merge_onto_fresh(ours, [], reply("x"), looked_up=True).coverage_result, NEW_LOOKUP)
        fresh.coverage_result = OLD_LOOKUP
        self.assertEqual(rt._merge_onto_fresh(ours, [], reply("x"), looked_up=False).coverage_result, OLD_LOOKUP)

    def test_a_merge_past_the_history_window_keeps_the_turn_once(self):
        """Review issue 4: the merge after a conflict once history is past the window."""
        store = CopyingStore()
        state = store.get("c1")
        long_history(state)
        store.save(state)
        store.conflicts = 1
        send(runtime(store, ScriptedClaude([TICKET_CALL, say("Your reference is EM-00001.")])), "my battery won't charge")
        history = store.get("c1").history
        sent = [e for e in history if e.get("role") == "user" and "my battery won't charge" in str(e.get("content"))]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sum(1 for e in history if _is_customer_turn(e)), HISTORY_TURNS)


class SideEffectTests(unittest.TestCase):
    """Review issue 4 and minor 5: verification tools count as side effects,
    and a code that failed to verify was still spent."""

    def test_sending_a_code_and_trying_one_are_side_effects_even_when_it_fails(self):
        rt = runtime(InMemoryConversationStore(), ScriptedClaude([]), registry=build_registry(today=TODAY, verification=VerificationStore()))
        mark = len(rt.log.events)
        rt.log.tool_call("c1", REQUEST_IDENTITY_VERIFICATION, {}, {"data": {"sent": True}})
        rt.log.tool_call("c1", VERIFY_IDENTITY, {"code": "000000"}, {"error": {"code": "wrong_code", "message": "no"}})
        self.assertEqual([w["tool"] for w in rt._side_effects_since(mark, "c1")], [REQUEST_IDENTITY_VERIFICATION, VERIFY_IDENTITY])

    def test_an_openrouter_error_is_logged_with_its_code_as_error(self):
        rt = runtime(InMemoryConversationStore(), ScriptedClaude([say("From the full agent.")]),
                     jev=ScriptedJev([JevDecision(answers=NARROW)]), narrow_llm=Sequence(OpenRouterUnavailable("down")))
        send(rt, "my battery won't charge")
        [error] = [e for e in rt.log.events if e["event"] == "llm_error"]
        self.assertEqual(error["error"], "unavailable")


class OpenRouterPhotoTests(unittest.TestCase):
    """Review issue 3: a photo must reach an OpenRouter model, not vanish."""

    def test_a_photo_with_text_becomes_an_image_part_beside_the_text(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        ]}]
        self.assertEqual(to_openai_messages("S", messages)[-1], {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ]})

    def test_a_photo_on_its_own_is_still_sent(self):
        messages = [{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://x.test/p.jpg"}}]}]
        self.assertEqual(to_openai_messages("S", messages)[-1],
                         {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x.test/p.jpg"}}]})

    def test_a_pdf_is_named_rather_than_dropped_and_plain_text_is_unchanged(self):
        pdf = {"role": "user", "content": [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}}]}
        [part] = to_openai_messages("S", [pdf])[-1]["content"]
        self.assertEqual(part["type"], "text")
        self.assertIn("PDF", part["text"])
        text_only = {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        self.assertEqual(to_openai_messages("S", [text_only])[-1], {"role": "user", "content": "a\nb"})


class MongoPhotoTrimTests(unittest.TestCase):
    """Minor 6: one large photo must not push every earlier turn out."""

    def test_old_photo_data_is_dropped_before_any_turn_is(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        store = MongoConversationStore(db, max_state_bytes=6_000)
        state = store.get("c1")
        photo = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "A" * 8_000}}
        state.history = [
            {"role": "user", "content": [{"type": "text", "text": "battery dead, here it is"}, photo]},
            {"role": "assistant", "content": [{"type": "text", "text": "Thanks."}]},
            {"role": "user", "content": "the light is red"},
            {"role": "assistant", "content": [{"type": "text", "text": "Ok."}]},
        ]
        store.save(state)
        saved = store.get("c1").history
        self.assertEqual(len(saved), 4)  # nothing the customer typed was dropped
        self.assertEqual(saved[0]["content"][0]["text"], "battery dead, here it is")
        self.assertNotIn("A" * 100, str(saved))


class TranscriptMarkerTests(unittest.TestCase):
    """Minor 8: a photo-only message must not read as an empty line on a ticket."""

    def test_attachments_are_named_on_the_ticket_transcript(self):
        store = InMemoryConversationStore()
        state = store.get("c1")
        state.turns = 1
        store.record_turn(state, inbound("", attachments=[Attachment(kind="image", url="data:image/jpeg;base64,AAAA")]), reply("Thanks."))
        self.assertIn("Customer: [photo]", render_transcript(store.transcript("c1")))


class RedactionTests(unittest.TestCase):
    """Minor 13: a signed link's credential never reaches the log."""

    def test_the_signature_of_a_signed_link_is_redacted(self):
        log = EventLog(path=None)
        url = "https://emotorad-ai-stage-media.s3.ap-south-1.amazonaws.com/assets/x.png?X-Amz-Algorithm=AWS4&X-Amz-Signature=abc123"
        log.tool_call("c1", "send_guide_media", {"key": "x"}, {"data": {"media": [{"url": url}]}})
        logged = log.events[-1]["result"]["data"]["media"][0]["url"]
        self.assertTrue(logged.startswith("https://emotorad-ai-stage-media.s3.ap-south-1.amazonaws.com/assets/x.png"))
        self.assertNotIn("abc123", logged)


def api_on(conversations_store=None, media_store=None):
    env = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory",
           "EMOTORAD_AI_MEDIA_BUCKET": "fake" if media_store else "", "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": ""}
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=media_store):
        import emotorad_ai.api as api
        api = importlib.reload(api)
    if conversations_store is not None:
        api.runtime.conversations = conversations_store
    return api


class ApiTests(unittest.TestCase):
    def test_the_web_chats_cluster_and_pin_reach_mongodb_through_post_message(self):
        """Review issue 4: through the real endpoint, not the runtime alone."""
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        api = api_on(MongoConversationStore(db))
        r = TestClient(api.app).post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                                       "text": "the motor makes a noise", "agent": "motor_support"})
        self.assertEqual(r.status_code, 200, r.text)
        saved = MongoConversationStore(db).get("c1")
        self.assertEqual(saved.agent, "motor_support")
        self.assertIsNotNone(saved.cluster_id)

    def test_an_upload_when_the_store_is_down_is_a_503_not_a_500(self):
        """Minor 11."""
        from tests.test_api_uploads import _Store

        class Down(InMemoryConversationStore):
            def peek(self, conversation_id):
                raise StoreUnavailable("MongoDB find_one failed")

        api = api_on(Down(), media_store=_Store())
        r = TestClient(api.app).post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
                                                       "mime_type": "image/png", "size_bytes": 9})
        self.assertEqual(r.status_code, 503, r.text)


if __name__ == "__main__":
    unittest.main()
