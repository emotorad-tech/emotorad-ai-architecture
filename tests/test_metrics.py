"""Quality metrics — the numbers that would have caught Klarna.

Every test here exists because the obvious metric lies in a specific way.
"""

import unittest

from emotorad_ai.metrics import (
    ConversationSummary,
    build_report,
    detect_language,
    render,
    summarise,
)


def conversation(cid, *, text="battery won't charge", handled_by="battery_support",
                 escalated=False, channel="whatsapp", cluster="clu-1",
                 start="2026-08-06T10:00:00+00:00", end="2026-08-06T10:05:00+00:00",
                 guardrail=None, tokens=(500, 100)):
    events = [
        {"event": "inbound", "conversation_id": cid, "channel": channel, "text": text,
         "timestamp": start},
        {"event": "identity_resolved", "conversation_id": cid, "cluster_id": cluster,
         "timestamp": start},
        {"event": "llm_turn", "conversation_id": cid,
         "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}, "timestamp": start},
    ]
    if guardrail:
        events.append({"event": "guardrail_triggered", "conversation_id": cid,
                       "guardrail": guardrail, "timestamp": start})
    events.append({"event": "outcome", "conversation_id": cid, "handled_by": handled_by,
                   "escalated": escalated, "timestamp": end})
    return events


class ResolutionDefinitionTests(unittest.TestCase):
    def test_an_agent_answer_counts_as_resolved(self):
        [summary] = summarise(conversation("c1"))
        self.assertTrue(summary.resolved)

    def test_a_guardrail_stop_is_correct_but_not_resolved(self):
        # A safety escalation is the right outcome and not a solved problem.
        # Counting it as one is how deflection numbers start lying.
        [summary] = summarise(
            conversation("c1", handled_by="guardrail:battery_safety", escalated=True)
        )
        self.assertFalse(summary.resolved)

    def test_a_triage_question_is_not_a_resolution(self):
        [summary] = summarise(conversation("c1", handled_by="triage"))
        self.assertFalse(summary.resolved)

    def test_a_coverage_block_is_not_a_resolution(self):
        [summary] = summarise(
            conversation("c1", handled_by="guardrail:coverage_post_check", escalated=True)
        )
        self.assertFalse(summary.resolved)


class EscalationHealthTests(unittest.TestCase):
    def test_zero_escalation_is_flagged_as_suspicious_not_excellent(self):
        # The Klarna shape exactly: a bot that will not hand over looks perfect
        # on a deflection dashboard and is worse than the humans it replaced.
        events = [e for i in range(20) for e in conversation("c%d" % i, cluster="clu-%d" % i)]
        report = build_report(events)
        self.assertEqual(report.escalation_rate, 0.0)
        self.assertEqual(report.escalation_health, "suspiciously_low")

    def test_a_moderate_escalation_rate_is_healthy(self):
        events = []
        for i in range(20):
            events += conversation("c%d" % i, cluster="clu-%d" % i,
                                   escalated=i < 4, handled_by="battery_support" if i >= 4 else "guardrail:human_handoff")
        report = build_report(events)
        self.assertEqual(report.escalation_health, "healthy")

    def test_a_very_high_escalation_rate_is_flagged(self):
        events = []
        for i in range(10):
            events += conversation("c%d" % i, cluster="clu-%d" % i, escalated=i < 8,
                                   handled_by="guardrail:human_handoff" if i < 8 else "battery_support")
        self.assertEqual(build_report(events).escalation_health, "too_high")


class RepeatContactTests(unittest.TestCase):
    def test_a_resolved_conversation_that_comes_back_is_counted(self):
        # The honest counter-metric. Deflection says this was a success.
        events = (
            conversation("c1", cluster="clu-1", start="2026-08-06T10:00:00+00:00",
                         end="2026-08-06T10:05:00+00:00")
            + conversation("c2", cluster="clu-1", start="2026-08-07T09:00:00+00:00",
                           end="2026-08-07T09:05:00+00:00")
        )
        report = build_report(events)
        self.assertEqual(report.repeat_contacts, 1)
        self.assertEqual(report.repeat_contact_rate, 0.5)

    def test_a_return_after_the_window_is_not_a_repeat(self):
        events = (
            conversation("c1", cluster="clu-1", start="2026-08-01T10:00:00+00:00",
                         end="2026-08-01T10:05:00+00:00")
            + conversation("c2", cluster="clu-1", start="2026-08-06T10:00:00+00:00",
                           end="2026-08-06T10:05:00+00:00")
        )
        self.assertEqual(build_report(events).repeat_contacts, 0)

    def test_different_people_are_not_repeat_contacts(self):
        events = conversation("c1", cluster="clu-1") + conversation(
            "c2", cluster="clu-2", start="2026-08-06T11:00:00+00:00",
            end="2026-08-06T11:05:00+00:00")
        self.assertEqual(build_report(events).repeat_contacts, 0)

    def test_a_return_after_an_escalation_is_not_counted_against_the_bot(self):
        # It never claimed to have resolved that one.
        events = (
            conversation("c1", cluster="clu-1", escalated=True,
                         handled_by="guardrail:human_handoff")
            + conversation("c2", cluster="clu-1", start="2026-08-06T14:00:00+00:00",
                           end="2026-08-06T14:05:00+00:00")
        )
        self.assertEqual(build_report(events).repeat_contacts, 0)


class LanguageReportingTests(unittest.TestCase):
    def test_devanagari_and_hinglish_are_detected(self):
        self.assertEqual(detect_language("बैटरी चार्ज नहीं हो रही"), "hi-deva")
        self.assertEqual(detect_language("battery charge nahi ho rahi hai"), "hinglish")
        self.assertEqual(detect_language("my battery will not charge"), "en")

    def test_quality_is_never_reported_as_a_single_average(self):
        # An aggregate can hide 95% English and 40% Hindi, and the aggregate is
        # what ends up on a slide.
        events = []
        for i in range(8):
            events += conversation("en%d" % i, cluster="c-en%d" % i, text="battery not charging")
        for i in range(4):
            events += conversation("hi%d" % i, cluster="c-hi%d" % i,
                                   text="बैटरी चार्ज नहीं हो रही", handled_by="triage")
        report = build_report(events)

        self.assertIn("en", report.by_language)
        self.assertIn("hi-deva", report.by_language)
        self.assertEqual(report.by_language["en"]["deflection_rate"], 1.0)
        self.assertEqual(report.by_language["hi-deva"]["deflection_rate"], 0.0)
        # The aggregate would have read 67% and hidden a total failure.
        self.assertAlmostEqual(report.deflection_rate, 8 / 12)

    def test_a_conversation_with_any_hindi_is_reported_as_hindi(self):
        events = [
            {"event": "inbound", "conversation_id": "c1", "text": "hello", "channel": "whatsapp",
             "timestamp": "2026-08-06T10:00:00+00:00"},
            {"event": "inbound", "conversation_id": "c1", "text": "बैटरी चार्ज नहीं",
             "channel": "whatsapp", "timestamp": "2026-08-06T10:01:00+00:00"},
            {"event": "outcome", "conversation_id": "c1", "handled_by": "battery_support",
             "escalated": False, "timestamp": "2026-08-06T10:02:00+00:00"},
        ]
        self.assertIn("hi-deva", build_report(events).by_language)


class CostTests(unittest.TestCase):
    def test_cost_is_measured_per_resolved_conversation(self):
        # Ten cheap turns that fix nothing are not cheaper than one that works.
        events = conversation("c1", tokens=(1000, 200)) + conversation(
            "c2", cluster="clu-2", handled_by="triage", tokens=(1000, 200))
        report = build_report(events)
        self.assertEqual(report.total_tokens, 2400)
        self.assertEqual(report.tokens_per_resolved(), 2400.0)

    def test_no_resolutions_gives_infinite_cost_rather_than_a_flattering_zero(self):
        report = build_report(conversation("c1", handled_by="triage"))
        self.assertEqual(report.tokens_per_resolved(), float("inf"))


class EdgeCaseCountingTests(unittest.TestCase):
    def test_edge_case_frequencies_are_reported_as_rates(self):
        # This is what promotes a case out of CAPTURE in the edge case register:
        # 8% of conversations is a roadmap item, 0.01% was correctly deferred.
        events = [e for i in range(50) for e in conversation("c%d" % i, cluster="clu-%d" % i)]
        report = build_report(events, edge_case_signals={"purchase_date_missing": 4})
        output = render(report)
        self.assertIn("purchase_date_missing", output)
        self.assertIn("8.0%", output)


class RenderTests(unittest.TestCase):
    def test_the_scorecard_puts_the_honest_numbers_first(self):
        events = [e for i in range(10) for e in conversation("c%d" % i, cluster="clu-%d" % i)]
        output = render(build_report(events))
        self.assertLess(output.index("escalation rate"), output.index("deflection"))
        self.assertIn("reported, not a target", output)

    def test_an_empty_log_does_not_divide_by_zero(self):
        report = build_report([])
        self.assertEqual(report.deflection_rate, 0.0)
        self.assertEqual(report.escalation_rate, 0.0)
        self.assertEqual(report.repeat_contact_rate, 0.0)
        self.assertIsInstance(render(report), str)


if __name__ == "__main__":
    unittest.main()
