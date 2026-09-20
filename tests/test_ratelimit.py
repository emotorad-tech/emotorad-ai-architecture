"""Bounding what one caller can spend.

`/message` has no authentication, and every call reaches a real model and the
real OMS. Bound to the LAN so a phone can reach it, that is anyone on the
network able to burn Anthropic credits in a loop.

This does not make the endpoint safe to expose, and is not meant to. It is the
half of the problem that needed no decision about who may use the chat.
"""

import unittest

from emotorad_ai.ratelimit import RateLimiter


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.limiter = RateLimiter(limit=3, window_seconds=60, clock=self.clock)

    def test_calls_under_the_limit_are_allowed(self):
        for _ in range(3):
            self.assertTrue(self.limiter.allow("1.2.3.4"))

    def test_the_call_over_the_limit_is_refused(self):
        for _ in range(3):
            self.limiter.allow("1.2.3.4")
        self.assertFalse(self.limiter.allow("1.2.3.4"))

    def test_callers_are_counted_separately(self):
        for _ in range(3):
            self.limiter.allow("1.2.3.4")
        self.assertTrue(self.limiter.allow("5.6.7.8"))

    def test_the_window_slides(self):
        for _ in range(3):
            self.limiter.allow("1.2.3.4")
        self.clock.advance(61)
        self.assertTrue(self.limiter.allow("1.2.3.4"))

    def test_it_slides_rather_than_resetting_on_a_fixed_boundary(self):
        """A fixed window lets twice the limit through across its edge."""
        self.limiter.allow("1.2.3.4")
        self.clock.advance(30)
        self.limiter.allow("1.2.3.4")
        self.limiter.allow("1.2.3.4")
        self.clock.advance(31)  # the first call has aged out, the other two have not
        self.assertTrue(self.limiter.allow("1.2.3.4"))
        self.assertFalse(self.limiter.allow("1.2.3.4"))

    def test_old_callers_are_forgotten(self):
        """Otherwise this is one more dictionary that grows forever."""
        for i in range(100):
            self.limiter.allow("10.0.0.%d" % i)
        self.clock.advance(61)
        self.limiter.allow("1.2.3.4")
        self.assertEqual(len(self.limiter), 1)

    def test_an_unknown_caller_is_still_limited(self):
        """A missing client address must not mean unlimited. Everything without
        an address shares one bucket."""
        limiter = RateLimiter(limit=1, window_seconds=60, clock=self.clock)
        self.assertTrue(limiter.allow(None))
        self.assertFalse(limiter.allow(None))


if __name__ == "__main__":
    unittest.main()
