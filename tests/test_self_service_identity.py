"""The surfaces that must establish identity inside the conversation.

Every production channel resolves identity upstream: WhatsApp supplies a
verified phone natively, Amiigo authenticates on one, and the website hands over
a session the site has already authenticated. `battery_support.TOOL_NAMES` is
written for that world and is right to omit the verification tools.

Website chat opened by an anonymous visitor is the exception. There the agent
has to ask for the number and prove it itself, and slicing strictly by
TOOL_NAMES left those tools registered but unreachable: the model was told to
ask for a phone number and then had nothing to do with the answer.

These tests pin both halves — the exception is available where it is needed, and
the production slice never quietly acquires it.
"""

import unittest
from datetime import date

from emotorad_ai.agents import battery_support
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.tools.verification import (
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    VerificationStore,
    apply_verified_identity,
)


def _runtime(**kwargs) -> Runtime:
    registry = build_registry(verification=VerificationStore())
    return Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=ScriptedClaude([]),
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
        **kwargs,
    )


class SelfServiceIdentityToolsTests(unittest.TestCase):
    def test_agent_can_see_the_verification_tools_when_enabled(self):
        """The bug this exists to fix: asked for a number, then no way to use it."""
        runtime = _runtime(self_service_identity=True)
        names = runtime.agents[battery_support.AGENT_NAME].definition.tool_names
        self.assertIn(REQUEST_IDENTITY_VERIFICATION, names)
        self.assertIn(VERIFY_IDENTITY, names)

    def test_agent_cannot_see_them_by_default(self):
        """WhatsApp, Amiigo and voice are unchanged: identity arrives resolved."""
        runtime = _runtime()
        names = runtime.agents[battery_support.AGENT_NAME].definition.tool_names
        self.assertNotIn(REQUEST_IDENTITY_VERIFICATION, names)
        self.assertNotIn(VERIFY_IDENTITY, names)

    def test_the_production_tool_slice_is_left_alone(self):
        """The module constant is the production contract. Widening it here would
        hand the verification tools to every channel that already has identity."""
        _runtime(self_service_identity=True)
        self.assertNotIn(REQUEST_IDENTITY_VERIFICATION, battery_support.TOOL_NAMES)
        self.assertNotIn(VERIFY_IDENTITY, battery_support.TOOL_NAMES)

    def test_the_agents_own_tools_survive(self):
        """Extending the slice must add, never replace."""
        runtime = _runtime(self_service_identity=True)
        names = runtime.agents[battery_support.AGENT_NAME].definition.tool_names
        for name in battery_support.TOOL_NAMES:
            self.assertIn(name, names)

    def test_a_tool_absent_from_the_registry_is_not_offered(self):
        """Without a VerificationStore the tools are never registered. Naming them
        anyway would advertise a tool the model cannot call."""
        registry = build_registry()
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False),
            registry=registry,
            llm=ScriptedClaude([]),
            log=EventLog(path=None),
            resolver=IdentityResolver(registry),
            self_service_identity=True,
        )
        names = runtime.agents[battery_support.AGENT_NAME].definition.tool_names
        self.assertNotIn(REQUEST_IDENTITY_VERIFICATION, names)



class VerifiedPhoneReachesTheToolsTests(unittest.TestCase):
    """Proving a code must actually change what the agent can look up.

    `VerificationStore` records the number a conversation proved, but nothing
    read it back. The resolver hydrates from `message.identity`, which for an
    anonymous visitor carries no phone, so `lookup_warranty_record` was refused
    for want of one on every turn after a correct code as surely as before it.
    The customer typed a code and nothing whatsoever changed.
    """

    def setUp(self) -> None:
        self.store = VerificationStore()

    def _anonymous(self) -> InboundMessage:
        return InboundMessage(
            conversation_id="c1",
            persona="customer",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
            channel="website_chat",
            message_text="my battery is dead",
        )

    def test_a_proved_number_arrives_on_the_identity(self):
        self.store.issue("c1", "+919876500000", "123456")
        self.store.check("c1", "123456")
        message = apply_verified_identity(self._anonymous(), self.store)
        self.assertEqual(message.identity.phone, "+919876500000")
        self.assertEqual(message.identity.strength, VERIFIED)

    def test_the_disclosure_gate_opens_only_then(self):
        """`may_disclose` is the gate the whole design leans on. It must follow
        the proof, not the asking."""
        self.store.issue("c1", "+919876500000", "123456")
        self.store.check("c1", "123456")
        message = apply_verified_identity(self._anonymous(), self.store)
        self.assertTrue(message.identity.may_disclose)

    def test_an_issued_code_alone_proves_nothing(self):
        """Sending a code must never authorise anything. Otherwise anyone could
        type any number and be told whose bikes it owns."""
        self.store.issue("c1", "+919876500000", "123456")
        message = apply_verified_identity(self._anonymous(), self.store)
        self.assertIsNone(message.identity.phone)
        self.assertEqual(message.identity.strength, ANONYMOUS)
        self.assertFalse(message.identity.may_disclose)

    def test_a_wrong_code_proves_nothing(self):
        self.store.issue("c1", "+919876500000", "123456")
        self.store.check("c1", "000000")
        message = apply_verified_identity(self._anonymous(), self.store)
        self.assertEqual(message.identity.strength, ANONYMOUS)

    def test_an_untouched_conversation_is_left_exactly_as_it_was(self):
        message = self._anonymous()
        self.assertIs(apply_verified_identity(message, self.store), message)

    def test_an_identity_the_channel_already_resolved_is_not_overwritten(self):
        """WhatsApp and Amiigo arrive verified. A stale store entry on the same
        conversation id must never redirect the lookup to another number."""
        self.store.issue("c1", "+919876500000", "123456")
        self.store.check("c1", "123456")
        already = InboundMessage(
            conversation_id="c1",
            persona="customer",
            identity=Identity(strength=VERIFIED, phone="+919812345678"),
            channel="whatsapp",
            message_text="hi",
        )
        message = apply_verified_identity(already, self.store)
        self.assertEqual(message.identity.phone, "+919812345678")

    def test_the_rest_of_the_identity_survives(self):
        """The browser cookie is how this visitor's pre-verification activity is
        found. Losing it on verification loses the enrichment it feeds."""
        self.store.issue("c1", "+919876500000", "123456")
        self.store.check("c1", "123456")
        message = apply_verified_identity(self._anonymous(), self.store)
        self.assertEqual(message.identity.em_aid, "aid-1")
        self.assertEqual(message.message_text, "my battery is dead")
        self.assertEqual(message.channel, "website_chat")


class IdentityArrivingMidTurnTests(unittest.TestCase):
    """Identity can change *during* a turn, and the injection must see it.

    The model verifies a code and looks the customer up in the same assistant
    turn — it has just been told the code was correct, so of course it does. The
    tool context was a snapshot taken before the first tool ran, so the lookup
    was refused for want of a phone the conversation had proved seconds earlier.
    The agent then retried, tripped the repeated-call breaker and handed the
    conversation to a human at the exact moment it succeeded.

    Observed live on 2026-09-20, conversation 37e08340: verify_identity ->
    verified True, then lookup_warranty_record -> missing_identity, then
    STUCK_AGENT, then escalation.
    """

    def test_a_phone_proved_mid_turn_is_injected(self):
        proved = {}
        context = ToolContext(
            conversation_id="c1",
            late={"phone": lambda: proved.get("phone")},
        )
        self.assertIsNone(context.value_for("phone"))
        proved["phone"] = "+919876500000"
        self.assertEqual(context.value_for("phone"), "+919876500000")

    def test_a_phone_already_known_is_never_re_resolved(self):
        """The channel's own identity wins. A late resolver is a fallback for a
        surface that had none, never an override of one that did."""
        context = ToolContext(
            conversation_id="c1",
            phone="+919812345678",
            late={"phone": lambda: "+919876500000"},
        )
        self.assertEqual(context.value_for("phone"), "+919812345678")

    def test_a_context_without_resolvers_behaves_as_before(self):
        context = ToolContext(conversation_id="c1", phone="+919812345678")
        self.assertEqual(context.value_for("phone"), "+919812345678")
        self.assertIsNone(context.value_for("dealer_id"))

    def test_runtime_facts_are_injectable(self):
        """A tool that declares injects=('evidence_seen',) gets the
        conversation's value, resolved at call time."""
        from emotorad_ai.tools.registry import ToolRegistry, ok

        registry = ToolRegistry()

        @registry.register("peek", "test", parameters={}, injects=("evidence_seen",))
        def peek(evidence_seen):
            return ok({"seen": evidence_seen})

        context = ToolContext(conversation_id="c1", late={"evidence_seen": lambda: True})
        self.assertEqual(registry.call("peek", {}, context)["data"]["seen"], True)


class VerifyThenLookUpInOneTurnTests(unittest.TestCase):
    """The regression test for the live failure of 2026-09-20.

    The model verified a code and called `lookup_warranty_record` in the same
    assistant turn. The lookup was refused for want of a phone, the model
    retried, the repeated-call breaker fired and the customer was handed to a
    human at the moment they successfully identified themselves.

    Telling the prompt to wait a turn does not fix this, and it was tried: the
    model did it anyway. Identity is enforced in code, so it is fixed in code.
    """

    def test_the_lookup_is_not_refused_on_the_turn_the_code_is_verified(self):
        calls = []

        class CapturingLog(EventLog):
            def tool_call(self, conversation_id, tool, arguments, result, **fields):
                calls.append((tool, result))
                super().tool_call(conversation_id, tool, arguments, result, **fields)

        store = VerificationStore()
        registry = build_registry(verification=store, today=date(2026, 7, 28))
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False),
            registry=registry,
            llm=ScriptedClaude(
                [
                    call_tool("verify_identity", {"code": "123456"}),
                    call_tool("lookup_warranty_record", {}),
                    say("Thanks, that code checked out."),
                ]
            ),
            log=CapturingLog(path=None),
            resolver=IdentityResolver(registry),
            self_service_identity=True,
            phone_resolver=store.verified_phone,
        )
        store.issue("conv-mid", "+919876543210", "123456")

        message = InboundMessage(
            conversation_id="conv-mid",
            persona="customer",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
            channel="website_chat",
            message_text="123456",
        )
        runtime.conversations.get("conv-mid").route_to(battery_support.AGENT_NAME)
        runtime.handle(message)

        lookups = [result for tool, result in calls if tool == "lookup_warranty_record"]
        self.assertEqual(len(lookups), 1, calls)
        # The failure this test exists for: refused for want of a phone the
        # conversation proved one tool call earlier.
        self.assertNotEqual(
            lookups[0].get("error", {}).get("code"), "missing_identity", lookups[0]
        )
        self.assertIn("bikes", lookups[0].get("data", {}), lookups[0])


if __name__ == "__main__":
    unittest.main()
