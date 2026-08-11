import unittest

from emotorad_ai.guardrails import check_battery_safety, check_human_handoff
from emotorad_ai.knowledge import BatteryKnowledgeBase


class SafetyGateTests(unittest.TestCase):
    def test_catches_the_dangerous_phrasings(self):
        for text in [
            "my battery is swelling on one side",
            "the pack looks bulged",
            "there was smoke coming from the battery",
            "it caught fire while charging",
            "there is a burning smell after I charge it",
            "the battery is too hot to touch",
            "something is leaking from the pack",
            "the casing is cracked",
            "I saw sparks at the charging port",
        ]:
            with self.subTest(text=text):
                self.assertTrue(check_battery_safety(text).triggered)

    def test_leaves_ordinary_complaints_alone(self):
        for text in [
            "my battery won't charge",
            "the range has dropped a lot since last month",
            "the display doesn't turn on",
            "how long should a full charge take?",
        ]:
            with self.subTest(text=text):
                self.assertFalse(check_battery_safety(text).triggered)

    def test_reports_what_matched_for_the_log(self):
        verdict = check_battery_safety("battery is swelling and there is a burning smell")
        self.assertIn("swelling", verdict.matched)
        self.assertIn("burning_smell", verdict.matched)


class HandoffGateTests(unittest.TestCase):
    def test_catches_requests_for_a_person(self):
        for text in [
            "talk to a human",
            "can I speak with someone please",
            "connect me to an agent",
            "I want customer care",
            "I don't want a bot",
        ]:
            with self.subTest(text=text):
                self.assertTrue(check_human_handoff(text).triggered)

    def test_does_not_fire_on_ordinary_support_talk(self):
        for text in ["my battery won't charge", "who services these bikes in Pune?"]:
            with self.subTest(text=text):
                self.assertFalse(check_human_handoff(text).triggered)


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.kb = BatteryKnowledgeBase()

    def test_retrieves_the_relevant_passage(self):
        passages = self.kb.search("charger LED does not come on, battery not charging")
        self.assertTrue(passages)
        self.assertEqual(passages[0].id, "battery-wont-charge")
        self.assertTrue(passages[0].source)

    def test_range_question_retrieves_the_range_passage(self):
        passages = self.kb.search("range dropped and it drains fast now")
        self.assertEqual(passages[0].id, "battery-range-dropped")

    def test_returns_nothing_rather_than_noise_for_an_unrelated_query(self):
        self.assertEqual(self.kb.search("the the and of"), [])


if __name__ == "__main__":
    unittest.main()
