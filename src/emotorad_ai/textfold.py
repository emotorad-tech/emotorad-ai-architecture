"""One folded copy of a text, for the claim shapes and the date reader.

Spec: docs/superpowers/specs/2026-10-06-warranty-status-design.md, 3.4.

Every coverage pattern (guardrails.py) and every date reading
(invoice_dates.py) runs on the same fold, so a match's span means the same
thing to all of them. NFC turns a precomposed nukta letter (U+0958 to U+095F)
into the letter and U+093C; dropping U+093C then makes फ़्री and फ्री, ख़त्म
and खत्म the same. The typographic apostrophes (U+2018, U+2019, U+02BC,
U+FF07) become "'", one character for one, so "you’ll" reads as "you'll" to
every contraction shape. Whitespace collapses to one space and Latin letters
go to lower case. Nothing here imports from the package.
"""

from __future__ import annotations

import unicodedata

NUKTA = "़"
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "＇": "'"})


def fold(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").replace(NUKTA, "").translate(_APOSTROPHES)
    return " ".join(text.split()).lower()
