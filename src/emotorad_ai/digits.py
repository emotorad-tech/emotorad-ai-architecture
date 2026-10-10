"""Digits of any script, read as ASCII.

A customer on a Hindi keyboard types ९८७६५४३२१० for 9876543210. Whatever reads
a number (the verify-first step, the log's redaction) runs this first, so a
number is the same number whatever script it was typed in.

Nothing here imports from the package. observability.py uses it, and
verify_first.py imports observability, so it could live in neither without an
import cycle.
"""

from __future__ import annotations

import unicodedata


def ascii_digits(text: str) -> str:
    """Digits of any script (Devanagari ९७००…) as ASCII, everything else as typed."""
    return "".join(
        str(unicodedata.decimal(ch)) if not ch.isascii() and unicodedata.decimal(ch, None) is not None else ch
        for ch in text
    )
