"""The playground's tool loop.

The model is scripted here, so what is under test is the loop, not Claude. Every
tool call, guardrail and envelope below is the real thing.

The case this file exists for: a customer proves who they are and the bot reads
their warranty record **in the same assistant turn**. The identity has to be
visible to the second call, which it was not — the context was built once per
turn, so the lookup failed with missing_identity on the very turn the customer
had just verified.
"""

import copy
import unittest
from datetime import date

from emotorad_ai.playground import MAX_TOOL_ITERATIONS, _run_agent_turn, _tool_context
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.tools.verification import (
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    VerificationStore,
)

CHAT = "c-loop"


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))  # snapshot: the caller mutates messages
        return self.script[len(self.calls) - 1]


class _Client:
    """Stands in for Anthropic. The tools it calls are real."""

    def __init__(self, script):
        self.messages = _Messages(script)


def _text(text):
    return _Response([_Block(type="text", text=text)])


def _uses(*calls):
    return _Response([_Block(type="tool_use", id="t%d" % i, name=n, input=a) for i, (n, a) in enumerate(calls)])


def _run(client, registry, tools, context_factory, messages=None):
    return _run_agent_turn(
        client,
        "model",
        "system",
        messages if messages is not None else [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        registry,
        tools,
        context_factory,
    )


class IdentityWithinOneTurnTests(unittest.TestCase):
    """The regression this file was written for."""

    def setUp(self):
        self.store = VerificationStore()
        self.registry = build_registry(today=date.today(), verification=self.store)
        self.phone = next(iter(fixtures.WARRANTY_RECORDS))
        self.tools = (
            REQUEST_IDENTITY_VERIFICATION,
            VERIFY_IDENTITY,
            "lookup_warranty_record",
        )

    def _factory(self):
        """What the live playground passes: identity read fresh, every call."""

        def factory():
            verified = self.store.verified_phone(CHAT)
            return ToolContext(conversation_id=CHAT, phone=verified)

        return factory

    def test_verifying_then_reading_the_record_in_one_turn_works(self):
        self.registry.call(
            REQUEST_IDENTITY_VERIFICATION, {"phone": self.phone}, ToolContext(conversation_id=CHAT)
        )
        code = self.store.pending_code(CHAT)

        client = _Client(
            [
                _uses((VERIFY_IDENTITY, {"code": code}), ("lookup_warranty_record", {})),
                _text("Your bike is in warranty."),
            ]
        )
        outcome = _run(client, self.registry, self.tools, self._factory())

        calls = {c["tool"]: c for c in outcome["trace"]}
        self.assertFalse(is_error(calls[VERIFY_IDENTITY]["result"]))
        self.assertFalse(
            is_error(calls["lookup_warranty_record"]["result"]),
            "the lookup ran on the turn the customer verified, and must see that identity",
        )
        self.assertIn("bikes", calls["lookup_warranty_record"]["result"]["data"])

    def test_the_lookup_still_refuses_when_nothing_has_been_verified(self):
        # The fix must not become "always resolve a phone".
        client = _Client([_uses(("lookup_warranty_record", {})), _text("ok")])
        outcome = _run(client, self.registry, self.tools, self._factory())
        envelope = outcome["trace"][0]["result"]
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "missing_identity")

    def test_a_wrong_code_leaves_the_lookup_refusing(self):
        self.registry.call(
            REQUEST_IDENTITY_VERIFICATION, {"phone": self.phone}, ToolContext(conversation_id=CHAT)
        )
        client = _Client(
            [
                _uses((VERIFY_IDENTITY, {"code": "000000"}), ("lookup_warranty_record", {})),
                _text("ok"),
            ]
        )
        outcome = _run(client, self.registry, self.tools, self._factory())
        calls = {c["tool"]: c for c in outcome["trace"]}
        self.assertTrue(is_error(calls[VERIFY_IDENTITY]["result"]))
        self.assertEqual(calls["lookup_warranty_record"]["result"]["error"]["code"], "missing_identity")


class LoopMechanicsTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=date.today())
        self.phone = next(iter(fixtures.WARRANTY_RECORDS))
        self.context = ToolContext(conversation_id=CHAT, phone=self.phone)

    def _factory(self):
        return lambda: self.context

    def test_a_tool_result_is_fed_back_before_the_model_answers(self):
        client = _Client([_uses(("lookup_warranty_record", {})), _text("Here you go.")])
        outcome = _run(client, self.registry, ("lookup_warranty_record",), self._factory())
        self.assertEqual(outcome["text"], "Here you go.")
        second = client.messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "user"])
        self.assertEqual(second[-1]["content"][0]["type"], "tool_result")

    def test_results_from_one_turn_go_back_in_a_single_message(self):
        # Splitting them teaches the model to stop batching its calls.
        client = _Client(
            [
                _uses(("lookup_warranty_record", {}), ("search_knowledge", {"query": "won't charge"})),
                _text("done"),
            ]
        )
        _run(client, self.registry, ("lookup_warranty_record", "search_knowledge"), self._factory())
        back = client.messages.calls[1]["messages"][-1]
        self.assertEqual(len(back["content"]), 2)
        self.assertEqual({b["type"] for b in back["content"]}, {"tool_result"})

    def test_the_same_call_twice_stops_the_loop(self):
        client = _Client([_uses(("lookup_warranty_record", {})), _uses(("lookup_warranty_record", {}))])
        outcome = _run(client, self.registry, ("lookup_warranty_record",), self._factory())
        self.assertTrue(any(c.get("stuck") for c in outcome["trace"]))
        self.assertIn("identical arguments twice", outcome["text"])

    def test_the_iteration_cap_holds(self):
        client = _Client(
            [_uses(("search_knowledge", {"query": "q%d" % i})) for i in range(MAX_TOOL_ITERATIONS + 2)]
        )
        outcome = _run(client, self.registry, ("search_knowledge",), self._factory())
        self.assertEqual(outcome["iterations"], MAX_TOOL_ITERATIONS)
        self.assertIn("Stopped after", outcome["text"])

    def test_a_write_gets_an_idempotency_key_the_model_forgot(self):
        client = _Client(
            [
                _uses(("create_support_ticket",
                       {"category": "battery_charging", "description": "won't charge", "severity": "normal"})),
                _text("Ticket raised."),
            ]
        )
        outcome = _run(client, self.registry, ("create_support_ticket",), self._factory())
        self.assertTrue(outcome["trace"][0]["arguments"].get("idempotency_key"))
        self.assertFalse(is_error(outcome["trace"][0]["result"]))

    def test_an_empty_reply_is_explained_rather_than_rendered_blank(self):
        client = _Client([_Response([], stop_reason="max_tokens")])
        outcome = _run(client, self.registry, (), self._factory())
        self.assertIn("No text returned", outcome["text"])
        self.assertIn("max_tokens", outcome["text"])


class PersonaIsolationTests(unittest.TestCase):
    """Dealers register most warranties under their own number.

    If the dealer agent were offered the customer warranty tool, one call would
    hand a dealer dozens of unrelated customers' bikes. Asserted here rather than
    assumed, because nothing else in the loop enforces it.
    """

    def test_the_dealer_slice_is_never_offered_the_customer_warranty_table(self):
        import importlib

        registry = build_registry(today=date.today())
        dealer = importlib.import_module("emotorad_ai.agents.dealer_orders")
        client = _Client([_text("ok")])
        _run(client, registry, dealer.TOOL_NAMES, lambda: ToolContext(conversation_id=CHAT))
        offered = [t["name"] for t in client.messages.calls[0]["tools"]]
        self.assertNotIn("lookup_warranty_record", offered)
        self.assertIn("get_dealer_account", offered)

    def test_injected_identity_is_absent_from_every_schema_the_model_sees(self):
        import importlib

        registry = build_registry(today=date.today(), verification=VerificationStore())
        battery = importlib.import_module("emotorad_ai.agents.battery_support")
        client = _Client([_text("ok")])
        _run(client, registry, battery.TOOL_NAMES, lambda: ToolContext(conversation_id=CHAT))
        for tool in client.messages.calls[0]["tools"]:
            properties = set(tool["input_schema"]["properties"])
            self.assertNotIn("phone", properties, tool["name"])
            self.assertNotIn("conversation_id", properties, tool["name"])


if __name__ == "__main__":
    unittest.main()


class SuppressedToolTests(unittest.TestCase):
    """Tools withheld on this page only, while content is being authored.

    battery_support's knowledge records are the R1 skeleton and now hold worse
    content than the prompt does, so searching them would return thinner answers
    from a second, diverging source of truth. Withheld in the playground and
    nowhere else — production's TOOL_NAMES must not move for a tester's
    convenience.
    """

    def setUp(self):
        import importlib

        from emotorad_ai.playground import _playground_tool_names

        self.slice_for = _playground_tool_names
        self.registry = build_registry(today=date.today(), verification=VerificationStore())
        self.battery = importlib.import_module("emotorad_ai.agents.battery_support")
        self.motor = importlib.import_module("emotorad_ai.agents.motor_support")

    def test_battery_is_not_offered_the_knowledge_search(self):
        for live in (True, False):
            names = self.slice_for("battery_support", self.battery, self.registry, live)
            self.assertNotIn("search_knowledge", names, "live=%s" % live)

    def test_the_rest_of_batterys_slice_is_untouched(self):
        names = self.slice_for("battery_support", self.battery, self.registry, False)
        self.assertIn("lookup_warranty_record", names)
        self.assertIn("create_support_ticket", names)

    def test_no_other_agent_is_affected(self):
        names = self.slice_for("motor_support", self.motor, self.registry, True)
        self.assertIn("search_knowledge", names)

    def test_production_tool_names_are_not_modified(self):
        # The suppression is a view, not an edit. If it mutated TOOL_NAMES the
        # deployed agent would lose its knowledge tool too.
        self.slice_for("battery_support", self.battery, self.registry, True)
        self.assertIn("search_knowledge", self.battery.TOOL_NAMES)

    def test_the_model_is_never_shown_the_withheld_tool(self):
        client = _Client([_text("ok")])
        _run(
            client,
            self.registry,
            self.slice_for("battery_support", self.battery, self.registry, True),
            lambda: ToolContext(conversation_id=CHAT),
        )
        offered = [t["name"] for t in client.messages.calls[0]["tools"]]
        self.assertNotIn("search_knowledge", offered)


class PromptPublishingTests(unittest.TestCase):
    """Publishing from outside the browser tab.

    Versions are appended, never overwritten, so a bad publish is undone by
    loading the one before it — and an editor that already has unsaved text is
    told a newer version exists rather than having it swapped in underneath.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from emotorad_ai import playground

        self.playground = playground
        self._dir = tempfile.TemporaryDirectory()
        self._previous = playground.PLAYGROUND_DIR
        playground.PLAYGROUND_DIR = Path(self._dir.name)
        playground.PROMPT_DIR = Path(self._dir.name) / "prompts"

    def tearDown(self):
        self.playground.PLAYGROUND_DIR = self._previous
        self.playground.PROMPT_DIR = self._previous / "prompts"
        self._dir.cleanup()

    def test_versions_are_appended_and_numbered(self):
        first = self.playground._save_prompt_version("battery_support", "one")
        second = self.playground._save_prompt_version("battery_support", "two")
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(len(self.playground._load_prompt_versions("battery_support")), 2)

    def test_publishing_never_destroys_an_earlier_version(self):
        # The whole rollback story depends on this.
        self.playground._save_prompt_version("battery_support", "original")
        self.playground._save_prompt_version("battery_support", "replacement")
        texts = [v["text"] for v in self.playground._load_prompt_versions("battery_support")]
        self.assertIn("original", texts)

    def test_the_newest_version_is_what_a_fresh_editor_starts_from(self):
        import types

        self.playground._save_prompt_version("battery_support", "older")
        self.playground._save_prompt_version("battery_support", "newest")
        module = types.SimpleNamespace(_BASE_PROMPT="module default")
        self.assertEqual(self.playground._default_prompt("battery_support", module), "newest")

    def test_a_fresh_agent_falls_back_to_its_module_prompt(self):
        import types

        module = types.SimpleNamespace(_BASE_PROMPT="module default")
        self.assertEqual(self.playground._default_prompt("motor_support", module), "module default")

    def test_the_version_label_distinguishes_saved_from_draft(self):
        # What tells the tester whether what they are testing is what is stored.
        self.playground._save_prompt_version("battery_support", "saved text")
        self.assertEqual(self.playground._prompt_version_label("battery_support", "saved text"), "v1")
        self.assertEqual(
            self.playground._prompt_version_label("battery_support", "edited since"), "v1+draft"
        )
