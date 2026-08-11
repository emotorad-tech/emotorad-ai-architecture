"""Dealer persona (W2) — money guardrails and persona isolation.

Two things are being proved here. First, that **the model may propose but only
code may price, commit or extend credit** — a model can be talked into a
discount, and what it produces is a confident commitment a company is held to.
Second, that a dealer **cannot reach customer data**, which is a real risk rather
than a theoretical one: dealers register most warranties under their own number.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import DealerWhatsAppAdapter, WhatsAppAdapter
from emotorad_ai.agents.dealer_orders import DEFINITION as DEALER_DEFINITION
from emotorad_ai.agents.dealer_orders import TOOL_NAMES as DEALER_TOOLS
from emotorad_ai.config import Settings
from emotorad_ai.contract import VERIFIED
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import (
    GET_DEALER_ACCOUNT,
    LOOKUP_WARRANTY_RECORD,
    PLACE_ORDER,
    QUOTE_ORDER,
    build_registry,
)
from emotorad_ai.tools.registry import ToolContext, is_error

TODAY = date(2026, 8, 6)

HEALTHY = "919000000001"      # Royal Cycle Stores, 120k credit available
NEAR_LIMIT = "919000000002"   # 5k credit, 42k overdue
ON_HOLD = "919000000003"      # on_hold, 118k overdue


def make_runtime(script):
    registry = build_registry(today=TODAY)
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None),
        registry=registry, llm=llm,
        log=EventLog(path=None, to_stdout=False),
        resolver=IdentityResolver(registry),
    )
    return runtime, llm


def dealer_says(runtime, sender, text, conversation_id="d1"):
    adapter = DealerWhatsAppAdapter(runtime.resolver)
    return runtime.handle(
        adapter.to_message({"from": sender, "text": text, "conversation_id": conversation_id})
    )


def ctx(dealer_id="DLR-PUN-014"):
    return ToolContext(conversation_id="d1", dealer_id=dealer_id)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.resolver = IdentityResolver(build_registry(today=TODAY))

    def test_a_known_dealer_resolves_verified_with_a_dealer_id(self):
        persona, identity = self.resolver.resolve_dealer(HEALTHY)
        self.assertEqual(persona, "dealer")
        self.assertEqual(identity.strength, VERIFIED)
        self.assertEqual(identity.dealer_id, "DLR-PUN-014")

    def test_an_unknown_number_on_the_dealer_line_is_not_downgraded_to_a_customer(self):
        # The dealer number is a separate line. An unknown sender there is an
        # error worth surfacing, not a shopper to be helped.
        persona, identity = self.resolver.resolve_dealer("919999999999")
        self.assertEqual(persona, "unknown")
        self.assertIsNone(identity.dealer_id)

    def test_a_dealer_never_resolves_through_the_customer_warranty_path(self):
        # The real risk: dealers register most warranties under their own number,
        # so the customer path would hand them dozens of unrelated bikes.
        from emotorad_ai.contract import InboundMessage

        _, identity = self.resolver.resolve_dealer(HEALTHY)
        message = InboundMessage("d1", "dealer", identity, "dealer_app", "need stock")
        resolved = self.resolver.hydrate(message)

        self.assertEqual(resolved.persona, "dealer")
        self.assertEqual(resolved.bikes, [], "a dealer must never be handed owned bikes")
        self.assertEqual(resolved.profile["dealer_id"], "DLR-PUN-014")


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)

    def test_prices_come_from_the_price_list_not_the_conversation(self):
        data = self.registry.call(
            QUOTE_ORDER, {"lines": [{"product_name": "EMX Plus", "quantity": 2}]}, ctx()
        )["data"]
        self.assertEqual(data["lines"][0]["unit_price"], 32000)
        self.assertEqual(data["total"], 64000)
        self.assertTrue(data["can_place"])

    def test_an_unknown_product_refuses_rather_than_guessing_a_price(self):
        envelope = self.registry.call(
            QUOTE_ORDER, {"lines": [{"product_name": "EMX Ultra Pro", "quantity": 1}]}, ctx()
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "unknown_product")

    def test_quoting_commits_nothing(self):
        for _ in range(3):
            self.registry.call(
                QUOTE_ORDER, {"lines": [{"product_name": "EMX Plus", "quantity": 1}]}, ctx()
            )
        self.assertEqual(len(self.registry.orders.orders), 0)


class CreditGuardrailTests(unittest.TestCase):
    """Credit is a money decision. Code makes it, never the model."""

    def setUp(self):
        self.registry = build_registry(today=TODAY)

    def test_an_order_over_the_credit_limit_cannot_be_placed(self):
        data = self.registry.call(
            QUOTE_ORDER, {"lines": [{"product_name": "EMX Plus", "quantity": 5}]}, ctx()
        )["data"]
        self.assertFalse(data["can_place"])
        self.assertIn("exceeds available credit", data["blockers"][0])

    def test_an_overdue_balance_blocks_a_dealer_who_has_credit_left(self):
        data = self.registry.call(
            QUOTE_ORDER, {"lines": [{"product_name": "Doodle V3", "quantity": 1}]},
            ctx("DLR-NAG-002"),
        )["data"]
        self.assertFalse(data["can_place"])
        self.assertTrue(any("overdue" in blocker for blocker in data["blockers"]))

    def test_an_on_hold_account_is_blocked_on_status_alone(self):
        data = self.registry.call(
            QUOTE_ORDER, {"lines": [{"product_name": "Doodle V3", "quantity": 1}]},
            ctx("DLR-NAG-002"),
        )["data"]
        self.assertTrue(any("on_hold" in blocker for blocker in data["blockers"]))

    def test_out_of_stock_blocks_the_order(self):
        data = self.registry.call(
            QUOTE_ORDER, {"lines": [{"product_name": "T-Rex Air", "quantity": 1}]}, ctx()
        )["data"]
        self.assertFalse(data["can_place"])
        self.assertIn("in stock", data["blockers"][0])

    def test_a_blocked_order_cannot_be_forced_through_place_order(self):
        # The model has the blocked quote in front of it and calls place anyway —
        # whether from confusion or because the dealer pushed.
        envelope = self.registry.call(
            PLACE_ORDER,
            {"lines": [{"product_name": "EMX Plus", "quantity": 5}],
             "quoted_total": 160000, "idempotency_key": "k1"},
            ctx(),
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "order_blocked")
        self.assertEqual(len(self.registry.orders.orders), 0)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)

    def test_a_confirmed_order_is_placed_and_spends_credit(self):
        envelope = self.registry.call(
            PLACE_ORDER,
            {"lines": [{"product_name": "EMX Plus", "quantity": 2}],
             "quoted_total": 64000, "idempotency_key": "k-ok"},
            ctx(),
        )
        data = envelope["data"]
        self.assertEqual(data["status"], "placed")
        self.assertEqual(data["credit_available_after"], 120000 - 64000)
        self.assertEqual(len(self.registry.orders.orders), 1)

    def test_a_total_the_model_carried_wrongly_is_refused(self):
        # The model has held a number across several turns. It may have mistyped,
        # rounded, or applied a discount nobody authorised.
        envelope = self.registry.call(
            PLACE_ORDER,
            {"lines": [{"product_name": "EMX Plus", "quantity": 2}],
             "quoted_total": 60000, "idempotency_key": "k-bad"},
            ctx(),
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "quote_mismatch")
        self.assertEqual(len(self.registry.orders.orders), 0)

    def test_placing_the_same_order_twice_does_not_double_it(self):
        args = {"lines": [{"product_name": "EMX Plus", "quantity": 1}],
                "quoted_total": 32000, "idempotency_key": "k-dup"}
        first = self.registry.call(PLACE_ORDER, dict(args), ctx())
        second = self.registry.call(PLACE_ORDER, dict(args), ctx())
        self.assertEqual(first["data"]["order_id"], second["data"]["order_id"])
        self.assertEqual(len(self.registry.orders.orders), 1)

    def test_a_write_without_an_idempotency_key_is_refused(self):
        envelope = self.registry.call(
            PLACE_ORDER,
            {"lines": [{"product_name": "EMX Plus", "quantity": 1}], "quoted_total": 32000},
            ctx(),
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "missing_idempotency_key")


class PersonaIsolationTests(unittest.TestCase):
    """The property that matters most: a dealer cannot reach customer data."""

    def test_the_dealer_agent_is_not_given_the_customer_warranty_tool(self):
        self.assertNotIn(LOOKUP_WARRANTY_RECORD, DEALER_TOOLS)

    def test_a_dealer_cannot_be_handed_customer_tools_by_the_runtime(self):
        runtime, llm = make_runtime([say("What model and how many?")])
        dealer_says(runtime, HEALTHY, "need some stock")
        offered = {tool["name"] for tool in llm.requests[0]["tools"]}
        self.assertNotIn(LOOKUP_WARRANTY_RECORD, offered)
        self.assertIn(QUOTE_ORDER, offered)

    def test_a_customer_is_never_offered_dealer_tools(self):
        runtime, llm = make_runtime([say("Try a different socket.")])
        adapter = WhatsAppAdapter(runtime.resolver)
        runtime.handle(adapter.to_message(
            {"from": "919876543210", "text": "battery won't charge", "conversation_id": "c1"}
        ))
        offered = {tool["name"] for tool in llm.requests[0]["tools"]}
        self.assertNotIn(PLACE_ORDER, offered)
        self.assertNotIn(GET_DEALER_ACCOUNT, offered)

    def test_a_dealer_asking_about_a_battery_does_not_reach_battery_support(self):
        # Same words, different persona, different meaning. A dealer saying
        # "battery problem" is talking about stock or a claim, not their own bike.
        runtime, llm = make_runtime([say("Which order is that about?")])
        reply = dealer_says(runtime, HEALTHY, "battery problem")
        self.assertEqual(reply.handled_by, "dealer_orders")

    def test_the_dealer_prompt_carries_no_customer_context(self):
        runtime, llm = make_runtime([say("What model?")])
        dealer_says(runtime, HEALTHY, "need stock")
        prompt = llm.requests[0]["system"]
        self.assertIn("Royal Cycle Stores", prompt)
        for leak in ("Ananya", "EMXP2025004417", "frame"):
            self.assertNotIn(leak, prompt, leak)


class RuntimeJourneyTests(unittest.TestCase):
    def test_a_dealer_order_runs_end_to_end(self):
        runtime, llm = make_runtime([
            call_tool(QUOTE_ORDER, {"lines": [{"product_name": "EMX Plus", "quantity": 2}]}, "t1"),
            say("Two EMX Plus at 32,000 each, total 64,000. Shall I place it?"),
        ])
        reply = dealer_says(runtime, HEALTHY, "need 2 EMX Plus")
        self.assertEqual(reply.handled_by, "dealer_orders")
        self.assertIn(QUOTE_ORDER, reply.metadata["tool_calls"])

    def test_an_overdue_dealer_is_told_before_being_led_on(self):
        runtime, llm = make_runtime([say("There is an overdue balance to clear first.")])
        dealer_says(runtime, ON_HOLD, "need 1 Doodle")
        prompt = llm.requests[0]["system"]
        self.assertIn("OVERDUE", prompt)
        self.assertIn("Say so early", prompt)

    def test_an_unknown_dealer_number_gets_no_agent_at_all(self):
        runtime, llm = make_runtime([])
        reply = dealer_says(runtime, "919999999999", "need stock")
        self.assertEqual(llm.requests, [])
        self.assertTrue(reply.escalated)

    def test_the_dealer_still_gets_the_ai_disclosure(self):
        from emotorad_ai.disclosure import has_disclosure

        runtime, _ = make_runtime([say("What model and how many?")])
        reply = dealer_says(runtime, HEALTHY, "need stock")
        self.assertTrue(has_disclosure(reply.text))

    def test_a_dealer_asking_for_a_human_still_exits_immediately(self):
        # Dealers do not say "agent" — they name a role. Same intent, different
        # vocabulary, and the customer-shaped pattern missed every one of them.
        runtime, llm = make_runtime([])
        reply = dealer_says(runtime, HEALTHY, "connect me to my account manager please")
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertEqual(llm.requests, [])

    def test_invoking_an_account_manager_to_argue_is_not_a_transfer_request(self):
        # "My AM said I get 5% off" is a discount argument, not a handoff. The
        # money guardrails refuse it in code, so the agent can answer it.
        runtime, llm = make_runtime([say("I am not able to approve a discount.")])
        reply = dealer_says(runtime, HEALTHY, "my account manager said I get 5% discount")
        self.assertEqual(reply.handled_by, "dealer_orders")


if __name__ == "__main__":
    unittest.main()
