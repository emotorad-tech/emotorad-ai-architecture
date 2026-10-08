"""The date post-check (date_check.py, ported from feat/warranty-status,
rulings W-25 to W-29, adapted to this branch's records on 8 October 2026).

A date in a customer agent's reply must be one the model was given: a date
the warranty record holds (purchase, start, end, each part's end), a date the
customer typed, the invoice date code told the customer, and, where the rule
allows, a date in a tool result. An invoice date read off a photo, or an end
date the model worked out, is blocked, and the blocked text is logged with
every date hidden.
"""

import unittest
from datetime import date

from emotorad_ai import date_check
from emotorad_ai.date_check import check_dates, invoice_days

DATED = {"frame_number": "EMXP2025004417", "coverage_status": "from_warranty_api", "purchase_date": "2025-03-12",
         "warranty_start": "2025-03-12", "warranty_end": "2026-03-11",
         "components": [{"component": "battery", "valid_until": "2026-03-11"},
                        {"component": "frame", "valid_until": "2030-03-11"}]}
UNDATED = {"frame_number": "EMXP2025004417", "coverage_status": "purchase_date_missing", "purchase_date": None}


def lookup(*bikes):
    return [{"data": {"bikes": list(bikes)}}]


def blocked(reply, evidence=(), customer=(), **kwargs):
    return check_dates(reply, list(evidence), list(customer), **kwargs)


class AllowedTests(unittest.TestCase):
    def test_no_date_passes(self):
        self.assertFalse(blocked("Please check the charger socket.", lookup(DATED)).blocked)

    def test_the_records_own_dates_pass(self):
        for reply in ("Your battery is covered until 11 March 2026.", "You bought it on 12 March 2025.",
                      "The frame is covered until 11 March 2030.",
                      "आपकी बैटरी 11 मार्च 2026 तक वारंटी में है।"):
            with self.subTest(reply=reply):
                self.assertFalse(blocked(reply, lookup(DATED)).blocked)

    def test_a_date_the_customer_typed_passes(self):
        self.assertFalse(blocked("You said you bought it on 5 June 2024; the invoice will confirm it.",
                                 lookup(UNDATED), ["I bought it on 5 June 2024"]).blocked)

    def test_the_invoice_date_code_told_passes_and_its_end(self):
        days = invoice_days(date(2025, 3, 12))
        self.assertFalse(blocked("As I said, your invoice is dated 12 March 2025.", lookup(UNDATED),
                                 invoice_days=days).blocked)
        self.assertFalse(blocked("Your battery is covered until 11 March 2026.", lookup(UNDATED),
                                 invoice_days=days).blocked)

    def test_a_tools_date_away_from_any_warranty_or_invoice_word_passes(self):
        evidence = lookup(DATED) + [{"data": {"slot": "2026-10-14T10:00"}}]
        self.assertFalse(blocked("I have booked you for 14 October 2026.", evidence).blocked)


class BlockedTests(unittest.TestCase):
    def test_an_invoice_date_the_model_read_is_blocked_in_every_language(self):
        for reply in ("Going by your invoice dated 5 June 2024, you are covered.",
                      "Aapka bill 5 June 2024 ka hai.",
                      "आपका बिल 5 जून 2024 का है।",
                      "The photo you sent shows 05/06/2024."):
            with self.subTest(reply=reply):
                check = blocked(reply, lookup(UNDATED))
                self.assertTrue(check.blocked)
                self.assertEqual(check.reason, "invoice_date_by_model")

    def test_an_end_date_the_model_worked_out_is_blocked(self):
        # 24 months from the record's purchase date: never given by any tool.
        check = blocked("Your battery is covered until 11 March 2027.", lookup(DATED))
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "warranty_date_not_from_result")

    def test_any_date_without_a_record_date_must_have_been_given(self):
        check = blocked("You should be fine until 1 January 2027.", lookup(UNDATED))
        self.assertTrue(check.blocked)
        self.assertTrue(blocked("You should be fine until 1 January 2027.", lookup(DATED),
                                no_oms_date=True).blocked)

    def test_a_date_the_model_passed_to_a_tool_vouches_for_nothing(self):
        evidence = lookup(UNDATED) + [{"data": {"echo": "2024-06-05"}}]
        check = check_dates("Going by your invoice dated 5 June 2024.", evidence, [],
                            arguments=[{}, {"date": "2024-06-05"}])
        self.assertTrue(check.blocked)


class RedactionTests(unittest.TestCase):
    def test_a_blocked_reply_is_logged_with_every_date_hidden(self):
        self.assertEqual(date_check.DATE_REASONS, ("invoice_date_by_model", "warranty_date_not_from_result"))
        self.assertEqual(date_check.redacted("Your invoice dated 5 June 2024."), "your invoice dated [date].")
        # A text with no date is logged as it was written.
        self.assertEqual(date_check.redacted("Good news: it is covered."), "Good news: it is covered.")


if __name__ == "__main__":
    unittest.main()


class RuntimeTests(unittest.TestCase):
    """Through runtime.handle(): the check runs on customer agents' replies,
    a blocked reply never reaches the customer and its log holds no date, and
    the date code told may be said again."""

    def chat(self, replies, svc=None):
        from tests.test_invoice_flow import Chat, service

        return Chat(replies, svc or service())

    def test_an_invoice_date_the_model_wrote_is_blocked_and_never_logged(self):
        chat = self.chat([__import__("emotorad_ai.llm", fromlist=["say"]).say(
            "Going by your invoice dated 5 June 2024, your battery is covered.")])
        reply = chat.say("is my battery covered?")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertNotIn("5 June 2024", reply.text)
        logged = str([e for e in chat.log.events if "suppressed_text" in str(e)])
        self.assertNotIn("5 June 2024", logged)
        self.assertNotIn("2024", logged)

    def test_a_date_only_reply_is_blocked_by_this_check(self):
        from emotorad_ai.llm import say

        chat = self.chat([say("Your invoice is dated 5 June 2024, thanks for sending it.")])
        reply = chat.say("here is my invoice")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        [event] = [e for e in chat.log.events if e.get("guardrail") == "coverage_post_check"]
        self.assertEqual(event["triggered_by"]["reason"], "invoice_date_by_model")
        self.assertEqual(event["triggered_by"]["suppressed_text"], "your invoice is dated [date], thanks for sending it.")

    def test_the_date_code_told_may_be_said_again(self):
        from emotorad_ai.llm import say
        from tests.test_invoice_flow import CLUSTER, FRAME, PHONE, service

        svc = service()
        svc.read_from_oms("c1", "u", CLUSTER, PHONE, FRAME)
        chat = self.chat([say("Let me check your invoice."), say("As I said, your invoice is dated 12 March 2025.")],
                         svc)
        chat.say("is my battery covered?")
        again = chat.say("what date was that?")
        self.assertNotEqual(again.handled_by, "guardrail:coverage_post_check")
        self.assertIn("12 March 2025", again.text)
