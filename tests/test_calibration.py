import unittest

from emotorad_ai.calibration import evaluate_choice, evaluate_noul, suggest_choice, suggest_noul
from emotorad_ai.decisions import NONE, NONE_OF_THESE, Q_CATEGORY
from emotorad_ai.jev import JevDecision, choose, yes
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import load_standard_responses
from tests.jev_golden import GoldenCase, load_golden


class GoldenSetTests(unittest.TestCase):
    def test_every_label_points_at_something_that_exists(self):
        records = {r.id for r in KnowledgeBase().records}
        standard = {s.id for s in load_standard_responses()}
        cases = load_golden()
        self.assertGreaterEqual(len(cases), 61 + 10)
        for case in cases:
            with self.subTest(case.text):
                self.assertIn(case.category, (None, "battery", "motor", NONE_OF_THESE))
                self.assertIn(case.sub_category, records | {NONE, None})
                self.assertIn(case.standard_response, standard | {NONE, None})

    def test_english_hinglish_and_hindi_are_all_represented(self):
        languages = {case.language for case in load_golden()}
        self.assertTrue({"english", "hinglish", "hindi"} <= languages)


def case(language, category):
    return GoldenCase(text="x", language=language, category=category)


class CalibrationMathTests(unittest.TestCase):
    PAIRS = [
        (case("english", "battery"), JevDecision(answers={Q_CATEGORY: choose("battery", 0.95)})),
        (case("english", "battery"), JevDecision(answers={Q_CATEGORY: choose("motor", 0.70)})),
        (case("hinglish", "battery"), JevDecision(answers={Q_CATEGORY: choose("battery", 0.88)})),
        (case("hinglish", "motor"), JevDecision(answers={Q_CATEGORY: choose("battery", 0.60)})),
    ]

    def test_choice_accuracy_and_coverage_per_language_never_averaged(self):
        rows = evaluate_choice(self.PAIRS, Q_CATEGORY, lambda c: c.category, [0.5, 0.8, 0.9])
        self.assertEqual(rows[0.5]["english"], {"n": 2, "covered": 2, "correct": 1})
        self.assertEqual(rows[0.8]["english"], {"n": 2, "covered": 1, "correct": 1})
        self.assertEqual(rows[0.8]["hinglish"], {"n": 2, "covered": 1, "correct": 1})
        self.assertEqual(rows[0.9]["hinglish"], {"n": 2, "covered": 0, "correct": 0})

    def test_the_suggestion_is_the_lowest_threshold_every_language_meets(self):
        rows = evaluate_choice(self.PAIRS, Q_CATEGORY, lambda c: c.category, [0.5, 0.8, 0.9])
        self.assertEqual(suggest_choice(rows, target=1.0), 0.8)
        self.assertIsNone(suggest_choice({0.5: rows[0.5]}, target=1.0))

    def test_noul_recall_drives_the_prefetch_suggestion(self):
        pairs = [
            (GoldenCase("x", "english", needs_warranty_lookup=True), JevDecision(answers={"w": yes(0.9)})),
            (GoldenCase("x", "english", needs_warranty_lookup=True), JevDecision(answers={"w": yes(0.65)})),
            (GoldenCase("x", "english", needs_warranty_lookup=False), JevDecision(answers={"w": yes(0.7)})),
        ]
        rows = evaluate_noul(pairs, "w", lambda c: c.needs_warranty_lookup, [0.6, 0.8])
        self.assertEqual(rows[0.6]["english"], {"tp": 2, "fp": 1, "fn": 0, "tn": 0})
        self.assertEqual(rows[0.8]["english"], {"tp": 1, "fp": 0, "fn": 1, "tn": 1})
        self.assertEqual(suggest_noul(rows, target_recall=1.0), 0.6)


if __name__ == "__main__":
    unittest.main()
