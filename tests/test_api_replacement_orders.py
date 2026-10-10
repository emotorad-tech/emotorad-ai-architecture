"""Replacement orders in the API (spec 2026-10-10 replacement orders, section 7)."""

import unittest

from emotorad_ai.fulfilment import ProductIds, load_parts_table, motor_orderable
from emotorad_ai.tools import oms_db
from tests.test_api_health import fresh_api

OFF = {"EMOTORAD_OMS_AFS_ORDERS": "", "EMOTORAD_OMS_WS_URL": "", "EMOTORAD_OMS_LOGIN_URL": "",
       "EMOTORAD_OMS_ADMIN_EMAIL": "", "EMOTORAD_OMS_ADMIN_PASSWORD": "", "EMOTORAD_OMS_ADMIN_TOKEN": ""}
ON = dict(OFF, EMOTORAD_OMS_AFS_ORDERS="on", EMOTORAD_OMS_WS_URL="wss://x/ws/",
          EMOTORAD_OMS_LOGIN_URL="https://x/user/login", EMOTORAD_OMS_ADMIN_EMAIL="a@b.c",
          EMOTORAD_OMS_ADMIN_PASSWORD="p")


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
        api = fresh_api(ON)
        self.assertEqual(api.health()["oms_afs_orders"], "on")
        self.assertIsNotNone(api.ORDER_WORKER)
        self.assertFalse(api.ORDER_WORKER.status["running"])


class WorkerLogTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: fresh_api(OFF))

    def test_a_failed_order_reaches_the_event_log_at_error_level(self):
        api = fresh_api(ON)
        for event in ("replacement_order_failed", "replacement_order_pass_failed",
                      "replacement_order_intent_unsaved", "order_worker_pass_failed"):
            api._order_worker_log(event, {"reference": "RO-0000001"})
            found = [e for e in api.log.events if e["event"] == event][-1]
            self.assertEqual(found["level"], "error", event)
            self.assertEqual(found["reference"], "RO-0000001")

    def test_a_sent_order_is_not_an_error(self):
        api = fresh_api(ON)
        api._order_worker_log("replacement_order_sent", {"reference": "RO-0000002"})
        found = [e for e in api.log.events if e["event"] == "replacement_order_sent"][-1]
        self.assertNotIn("level", found)

    def test_a_failed_login_is_an_error(self):
        api = fresh_api(ON)
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


if __name__ == "__main__":
    unittest.main()
