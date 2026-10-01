"""How the person asking for a deletion was proven (manual erasure spec,
section 2): the review shows it to the person deciding."""

import unittest

from emotorad_ai import erasure
from emotorad_ai.tools.verification import VERIFIED_TTL_SECONDS, VerificationStore


class VerifiedOnTests(unittest.TestCase):
    def test_the_time_of_day_a_number_was_proved(self):
        now = [1000.0]
        store = VerificationStore(clock=lambda: now[0], wall_clock=lambda: "2026-10-01T08:58:00+00:00")
        store.issue("c1", "+919700000033", "123456")
        self.assertIsNone(store.verified_on("c1"))
        self.assertTrue(store.check("c1", "123456"))
        self.assertEqual(store.verified_on("c1"), "2026-10-01T08:58:00+00:00")
        now[0] += VERIFIED_TTL_SECONDS + 1
        self.assertIsNone(store.verified_on("c1"))
        self.assertIsNone(store.verified_on("nobody"))


class ProofOfTests(unittest.TestCase):
    def test_otp_or_the_app(self):
        self.assertEqual(erasure.proof_of("2026-10-01T08:58:00+00:00"),
                         {"method": "otp", "verified_at": "2026-10-01T08:58:00+00:00"})
        self.assertEqual(erasure.proof_of(None), {"method": "app_sign_in"})
