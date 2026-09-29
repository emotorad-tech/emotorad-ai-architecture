import re
import tempfile
import textwrap
import unittest
from pathlib import Path

from emotorad_ai.live_eval.scenarios import Known, ScenarioError, known_from_code, load_suite
from emotorad_ai.standard_responses import load_standard_responses

KNOWN = Known(
    tools=frozenset({"create_support_ticket", "lookup_warranty_record"}),
    records=frozenset({"battery-wont-charge"}),
    standard=frozenset({"std-thanks-goodbye"}),
    agents=frozenset({"battery_support", "narrow_support"}),
)

VALID = """
scenarios:
  - id: a
    family: routing
    note: one turn
    who: {channel: website, session: sess-ananya}
    turns:
      - text: "my battery won't charge"
        expect: {path: [narrow, full], handled_by: narrow_support, sub_category: battery-wont-charge, no_tools: [create_support_ticket], ticket: false}
  - id: b
    family: failures
    who: {channel: whatsapp, phone: "+919700000009"}
    settings: {narrow_model: nonexistent/model, openrouter_timeout: 0.01}
    turns:
      - {text: "hi", repeat: 3}
cost_mixes:
  typical: {a: 7, b: 3}
  worst: {a: 10}
"""


def load(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scenarios.yaml"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return load_suite(path, KNOWN)


class LoadTests(unittest.TestCase):
    def test_a_valid_file_loads(self):
        suite = load(VALID)
        a, b = suite.scenarios
        self.assertEqual((a.id, a.family, a.who.session, a.note), ("a", "routing", "sess-ananya", "one turn"))
        self.assertEqual(a.turns[0].expect.path, ("narrow", "full"))
        self.assertEqual(a.turns[0].expect.handled_by, ("narrow_support",))
        self.assertEqual(a.turns[0].expect.ticket, False)
        self.assertEqual(b.settings, {"narrow_model": "nonexistent/model", "openrouter_timeout": 0.01})
        self.assertEqual(b.turns[0].message, "hi hi hi")
        self.assertEqual(suite.mixes["worst"], {"a": 10})

    def test_every_kind_of_typo_is_refused(self):
        cases = (
            ("family: routing", "family: routng", "'routng'"),
            ("path: [narrow, full]", "path: [narow, full]", "'narow'"),
            ("handled_by: narrow_support", "handled_by: narrow_suport", "'narrow_suport'"),
            ("sub_category: battery-wont-charge", "sub_category: battery-wont-chrage", "'battery-wont-chrage'"),
            ("no_tools: [create_support_ticket]", "no_tools: [create_ticket]", "'create_ticket'"),
            ("ticket: false", "tickt: false", "unknown field(s) tickt"),
            ("ticket: false", "ticket: nope", "expected true or false"),
            ("channel: website, session", "channel: web, session", "'web'"),
            ("narrow_model: nonexistent/model", "narrow_modle: nonexistent/model", "unknown field(s) narrow_modle"),
            ("openrouter_timeout: 0.01", "openrouter_timeout: fast", "expected a number above 0"),
            ("typical: {a: 7, b: 3}", "typical: {a: 7, b: 2}", "counts add up to 9, not 10"),
            ("worst: {a: 10}", "worst: {c: 10}", "unknown scenario(s) c"),
            ("id: b", "id: a", "duplicate scenario id(s): a"),
            ('{text: "hi", repeat: 3}', '{text: "hi", repeat: 0}', "repeat: expected a whole number of 1 or more"),
            ('who: {channel: whatsapp, phone: "+919700000009"}', "who: {channel: whatsapp}", "whatsapp needs a phone"),
        )
        for old, new, message in cases:
            with self.subTest(new):
                self.assertIn(old, VALID)
                with self.assertRaisesRegex(ScenarioError, re.escape(message)):
                    load(VALID.replace(old, new))

    def test_only_selects_by_family_or_id_and_refuses_unknown_names(self):
        suite = load(VALID)
        self.assertEqual([s.id for s in suite.select(["failures"])], ["b"])
        self.assertEqual([s.id for s in suite.select(["a"])], ["a"])
        self.assertEqual([s.id for s in suite.select(None)], ["a", "b"])
        with self.assertRaisesRegex(ScenarioError, "nope"):
            suite.select(["nope"])

    def test_the_real_code_is_known(self):
        known = known_from_code()
        self.assertIn("battery-wont-charge", known.records)
        self.assertIn("create_support_ticket", known.tools)
        self.assertIn("lookup_error_code", known.tools)
        self.assertIn("send_guide_media", known.tools)
        # Only approved standard replies reach Jev; drafts are not known.
        approved = frozenset(r.id for r in load_standard_responses() if r.approved)
        self.assertEqual(known.standard, approved)
        self.assertIn("late_warranty_registration", known.agents)
        self.assertIn("guardrail:battery_safety", known.handlers())
        self.assertIn("router", known.handlers())


if __name__ == "__main__":
    unittest.main()
