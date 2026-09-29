"""Gaps a mutation audit of the web chat, the turn and the log redaction found.

Each test below failed against a small, plausible fault that the whole suite
otherwise let through (the mutant named in its docstring), and passes on the
code as it is. Audit of 2026-09-29 on 478a1ae.
"""

import importlib
import os
import unittest
from dataclasses import replace
from datetime import date
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.adapters import VoiceAdapter, WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY, Q_WARRANTY
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose, yes
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog, redact_fields, redact_pii
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, build_registry
from emotorad_ai.tools.registry import IdempotencyStore
from emotorad_ai.wiring import Stores
from tests.test_api_uploads import _Store

TODAY = date(2026, 7, 28)


def fresh_api(media_store=None, **env):
    """The app as a deployment imports it, with this environment."""
    settings = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory", "EMOTORAD_AI_MEDIA_BUCKET": "fake" if media_store else "",
                "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
    settings.update(env)
    with mock.patch.dict(os.environ, settings), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=media_store):
        import emotorad_ai.api as api

        return importlib.reload(api)


def runtime(store=None, replies=()):
    registry = build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=ScriptedClaude(list(replies)),
                   log=EventLog(path=None), resolver=IdentityResolver(registry),
                   conversations=store if store is not None else InMemoryConversationStore())


def web(rt, text, cid="c1", session="sess-ananya", **meta):
    message = WebsiteChatAdapter(rt.resolver).to_message({"conversation_id": cid, "session_token": session, "text": text})
    if meta:
        message = replace(message, entry_metadata=dict(message.entry_metadata, **meta))
    return rt.handle(message)


PRESIGN = {"tree": "customers", "mime_type": "image/png", "size_bytes": 9}


class WebChatOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api(media_store=_Store())
        self.client = TestClient(self.api.app)

    def test_an_anonymous_visitor_can_send_a_video_after_chatting(self):
        """Mutant A04: /uploads refuses any existing conversation with no
        recorded cluster. A cookie-only chat records none, so the visitor who
        chats first and then sends the clip would be refused their own upload."""
        r = self.client.post("/message", json={"conversation_id": "c-anon", "em_aid": "visitor-abc", "text": "hello"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.api.runtime.conversations.peek("c-anon").cluster_id)
        r = self.client.post("/uploads", json=dict(PRESIGN, em_aid="visitor-abc", conversation_id="c-anon",
                                                   mime_type="video/mp4", size_bytes=1024))
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_presign_for_a_new_conversation_does_not_start_it(self):
        """Mutant A17: the ownership check falls back to `get`, which creates
        the conversation. The 503 test cannot see it (its store's `get`
        works); a presign must leave no state behind for an id nobody has
        chatted on."""
        r = self.client.post("/uploads", json=dict(PRESIGN, session_token="sess-ananya", conversation_id="c-new"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.api.runtime.conversations.peek("c-new"))

    def test_a_message_from_another_session_does_not_take_the_conversation_over(self):
        """Mutant R01: the cluster is overwritten by each turn's sender. Then
        anyone who posts one message into a conversation id owns it: the
        stranger may presign under it and the customer who started it may not."""
        for session in ("sess-ananya", "sess-rohit"):
            r = self.client.post("/message", json={"conversation_id": "c1", "session_token": session, "text": "hello"})
            self.assertEqual(r.status_code, 200, r.text)
        owner = self.client.post("/uploads", json=dict(PRESIGN, session_token="sess-ananya", conversation_id="c1"))
        stranger = self.client.post("/uploads", json=dict(PRESIGN, session_token="sess-rohit", conversation_id="c1"))
        self.assertEqual((owner.status_code, stranger.status_code), (200, 403), (owner.text, stranger.text))

    def test_the_pinned_agent_is_the_one_that_answers_over_http(self):
        """Mutant A07: /message drops `agent` before the turn. Motor words
        with the battery agent pinned tell the pin from triage's own choice."""
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                               "text": "the motor makes a grinding noise", "agent": "battery_support"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["handled_by"], "battery_support")
        self.assertEqual(self.api.runtime.conversations.peek("c1").agent, "battery_support")


class HealthTests(unittest.TestCase):
    def test_health_names_the_live_mode_and_store_not_the_defaults(self):
        """Mutants A13 and A16: /health reports a fixed "memory" store or a
        fixed "offline" mode. Every other health test runs offline in memory,
        which is exactly what a hard-coded answer says."""
        stores = Stores(conversations=InMemoryConversationStore(), idempotency=IdempotencyStore())
        with mock.patch("emotorad_ai.wiring.build_stores", return_value=stores):
            api = fresh_api(EMOTORAD_AI_MODE="openrouter", EMOTORAD_STORE="mongodb", OPENROUTER_API_KEY="sk-or-v1-FAKE-FOR-TESTS")
        health = api.health()
        self.assertEqual((health["mode"], health["store"]), ("openrouter", "mongodb"))


class PinnedAgentTests(unittest.TestCase):
    def test_a_new_pin_moves_a_conversation_already_on_another_agent(self):
        """Mutant R02: the pin is applied only while no agent is set. A tester
        who switches agent in the sidebar would stay on the first one."""
        rt = runtime(replies=[say("Motor agent here."), say("Battery agent here.")])
        self.assertEqual(web(rt, "the motor makes a grinding noise", pinned_agent="motor_support").handled_by, "motor_support")
        answer = web(rt, "and the battery drains fast", pinned_agent="battery_support")
        self.assertEqual(answer.handled_by, "battery_support")


class MemoryKeyTests(unittest.TestCase):
    def test_a_caller_id_gets_no_history_of_its_own(self):
        """Mutant R04: memory keyed on any phone, proven or not. Caller ID is
        spoofable, so a call from Ananya's number must neither be filed in
        her history nor read it back."""
        store = InMemoryConversationStore()
        web(runtime(store, [say("Ok.")]), "my battery won't charge", cid="web-1")
        rt = runtime(store, [say("Hello caller.")])
        caller = VoiceAdapter(rt.resolver).to_message({"conversation_id": "call-1", "caller_id": "+919876543210",
                                                        "transcript": "my battery won't charge"})
        rt.handle(caller)
        self.assertIsNone(store.peek("call-1").user_key)
        self.assertEqual([s.conversation_id for s in store.recent_summaries("PHONE#+919876543210")], ["web-1"])

    def test_memory_being_unreachable_costs_the_memory_not_the_answer(self):
        """Mutant R07: the memory read's StoreUnavailable is not caught in
        the prepare step. The turn's backstop then hands the customer over,
        when all that was lost was a line of history."""

        class MemoryDown(InMemoryConversationStore):
            def recent_summaries(self, user_key, limit=3, exclude=None):
                raise StoreUnavailable("MongoDB find failed")

        rt = runtime(MemoryDown(), [say("Try another socket.")])
        answer = web(rt, "my battery won't charge")
        self.assertEqual((answer.handled_by, answer.escalated), ("battery_support", False))
        self.assertEqual(len([e for e in rt.log.events if e["event"] == "memory_unavailable"]), 1)


class PostCheckBookkeepingTests(unittest.TestCase):
    """What a blocked reply leaves behind: the ticket, and one copy of the turn."""

    CLAIM = "Good news: your battery is still covered."
    TICKET = call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                               "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1")

    def test_a_coverage_block_keeps_the_ticket_the_turn_raised(self):
        """Mutant R27: the coverage block drops the turn's ticket. The ticket
        exists either way; without its id the conversation forgets it, the
        customer is not told it, and the transcript never reaches it."""
        rt = runtime(replies=[self.TICKET, say(self.CLAIM)])
        rt.conversations.get("c1").route_to("battery_support")
        # The customer sent a photo earlier: a fault ticket needs evidence (test_evidence_before_ticket).
        rt.conversations.get("c1").evidence_seen = True
        answer = web(rt, "my battery won't charge")
        self.assertEqual(answer.handled_by, "guardrail:coverage_post_check")
        self.assertIsNotNone(answer.ticket_id)
        self.assertIn(answer.ticket_id, rt.registry.tickets.tickets)
        self.assertEqual(rt.conversations.peek("c1").ticket_id, answer.ticket_id)

    def test_a_blocked_turn_is_in_the_history_once(self):
        """Mutant R25: `_finish` appends the customer's message even when the
        agent loop already did. The model then sees every blocked message
        twice on every later turn."""
        rt = runtime(replies=[say(self.CLAIM)])
        rt.conversations.get("c1").route_to("battery_support")
        web(rt, "my battery won't charge, is it covered?")
        said = [e for e in rt.conversations.peek("c1").history if e["role"] == "user"
                and "is it covered?" in str(e["content"])]
        self.assertEqual(len(said), 1)

    def test_a_clip_that_trips_the_safety_gate_is_in_the_history_once(self):
        """Mutant R28: the safety branch writes the described clip into the
        history and `_finish` then writes the turn again."""
        rt = runtime()
        message = WebsiteChatAdapter(rt.resolver).to_message({
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "video attached",
            "attachments": [{"kind": "video", "url": "s3://customers/x/y/videos/z.mp4", "mime_type": "video/mp4",
                             "summary": "0:03 white smoke rising from the battery pack."}],
        })
        self.assertEqual(rt.handle(message).handled_by, "guardrail:battery_safety")
        history = rt.conversations.peek("c1").history
        self.assertEqual([e["role"] for e in history], ["user", "assistant"])


class CoverageFactTests(unittest.TestCase):
    """The coverage post-check judges a claim against the conversation's
    latest successful lookup."""

    def test_a_lookup_prefetched_on_the_narrow_path_is_remembered_for_later_turns(self):
        """Mutant R17: only lookups the model made are remembered. A lookup
        Jev prefetched satisfies its own turn, and the next turn's correct
        claim is blocked and the customer handed over."""
        narrow_with_lookup = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9),
                              Q_WARRANTY: yes(0.9)}
        unsure = {Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose("none", 0.5)}
        registry = build_registry(today=TODAY)
        narrow = ScriptedClaude([say("Try another socket first."), say("Good news: your battery is still covered.")])
        rt = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=ScriptedClaude([]),
                     narrow_llm=narrow, jev=ScriptedJev([JevDecision(answers=narrow_with_lookup), JevDecision(answers=unsure)]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry))
        first = web(rt, "my battery won't charge")
        self.assertIn(LOOKUP_WARRANTY_RECORD, first.metadata["tool_calls"])
        second = web(rt, "still nothing, is it covered?")
        self.assertEqual(second.handled_by, "narrow_support")

    def test_a_failed_lookup_does_not_erase_the_earlier_one(self):
        """Mutant R18: a lookup that errored replaces the remembered result.
        An outage on turn two then blocks a claim turn one already proved."""
        down = {"now": False}

        def source(phone):
            if down["now"]:
                raise RuntimeError("OMS timed out")
            return fixtures.WARRANTY_RECORDS.get(phone)

        registry = build_registry(today=TODAY, warranty_source=source)
        rt = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry,
                     llm=ScriptedClaude([call_tool(LOOKUP_WARRANTY_RECORD, {}, "toolu_1"), say("Let me check the charger."),
                                         call_tool(LOOKUP_WARRANTY_RECORD, {}, "toolu_2"), say("Good news: your battery is still covered.")]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry))
        rt.conversations.get("c1").route_to("battery_support")
        web(rt, "my battery won't charge")
        down["now"] = True
        answer = web(rt, "is it covered?")
        self.assertEqual(answer.handled_by, "battery_support")


class CodeRedactionTests(unittest.TestCase):
    """The log hides a field named `code` by its name. The existing tests use
    a six-digit code, which the bare-digits rule hides anyway, so they pass
    whatever the name rule does. A code typed with spaces is caught by the
    name alone."""

    def logged(self, arguments, result):
        return EventLog(path=None).emit("tool_call", "c1", tool="verify_identity", arguments=arguments, result=result)

    def test_a_code_is_hidden_by_its_name_wherever_it_sits(self):
        """Mutants O02 (no `code` is ever hidden by name) and O03 (a `code`
        under any parent is exempt, not only an error envelope's)."""
        event = self.logged({"code": "48 29 13"}, {"data": {"code": "48 29 13", "details": {"code": "48 29 13"}}})
        self.assertEqual(event["arguments"]["code"], "[redacted]")
        self.assertEqual(event["result"]["data"]["code"], "[redacted]")
        self.assertEqual(event["result"]["data"]["details"]["code"], "[redacted]")
        self.assertNotIn("48 29 13", str(event))

    def test_only_an_error_envelopes_own_code_stays_readable(self):
        event = self.logged({}, {"error": {"code": "verification_failed", "details": {"code": "48 29 13"}}})
        self.assertEqual(event["result"]["error"]["code"], "verification_failed")
        self.assertEqual(event["result"]["error"]["details"]["code"], "[redacted]")


class FieldRedactionTests(unittest.TestCase):
    """A spaced number is invisible to the phone pattern, so these are held by
    the field name alone."""

    def test_a_sensitive_field_name_is_matched_whatever_its_case(self):
        """Mutant O07: `Phone` from an upstream API is not `phone`."""
        self.assertEqual(redact_fields({"Phone": "98765 43210", "MOBILE": "98765 43210"}),
                         {"Phone": "[redacted]", "MOBILE": "[redacted]"})

    def test_a_contact_the_customer_stated_is_hidden_however_it_was_written(self):
        """Mutant O15: `stated_contact` dropped from the sensitive names.
        `raise_intake_ticket` takes it "exactly as given", which is rarely a
        clean number the phone pattern would catch."""
        event = EventLog(path=None).emit("tool_call", "c1", tool="raise_intake_ticket",
                                         arguments={"stated_contact": "ring my brother on 98765 43210 after six"})
        self.assertEqual(event["arguments"]["stated_contact"], "[redacted]")

    def test_every_item_of_a_list_under_a_sensitive_name_is_hidden(self):
        """Mutant O08: the field name is lost on the way into a list."""
        self.assertEqual(redact_fields({"phone": ["98765 43210", "98123-45678"]}), {"phone": ["[redacted]", "[redacted]"]})

    def test_an_inline_image_is_dropped_whatever_the_scheme_case(self):
        """Mutant O12: `DATA:` is the same URI as `data:` (RFC 2397 schemes are
        case-insensitive), and a megabyte of it on one line is the photo."""
        self.assertEqual(redact_fields({"url": "DATA:image/jpeg;BASE64," + "A" * 64}), {"url": "[attachment]"})


class FreeTextRedactionTests(unittest.TestCase):
    def test_a_message_that_starts_with_a_number_is_kept(self):
        """Mutant O10: the bare-digits rule unanchored. It exists for a code
        or a pincode typed alone; a sentence that opens with a figure is the
        symptom, and hiding it hides what the customer said."""
        self.assertEqual(redact_pii("1500 km and the range has dropped by half"), "1500 km and the range has dropped by half")
        self.assertEqual(redact_pii(" 560001 "), "[6 digits]")


class DescriptionSafetyTests(unittest.TestCase):
    def test_a_hazard_followed_by_a_negated_other_hazard_still_stops_the_turn(self):
        """Mutant G02: a negation after the term counts without the label
        colon. "Swollen, not cracked" would then clear a swollen pack, and
        the clip would go to the model instead of the safety team."""
        rt = runtime()  # any model call would fail: ScriptedClaude has nothing queued
        message = WebsiteChatAdapter(rt.resolver).to_message({
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "video attached",
            "attachments": [{"kind": "video", "url": "s3://customers/x/y/videos/z.mp4", "mime_type": "video/mp4",
                             "summary": "0:02 the battery pack is swollen, not cracked."}],
        })
        reply = rt.handle(message)
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertEqual(rt.llm.requests, [])
        self.assertIn("the battery pack is swollen, not cracked", rt.registry.tickets.tickets[reply.ticket_id]["description"])


if __name__ == "__main__":
    unittest.main()
