"""Replacement orders in the API (spec 2026-10-10 replacement orders, section 7)."""

import dataclasses
import unittest
from unittest import mock

from emotorad_ai.fulfilment import ProductIds, load_parts_table, motor_orderable
from emotorad_ai.tools import oms_db
from tests.test_api_health import fresh_api

OFF = {"EMOTORAD_OMS_AFS_ORDERS": "", "EMOTORAD_OMS_WS_URL": "", "EMOTORAD_OMS_LOGIN_URL": "",
       "EMOTORAD_OMS_ADMIN_EMAIL": "", "EMOTORAD_OMS_ADMIN_PASSWORD": "", "EMOTORAD_OMS_ADMIN_TOKEN": "",
       "EMOTORAD_STORE": "memory", "EMOTORAD_AI_ENV": "", "EMOTORAD_OMS_PG_DSN": "",
       "EMOTORAD_WARRANTY_API_KEY": "", "EMOTORAD_OMS_API_KEY": "", "EMOTORAD_AMIGO_PG_DSN": ""}
ON = dict(OFF, EMOTORAD_OMS_AFS_ORDERS="on", EMOTORAD_OMS_WS_URL="wss://x/ws/",
          EMOTORAD_OMS_LOGIN_URL="https://x/user/login", EMOTORAD_OMS_ADMIN_EMAIL="a@b.c",
          EMOTORAD_OMS_ADMIN_PASSWORD="p")


def with_ledger(status):
    """The API as if the order ledger were kept where `status` says (the
    MongoDB ledger needs a cluster; the memory one stands in for it here)."""
    from emotorad_ai import wiring

    real = wiring.build_stores

    def build(settings, log=None, client=None):
        return dataclasses.replace(real(settings, log=log, client=client), replacement_orders_status=status)

    return mock.patch.object(wiring, "build_stores", build)


def fresh_api_on_mongodb(env):
    with with_ledger("mongodb"):
        return fresh_api(env)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: fresh_api(OFF))

    def test_off_by_default_with_no_worker(self):
        api = fresh_api(OFF)
        self.assertEqual(api.health()["oms_afs_orders"], "off")
        self.assertEqual(api.health()["replacement_orders"], "memory")
        self.assertIsNone(api.ORDER_WORKER)

    def test_on_without_its_settings_is_misconfigured_and_sends_nothing(self):
        api = fresh_api(dict(OFF, EMOTORAD_OMS_AFS_ORDERS="on"))
        self.assertTrue(api.health()["oms_afs_orders"].startswith("misconfigured: "))
        self.assertIsNone(api.ORDER_WORKER)

    def test_on_with_its_settings_builds_a_worker_that_is_not_started_at_import(self):
        api = fresh_api_on_mongodb(ON)
        self.assertEqual(api.health()["oms_afs_orders"], "on")
        self.assertTrue(api.ORDERS_LIVE)
        self.assertIsNotNone(api.ORDER_WORKER)
        self.assertFalse(api.ORDER_WORKER.status["running"])

    def test_on_with_the_memory_ledger_is_misconfigured_and_records_orders(self):
        # Finding 3: a memory ledger restarts its references at RO-1000001, so
        # a look-up would match an earlier process's order and a new one would
        # be marked sent and never placed. Orders are sent only with MongoDB.
        api = fresh_api(ON)
        self.assertEqual(api.health()["oms_afs_orders"], "misconfigured: replacement orders need MongoDB (memory)")
        self.assertFalse(api.ORDERS_LIVE)
        self.assertIsNone(api.ORDER_WORKER)
        self.assertEqual(self._order_status(api), "recorded")

    def test_on_with_the_ledger_index_missing_is_misconfigured(self):
        status = "memory: index missing, run scripts/mongo_setup.py"
        with with_ledger(status):
            api = fresh_api(ON)
        self.assertEqual(api.health()["oms_afs_orders"],
                         "misconfigured: replacement orders need MongoDB (%s)" % status)
        self.assertIsNone(api.ORDER_WORKER)

    def test_missing_settings_keep_their_own_text_on_the_mongodb_ledger(self):
        api = fresh_api_on_mongodb(dict(OFF, EMOTORAD_OMS_AFS_ORDERS="on"))
        self.assertTrue(api.health()["oms_afs_orders"].startswith("misconfigured: EMOTORAD_OMS_"))

    @staticmethod
    def _order_status(api):
        """An order placed through the API's registry, with a product id for the
        part, so only the switch decides between queued and recorded."""
        from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER
        from emotorad_ai.tools.registry import ToolContext

        frame = "EMXP2025004417"  # fixtures: Ananya's EMX Plus
        coverage = {"data": {"bikes": [{"frame_number": frame, "ownership_source": "oms_purchase",
                                        "purchase_date": "2026-03-01",
                                        "components": [{"component": "battery", "active": True}]}]}}
        context = ToolContext(conversation_id="c-live", phone="+919876543210", late={
            "evidence_seen": lambda: True, "evidence_verified": lambda: {"component": "battery", "frame": frame},
            "coverage_result": lambda: coverage,
            "customer_messages": lambda: ("I'm Test Rider, test.rider@example.com",)})
        with mock.patch.object(ProductIds, "resolve", return_value="uuid-1"):
            envelope = api.registry.call(PLACE_REPLACEMENT_ORDER, {
                "frame_number": frame, "part": "battery", "use_record_address": True, "idempotency_key": "k-live",
                "customer_name": "Test Rider", "email": "test.rider@example.com"}, context)
        return envelope["data"]["status"]

    def test_the_mongodb_ledger_with_the_switch_on_queues_orders(self):
        self.assertEqual(self._order_status(fresh_api_on_mongodb(ON)), "queued")

    def test_the_client_carries_the_deployments_environment(self):
        api = fresh_api_on_mongodb(dict(ON, EMOTORAD_AI_ENV="stage"))
        self.assertEqual(api.ORDER_WORKER._client.env, "stage")


class WorkerLogTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: fresh_api(OFF))

    def test_a_failed_order_reaches_the_event_log_at_error_level(self):
        api = fresh_api_on_mongodb(ON)
        for event in ("replacement_order_failed", "replacement_order_pass_failed",
                      "replacement_order_intent_unsaved", "order_worker_pass_failed",
                      "replacement_order_look_only", "replacement_order_save_failed",
                      "oms_pin_code_lookup_failed"):
            api._order_worker_log(event, {"reference": "RO-0000001"})
            found = [e for e in api.log.events if e["event"] == event][-1]
            self.assertEqual(found["level"], "error", event)
            self.assertEqual(found["reference"], "RO-0000001")

    def test_a_sent_order_is_not_an_error(self):
        api = fresh_api_on_mongodb(ON)
        api._order_worker_log("replacement_order_sent", {"reference": "RO-0000002"})
        found = [e for e in api.log.events if e["event"] == "replacement_order_sent"][-1]
        self.assertNotIn("level", found)

    def test_a_failed_login_is_an_error(self):
        api = fresh_api_on_mongodb(ON)
        api._oms_afs_log("oms_login_failed", {"status": 401})
        found = [e for e in api.log.events if e["event"] == "oms_login_failed"][-1]
        self.assertEqual(found["level"], "error")


class MotorTests(unittest.TestCase):
    def test_no_motor_part_may_be_ordered_today(self):
        self.assertFalse(motor_orderable(load_parts_table(), ProductIds()))

    def test_a_fitted_motor_part_with_a_product_id_would_be(self):
        from emotorad_ai.fulfilment import PartRule

        table = {"controller": PartRule(part="controller", technician=False)}
        self.assertTrue(motor_orderable(table, ProductIds({"EMX Plus": {"controller": "uuid-9"}})))
        self.assertFalse(motor_orderable(table, ProductIds()))

    def test_the_motor_agent_is_not_offered_the_tool_today(self):
        from emotorad_ai.agents.motor_support import TOOL_NAMES
        from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER

        self.assertNotIn(PLACE_REPLACEMENT_ORDER, TOOL_NAMES)


class PinCodeTests(unittest.TestCase):
    def test_the_pin_code_id_is_read_by_its_pin_code(self):
        from tests.test_oms_orders import FakeConnection

        fake = FakeConnection()
        fake.answers[oms_db.PIN_CODE_SQL] = [{"id": "pin-1"}]
        db = oms_db.OMSDatabase("postgresql://ro@oms/x", connect=fake)
        self.assertEqual(db.pin_code_id("122018"), "pin-1")
        self.assertIsNone(db.pin_code_id("12201"))

    def test_a_failed_pin_code_lookup_is_logged_by_its_class(self):
        # Finding 6b: still None, but never silent.
        def down(*args, **kwargs):
            raise OSError("connection refused to oms host")

        logged = []
        db = oms_db.OMSDatabase("postgresql://ro@oms/x", connect=down)
        self.assertIsNone(db.pin_code_id("122018", log=lambda event, fields: logged.append((event, fields))))
        self.assertEqual(logged, [("oms_pin_code_lookup_failed", {"error": "OSError"})])
        self.assertIsNone(db.pin_code_id("122018"))  # no log given: still None, still no raise

    def test_the_worker_logs_a_failed_pin_code_lookup_as_an_error(self):
        def down(*args, **kwargs):
            raise OSError("connection refused")

        self.addCleanup(lambda: fresh_api(OFF))
        api = fresh_api_on_mongodb(ON)
        api.OMS_DB = oms_db.OMSDatabase("postgresql://ro@oms/x", connect=down)
        self.assertIsNone(api.ORDER_WORKER._pin_codes("122018"))
        found = [e for e in api.log.events if e["event"] == "oms_pin_code_lookup_failed"][-1]
        self.assertEqual((found["level"], found["error"]), ("error", "OSError"))


if __name__ == "__main__":
    unittest.main()
