"""Conformance with the build plan — the divergences a QC pass found.

Each test here corresponds to something `docs/Emotorad_Platform_Build_Plan.md` or
`docs/Emotorad_HLD_Current.md` states as a requirement, where the code had
quietly stopped matching it. They exist so the drift cannot happen again silently.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WhatsAppAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import Reply
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 6)


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


def whatsapp(runtime, text, sender="919876543210", conversation_id="c1"):
    return runtime.handle(
        WhatsAppAdapter(runtime.resolver).to_message(
            {"from": sender, "text": text, "conversation_id": conversation_id}
        )
    )


class EnrichmentReachesTheModelTests(unittest.TestCase):
    """§3.2.1 — enrichment was computed, logged, and then thrown away."""

    def test_the_enriched_block_is_actually_in_the_system_prompt(self):
        runtime, llm = make_runtime([say("Let me help.")])
        # Give this cluster some browsing to summarise.
        runtime.enricher_events = None
        original = runtime.enricher.build

        def with_events(resolved, **kwargs):
            return original(
                resolved,
                events=[{"properties": {"model": "EMX Plus"}}] * 2,
                signals=["emi_page_viewed"],
            )

        runtime.enricher.build = with_events
        whatsapp(runtime, "battery won't charge")

        prompt = llm.requests[0]["system"]
        self.assertIn("Recently viewed", prompt)
        self.assertIn("EMI options", prompt)

    def test_it_is_built_once_per_conversation_not_once_per_turn(self):
        # Rebuilding it every turn costs queries, moves the block in the prompt
        # (defeating prefix caching), and cannot change the answer.
        runtime, llm = make_runtime([say("First."), say("Second."), say("Third.")])
        calls = []
        original = runtime.enricher.build
        runtime.enricher.build = lambda *a, **k: (calls.append(1), original(*a, **k))[1]

        for _ in range(3):
            whatsapp(runtime, "still not charging")

        self.assertEqual(len(calls), 1, "enrichment rebuilt per turn")

    def test_enrichment_never_authorises_an_ownership_claim(self):
        # It may personalise; only the warranty record may authorise.
        runtime, llm = make_runtime([say("Let me help.")])
        whatsapp(runtime, "battery won't charge")
        prompt = llm.requests[0]["system"]
        self.assertIn("never treat as proof of ownership or coverage", prompt)


class ReplyAttachmentsTests(unittest.TestCase):
    """§3.1 — 'the reply shape needs its own attachments field'."""

    def test_the_reply_contract_carries_attachments(self):
        self.assertIn("attachments", Reply("c1", "hi", "agent").to_dict())

    def test_media_on_a_retrieved_record_reaches_the_customer(self):
        # Knowledge records carry diagrams and clips. Before this, the media was
        # authored, retrieved, and silently dropped on the floor.
        runtime, _ = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging", "topic": "battery"}, "t1"),
            say("Check the charger is fully seated."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        self.assertTrue(reply.attachments)
        self.assertIn("charger-seating", reply.attachments[0].url)

    def test_a_reply_with_no_media_carries_an_empty_list_not_none(self):
        runtime, _ = make_runtime([say("Try a different socket.")])
        self.assertEqual(whatsapp(runtime, "battery won't charge").attachments, [])

    def test_the_same_image_is_not_attached_twice(self):
        runtime, _ = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "charger light"}, "t2"),
            say("Check the charger seating."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        urls = [a.url for a in reply.attachments]
        self.assertEqual(len(urls), len(set(urls)))


class StuckAgentTests(unittest.TestCase):
    """HLD §6 — 'duplicate tool calls break the loop early'. Only the cap existed."""

    def test_the_same_call_twice_breaks_the_loop_and_hands_over(self):
        runtime, llm = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t2"),  # identical
            say("never reached"),
        ])
        reply = whatsapp(runtime, "battery not charging")

        self.assertTrue(reply.escalated)
        self.assertIn("pass you to someone", reply.text)
        self.assertLess(len(llm.requests), 3, "should not burn the full iteration budget")

    def test_it_is_logged_so_the_loop_can_be_diagnosed(self):
        runtime, _ = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "x"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "x"}, "t2"),
            say("never reached"),
        ])
        whatsapp(runtime, "battery not charging")
        self.assertTrue(any(e["event"] == "stuck_agent" for e in runtime.log.events))

    def test_different_arguments_are_not_treated_as_stuck(self):
        runtime, llm = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "charger light off"}, "t2"),
            say("Try a different socket."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        self.assertFalse(reply.escalated)


if __name__ == "__main__":
    unittest.main()
