import unittest

from emotorad_ai.live_eval.scenarios import DEFAULT_PATH, FAMILIES, known_from_code, load_suite
from emotorad_ai.tools import fixtures


class LiveScenarioFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_suite(DEFAULT_PATH, known_from_code())

    def test_the_file_loads_against_the_real_code_and_covers_every_family(self):
        self.assertGreaterEqual(len(self.suite.scenarios), 45)
        self.assertEqual({s.family for s in self.suite.scenarios}, set(FAMILIES))

    def test_every_scenario_speaks_as_a_fixture_person(self):
        known_phones = set(fixtures.WARRANTY_RECORDS) | set(fixtures.DEALERS) | {"+919700000009", "+919000000099"}
        for s in self.suite.scenarios:
            with self.subTest(s.id):
                if s.who.session:
                    self.assertIn(s.who.session, fixtures.SESSIONS)
                if s.who.phone:
                    self.assertIn(s.who.phone, known_phones)

    def test_the_mixes_have_the_agreed_shape(self):
        by_id = {s.id: s for s in self.suite.scenarios}
        typical = self.suite.mixes["typical"]
        self.assertIn("guardrails", {by_id[sid].family for sid in typical})
        self.assertTrue(any(by_id[sid].who.channel == "dealer_whatsapp" for sid in typical))
        [(worst, count)] = self.suite.mixes["worst"].items()
        self.assertEqual(count, 10)
        self.assertGreaterEqual(len(by_id[worst].turns), 5)


if __name__ == "__main__":
    unittest.main()
