import json
import unittest

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import OpenRouterChat
from emotorad_ai.metrics import build_report, render
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import OpenRouterTransport
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from tests.fake_http import FakeServer

KEY = "sk-or-test-NEVER-IN-LOGS"


def events(cid, handled_by, cost, path=None, escalated=False):
    rows = [
        {"event": "inbound", "conversation_id": cid, "channel": "whatsapp", "text": "battery not charging"},
        {"event": "llm_turn", "conversation_id": cid, "usage": {"input_tokens": 100, "output_tokens": 10, "cost": cost}},
        {"event": "outcome", "conversation_id": cid, "handled_by": handled_by, "escalated": escalated},
    ]
    if path:
        rows.insert(1, {"event": "jev_decision", "conversation_id": cid, "path": path, "cost": 0.00002})
        rows.insert(2, {"event": "turn_path", "conversation_id": cid, "path": path})
    return rows


class CostReportTests(unittest.TestCase):
    def test_cost_per_resolved_counts_every_conversation_and_divides_by_the_resolved(self):
        log = events("a", "narrow_support", 0.0001, path="narrow") + events("b", "battery_support", 0.005, path="full", escalated=True)
        report = build_report(log)
        self.assertAlmostEqual(report.total_cost, 0.0001 + 0.005 + 0.00004)
        self.assertAlmostEqual(report.cost_per_resolved(), report.total_cost / 1)
        self.assertEqual(report.by_path, {"narrow": 1, "full": 1})
        text = render(report)
        self.assertIn("cost per resolved", text)
        self.assertIn("narrow", text)

    def test_no_resolved_conversation_is_infinite_cost(self):
        self.assertEqual(build_report(events("a", "llm_error", 0.001, escalated=True)).cost_per_resolved(), float("inf"))


class ModelInLogTests(unittest.TestCase):
    def test_llm_turn_records_the_model_and_the_key_never_reaches_the_log(self):
        reply = {"model": "deepseek/deepseek-v4-flash-20260731",
                 "choices": [{"finish_reason": "stop", "message": {"content": "Try another socket."}}],
                 "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.00001}}
        with FakeServer() as server:
            server.queue(200, reply)
            transport = OpenRouterTransport(api_key=KEY, base_url=server.url)
            registry = build_registry()
            runtime = Runtime(
                settings=Settings(log_path="", log_to_stdout=False),
                registry=registry,
                llm=OpenRouterChat("anthropic/claude-haiku-4.5", transport),
                narrow_llm=OpenRouterChat("deepseek/deepseek-v4-flash-0731", transport),
                jev=ScriptedJev([JevDecision(answers={Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)})]),
                standard_responses=[],
                log=EventLog(path=None),
                resolver=IdentityResolver(registry),
            )
            adapter = WebsiteChatAdapter(runtime.resolver)
            runtime.handle(adapter.to_message({"conversation_id": "c", "session_token": "sess-ananya", "text": "my battery won't charge"}))
        [turn] = [e for e in runtime.log.events if e["event"] == "llm_turn"]
        # The id OpenRouter reports answering with, not the one the client asked for.
        self.assertEqual(turn["model"], "deepseek/deepseek-v4-flash-20260731")
        self.assertEqual(turn["usage"]["cost"], 0.00001)
        self.assertNotIn(KEY, json.dumps(runtime.log.events, default=str))


if __name__ == "__main__":
    unittest.main()
