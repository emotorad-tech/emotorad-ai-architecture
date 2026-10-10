"""The Desk access token (spec 2026-10-05, section 1).

The token answer is read by its body, whatever the status. Zoho throttles with
HTTP 200 "Access Denied", and a rotated OMS secret shows as
invalid_client_secret.
"""

import socket
import threading
import unittest
import urllib.parse

from emotorad_ai.zoho.auth import (
    ACCOUNTS_URL,
    REFRESH_EARLY_SECONDS,
    REFUSED_WAIT_SECONDS,
    THROTTLE_WAIT_SECONDS,
    TOKEN_URL,
    TokenSource,
)
from emotorad_ai.zoho.errors import ZohoError, ZohoTokenRefused, ZohoTokenThrottled, ZohoUnavailable
from emotorad_ai.zoho.http import DeskHTTP
from tests.fake_zoho import (
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH,
    SECRETS,
    Answer,
    FakeZoho,
    shape,
    token_answer,
    zoho_settings,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def source(*answers, auto_token=False):
    fake = FakeZoho(auto_token=auto_token).queue(*answers)
    clock = Clock()
    return TokenSource(zoho_settings(), DeskHTTP(opener=fake), clock=clock), fake, clock


def token_body(name):
    return shape("zoho-token.json")[name]


class TokenTests(unittest.TestCase):
    def test_the_accounts_host_is_the_india_data_centre(self):
        self.assertEqual(ACCOUNTS_URL, "https://accounts.zoho.in")
        self.assertEqual(TOKEN_URL, "https://accounts.zoho.in/oauth/v2/token")

    def test_the_refresh_token_goes_in_a_form_body_never_the_address(self):
        tokens, fake, _ = source(Answer(200, token_answer(1)))
        self.assertEqual(tokens.token(), "tok-1")
        sent = fake.requests[0]
        self.assertEqual((sent["method"], sent["url"], sent["query"]), ("POST", TOKEN_URL, {}))
        self.assertEqual(sent["headers"]["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(urllib.parse.parse_qs(sent["body"].decode("ascii")), {
            "refresh_token": [REFRESH], "client_id": [CLIENT_ID], "client_secret": [CLIENT_SECRET],
            "grant_type": ["refresh_token"],
        })
        self.assertEqual(tokens.state, "ok")

    def test_the_token_is_kept_until_five_minutes_before_its_hour_ends(self):
        tokens, fake, clock = source(Answer(200, token_answer(1)), Answer(200, token_answer(2)))
        self.assertEqual(REFRESH_EARLY_SECONDS, 300)
        self.assertEqual(tokens.token(), "tok-1")
        clock.now += 3600 - REFRESH_EARLY_SECONDS - 1
        self.assertEqual(tokens.token(), "tok-1")
        self.assertEqual(len(fake.requests), 1)
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-2")
        self.assertEqual(len(fake.requests), 2)

    def test_a_missing_lifetime_is_taken_as_an_hour(self):
        no_lifetime = {key: value for key, value in token_answer(1).items() if key != "expires_in"}
        tokens, _, clock = source(Answer(200, no_lifetime), Answer(200, token_answer(2)))
        tokens.token()
        clock.now += 3600 - 300 - 1
        self.assertEqual(tokens.token(), "tok-1")
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-2")

    def test_a_lifetime_that_is_no_number_is_taken_as_an_hour(self):
        for lifetime in ("soon", -5, 0, float("inf"), None):
            with self.subTest(lifetime=lifetime):
                tokens, _, clock = source(Answer(200, dict(token_answer(1), expires_in=lifetime)),
                                          Answer(200, token_answer(2)))
                tokens.token()
                clock.now += 3600 - 300 - 1
                self.assertEqual(tokens.token(), "tok-1")

    def test_invalidate_drops_the_token_so_the_next_call_fetches_one(self):
        tokens, fake, _ = source(Answer(200, token_answer(1)), Answer(200, token_answer(2)))
        tokens.token()
        tokens.invalidate()
        self.assertEqual(tokens.token(), "tok-2")
        self.assertEqual(len(fake.requests), 2)


class ThrottleTests(unittest.TestCase):
    def test_access_denied_with_http_200_stops_token_requests_for_ten_minutes(self):
        tokens, fake, clock = source(Answer(200, token_body("access_denied")), Answer(200, token_answer(1)))
        self.assertEqual(THROTTLE_WAIT_SECONDS, 600)
        with self.assertRaises(ZohoTokenThrottled):
            tokens.token()
        self.assertEqual(tokens.state, "throttled")
        clock.now += THROTTLE_WAIT_SECONDS - 1
        with self.assertRaises(ZohoTokenThrottled):
            tokens.token()
        self.assertEqual(len(fake.requests), 1)
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-1")
        self.assertEqual(tokens.state, "ok")


class RefusalTests(unittest.TestCase):
    def test_a_rotated_secret_sets_the_health_state_and_waits_an_hour(self):
        tokens, fake, clock = source(Answer(400, token_body("invalid_client_secret")), Answer(200, token_answer(1)))
        self.assertEqual(REFUSED_WAIT_SECONDS, 3600)
        with self.assertRaises(ZohoTokenRefused) as caught:
            tokens.token()
        self.assertEqual(caught.exception.error, "invalid_client_secret")
        self.assertEqual(tokens.state, "token refused: invalid_client_secret")
        clock.now += REFUSED_WAIT_SECONDS - 1
        with self.assertRaises(ZohoTokenRefused) as again:
            tokens.token()
        self.assertEqual(again.exception.error, "invalid_client_secret")
        self.assertEqual(len(fake.requests), 1)
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-1")
        self.assertEqual(tokens.state, "ok")

    def test_each_refusal_is_named_whatever_the_status(self):
        for name, status in (("invalid_code", 200), ("invalid_client", 401), ("invalid_client_secret", 200)):
            with self.subTest(name=name):
                tokens, _, _ = source(Answer(status, token_body(name)))
                with self.assertRaises(ZohoTokenRefused):
                    tokens.token()
                self.assertEqual(tokens.state, "token refused: %s" % name)

    def test_any_other_error_the_endpoint_names_is_a_refusal_too(self):
        # Zoho's token endpoint answers more than the three the design names.
        # Each used to go on the 30-second schedule, ask again every time and
        # trip the throttle, and /health never said the token was refused.
        for name, status in (("invalid_grant", 400), ("unauthorized_client", 400), ("invalid_request", 400),
                             ("invalid_scope", 200)):
            with self.subTest(name=name):
                tokens, fake, clock = source(Answer(status, {"error": name}), Answer(200, token_answer(1)))
                with self.assertRaises(ZohoTokenRefused) as caught:
                    tokens.token()
                self.assertEqual(caught.exception.error, name)
                self.assertEqual(tokens.state, "token refused: %s" % name)
                clock.now += REFUSED_WAIT_SECONDS - 1
                with self.assertRaises(ZohoTokenRefused):
                    tokens.token()
                self.assertEqual(len(fake.requests), 1)
                clock.now += 1
                self.assertEqual(tokens.token(), "tok-1")

    def test_a_named_error_behind_a_5xx_is_unavailable_not_a_refusal(self):
        # A refusal pauses every ticket, safety ones included, for an hour.
        # A server fault that happens to name an error is transient: the
        # 30-second schedule, and /health keeps "ok".
        for name, status in (("invalid_grant", 500), ("invalid_client_secret", 502), ("server_error", 503)):
            with self.subTest(name=name, status=status):
                tokens, fake, _ = source(Answer(status, {"error": name}), Answer(200, token_answer(1)))
                with self.assertRaises(ZohoUnavailable) as caught:
                    tokens.token()
                self.assertNotIsInstance(caught.exception, ZohoTokenRefused)
                self.assertEqual(caught.exception.error, "http_%d" % status)
                self.assertEqual(tokens.state, "ok")
                self.assertEqual(tokens.token(), "tok-1")
                self.assertEqual(len(fake.requests), 2)

    def test_a_named_error_below_500_is_still_a_refusal(self):
        for status in (200, 400, 401, 499):
            with self.subTest(status=status):
                tokens, _, _ = source(Answer(status, {"error": "invalid_grant"}))
                with self.assertRaises(ZohoTokenRefused):
                    tokens.token()
                self.assertEqual(tokens.state, "token refused: invalid_grant")

    def test_an_error_that_is_not_a_code_is_a_refusal_that_shows_no_text(self):
        tokens, _, _ = source(Answer(400, {"error": "call me on +919999999999"}))
        with self.assertRaises(ZohoTokenRefused) as caught:
            tokens.token()
        self.assertEqual(caught.exception.error, ZohoTokenRefused.error)
        self.assertEqual(tokens.state, "token refused: token_refused")
        self.assertNotIn("9999999999", str(caught.exception) + repr(caught.exception) + tokens.state)


class FailureTests(unittest.TestCase):
    def test_a_network_failure_is_unavailable_and_leaves_the_state_alone(self):
        tokens, _, _ = source(socket.timeout("timed out"), Answer(200, token_answer(1)))
        with self.assertRaises(ZohoUnavailable):
            tokens.token()
        self.assertEqual(tokens.state, "ok")
        self.assertEqual(tokens.token(), "tok-1")

    def test_an_answer_without_a_token_is_unavailable(self):
        answers = (Answer(200, {}), Answer(200, {"token_type": "Bearer"}), Answer(200, {"access_token": ""}),
                   Answer(200, {"error": ""}), Answer(200, {"error": {"nested": True}}),
                   Answer(502, b""), Answer(200, b"<html></html>"))
        for answer in answers:
            with self.subTest(answer=answer):
                tokens, _, _ = source(answer)
                with self.assertRaises(ZohoUnavailable):
                    tokens.token()
                self.assertEqual(tokens.state, "ok")

    def test_a_refusal_after_invalidate_is_not_asked_again_within_the_hour(self):
        tokens, fake, clock = source(Answer(200, token_answer(1)), Answer(400, token_body("invalid_client_secret")))
        tokens.token()
        tokens.invalidate()
        with self.assertRaises(ZohoTokenRefused):
            tokens.token()
        clock.now += 1
        with self.assertRaises(ZohoTokenRefused):
            tokens.token()
        self.assertEqual(len(fake.requests), 2)


class ConcurrencyTests(unittest.TestCase):
    def test_callers_at_the_same_moment_share_one_refresh(self):
        fake = FakeZoho(auto_token=True)
        entered, release = threading.Event(), threading.Event()

        def hold():
            entered.set()
            release.wait(5)

        fake.token_gate = hold
        tokens = TokenSource(zoho_settings(), DeskHTTP(opener=fake), clock=Clock())
        got = []
        first = threading.Thread(target=lambda: got.append(tokens.token()))
        first.start()
        self.assertTrue(entered.wait(5))
        second = threading.Thread(target=lambda: got.append(tokens.token()))
        second.start()
        second.join(0.2)  # time to reach the lock (or, without one, Zoho)
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(got, ["tok-1", "tok-1"])
        self.assertEqual(len(fake.token_requests), 1)


class SecretTests(unittest.TestCase):
    def test_no_secret_or_token_reaches_a_message_or_the_state(self):
        shown = []
        for status, body in ((200, token_body("access_denied")), (400, token_body("invalid_client_secret")),
                             (400, {"error": "invalid_request"}), (502, b"")):
            tokens, _, _ = source(Answer(status, body))
            with self.assertRaises(ZohoError) as caught:
                tokens.token()
            shown += [str(caught.exception), repr(caught.exception), caught.exception.error, tokens.state, repr(tokens)]
        tokens, _, _ = source(Answer(200, token_answer(1)))
        tokens.token()
        shown += [tokens.state, repr(tokens)]
        for text in shown:
            for secret in SECRETS + ("tok-1",):
                self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
