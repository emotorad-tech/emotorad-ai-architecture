"""The date post-check: a customer agent's reply may state only dates it was
given (ported from feat/warranty-status, spec 2026-10-06 section 3.5, rulings
W-25 to W-29; adapted on 8 October 2026 to this branch's records, spec
2026-10-08 section 3).

The model is never given a date read off an invoice, but it can see an
invoice the customer sends, and it can work an end date out itself. So a
date in its reply must be one it was given: a date the warranty record holds
(the purchase date, the start, the end and each part's end), a date the
customer typed, the invoice date code told the customer (invoice_ocr.py) and
the ends worked out from it, and, where the rule allows, a date in a tool
result. The word lists, the sentence reading and the five rules are the
paused branch's, verbatim; what changed is where the allowed dates come from
(this branch's lookup records carry `purchase_date`, `warranty_start`,
`warranty_end` and `components[].valid_until`, not that branch's resolver
entries).

Tier-1 (CLAUDE.md, danger zones): human review before merge. Never use a bare
word boundary or word class here: the edges are explicit, so Devanagari is
read.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from typing import Any, Iterable, List, Mapping, Sequence, Tuple

from . import warranty_terms
from .guardrails import CoverageCheck
from .invoice_dates import find_dates, redact_dates
from .textfold import fold as _fold

# The reasons a reply is blocked for a date; its logged text has every date
# replaced (redacted).
DATE_REASONS = ("invoice_date_by_model", "warranty_date_not_from_result")

_DEV = "ऀ-ॣ०-ॿ"
_HI_START, _HI_END = "(?<![%s])" % _DEV, "(?![%s])" % _DEV
_LAT_START, _LAT_END = "(?<![a-z])", "(?![a-z])"
_WARRANTY_HI = "(?:वारंटी|वॉरंटी|वारन्टी|गारंटी)"
_WARRANTY_LAT = "(?:warranty|warrenty|waranty|guarantee)"


# Purchase words, matched on a folded sentence (spec 3.5). W1's
# evidence_asks.asks_for_invoice reads them for an ask for the invoice.
_PURCHASE_WORD = re.compile(
    r"(?<![a-z])(?:invoices?|bills?|receipts?|purchased|bought|purchase\s+date|date\s+of\s+purchase"
    r"|kharid[a-z]*|kharee?d[a-z]*)(?![a-z])"
    r"|" + _HI_START + r"(?:इनवॉइस|इनवॉयस|इन्वॉइस|इनवाइस|बिल|रसीद|चालान|खरीद|खरीदा|खरीदी|खरीदे)" + _HI_END
)
# What a model says of a purchase when it names no document: "you were
# invoiced on", "purchase kiya tha", "लिया था" (ruling W-25). Rule 1 only:
# none is a word for the invoice, so evidence_asks does not read them.
_PURCHASE_SAID = re.compile(
    _LAT_START + r"(?:invoiced|billed|your\s+purchase|the\s+document\s+you\s+sent\s+shows"
    r"|purchase\s+(?:kiya|kiye)|li\s+gay?i\s+thi|liya\s+tha|li\s+thi)" + _LAT_END
    + r"|" + _HI_START + r"(?:खरीदारी|लिया\s+था|ली\s+थी|ली\s+ग(?:ई|यी)\s+थी)" + _HI_END
)
# Warranty words: 3.4's, and cover, covered and coverage (ruling W-25). Not a
# cover on the bike: a battery, seat, rain or port cover, or one that is
# something's ("ka cover", "का कवर") (ruling W-26). Each casing word is a
# whole word, so "aapka cover", "आपका कवर", "bike cover" and "support cover"
# keep theirs (ruling W-27).
_CASING_LAT = "".join("(?<!(?<![a-z])%s )" % word
                      for word in ("ka", "ki", "ke", "battery", "seat", "rain", "port"))
_CASING_HI = "".join("(?<!(?<![%s])%s )" % (_DEV, word)
                     for word in ("का", "की", "के", "बैटरी", "सीट", "रेन", "पोर्ट"))
_WARRANTY_WORD = re.compile(
    _LAT_START + r"(?:" + _WARRANTY_LAT + r"|cover(?:ed|age)|" + _CASING_LAT + r"cover)" + _LAT_END
    + "|" + _HI_START + r"(?:" + _WARRANTY_HI + r"|कवर्ड|कवरेज|" + _CASING_HI + r"कवर)" + _HI_END
)
# An invoice, bill or receipt word, in any language, or wording that reads a
# photo or a document the customer sent: anywhere in a reply, either brings
# the whole-reply rule (ruling W-26). Not a bare "photo": asking for one is
# not reading one.
_INVOICE_WORD = re.compile(
    _LAT_START + r"(?:invoices?|invoiced|inv|bills?|billed|receipts?|challans?|raseed|rasid|parchi"
    r"|cash\s+memos?)" + _LAT_END
    + r"|" + _HI_START + r"(?:इनवॉइस|इनवॉयस|इन्वॉइस|इनवाइस|इनवोइस|इन्वोयस|बिल|रसीद|चालान|पर्ची)" + _HI_END
)
# Ruling W-27 adds "the photo you just sent / you've sent shows", "looking at
# your photo", "in the picture", "I can see the date", "it reads" and
# "photo mein ... dikh", "फोटो में ... दिख". Ruling W-28 adds "in your photo",
# "in this image" ("the date in your photo"), "the photo that you sent" and
# "photograph".
_PHOTO_NOUN = r"(?:photo(?:graph)?|image|picture|pic|pdf|document|screenshot|scan|attachment|file)"
_PHOTO_READ = re.compile(
    _LAT_START + _PHOTO_NOUN + r"s?\s+"
    r"(?:(?:that\s+)?you(?:\s+just|\s+have|'ve)?\s+(?:just\s+)?(?:sent|shared|uploaded|attached)"
    r"|(?:clearly\s+)?(?:shows?|says|reads|mentions|is\s+dated))" + _LAT_END
    + r"|" + _LAT_START + r"(?:i\s+(?:can|could)\s+(?:read|make\s+out)|what\s+i\s+(?:can\s+|could\s+)?read"
    r"|i\s+can\s+see\s+the\s+date|it\s+reads|looking\s+at\s+(?:your|the|this)\s+" + _PHOTO_NOUN + r"s?"
    r"|in\s+(?:the|your|this)\s+" + _PHOTO_NOUN + r")" + _LAT_END
    + r"|" + _LAT_START + r"(?:photo|foto|image|pic|tasveer|pdf)\s+(?:me|mein|mai|main|par|pe)\s+"
    r"(?:\S+\s+){0,8}?(?:likh|dikh)"
    + r"|" + _HI_START + r"(?:फोटो|तस्वीर|इमेज|पीडीएफ)\s+(?:में|पर)\s+(?:\S+\s+){0,8}?(?:लिख|दिख)"
)

# A sentence ends at . ! or ? before a space or the end, at a line break and
# at the danda (spec 3.5). Not at a full stop before a digit or a lower-case
# letter ("15.03.2025", "dt. 15.03.2025", "fine. then"), nor at one after an
# abbreviation ("No.", "Inv.", "Pvt. Ltd.") or a list number at the start of
# a line ("1."); and a line ending in a colon runs on into the next ("Invoice
# date:\n15 March 2025") (rulings W-25, W-26). A line break is matched from
# the start of its run of spaces only, so a long run is read once.
_SENTENCE_MARK = re.compile(r"(?P<stop>[.!?]+)(?=\s|$)|[।॥]|(?<![ \t])[ \t]*\n\s*")
# Read on the lower-cased eight characters before the full stop.
_ABBREVIATION = re.compile(
    r"(?<![a-z])(?:no|dt|sept|mar|inv|rs|pvt|ltd|co|dr|mr|mrs|smt|st|rd|approx|etc|vs|ph|mob|qty|amt|s/o|d/o)$"
)
# Read on the eight characters before the full stop, with a line break put
# in front when they start the reply.
_LIST_NUMBER = re.compile(r"\n[ \t]*[0-9]{1,3}$")
_CONTINUES = re.compile(r"\s+[0-9०-९a-z]")


# A bike only the app knows (tools/amigo.merged_source) has no OMS date.
_APP_ONLY = ("not_registered", "warranty_unknown", "warranty_unavailable")
# Statuses with no OMS date behind them: an invoice may have been read, or
# may be on screen, the number has no record, the OMS did not answer, or the
# bike is only in the app (rule 2; rulings W-25, W-26).
_NO_OMS_DATE = ("purchase_date_missing", "invoice_date", "no_warranty_record", "oms_unavailable") + _APP_ONLY
# A look-up that failed this way says no OMS date stands behind the reply.
_NO_OMS_ANSWER = ("no_warranty_record", "oms_unavailable")
# Ruling W-26: a warranty term is a whole number of half years.
_TERM_STEP_MONTHS, _TERM_MAX_MONTHS = 6, 120


def _sentences(reply: str) -> List[str]:
    """The reply's sentences, in order, as written (not folded)."""
    sentences, start = [], 0
    for mark in _SENTENCE_MARK.finditer(reply):
        stop = mark.group("stop")
        if stop is not None:
            if set(stop) == {"."} and _runs_on(reply, mark.start(), mark.end()):
                continue
        elif "\n" in mark.group() and reply.endswith(":", 0, mark.start()):
            continue
        sentences.append(reply[start:mark.start()])
        start = mark.end()
    sentences.append(reply[start:])
    return [sentence for sentence in sentences if sentence.strip()]


def _runs_on(reply: str, start: int, end: int) -> bool:
    """Whether the full stop at `start` leaves the sentence running on: a
    digit or a lower-case letter follows it, it ends an abbreviation, or it
    follows a list number at the start of a line."""
    if _CONTINUES.match(reply, end):
        return True
    before = reply[max(0, start - 8):start]
    if _ABBREVIATION.search(before.lower()):
        return True
    return bool(_LIST_NUMBER.search(before if start > 8 else "\n" + before))


def _dated_sentences(reply: str, found: Sequence[Any]) -> List[Tuple[str, Sequence[Any]]]:
    """For each sentence holding a date: that sentence and the one before
    it, folded, with the sentence's own dates (ruling W-25). If reading
    sentence by sentence does not find the same dates as reading the whole
    reply (a line break inside a date cuts it, and may make another), the
    whole reply is one sentence (ruling W-26)."""
    sentences = _sentences(reply)
    own = [find_dates(sentence) for sentence in sentences]
    if Counter(day for dates in own for day in dates) != Counter(found):
        return [(_fold(reply), found)]
    return [(_fold(" ".join(sentences[max(0, index - 1):index + 1])), dates)
            for index, dates in enumerate(own) if dates]


# --- where the allowed dates come from (adapted to this branch) --------------

# The record's own dates, on a bike whose cover was worked out.
_RECORD_DATE_KEYS = ("purchase_date", "warranty_start", "warranty_end")


def _days(text: str) -> set:
    days = set()
    for found in find_dates(text):
        days.add(found.date)
        if found.ambiguous:
            days.add(found.other_reading)
    return days


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _days_in(value: Any) -> set:
    days = set()
    for text in _strings(value):
        days |= _days(text)
    return days


def _bikes(evidence: Sequence[Any]) -> List[Mapping[str, Any]]:
    """Every bike a lookup in the evidence returned."""
    bikes: List[Mapping[str, Any]] = []
    for result in evidence:
        data = result.get("data") if isinstance(result, Mapping) else None
        for bike in (data or {}).get("bikes") or [] if isinstance(data, Mapping) else []:
            if isinstance(bike, Mapping):
                bikes.append(bike)
    return bikes


def _record_days(bikes: Iterable[Mapping[str, Any]]) -> set:
    """The dates the warranty record holds for bikes with a purchase date:
    the purchase date, the start, the end and each part's end."""
    days = set()
    for bike in bikes:
        if bike.get("coverage_status") in _NO_OMS_DATE or not bike.get("purchase_date"):
            continue
        for key in _RECORD_DATE_KEYS:
            if bike.get(key):
                days |= _days(str(bike[key]))
        for part in bike.get("components") or []:
            if isinstance(part, Mapping) and part.get("valid_until"):
                days |= _days(str(part["valid_until"]))
    return days


def _purchase_days(bikes: Iterable[Mapping[str, Any]]) -> set:
    days = set()
    for bike in bikes:
        if bike.get("coverage_status") not in _NO_OMS_DATE and bike.get("purchase_date"):
            days |= _days(str(bike["purchase_date"]))
    return days


def _worked_out_ends(bikes: Iterable[Mapping[str, Any]]) -> set:
    """The end dates a model could work out from a purchase date: the date
    plus 6, 12, 18 ... 120 months, and the day either side (ruling W-26)."""
    days = set()
    for bought in _purchase_days(bikes):
        for months in range(_TERM_STEP_MONTHS, _TERM_MAX_MONTHS + 1, _TERM_STEP_MONTHS):
            end = warranty_terms.valid_until(bought, months) + timedelta(days=1)
            days |= {end - timedelta(days=1), end, end + timedelta(days=1)}
    return days


def _tool_days(evidence: Sequence[Any], arguments: Sequence[Any]) -> set:
    """Every date in the tool results, less, for each result, any date the
    model itself passed in that call's arguments: a tool that echoes what it
    was given vouches for nothing (ruling W-25)."""
    days = set()
    for index, result in enumerate(evidence):
        given = _days_in(arguments[index]) if index < len(arguments) else set()
        days |= _days_in(result) - given
    return days


def _no_oms_answer(evidence: Sequence[Any]) -> bool:
    return any(isinstance(result, Mapping) and isinstance(result.get("error"), Mapping)
               and result["error"].get("code") in _NO_OMS_ANSWER for result in evidence)


def _allowed(found: Any, days: set) -> bool:
    """An ambiguous date counts under both readings."""
    return found.date in days or (found.ambiguous and found.other_reading in days)


def invoice_days(purchase_date: date) -> set:
    """The dates code told the customer from a confident invoice reading
    (invoice_ocr.customer_line): the invoice date and each part's end."""
    days = {purchase_date}
    for months in warranty_terms.TERMS.values():
        days.add(warranty_terms.valid_until(purchase_date, months))
    return days


def redacted(text: str) -> str:
    """`text` with every date hidden, for the log of a blocked reply; as it
    was written when it holds no date (hiding one folds the text)."""
    hidden = redact_dates(text or "")
    return hidden if "[date]" in hidden else (text or "")


def check_dates(reply: str, evidence: Sequence[Any], customer_texts: Sequence[str], *,
                arguments: Sequence[Any] = (), known_bikes: Sequence[Mapping[str, Any]] = (),
                no_oms_date: bool = False, invoice_days: Iterable[date] = ()) -> CoverageCheck:
    """Block a reply that states a date the model was not given. The first
    rule that applies decides (the paused branch's rules):

    1. a date whose sentence, or the sentence before it, holds a purchase
       word: only record, customer and told-invoice days;
    1a. an invoice, bill or receipt word, or wording that reads a photo,
       anywhere in the reply, and any date in it: those and tool days;
    2. any bike without a purchase date, a look-up with no record or no
       answer, or `no_oms_date`, and any date in the reply: the same;
    2a. a date within a day of a purchase date plus a whole number of half
       years, unless given: an end date the model worked out;
    3. a date whose sentence, or the sentence before it, holds a warranty
       word: only record, customer and told-invoice days.
    """
    found = find_dates(reply or "")
    if not found:
        return CoverageCheck(blocked=False)
    bikes = _bikes(evidence) + [bike for bike in known_bikes if isinstance(bike, Mapping)]
    said_by = _record_days(bikes) | set(invoice_days)
    for text in customer_texts:
        said_by |= _days(text)
    everywhere = said_by | _tool_days(evidence, arguments)
    dated = _dated_sentences(reply, found)
    whole = _fold(reply)

    for words, dates in dated:
        if ((_PURCHASE_WORD.search(words) or _PURCHASE_SAID.search(words))
                and not all(_allowed(day, said_by) for day in dates)):
            return CoverageCheck(blocked=True, reason="invoice_date_by_model", claimed="invoice_date")
    if ((_INVOICE_WORD.search(whole) or _PHOTO_READ.search(whole))
            and not all(_allowed(day, everywhere) for day in found)):
        return CoverageCheck(blocked=True, reason="invoice_date_by_model", claimed="invoice_date")
    if (no_oms_date or _no_oms_answer(evidence)
            or any(bike.get("coverage_status") in _NO_OMS_DATE for bike in bikes)):
        if not all(_allowed(day, everywhere) for day in found):
            return CoverageCheck(blocked=True, reason="invoice_date_by_model", claimed="invoice_date")
    ends = _worked_out_ends(bikes)
    for day in found:
        worked_out = day.date in ends or (day.ambiguous and day.other_reading in ends)
        if worked_out and not _allowed(day, everywhere):
            return CoverageCheck(blocked=True, reason="warranty_date_not_from_result", claimed="warranty_date")
    for words, dates in dated:
        if _WARRANTY_WORD.search(words) and not all(_allowed(day, said_by) for day in dates):
            return CoverageCheck(blocked=True, reason="warranty_date_not_from_result", claimed="warranty_date")
    return CoverageCheck(blocked=False)
