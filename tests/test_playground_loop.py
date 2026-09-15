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

    def test_an_empty_reply_from_a_truncated_turn_points_at_the_output_cap(self):
        client = _Client([_Response([], stop_reason="max_tokens")])
        outcome = _run(client, self.registry, (), self._factory())
        self.assertIn("No text returned", outcome["text"])
        self.assertIn("output cap", outcome["text"])

    def test_an_empty_reply_that_was_not_truncated_says_so_instead(self):
        """The two causes need different explanations.

        A turn that simply ends with nothing written — `end_turn`, no text block,
        nothing truncated — used to be reported as though the output cap might be
        to blame, sending the tester to a knob that has no bearing on it. Seen in
        a real session after the model's tool calls came back.
        """
        client = _Client([_Response([], stop_reason="end_turn")])
        outcome = _run(client, self.registry, (), self._factory())
        self.assertIn("No text returned", outcome["text"])
        self.assertIn("nothing truncated", outcome["text"])
        self.assertNotIn("output cap", outcome["text"])


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
    """Withholding a tool on this page only.

    battery_support's search_knowledge was withheld while its flows lived in the
    prompt and its records were the R1 skeleton: searching would have returned
    thinner content than the model already had, from a second source of truth
    quietly diverging from the first. The list is empty again now that the Doodle
    flow lives only in a record — so what is worth asserting is the mechanism and
    its blast radius, not which agent happened to be in it.
    """

    def setUp(self):
        import importlib

        from emotorad_ai.playground import _playground_tool_names

        self.slice_for = _playground_tool_names
        self.registry = build_registry(today=date.today(), verification=VerificationStore())
        self.battery = importlib.import_module("emotorad_ai.agents.battery_support")

    def test_a_suppressed_tool_is_withheld_in_every_rider_mode(self):
        from emotorad_ai import playground

        previous = playground.PLAYGROUND_SUPPRESSED_TOOLS
        playground.PLAYGROUND_SUPPRESSED_TOOLS = {"battery_support": ("search_knowledge",)}
        try:
            for live in (True, False):
                names = self.slice_for("battery_support", self.battery, self.registry, live)
                self.assertNotIn("search_knowledge", names, "live=%s" % live)
        finally:
            playground.PLAYGROUND_SUPPRESSED_TOOLS = previous

    def test_suppression_touches_only_the_named_agent(self):
        import importlib

        from emotorad_ai import playground

        previous = playground.PLAYGROUND_SUPPRESSED_TOOLS
        playground.PLAYGROUND_SUPPRESSED_TOOLS = {"battery_support": ("search_knowledge",)}
        try:
            motor = importlib.import_module("emotorad_ai.agents.motor_support")
            self.assertIn("search_knowledge", self.slice_for("motor_support", motor, self.registry, True))
        finally:
            playground.PLAYGROUND_SUPPRESSED_TOOLS = previous

    def test_it_is_a_view_and_never_edits_production_tool_names(self):
        # If it mutated TOOL_NAMES the deployed agent would lose the tool too.
        self.slice_for("battery_support", self.battery, self.registry, True)
        self.assertIn("search_knowledge", self.battery.TOOL_NAMES)

    def test_battery_can_search_again_now_that_a_flow_lives_in_a_record(self):
        # The Doodle flow is only in knowledge/. Withholding the tool would make
        # it unreachable, which is the failure this migration exists to avoid.
        names = self.slice_for("battery_support", self.battery, self.registry, True)
        self.assertIn("search_knowledge", names)


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


class TestEvidenceCommandTests(unittest.TestCase):
    """`/proof` stands in for an upload, in the harness and not in the prompt.

    A bypass phrase written into the prompt would be one prompt-extraction away
    from opening a warranty claim with no evidence, and it would mean testing a
    bot that has a bypass when the production one will not. Nothing is bypassed
    here: the turn genuinely carries evidence.
    """

    def setUp(self):
        from emotorad_ai.playground import PROOF_COMMAND, _split_proof

        self.split = _split_proof
        self.command = PROOF_COMMAND

    def test_the_bare_command_produces_evidence_and_a_plain_message(self):
        text, note = self.split(self.command)
        self.assertEqual(text, "Here you go.")
        self.assertIn("Evidence attached", note)

    def test_the_rest_of_the_line_describes_what_it_shows(self):
        # So a tester can steer the flow without opening a camera.
        text, note = self.split("/proof green light, no red")
        self.assertEqual(text, "green light, no red")
        self.assertIn("green light, no red", note)

    def test_it_is_case_insensitive(self):
        self.assertIsNotNone(self.split("/PROOF blurry")[1])

    def test_an_ordinary_message_is_untouched(self):
        text, note = self.split("no light coming")
        self.assertEqual(text, "no light coming")
        self.assertIsNone(note)

    def test_the_command_must_start_the_message(self):
        # A customer whose sentence happens to contain it is not sending evidence.
        text, note = self.split("the /proof is in the pudding")
        self.assertIsNone(note)
        self.assertEqual(text, "the /proof is in the pudding")

    def test_nothing_at_all_is_handled(self):
        self.assertEqual(self.split(None), (None, None))
        self.assertEqual(self.split("")[1], None)

    def test_the_note_tells_the_model_it_is_a_fixture(self):
        # It must never read as a real photo in a saved transcript, and the model
        # should know it is standing in for one.
        note = self.split("/proof")[1]
        self.assertIn("test fixture", note)

    def test_the_command_appears_nowhere_in_any_shipped_prompt(self):
        # The whole point: this is harness-only. If it ever turns up in an agent's
        # prompt it is on its way to production.
        import importlib

        for module_name in (
            "emotorad_ai.agents.battery_support",
            "emotorad_ai.agents.motor_support",
            "emotorad_ai.agents.late_warranty",
            "emotorad_ai.agents.dealer_orders",
        ):
            module = importlib.import_module(module_name)
            self.assertNotIn(self.command, module._BASE_PROMPT, module_name)


class PlaygroundVersionTests(unittest.TestCase):
    """The build number and its changelog must not drift apart.

    A saved transcript is the product of a prompt version *and* the harness that
    ran it, so a build number that lies is worse than none — it would date a
    transcript to behaviour the code did not have.
    """

    def test_the_version_matches_the_newest_changelog_entry(self):
        from emotorad_ai.playground_version import CHANGELOG, PLAYGROUND_VERSION

        self.assertEqual(PLAYGROUND_VERSION, CHANGELOG[0][0])

    def test_the_changelog_is_newest_first_and_has_no_duplicates(self):
        from emotorad_ai.playground_version import CHANGELOG

        versions = [v for v, _, _ in CHANGELOG]
        self.assertEqual(len(versions), len(set(versions)), "duplicate version")
        as_tuples = [tuple(int(p) for p in v.split(".")) for v in versions]
        self.assertEqual(as_tuples, sorted(as_tuples, reverse=True), "not newest-first")

    def test_every_entry_says_what_a_tester_would_notice(self):
        from emotorad_ai.playground_version import CHANGELOG

        for version, released, summary in CHANGELOG:
            self.assertRegex(released, r"^\d{4}-\d{2}-\d{2}$", version)
            self.assertGreater(len(summary), 40, "%s: summary too thin to be useful" % version)


class StaticCorrectnessTests(unittest.TestCase):
    """Names that are used but never defined.

    A NameError on the submit path shipped and reached the user, past a green
    boot matrix and 423 tests. It could not have been caught by either: the page
    tests load the app and click controls, and Streamlit's AppTest cannot drive a
    file-accepting chat_input at all (it never fills the uploader half of the
    widget state), so nothing exercises submit.

    But it was a *static* error, and a static check finds it without running
    anything. pyflakes on the same file reports "undefined name 'nonce'".
    """

    def test_no_undefined_names_anywhere_in_the_package(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", "src/emotorad_ai", "scripts"],
            capture_output=True,
            text=True,
        )
        # Unused imports are noise, not bugs. Undefined names are always bugs.
        serious = [
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "undefined name" in line or "local variable" in line and "referenced before" in line
        ]
        self.assertEqual(serious, [], "\n".join(serious))



class RetryAfterErrorTests(unittest.TestCase):
    """Repeating a call that failed is retrying; repeating one that worked is stuck.

    From chat 20260909-cc16dd19: a customer verified and asked about an error
    code in the same turn. The lookup failed because the bikes had been captured
    while they were still anonymous, the model sensibly tried again — and the
    loop killed the turn as "stuck", discarding the retry *and* the reply. The
    customer got a handover message instead of their answer.
    """

    def setUp(self):
        self.registry = build_registry(today=date.today())
        self.context = ToolContext(conversation_id=CHAT, phone=next(iter(fixtures.WARRANTY_RECORDS)))

    def _factory(self):
        return lambda: self.context

    def test_a_call_that_failed_may_be_retried(self):
        # First attempt errors (no frame number owned), second succeeds because
        # the arguments differ — but the retry must not be pre-emptively blocked.
        calls = 0

        class _Flaky:
            def __init__(self, registry):
                self.registry = registry

            def call(self, name, arguments, context):
                nonlocal calls
                calls += 1
                if calls == 1:
                    from emotorad_ai.tools.registry import err

                    return err("temporarily_unavailable", "not yet")
                return self.registry.call(name, arguments, context)

            def __getattr__(self, item):
                return getattr(self.registry, item)

        flaky = _Flaky(self.registry)
        client = _Client(
            [
                _uses(("lookup_warranty_record", {})),
                _uses(("lookup_warranty_record", {})),
                _text("Your bike is in warranty."),
            ]
        )
        outcome = _run_agent_turn(
            client, "m", "sys", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            flaky, ("lookup_warranty_record",), self._factory(),
        )
        self.assertEqual(outcome["text"], "Your bike is in warranty.")
        self.assertFalse(any(c.get("stuck") for c in outcome["trace"]))
        self.assertFalse(is_error(outcome["trace"][1]["result"]), "the retry's result was kept")

    def test_repeating_a_call_that_already_worked_is_still_stuck(self):
        client = _Client([_uses(("lookup_warranty_record", {})), _uses(("lookup_warranty_record", {}))])
        outcome = _run_agent_turn(
            client, "m", "sys", [{"role": "user", "content": []}],
            self.registry, ("lookup_warranty_record",), self._factory(),
        )
        self.assertTrue(any(c.get("stuck") for c in outcome["trace"]))

    def test_failing_the_same_way_twice_is_stuck(self):
        # One retry, not unlimited: the same error twice is no progress.
        context = ToolContext(conversation_id=CHAT)  # no phone -> always missing_identity
        client = _Client(
            [
                _uses(("lookup_warranty_record", {})),
                _uses(("lookup_warranty_record", {})),
                _uses(("lookup_warranty_record", {})),
            ]
        )
        outcome = _run_agent_turn(
            client, "m", "sys", [{"role": "user", "content": []}],
            self.registry, ("lookup_warranty_record",), lambda: context,
        )
        self.assertTrue(any(c.get("stuck") for c in outcome["trace"]))


class BikesReadWhenTheToolRunsTests(unittest.TestCase):
    """owned_bikes may be a callable, for the same reason the context is a factory."""

    def test_a_callable_is_read_at_call_time_not_wiring_time(self):
        from emotorad_ai.errorcodes import load_table

        bikes = []
        registry = build_registry(
            today=date.today(), error_codes=load_table(), owned_bikes=lambda: bikes
        )
        first = registry.call("lookup_error_code", {"code": "E-07"}, ToolContext(conversation_id=CHAT))
        self.assertEqual(first["error"]["code"], "no_bike_resolved")

        bikes.append({"product_name": "X2 Furious Red V2"})
        second = registry.call("lookup_error_code", {"code": "E-07"}, ToolContext(conversation_id=CHAT))
        self.assertFalse(is_error(second), "bikes that arrived mid-turn must be visible")

    def test_a_plain_list_still_works(self):
        from emotorad_ai.errorcodes import load_table

        registry = build_registry(
            today=date.today(), error_codes=load_table(),
            owned_bikes=[{"product_name": "X2 Furious Red V2"}],
        )
        self.assertFalse(
            is_error(registry.call("lookup_error_code", {"code": "E-07"}, ToolContext(conversation_id=CHAT)))
        )
