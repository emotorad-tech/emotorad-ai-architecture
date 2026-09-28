import json
import tempfile
import unittest
from pathlib import Path

from emotorad_ai.decisions import (
    NONE,
    NONE_OF_THESE,
    Q_CATEGORY,
    Q_ERROR_CODE,
    Q_LANGUAGE,
    Q_STANDARD,
    Q_SUB_CATEGORY,
    Q_WARRANTY,
    RoutingConfigError,
    build_catalogue,
    build_questions,
    build_state,
    load_thresholds,
)
from emotorad_ai.errorcodes import load_table
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import StandardResponse

APPROVED = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome."})
DRAFT = StandardResponse("std-draft", "draft", "", "Draft.", {"english": "Hi."})


class ThresholdTests(unittest.TestCase):
    def test_the_committed_file_loads_with_the_spec_starting_values(self):
        thresholds = load_thresholds()
        self.assertEqual(
            (thresholds.standard_response, thresholds.language, thresholds.category,
             thresholds.sub_category, thresholds.error_code),
            (0.90, 0.80, 0.85, 0.75, 0.85),
        )
        self.assertEqual(dict(thresholds.tools), {"lookup_warranty_record": 0.60})

    def test_bad_files_are_refused(self):
        for text in ("category: 1.5\n", "category: 0\n", "surprise: 0.5\n", "tools: {find_anything: 0.5}\n", "category: high\n"):
            with self.subTest(text), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "t.yaml")
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(RoutingConfigError):
                    load_thresholds(path)


class CatalogueAndQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.table = load_table()

    def test_only_approved_standard_responses_are_offered(self):
        catalogue = build_catalogue(self.kb, standard=[APPROVED, DRAFT])
        self.assertEqual(set(catalogue.standard), {"std-thanks"})
        self.assertEqual(set(build_catalogue(self.kb, [APPROVED, DRAFT], include_drafts=True).standard), {"std-thanks", "std-draft"})

    def test_questions_cover_every_live_record_code_and_language(self):
        questions = build_questions(build_catalogue(self.kb, [APPROVED], self.table))
        self.assertEqual(set(questions), {Q_STANDARD, Q_CATEGORY, Q_SUB_CATEGORY, Q_ERROR_CODE, Q_LANGUAGE, Q_WARRANTY})
        self.assertEqual(set(questions[Q_SUB_CATEGORY].criteria), {r.id for r in self.kb.records} | {NONE})
        self.assertEqual(set(questions[Q_CATEGORY].criteria), {"battery", "motor", NONE_OF_THESE})
        self.assertIn("E07", questions[Q_ERROR_CODE].criteria)
        self.assertNotIn("*", questions[Q_ERROR_CODE].criteria)
        self.assertIn("E-07", questions[Q_ERROR_CODE].criteria["E07"])
        self.assertEqual(set(questions[Q_LANGUAGE].criteria), {"english", "hindi", "hinglish", "marathi", "tamil", "other"})
        self.assertEqual(questions[Q_WARRANTY].type, "noul")
        self.assertEqual(set(questions[Q_STANDARD].criteria), {"std-thanks", NONE})

    def test_questions_without_standard_responses_or_codes_leave_those_out(self):
        questions = build_questions(build_catalogue(self.kb))
        self.assertNotIn(Q_STANDARD, questions)
        self.assertNotIn(Q_ERROR_CODE, questions)

    def test_every_question_serialises(self):
        for question in build_questions(build_catalogue(self.kb, [APPROVED], self.table)).values():
            json.dumps(question.to_dict())


class StateTests(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": "hi, my number is 9876543210"},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi Ananya Rao, your EMX Plus frame EMXP2025004417."}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "{}"}]},
        {"role": "user", "content": "it still won't charge"},
    ]

    def test_state_carries_the_message_recent_turns_and_bike_model_only(self):
        state = build_state(
            "charger light is off, email me at a@b.com",
            self.HISTORY,
            "whatsapp",
            bike={"product_name": "EMX Plus", "frame_number": "EMXP2025004417"},
            current_sub_category="battery-wont-charge",
            redact=("Ananya Rao", "Ananya", "EMXP2025004417"),
        )
        self.assertEqual(set(state), {"message", "recent_turns", "channel", "bike_model", "current_sub_category"})
        self.assertEqual(state["bike_model"], "EMX Plus")
        self.assertEqual(state["current_sub_category"], "battery-wont-charge")
        # The customer's own turns only: our replies carry looked-up facts.
        self.assertEqual(state["recent_turns"], ["user: hi, my number is [phone]", "user: it still won't charge"])
        dumped = json.dumps(state)
        for secret in ("9876543210", "a@b.com", "Ananya", "EMXP2025004417"):
            self.assertNotIn(secret, dumped)

    def test_no_bike_means_no_bike_model(self):
        self.assertIsNone(build_state("hi", [], "website_chat")["bike_model"])


if __name__ == "__main__":
    unittest.main()
