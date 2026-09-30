"""One bike list from the OMS and Amigo (spec 2026-09-30, section 2)."""

import unittest
from datetime import date

from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import amigo_records, app_ref, display_model, frame_on_record, merged_source
from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, build_registry
from emotorad_ai.tools.registry import ToolContext, ToolError
from tests.amigo_fake import RIDER_A, RIDER_B, RIDER_C, RIDERS, FakeAmigo

TODAY = date(2026, 9, 30)


def fixture_oms(phone):
    return fixtures.WARRANTY_RECORDS.get(phone)


def lookup(source, phone):
    registry = build_registry(today=TODAY, warranty_source=source)
    return registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=phone))


class ModelNameTests(unittest.TestCase):
    def test_codes_become_the_names_riders_know(self):
        self.assertEqual(display_model("EMXPLUS"), "EMX Plus")
        self.assertEqual(display_model("DOODLEPRO"), "Doodle Pro")
        self.assertEqual(display_model("TREXSMART"), "T-Rex Smart")
        self.assertEqual(display_model("dy"), "Dynem")
        self.assertEqual(display_model("NEWMODEL9"), "NEWMODEL9")


class FrameOnRecordTests(unittest.TestCase):
    def test_an_imei_is_not_a_frame_number(self):
        self.assertFalse(frame_on_record({"framenumber": "860000000000032", "imei": "860000000000032"}))
        self.assertFalse(frame_on_record({"framenumber": "123456789012345", "imei": None}))
        self.assertFalse(frame_on_record({"framenumber": "", "imei": None}))
        self.assertTrue(frame_on_record({"framenumber": "TESTEMXP0000001", "imei": None}))


class AmigoRecordsTests(unittest.TestCase):
    def test_rider_b_has_no_frame_number_and_an_opaque_reference(self):
        # Opaque, not the VIN: the model reads bike_ref in tool results.
        [record] = amigo_records(RIDERS[RIDER_B])
        self.assertIsNone(record["frame_number"])
        self.assertFalse(record["frame_on_record"])
        self.assertEqual(record["bike_ref"], app_ref("FRPVINTEST0000000000000b"))
        self.assertRegex(record["bike_ref"], r"^app:[0-9a-f]{12}$")
        self.assertEqual(record["product_name"], "T-Rex Smart")
        self.assertEqual(record["product_color"], "Grey")
        self.assertFalse(record["warranty_on_record"])

    def test_the_default_username_is_not_a_name(self):
        rider = dict(RIDERS[RIDER_A], username="User")
        self.assertIsNone(amigo_records(rider)[0]["customer_name"])
        self.assertEqual(amigo_records(RIDERS[RIDER_A])[0]["customer_name"], "TEST Rider A")


class MergeTests(unittest.TestCase):
    def test_amigo_only_rider_gets_their_app_bikes_with_no_warranty_on_record(self):
        envelope = lookup(merged_source(fixture_oms, FakeAmigo()), RIDER_A)
        bikes = envelope["data"]["bikes"]
        self.assertEqual([b["product_name"] for b in bikes], ["EMX Plus", "Doodle Pro"])
        self.assertEqual({b["coverage_status"] for b in bikes}, {"not_registered"})
        self.assertTrue(all(b["in_warranty"] is None for b in bikes))
        self.assertEqual(envelope["data"]["customer_name"], "TEST Rider A")

    def test_oms_only_rider_is_unchanged(self):
        plain = lookup(None, "+919876543210")["data"]["bikes"]
        merged = lookup(merged_source(fixture_oms, FakeAmigo()), "+919876543210")["data"]["bikes"]
        self.assertEqual([b["frame_number"] for b in merged], [b["frame_number"] for b in plain])
        self.assertEqual(merged[0]["coverage_status"], plain[0]["coverage_status"])
        self.assertFalse(merged[0]["in_app"])

    def test_a_bike_in_both_is_listed_once_ignoring_case_and_spaces(self):
        def oms(phone):
            return [dict(fixtures.WARRANTY_RECORDS["+919876543210"][0], frame_number="testemxp 0000001",
                         mobile=RIDER_A, customer_name="Ananya Rao")]
        bikes = lookup(merged_source(oms, FakeAmigo()), RIDER_A)["data"]["bikes"]
        self.assertEqual(len(bikes), 2)  # the EMX Plus once, the Doodle Pro from the app
        self.assertTrue(bikes[0]["in_app"])
        self.assertNotEqual(bikes[0]["coverage_status"], "not_registered")
        self.assertEqual(bikes[1]["product_name"], "Doodle Pro")

    def test_amigo_down_leaves_the_oms_list(self):
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING") as logs:
            bikes = lookup(merged_source(fixture_oms, FakeAmigo(down=True)), "+919876543210")["data"]["bikes"]
        self.assertEqual(len(bikes), 1)
        self.assertIn("amigo_unavailable", "\n".join(logs.output))

    def test_amigo_down_and_no_oms_record_is_no_record(self):
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING"):
            envelope = lookup(merged_source(fixture_oms, FakeAmigo(down=True)), RIDER_A)
        self.assertEqual(envelope["error"]["code"], "no_warranty_record")

    def test_oms_down_still_lists_the_app_bikes(self):
        def down(phone):
            raise ToolError("oms_unavailable", "The warranty system is not responding.", retryable=True)
        bikes = lookup(merged_source(down, FakeAmigo()), RIDER_A)["data"]["bikes"]
        self.assertEqual({b["coverage_status"] for b in bikes}, {"warranty_unavailable"})

    def test_oms_down_and_no_app_bikes_is_the_oms_error(self):
        def down(phone):
            raise ToolError("oms_unavailable", "The warranty system is not responding.", retryable=True)
        envelope = lookup(merged_source(down, FakeAmigo()), "+919876543210")
        self.assertEqual(envelope["error"]["code"], "oms_unavailable")

    def test_neither_is_no_record(self):
        self.assertEqual(lookup(merged_source(fixture_oms, FakeAmigo()), "+919700000099")["error"]["code"],
                         "no_warranty_record")

    def test_bike_ref_is_the_frame_number_when_on_record(self):
        bikes = lookup(merged_source(fixture_oms, FakeAmigo()), RIDER_C)["data"]["bikes"]
        self.assertEqual(bikes[0]["bike_ref"], "TESTTREX0000003")
        self.assertTrue(bikes[0]["frame_on_record"])


if __name__ == "__main__":
    unittest.main()
