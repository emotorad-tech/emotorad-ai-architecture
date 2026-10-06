"""Pin mongomock's TTL clock to the clock a test's stores run on.

mongomock removes a document from a collection with a TTL index whenever that
collection is read and `mongomock.utcnow()` has reached the document's
`expires_at`. Left alone, `mongomock.utcnow()` is the real clock. A store that
runs on a fixed date (`now=lambda: NOW`) writes `expires_at` from that date, so
once the real date passes NOW plus the TTL the document vanishes before the
test reads it back. That broke the suite at 10:00 UTC on 6 October 2026, seven
days (the receipt TTL) after 29 September 10:00. Worse, a test that expects a
document to be gone, or a fresh claim to succeed, then passes without testing
anything.

A real MongoDB's TTL monitor runs on the server's clock, the same clock the
store runs on, so a test pins mongomock to the test's clock:

    _MONGOMOCK_CLOCK = mongomock_clock_at(NOW)

    def setUpModule():
        _MONGOMOCK_CLOCK.start()

    def tearDownModule():
        _MONGOMOCK_CLOCK.stop()

or, for one class, `start()` it in `setUp` and `addCleanup(...stop)`. A store
on the real clock (the default `now`) needs no pin: it and mongomock read the
same clock. tests/test_clock.py checks the pin still governs mongomock's TTL.
"""

from datetime import datetime, timezone
from unittest import mock


def mongomock_clock_at(now: datetime):
    """A patcher (start/stop it, or use it as a context manager) under which
    mongomock's TTL indexes read `now`, a datetime with a zone."""
    if now.tzinfo is None:
        raise ValueError("give the time with its zone, as the stores write it")
    naive_utc = now.astimezone(timezone.utc).replace(tzinfo=None)  # mongomock compares naive UTC
    return mock.patch("mongomock.utcnow", lambda: naive_utc)
