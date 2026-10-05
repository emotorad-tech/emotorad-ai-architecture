"""Caps on unverified tickets that are not urgent (spec 2026-10-05, section 6).

A number nobody proved can be typed by anyone, so the tickets it raises are
capped: at most two per number and fifty in all, per calendar day in IST.
Urgent tickets (a safety report) are never capped. This is the one helper
every path that records an unverified ticket uses: the handover, the call-back
number and the lock-out (runtime.Runtime._record_ticket), and the intake tool
(tools/mocks.py).
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any, Optional

from ..guardrails import CAP_OVERALL_MESSAGE, CAP_PER_NUMBER_MESSAGE
from .clock import iso, parse

PER_NUMBER = 2
OVERALL = 50
PER_NUMBER_CAP = "per_number"
OVERALL_CAP = "overall"
# What the customer is told for each cap.
CAP_TEXTS = {PER_NUMBER_CAP: CAP_PER_NUMBER_MESSAGE, OVERALL_CAP: CAP_OVERALL_MESSAGE}

IST = timezone(timedelta(hours=5, minutes=30))


def ist_day_start(now: str) -> str:
    """The start of `now`'s calendar day in IST, as a UTC time in the ticket
    store's format (tickets.clock.now_iso), so it compares as a string."""
    start = parse(now).astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return iso(start)


def cap_reached(store: Any, *, phone: Optional[str], source_key: str, now: str) -> Optional[str]:
    """Which cap a new unverified, non-urgent ticket would break, or None.

    No store (Zoho off) means no cap. A source key already recorded is a
    retry, not a new ticket, so it is never capped and gets the record it
    already has. The caller decides what is unverified and not urgent. Raises
    StoreUnavailable when the store cannot answer."""
    if store is None or store.by_source_key(source_key) is not None:
        return None
    since = ist_day_start(now)
    if phone and store.unverified_since(since, phone=phone) >= PER_NUMBER:
        return PER_NUMBER_CAP
    if store.unverified_since(since) >= OVERALL:
        return OVERALL_CAP
    return None
