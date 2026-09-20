"""A proved identity has to stop being proof eventually.

`_Pending` carried no timestamp and nothing pruned it, so a code issued today
still verified next week, and a conversation verified once stayed verified for
the life of the process. Both stores also grew forever, because nothing had a
reason to remove an entry.

Two different lifetimes, because they answer different questions. A code is a
secret in transit and should be short. A verified session is how long someone
may keep talking without proving themselves again, and a customer who walks away
from a half-finished battery diagnosis should not have to start over.
"""

import unittest

from emotorad_ai.tools.verification import (
    CODE_TTL_SECONDS,
    VERIFIED_TTL_SECONDS,
    VerificationStore,
)


class _Clock:
    """A hand-wound clock, so expiry is tested by elapsing time rather than by
    sleeping through it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CodeExpiryTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.store = VerificationStore(clock=self.clock)

    def test_a_fresh_code_verifies(self):
        self.store.issue("c1", "+919876500000", "123456")
        self.assertTrue(self.store.check("c1", "123456"))

    def test_a_code_still_verifies_just_inside_its_life(self):
        self.store.issue("c1", "+919876500000", "123456")
        self.clock.advance(CODE_TTL_SECONDS - 1)
        self.assertTrue(self.store.check("c1", "123456"))

    def test_an_expired_code_does_not_verify(self):
        self.store.issue("c1", "+919876500000", "123456")
        self.clock.advance(CODE_TTL_SECONDS + 1)
        self.assertFalse(self.store.check("c1", "123456"))

    def test_an_expired_code_is_not_readable(self):
        """The dev readout must not keep offering a code that no longer works."""
        self.store.issue("c1", "+919876500000", "123456")
        self.clock.advance(CODE_TTL_SECONDS + 1)
        self.assertIsNone(self.store.pending_code("c1"))

    def test_an_expired_code_does_not_burn_an_attempt(self):
        """Otherwise a customer who came back late would be locked out by a code
        that could never have worked."""
        self.store.issue("c1", "+919876500000", "123456")
        self.clock.advance(CODE_TTL_SECONDS + 1)
        self.store.check("c1", "123456")
        self.store.issue("c1", "+919876500000", "654321")
        self.assertEqual(self.store.attempts_left("c1"), 5)


class VerifiedSessionExpiryTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.store = VerificationStore(clock=self.clock)
        self.store.issue("c1", "+919876500000", "123456")
        self.store.check("c1", "123456")

    def test_a_verified_conversation_stays_verified_for_a_while(self):
        self.clock.advance(VERIFIED_TTL_SECONDS - 1)
        self.assertEqual(self.store.verified_phone("c1"), "+919876500000")

    def test_it_does_not_stay_verified_forever(self):
        self.clock.advance(VERIFIED_TTL_SECONDS + 1)
        self.assertIsNone(self.store.verified_phone("c1"))

    def test_the_code_lifetime_does_not_end_the_session(self):
        """Proving the number and then talking for twenty minutes is normal."""
        self.clock.advance(CODE_TTL_SECONDS + 1)
        self.assertEqual(self.store.verified_phone("c1"), "+919876500000")


class ExpiredEntriesAreRemovedTests(unittest.TestCase):
    """Expiry is also the only thing these stores have ever had that can justify
    forgetting an entry, so it is what stops them growing forever."""

    def setUp(self):
        self.clock = _Clock()
        self.store = VerificationStore(clock=self.clock)

    def test_dead_entries_do_not_accumulate(self):
        for i in range(50):
            self.store.issue("conv-%d" % i, "+91987650000%d" % (i % 10), "123456")
        self.clock.advance(VERIFIED_TTL_SECONDS + 1)
        self.store.issue("fresh", "+919876500000", "123456")
        self.assertEqual(len(self.store), 1)

    def test_a_live_entry_is_kept(self):
        self.store.issue("c1", "+919876500000", "123456")
        self.store.issue("c2", "+919876500001", "654321")
        self.assertEqual(len(self.store), 2)


if __name__ == "__main__":
    unittest.main()
