"""Reading an invoice (spec 2026-10-08, section 3): Gemini reads, code decides
whether the reading is confident and writes the customer's line."""

import base64
import json
import unittest
from datetime import date

from emotorad_ai import invoice_ocr
from emotorad_ai.invoice_ocr import InvoiceReadError, assess, customer_line, findings_text, parse
from emotorad_ai.openrouter import OpenRouterRateLimited
from tests.test_serial_read import FakeTransport

TODAY = date(2025, 10, 1)
FRAME = "EMXP2025004417"
GOOD = {"is_invoice": True, "invoice_date": "12/03/2025", "seller": "Ride Shop",
        "frame_numbers": [FRAME], "product": "EMX Plus", "legible": True}


class ParseTests(unittest.TestCase):
    def test_a_clear_invoice(self):
        self.assertEqual(parse(json.dumps(GOOD)), GOOD)

    def test_only_the_known_keys_and_real_trues_are_kept(self):
        found = parse(json.dumps(dict(GOOD, legible="yes", is_invoice=1, frame_numbers="EMX1", extra="x")))
        self.assertEqual((found["legible"], found["is_invoice"], found["frame_numbers"]), (False, False, ["EMX1"]))
        self.assertNotIn("extra", found)

    def test_a_photo_that_is_not_an_invoice(self):
        self.assertFalse(parse(json.dumps({"is_invoice": False}))["is_invoice"])

    def test_bad_json_is_an_error(self):
        with self.assertRaises(InvoiceReadError):
            parse("the date is 12 March")


class AssessTests(unittest.TestCase):
    def test_confident_when_the_date_is_real_and_the_frame_matches(self):
        result = assess(GOOD, FRAME, TODAY)
        self.assertEqual((result["confident"], result["purchase_date"]), (True, date(2025, 3, 12)))

    def test_the_date_is_read_day_first_in_words_too(self):
        self.assertEqual(assess(dict(GOOD, invoice_date="12 Mar 2025"), FRAME, TODAY)["purchase_date"],
                         date(2025, 3, 12))

    def test_the_frame_matches_ignoring_case_spaces_and_dashes(self):
        self.assertTrue(assess(dict(GOOD, frame_numbers=["emxp-2025 004417"]), FRAME, TODAY)["confident"])

    def test_not_confident(self):
        cases = {
            "future": dict(GOOD, invoice_date="12/03/2026"),
            "before 2019": dict(GOOD, invoice_date="12/03/2018"),
            "another frame": dict(GOOD, frame_numbers=["EMXP2025009999"]),
            "no frame": dict(GOOD, frame_numbers=[]),
            "illegible": dict(GOOD, legible=False),
            "no date": dict(GOOD, invoice_date=None),
            "not an invoice": dict(GOOD, is_invoice=False),
            "nonsense date": dict(GOOD, invoice_date="31/02/2025"),
        }
        for name, found in cases.items():
            with self.subTest(name):
                result = assess(found, FRAME, TODAY)
                self.assertFalse(result["confident"])
                self.assertTrue(result["reason"])


class LineTests(unittest.TestCase):
    def test_confident_names_the_date_and_the_batterys_end(self):
        self.assertEqual(customer_line(assess(GOOD, FRAME, TODAY), TODAY, hindi=False),
                         "Going by your invoice dated 12 March 2025, your battery is covered until 11 March 2026. "
                         "A support executive will confirm this.")

    def test_not_confident_names_no_date(self):
        line = customer_line(assess(dict(GOOD, legible=False), FRAME, TODAY), TODAY, hindi=False)
        self.assertEqual(line, "Thanks, I have passed your invoice to our support team. A support executive will "
                               "confirm your warranty.")

    def test_hindi(self):
        self.assertIn("12 March 2025", customer_line(assess(GOOD, FRAME, TODAY), TODAY, hindi=True))
        self.assertIn("सपोर्ट टीम", customer_line(assess(dict(GOOD, legible=False), FRAME, TODAY), TODAY, hindi=True))

    def test_the_tickets_findings(self):
        text = findings_text(GOOD, assess(GOOD, FRAME, TODAY), "oms")
        self.assertEqual(text, "Invoice read by AI (source: OMS): date 12/03/2025, seller Ride Shop, frame numbers "
                               "EMXP2025004417, product EMX Plus; confident: yes. Warranty check: not confirmed yet.")
        self.assertIn("customer upload", findings_text(GOOD, assess(dict(GOOD, legible=False), FRAME, TODAY),
                                                       "customer"))
        self.assertIn("confident: no (not legible)",
                      findings_text(GOOD, assess(dict(GOOD, legible=False), FRAME, TODAY), "customer"))


class ReaderTests(unittest.TestCase):
    def test_a_pdf_goes_as_a_file_with_the_frame_and_no_retention(self):
        transport = FakeTransport(answer=GOOD)
        self.assertEqual(invoice_ocr.OpenRouterInvoiceReader(transport).read(b"%PDF", "application/pdf", FRAME), GOOD)
        body = transport.posts[0]["body"]
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        content = body["messages"][0]["content"]
        self.assertIn(FRAME, " ".join(p.get("text", "") for p in content if p["type"] == "text"))
        [file_part] = [p for p in content if p["type"] == "file"]
        self.assertEqual(file_part["file"]["file_data"], "data:application/pdf;base64," + base64.b64encode(b"%PDF").decode())

    def test_an_image_goes_as_an_image(self):
        transport = FakeTransport(answer=GOOD)
        invoice_ocr.OpenRouterInvoiceReader(transport).read(b"\xff\xd8", "image/jpeg", FRAME)
        self.assertTrue(any(p["type"] == "image_url" for p in transport.posts[0]["body"]["messages"][0]["content"]))

    def test_a_provider_failure_is_its_code_and_too_large_is_never_sent(self):
        with self.assertRaises(InvoiceReadError) as caught:
            invoice_ocr.OpenRouterInvoiceReader(FakeTransport(error=OpenRouterRateLimited("rate_limited"))).read(
                b"x", "image/jpeg", FRAME)
        self.assertEqual(str(caught.exception), "rate_limited")
        transport = FakeTransport(answer=GOOD)
        with self.assertRaises(InvoiceReadError):
            invoice_ocr.OpenRouterInvoiceReader(transport).read(b"x" * (invoice_ocr.INLINE_LIMIT + 1), "image/jpeg", FRAME)
        self.assertEqual(transport.posts, [])

    def test_off_unless_switched_on_with_the_key(self):
        self.assertIsNone(invoice_ocr.reader_from_env({"OPENROUTER_API_KEY": "k"}))
        self.assertIsNone(invoice_ocr.reader_from_env({"EMOTORAD_INVOICE_OCR": "on"}))
        self.assertIsNotNone(invoice_ocr.reader_from_env({"OPENROUTER_API_KEY": "k", "EMOTORAD_INVOICE_OCR": "on"}))


if __name__ == "__main__":
    unittest.main()
