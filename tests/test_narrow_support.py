import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import Agent
from emotorad_ai.agents.narrow_support import AGENT_NAME, TOOL_NAMES, build_narrow_definition
from emotorad_ai.config import Settings
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.tools.mocks import (
    BOOK_SERVICE_SLOT,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    GET_RECENT_TRIPS,
    GET_SERVICE_STATUS,
    LOOKUP_WARRANTY_RECORD,
    SEND_GUIDE_MEDIA,
    build_registry,
)
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 7, 28)


class NarrowFixture:
    """Shared setup. A mixin, not a base TestCase, so no test runs twice."""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.record = next(r for r in cls.kb.records if r.id == "battery-wont-charge")
        cls.registry = build_registry(today=TODAY)
        cls.resolver = IdentityResolver(cls.registry)
        cls.message = WebsiteChatAdapter(cls.resolver).to_message({"session_token": "sess-ananya", "text": "won't charge"})
        cls.resolved = cls.resolver.hydrate(cls.message)
        cls.warranty = {
            "tool": LOOKUP_WARRANTY_RECORD, "arguments": {}, "prefetched": True,
            "result": cls.registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c", phone="+919876543210")),
        }

    def prompt(self, prefetched=()):
        return build_narrow_definition(self.record, prefetched).build_system_prompt(self.message, self.resolved, "")


class NarrowDefinitionTests(NarrowFixture, unittest.TestCase):
    def test_the_prompt_holds_exactly_one_record(self):
        prompt = self.prompt()
        self.assertIn(self.record.title, prompt)
        for step in self.record.steps:
            self.assertIn(step, prompt)
        for other in self.kb.records:
            if other.id != self.record.id:
                self.assertNotIn(other.title, prompt)

    def test_the_prompt_is_small(self):
        self.assertLess(len(self.prompt([self.warranty])) // 4, 1500)

    def test_prefetched_results_are_in_the_prompt(self):
        self.assertIn("lookup_warranty_record", self.prompt([self.warranty]))
        self.assertNotIn("Looked up for this turn", self.prompt())

    def test_the_customer_facts_block_is_the_shared_one(self):
        self.assertIn("EMX Plus", self.prompt())

    def test_the_tool_slice_has_no_lookups_and_the_writes_the_flow_needs(self):
        definition = build_narrow_definition(self.record, ())
        self.assertEqual(definition.name, AGENT_NAME)
        # No diagnostic lookups. The app's two reads answer the customer's own
        # questions ("when is my next service due?", staging 2026-10-01).
        self.assertEqual(set(definition.tool_names), {SEND_GUIDE_MEDIA, CREATE_SUPPORT_TICKET, FIND_SERVICE_SLOTS,
                                                      BOOK_SERVICE_SLOT, GET_SERVICE_STATUS, GET_RECENT_TRIPS})
        self.assertEqual(tuple(definition.tool_names), TOOL_NAMES)


class PrefetchedAgentRunTests(NarrowFixture, unittest.TestCase):
    def test_prefetched_calls_lead_the_turns_tool_calls(self):
        llm = ScriptedClaude([say("Let's start with the socket.")])
        agent = Agent(build_narrow_definition(self.record, [self.warranty]), self.registry, llm, EventLog(path=None), Settings(log_path=""))
        turn = agent.run(self.message, self.resolved, [], "", prefetched=[self.warranty])
        self.assertEqual(turn.tool_calls[0]["tool"], LOOKUP_WARRANTY_RECORD)
        self.assertEqual(turn.text, "Let's start with the socket.")


class SubCategoryStateTests(unittest.TestCase):
    def test_changing_bike_or_handing_back_clears_the_sub_category(self):
        state = ConversationState(conversation_id="c")
        state.select_bike("A")
        state.sub_category = "battery-wont-charge"
        state.select_bike("B")
        self.assertIsNone(state.sub_category)
        state.sub_category = "battery-wont-charge"
        state.hand_back("resolved")
        self.assertIsNone(state.sub_category)


if __name__ == "__main__":
    unittest.main()
