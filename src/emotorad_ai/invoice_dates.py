"""Dates as invoices and people write them, read day-first.

Spec: docs/superpowers/specs/2026-10-06-warranty-status-design.md, 10.5.
Pure. Used by the coverage post-check's date rule (guardrails.check_dates)
in Part 1, and by the invoice reader in Part 2, so both read a date the same
way.

Every text is read after `digits.ascii_digits` (१५ is 15) and the fold of
`textfold.fold` (lower case, one space, no nukta). Number edges are
`(?<![0-9])` and `(?![0-9])`, letter edges are explicit classes: never a
bare word boundary or word class, which miss Devanagari.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple

from .digits import ascii_digits
from .textfold import fold

UNPARSED = "unparsed"
INVALID_DATE = "invalid_date"
NOT_DAY_FIRST = "not_day_first"


@dataclass(frozen=True)
class ParsedDate:
    date: Optional[date]
    ambiguous: bool = False
    other_reading: Optional[date] = None
    reason: Optional[str] = None


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    # Hindi, as the fold leaves them (no nukta).
    "जनवरी": 1, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "अप्रेल": 4, "मई": 5, "जून": 6, "जुलाई": 7,
    "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11,
    "दिसंबर": 12, "दिसम्बर": 12,
}
_MONTH = "(?:%s)" % "|".join(re.escape(name) for name in sorted(_MONTHS, key=len, reverse=True))
# Latin letters and Devanagari letters and signs (U+0900 to U+0963, U+0966 to
# U+097F; not the danda).
_LETTER = "a-zऀ-ॣ०-ॿ"
_NAME_START, _NAME_END = "(?<![%s])" % _LETTER, "(?![%s])" % _LETTER
_ORDINAL = "(?:st|nd|rd|th)?"

# Year first; day, month name, year; month name, day, year; day, month,
# year with the same separator twice. Tried in that order at each position.
# A year is never followed by what makes it a time of day: in "15 March 14:30"
# the 14 is an hour, not 2014 (ruling W-5). Nor in 10.30, 10h30, "11 am",
# "11 a.m.", "11 baje", "11 बजे" or "1030 hrs" (ruling W-25), "11 a. m." or
# "11 o'clock" (ruling W-26).
_TIME_WORD = (r"(?:[ap]\.?\s?m(?![a-z])|baje(?![a-z])|बजे(?![%s])|hrs?(?![a-z])|o'?\s?clock(?![a-z]))"
              % _LETTER)
_NOT_A_TIME = r"(?![:.h][0-9]|\s?" + _TIME_WORD + r")"
# Nor is a two-digit "year" after a month name that starts a range to a time:
# "10 October, 11-1 pm", "11 se 1 baje", "11 से 1 बजे" (ruling W-26). Only a
# two-digit one: "15 March 2025 - 4 pm" is a date and a time (ruling W-27).
# And only one that could be an hour: "15-Mar-25 - 04:35 PM", as Tally prints
# an invoice date, is 15 March 2025 at 4:35 pm (ruling W-28). A range is
# spoken on a 12-hour clock ("11 se 1 baje", "9 to 11 am"), and EMotorad has
# sold bikes only since 2020, so the hour is 01 to 12 and "15 Mar 23 - 4:35
# PM" is a 2023 date (ruling W-29). A 24-hour range straight after a month
# name ("15 March 18 se 20 baje") reads as a date: it is judged, so it blocks.
_CLOCK_HOUR = r"(?:0[1-9]|1[0-2])"
_NOT_A_RANGE = r"(?!\s?(?:-|–|to|se|से)\s?[0-9]{1,2}(?:[:.][0-9]{2})?\s?" + _TIME_WORD + r")"
# Between a day and a month name: "15th of March", "15 tarikh March",
# "15 तारीख, मार्च" (ruling W-26).
_DAY_OF = r"(?:(?:of|tarikh|tareekh|tarik|तारीख)[ .,/–-]+)?"
# Separators take the en dash as well as the hyphen (ruling W-26).
_SHAPES = re.compile(
    r"(?<![0-9])(?P<y1>[0-9]{4})(?P<s1>[./–-])(?P<m1>[0-9]{1,2})(?P=s1)(?P<d1>[0-9]{1,2})(?![0-9])"
    r"|(?<![0-9])(?P<d2>[0-9]{1,2})" + _ORDINAL + r"[ .,/–-]*" + _DAY_OF + _NAME_START + r"(?P<n2>" + _MONTH
    + r")" + _NAME_END + r"[ .,/'–-]*(?P<y2>[0-9]{4}|" + _CLOCK_HOUR + r"(?![0-9])" + _NOT_A_RANGE
    + r"|(?!" + _CLOCK_HOUR + r")[0-9]{2}(?![0-9]))(?![0-9])"
    + _NOT_A_TIME
    + r"|" + _NAME_START + r"(?P<n3>" + _MONTH + r")" + _NAME_END + r"[ .–-]*(?:the )?(?P<d3>[0-9]{1,2})"
    + _ORDINAL + r"[ ,]+(?P<y3>[0-9]{4})(?![0-9])"
    r"|(?<![0-9])(?P<d4>[0-9]{1,2})(?P<s4>[./ –-])(?P<m4>[0-9]{1,2})(?P=s4)(?P<y4>[0-9]{4}|[0-9]{2})(?![0-9])"
    + _NOT_A_TIME
)
# For a log only (redact_dates): part of a date. find_dates reads none of
# them as a day (a month and a year is an Edge Case Register CAPTURE row), but
# the log never holds one (rulings W-25, W-26): a month and a year (March
# 2025, Mar '25, March 25, मार्च 2025), a day and a month (15 March, 15th of
# March, March the 15th), and in figures 2025-03, 03/2025, 03/25 and 15/03.
_PARTIAL_DATE = re.compile(
    _NAME_START + _MONTH + _NAME_END + r"[ .,'/–-]*(?:[0-9]{4}|[0-9]{2})(?![0-9])"
    r"|(?<![0-9])[0-9]{1,2}" + _ORDINAL + r"[ .,/–-]*" + _DAY_OF + _NAME_START + _MONTH + _NAME_END
    + r"|" + _NAME_START + _MONTH + _NAME_END + r"[ .–-]*(?:the )?[0-9]{1,2}(?![0-9])"
    r"|(?<![0-9])(?:19|20)[0-9]{2}[/.–-](?:0?[1-9]|1[0-2])(?![0-9])"
    r"|(?<![0-9])(?:0?[1-9]|1[0-2])[/–-](?:19|20)[0-9]{2}(?![0-9])"
    r"|(?<![0-9])[0-9]{1,2}/[0-9]{1,2}(?![0-9])"
)
_TIME_AT_END = re.compile(r"\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?:\s*[ap]\.?m\.?)?$")
_EDGE_PUNCTUATION = " ,.:;()"


def _prepare(text: str) -> str:
    return fold(ascii_digits(text or ""))


def _year(text: str) -> int:
    return 2000 + int(text) if len(text) == 2 else int(text)


def _real(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _read(match: "re.Match[str]") -> ParsedDate:
    if match.group("y1"):
        found = _real(int(match.group("y1")), int(match.group("m1")), int(match.group("d1")))
        return ParsedDate(found) if found else ParsedDate(None, reason=INVALID_DATE)
    if match.group("n2"):
        found = _real(_year(match.group("y2")), _MONTHS[match.group("n2")], int(match.group("d2")))
        return ParsedDate(found) if found else ParsedDate(None, reason=INVALID_DATE)
    if match.group("n3"):
        found = _real(int(match.group("y3")), _MONTHS[match.group("n3")], int(match.group("d3")))
        return ParsedDate(found) if found else ParsedDate(None, reason=INVALID_DATE)
    day, month, year = int(match.group("d4")), int(match.group("m4")), _year(match.group("y4"))
    found = _real(year, month, day)
    if found is None:
        if _real(year, day, month) is not None:
            return ParsedDate(None, reason=NOT_DAY_FIRST)
        return ParsedDate(None, reason=INVALID_DATE)
    if day <= 12 and month <= 12 and day != month:
        return ParsedDate(found, ambiguous=True, other_reading=date(year, day, month))
    return ParsedDate(found)


def parse_printed_date(text: str) -> ParsedDate:
    """The whole of `text` as one date, read day-first (spec 10.5)."""
    cleaned = _TIME_AT_END.sub("", _prepare(text).strip(_EDGE_PUNCTUATION))
    match = _SHAPES.fullmatch(cleaned.strip(_EDGE_PUNCTUATION))
    if match is None:
        return ParsedDate(None, reason=UNPARSED)
    return _read(match)


_MONTH_FIRST_SEPARATORS = ("/", "-", "–")


def _scan(text: str) -> Tuple[str, List["re.Match[str]"]]:
    prepared = _prepare(text)
    return prepared, list(_SHAPES.finditer(prepared))


def _found(match: "re.Match[str]") -> ParsedDate:
    """A match as find_dates reads it: as the invoice reader does, except
    that a month-first date that cannot be day-first (03/15/2025: no 15th
    month) is read month-first (ruling W-26), when its separator is a slash
    or a dash: "12 13 25" is a phone number and "2.14.10" a version (ruling
    W-27)."""
    parsed = _read(match)
    if parsed.reason != NOT_DAY_FIRST or match.group("s4") not in _MONTH_FIRST_SEPARATORS:
        return parsed
    return ParsedDate(date(_year(match.group("y4")), int(match.group("d4")), int(match.group("m4"))))


def find_dates(text: str) -> List[ParsedDate]:
    """Every real calendar date in `text` (reason None), in order. An
    ambiguous one carries its other reading."""
    return [parsed for parsed in map(_found, _scan(text)[1]) if parsed.reason is None]


def redact_dates(text: str) -> str:
    """`text` folded, with every date `find_dates` reads replaced by [date]:
    for a log or a reply's metadata that must never hold a date the model may
    have read off an invoice (spec 3.6, 10.12). It also hides part of a date,
    which find_dates does not read as a day (_PARTIAL_DATE; rulings W-25,
    W-26)."""
    prepared, matches = _scan(text)
    for match in reversed(matches):
        # A month-first date find_dates leaves (03.15.2025) is still hidden.
        if _found(match).reason in (None, NOT_DAY_FIRST):
            prepared = prepared[:match.start()] + "[date]" + prepared[match.end():]
    return _PARTIAL_DATE.sub("[date]", prepared)
