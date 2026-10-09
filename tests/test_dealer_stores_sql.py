"""The dealer stores query run by Postgres (spec 2026-10-09, section 2).

Opt-in: set EMOTORAD_TEST_OMS_PG_DSN to any Postgres connection string. No
table is read: the query's tables are replaced by inline rows of made-up
data, so the check is the SQL's own behaviour.
"""

import os
import unittest

from emotorad_ai.tools.dealer_stores import STORES_SQL

DSN_ENV = "EMOTORAD_TEST_OMS_PG_DSN"

FIXTURES = """WITH em_franchise_type(id, franchise_type_name) AS (VALUES ('T1', 'Dealers'), ('T2', 'Others')),
em_pin_code(id, pin_code) AS (VALUES ('P1', '411014'), ('P2', '400054')),
em_district(id, district_name) AS (VALUES ('DI1', 'Pune')),
em_state(id, state_name) AS (VALUES ('S1', 'Maharashtra')),
em_franchise(id, customer_name, address, address2, pin_code_id, district_id, state_id, franchise_type_id,
             poc_name, mobile, is_active, deleted_at, is_distributor) AS (VALUES
  ('F1', 'Dealer One', 'Road 1', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner One', '9000000011', true, NULL::timestamptz, false),
  ('F2', 'Other Store', 'Road 2', NULL, 'P1', 'DI1', 'S1', 'T2', 'Owner Two', '9000000012', true, NULL::timestamptz, false),
  ('F3', 'Distributor', 'Road 3', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner Three', '9000000013', true, NULL::timestamptz, true),
  ('F4', 'Closed Dealer', 'Road 4', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner Four', '9000000014', false, NULL::timestamptz, false),
  ('F5', 'Deleted Dealer', 'Road 5', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner Five', '9000000015', true, now(), false),
  ('F6', 'Dealer Six', 'Road 6', NULL, 'P2', NULL, NULL, 'T1', 'Owner Six', '9000000016', true, NULL::timestamptz, NULL::boolean)),
em_users(full_name, mobile, user_type, related_id, is_active, deleted_at, updated_at) AS (VALUES
  ('Manager Old', '9000000021', 'franchise_manager', 'F1', true, NULL::timestamptz, now() - interval '1 day'),
  ('Manager New', '9000000022', 'franchise_manager', 'F1', true, NULL::timestamptz, now()),
  ('Sales Person', '9000000023', 'sale_franchise_person', 'F6', true, NULL::timestamptz, now()))
"""


@unittest.skipUnless(os.environ.get(DSN_ENV), "set %s to run the SQL against Postgres" % DSN_ENV)
class RealSqlTests(unittest.TestCase):
    def rows(self):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(os.environ[DSN_ENV], row_factory=dict_row,
                             options="-c default_transaction_read_only=on") as conn:
            return {r["id"]: r for r in conn.execute(FIXTURES + STORES_SQL).fetchall()}

    def test_only_active_dealers_and_the_newest_franchise_manager(self):
        rows = self.rows()
        self.assertEqual(set(rows), {"F1", "F6"})
        self.assertEqual((rows["F1"]["manager_name"], rows["F1"]["manager_mobile"]), ("Manager New", "9000000022"))
        self.assertIsNone(rows["F6"]["manager_name"])
        self.assertEqual((rows["F6"]["poc_name"], rows["F6"]["pin_code"]), ("Owner Six", "400054"))


class FixtureTests(unittest.TestCase):
    def test_the_query_starts_with_select_so_the_fixture_ctes_lead_it(self):
        self.assertTrue(STORES_SQL.lstrip().upper().startswith("SELECT"))


if __name__ == "__main__":
    unittest.main()
