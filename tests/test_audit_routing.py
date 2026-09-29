"""Routing and model audit, 2026-09-29 (branch test-audit/routing).

Each test closes a gap shown by mutation testing: a small, plausible fault in
the production code that every test before this file let through. The mutant
a test kills is named in its docstring (the ids are the audit's), so it can be
re-applied by hand to check the test still bites.

No network and no ambient environment: HTTP goes through a stub opener or a
fake transport, and every environment variable a test depends on is set by
the test itself (the config defaults are read in a child process with the
EMOTORAD_ variables removed).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.decisions import (
    NONE,
    Q_CATEGORY,
    Q_ERROR_CODE,
    Q_LANGUAGE,
    Q_STANDARD,
    Q_SUB_CATEGORY,
    RECENT_TURNS,
    RoutingConfigError,
    Thresholds,
    build_catalogue,
    build_state,
    load_thresholds,
    route,
)
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choice, choose, noul, parse_decision
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.llm import ScriptedClaude, from_openai_response, say, to_openai_messages
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import (
    CHAT_PATH,
    DECISIONS_PATH,
    OpenRouterAuthError,
    OpenRouterBadResponse,
    OpenRouterError,
    OpenRouterPaymentRequired,
    OpenRouterRateLimited,
    OpenRouterRequestError,
    OpenRouterTransport,
    OpenRouterUnavailable,
)
from emotorad_ai.runtime import Runtime
from emotorad_ai.standard_responses import StandardResponse
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.video_summary import INLINE_LIMIT, OpenRouterVideoSummariser, summariser_from_env
from emotorad_ai.wiring import build_models
from tests.fake_http import FakeTransport

TODAY = date(2026, 7, 28)
T = Thresholds()
KEY = "sk-or-test-AUDIT-NEVER-LEAK"
SRC = Path(__file__).resolve().parents[1] / "src"

NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}
UNSURE = {Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose(NONE, 0.5)}


def decide(**answers):
    return JevDecision(answers=answers)


def chat_reply(text="ok", **extra):
    body = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    body.update(extra)
    return body


# -- decisions.route ----------------------------------------------------------

class RouteGapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        thanks = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome."})
        cls.catalogue = build_catalogue(KnowledgeBase(), standard=[thanks])

    def go(self, decision, error=None, current=None):
        return route(decision, error, T, self.catalogue, current_sub_category=current, bike={"product_name": "EMX Plus"})

    def test_a_follow_up_that_names_the_current_record_stays_on_it_when_the_category_is_unsure(self):
        """D05: rule 4 treated a confident naming of the *current* record as
        "something else" and sent a plain follow-up to the full agent."""
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.5), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}),
                         current="battery-wont-charge")
        self.assertEqual((result.path, result.sub_category), ("narrow", "battery-wont-charge"))
        self.assertIn("narrow_continue:battery-wont-charge", result.reasons)

    def test_a_jev_failure_mid_flow_goes_to_the_full_agent_not_the_current_record(self):
        """D06: only an empty message may continue on the record without a
        decision. A failed Jev call is the safe direction, the full agent."""
        for error in ("unavailable", "rate_limited", "bad_response", "auth"):
            with self.subTest(error):
                result = self.go(None, error=error, current="battery-wont-charge")
                self.assertEqual((result.path, result.sub_category), ("full", None))
                self.assertEqual(result.reasons, ("jev_error:%s" % error,))

    def test_the_error_code_prefetch_uses_the_error_code_threshold(self):
        """D19: a score between the sub-category and error-code thresholds must
        not prefetch a code lookup."""
        self.assertLess(T.sub_category, T.error_code)
        between = (T.sub_category + T.error_code) / 2

        def lookups(p):
            result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9),
                                       Q_ERROR_CODE: choose("E07", p)}))
            return result.error_code, [c.tool for c in result.prefetch]

        self.assertEqual(lookups(between), (None, []))
        self.assertEqual(lookups(T.error_code), ("E07", ["lookup_error_code"]))

    def test_a_standard_reply_needs_the_language_threshold(self):
        """D20: a language score between the sub-category and language
        thresholds is not confident enough to pick a reply's language."""
        self.assertLess(T.sub_category, T.language)
        between = (T.sub_category + T.language) / 2
        unsure = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.99), Q_LANGUAGE: choose("english", between)}))
        self.assertEqual(unsure.path, "full")
        self.assertIn("standard_language_unsure", unsure.reasons)
        sure = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.99), Q_LANGUAGE: choose("english", T.language)}))
        self.assertEqual((sure.path, sure.language), ("standard", "english"))


# -- decisions.build_state ----------------------------------------------------

class StateGapTests(unittest.TestCase):
    def test_only_the_last_three_customer_turns_are_sent(self):
        """D13: the window was one turn too wide."""
        history = []
        for i in range(1, 6):
            history += [{"role": "user", "content": "turn %d" % i},
                        {"role": "assistant", "content": [{"type": "text", "text": "reply %d" % i}]}]
        state = build_state("now", history, "whatsapp")
        self.assertEqual(RECENT_TURNS, 3)
        self.assertEqual(state["recent_turns"], ["user: turn 3", "user: turn 4", "user: turn 5"])

    def test_a_name_typed_in_another_case_is_still_redacted(self):
        """D15: customers type their own name in lower case; a case-sensitive
        match sent it to Jev."""
        state = build_state("ananya here, ANANYA RAO", [{"role": "user", "content": "this is ananya rao"}], "whatsapp",
                            redact=("Ananya Rao", "Ananya", "Rao"))
        dumped = json.dumps(state).lower()
        self.assertNotIn("ananya", dumped)
        self.assertNotIn("rao", dumped)
        self.assertIn("[redacted] here", state["message"])


# -- decisions.load_thresholds ------------------------------------------------

class ThresholdFileGapTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "t.yaml")
            path.write_text(text, encoding="utf-8")
            return load_thresholds(path)

    def test_a_threshold_of_exactly_one_is_allowed(self):
        """D17: the range is (0, 1]; 1.0 is how a path is switched to
        "only when certain", and refusing it would fail the deploy."""
        self.assertEqual(self.load("category: 1.0\n").category, 1.0)
        self.assertEqual(self.load("tools: {lookup_warranty_record: 1}\n").tools["lookup_warranty_record"], 1.0)

    def test_a_yaml_boolean_is_not_a_threshold(self):
        """D18: `true` is an int in Python and would load as 1.0."""
        for text in ("category: true\n", "tools: {lookup_warranty_record: yes}\n"):
            with self.subTest(text), self.assertRaises(RoutingConfigError):
                self.load(text)


# -- jev.parse_decision -------------------------------------------------------

class JevParseGapTests(unittest.TestCase):
    QUESTIONS = {
        "category": choice("Which area?", {"battery": "B", "motor": "M", "none_of_these": "O"}),
        "needs_warranty_lookup": noul("Warranty?"),
    }

    def test_certainty_is_a_valid_probability(self):
        """J01: a probability of exactly 1.0 (or 0.0) is in range. Refusing it
        would send every fully confident answer to the full agent."""
        payload = {"answers": {
            "category": {"type": "choice", "choice": "battery",
                         "probabilities": {"battery": 1.0, "motor": 0.0, "none_of_these": 0.0}, "confidence": 1.0},
            "needs_warranty_lookup": {"type": "noul", "noul": 1.0},
        }}
        decision = parse_decision(payload, self.QUESTIONS)
        self.assertEqual(decision.answers["category"].p, 1.0)
        self.assertEqual(decision.answers["needs_warranty_lookup"].p, 1.0)


# -- openrouter.OpenRouterTransport -------------------------------------------

class _Body:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.raw


def answering(payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return OpenRouterTransport(api_key=KEY, base_url="http://127.0.0.1:9", opener=lambda request, timeout=None: _Body(raw))


class TransportGapTests(unittest.TestCase):
    def test_a_provider_error_inside_a_200_never_carries_the_key(self):
        """O03: the key was scrubbed from HTTP errors but not from an error
        body that arrives with status 200."""
        with self.assertRaises(OpenRouterUnavailable) as caught:
            answering({"error": {"code": 502, "message": "upstream rejected " + KEY}}).post("/x", {})
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIn("[key]", str(caught.exception))

    def test_the_code_inside_a_200_error_body_picks_the_error_type(self):
        """O04: every error body with status 200 became `unavailable`, so a
        provider's rate limit or auth failure was reported as an outage."""
        cases = [
            (429, OpenRouterRateLimited), (401, OpenRouterAuthError), (402, OpenRouterPaymentRequired),
            (400, OpenRouterRequestError), (503, OpenRouterUnavailable),
            (None, OpenRouterUnavailable), ("429", OpenRouterUnavailable), (True, OpenRouterUnavailable),
        ]
        for code, error in cases:
            with self.subTest(code=code), self.assertRaises(OpenRouterError) as caught:
                answering({"error": {"code": code, "message": "provider said no"}}).post("/x", {})
            self.assertIs(type(caught.exception), error)

    def test_json_that_is_not_an_object_is_a_bad_response(self):
        """O09."""
        with self.assertRaises(OpenRouterBadResponse):
            answering(b"[1, 2]").post("/x", {})


# -- llm: OpenRouter translation ----------------------------------------------

class ChatTranslationGapTests(unittest.TestCase):
    def test_tool_results_come_before_the_customers_words_in_the_same_turn(self):
        """L03: a `tool` message must directly follow the assistant turn that
        asked for it; text placed first breaks that pairing."""
        calls = {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup_warranty_record", "input": {}}]}
        for extra in ([{"type": "text", "text": "here it is"}],
                      [{"type": "text", "text": "here it is"},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBO"}}]):
            with self.subTest(len(extra)):
                turn = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}] + extra}
                out = to_openai_messages("S", [calls, turn])
                self.assertEqual([m["role"] for m in out], ["system", "assistant", "tool", "user"])
                self.assertEqual(out[2]["tool_call_id"], "toolu_1")

    def test_cached_prompt_tokens_are_reported(self):
        """L07: the tracer prices cache reads from this field."""
        usage = from_openai_response(chat_reply(usage={"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.0001,
                                                       "prompt_tokens_details": {"cached_tokens": 80}})).usage
        self.assertEqual(usage, {"input_tokens": 100, "output_tokens": 5, "cost": 0.0001, "cache_read_input_tokens": 80})
        self.assertNotIn("cache_read_input_tokens", from_openai_response(chat_reply()).usage)

    def test_a_content_filter_stop_is_a_refusal(self):
        """L13."""
        self.assertEqual(from_openai_response(dict(chat_reply("..."), choices=[
            {"finish_reason": "content_filter", "message": {"content": "..."}}])).stop_reason, "refusal")


# -- wiring.build_models --------------------------------------------------------

class RecordingTransport(FakeTransport):
    """Stands in for OpenRouterTransport inside wiring: records how it was
    built and every post, and answers from a queue. Never touches a network."""

    made = []

    def __init__(self, **kwargs):
        super().__init__([
            {"answers": {"w": {"type": "noul", "noul": 0.2}}},
            chat_reply("from the full agent"),
            chat_reply("from the narrow agent"),
        ])
        self.kwargs = kwargs
        RecordingTransport.made.append(self)


class WiringGapTests(unittest.TestCase):
    def openrouter(self, **overrides):
        values = dict(mode="openrouter", fallback_model="anthropic/claude-haiku-4.5",
                      narrow_model="deepseek/deepseek-v4-flash-0731", openrouter_zdr=True)
        values.update(overrides)
        return Settings(**values)

    def test_zero_data_retention_follows_the_setting_on_both_reply_models(self):
        """W03: the setting was never read by the wiring."""
        for zdr in (True, False):
            with self.subTest(zdr=zdr):
                transport = FakeTransport([chat_reply(), chat_reply()])
                models = build_models(self.openrouter(openrouter_zdr=zdr), transport=transport)
                models.llm.create("S", [], [])
                models.narrow_llm.create("S", [], [])
                for call in transport.calls:
                    self.assertEqual(call["body"].get("provider"), {"zdr": True, "data_collection": "deny"} if zdr else None)

    def test_one_transport_built_from_the_settings_serves_jev_and_both_models(self):
        """W06: the base URL setting was ignored (and staging could not be
        pointed at a proxy)."""
        RecordingTransport.made = []
        settings = self.openrouter(openrouter_base_url="http://openrouter.test/api", openrouter_timeout=12.5)
        with mock.patch("emotorad_ai.wiring.OpenRouterTransport", RecordingTransport):
            models = build_models(settings)
        [transport] = RecordingTransport.made
        self.assertEqual(transport.kwargs, {"base_url": "http://openrouter.test/api", "timeout": 12.5})
        models.jev.decide({"message": "hi"}, {"w": noul("x")})
        models.llm.create("S", [], [])
        models.narrow_llm.create("S", [], [])
        self.assertEqual([c["path"] for c in transport.calls], [DECISIONS_PATH, CHAT_PATH, CHAT_PATH])
        self.assertEqual([c["body"]["model"] for c in transport.calls],
                         [settings.jev_model, "anthropic/claude-haiku-4.5", "deepseek/deepseek-v4-flash-0731"])


# -- config.Settings --------------------------------------------------------------

def settings_in_a_clean_environment(**extra):
    """Settings() in a child process whose environment has no EMOTORAD_
    variables (plus `extra`): the class-level defaults are read at import,
    so only a fresh interpreter shows what the code defaults to."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("EMOTORAD_") and k != "OPENROUTER_API_KEY"}
    env.update(extra)
    env["PYTHONPATH"] = str(SRC)
    code = ("import json; from emotorad_ai.config import Settings; s = Settings(); "
            "print(json.dumps({k: getattr(s, k) for k in ('mode', 'store', 'approval_mode', 'openrouter_zdr', "
            "'jev_model', 'narrow_model', 'fallback_model', 'openrouter_base_url', 'jev_timeout')}))")
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


class ConfigGapTests(unittest.TestCase):
    def test_mode_store_and_approval_mode_are_read_per_construction(self):
        """C02: the store read once at import ignored a patched environment
        (mode and approval mode have their own tests elsewhere; they ride
        along). Two different values each, so one differs from whatever the
        process started with."""
        for mode, store, approval in (("openrouter", "mongodb", "human"), ("anthropic", "memory", "bot")):
            with self.subTest(mode=mode), mock.patch.dict(
                os.environ, {"EMOTORAD_AI_MODE": mode, "EMOTORAD_STORE": store, "EMOTORAD_AI_APPROVAL_MODE": approval}
            ):
                settings = Settings()
                self.assertEqual((settings.mode, settings.store, settings.approval_mode), (mode, store, approval))

    def test_the_defaults_in_a_clean_environment(self):
        """C05: zero data retention on unless deliberately turned off. Also the
        spec's model defaults, which tests.test_openrouter_transport can only
        check against whatever environment the test run inherited."""
        defaults = settings_in_a_clean_environment()
        self.assertEqual(defaults, {
            "mode": "offline", "store": "memory", "approval_mode": "reasonable", "openrouter_zdr": True,
            "jev_model": "typesafe/jev-1.13", "narrow_model": "anthropic/claude-haiku-4.5",
            "fallback_model": "anthropic/claude-haiku-4.5", "openrouter_base_url": "https://openrouter.ai/api",
            "jev_timeout": 2.0,
        })
        self.assertFalse(settings_in_a_clean_environment(EMOTORAD_OPENROUTER_ZDR="0")["openrouter_zdr"])


# -- video_summary ------------------------------------------------------------------

class VideoGapTests(unittest.TestCase):
    def test_a_clip_of_exactly_the_inline_limit_is_sent(self):
        """V01: the limit is inclusive, as on the Gemini-direct path."""
        transport = FakeTransport([chat_reply("A grinding sound at 0:04.")])
        text = OpenRouterVideoSummariser(transport).summarise(b"\x00" * INLINE_LIMIT, "video/mp4")
        self.assertEqual(text, "A grinding sound at 0:04.")
        self.assertEqual(len(transport.calls), 1)

    def test_zero_data_retention_follows_the_environment(self):
        """V04: the choice only checked the class it built."""
        base = {"OPENROUTER_API_KEY": "sk-or-test-video"}
        self.assertTrue(summariser_from_env(base).zdr)
        self.assertTrue(summariser_from_env(dict(base, EMOTORAD_OPENROUTER_ZDR="1")).zdr)
        self.assertFalse(summariser_from_env(dict(base, EMOTORAD_OPENROUTER_ZDR="0")).zdr)

    def test_the_transport_is_built_from_the_environment(self):
        """V07: the base URL setting was ignored for videos."""
        made = []

        def transport(**kwargs):
            made.append(kwargs)
            return FakeTransport([chat_reply("A clip.")])

        env = {"OPENROUTER_API_KEY": "sk-or-test-video", "EMOTORAD_OPENROUTER_BASE_URL": "http://openrouter.test/api"}
        with mock.patch("emotorad_ai.openrouter.OpenRouterTransport", transport):
            summariser_from_env(env)
        [kwargs] = made
        self.assertEqual((kwargs["api_key"], kwargs["base_url"]), ("sk-or-test-video", "http://openrouter.test/api"))


# -- runtime: the routing nodes ---------------------------------------------------

def build_runtime(jev_decisions, fallback=(), narrow=(), fallback_llm=None, standard=()):
    registry = build_registry(today=TODAY)
    jev = ScriptedJev(jev_decisions)
    narrow_llm = ScriptedClaude(list(narrow))
    fallback_llm = fallback_llm or ScriptedClaude(list(fallback))
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=fallback_llm,
        narrow_llm=narrow_llm, jev=jev, standard_responses=list(standard), log=EventLog(path=None),
        resolver=IdentityResolver(registry), conversations=InMemoryConversationStore(),
    )
    return runtime, jev, narrow_llm, fallback_llm


def send(runtime, text, session="sess-ananya", **meta):
    message = WebsiteChatAdapter(runtime.resolver).to_message({"conversation_id": "conv-1", "session_token": session, "text": text})
    if meta:
        message = replace(message, entry_metadata=dict(message.entry_metadata, **meta))
    return runtime.handle(message)


class Unavailable:
    def create(self, system, messages, tools):
        raise OpenRouterUnavailable("down")


class RuntimeNodeGapTests(unittest.TestCase):
    def test_an_unknown_pin_does_not_skip_jev(self):
        """R02: a pin the runtime does not have is cleared, and the turn is
        routed as if there were none; it must not also bypass Jev."""
        runtime, jev, narrow, fallback = build_runtime([decide(**NARROW)], narrow=[say("Try another socket.")],
                                                       fallback=[say("From the full agent.")])
        reply = send(runtime, "my battery won't charge", pinned_agent="no_such_agent")
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertEqual(fallback.requests, [])

    def test_a_change_of_topic_clears_the_record_so_the_next_follow_up_is_not_answered_from_it(self):
        """R10: the full agent took the conversation to motor but kept the
        battery record, and the next unsure follow-up got battery steps."""
        runtime, jev, narrow, fallback = build_runtime(
            [decide(**NARROW),
             decide(**{Q_CATEGORY: choose("motor", 0.95), Q_SUB_CATEGORY: choose(NONE, 0.9)}),
             decide(**{Q_CATEGORY: choose("motor", 0.4), Q_SUB_CATEGORY: choose(NONE, 0.5)})],
            narrow=[say("Try another socket."), say("Battery steps again.")],
            fallback=[say("Does the noise change with speed?"), say("Please send a short video.")],
        )
        send(runtime, "my battery won't charge")
        self.assertEqual(send(runtime, "it charges now, but the motor makes a grinding noise").handled_by, "motor_support")
        reply = send(runtime, "yes, louder uphill")
        self.assertEqual(reply.handled_by, "motor_support")
        self.assertEqual(len(narrow.requests), 1)
        self.assertIsNone(runtime.conversations.get("conv-1").sub_category)

    def test_a_standard_reply_is_sent_in_the_language_jev_named(self):
        """R11: the node always sent the English reply."""
        thanks = StandardResponse("std-thanks", "approved", "K", "Only thanks.",
                                  {"english": "You're welcome.", "hinglish": "Aapka swagat hai."})
        runtime, *_ = build_runtime(
            [decide(**NARROW), decide(**{Q_STANDARD: choose("std-thanks", 0.97), Q_LANGUAGE: choose("hinglish", 0.95)})],
            narrow=[say("Try another socket.")], standard=[thanks],
        )
        send(runtime, "my battery won't charge")
        reply = send(runtime, "bahut shukriya, ho gaya")
        self.assertEqual(reply.handled_by, "standard:std-thanks")
        self.assertIn("Aapka swagat hai.", reply.text)
        self.assertNotIn("You're welcome.", reply.text)

    def test_a_standard_reply_that_claims_coverage_goes_to_the_full_agent(self):
        """R12: the node's own coverage and evidence backstop. The loader
        refuses such a reply; this is the check that survives a loader edit."""
        for bad in ("Good news, it's covered under warranty.", "Your battery is dead, so we will arrange a replacement."):
            with self.subTest(bad):
                reply_text = StandardResponse("std-bad", "approved", "K", "Anything.", {"english": bad})
                runtime, jev, narrow, fallback = build_runtime(
                    [decide(**{Q_STANDARD: choose("std-bad", 0.99), Q_LANGUAGE: choose("english", 0.99)})],
                    fallback=[say("Let me look that up for you.")], standard=[reply_text],
                )
                reply = send(runtime, "my battery won't charge")
                self.assertEqual(reply.handled_by, "battery_support")
                self.assertNotIn(bad, reply.text)
                self.assertEqual(len(fallback.requests), 1)
                blocked = [e for e in runtime.log.events if e.get("guardrail") == "standard_response_blocked"]
                self.assertEqual(len(blocked), 1)

    def test_a_handover_after_a_model_failure_keeps_the_message_once(self):
        """R15: without the rollback the customer's message was saved twice,
        once by the failed agent loop and once with the handover."""
        runtime, *_ = build_runtime([decide(**UNSURE)], fallback_llm=Unavailable())
        reply = send(runtime, "my battery won't charge")
        self.assertEqual(reply.handled_by, "llm_error")
        history = runtime.conversations.get("conv-1").history
        self.assertEqual([entry["role"] for entry in history], ["user", "assistant"])
        self.assertEqual(sum(1 for e in history if "my battery won't charge" in json.dumps(e)), 1)

    def test_a_record_that_does_not_fit_the_customers_bike_is_not_used(self):
        """R19: routing without the bike let a record written to exclude the
        Doodle reach a Doodle owner."""
        runtime, jev, narrow, fallback = build_runtime(
            [decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-power-on", 0.95)})],
            narrow=[say("Check the on/off switch.")], fallback=[say("Tell me more.")],
        )
        reply = send(runtime, "my battery won't power on", session="sess-rohit")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual(narrow.requests, [])
        [event] = [e for e in runtime.log.events if e["event"] == "jev_decision"]
        self.assertIn("not_applicable:battery-wont-power-on", event["reasons"])


if __name__ == "__main__":
    unittest.main()
