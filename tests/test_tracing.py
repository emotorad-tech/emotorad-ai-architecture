"""Tracing: the event log fans out to sinks, and the Langfuse sink maps our
events onto traces. No network anywhere in here; the Langfuse client is a fake
with the same method names as the SDK."""

import io
import unittest
from contextlib import redirect_stderr

from emotorad_ai.llm import call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.tools.mocks import SEARCH_BATTERY_KNOWLEDGE
from emotorad_ai.tracing import LangfuseSink, langfuse_sink_from_env

from tests.test_agent_and_runtime import make_runtime, send


class EventLogSinkTests(unittest.TestCase):
    def test_every_event_reaches_each_sink_after_redaction(self):
        seen = []
        log = EventLog(sinks=[seen.append])
        log.emit("inbound", "c1", text="call me on 9876543210")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["event"], "inbound")
        self.assertNotIn("9876543210", seen[0]["text"])

    def test_a_failing_sink_is_reported_and_never_raises_into_the_turn(self):
        def broken(event):
            raise RuntimeError("langfuse down")

        seen = []
        log = EventLog(sinks=[broken, seen.append])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            event = log.emit("inbound", "c1", text="hi")
        self.assertEqual(event["event"], "inbound")
        self.assertEqual(len(log.events), 1)
        self.assertEqual(len(seen), 1, "one sink failing must not starve the next")
        self.assertIn("tracing sink failed: RuntimeError", stderr.getvalue())


class TimingFieldTests(unittest.TestCase):
    """The three facts a trace needs that the events did not carry before:
    which model answered, how long it took, and how long the whole turn took."""

    def test_llm_turn_records_model_and_duration(self):
        log = EventLog()
        log.llm_turn("c1", "battery_support", 1, "end_turn", usage={"input_tokens": 10},
                     model="claude-opus-5", duration_ms=412)
        event = log.events[-1]
        self.assertEqual(event["model"], "claude-opus-5")
        self.assertEqual(event["duration_ms"], 412)

    def test_tool_call_records_duration(self):
        log = EventLog()
        log.tool_call("c1", "lookup_warranty_record", {}, {"data": {}}, duration_ms=37)
        self.assertEqual(log.events[-1]["duration_ms"], 37)

    def test_outcome_records_time_since_the_inbound_event(self):
        clock = iter([100.0, 101.25])
        log = EventLog(clock=lambda: next(clock))
        log.emit("inbound", "c1")
        log.outcome("c1", "battery_support", False, None, "done")
        self.assertEqual(log.events[-1]["duration_ms"], 1250)

    def test_outcome_without_an_inbound_has_no_duration(self):
        log = EventLog()
        log.outcome("c1", "battery_support", False, None, "done")
        self.assertIsNone(log.events[-1]["duration_ms"])


class RuntimeTimingTests(unittest.TestCase):
    """The agent loop and the runtime actually fill the fields in."""

    def _events(self):
        runtime, adapter, _ = make_runtime(
            [call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "x"}, "toolu_1"), say("done")]
        )
        send(runtime, adapter, "my battery won't charge")
        return runtime.log.events

    def test_the_agent_loop_times_each_model_call(self):
        turns = [e for e in self._events() if e["event"] == "llm_turn"]
        self.assertTrue(turns)
        for turn in turns:
            self.assertIsInstance(turn["duration_ms"], int)
            self.assertGreaterEqual(turn["duration_ms"], 0)

    def test_the_agent_loop_times_each_tool_call(self):
        calls = [e for e in self._events() if e["event"] == "tool_call"]
        self.assertTrue(calls)
        for call in calls:
            self.assertIsInstance(call["duration_ms"], int)

    def test_every_model_call_is_announced_before_it_is_made(self):
        events = [e["event"] for e in self._events()]
        self.assertEqual(events.count("llm_request"), events.count("llm_turn"))
        self.assertLess(events.index("llm_request"), events.index("llm_turn"))

    def test_every_tool_call_is_announced_before_it_is_made(self):
        events = [e["event"] for e in self._events()]
        self.assertEqual(events.count("tool_request"), events.count("tool_call"))
        self.assertLess(events.index("tool_request"), events.index("tool_call"))

    def test_the_safety_branch_announces_its_ticket_call_too(self):
        runtime, adapter, _ = make_runtime([])
        send(runtime, adapter, "the battery is bulging")
        events = [e["event"] for e in runtime.log.events]
        self.assertIn("tool_request", events)
        self.assertLess(events.index("tool_request"), events.index("tool_call"))

    def test_the_safety_branch_times_its_own_ticket_call(self):
        runtime, adapter, _ = make_runtime([])
        send(runtime, adapter, "the battery is bulging")
        calls = [e for e in runtime.log.events if e["event"] == "tool_call"]
        self.assertTrue(calls, "the safety branch raises a ticket deterministically")
        self.assertIsInstance(calls[-1]["duration_ms"], int)

    def test_the_turn_outcome_carries_the_whole_handler_time(self):
        outcome = [e for e in self._events() if e["event"] == "outcome"][-1]
        self.assertIsInstance(outcome["duration_ms"], int)


class FakeObservation:
    """Records what the sink asks of the SDK. Same method names as
    langfuse's LangfuseSpan / LangfuseGeneration, nothing else."""

    def __init__(self, name, as_type, **kwargs):
        self.name = name
        self.as_type = as_type
        self.kwargs = kwargs
        self.children = []
        self.updates = []
        self.trace_updates = []
        self.ended = False

    def start_observation(self, *, name, as_type="span", **kwargs):
        child = FakeObservation(name, as_type, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def update_trace(self, **kwargs):
        self.trace_updates.append(kwargs)
        return self

    def end(self, **kwargs):
        self.ended = True
        return self

    def merged_updates(self):
        merged = {}
        for update in self.updates:
            merged.update(update)
        return merged

    def merged_trace(self):
        merged = {}
        for update in self.trace_updates:
            merged.update(update)
        return merged


class FakeLangfuse:
    def __init__(self):
        self.roots = []
        self.flushed = 0

    def start_observation(self, *, name, as_type="span", **kwargs):
        root = FakeObservation(name, as_type, **kwargs)
        self.roots.append(root)
        return root

    def flush(self):
        self.flushed += 1


def inbound(conversation_id="c1", text="my battery won't charge", persona="customer", channel="website_chat"):
    return {"event": "inbound", "conversation_id": conversation_id, "text": text,
            "persona": persona, "channel": channel, "pill_clicked": None}


def outcome(conversation_id="c1", text="try the charger", handled_by="battery_support",
            escalated=False, ticket_id=None, duration_ms=900):
    return {"event": "outcome", "conversation_id": conversation_id, "text": text,
            "handled_by": handled_by, "escalated": escalated, "ticket_id": ticket_id,
            "duration_ms": duration_ms}


class LangfuseSinkTraceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeLangfuse()
        self.sink = LangfuseSink(self.client)

    def test_an_inbound_message_opens_one_trace_per_turn_grouped_by_conversation(self):
        self.sink(inbound())
        self.assertEqual(len(self.client.roots), 1)
        root = self.client.roots[0]
        self.assertEqual(root.name, "customer-turn")
        self.assertEqual(root.kwargs["input"], "my battery won't charge")
        trace = root.merged_trace()
        self.assertEqual(trace["session_id"], "c1")
        self.assertEqual(trace["name"], "customer-turn")
        self.assertIn("website_chat", trace["tags"])
        self.assertIn("customer", trace["tags"])

    def test_identity_resolution_puts_the_cluster_not_the_phone_on_the_trace(self):
        self.sink(inbound())
        self.sink({"event": "identity_resolved", "conversation_id": "c1", "persona": "customer",
                   "method": "cookie", "cluster_id": "clu_1", "strength": "verified"})
        trace = self.client.roots[0].merged_trace()
        self.assertEqual(trace["user_id"], "clu_1")
        self.assertNotIn("phone", str(trace))

    def test_the_outcome_closes_the_trace_with_the_reply_as_output(self):
        self.sink(inbound())
        self.sink(outcome(ticket_id="T-1", escalated=True))
        root = self.client.roots[0]
        self.assertTrue(root.ended)
        self.assertEqual(root.merged_updates()["output"], "try the charger")
        trace = root.merged_trace()
        self.assertEqual(trace["output"], "try the charger")
        self.assertEqual(trace["metadata"]["handled_by"], "battery_support")
        self.assertTrue(trace["metadata"]["escalated"])
        self.assertEqual(trace["metadata"]["ticket_id"], "T-1")
        self.assertEqual(trace["metadata"]["duration_ms"], 900)
        self.assertNotIn("c1", self.sink.open_turns)

    def test_events_without_an_open_turn_are_ignored(self):
        self.sink(outcome())
        self.sink({"event": "llm_request", "conversation_id": "c9", "agent": "x", "iteration": 1})
        self.assertEqual(self.client.roots, [])

    def test_a_second_inbound_on_an_open_turn_closes_the_first(self):
        self.sink(inbound())
        self.sink(inbound(text="hello again"))
        self.assertTrue(self.client.roots[0].ended)
        self.assertFalse(self.client.roots[1].ended)

    def test_flush_reaches_the_client(self):
        self.sink.flush()
        self.assertEqual(self.client.flushed, 1)


class LangfuseSinkObservationTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeLangfuse()
        self.sink = LangfuseSink(self.client)
        self.sink(inbound())
        self.root = self.client.roots[0]

    def ev(self, event, **fields):
        fields.setdefault("conversation_id", "c1")
        fields["event"] = event
        self.sink(fields)

    def test_routing_opens_an_agent_observation_named_after_the_sub_agent(self):
        self.ev("routed", agent="battery_support", reason="pill:battery_issue->battery")
        agent = self.root.children[-1]
        self.assertEqual((agent.name, agent.as_type), ("battery_support", "agent"))
        self.assertEqual(agent.kwargs["metadata"]["reason"], "pill:battery_issue->battery")

    def test_a_model_call_is_a_generation_priced_from_the_api_usage(self):
        self.ev("routed", agent="battery_support", reason="r")
        self.ev("llm_request", agent="battery_support", iteration=1)
        self.ev("llm_turn", agent="battery_support", iteration=1, stop_reason="end_turn",
                model="claude-opus-5", duration_ms=412,
                usage={"input_tokens": 1200, "output_tokens": 80,
                       "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0,
                       "server_tool_use": None})
        agent = self.root.children[-1]
        gen = agent.children[-1]
        self.assertEqual(gen.as_type, "generation")
        self.assertTrue(gen.ended)
        update = gen.merged_updates()
        self.assertEqual(update["model"], "claude-opus-5")
        self.assertEqual(update["usage_details"], {
            "input": 1200, "output": 80, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0,
        })
        self.assertEqual(update["metadata"]["stop_reason"], "end_turn")
        self.assertEqual(update["metadata"]["iteration"], 1)

    def test_a_model_outage_closes_the_generation_as_an_error(self):
        self.ev("llm_request", agent="battery_support", iteration=1)
        self.ev("llm_error", agent="battery_support", iteration=1, error="APIConnectionError")
        gen = self.root.children[-1]
        self.assertTrue(gen.ended)
        update = gen.merged_updates()
        self.assertEqual(update["level"], "ERROR")
        self.assertEqual(update["status_message"], "APIConnectionError")

    def test_a_tool_call_is_a_tool_observation_with_arguments_and_result(self):
        self.ev("tool_request", tool="lookup_warranty_record")
        self.ev("tool_call", tool="lookup_warranty_record", arguments={"phone": "[redacted]"},
                ok=True, result={"data": {"bikes": []}}, duration_ms=37)
        tool = self.root.children[-1]
        self.assertEqual((tool.name, tool.as_type), ("lookup_warranty_record", "tool"))
        self.assertTrue(tool.ended)
        update = tool.merged_updates()
        self.assertEqual(update["input"], {"phone": "[redacted]"})
        self.assertEqual(update["output"], {"data": {"bikes": []}})
        self.assertNotIn("level", update)

    def test_knowledge_search_is_typed_as_a_retriever(self):
        self.ev("tool_request", tool="search_battery_knowledge")
        self.assertEqual(self.root.children[-1].as_type, "retriever")

    def test_a_failed_tool_call_is_marked_as_an_error(self):
        self.ev("tool_request", tool="place_order")
        self.ev("tool_call", tool="place_order", arguments={}, ok=False,
                result={"error": {"code": "quote_mismatch", "message": "m"}}, duration_ms=3)
        update = self.root.children[-1].merged_updates()
        self.assertEqual(update["level"], "ERROR")
        self.assertEqual(update["status_message"], "quote_mismatch")

    def test_a_guardrail_is_a_closed_guardrail_observation(self):
        self.ev("guardrail_triggered", guardrail="battery_safety", triggered_by="bulging")
        guard = self.root.children[-1]
        self.assertEqual((guard.name, guard.as_type), ("battery_safety", "guardrail"))
        self.assertEqual(guard.kwargs["input"], "bulging")
        self.assertTrue(guard.ended)

    def test_an_escalation_is_an_event_on_the_turn(self):
        self.ev("escalation", reason="customer_requested_human", ticket_id=None)
        event = self.root.children[-1]
        self.assertEqual((event.name, event.as_type), ("escalation", "event"))
        self.assertEqual(event.kwargs["metadata"]["reason"], "customer_requested_human")

    def test_the_agent_observation_ends_with_the_turn(self):
        self.ev("routed", agent="battery_support", reason="r")
        self.sink(outcome())
        self.assertTrue(self.root.children[-1].ended)


class SinkFromEnvTests(unittest.TestCase):
    def test_no_keys_means_no_sink(self):
        self.assertIsNone(langfuse_sink_from_env({}))
        self.assertIsNone(langfuse_sink_from_env({"LANGFUSE_PUBLIC_KEY": "pk"}))
        self.assertIsNone(langfuse_sink_from_env({"LANGFUSE_SECRET_KEY": "sk"}))

    def test_keys_build_a_client_against_the_eu_cloud_by_default(self):
        built = {}

        def factory(**kwargs):
            built.update(kwargs)
            return FakeLangfuse()

        sink = langfuse_sink_from_env(
            {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk", "EMOTORAD_AI_ENV": "stage"},
            factory=factory,
        )
        self.assertIsInstance(sink, LangfuseSink)
        self.assertEqual(built["public_key"], "pk")
        self.assertEqual(built["secret_key"], "sk")
        self.assertEqual(built["host"], "https://cloud.langfuse.com")
        self.assertEqual(built["environment"], "stage")

    def test_host_is_one_variable_so_self_hosting_is_a_config_change(self):
        built = {}
        langfuse_sink_from_env(
            {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk",
             "LANGFUSE_HOST": "https://langfuse.internal"},
            factory=lambda **kw: built.update(kw) or FakeLangfuse(),
        )
        self.assertEqual(built["host"], "https://langfuse.internal")


if __name__ == "__main__":
    unittest.main()
