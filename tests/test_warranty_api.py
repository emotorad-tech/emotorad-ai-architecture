"""The warranty API (Sachin's warranty service, 7 October 2026), exercised offline.

It replaces the OMS warranty lookup for the bot: one server-to-server call by
phone (`searchRegistrationsByPhone`), with per-part coverage and dates the
service computes itself. The responses here are the spec's own examples
(docs/api-shapes/warranty-ai-search.json), varied only where a test needs a
different state.
"""

import copy
import importlib
import io
import json
import os
import pathlib
import unittest
import urllib.error
from datetime import date
from unittest import mock

from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, build_registry
from emotorad_ai.tools.registry import ToolContext, ToolError, is_error
from emotorad_ai.tools.warranty_api import (
    DEFAULT_BASE_URL,
    SEARCH_PATH,
    WarrantyAPIClient,
    WarrantyAPIConfigError,
    WarrantyAPINoRecord,
    WarrantyAPIUnavailable,
    api_warranty_source,
)

SHAPES = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "docs" / "api-shapes" / "warranty-ai-search.json").read_text()
)
EXAMPLE = SHAPES["response_200"]
PHONE = "+919999999999"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _client(handler, api_key="test-service-key"):
    """A client whose network is a function, so the suite never opens a socket."""
    calls = []

    def opener(request, timeout=None):
        calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": json.loads(request.data.decode("utf-8")) if request.data else None,
            "timeout": timeout,
        })
        result = handler(request)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return _Response(result)
        return _Response(json.dumps(result).encode("utf-8"))

    return WarrantyAPIClient(api_key=api_key, base_url="https://warranty.test", opener=opener), calls


def _http_error(code, body=None):
    raw = json.dumps(body or {"error": {"code": "X", "message": "x", "errorId": "e"}}).encode("utf-8")
    return urllib.error.HTTPError("https://warranty.test", code, "err", {}, io.BytesIO(raw))


def _item(**changes):
    item = copy.deepcopy(EXAMPLE[0])
    coverage = changes.pop("coverage", None)
    item.update(changes)
    if coverage is not None:
        item["coverage"].update(coverage)
    return item


def _component(item, name, **changes):
    for part in item["coverage"]["components"]:
        if part["component"] == name:
            part.update(changes)
    return item


class ClientTests(unittest.TestCase):
    def test_the_spec_example_is_returned_as_is(self):
        client, calls = _client(lambda request: EXAMPLE)
        self.assertEqual(client.search(PHONE), EXAMPLE)

    def test_it_posts_the_phone_and_country_with_the_service_key_header(self):
        client, calls = _client(lambda request: EXAMPLE)
        client.search(PHONE)
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://warranty.test" + SEARCH_PATH)
        self.assertEqual(SEARCH_PATH, "/api/warranty/v1/service/ai/registrations/search")
        self.assertEqual(call["headers"]["x-service-key"], "test-service-key")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(call["body"], {"phone": PHONE, "country": "IN"})
        self.assertIsNotNone(call["timeout"])

    def test_the_body_matches_the_spec_request_shape(self):
        # The spec allows phone and country only (additionalProperties: false).
        client, calls = _client(lambda request: EXAMPLE)
        client.search("+919000000042")
        self.assertEqual(calls[0]["body"], SHAPES["request"])

    def test_a_number_that_is_not_an_indian_mobile_never_reaches_the_network(self):
        client, calls = _client(lambda request: EXAMPLE)
        for supplied in ("abc", "", None, "+14155552671", "12345"):
            with self.assertRaises(WarrantyAPIConfigError, msg=supplied):
                client.search(supplied)
        self.assertEqual(calls, [])

    def test_no_key_means_no_call(self):
        client, calls = _client(lambda request: EXAMPLE, api_key="")
        with self.assertRaises(WarrantyAPIConfigError):
            client.search(PHONE)
        self.assertEqual(calls, [])

    def test_an_empty_list_is_no_record(self):
        client, _ = _client(lambda request: [])
        with self.assertRaises(WarrantyAPINoRecord):
            client.search(PHONE)

    def test_a_rejected_key_or_bad_request_is_our_outage_not_no_record(self):
        # 400 after our own validation, 401 or 403 are our misconfiguration:
        # told to a customer as "you own nothing" they would send a registered
        # owner to re-register.
        for code in (400, 401, 403):
            client, _ = _client(lambda request, code=code: _http_error(code))
            with self.assertRaises(WarrantyAPIConfigError, msg=code):
                client.search(PHONE)

    def test_rate_limits_server_errors_and_network_faults_are_unavailable(self):
        for result in (_http_error(429), _http_error(500), _http_error(503), urllib.error.URLError("down"),
                       TimeoutError("slow")):
            client, _ = _client(lambda request, result=result: result)
            with self.assertRaises(WarrantyAPIUnavailable, msg=repr(result)):
                client.search(PHONE)

    def test_a_body_that_is_not_a_list_of_objects_is_unavailable(self):
        for body in (b"not json", {"items": []}, ["a string"]):
            client, _ = _client(lambda request, body=body: body)
            with self.assertRaises(WarrantyAPIUnavailable, msg=repr(body)):
                client.search(PHONE)

    def test_the_key_never_appears_in_an_error_or_the_repr(self):
        for result in (_http_error(401), _http_error(500), urllib.error.URLError("down")):
            client, _ = _client(lambda request, result=result: result, api_key="sekret-value-123")
            try:
                client.search(PHONE)
            except Exception as exc:  # noqa: BLE001 - the message is what is checked
                self.assertNotIn("sekret-value-123", str(exc))
            self.assertNotIn("sekret-value-123", repr(client))

    def test_the_base_url_defaults_to_staging_and_comes_from_the_environment(self):
        self.assertEqual(DEFAULT_BASE_URL, "https://d2c-storefront-staging.emotorad.com")
        with mock.patch.dict(os.environ, {"EMOTORAD_WARRANTY_API_URL": "https://warranty.example/",
                                          "EMOTORAD_WARRANTY_API_KEY": "k"}):
            client = WarrantyAPIClient()
        self.assertEqual(client.base_url, "https://warranty.example")
        self.assertEqual(client.host, "warranty.example")


class SourceTests(unittest.TestCase):
    def _source(self, result):
        client, _ = _client(lambda request: result)
        return api_warranty_source(client)

    def test_each_registration_becomes_a_record_the_lookup_reads(self):
        records = self._source(EXAMPLE)(PHONE)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["frame_number"], "ZZ.EXAMPLE.0001")
        self.assertEqual(record["product_name"], "T-Rex Pro")
        self.assertEqual(record["purchase_date"], "2027-03-15")
        self.assertEqual(record["registration_status"], "active")
        self.assertEqual(record["warranty_api"], EXAMPLE[0]["coverage"])
        # The API carries no personal fields, and none is invented.
        for field in ("customer_name", "full_address", "mobile"):
            self.assertNotIn(field, record)

    def test_no_record_is_none(self):
        self.assertIsNone(self._source([])(PHONE))

    def test_an_outage_is_the_tools_retryable_unavailable_error(self):
        for result in (_http_error(500), _http_error(401), urllib.error.URLError("down")):
            source = self._source(result)
            with self.assertRaises(ToolError) as caught:
                source(PHONE)
            self.assertEqual(caught.exception.code, "oms_unavailable")
            self.assertTrue(caught.exception.retryable)


class LookupTests(unittest.TestCase):
    """The lookup tool over the new source: the same outcomes the agents know,
    with the service's own dates and per-part coverage."""

    def _lookup(self, items, today=date(2027, 6, 1)):
        client, _ = _client(lambda request: items)
        registry = build_registry(warranty_source=api_warranty_source(client), today=today)
        return registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=PHONE))

    def test_an_active_bike_is_in_warranty_on_the_battery_s_own_date(self):
        envelope = self._lookup(EXAMPLE)
        self.assertFalse(is_error(envelope), envelope)
        bike = envelope["data"]["bikes"][0]
        self.assertIs(bike["in_warranty"], True)
        self.assertEqual(bike["coverage_status"], "from_warranty_api")
        self.assertEqual(bike["warranty_start"], "2027-03-15")
        self.assertEqual(bike["warranty_end"], "2028-03-15")
        self.assertEqual(bike["term_months"], 12)
        self.assertEqual(bike["term_source"], "warranty_api")
        self.assertEqual(bike["warranty_start_source"], "purchase_date")
        self.assertEqual(bike["months_remaining"], 9)
        self.assertEqual(
            {part["component"]: (part["valid_until"], part["active"]) for part in bike["components"]},
            {"frame": ("2032-03-15", True), "motor": ("2028-03-15", True), "battery": ("2028-03-15", True),
             "controller": ("2028-03-15", True), "display": ("2028-03-15", True),
             "charger": ("2027-09-15", True), "other": ("2027-09-15", True)},
        )

    def test_the_battery_part_decides_even_when_the_bike_as_a_whole_is_active(self):
        # The frame runs 60 months and the battery 12. A bike whose battery
        # cover has ended is "active" overall; reading that would let the bot
        # tell a customer their battery is covered.
        item = _component(_item(), "battery", active=False, validUntil="2027-05-01")
        bike = self._lookup([item])["data"]["bikes"][0]
        self.assertIs(bike["in_warranty"], False)
        self.assertEqual(bike["warranty_end"], "2027-05-01")
        self.assertEqual(bike["months_remaining"], 0)
        frame = [part for part in bike["components"] if part["component"] == "frame"][0]
        self.assertIs(frame["active"], True)

    def test_an_expired_bike_is_out_of_warranty(self):
        item = _item(coverage={"status": "expired"})
        for part in item["coverage"]["components"]:
            part["active"] = False
        bike = self._lookup([item])["data"]["bikes"][0]
        self.assertIs(bike["in_warranty"], False)

    def test_with_no_battery_part_the_bike_s_own_status_decides(self):
        item = _item()
        item["coverage"]["components"] = [part for part in item["coverage"]["components"]
                                          if part["component"] != "battery"]
        self.assertIs(self._lookup([item])["data"]["bikes"][0]["in_warranty"], True)
        item = _item(coverage={"status": "expired"})
        item["coverage"]["components"] = []
        self.assertIs(self._lookup([item])["data"]["bikes"][0]["in_warranty"], False)

    def test_unknown_coverage_asks_for_purchase_proof_as_before(self):
        item = _item(purchaseDate=None, coverage={"status": "unknown", "remedy": "collect_purchase_proof"})
        for part in item["coverage"]["components"]:
            part.update(validUntil=None, active=False)
        bike = self._lookup([item])["data"]["bikes"][0]
        self.assertIsNone(bike["in_warranty"])
        self.assertEqual(bike["coverage_status"], "purchase_date_missing")
        self.assertEqual(bike["remedy"], "collect_purchase_proof")

    def test_a_registration_under_review_states_no_coverage(self):
        bike = self._lookup([_item(status="pending_review")])["data"]["bikes"][0]
        self.assertIsNone(bike["in_warranty"])
        self.assertEqual(bike["coverage_status"], "pending_review")
        self.assertIn("review", bike["note"])
        self.assertNotIn("warranty_end", bike)

    def test_an_unknown_registration_status_states_no_coverage(self):
        # Only "active" may lead to a coverage answer; anything new the
        # service adds is treated as "do not state coverage".
        bike = self._lookup([_item(status="suspended")])["data"]["bikes"][0]
        self.assertIsNone(bike["in_warranty"])
        self.assertEqual(bike["coverage_status"], "pending_review")

    def test_no_registration_routes_to_late_registration(self):
        envelope = self._lookup([])
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "no_warranty_record")

    def test_an_outage_is_not_no_record(self):
        client, _ = _client(lambda request: _http_error(503))
        registry = build_registry(warranty_source=api_warranty_source(client), today=date(2027, 6, 1))
        envelope = registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=PHONE))
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "oms_unavailable")

    def test_several_bikes_each_get_their_own_answer(self):
        second = _item(frameNumber="ZZ.EXAMPLE.0002", status="pending_review")
        bikes = self._lookup([_item(), second])["data"]["bikes"]
        self.assertEqual([bike["frame_number"] for bike in bikes], ["ZZ.EXAMPLE.0001", "ZZ.EXAMPLE.0002"])
        self.assertEqual([bike["coverage_status"] for bike in bikes], ["from_warranty_api", "pending_review"])


class AgentLineTests(unittest.TestCase):
    def _bike(self, items):
        client, _ = _client(lambda request: items)
        registry = build_registry(warranty_source=api_warranty_source(client), today=date(2027, 6, 1))
        return registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=PHONE))["data"]["bikes"][0]

    def test_the_battery_agent_quotes_the_service_s_term_and_lists_each_part(self):
        from emotorad_ai.agents.battery_support import _coverage_line

        line = _coverage_line(self._bike(EXAMPLE))
        self.assertIn("in warranty", line)
        self.assertIn("12 month", line)
        self.assertIn("frame until 2032-03-15", line)
        self.assertIn("charger until 2027-09-15", line)

    def test_the_battery_agent_states_no_coverage_while_under_review(self):
        from emotorad_ai.agents.battery_support import _coverage_line

        line = _coverage_line(self._bike([_item(status="pending_review")]))
        self.assertIn("UNDER REVIEW", line)
        self.assertIn("Do not state or estimate coverage", line)

    def test_the_customer_summary_says_under_review(self):
        from emotorad_ai.enrichment import ContextEnricher

        block = ContextEnricher.__new__(ContextEnricher)._bikes_block([self._bike([_item(status="pending_review")])])
        self.assertIn("registration under review", block)


class WiringTests(unittest.TestCase):
    def _api(self, env):
        base = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory", "EMOTORAD_AMIGO_PG_DSN": "",
                "EMOTORAD_AI_MEDIA_BUCKET": "", "EMOTORAD_OMS_API_KEY": "", "EMOTORAD_WARRANTY_API_KEY": "",
                "EMOTORAD_WARRANTY_API_URL": ""}
        base.update(env)
        with mock.patch.dict(os.environ, base), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
            import emotorad_ai.api as api

            return importlib.reload(api)

    def test_without_either_key_the_fixtures_answer(self):
        self.assertEqual(self._api({}).health()["warranty_source"], "fixtures")

    def test_the_oms_answers_when_only_its_key_is_set(self):
        self.assertEqual(self._api({"EMOTORAD_OMS_API_KEY": "oms-test"}).health()["warranty_source"], "oms")

    def test_the_warranty_api_wins_when_its_key_is_set_and_health_names_its_host(self):
        api = self._api({"EMOTORAD_WARRANTY_API_KEY": "svc-test", "EMOTORAD_OMS_API_KEY": "oms-test"})
        self.assertEqual(api.health()["warranty_source"], "warranty_api: d2c-storefront-staging.emotorad.com")
        self.assertNotIn("svc-test", json.dumps(api.health()))

    def test_the_bot_s_lookup_goes_to_the_warranty_api_when_its_key_is_set(self):
        api = self._api({"EMOTORAD_WARRANTY_API_KEY": "svc-test"})
        with mock.patch("emotorad_ai.tools.warranty_api.WarrantyAPIClient.search", return_value=EXAMPLE) as search:
            envelope = api.registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=PHONE))
        search.assert_called_once_with(PHONE)
        self.assertEqual(envelope["data"]["bikes"][0]["coverage_status"], "from_warranty_api")


if __name__ == "__main__":
    unittest.main()
