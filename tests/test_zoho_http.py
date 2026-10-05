"""One call to Zoho, classified by Zoho's error code (spec 2026-10-05, section 4).

Every row of the spec's answers table, against tests/fake_zoho.py. No socket
is opened. The privacy tests matter as much as the rest, because these
messages reach the log, and through the registry, the model.
"""

import http.client
import socket
import unittest
import urllib.error

from emotorad_ai.zoho.errors import (
    ZohoAuthExpired,
    ZohoBusy,
    ZohoConfigError,
    ZohoCreditsExhausted,
    ZohoError,
    ZohoGone,
    ZohoRejected,
    ZohoTokenRefused,
    ZohoTokenThrottled,
    ZohoTooLarge,
    ZohoUnavailable,
    ZohoUnknownOutcome,
    safe_code,
)
from emotorad_ai.zoho.http import CREDITS_HEADER, DeskHTTP
from tests.fake_zoho import ORG_ID, Answer, FakeZoho, shape

URL = "https://desk.zoho.in/api/v1/tickets"
TOKEN = "tok-SECRET-DO-NOT-LEAK"
HEADERS = {"Authorization": "Zoho-oauthtoken " + TOKEN, "orgId": ORG_ID, "Accept": "application/json"}
PHONE = "+919999999999"


def zoho_error(name):
    return shape("zoho-errors.json")[name]


def http_with(*answers):
    fake = FakeZoho(auto_token=False).queue(*answers)
    return DeskHTTP(opener=fake), fake


class ErrorTests(unittest.TestCase):
    ALL = (ZohoUnavailable, ZohoUnknownOutcome, ZohoRejected, ZohoConfigError, ZohoAuthExpired, ZohoGone,
           ZohoTooLarge, ZohoBusy, ZohoCreditsExhausted, ZohoTokenRefused, ZohoTokenThrottled)

    def test_every_error_is_a_zoho_error_with_a_code_safe_to_log(self):
        for cls in self.ALL:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, ZohoError))
                self.assertEqual(cls("message").error, cls.error)
                self.assertRegex(cls.error, r"^[A-Za-z][A-Za-z0-9_]*$")

    def test_the_code_is_given_by_keyword_only(self):
        self.assertEqual(ZohoUnavailable("message", error="timeout").error, "timeout")
        with self.assertRaises(TypeError):
            ZohoUnavailable("message", "timeout")

    def test_a_code_that_is_text_falls_back_to_the_class_code(self):
        self.assertEqual(ZohoGone("message", error="call me on " + PHONE).error, ZohoGone.error)

    def test_rejected_carries_the_field_names_and_credits_the_wait(self):
        self.assertEqual(ZohoRejected("message", fields=["contactId", "subject"]).fields, ("contactId", "subject"))
        self.assertEqual(ZohoRejected("message").fields, ())
        self.assertEqual(ZohoCreditsExhausted("message", retry_after_seconds=60.0).retry_after_seconds, 60.0)
        self.assertIsNone(ZohoCreditsExhausted("message").retry_after_seconds)
        with self.assertRaises(TypeError):
            ZohoRejected("message", None, ("contactId",))


class AnswerTests(unittest.TestCase):
    def test_a_200_comes_back_parsed_and_the_credits_left_are_kept(self):
        ticket = shape("zoho-ticket.json")
        transport, _ = http_with(Answer(200, ticket, headers={CREDITS_HEADER: "48712"}))
        self.assertEqual(transport.call("POST", URL, HEADERS, b"{}", write=True), (200, ticket))
        self.assertEqual(transport.last_credits_remaining, 48712)

    def test_a_credits_header_that_is_not_a_whole_number_is_ignored(self):
        for value in ("many", "-5", "१२३", ""):
            with self.subTest(value=value):
                transport, _ = http_with(Answer(200, {}, headers={CREDITS_HEADER: value}))
                transport.call("GET", URL, HEADERS, write=False)
                self.assertIsNone(transport.last_credits_remaining)

    def test_a_204_is_no_body_not_an_error(self):
        for write in (False, True):
            with self.subTest(write=write):
                transport, _ = http_with(Answer(204))
                self.assertEqual(transport.call("GET", URL, HEADERS, write=write), (204, None))

    def test_401_invalid_oauth_is_an_expired_token(self):
        transport, _ = http_with(Answer(401, zoho_error("invalid_oauth")))
        with self.assertRaises(ZohoAuthExpired) as caught:
            transport.call("GET", URL, HEADERS, write=False)
        self.assertEqual(caught.exception.error, "INVALID_OAUTH")

    def test_scope_organisation_access_and_licence_are_configuration(self):
        named = (("scope_mismatch", "SCOPE_MISMATCH"), ("oauth_org_mismatch", "OAUTH_ORG_MISMATCH"),
                 ("forbidden", "FORBIDDEN"), ("license_access_limited", "LICENSE_ACCESS_LIMITED"))
        for status in (401, 403):
            for name, code in named:
                with self.subTest(status=status, code=code):
                    transport, _ = http_with(Answer(status, zoho_error(name)))
                    with self.assertRaises(ZohoConfigError) as caught:
                        transport.call("POST", URL, HEADERS, b"{}", write=True)
                    self.assertEqual(caught.exception.error, code)

    def test_a_401_or_403_with_no_code_is_configuration_named_by_status(self):
        for status in (401, 403):
            with self.subTest(status=status):
                transport, _ = http_with(Answer(status, b""))
                with self.assertRaises(ZohoConfigError) as caught:
                    transport.call("GET", URL, HEADERS, write=False)
                self.assertEqual(caught.exception.error, "http_%d" % status)

    def test_invalid_data_names_the_fields_zoho_named(self):
        for status in (400, 422):
            with self.subTest(status=status):
                transport, _ = http_with(Answer(status, zoho_error("invalid_data")))
                with self.assertRaises(ZohoRejected) as caught:
                    transport.call("POST", URL, HEADERS, b"{}", write=True)
                self.assertEqual(caught.exception.error, "INVALID_DATA")
                self.assertEqual(caught.exception.fields, ("contactId", "departmentId"))
                self.assertIn("contactId", str(caught.exception))

    def test_404_is_gone(self):
        transport, _ = http_with(Answer(404, zoho_error("url_not_found")))
        with self.assertRaises(ZohoGone) as caught:
            transport.call("GET", URL + "/4000000528005", HEADERS, write=False)
        self.assertEqual(caught.exception.error, "URL_NOT_FOUND")

    def test_413_is_too_large(self):
        transport, _ = http_with(Answer(413, zoho_error("resource_size_exceeded")))
        with self.assertRaises(ZohoTooLarge):
            transport.call("POST", URL, HEADERS, b"x", write=True)

    def test_408_is_unavailable_even_for_a_write(self):
        transport, _ = http_with(Answer(408, b""))
        with self.assertRaises(ZohoUnavailable):
            transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_429_too_many_requests_is_busy(self):
        transport, _ = http_with(Answer(429, zoho_error("too_many_requests")))
        with self.assertRaises(ZohoBusy):
            transport.call("GET", URL, HEADERS, write=False)

    def test_429_threshold_exceeded_carries_retry_after_and_the_credits(self):
        transport, _ = http_with(Answer(429, zoho_error("threshold_exceeded"),
                                        headers={"Retry-After": "3600", CREDITS_HEADER: "0"}))
        with self.assertRaises(ZohoCreditsExhausted) as caught:
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        self.assertEqual(caught.exception.retry_after_seconds, 3600.0)
        self.assertEqual(transport.last_credits_remaining, 0)

    def test_threshold_exceeded_without_a_readable_retry_after_has_no_wait(self):
        for headers in ({}, {"Retry-After": "soon"}, {"Retry-After": "-5"}, {"Retry-After": "inf"}):
            with self.subTest(headers=headers):
                transport, _ = http_with(Answer(429, zoho_error("threshold_exceeded"), headers=headers))
                with self.assertRaises(ZohoCreditsExhausted) as caught:
                    transport.call("GET", URL, HEADERS, write=False)
                self.assertIsNone(caught.exception.retry_after_seconds)

    def test_a_5xx_on_a_read_is_unavailable(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                transport, _ = http_with(Answer(status, b"<html>bad gateway</html>"))
                with self.assertRaises(ZohoUnavailable) as caught:
                    transport.call("GET", URL, HEADERS, write=False)
                self.assertEqual(caught.exception.error, "http_%d" % status)

    def test_a_5xx_on_a_write_is_an_unknown_outcome(self):
        transport, _ = http_with(Answer(503, b""))
        with self.assertRaises(ZohoUnknownOutcome):
            transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_a_2xx_body_that_is_not_json(self):
        transport, _ = http_with(Answer(200, b"<html>ok</html>"))
        with self.assertRaises(ZohoUnavailable):
            transport.call("GET", URL, HEADERS, write=False)
        transport, _ = http_with(Answer(200, b"<html>ok</html>"))
        with self.assertRaises(ZohoUnknownOutcome):
            transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_an_answer_with_no_row_in_the_table_is_a_rejection_named_by_its_code(self):
        transport, _ = http_with(Answer(409, {"errorCode": "DUPLICATE"}))
        with self.assertRaises(ZohoRejected) as caught:
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        self.assertEqual(caught.exception.error, "DUPLICATE")

    def test_unclassified_answers_come_back_as_they_came(self):
        # The token endpoint's body says what went wrong, whatever the status.
        refused = shape("zoho-token.json")["invalid_client"]
        transport, _ = http_with(Answer(400, refused), Answer(500, b"<html></html>"))
        self.assertEqual(transport.call("POST", URL, HEADERS, b"", write=False, classify=False), (400, refused))
        self.assertEqual(transport.call("POST", URL, HEADERS, b"", write=False, classify=False), (500, None))

    def test_unclassified_still_raises_for_a_network_failure_and_a_2xx_that_is_not_json(self):
        transport, _ = http_with(socket.timeout("timed out"))
        with self.assertRaises(ZohoUnavailable):
            transport.call("POST", URL, HEADERS, b"", write=False, classify=False)
        transport, _ = http_with(Answer(200, b"<html>ok</html>"))
        with self.assertRaises(ZohoUnavailable):
            transport.call("POST", URL, HEADERS, b"", write=False, classify=False)


class NetworkTests(unittest.TestCase):
    # urllib wraps a failure while the request is being sent in URLError
    # (urllib.request.AbstractHTTPHandler.do_open) and lets one raised while
    # waiting for, or reading, the answer through as it is. So URLError means
    # Zoho never had the whole request.
    NOT_SENT = (
        urllib.error.URLError(socket.timeout("timed out")),
        urllib.error.URLError(ConnectionRefusedError()),
        urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")),
    )
    AFTER_SENDING = (socket.timeout("timed out"), ConnectionResetError(), http.client.RemoteDisconnected("closed"))

    def test_a_request_that_was_never_sent_is_unavailable_even_for_a_write(self):
        for exc in self.NOT_SENT:
            for write in (False, True):
                with self.subTest(reason=type(exc.reason).__name__, write=write):
                    transport, _ = http_with(exc)
                    with self.assertRaises(ZohoUnavailable):
                        transport.call("POST", URL, HEADERS, b"{}", write=write)

    def test_a_write_with_no_answer_after_sending_has_an_unknown_outcome(self):
        for exc in self.AFTER_SENDING:
            with self.subTest(exc=type(exc).__name__):
                transport, _ = http_with(exc)
                with self.assertRaises(ZohoUnknownOutcome):
                    transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_a_read_with_no_answer_is_unavailable(self):
        for exc in self.AFTER_SENDING:
            with self.subTest(exc=type(exc).__name__):
                transport, _ = http_with(exc)
                with self.assertRaises(ZohoUnavailable):
                    transport.call("GET", URL, HEADERS, write=False)

    def test_a_body_cut_off_after_a_200(self):
        cut = Answer(200, shape("zoho-ticket.json"), read_error=http.client.IncompleteRead(b""))
        transport, _ = http_with(cut)
        with self.assertRaises(ZohoUnknownOutcome):
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        transport, _ = http_with(cut)
        with self.assertRaises(ZohoUnavailable):
            transport.call("GET", URL, HEADERS, write=False)

    def test_a_timeout_is_named_timeout_and_anything_else_network(self):
        transport, _ = http_with(socket.timeout("timed out"))
        with self.assertRaises(ZohoUnavailable) as caught:
            transport.call("GET", URL, HEADERS, write=False)
        self.assertEqual(caught.exception.error, "timeout")
        transport, _ = http_with(urllib.error.URLError(ConnectionRefusedError()))
        with self.assertRaises(ZohoUnavailable) as caught:
            transport.call("GET", URL, HEADERS, write=False)
        self.assertEqual(caught.exception.error, "network")


class RequestTests(unittest.TestCase):
    def test_the_request_goes_as_given_with_the_default_timeout(self):
        transport, fake = http_with(Answer(200, {"id": "1"}))
        transport.call("POST", URL, HEADERS, b'{"a": 1}', write=True)
        sent = fake.requests[0]
        self.assertEqual((sent["method"], sent["url"], sent["body"], sent["timeout"]), ("POST", URL, b'{"a": 1}', 8.0))
        self.assertEqual(sent["headers"]["orgid"], ORG_ID)
        self.assertEqual(sent["headers"]["authorization"], "Zoho-oauthtoken " + TOKEN)

    def test_a_timeout_can_be_given_per_call(self):
        transport, fake = http_with(Answer(200, {"id": "1"}))
        transport.call("POST", URL, HEADERS, b"x", write=True, timeout=60.0)
        self.assertEqual(fake.requests[0]["timeout"], 60.0)

    def test_a_header_with_a_line_break_is_refused_before_the_network(self):
        transport, fake = http_with()
        with self.assertRaises(ZohoConfigError) as caught:
            transport.call("GET", URL, {"Authorization": "Zoho-oauthtoken tok-SECRET\nX-Injected: 1"}, write=False)
        self.assertEqual(fake.requests, [])
        self.assertEqual(caught.exception.error, "bad_header")
        self.assertNotIn("tok-SECRET", str(caught.exception))


class PrivacyTests(unittest.TestCase):
    LEAKY = {
        "errorCode": "INVALID_DATA",
        "message": "phone %s rejected, token %s" % (PHONE, TOKEN),
        "errors": [{"fieldName": "/phone", "errorType": "invalid", "errorMessage": PHONE}],
    }

    def assert_clean(self, exc):
        shown = " ".join([str(exc), repr(exc), exc.error] + [str(arg) for arg in exc.args]
                         + list(getattr(exc, "fields", ())))
        for secret in (PHONE, PHONE[3:], TOKEN):
            self.assertNotIn(secret, shown)

    def test_no_message_carries_a_body_a_token_or_a_phone(self):
        answers = [
            Answer(400, self.LEAKY), Answer(401, dict(self.LEAKY, errorCode="INVALID_OAUTH")),
            Answer(403, dict(self.LEAKY, errorCode="FORBIDDEN")), Answer(404, self.LEAKY), Answer(413, self.LEAKY),
            Answer(429, dict(self.LEAKY, errorCode="THRESHOLD_EXCEEDED")), Answer(500, self.LEAKY),
            Answer(200, b"not json " + PHONE.encode("ascii")), socket.timeout(PHONE), urllib.error.URLError(PHONE),
        ]
        for answer in answers:
            for write in (False, True):
                with self.subTest(answer=answer, write=write):
                    transport, _ = http_with(answer)
                    with self.assertRaises(ZohoError) as caught:
                        transport.call("POST", URL, HEADERS, b"{}", write=write)
                    self.assert_clean(caught.exception)

    def test_a_code_that_is_not_a_code_is_not_passed_on(self):
        transport, _ = http_with(Answer(400, {"errorCode": "call me on " + PHONE}))
        with self.assertRaises(ZohoRejected) as caught:
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        self.assertEqual(caught.exception.error, "http_400")

    def test_a_field_name_that_is_not_a_field_name_is_not_passed_on(self):
        body = {"errorCode": "INVALID_DATA", "errors": [
            {"fieldName": "/" + PHONE}, {"fieldName": "/call me on " + PHONE}, {"fieldName": 7}, "text",
            {"fieldName": "/contactId"}, {"fieldName": "contactId"},
        ]}
        transport, _ = http_with(Answer(422, body))
        with self.assertRaises(ZohoRejected) as caught:
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        self.assertEqual(caught.exception.fields, ("contactId",))

    def test_safe_code_knows_a_code_from_text(self):
        self.assertEqual(safe_code("INVALID_OAUTH", "x"), "INVALID_OAUTH")
        self.assertEqual(safe_code("invalid_client_secret", "x"), "invalid_client_secret")
        for text in ("Access Denied", PHONE, "", None, "9999999999", "a" * 61):
            self.assertEqual(safe_code(text, "fallback"), "fallback")

    def test_repr_holds_no_header(self):
        transport, _ = http_with(Answer(200, {}))
        transport.call("GET", URL, HEADERS, write=False)
        self.assertNotIn(TOKEN, repr(transport))


if __name__ == "__main__":
    unittest.main()
