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


class SwollenBatteryTests(unittest.TestCase):
    """The way people actually say it.

    `swell(ing|ed|s)?` does not match "swollen" — the irregular past participle,
    and the most natural phrasing. "My battery is swollen" was passing straight
    through the safety gate to the model, and swelling is the canonical pre-fire
    symptom on a lithium pack. Found while checking whether a melted-terminal
    flow could run at all.
    """

    def test_swollen_reaches_the_safety_gate(self):
        from emotorad_ai.guardrails import check_safety

        for phrasing in (
            "my battery is swollen",
            "the pack looks swollen",
            "battery has swollen up",
            "it has bulged out",
        ):
            self.assertTrue(check_safety(phrasing).triggered, phrasing)

    def test_ordinary_faults_still_pass(self):
        from emotorad_ai.guardrails import check_safety

        for phrasing in (
            "my cycle is not turning on",
            "the display shows E-06",
            "the range has dropped",
        ):
            self.assertFalse(check_safety(phrasing).triggered, phrasing)


class MeltingReachesTheAgentTests(unittest.TestCase):
    """Melting is assessed, not intercepted — swelling is the other way round.

    A melted terminal is a thermal event that has already finished, and the case
    turns on how far the heat travelled: the battery photo *and* the controller
    photo. Handing it straight to a human meant that assessment never happened.

    A swollen pack is not the same thing and must keep firing. It is venting gas
    now, which is the standard pre-ignition sign, and nothing in the melting flow
    needs it to reach the model.
    """

    def test_melting_reaches_the_agent(self):
        from emotorad_ai.guardrails import check_safety

        for phrasing in (
            "my battery is melted",
            "the terminal looks melted",
            "battery port melted",
        ):
            self.assertFalse(check_safety(phrasing).triggered, phrasing)

    def test_swelling_still_stops_the_conversation(self):
        from emotorad_ai.guardrails import check_safety

        for phrasing in ("my battery is swollen", "the pack has swelled up", "it has bulged"):
            self.assertTrue(check_safety(phrasing).triggered, phrasing)

    def test_the_rest_of_the_gate_is_untouched(self):
        from emotorad_ai.guardrails import check_safety

        expected = {
            "battery caught fire": "fire",
            "there are sparks from the battery": "sparks",
            "smoke is coming from the battery": "smoke",
            "it is too hot to touch": "overheating",
            "fluid is leaking from it": "leak",
            "the casing is cracked": "physical_damage",
            "there is a burning smell": "burning_smell",
        }
        for phrasing, label in expected.items():
            verdict = check_safety(phrasing)
            self.assertTrue(verdict.triggered, phrasing)
            self.assertIn(label, verdict.matched, phrasing)
