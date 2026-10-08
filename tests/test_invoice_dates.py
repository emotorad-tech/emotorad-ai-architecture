"""Reading a printed date, day-first (warranty-status spec, 10.5).

The post-check's date rule (guardrails.check_dates) and, in Part 2, the
invoice reader both read dates here, so a date is the same date to both.
"""

import re
import unittest
from datetime import date

from emotorad_ai import invoice_dates
from emotorad_ai.invoice_dates import ParsedDate, find_dates, parse_printed_date, redact_dates
from emotorad_ai.textfold import fold


class FoldTests(unittest.TestCase):
    def test_the_nukta_case_and_spacing_are_folded(self):
        self.assertEqual(fold("मुफ़्त  में\nबदल"), fold("मुफ्त में बदल"))
        self.assertEqual(fold("  Your   WARRANTY  "), "your warranty")
        self.assertEqual(fold(None), "")

    def test_every_apostrophe_folds_to_one_ascii_apostrophe(self):
        # U+2018, U+2019, U+02BC and U+FF07: a phone keyboard or a model's
        # typography puts any of them in "you'll". One character for one, so
        # a span on the folded text does not move.
        for apostrophe in ("‘", "’", "ʼ", "＇"):
            with self.subTest(apostrophe="U+%04X" % ord(apostrophe)):
                typed = "You%sll be covered" % apostrophe
                self.assertEqual(fold(typed), "you'll be covered")
                self.assertEqual(len(fold(typed)), len(typed))


class ParsePrintedDateTests(unittest.TestCase):
    def test_an_ambiguous_date_is_read_day_first_with_its_other_reading(self):
        self.assertEqual(parse_printed_date("03/04/2025"),
                         ParsedDate(date(2025, 4, 3), ambiguous=True, other_reading=date(2025, 3, 4)))

    def test_every_printed_shape_gives_its_day(self):
        for printed in ("15/03/2025", "15-03-2025", "15.03.2025", "15 03 2025", "15-03-25", "2025-03-15",
                        "15 March 2025", "15th Mar, 2025", "Mar 15, 2025", "15-Mar-2025", "१५/०३/२०२५",
                        "15 मार्च 2025", "15/03/2025 14:32", "(15/03/2025)"):
            with self.subTest(printed=printed):
                self.assertEqual(parse_printed_date(printed), ParsedDate(date(2025, 3, 15)))

    def test_a_hindi_month_with_a_nukta_and_hindi_digits(self):
        self.assertEqual(parse_printed_date("३ फ़रवरी २०२५"), ParsedDate(date(2025, 2, 3)))

    def test_the_same_number_twice_is_not_ambiguous(self):
        self.assertEqual(parse_printed_date("04/04/2025"), ParsedDate(date(2025, 4, 4)))

    def test_a_day_that_does_not_exist_is_invalid(self):
        self.assertEqual(parse_printed_date("31/02/2025").reason, invoice_dates.INVALID_DATE)
        self.assertEqual(parse_printed_date("31 February 2025").reason, invoice_dates.INVALID_DATE)

    def test_a_month_first_date_is_not_read(self):
        self.assertEqual(parse_printed_date("03/15/2025"), ParsedDate(None, reason=invoice_dates.NOT_DAY_FIRST))

    def test_anything_else_is_unparsed(self):
        for printed in ("15/03-2025", "abc", "", "Ravi Kumar, Pune", "Invoice Date"):
            with self.subTest(printed=printed):
                self.assertEqual(parse_printed_date(printed), ParsedDate(None, reason=invoice_dates.UNPARSED))


class FindDatesTests(unittest.TestCase):
    def test_both_dates_in_a_sentence_are_found(self):
        self.assertEqual([found.date for found in find_dates("Bill dated 15/03/2025, delivered 18 March 2025")],
                         [date(2025, 3, 15), date(2025, 3, 18)])

    def test_only_real_dates_are_returned(self):
        # Ruling W-26: "03/15/2025" was here too; find_dates now reads an
        # unambiguous month-first date (test_a_month_first_date_is_read_...).
        self.assertEqual(find_dates("Order 31/02/2025 and 31/04/2025"), [])

    def test_a_month_first_date_is_read_when_it_can_be_nothing_else(self):
        # Ruling W-26: the second number is over 12, so it is the day. The
        # invoice reader still refuses it (parse_printed_date, NOT_DAY_FIRST).
        self.assertEqual([found.date for found in find_dates("Your invoice is dated 03/15/2025.")],
                         [date(2025, 3, 15)])
        self.assertEqual([found.date for found in find_dates("12/31/2024")], [date(2024, 12, 31)])
        self.assertEqual([found.date for found in find_dates("3-15-2025 and 03/15/25")],
                         [date(2025, 3, 15), date(2025, 3, 15)])
        self.assertEqual(parse_printed_date("03/15/2025"), ParsedDate(None, reason=invoice_dates.NOT_DAY_FIRST))

    def test_a_month_first_reading_needs_a_slash_or_a_dash(self):
        # Ruling W-27: a phone number or a version is not a date.
        for text in ("12 13 25", "Call 1800 12 13 25", "Version 2.14.10", "03.15.2025"):
            with self.subTest(text=text):
                self.assertEqual(find_dates(text), [])
        # A log still never holds one (ruling W-25).
        self.assertEqual(redact_dates("Bill 03.15.2025"), "bill [date]")

    def test_more_written_shapes_are_read(self):
        # Ruling W-26.
        for text in ("Your invoice is dated 15th of March 2025.", "Dated the 15th of March, 2025.",
                     "Your bill says 15 of March 2025.", "Your invoice is dated March the 15th, 2025.",
                     "Your invoice is dated 15th Mar'25.", "Your invoice is dated 15–03–2025.",
                     "Dated 2025–03–15.", "Dated 15–Mar–2025.", "Aapka bill 15 tarikh March 2025 ka hai.",
                     "आपका बिल 15 तारीख, मार्च 2025 का है।", "Bill 15 tareekh March 2025 ka."):
            with self.subTest(text=text):
                self.assertEqual([found.date for found in find_dates(text)], [date(2025, 3, 15)])

    def test_a_phone_like_group_is_not_a_date(self):
        self.assertEqual(find_dates("Your bill number is 98 76 54."), [])

    def test_a_day_and_month_with_no_year_is_not_a_date(self):
        self.assertEqual(find_dates("बिलकुल, 15 मार्च को आइए।"), [])

    def test_a_date_inside_a_longer_number_is_not_read(self):
        self.assertEqual(find_dates("Reference 1115/03/20251"), [])

    def test_a_time_is_not_read_as_a_year(self):
        # Ruling W-5: "15 March 14:30" is not 15 March 2014. Ruling W-25: nor
        # is a dotted time, 10h30, an hour with am or pm, baje or बजे, or hrs.
        for text in ("Collection on 15 March 14:30", "15 March, 10:30 am", "15 मार्च 10:30 बजे", "15 03 14:30",
                     "15.03.14:30", "10 October, 11 am", "10 October 10.30 am", "10 अक्टूबर 11 बजे",
                     "15 March 10 baje", "15 March 10h30", "15 March 1030 hrs", "15 March 11 hr", "15 March 11am",
                     "15 March 11 a.m.", "15 March 09 P.M.", "10 10 11 pm",
                     # Ruling W-26.
                     "Pickup on 10 October 11 o'clock", "Pickup 10 October 11 se 1 baje tak",
                     "पिकअप 10 अक्टूबर 11 से 1 बजे तक", "Pickup on 10 October, 11-1 pm", "Pickup on 10 October 11 a. m.",
                     "Pickup on 10 October 11 to 1 pm",
                     # Rulings W-28 and W-29: a range starts at an hour on a
                     # 12-hour clock, 01 to 12, so these still read no year.
                     "Pickup on 10 October 11 – 1 pm", "Pickup 10 October 10 se 12 baje", "Pickup 10 Oct, 9 to 11 am",
                     "Pickup 10 October 12 - 2 pm", "Pickup 10 October 01 - 03 pm",
                     # One digit is never a year.
                     "Pickup 10 October 9 bikes"):
            with self.subTest(text=text):
                self.assertEqual(find_dates(text), [])
        # Rulings W-28 and W-29: a two-digit year that cannot be a 12-hour
        # hour, then a range to a time, is a date and a time (Tally prints
        # "15-Mar-25 - 04:35 PM"). EMotorad has sold bikes only since 2020.
        for text, day in (("15 Mar 23 - 4:35 PM", date(2023, 3, 15)),
                          ("15 March 23 se 5 baje", date(2023, 3, 15)),
                          ("15 मार्च 23 से 5 बजे", date(2023, 3, 15)),
                          ("2 Oct 20 - 11:05 AM", date(2020, 10, 2)),
                          ("15-Mar-22 - 04:35 PM", date(2022, 3, 15)),
                          ("Invoice dated 15-Mar-25 - 04:35 PM", date(2025, 3, 15)),
                          ("dated 15 Mar 25 - 4:35 pm", date(2025, 3, 15)),
                          ("15 March 25 to 5 pm", date(2025, 3, 15)),
                          ("15 March 25 se 5 baje", date(2025, 3, 15)),
                          ("15 मार्च 25 से 5 बजे", date(2025, 3, 15)),
                          ("15 Mar '25 - 4:35 PM", date(2025, 3, 15)),
                          ("2 Oct 26 - 11:05 AM", date(2026, 10, 2)),
                          ("Bill dated 15 Mar 24 - 4:35 PM", date(2024, 3, 15)),
                          ("१५ मार्च २५ से ५ बजे", date(2025, 3, 15))):
            with self.subTest(text=text):
                self.assertEqual([found.date for found in find_dates(text)], [day])
        for text in ("Collection on 15 March 2025 14:30", "15/03/25 10:30", "15 Mar 25, 10:30 am", "15.03.25",
                     "on 15 March 2025: we received", "15 March 25 amps", "15 March 2025, 11 am",
                     # Ruling W-27: a full date then a time, after a dash, "to"
                     # or "se": the range rule follows a two-digit year only.
                     "Invoice date: 15/03/2025 - 04:35 PM", "Invoice date: 15-03-2025 - 16:35 hrs",
                     "Bill dated 15.03.2025 – 10:30 AM", "Your invoice is dated 15 March 2025 - 4 pm.",
                     "Your invoice is dated 15 March 2025 to 5 pm.", "Aapka bill 15 March 2025 se 5 baje ka hai.",
                     "आपका बिल 15 मार्च 2025 से 5 बजे का है।", "Invoice date: 15/03/25 - 04:35 PM"):
            with self.subTest(text=text):
                self.assertEqual([found.date for found in find_dates(text)], [date(2025, 3, 15)])

    def test_a_range_is_refused_only_after_a_year_that_could_be_a_12_hour_hour(self):
        # Ruling W-29: 01 to 12 may start a range to a time; 00 and 13 to 99
        # may not.
        for hour in ("01", "09", "10", "11", "12"):
            with self.subTest(hour=hour):
                self.assertTrue(re.fullmatch(invoice_dates._CLOCK_HOUR, hour))
        for year in ("00", "13", "18", "20", "22", "23", "24", "25", "99", "1", "9", "123"):
            with self.subTest(year=year):
                self.assertFalse(re.fullmatch(invoice_dates._CLOCK_HOUR, year))

    def test_a_24_hour_range_after_a_month_name_reads_as_a_date(self):
        # Ruling W-29, accepted: a range on a 24-hour clock straight after a
        # month name reads its first hour as a year. It errs towards
        # blocking: the date is judged, never let through unread.
        for text, day in (("15 March 18 se 20 baje", date(2018, 3, 15)),
                          ("Pickup 10 October 18 - 20 hrs", date(2018, 10, 10)),
                          ("Pickup 10 October 23 - 1 am", date(2023, 10, 10))):
            with self.subTest(text=text):
                self.assertEqual([found.date for found in find_dates(text)], [day])

    def test_the_date_shapes_use_no_bare_word_boundary_or_word_class(self):
        for pattern in (invoice_dates._SHAPES.pattern, invoice_dates._CLOCK_HOUR):
            self.assertNotIn("\\b", pattern)
            self.assertNotIn("\\w", pattern)

    def test_redaction_replaces_every_date(self):
        text = redact_dates("Your invoice is dated 15.03.2025. Bought on १८ मार्च २०२५.")
        self.assertEqual(text, "your invoice is dated [date]. bought on [date].")

    def test_redaction_also_hides_a_month_first_date_and_a_month_and_year(self):
        # Ruling W-25: for a log only. find_dates still reads neither (a
        # month-year form is an Edge Case Register CAPTURE row).
        self.assertEqual(redact_dates("Your bill says 03/15/2025 and 15/03/2025."),
                         "your bill says [date] and [date].")
        for text, redacted in (("Your invoice is from March 2025.", "your invoice is from [date]."),
                               ("आपका बिल मार्च 2025 का है।", "आपका बिल [date] का है।"),
                               ("Bought in Sept, 2025 or 03/2025", "bought in [date] or [date]")):
            with self.subTest(text=text):
                self.assertEqual(redact_dates(text), redacted)
                self.assertEqual(find_dates(text), [])
        self.assertEqual(redact_dates("Your bill number is 98 76 54."), "your bill number is 98 76 54.")

    def test_redaction_also_hides_a_short_year_a_year_and_month_and_a_day_with_no_year(self):
        # Ruling W-26: for a log only; find_dates reads none of these.
        for text, redacted in (("Your invoice is from Mar '25.", "your invoice is from [date]."),
                               ("Your invoice is from March 25.", "your invoice is from [date]."),
                               ("Your invoice is from 03/25.", "your invoice is from [date]."),
                               ("Your invoice is from 2025-03.", "your invoice is from [date]."),
                               ("Your invoice is dated 15 March.", "your invoice is dated [date]."),
                               ("Your invoice is dated March 15.", "your invoice is dated [date]."),
                               ("Your invoice is dated 15th of March.", "your invoice is dated [date]."),
                               ("Your invoice is dated 15/03.", "your invoice is dated [date]."),
                               ("आपका बिल 15 मार्च का है।", "आपका बिल [date] का है।")):
            with self.subTest(text=text):
                self.assertEqual(redact_dates(text), redacted)
                self.assertEqual(find_dates(text), [])
        for text in ("Charge it for 3-4 hours.", "Your bill number is 98 76 54.", "Order RO-1234 is placed."):
            with self.subTest(text=text):
                self.assertNotIn("[date]", redact_dates(text))


if __name__ == "__main__":
    unittest.main()
