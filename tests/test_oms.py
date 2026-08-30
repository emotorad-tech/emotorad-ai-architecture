"""The OMS read client, exercised offline.

Every response here is one this suite has actually seen from the live API
(2026-08-29), including the awkward one: a malformed mobile returns the same
404 as a genuinely unregistered number, which is why validation happens before
the call rather than after.
"""

import io
import json
import unittest
import urllib.error

from emotorad_ai.tools.oms import (
    OMSClient,
    OMSConfigError,
    OMSNoRecord,
    OMSUnavailable,
    normalise_mobile,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _client(handler):
    """A client whose network is a function, so the suite never opens a socket."""
    calls = []

    def opener(request, timeout=None):
        calls.append({"url": request.full_url, "headers": dict(request.headers), "timeout": timeout})
        result = handler(request)
        if isinstance(result, Exception):
            raise result
        return _Response(json.dumps(result).encode("utf-8"))

    client = OMSClient(api_key="test-key", base_url="https://oms.test/purchase", opener=opener)
    return client, calls


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://oms.test", code, "err", {}, io.BytesIO(body))


class NormaliseTests(unittest.TestCase):
    def test_repo_format_becomes_api_format(self):
        for supplied in ("+919876543210", "919876543210", "9876543210", "09876543210", "98765 43210"):
            self.assertEqual(normalise_mobile(supplied), "9876543210", supplied)

    def test_anything_not_an_indian_mobile_is_refused_before_the_network(self):
        # The live API answers 404 "No Purchase data found." for `abc` and `123`
        # exactly as it does for a real unregistered number. If these reached the
        # wire we would route a real owner to "you have no bike registered".
        for supplied in ("abc", "123", "", None, "1234567890", "+14155552671"):
            with self.assertRaises(OMSConfigError):
                normalise_mobile(supplied)

    def test_a_bad_number_never_reaches_the_network(self):
        client, calls = _client(lambda request: {"purchases": [{"frame_number": "X"}]})
        with self.assertRaises(OMSConfigError):
            client.get_warranties_by_mobile("123")
        self.assertEqual(calls, [], "no request should have been made")


class WarrantyReadTests(unittest.TestCase):
    def test_rows_come_back_and_the_key_is_sent_as_a_header(self):
        client, calls = _client(
            lambda request: {"message": "ok", "purchases": [{"frame_number": "EMXP1"}, {"frame_number": "EMXP2"}]}
        )
        rows = client.get_warranties_by_mobile("+919876543210")
        self.assertEqual([r["frame_number"] for r in rows], ["EMXP1", "EMXP2"])
        self.assertIn("mobile=9876543210", calls[0]["url"])
        self.assertEqual(calls[0]["headers"].get("X-api-key"), "test-key")

    def test_404_is_no_record_not_an_outage(self):
        client, _ = _client(lambda request: _http_error(404, b'{"error": "No Purchase data found."}'))
        with self.assertRaises(OMSNoRecord):
            client.get_warranties_by_mobile("+919876543210")

    def test_an_empty_list_means_the_same_as_a_404(self):
        client, _ = _client(lambda request: {"message": "ok", "purchases": []})
        with self.assertRaises(OMSNoRecord):
            client.get_warranties_by_mobile("+919876543210")

    def test_a_rejected_key_is_our_problem_not_the_customers(self):
        # Must never surface as "you own nothing". 403 is an outage from where
        # the customer sits, and telling them to re-register would be a lie.
        for code in (400, 401, 403):
            client, _ = _client(lambda request, code=code: _http_error(code, b'{"message": "Invalid API key"}'))
            with self.assertRaises(OMSConfigError):
                client.get_warranties_by_mobile("+919876543210")

    def test_a_server_error_is_retryable_unavailability(self):
        for code in (500, 502, 503):
            client, _ = _client(lambda request, code=code: _http_error(code))
            with self.assertRaises(OMSUnavailable):
                client.get_warranties_by_mobile("+919876543210")

    def test_a_dead_connection_is_unavailability(self):
        client, _ = _client(lambda request: urllib.error.URLError("timed out"))
        with self.assertRaises(OMSUnavailable):
            client.get_warranties_by_mobile("+919876543210")

    def test_garbage_body_is_unavailability_not_a_crash(self):
        def opener(request, timeout=None):
            return _Response(b"<html>502 Bad Gateway</html>")

        client = OMSClient(api_key="k", base_url="https://oms.test/purchase", opener=opener)
        with self.assertRaises(OMSUnavailable):
            client.get_warranties_by_mobile("+919876543210")

    def test_a_missing_key_fails_before_the_network(self):
        client, calls = _client(lambda request: {"purchases": []})
        client.api_key = ""
        with self.assertRaises(OMSConfigError):
            client.get_warranties_by_mobile("+919876543210")
        self.assertEqual(calls, [])


class OrderReadTests(unittest.TestCase):
    def test_orders_come_back(self):
        client, calls = _client(lambda request: {"orders": [{"order_code": "ORD/1"}]})
        self.assertEqual(client.get_orders_by_identifiers("+919876543210")[0]["order_code"], "ORD/1")
        self.assertIn("get_orders_by_identifiers", calls[0]["url"])

    def test_no_orders_is_not_evidence_of_anything(self):
        # A franchise walk-in has registrations and no OMS orders at all —
        # verified live, on a number carrying three registered bikes. Absence
        # must not be read as "never bought a bike".
        client, _ = _client(lambda request: _http_error(404, b'{"error": "No Order data found."}'))
        with self.assertRaises(OMSNoRecord):
            client.get_orders_by_identifiers("+919876543210")


if __name__ == "__main__":
    unittest.main()
