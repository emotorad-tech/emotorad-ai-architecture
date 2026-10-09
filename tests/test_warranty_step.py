"""The warranty step's pure parts (spec 2026-10-09 warranty step, sections 3 and 4)."""

import unittest

from emotorad_ai import warranty_step as ws
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.contract import Identity

DATED = {"frame_number": "EMXP0001", "product_name": "X1", "coverage_status": "from_warranty_api",
         "purchase_date": "2025-03-12", "in_warranty": True, "months_remaining": 5, "warranty_end": "2026-03-11"}
ON_FILE = {"frame_number": "EMXP0002", "product_name": "X1", "coverage_status": "purchase_date_missing",
           "purchase_date": None, "invoice_on_file": True, "invoice_with_support": False}
NO_INVOICE = {"frame_number": "EMXP0003", "product_name": "X1", "coverage_status": "purchase_date_missing",
              "purchase_date": None, "invoice_on_file": False, "invoice_with_support": False}
WITH_SUPPORT = dict(NO_INVOICE, frame_number="EMXP0004", invoice_with_support=True)


def resolved(bikes, method="warranty_lookup"):
    return ResolvedIdentity(persona="customer", identity=Identity(phone="+919999999999", strength="verified"),
                            bikes=list(bikes), method=method)


def state(frame=None, unlisted=None):
    s = ConversationState(conversation_id="c1")
    s.selected_frame = frame
    s.unlisted_bike = unlisted
    return s


class CaseTests(unittest.TestCase):
    def test_each_case_for_the_chosen_bike(self):
        bikes = [DATED, ON_FILE, NO_INVOICE, WITH_SUPPORT]
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0001")), ws.DATED)
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0002")), ws.INVOICE_ON_FILE)
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0003")), ws.NEEDS_INVOICE)
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0004")), ws.NEEDS_INVOICE)

    def test_no_frame_for_no_record_and_for_an_unlisted_bike(self):
        self.assertEqual(ws.case_of(resolved([], method="no_warranty_record"), state()), ws.NO_FRAME)
        self.assertEqual(ws.case_of(resolved([DATED]), state(unlisted={"frame_number": "TYPED1"})), ws.NO_FRAME)

    def test_bikes_but_none_chosen_waits(self):
        self.assertIsNone(ws.case_of(resolved([DATED, ON_FILE]), state()))
        self.assertIsNone(ws.case_of(resolved([DATED]), state("NOT-A-FRAME")))


class LineTests(unittest.TestCase):
    def test_lines_and_actions(self):
        self.assertIsNone(ws.line_for(ws.DATED, DATED, hindi=False))
        self.assertEqual(ws.line_for(ws.INVOICE_ON_FILE, ON_FILE, hindi=False), ws.CHECKING_INVOICE_LINE)
        self.assertEqual(ws.line_for(ws.NEEDS_INVOICE, NO_INVOICE, hindi=False), ws.NEEDS_INVOICE_LINE)
        self.assertEqual(ws.line_for(ws.NEEDS_INVOICE, WITH_SUPPORT, hindi=False), ws.WITH_SUPPORT_LINE)
        self.assertEqual(ws.line_for(ws.NO_FRAME, None, hindi=False), ws.REGISTER_LATER_LINE)
        self.assertEqual(ws.actions_for(ws.NO_FRAME), [{"kind": "register_warranty", "label": "Register warranty"}])
        self.assertEqual(ws.actions_for(ws.DATED), [])

    def test_hindi_drafts(self):
        for case, bike in ((ws.INVOICE_ON_FILE, ON_FILE), (ws.NEEDS_INVOICE, NO_INVOICE), (ws.NO_FRAME, None)):
            with self.subTest(case=case):
                line = ws.line_for(case, bike, hindi=True)
                self.assertTrue(any("ऀ" <= ch <= "ॿ" for ch in line), line)

    def test_no_line_holds_a_date_or_a_coverage_claim(self):
        for line in (ws.CHECKING_INVOICE_LINE, ws.NEEDS_INVOICE_LINE, ws.WITH_SUPPORT_LINE, ws.REGISTER_LATER_LINE):
            with self.subTest(line=line):
                self.assertNotIn("covered", line.lower())
                self.assertFalse(any(ch.isdigit() for ch in line))


class PendingViewTests(unittest.TestCase):
    def test_the_view_keeps_the_bike_and_drops_every_cover_field(self):
        view = ws.pending_view(dict(DATED, components=[{"component": "battery"}], invoice_on_file=True))
        self.assertEqual((view["frame_number"], view["product_name"], view["coverage_status"]),
                         ("EMXP0001", "X1", ws.AFTER_ISSUE))
        for key in ("in_warranty", "months_remaining", "warranty_end", "purchase_date", "components",
                    "invoice_on_file"):
            self.assertNotIn(key, view)


class IntentTests(unittest.TestCase):
    def test_registration_requests_in_english_hinglish_and_hindi(self):
        for text in ("I want to register my warranty", "warranty registration", "how do I register my bike",
                     "mujhe warranty register karna hai", "वारंटी रजिस्टर करनी है", "पंजीकरण करना है"):
            with self.subTest(text=text):
                self.assertTrue(ws.asks_to_register(text))

    def test_not_a_registration_request(self):
        for text in ("my battery won't charge", "the motor makes a noise", "register"):
            with self.subTest(text=text):
                self.assertEqual(ws.asks_to_register(text), text == "register")


if __name__ == "__main__":
    unittest.main()
