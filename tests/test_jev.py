import json
import unittest
from pathlib import Path

from emotorad_ai.jev import (
    ChoiceAnswer,
    JevClient,
    JevDecision,
    JevError,
    NoulAnswer,
    Question,
    ScriptedJev,
    choice,
    choose,
    noul,
    parse_decision,
    yes,
)
from emotorad_ai.openrouter import (
    DECISIONS_PATH,
    OpenRouterAuthError,
    OpenRouterBadResponse,
    OpenRouterPaymentRequired,
    OpenRouterRateLimited,
    OpenRouterUnavailable,
)
from tests.fake_http import FakeTransport

DOCUMENTED = Path(__file__).parent / "data" / "jev_decisions_documented.json"
LIVE_SHAPE = Path(__file__).resolve().parents[1] / "docs" / "api-shapes" / "jev-decisions.json"

QUESTIONS = {
    "category": choice("Which area is this?", {"battery": "Battery", "motor": "Motor", "none_of_these": "Other"}),
    "needs_warranty_lookup": noul("Asks about warranty", true="Asks about cover", false="Does not"),
}
GOOD = {
    "model": "typesafe/jev-1.13-20260915",
    "answers": {
        "category": {"type": "choice", "choice": "battery",
                     "probabilities": {"battery": 0.9, "motor": 0.07, "none_of_these": 0.03}, "confidence": 0.8},
        "needs_warranty_lookup": {"type": "noul", "noul": 0.2},
    },
    "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0000126},
}


def questions_from_fixture(raw):
    return {qid: Question(q["type"], q["instructions"], q.get("criteria")) for qid, q in raw.items()}


class QuestionTests(unittest.TestCase):
    def test_choice_and_noul_serialise_to_the_decisions_api_shape(self):
        self.assertEqual(QUESTIONS["category"].to_dict()["type"], "choice")
        self.assertEqual(QUESTIONS["category"].to_dict()["criteria"]["battery"], "Battery")
        self.assertEqual(
            QUESTIONS["needs_warranty_lookup"].to_dict(),
            {"type": "noul", "instructions": "Asks about warranty", "criteria": {"true": "Asks about cover", "false": "Does not"}},
        )
        self.assertNotIn("criteria", noul("Plain").to_dict())

    def test_a_choice_needs_two_to_255_options(self):
        with self.assertRaises(ValueError):
            choice("x", {"only": "one"})
        with self.assertRaises(ValueError):
            choice("x", {str(i): "o" for i in range(256)})


class ParseTests(unittest.TestCase):
    def test_parses_choice_and_noul_answers(self):
        decision = parse_decision(GOOD, QUESTIONS)
        self.assertEqual(decision.answers["category"].choice, "battery")
        self.assertAlmostEqual(decision.answers["category"].p, 0.9)
        self.assertAlmostEqual(decision.answers["needs_warranty_lookup"].p, 0.2)
        self.assertEqual(decision.model, "typesafe/jev-1.13-20260915")
        self.assertAlmostEqual(decision.cost, 0.0000126)

    def test_the_documented_shape_parses(self):
        raw = json.loads(DOCUMENTED.read_text(encoding="utf-8"))
        decision = parse_decision(raw["response"], questions_from_fixture(raw["questions"]))
        self.assertEqual(decision.answers["team"].choice, "billing")
        self.assertAlmostEqual(decision.answers["refund_requested"].p, 0.97)

    def test_the_live_shape_parses_once_the_probe_has_saved_it(self):
        if not LIVE_SHAPE.exists():
            self.skipTest("run scripts/jev_probe.py once to capture the live Decisions API shape")
        raw = json.loads(LIVE_SHAPE.read_text(encoding="utf-8"))
        decision = parse_decision(raw["response"], questions_from_fixture(raw["questions"]))
        self.assertEqual(set(decision.answers), set(raw["questions"]))

    def test_malformed_answers_are_refused(self):
        def broken(mutate):
            payload = json.loads(json.dumps(GOOD))
            mutate(payload)
            return payload

        cases = {
            "no answers object": broken(lambda p: p.pop("answers")),
            "missing answer": broken(lambda p: p["answers"].pop("category")),
            "wrong type": broken(lambda p: p["answers"]["category"].update(type="noul")),
            "choice not in criteria": broken(lambda p: p["answers"]["category"].update(choice="brakes")),
            "probability key not in criteria": broken(lambda p: p["answers"]["category"]["probabilities"].update(brakes=0.1)),
            "probability out of range": broken(lambda p: p["answers"]["category"]["probabilities"].update(battery=1.4)),
            "noul out of range": broken(lambda p: p["answers"]["needs_warranty_lookup"].update(noul=-0.1)),
            "noul not a number": broken(lambda p: p["answers"]["needs_warranty_lookup"].update(noul="high")),
            "noul is a bool": broken(lambda p: p["answers"]["needs_warranty_lookup"].update(noul=True)),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(JevError) as caught:
                parse_decision(payload, QUESTIONS)
            self.assertEqual(caught.exception.code, "bad_response")


class ClientTests(unittest.TestCase):
    def test_decide_posts_state_and_questions_with_the_jev_timeout(self):
        transport = FakeTransport([GOOD])
        ticks = iter([10.0, 10.25])
        client = JevClient(transport, model="typesafe/jev-1.13", timeout=2.0, clock=lambda: next(ticks))
        decision = client.decide({"message": "battery not charging"}, QUESTIONS)
        call = transport.calls[0]
        self.assertEqual(call["path"], DECISIONS_PATH)
        self.assertEqual(call["timeout"], 2.0)
        self.assertEqual(call["body"]["model"], "typesafe/jev-1.13")
        self.assertEqual(call["body"]["state"], {"message": "battery not charging"})
        self.assertEqual(set(call["body"]["questions"]), {"category", "needs_warranty_lookup"})
        self.assertEqual(decision.latency_ms, 250)

    def test_transport_failures_become_jev_errors_with_stable_codes(self):
        cases = [
            (OpenRouterAuthError("x"), "auth"), (OpenRouterRateLimited("x"), "rate_limited"),
            (OpenRouterUnavailable("x"), "unavailable"), (OpenRouterBadResponse("x"), "bad_response"),
            (OpenRouterPaymentRequired("x"), "payment_required"),
        ]
        for error, code in cases:
            with self.subTest(code), self.assertRaises(JevError) as caught:
                JevClient(FakeTransport([error])).decide({}, QUESTIONS)
            self.assertEqual(caught.exception.code, code)


class ScriptedJevTests(unittest.TestCase):
    def test_replays_decisions_and_errors_and_records_calls(self):
        jev = ScriptedJev([JevDecision(answers={"category": choose("battery", 0.9)}), JevError("unavailable", "down")])
        self.assertEqual(jev.decide({"m": 1}, QUESTIONS).answers["category"].choice, "battery")
        with self.assertRaises(JevError):
            jev.decide({"m": 2}, QUESTIONS)
        self.assertEqual([c["state"] for c in jev.calls], [{"m": 1}, {"m": 2}])

    def test_helpers_build_answers(self):
        self.assertEqual(choose("battery", 0.9), ChoiceAnswer("battery", {"battery": 0.9}, 0.9))
        self.assertEqual(yes(0.7), NoulAnswer(0.7))

    def test_scores_are_compact_and_carry_no_text(self):
        decision = JevDecision(answers={"category": choose("battery", 0.912345), "w": yes(0.1)})
        self.assertEqual(decision.scores(), {"category": {"choice": "battery", "p": 0.9123}, "w": {"p": 0.1}})


if __name__ == "__main__":
    unittest.main()
