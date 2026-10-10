"""Times in the ticket store.

Every time the store holds is an ISO-8601 UTC string with microseconds,
"2026-10-05T10:00:00.000000+00:00", so the store compares and sorts them as
strings, in MongoDB and in memory alike. One format, from one place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def iso(dt: datetime) -> str:
    """A datetime as the store writes it. A naive one is taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def parse(text: str) -> datetime:
    """A stored time back as an aware datetime. A naive one is taken as UTC."""
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def plus(at: str, seconds: float) -> str:
    """`at` moved by `seconds` (negative goes back), in the store's format."""
    return iso(parse(at) + timedelta(seconds=seconds))
