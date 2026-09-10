"""Display error codes, resolved against the customer's own bike.

An exact lookup, never a near match. Semantic search over a code table would
return E-08 for a customer who said E-07, and a confidently wrong diagnosis is
the worst thing this bot can produce. A code that is not in the table comes back
as *not in the table*.
"""

import unittest
from datetime import date

from emotorad_ai.errorcodes import ErrorCodeError, load_table
from emotorad_ai.tools.mocks import LOOKUP_ERROR_CODE, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error


class TableTests(unittest.TestCase):
    def setUp(self):
        self.table = load_table()

    def test_the_shipped_table_loads(self):
        self.assertTrue(self.table.groups)
        self.assertTrue(self.table.entries)

    def test_codes_are_normalised_the_way_people_read_them_out(self):
        for written in ("E-07", "e7", "E 07", "e-07", "E07"):
            self.assertEqual(self.table.normalise(written), "E07", written)

    def test_messy_oms_product_names_still_match(self):
        # OMS returns "X2 Furious Red V2 - EM02BV01C23", not "X2".
        for name in ("X2 Furious Red V2 - EM02BV01C23", "Doodle V4 Indicator Edition", "STX 27.5 inch"):
            self.assertIsNotNone(self.table.group_for(name), name)

    def test_a_doodle_with_no_version_in_its_name_still_matches(self):
        # Live OMS ships "Doodle Black" with no version at all. Matching on
        # "doodle v2"/"doodle v3"/... missed it entirely, which would have been an
        # unknown_model refusal for a bike that is squarely in the table.
        for name in ("Doodle Black", "Doodle V1", "Doodle Pro", "Doodle V4 Indicator Edition"):
            group = self.table.group_for(name)
            self.assertIsNotNone(group, name)
            self.assertEqual(group["id"], "standard", name)

    def test_the_most_specific_model_group_wins(self):
        # A T-REX + V3 must not fall into the plain TREX+ group, or it would be
        # told E-30 is undocumented when it has a real diagnosis.
        v3 = self.table.group_for("T-REX + V3 Matte Black")
        plain = self.table.group_for("TREX+ 2024")
        self.assertEqual(v3["id"], "trex_plus_v3")
        self.assertNotEqual(plain["id"], "trex_plus_v3")

    def test_the_same_code_differs_by_model(self):
        # The reason the model is part of the key at all.
        on_v3 = self.table.lookup("E30", "T-REX + V3 Matte Black")
        on_plain = self.table.lookup("E30", "TREX+ 2024")
        self.assertEqual(on_v3["entry"]["disposition"], "diagnose")
        self.assertEqual(on_plain["entry"]["disposition"], "human_support")

    def test_an_unknown_code_is_not_answered_with_its_neighbour(self):
        result = self.table.lookup("E-99", "STX 27.5 inch")
        self.assertEqual(result["status"], "unknown_code")

    def test_an_unlisted_model_refuses_rather_than_guessing(self):
        self.assertEqual(self.table.lookup("E-07", "Some Unlisted Bike")["status"], "unknown_model")

    def test_a_wildcard_row_covers_every_code_on_its_model(self):
        for code in ("E-01", "E-42", "E99"):
            self.assertEqual(self.table.lookup(code, "X3 V1 Special")["status"], "found", code)

    def test_every_diagnosed_row_carries_both_audiences(self):
        # The customer line and the technician chain are different jobs; a row
        # missing one makes the bot improvise customer words from engineer
        # shorthand, or send a customer a parts list.
        for entry in self.table.entries:
            self.assertTrue(entry.customer_message, entry.code)
            if entry.disposition == "diagnose":
                self.assertTrue(entry.technician_chain, entry.code)


class MalformedTableTests(unittest.TestCase):
    """A bad row raises at load. A silently dropped row is a code the bot has
    quietly stopped recognising, with nothing anywhere to say so."""

    def _load(self, text):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "codes.yaml"
            path.write_text(text)
            return load_table(path)

    def _base(self, row):
        return (
            "model_groups:\n  - id: g\n    label: G\n    match: ['g']\ncodes:\n" + row
        )

    def test_a_diagnosed_row_without_a_chain_is_refused(self):
        with self.assertRaises(ErrorCodeError):
            self._load(self._base("  - code: E-01\n    groups: [g]\n    disposition: diagnose\n    customer_message: x\n"))

    def test_a_row_without_a_customer_message_is_refused(self):
        with self.assertRaises(ErrorCodeError):
            self._load(self._base("  - code: E-01\n    groups: [g]\n    disposition: human_support\n"))

    def test_an_unknown_model_group_is_refused(self):
        with self.assertRaises(ErrorCodeError):
            self._load(self._base("  - code: E-01\n    groups: [nope]\n    disposition: human_support\n    customer_message: x\n"))


class LookupToolTests(unittest.TestCase):
    def _call(self, bikes, code):
        registry = build_registry(today=date.today(), error_codes=load_table(), owned_bikes=bikes)
        return registry.call(LOOKUP_ERROR_CODE, {"code": code}, ToolContext(conversation_id="c"))

    def test_a_documented_code_returns_both_audiences(self):
        data = self._call([{"product_name": "X2 Furious Red V2"}], "E-07")["data"]
        self.assertEqual(data["disposition"], "diagnose")
        self.assertIn("motor", data["customer_message"].lower())
        self.assertIn("motor", data["technician_chain"].lower())

    def test_it_will_not_pick_a_bike_for_a_multi_model_customer(self):
        # The same code means different things on different bikes, so guessing
        # which one is showing it is guessing the answer.
        envelope = self._call(
            [{"product_name": "X2 Furious"}, {"product_name": "T-REX + V3"}], "E-30"
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "frame_number_required")

    def test_two_of_the_same_model_is_not_ambiguous(self):
        envelope = self._call([{"product_name": "X2 Furious"}, {"product_name": "X2 Doodle-free"}], "E-09")
        self.assertFalse(is_error(envelope))

    def test_no_resolved_bike_is_refused(self):
        self.assertTrue(is_error(self._call([], "E-07")))

    def test_the_tool_is_absent_without_a_table(self):
        self.assertNotIn(LOOKUP_ERROR_CODE, build_registry(today=date.today()).specs)


if __name__ == "__main__":
    unittest.main()
