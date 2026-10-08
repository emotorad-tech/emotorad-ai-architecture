"""The API's bikes source with the OMS database, and the OMS key kept out of
verification while dev codes are on (spec 2026-10-08, sections 1 and 4)."""

import unittest
from unittest import mock

from emotorad_ai.tools import fixtures, oms_db
from tests.test_api_health import fresh_api

DSN = {"EMOTORAD_OMS_PG_DSN": "postgresql://ro@oms.example/emotorad"}


def never_connect(*args, **kwargs):
    raise AssertionError("the API connected to OMS at import")


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: fresh_api({"EMOTORAD_OMS_PG_DSN": "", "EMOTORAD_OMS_API_KEY": "",
                                           "EMOTORAD_AI_DEV_CODES": "", "EMOTORAD_WARRANTY_API_KEY": ""}))

    def test_the_oms_database_is_the_source_when_its_dsn_is_set(self):
        with mock.patch.object(oms_db, "_psycopg_connect", never_connect):
            api = fresh_api(DSN)
        self.assertEqual(api.WARRANTY_SOURCE, "oms_db")
        self.assertIsInstance(api.OMS_DB, oms_db.OMSDatabase)
        self.assertEqual(api.health()["warranty_source"], "oms_db")

    def test_it_comes_before_the_warranty_service(self):
        with mock.patch.object(oms_db, "_psycopg_connect", never_connect):
            api = fresh_api(dict(DSN, EMOTORAD_WARRANTY_API_KEY="k"))
        self.assertEqual(api.WARRANTY_SOURCE, "oms_db")

    def test_without_it_nothing_changes(self):
        api = fresh_api({"EMOTORAD_OMS_PG_DSN": "", "EMOTORAD_OMS_API_KEY": "", "EMOTORAD_WARRANTY_API_KEY": ""})
        self.assertIsNone(api.OMS_DB)
        self.assertEqual(api.WARRANTY_SOURCE, "fixtures")


class AccountFinderTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: fresh_api({"EMOTORAD_OMS_API_KEY": "", "EMOTORAD_AI_DEV_CODES": ""}))

    def test_while_dev_codes_are_on_the_oms_key_never_verifies_anyone(self):
        api = fresh_api({"EMOTORAD_OMS_API_KEY": "k", "EMOTORAD_AI_DEV_CODES": "1"})
        self.assertIs(api.ACCOUNT_FINDER, fixtures.find_account_by_order_code)
        self.assertIsNotNone(api.OMS_CLIENT)

    def test_with_dev_codes_off_the_oms_finds_the_account(self):
        api = fresh_api({"EMOTORAD_OMS_API_KEY": "k", "EMOTORAD_AI_DEV_CODES": ""})
        self.assertIsNot(api.ACCOUNT_FINDER, fixtures.find_account_by_order_code)


if __name__ == "__main__":
    unittest.main()
