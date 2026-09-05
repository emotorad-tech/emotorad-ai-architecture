"""A bot defined in YAML, run through the real skeleton.

Mirrors tests/test_motor_support.py::InheritedBehaviourTests on purpose: if a
YAML bot inherits the same things a hand-written second bot did, the catalogue
has not opened a way around any of them.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import DealerWhatsAppAdapter, WhatsAppAdapter
from emotorad_ai.bots import DRAFT, BotCatalogue, builtin_specs, spec_from_dict
from emotorad_ai.config import Settings
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 6)

BRAKES = spec_from_dict(
    {
        "name": "brakes_support",
        "persona": "customer",
        "topic": "brakes",
        "keywords": ["brake", "braking", "ब्रेक"],
        "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
        "prompt": "You are the brake support assistant for EMotorad.\n",
    },
    DRAFT,
)

STOCK = spec_from_dict(
    {
        "name": "dealer_stock",
        "persona": "dealer",
        "topic": "stock",
        "keywords": ["stock", "availability"],
        "tools": ["get_dealer_account", "create_support_ticket"],
        "prompt": "You answer dealer stock questions.\n",
    },
    DRAFT,
)


def make_runtime(script, extra=(BRAKES, STOCK)):
    catalogue = BotCatalogue(builtin_specs() + list(extra))
    registry = build_registry(today=TODAY, topics=catalogue.topics())
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None),
        registry=registry,
        llm=llm,
        log=EventLog(path=None, to_stdout=False),
        resolver=IdentityResolver(registry),
        catalogue=catalogue,
    )
    return runtime, llm


def whatsapp(runtime, sender, text, conversation_id="c1"):
    adapter = WhatsAppAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"from": sender, "text": text, "conversation_id": conversation_id}))


def dealer_says(runtime, sender, text, conversation_id="d1"):
    adapter = DealerWhatsAppAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"from": sender, "text": text, "conversation_id": conversation_id}))


class RoutingTests(unittest.TestCase):
    def test_a_yaml_bot_is_reached_on_its_keywords(self):
        runtime, llm = make_runtime(
            [call_tool(SEARCH_KNOWLEDGE, {"query": "squeak", "topic": "brakes"}, "t1"), say("Let me help.")]
        )
        reply = whatsapp(runtime, "919876543210", "my brake squeaks when I stop slowly")
        self.assertEqual(reply.handled_by, "brakes_support")
        self.assertEqual(llm.requests[0]["tools"][0]["name"], "lookup_warranty_record")
        self.assertEqual(
            [t["name"] for t in llm.requests[0]["tools"]],
            ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
        )

    def test_built_in_routing_is_unchanged(self):
        runtime, _ = make_runtime([say("Try another socket.")])
        self.assertEqual(whatsapp(runtime, "919876543210", "battery won't charge").handled_by, "battery_support")

    def test_the_search_enum_includes_the_new_topic(self):
        runtime, llm = make_runtime([say("ok")])
        whatsapp(runtime, "919876543210", "brake squeaks")
        search = next(t for t in llm.requests[0]["tools"] if t["name"] == SEARCH_KNOWLEDGE)
        self.assertEqual(search["input_schema"]["properties"]["topic"]["enum"], ["battery", "brakes", "motor"])

    def test_the_unsupported_reply_names_every_customer_topic(self):
        runtime, llm = make_runtime([], extra=(BRAKES,))
        # "seat" is classified by nothing, so triage asks; make it a topic with
        # no agent by giving triage a table entry with no bot behind it.
        runtime.triage.keywords = dict(runtime.triage.keywords, seat=("seat",))
        reply = whatsapp(runtime, "919876543210", "the seat is loose")
        self.assertEqual(llm.requests, [])
        self.assertIn("battery, motor and brakes problems", reply.text)

    def test_the_default_catalogue_is_the_built_ins_plus_bots_dir(self):
        registry = build_registry(today=TODAY)
        runtime = Runtime(
            settings=Settings(log_to_stdout=False, log_path=None),
            registry=registry, llm=ScriptedClaude([]), log=EventLog(path=None, to_stdout=False),
            resolver=IdentityResolver(registry),
        )
        self.assertIn("battery_support", runtime.agents)
        self.assertIn("dealer_orders", runtime.agents)


class InheritedBehaviourTests(unittest.TestCase):
    def test_it_inherits_customer_context_verbatim(self):
        runtime, llm = make_runtime([say("Let me help.")])
        whatsapp(runtime, "919876543210", "brake squeaks")
        prompt = llm.requests[0]["system"]
        self.assertTrue(prompt.startswith(BRAKES.prompt))
        self.assertIn("Ananya Rao", prompt)
        self.assertIn("EMXP2025004417", prompt)
        self.assertIn("in warranty", prompt)

    def test_it_inherits_multi_bike_disambiguation(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919700000001", "brake is squeaking")
        self.assertEqual(llm.requests, [])
        self.assertIn("Which one", reply.text)

    def test_it_inherits_the_ai_disclosure(self):
        runtime, _ = make_runtime([say("Let me help.")])
        self.assertTrue(has_disclosure(whatsapp(runtime, "919876543210", "brake squeaks").text))

    def test_it_inherits_the_coverage_post_check(self):
        runtime, _ = make_runtime(
            [call_tool("lookup_warranty_record", {}, "t1"),
             say("Your brakes are still covered under warranty, so no charge.")]
        )
        reply = whatsapp(runtime, "919812345678", "brake squeaks, is it covered")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertTrue(reply.escalated)

    def test_it_inherits_the_human_handoff(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "brake squeaks, connect me to an agent")
        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")

    def test_it_inherits_the_safety_gate(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "my brakes are not working")
        self.assertEqual(llm.requests, [])
        self.assertTrue(reply.escalated)


class DealerRoutingTests(unittest.TestCase):
    def test_a_dealer_yaml_bot_is_reached_on_its_keywords(self):
        runtime, llm = make_runtime([say("EMX Plus is in stock.")])
        reply = dealer_says(runtime, "919000000001", "is the EMX Plus in stock")
        self.assertEqual(reply.handled_by, "dealer_stock")
        self.assertEqual([t["name"] for t in llm.requests[0]["tools"]], ["get_dealer_account", "create_support_ticket"])

    def test_unmatched_dealer_messages_still_go_to_dealer_orders(self):
        runtime, _ = make_runtime([say("How many would you like?")])
        self.assertEqual(dealer_says(runtime, "919000000001", "need 5 EMX Plus").handled_by, "dealer_orders")

    def test_a_dealer_conversation_stays_with_its_agent(self):
        runtime, _ = make_runtime([say("In stock."), say("Yes, 40 units.")])
        dealer_says(runtime, "919000000001", "is the EMX Plus in stock")
        reply = dealer_says(runtime, "919000000001", "need 5")  # "need" is an orders phrase
        self.assertEqual(reply.handled_by, "dealer_stock")


if __name__ == "__main__":
    unittest.main()
