"""Write receipts scoped by run, and the persona and run on the tool context
(spec 2026-10-05-zoho-desk-tickets-design.md, section 2; plan Task 1).

A conversation id can hold several runs: a thread that went quiet past its
working-state expiry and started again, or a second person on the same
browser after verify-first's restart_for. A key the model reuses in a new
run must raise a new ticket, never hand back the first run's.
"""

import unittest
from dataclasses import replace
from datetime import date

import mongomock

from emotorad_ai.agents.base import Agent, AgentDefinition
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY_SUPPORT
from emotorad_ai.config import Settings
from emotorad_ai.contract import Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.jev import JevDecision
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import MongoConversationStore, MongoIdempotencyStore, ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, MockTicketSystem, build_registry
from emotorad_ai.tools.registry import IdempotencyStore, ToolContext, ok
from emotorad_ai.tools.verification import REQUEST_IDENTITY_VERIFICATION
from tests.test_agent_and_runtime import make_runtime
from tests.test_agent_and_runtime import send as send_web
from tests.test_jev_runtime import NARROW_WITH_WARRANTY
from tests.test_jev_runtime import build as build_jev
from tests.test_jev_runtime import send as send_jev
from tests.test_runtime_persistence import runtime_on
from tests.test_runtime_persistence import send as send_text
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

TODAY = date(2026, 7, 28)
RUN_A = "2026-10-05T09:00:00.000000+00:00"
RUN_B = "2026-10-05T11:00:00.000000+00:00"
PHONE = "+919999999999"
TICKET = {"category": "battery_charging", "severity": "normal", "description": "LED stays off.",
          "idempotency_key": "k1"}


class RecordingReceipts(IdempotencyStore):
    """The in-memory receipt store, noting every key it is asked to claim."""

    def __init__(self):
        super().__init__()
        self.claimed = []

    def claim(self, key):
        self.claimed.append(key)
        return super().claim(key)


def spy_on(registry):
    """Every (tool, context) the registry is called with, in order."""
    calls, real = [], registry.call

    def call(name, arguments, context, **kwargs):
        calls.append((name, context))
        return real(name, arguments, context, **kwargs)

    registry.call = call
    return calls


def context(**fields):
    return ToolContext(conversation_id="c1", phone=PHONE, **fields)


class ToolContextTests(unittest.TestCase):
    def test_persona_and_run_default_to_unknown(self):
        bare = ToolContext(conversation_id="c1")
        self.assertIsNone(bare.persona)
        self.assertIsNone(bare.value_for("started_at"))

    def test_a_run_given_late_is_read_at_call_time(self):
        run = [RUN_A]
        late = ToolContext(conversation_id="c1", late={"started_at": lambda: run[0]})
        run[0] = RUN_B  # restart_for moved the run after the context was built
        self.assertEqual(late.value_for("started_at"), RUN_B)

    def test_a_run_set_directly_wins_over_a_late_one(self):
        both = ToolContext(conversation_id="c1", started_at=RUN_A, late={"started_at": lambda: RUN_B})
        self.assertEqual(both.value_for("started_at"), RUN_A)


class ReceiptKeyTests(unittest.TestCase):
    def setUp(self):
        self.receipts, self.tickets = RecordingReceipts(), MockTicketSystem()
        self.registry = build_registry(ticket_system=self.tickets, idempotency=self.receipts)

    def raise_ticket(self, ctx):
        return self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), ctx)

    def test_a_context_with_a_run_scopes_the_key_by_it(self):
        self.raise_ticket(context(started_at=RUN_A))
        self.assertEqual(self.receipts.claimed, ["c1:%s:create_support_ticket:k1" % RUN_A])

    def test_a_run_given_late_scopes_it_the_same_way(self):
        self.raise_ticket(context(late={"started_at": lambda: RUN_A}))
        self.assertEqual(self.receipts.claimed, ["c1:%s:create_support_ticket:k1" % RUN_A])

    def test_a_context_without_a_run_keeps_todays_key(self):
        # Verify-first's calls, the playground's and the smoke script's carry no run.
        self.raise_ticket(context())
        self.assertEqual(self.receipts.claimed, ["c1:create_support_ticket:k1"])

    def test_a_retry_in_the_same_run_returns_the_first_ticket(self):
        first = self.raise_ticket(context(started_at=RUN_A))
        again = self.raise_ticket(context(started_at=RUN_A))
        self.assertEqual(again, first)
        self.assertEqual(len(self.tickets.tickets), 1)

    def test_the_same_key_in_a_new_run_raises_a_new_ticket(self):
        first = self.raise_ticket(context(started_at=RUN_A))
        second = self.raise_ticket(context(started_at=RUN_B))
        self.assertNotEqual(first["data"]["ticket_id"], second["data"]["ticket_id"])
        self.assertEqual(len(self.tickets.tickets), 2)

    def test_a_read_tool_claims_nothing(self):
        self.registry.call(LOOKUP_WARRANTY_RECORD, {}, context(started_at=RUN_A))
        self.assertEqual(self.receipts.claimed, [])


class RunScopedReceiptErasureTests(unittest.TestCase):
    def test_erasing_a_conversation_still_removes_its_run_scoped_receipts(self):
        """The erasure pattern is `^<id>:`, and a run-scoped key still starts
        with it. Erasure itself is not changed by this plan."""
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        receipts = MongoIdempotencyStore(db)
        receipts.put("c1:%s:create_support_ticket:k1" % RUN_A, ok({"ticket_id": "EM-00001"}))
        receipts.put("c10:%s:create_support_ticket:k1" % RUN_A, ok({"ticket_id": "EM-00002"}))
        counts = MongoConversationStore(db).delete_conversation("c1")
        self.assertEqual(counts["idempotency_keys"], 1)
        self.assertEqual([d["_id"] for d in db["idempotency_keys"].find()],
                         ["c10:%s:create_support_ticket:k1" % RUN_A])


def probe_registry(seen):
    registry = build_registry(today=TODAY)

    @registry.register("probe", "Shows what the platform injected.", parameters={},
                       optional_injects=("persona", "started_at"))
    def probe(persona=None, started_at=None):
        seen.append((persona, started_at))
        return ok({"seen": True})

    return registry


PROBE_AGENT = AgentDefinition(
    name="probe_agent", tool_names=("probe",),
    build_system_prompt=lambda message, resolved, context="": "Test agent.", one_step=False,
)


class AgentContextTests(unittest.TestCase):
    def run_probe(self, persona, channel, facts=None):
        seen = []
        agent = Agent(PROBE_AGENT, probe_registry(seen), ScriptedClaude([call_tool("probe", {}, "toolu_1"), say("ok")]),
                      EventLog(path=None), Settings(log_path="", log_to_stdout=False))
        message = InboundMessage(conversation_id="c1", persona=persona, identity=Identity(), channel=channel,
                                 message_text="hello")
        agent.run(message, ResolvedIdentity(persona=persona, method="test"), [], facts=facts)
        return seen

    def test_a_customer_agents_tools_are_told_the_persona(self):
        self.assertEqual(self.run_probe("customer", "website_chat"), [("customer", None)])

    def test_a_dealer_agents_tools_are_told_theirs(self):
        self.assertEqual(self.run_probe("dealer", "dealer_app"), [("dealer", None)])

    def test_the_run_comes_from_the_facts(self):
        self.assertEqual(self.run_probe("customer", "website_chat", facts={"started_at": lambda: RUN_A}),
                         [("customer", RUN_A)])


class RuntimeContextTests(unittest.TestCase):
    def test_an_agent_turn_gives_tools_the_persona_and_this_run(self):
        runtime, adapter, _ = make_runtime([call_tool("probe", {}, "toolu_1"), say("ok")])
        seen = []

        @runtime.registry.register("probe", "test", parameters={}, optional_injects=("persona", "started_at"))
        def probe(persona=None, started_at=None):
            seen.append((persona, started_at))
            return ok({"seen": True})

        agent = runtime.agents[BATTERY_SUPPORT]
        agent.definition = replace(agent.definition, tool_names=tuple(agent.definition.tool_names) + ("probe",))
        runtime.conversations.get("conv-1").route_to(BATTERY_SUPPORT)
        send_web(runtime, adapter, "here")
        self.assertEqual(seen, [("customer", runtime.conversations.peek("conv-1").started_at)])

    def test_the_narrow_paths_prefetch_carries_them_too(self):
        runtime, adapter, *_ = build_jev([JevDecision(answers=NARROW_WITH_WARRANTY)],
                                         narrow=[say("Try another wall socket first.")])
        calls = spy_on(runtime.registry)
        send_jev(runtime, adapter, "my battery won't charge, is it under warranty")
        # The last lookup is the prefetch: identity resolution's own comes first.
        prefetch = [ctx for name, ctx in calls if name == LOOKUP_WARRANTY_RECORD][-1]
        self.assertEqual(prefetch.persona, "customer")
        self.assertEqual(prefetch.value_for("started_at"), runtime.conversations.peek("conv-1").started_at)

    def test_the_safety_ticket_carries_them_and_its_receipt_is_scoped_by_the_run(self):
        receipts = RecordingReceipts()
        runtime = runtime_on(InMemoryConversationStore(), [], registry=build_registry(today=TODAY, idempotency=receipts))
        calls = spy_on(runtime.registry)
        reply = send_text(runtime, "my battery is swollen")
        started = runtime.conversations.peek("conv-1").started_at
        [ctx] = [c for name, c in calls if name == CREATE_SUPPORT_TICKET]
        self.assertEqual((ctx.persona, ctx.value_for("started_at")), ("customer", started))
        self.assertEqual(receipts.claimed,
                         ["conv-1:%s:create_support_ticket:safety:conv-1:%s" % (started, started)])
        self.assertTrue(reply.ticket_id)
        self.assertEqual(runtime.llm.requests, [], "the safety branch never calls the model")


class NewPersonNewTicketTests(unittest.TestCase):
    """Review Focus: a second person on the same browser after restart_for,
    with a model that reuses its idempotency key."""

    def test_a_key_reused_after_restart_for_raises_the_second_person_a_new_ticket(self):
        now = [0.0]
        chat = Chat(replies=[
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"), say("I've raised a ticket for you."),
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_2"), say("I've raised a ticket for you."),
        ], clock=lambda: now[0])
        chat.verify()
        chat.state().evidence_seen = True  # a photo earlier: a fault ticket may be raised
        first = chat.say("2")
        first_run = chat.state().started_at

        now[0] += 12 * 60 * 60 + 1  # the first person's session expires
        chat.say("hello again")
        chat.say(ONE_BIKE[3:])
        chat.say(chat.code())
        self.assertNotEqual(chat.state().started_at, first_run, "restart_for began a new run")
        chat.state().evidence_seen = True
        chat.say("yes")
        second = chat.say("battery dead")

        self.assertEqual(first.ticket_id, "EM-00001")
        self.assertEqual(second.ticket_id, "EM-00002")
        tickets = chat.registry.tickets.tickets
        self.assertEqual((tickets["EM-00001"]["phone"], tickets["EM-00002"]["phone"]), (RIDER, ONE_BIKE))
        self.assertNotIn("EM-00001", second.text)

    def test_verify_firsts_calls_carry_no_run_and_keep_todays_key(self):
        chat = Chat()
        calls = spy_on(chat.registry)
        chat.say("my battery isn't charging")
        chat.say(RIDER[3:])
        [ctx] = [c for name, c in calls if name == REQUEST_IDENTITY_VERIFICATION]
        self.assertIsNone(ctx.value_for("started_at"))
        self.assertIsNone(ctx.persona)


if __name__ == "__main__":
    unittest.main()
