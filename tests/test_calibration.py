import unittest
from collections import Counter

import yaml

from emotorad_ai.calibration import evaluate_choice, evaluate_noul, suggest_choice, suggest_noul
from emotorad_ai.decisions import NONE, NONE_OF_THESE, Q_CATEGORY
from emotorad_ai.errorcodes import ANY_CODE, load_table
from emotorad_ai.jev import JevDecision, choose, yes
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import LANGUAGES, load_standard_responses
from tests.jev_golden import EXTRAS, GoldenCase, load_golden
from tests.test_retrieval_evals import GOLDEN


class GoldenSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.cases = load_golden()

    def test_every_label_points_at_something_that_exists(self):
        records = {r.id for r in self.kb.records}
        standard = {s.id for s in load_standard_responses()}
        codes = {e.code for e in load_table().entries if e.code != ANY_CODE}
        for case in self.cases:
            with self.subTest(case.text):
                self.assertIn(case.category, (None, "battery", "motor", NONE_OF_THESE))
                self.assertIn(case.sub_category, records | {NONE, None})
                self.assertIn(case.standard_response, standard | {NONE, None})
                self.assertIn(case.error_code, codes | {NONE, None})
                self.assertIn(case.language, set(LANGUAGES) | {"other"})

    def test_a_record_label_agrees_with_its_category_and_applies_to_its_bike(self):
        records = {r.id: r for r in self.kb.records}
        for case in self.cases:
            record = records.get(case.sub_category)
            if record is None:
                continue
            with self.subTest(case.text):
                if case.category is not None:
                    self.assertEqual(record.topic, case.category)
                if case.bike.get("product_name"):
                    self.assertTrue(self.kb.applicable(record, case.bike))

    def test_the_set_is_big_enough_and_balanced_enough_to_calibrate_on(self):
        self.assertGreaterEqual(len(self.cases), 200)
        languages = Counter(case.language for case in self.cases)
        for language in ("english", "hinglish", "hindi"):
            self.assertGreaterEqual(languages[language], 30, languages)
        records = Counter(case.sub_category for case in self.cases)
        for record in self.kb.records:
            self.assertGreaterEqual(records[record.id], 4, record.id)
        self.assertGreaterEqual(sum(1 for c in self.cases if c.needs_warranty_lookup), 12)
        self.assertGreaterEqual(len({c.error_code for c in self.cases} - {NONE, None}), 12)

    def test_imported_label_overrides_name_real_retrieval_phrases_and_apply(self):
        raw = yaml.safe_load(EXTRAS.read_text(encoding="utf-8"))
        golden_texts = {query for query, _, _, _ in GOLDEN}
        self.assertTrue(set(raw["imported"]) <= golden_texts, set(raw["imported"]) - golden_texts)
        by_text = {case.text: case for case in self.cases}
        self.assertEqual(by_text["charger laga diya but nothing happens"].language, "hinglish")
        self.assertTrue(by_text["battery replacement under warranty"].needs_warranty_lookup)
        self.assertTrue(by_text["warranty mein replacement milega"].needs_warranty_lookup)

    def test_hindi_in_latin_letters_is_labelled_hinglish_without_a_marker_from_the_metrics_list(self):
        by_text = {case.text: case for case in self.cases}
        for text in ("dhanyavaad", "ruko check karta hoon", "aap insaan ho ya bot", "haan ek minute"):
            with self.subTest(text):
                self.assertEqual(by_text[text].language, "hinglish")
        for text in ("thanks", "are you human?", "one sec"):
            with self.subTest(text):
                self.assertEqual(by_text[text].language, "english")

    def test_an_error_code_message_is_not_scored_on_category(self):
        # Codes span components (E-07 is the motor, E-06 the charging port), so
        # the message alone does not settle the category.
        for case in self.cases:
            if case.error_code not in (None, NONE):
                with self.subTest(case.text):
                    self.assertIsNone(case.category)


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
