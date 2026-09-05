# tests/test_playground_runtime.py
"""The playground drives the same Runtime production does. These tests pin
that: routing, tools, guardrails and disclosure all happen, and the trace
reports what the platform decided."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from emotorad_ai.bots import DRAFT, BotCatalogue, builtin_specs, spec_from_dict
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.knowledge import KnowledgeError
from emotorad_ai.llm import OfflinePlanner, ScriptedClaude, call_tool, say
from emotorad_ai.playground_runtime import PlaygroundSession, knowledge_base_with_drafts
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 6)

BRAKES = spec_from_dict(
    {
        "name": "brakes_support",
        "persona": "customer",
        "topic": "brakes",
        "keywords": ["brake", "braking"],
        "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
        "prompt": "You are the brake support assistant.\n",
    },
    DRAFT,
)


def ananya():
    registry = build_registry(today=TODAY)
    message = InboundMessage(
        "preview", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", ""
    )
    return IdentityResolver(registry).hydrate(message)


def session(script, bot="battery_support", extra=(BRAKES,)):
    catalogue = BotCatalogue(builtin_specs() + list(extra))
    return PlaygroundSession(
        bot=bot, resolved=ananya(), channel="website_chat", catalogue=catalogue,
        llm=ScriptedClaude(script), today=TODAY,
    )


class SessionTests(unittest.TestCase):
    def test_a_turn_runs_triage_tools_and_disclosure(self):
        s = session([call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"), say("Try another socket.")])
        turn = s.send("battery won't charge")
        self.assertEqual(turn.reply.handled_by, "battery_support")
        self.assertIn("Try another socket.", turn.reply.text)
        kinds = [e["event"] for e in turn.events]
        self.assertIn("routed", kinds)
        self.assertIn("tool_call", kinds)
        self.assertEqual(turn.reply.metadata["tool_calls"], [SEARCH_KNOWLEDGE])

    def test_the_conversation_persists_across_turns(self):
        s = session([say("First."), say("Second.")])
        s.send("battery won't charge")
        turn = s.send("still nothing")
        self.assertEqual(turn.reply.handled_by, "battery_support")
        state = s.runtime.conversations.get(s.conversation_id)
        self.assertEqual(state.turns, 2)

    def test_a_pill_skips_classification(self):
        s = session([say("Which light is on?")])
        turn = s.send("hi", pill="battery_issue")
        routed = next(e for e in turn.events if e["event"] == "routed")
        self.assertEqual(routed["reason"], "pill:battery_issue->battery")

    def test_guardrails_run_before_the_model(self):
        s = session([])
        turn = s.send("the battery is swelling")
        self.assertTrue(turn.reply.escalated)
        self.assertEqual(s.runtime.llm.requests, [])
        self.assertIn("guardrail_triggered", [e["event"] for e in turn.events])

    def test_a_draft_bot_is_selectable_and_reached(self):
        s = session([say("Let me help with the brake.")], bot="brakes_support")
        turn = s.send("my brake squeaks")
        self.assertEqual(turn.reply.handled_by, "brakes_support")

    def test_set_prompt_on_a_yaml_bot_takes_effect_without_losing_the_conversation(self):
        s = session([say("one"), say("two")], bot="brakes_support")
        s.send("brake squeaks")
        s.set_prompt("You are a terse brake assistant.\n")
        s.send("still squeaking")
        self.assertTrue(s.runtime.llm.requests[1]["system"].startswith("You are a terse brake assistant."))
        self.assertEqual(s.runtime.conversations.get(s.conversation_id).turns, 2)

    def test_set_prompt_on_a_built_in_is_scoped_to_the_call(self):
        from emotorad_ai.agents import battery_support

        original = battery_support._BASE_PROMPT
        s = session([say("ok")])
        s.set_prompt("You are a terse battery assistant.\n")
        s.send("battery won't charge")
        self.assertTrue(s.runtime.llm.requests[0]["system"].startswith("You are a terse battery assistant."))
        self.assertIs(battery_support._BASE_PROMPT, original)

    def test_system_prompt_preview_matches_what_the_model_gets(self):
        s = session([say("ok")])
        preview = s.system_prompt_preview()
        s.send("battery won't charge")
        self.assertEqual(preview, s.runtime.llm.requests[0]["system"])

    def test_it_works_with_the_offline_planner(self):
        catalogue = BotCatalogue(builtin_specs())
        s = PlaygroundSession(
            bot="battery_support", resolved=ananya(), channel="website_chat",
            catalogue=catalogue, llm=OfflinePlanner(), today=TODAY,
        )
        turn = s.send("battery won't charge")
        self.assertEqual(turn.reply.handled_by, "battery_support")
        self.assertFalse(turn.reply.escalated)


class DraftKnowledgeTests(unittest.TestCase):
    RECORD = (
        "id: brakes-squeak\ntitle: Brakes squeak\ntopic: brakes\n"
        "symptoms: [squeak, squeal]\nsteps: [Check the pads for glazing.]\n"
    )

    def test_draft_records_are_added_to_the_published_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            (drafts / "knowledge" / "brakes").mkdir(parents=True)
            (drafts / "knowledge" / "brakes" / "squeak.yaml").write_text(self.RECORD, encoding="utf-8")
            kb = knowledge_base_with_drafts(drafts)
            self.assertIn("brakes-squeak", {r.id for r in kb.records})
            self.assertIn("motor-noise", {r.id for r in kb.records})

    def test_no_drafts_directory_is_just_the_published_set(self):
        self.assertEqual(
            {r.id for r in knowledge_base_with_drafts(None).records},
            {r.id for r in knowledge_base_with_drafts(Path("/nonexistent")).records},
        )

    def test_a_draft_id_that_shadows_a_published_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            (drafts / "knowledge" / "motor").mkdir(parents=True)
            (drafts / "knowledge" / "motor" / "noise.yaml").write_text(
                self.RECORD.replace("brakes-squeak", "motor-noise"), encoding="utf-8"
            )
            with self.assertRaises(KnowledgeError):
                knowledge_base_with_drafts(drafts)


if __name__ == "__main__":
    unittest.main()
