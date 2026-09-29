import unittest
from dataclasses import replace

from emotorad_ai.live_eval.runner import Summary, bikes_for, project, run_scenario, run_suite, scenario_registry, summarise
from emotorad_ai.live_eval.scenarios import Expect, Scenario, Turn, Who
from emotorad_ai.openrouter import OpenRouterAuthError, OpenRouterPaymentRequired, OpenRouterRateLimited
from tests.live_eval_helpers import BASE, JEV, TODAY, UNSURE, CrashingJev, Factory, Raising, decision, priced, scenario


def quiet(**kwargs):
    return dict(dict(sleep=lambda seconds: None, today=TODAY), **kwargs)


class RegistryTests(unittest.TestCase):
    def test_the_whole_tool_set_is_wired_with_the_persons_bikes(self):
        registry = scenario_registry(scenario(), TODAY)
        self.assertIn("lookup_error_code", registry.specs)
        self.assertIn("send_guide_media", registry.specs)
        self.assertEqual([b["frame_number"] for b in bikes_for(scenario(), TODAY)], ["EMXP2025004417"])

    def test_someone_unverified_owns_no_bikes(self):
        caller = Scenario(id="v", family="channels", who=Who(channel="voice", phone="+919876543210"), turns=(Turn(text="hi"),))
        self.assertEqual(bikes_for(caller, TODAY), [])


class RunScenarioTests(unittest.TestCase):
    def test_a_turn_is_run_checked_and_costed(self):
        attempt = run_scenario(scenario(), BASE, Factory(), today=TODAY)
        [turn] = attempt.turns
        self.assertEqual(attempt.status, "pass", turn.failures)
        self.assertEqual((turn.path, turn.handled_by, turn.sub_category), ("narrow", "narrow_support", "battery-wont-charge"))
        self.assertAlmostEqual(attempt.cost.total, 0.0021)
        self.assertEqual({m: round(a, 6) for m, a in attempt.cost.by_model.items()}, {BASE.narrow_model: 0.002, JEV: 0.0001})

    def test_a_reply_a_post_check_blocked_is_kept_for_the_reader(self):
        claim = "Good news: your battery is covered under warranty, so the replacement is free."
        attempt = run_scenario(scenario(expect=Expect()), BASE, Factory(replies=[priced(claim)]), today=TODAY)
        [turn] = attempt.turns
        self.assertEqual(turn.handled_by, "guardrail:coverage_post_check")
        self.assertEqual((turn.blocked_reason, turn.suppressed), ("coverage_claim_without_tool_result", claim))

    def test_every_scenario_runs_on_openrouter_in_memory_with_zero_retention(self):
        factory = Factory()
        base = replace(BASE, mode="offline", store="mongodb", openrouter_zdr=False)
        run_scenario(scenario(settings={"narrow_model": "x/y"}), base, factory, today=TODAY)
        [got] = factory.settings
        self.assertEqual((got.mode, got.store, got.openrouter_zdr, got.narrow_model), ("openrouter", "memory", True, "x/y"))

    def test_a_call_without_a_billed_cost_is_counted_as_unknown_not_free(self):
        attempt = run_scenario(scenario(), BASE, Factory(replies=[priced("Try another socket.", cost=None)]), today=TODAY)
        self.assertEqual((attempt.cost.unknown, round(attempt.cost.total, 6)), (1, 0.0001))

    def test_a_crash_is_a_failure_with_the_error(self):
        # A model error is no longer a crash (Agent.run turns it into an
        # apology and an escalation); a bug elsewhere in the turn still is.
        attempt = run_scenario(scenario(), BASE, Factory(jev=CrashingJev()), today=TODAY)
        self.assertEqual((attempt.status, attempt.error), ("fail", "turn 1 crashed: RuntimeError: boom"))

    def test_a_forced_failure_scenario_is_never_marked_provider(self):
        failing = scenario(family="failures", expect=Expect(handled_by=("battery_support",)))
        attempt = run_scenario(failing, BASE, Factory(narrow=Raising(OpenRouterRateLimited("slow down"))), today=TODAY)
        self.assertEqual(attempt.status, "pass", attempt.turns[0].failures)


class RunSuiteTests(unittest.TestCase):
    def test_a_provider_error_is_retried_once_after_a_pause_then_reported_as_provider(self):
        sleeps = []
        run = run_suite([scenario()], BASE, budget=1.0, models_factory=Factory(narrow=Raising(OpenRouterRateLimited("slow down"))),
                        pause=7, sleep=sleeps.append, today=TODAY)
        [outcome] = run.outcomes
        self.assertEqual([a.status for a in outcome.attempts], ["provider", "provider"])
        self.assertEqual(sleeps, [7])
        self.assertEqual(outcome.final.turns[0].provider_codes, ["rate_limited"])

    def test_a_rejected_key_or_no_credit_stops_the_whole_run(self):
        for error, reason in ((OpenRouterAuthError("no"), "OpenRouter rejected the key"),
                              (OpenRouterPaymentRequired("pay"), "the OpenRouter account is out of credit")):
            with self.subTest(reason):
                factory = Factory(narrow=Raising(error))
                run = run_suite([scenario("a"), scenario("b")], BASE, budget=1.0, models_factory=factory, **quiet())
                self.assertEqual(run.aborted, reason)
                self.assertEqual((run.outcomes, len(factory.settings)), ([], 1))

    def test_the_budget_stops_before_the_next_scenario_and_lists_what_was_skipped(self):
        run = run_suite([scenario("s1"), scenario("s2"), scenario("s3")], BASE, budget=0.003, models_factory=Factory(), **quiet())
        self.assertEqual([o.scenario.id for o in run.outcomes], ["s1", "s2"])
        self.assertEqual(run.skipped, ["s3"])

    def test_the_budget_running_out_inside_repeats_lists_the_scenario_once(self):
        run = run_suite([scenario("s1"), scenario("s2")], BASE, budget=0.005, repeat=2, models_factory=Factory(), **quiet())
        self.assertEqual([o.scenario.id for o in run.outcomes], ["s1", "s1", "s2"])
        self.assertEqual(run.skipped, ["s2"])

    def test_repeats_that_disagree_are_flaky(self):
        factory = Factory(decisions=[[decision()], [decision(UNSURE)]])
        run = run_suite([scenario()], BASE, budget=1.0, repeat=2, models_factory=factory, **quiet())
        [summary] = summarise(run)
        self.assertEqual((summary.status, summary.passes, summary.runs), ("flaky", 1, 2))

    def test_progress_is_told_about_every_outcome(self):
        seen = []
        run_suite([scenario("s1"), scenario("s2")], BASE, budget=1.0, models_factory=Factory(), progress=seen.append, **quiet())
        self.assertEqual([o.scenario.id for o in seen], ["s1", "s2"])


class ProjectionTests(unittest.TestCase):
    def test_mixes_are_summed_and_anything_unmeasured_is_named_not_zeroed(self):
        summaries = [Summary(scenario("a"), runs=1, passes=1, status="pass", cost=0.01),
                     Summary(scenario("b"), runs=1, passes=0, status="provider", cost=None)]
        got = project(summaries, {"typical": {"a": 7, "b": 3}, "worst": {"a": 10}})
        self.assertEqual(got["typical"], {"ten": None, "per_1000": None, "missing": ["b"]})
        self.assertEqual(got["worst"], {"ten": 0.1, "per_1000": 10.0, "missing": []})

    def test_a_scenario_with_an_unknown_cost_has_no_conversation_cost(self):
        run = run_suite([scenario()], BASE, budget=1.0, models_factory=Factory(replies=[priced("Try another socket.", cost=None)]), **quiet())
        [summary] = summarise(run)
        self.assertIsNone(summary.cost)


if __name__ == "__main__":
    unittest.main()
