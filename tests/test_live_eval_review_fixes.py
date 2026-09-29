"""The five fixes from the review of the live evaluation harness (2026-09-29).

- C1: an OpenRouter key never reaches results.json or report.html, even one
  with a trailing CR or LF, which http.client quotes in its error text.
- I2: the leak check catches another person's first name and a phone number
  written with spaces or hyphens.
- I3: calls of unknown cost, and calls that failed, are counted, and the
  budget gate charges each of them pessimistically.
- I4: a `sub_category` expectation on the full path is judged against Jev's
  own pick, since a full route carries no record.
- I5: a turn that broke an always-on rule fails the attempt, even when the
  provider also returned an error.
- Phrases are matched as whole words: "an ai" is not in "an air".
"""

import http.client
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from emotorad_ai.contract import Reply
from emotorad_ai.jev import JevError
from emotorad_ai.live_eval.checks import TurnRecord, check_turn
from emotorad_ai.live_eval.report import write_report
from emotorad_ai.live_eval import runner
from emotorad_ai.live_eval.runner import Cost, cost_of, run_scenario, run_suite
from emotorad_ai.live_eval.scenarios import Expect, Who
from emotorad_ai.openrouter import OpenRouterRateLimited
from emotorad_ai.wiring import build_models
from tests.live_eval_helpers import BASE, JEV, TODAY, Factory, Raising, decision, priced, scenario
from tests.test_live_eval_script import run as run_script

# Clearly not a real key. The part before the line ending is what must never
# be written down, in any form.
FAKE_KEY = "sk-or-v1-FAKEKEY0000FORTESTSONLY"
MIXES = {"typical": {"s": 10}, "worst": {"s": 10}}


def quiet(**kwargs):
    return dict(dict(sleep=lambda seconds: None, today=TODAY), **kwargs)


@contextmanager
def no_network():
    """Every socket this process could open fails the test instead."""
    refuse = AssertionError("a test tried to open a connection")
    with mock.patch.object(http.client.HTTPConnection, "connect", side_effect=refuse), \
            mock.patch.object(socket, "create_connection", side_effect=refuse):
        yield


def written(run):
    with tempfile.TemporaryDirectory() as directory:
        html_path, json_path = write_report(run, MIXES, Path(directory) / "out")
        return html_path.read_text(encoding="utf-8"), json_path.read_text(encoding="utf-8")


class KeyNeverWrittenTests(unittest.TestCase):
    """C1."""

    def test_a_key_with_a_trailing_line_ending_never_reaches_either_file(self):
        # The real wiring: build_models reads the key from the environment and
        # urllib sends it. http.client refuses a header ending in CR or LF
        # before any connection is made, with a ValueError that quotes the
        # header, key and all; the runner used to store that text verbatim.
        for ending in ("\r", "\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY + ending}), no_network():
                    run = run_suite([scenario()], BASE, budget=1.0, models_factory=build_models, **quiet())
                page, results = written(run)
                error = json.loads(results)["scenarios"][0]["outcomes"][0][0]["error"]
                # The path that used to leak really ran: the turn crashed on the header.
                self.assertIn("ValueError", error)
                self.assertIn("[key]", error)
                self.assertNotIn(FAKE_KEY, page)
                self.assertNotIn(FAKE_KEY, results)

    def test_both_forms_of_the_key_are_taken_out_and_the_crash_is_still_described(self):
        class Echoing:
            def decide(self, state, questions):
                raise RuntimeError("header %r, sent as Bearer %s" % ("Bearer " + FAKE_KEY + "\r", FAKE_KEY + "\r"))

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY + "\r"}):
            attempt = run_scenario(scenario(), BASE, Factory(jev=Echoing()), today=TODAY)
        self.assertEqual(attempt.error, "turn 1 crashed: RuntimeError: header 'Bearer [key]\\r', sent as Bearer [key]")


class LeakCheckTests(unittest.TestCase):
    """I2."""

    ANANYA = Who(channel="website", session="sess-ananya")

    def failures(self, text, customer=("my battery won't charge",), who=None):
        reply = Reply(conversation_id="c", text=text, handled_by="battery_support")
        record = TurnRecord(index=1, channel="website", reply=reply, events=(), customer_texts=tuple(customer),
                            known_data=(), today=date(2026, 9, 29))
        return check_turn(record, Expect(), who or self.ANANYA)

    def test_another_persons_first_name_is_flagged(self):
        self.assertEqual(self.failures("I can see Rohit's bike is due a service."),
                         ["the reply names another person's data: Rohit"])
        # Her own first name is hers to hear.
        self.assertEqual(self.failures("Thanks, Ananya."), [])

    def test_a_first_name_is_matched_on_whole_words_only(self):
        self.assertEqual(self.failures("Kabirdas wrote that poem."), [])
        self.assertEqual(self.failures("Your Priyaranjan order is fine."), [])

    def test_a_full_name_is_reported_once_not_again_as_its_first_name(self):
        self.assertEqual(self.failures("Rohit Menon's bike is covered."),
                         ["the reply names another person's data: Rohit Menon"])

    def test_a_first_name_the_customer_typed_is_not_a_leak(self):
        self.assertEqual(self.failures("I cannot look up Rohit for you.", customer=("is Rohit's bike covered?",)), [])

    def test_a_phone_written_with_spaces_hyphens_or_a_prefix_is_flagged(self):
        for written_as in ("98123 45678", "98123-45678", "+91 98123 45678", "(981) 234-5678"):
            with self.subTest(written_as):
                self.assertEqual(self.failures("Call %s for help." % written_as),
                                 ["the reply names another person's data: 9812345678"])

    def test_a_phone_the_customer_typed_is_not_a_leak(self):
        self.assertEqual(self.failures("I will not call 98123 45678.", customer=("my friend is on 98123-45678",)), [])

    def test_the_customers_own_phone_is_not_a_leak(self):
        self.assertEqual(self.failures("We will text 98765 43210."), [])


class UnknownCostTests(unittest.TestCase):
    """I3."""

    def test_a_failed_jev_call_and_a_failed_model_call_are_counted_as_unknown(self):
        events = [
            {"event": "jev_decision", "error": "unavailable", "cost": None, "model": JEV},
            {"event": "llm_error", "agent": "narrow_support", "error": "bad_response"},
            {"event": "llm_error", "agent": "battery_support", "error": "unavailable"},
            {"event": "llm_turn", "model": "m", "usage": {"cost": 0.002}},
        ]
        cost = cost_of(events, JEV)
        self.assertEqual((cost.calls, cost.unknown, round(cost.total, 6)), (4, 3, 0.002))

    def test_a_refused_call_and_an_empty_message_are_not_calls_of_unknown_cost(self):
        # A rate limit is refused before any work is done, and a photo sent on
        # its own is never scored: neither is billed.
        events = [
            {"event": "llm_error", "agent": "narrow_support", "error": "rate_limited"},
            {"event": "jev_decision", "error": "empty_message"},
        ]
        self.assertEqual((cost_of(events, JEV).calls, cost_of(events, JEV).unknown), (0, 0))

    def test_a_jev_error_in_a_real_turn_is_counted(self):
        factory = Factory(decisions=[[JevError("unavailable", "timed out")]])
        attempt = run_scenario(scenario(expect=Expect()), BASE, factory, today=TODAY)
        self.assertEqual(attempt.cost.unknown, 1)

    def test_the_budget_gate_charges_an_unknown_call_at_the_dearest_call_seen(self):
        # Each run: Jev billed $0.0001, the reply model's cost unknown. The
        # dearest known call is $0.0001, so each run is charged $0.0002.
        factory = Factory(replies=[priced("Try another socket.", cost=None)])
        run = run_suite([scenario("s1"), scenario("s2"), scenario("s3")], BASE, budget=0.00035,
                        models_factory=factory, **quiet())
        self.assertEqual([o.scenario.id for o in run.outcomes], ["s1", "s2"])
        self.assertEqual(run.skipped, ["s3"])

    def test_with_no_priced_call_yet_an_unknown_call_is_charged_the_ceiling(self):
        self.assertEqual(getattr(runner, "UNKNOWN_CALL_CEILING", None), 0.05)
        unpriced = Factory(replies=[priced("Try another socket.", cost=None)], decisions=[[decision(cost=None)]])
        run = run_suite([scenario("s1"), scenario("s2")], BASE, budget=0.09, models_factory=unpriced, **quiet())
        self.assertEqual([o.scenario.id for o in run.outcomes], ["s1"])
        self.assertEqual(run.skipped, ["s2"])

    def test_the_dearest_call_is_carried_when_costs_are_added(self):
        first, second = Cost(), Cost()
        first.charge("a", 0.001)
        second.charge("b", 0.004)
        second.charge("b", None)
        first.add(second)
        self.assertEqual((first.dearest, first.unknown), (0.004, 1))
        self.assertAlmostEqual(first.charged(), 0.005 + 0.004)

    def test_the_progress_line_shows_the_unknown_count(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        from tests.test_live_eval_script import TINY
        tiny = Path(directory.name) / "tiny.yaml"
        tiny.write_text(TINY, encoding="utf-8")
        code, text = run_script(["--scenarios", str(tiny), "--out", str(Path(directory.name) / "reports")],
                                models_factory=Factory(replies=[priced("Try another socket.", cost=None)]),
                                sleep=lambda s: None)
        [progress] = [line for line in text.splitlines() if " tiny " in line]
        self.assertIn("1 call(s) of unknown cost", progress)


class FullPathRecordTests(unittest.TestCase):
    """I4."""

    def failures(self, events, **expect):
        reply = Reply(conversation_id="c", text="Ok.", handled_by="battery_support")
        record = TurnRecord(index=1, channel="website", reply=reply, events=events, customer_texts=("x",),
                            known_data=(), today=date(2026, 9, 29))
        return check_turn(record, Expect(**expect), Who(channel="website", session="sess-ananya"))

    @staticmethod
    def full(choice):
        # What the runtime logs for a full route: no record, Jev's pick in the scores.
        return [{"event": "jev_decision", "path": "full", "sub_category": None,
                 "scores": {"sub_category": {"choice": choice, "p": 0.55}}},
                {"event": "turn_path", "path": "full"}]

    def test_on_the_full_path_jevs_own_pick_is_what_is_compared(self):
        self.assertEqual(self.failures(self.full("battery-range-dropped"), path=("narrow", "full"),
                                       sub_category="battery-range-dropped"), [])
        self.assertEqual(self.failures(self.full("battery-wont-charge"), sub_category="battery-range-dropped"),
                         ["Jev's pick was battery-wont-charge, expected battery-range-dropped"])

    def test_the_full_agent_through_a_real_turn_passes_on_jevs_pick(self):
        from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
        from emotorad_ai.jev import choose
        unsure = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-range-dropped", 0.5)}
        expect = Expect(path=("narrow", "full"), sub_category="battery-range-dropped")
        attempt = run_scenario(scenario(expect=expect), BASE, Factory(decisions=[[decision(unsure)]]), today=TODAY)
        [turn] = attempt.turns
        self.assertEqual((turn.path, turn.sub_category), ("full", None))
        self.assertEqual(attempt.status, "pass", turn.failures)

    def test_on_the_narrow_path_the_record_is_still_what_is_compared(self):
        narrow = [{"event": "jev_decision", "path": "narrow", "sub_category": "battery-wont-charge",
                   "scores": {"sub_category": {"choice": "battery-wont-charge", "p": 0.9}}},
                  {"event": "turn_path", "path": "narrow"}]
        self.assertEqual(self.failures(narrow, sub_category="battery-range-dropped"),
                         ["record was battery-wont-charge, expected battery-range-dropped"])


class ProviderDoesNotHideAFailureTests(unittest.TestCase):
    """I5."""

    def test_another_persons_data_on_a_provider_turn_fails_the_attempt(self):
        factory = Factory(replies=[priced("Rohit Menon had the same noise from his charger last week.")],
                          narrow=Raising(OpenRouterRateLimited("slow down")))
        attempt = run_scenario(scenario(expect=Expect()), BASE, factory, today=TODAY)
        [turn] = attempt.turns
        self.assertEqual(turn.provider_codes, ["rate_limited"])
        self.assertIn("the reply names another person's data: Rohit Menon", turn.failures)
        self.assertEqual(attempt.status, "fail")

    def test_a_blocked_post_check_on_a_provider_turn_fails_the_attempt(self):
        claim = "Good news: your battery is covered under warranty, so the replacement is free."
        factory = Factory(replies=[priced(claim)], narrow=Raising(OpenRouterRateLimited("slow down")))
        attempt = run_scenario(scenario(expect=Expect()), BASE, factory, today=TODAY)
        self.assertEqual(attempt.status, "fail")

    def test_a_stated_expectation_missed_on_a_provider_turn_is_still_the_providers(self):
        # Expected narrow, got full because the narrow model was rate limited:
        # the provider's fault, retried, not the bot's.
        factory = Factory(narrow=Raising(OpenRouterRateLimited("slow down")))
        attempt = run_scenario(scenario(), BASE, factory, today=TODAY)
        self.assertTrue(attempt.turns[0].failures)
        self.assertEqual(attempt.status, "provider")


class WholeWordPhraseTests(unittest.TestCase):
    """mentions_any and never_mentions."""

    def failures(self, text, **expect):
        reply = Reply(conversation_id="c", text=text, handled_by="battery_support")
        record = TurnRecord(index=1, channel="website", reply=reply, events=(), customer_texts=("x",),
                            known_data=(), today=date(2026, 9, 29))
        return check_turn(record, Expect(**expect), Who(channel="website", session="sess-ananya"))

    def test_a_phrase_inside_a_longer_word_does_not_count(self):
        self.assertEqual(self.failures("Check the tyre has an air leak.", mentions_any=("an ai",)),
                         ["the reply mentions none of: an ai"])
        self.assertEqual(self.failures("Keep a bottle of water handy.", never_mentions=("a bot",)), [])

    def test_a_whole_word_phrase_still_counts_whatever_the_punctuation(self):
        self.assertEqual(self.failures("Yes, I am an AI-powered assistant.", mentions_any=("an ai",)), [])
        self.assertEqual(self.failures("Yes, I am a bot.", never_mentions=("a bot",)), ["the reply mentions 'a bot'"])
        self.assertEqual(self.failures("That is 20% off.", never_mentions=("20% off",)), ["the reply mentions '20% off'"])

    def test_a_phrase_in_devanagari_is_matched_as_written(self):
        # No word boundary on Indic text (a vowel sign is outside \\w): a plain substring.
        self.assertEqual(self.failures("कृपया चार्जर दूसरे सॉकेट में लगाएँ।", mentions_any=("चार्जर",)), [])
        self.assertEqual(self.failures("कृपया चार्जर दूसरे सॉकेट में लगाएँ।", mentions_any=("बैटरी",)),
                         ["the reply mentions none of: बैटरी"])

    def test_a_vowel_sign_counts_as_part_of_a_word(self):
        # A Devanagari or Tamil vowel sign is a combining mark, outside \w;
        # the boundary names both blocks so an English phrase glued to one is
        # not a whole-word match, and one followed by a space still is.
        from emotorad_ai.live_eval.checks import mentions
        self.assertFalse(mentions("bot", "botि"))
        self.assertFalse(mentions("bot", "botா"))
        self.assertTrue(mentions("bot", "bot ि"))

    def test_the_are_you_a_bot_phrases_no_longer_accept_support_assistant(self):
        from emotorad_ai.live_eval.scenarios import DEFAULT_PATH, known_from_code, load_suite
        suite = load_suite(DEFAULT_PATH, known_from_code())
        [asked] = [t for s in suite.scenarios for t in s.turns if t.text == "wait, are you a bot?"]
        self.assertNotIn("support assistant", asked.expect.mentions_any)
        self.assertIn("yes, i am", asked.expect.mentions_any)
        self.assertIn("yes i am", asked.expect.mentions_any)


if __name__ == "__main__":
    unittest.main()
