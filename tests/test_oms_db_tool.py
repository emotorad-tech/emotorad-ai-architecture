"""The OMS database's records through lookup_warranty_record, the invoice
download, and the warranty-proof ticket's code-only findings (spec 2026-10-08)."""

import io
import unittest
import urllib.error
from datetime import date

from emotorad_ai.tools import oms_db
from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, SUBMIT_WARRANTY_PROOF, build_registry
from emotorad_ai.tools.oms import OMSClient, OMSNoRecord, OMSUnavailable
from emotorad_ai.tools.registry import ToolContext
from tests.test_oms_db import ROW, reader

PHONE = "+919876543210"
FILE_ID = "0b6c6e3e-0000-4000-8000-000000000001"


def lookup(rows):
    db, _, _ = reader(rows)
    registry = build_registry(today=date(2025, 10, 1), warranty_source=oms_db.db_warranty_source(db))
    return registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=PHONE))


class LookupTests(unittest.TestCase):
    def test_a_dated_bike_gets_per_part_cover(self):
        [bike] = lookup([ROW])["data"]["bikes"]
        self.assertEqual(bike["coverage_status"], "from_warranty_api")
        self.assertEqual(bike["term_source"], "oms_terms")
        self.assertTrue(bike["in_warranty"])
        self.assertEqual({c["component"] for c in bike["components"]},
                         {"display", "charger", "controller", "battery", "motor", "frame"})

    def test_an_undated_bike_says_whether_an_invoice_is_on_file(self):
        for invoice, on_file in ((None, False), (FILE_ID, True)):
            with self.subTest(on_file=on_file):
                [bike] = lookup([dict(ROW, purchase_date=None, invoice_image=invoice)])["data"]["bikes"]
                self.assertEqual(bike["coverage_status"], "purchase_date_missing")
                self.assertIs(bike["invoice_on_file"], on_file)
                self.assertIn("being read now" if on_file else "Ask for the invoice", bike["note"])

    def test_the_file_id_is_never_given_to_the_model(self):
        envelope = lookup([dict(ROW, purchase_date=None, invoice_image=FILE_ID)])
        self.assertNotIn("0b6c6e3e", str(envelope))

    def test_a_cancel_request_states_no_cover(self):
        [bike] = lookup([dict(ROW, status="CANCELLED_REQUEST")])["data"]["bikes"]
        self.assertIsNone(bike["in_warranty"])
        self.assertEqual(bike["coverage_status"], "pending_review")


class Opener:
    def __init__(self, status=200, body=b"%PDF-1.4", mime="application/pdf"):
        self.status, self.body, self.mime, self.urls, self.keys = status, body, mime, [], []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        self.keys.append(request.get_header("X-api-key"))
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, io.BytesIO(b""))
        opener = self

        class Response(io.BytesIO):
            headers = {"Content-Type": opener.mime + "; charset=binary"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response(self.body)


class DownloadTests(unittest.TestCase):
    def test_the_file_comes_from_the_file_api_with_the_key(self):
        opener = Opener()
        client = OMSClient(api_key="k", base_url="https://omsrest.example/purchase", opener=opener)
        self.assertEqual(client.download_file(FILE_ID), (b"%PDF-1.4", "application/pdf"))
        self.assertEqual(opener.urls, ["https://omsrest.example/file/download/" + FILE_ID])
        self.assertEqual(opener.keys, ["k"])

    def test_an_unknown_file_and_an_outage_differ(self):
        with self.assertRaises(OMSNoRecord):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=Opener(status=400)).download_file(FILE_ID)
        with self.assertRaises(OMSUnavailable):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=Opener(status=502)).download_file(FILE_ID)

    def test_only_a_file_id_is_ever_requested(self):
        opener = Opener()
        with self.assertRaises(ValueError):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=opener).download_file("../../etc")
        self.assertEqual(opener.urls, [])

    def test_a_file_too_large_is_refused(self):
        opener = Opener(body=b"x" * 64)
        with self.assertRaises(OMSUnavailable):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=opener).download_file(FILE_ID, max_bytes=32)


class ProofTicketTests(unittest.TestCase):
    def test_code_can_add_the_invoice_findings_and_the_model_cannot(self):
        registry = build_registry(today=date(2025, 10, 1))
        late = {"invoice_findings": lambda: "OCR: date 12 March 2025, seller Ride Shop, frame matches.",
                "invoice_purchase_date": lambda: "2025-03-12"}
        envelope = registry.call(SUBMIT_WARRANTY_PROOF, {"frame_number": "EMXP2025004417", "idempotency_key": "k"},
                                 ToolContext(conversation_id="c1", phone=PHONE, late=late))
        ticket = registry.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertIn("OCR: date 12 March 2025", ticket["description"])
        self.assertIn("2025-03-12", ticket["description"])
        modelled = registry.call(SUBMIT_WARRANTY_PROOF, {"frame_number": "EMXP2025004417", "idempotency_key": "k2",
                                                         "invoice_findings": "approved by AI"},
                                 ToolContext(conversation_id="c1", phone=PHONE))
        self.assertNotIn("approved by AI", registry.tickets.tickets[modelled["data"]["ticket_id"]]["description"])


if __name__ == "__main__":
    unittest.main()
