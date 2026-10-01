"""The customer's own address behind nginx. nginx sets X-Real-IP; the app
sees every request from Docker's bridge gateway, so without this every
customer shared one rate limit (checked on staging, 1 October 2026)."""

import unittest
from types import SimpleNamespace

from starlette.datastructures import Headers

from emotorad_ai.client_ip import client_ip, trusted_from_env
from emotorad_ai.ratelimit import RateLimiter

TRUSTED = frozenset({"127.0.0.1", "::1", "172.17.0.1"})


def request(peer, real_ip=None):
    headers = Headers({"x-real-ip": real_ip} if real_ip else {})
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None, headers=headers)


class ClientIpTests(unittest.TestCase):
    def test_nginx_header_is_used_from_a_trusted_peer(self):
        self.assertEqual(client_ip(request("172.17.0.1", "49.36.1.1"), TRUSTED), "49.36.1.1")

    def test_the_header_is_ignored_from_anyone_else(self):
        self.assertEqual(client_ip(request("203.0.113.9", "49.36.1.1"), TRUSTED), "203.0.113.9")

    def test_a_malformed_header_falls_back_to_the_peer(self):
        self.assertEqual(client_ip(request("172.17.0.1", "not-an-ip"), TRUSTED), "172.17.0.1")

    def test_no_peer_and_no_header_is_none(self):
        self.assertIsNone(client_ip(request(None), TRUSTED))

    def test_the_default_trusts_loopback_and_the_docker_gateway(self):
        self.assertEqual(trusted_from_env({}), TRUSTED)
        self.assertEqual(trusted_from_env({"EMOTORAD_TRUSTED_PROXIES": "10.0.0.2, 10.0.0.3"}),
                         frozenset({"10.0.0.2", "10.0.0.3"}))

    def test_two_customers_behind_the_proxy_get_their_own_limit(self):
        limiter = RateLimiter(limit=1, window_seconds=60.0)
        self.assertTrue(limiter.allow(client_ip(request("172.17.0.1", "49.36.1.1"), TRUSTED)))
        self.assertTrue(limiter.allow(client_ip(request("172.17.0.1", "49.36.1.2"), TRUSTED)))
        self.assertFalse(limiter.allow(client_ip(request("172.17.0.1", "49.36.1.1"), TRUSTED)))


if __name__ == "__main__":
    unittest.main()
