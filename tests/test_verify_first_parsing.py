"""What the verify-first step reads out of a message, on its own.

Deterministic on purpose (the person's decision, 2026-09-30): the step is the
same every time and costs nothing, so it has to understand the ways people
actually type a number, a code and an order number.
"""

import unittest

from emotorad_ai.verify_first import (
    asks_resend,
    find_code,
    find_order_code,
    find_phone,
    looks_like_a_number,
    redact,
)


class PhoneTests(unittest.TestCase):
    def test_the_forms_people_type(self):
        for typed in ("9700000010", "97000 00010", "97000-00010", "+91 97000-00010", "+919700000010",
                      "09700000010", "+91 970 000 0010", "my number is 9700000010, thanks",
                      "मेरा नंबर 9700000010 है"):
            found = find_phone(typed)
            self.assertIsNotNone(found, typed)
            self.assertEqual(found[0], "9700000010", typed)

    def test_the_span_covers_what_was_typed(self):
        text = "it's 97000 00010 ok"
        value, (start, end) = find_phone(text)
        self.assertEqual(text[start:end], "97000 00010")

    def test_a_number_then_a_code_finds_the_number(self):
        self.assertEqual(find_phone("9700000010 482913")[0], "9700000010")

    def test_not_a_mobile(self):
        for typed in ("1234567890", "482913", "12345", "EMXP2026001234", "INV-2026-0042", "hello"):
            self.assertIsNone(find_phone(typed), typed)


class CodeTests(unittest.TestCase):
    def test_the_forms_people_type(self):
        for typed in ("482913", "482 913", "482-913", "the code is 482913", " 482913 "):
            self.assertEqual(find_code(typed)[0], "482913", typed)

    def test_not_a_code(self):
        for typed in ("9700000010", "EMXP2026001234", "12345", "1234567", "what code?"):
            self.assertIsNone(find_code(typed), typed)


class OrderCodeTests(unittest.TestCase):
    def test_order_and_invoice_numbers(self):
        for typed, value in (("EMO-100234", "EMO-100234"), ("my order number is EMO-100234", "EMO-100234"),
                             ("INV-2026-0042", "INV-2026-0042"), ("order 2045678", "2045678"),
                             ("ORD/20456", "ORD/20456")):
            self.assertEqual(find_order_code(typed)[0], value, typed)

    def test_things_that_are_not_order_numbers(self):
        for typed in ("it shows E-06", "48V battery", "1234567890", "482913", "I don't remember it",
                      "Doodle V3", "9700000010"):
            self.assertIsNone(find_order_code(typed), typed)


class NumberAttemptTests(unittest.TestCase):
    def test_a_try_at_a_number(self):
        self.assertTrue(looks_like_a_number("1234567890"))
        self.assertTrue(looks_like_a_number("123 456 7890"))

    def test_not_a_try(self):
        self.assertFalse(looks_like_a_number("I don't remember"))
        self.assertFalse(looks_like_a_number("482913"))


class ResendTests(unittest.TestCase):
    def test_asking_for_another_code(self):
        for typed in ("resend", "Please resend", "send it again", "new code please", "I didn't get it",
                      "did not receive any code", "code nahi aaya"):
            self.assertTrue(asks_resend(typed), typed)

    def test_not_asking(self):
        for typed in ("482913", "yes", "my battery"):
            self.assertFalse(asks_resend(typed), typed)


class RedactTests(unittest.TestCase):
    def test_the_span_is_replaced(self):
        text = "my number is 97000 00010 thanks"
        _, span = find_phone(text)
        self.assertEqual(redact(text, span, "[phone]"), "my number is [phone] thanks")


if __name__ == "__main__":
    unittest.main()
