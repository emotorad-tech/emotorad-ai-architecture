"""The orders query's real SQL run by Postgres (spec 2026-10-10, section 7).

Opt-in: set EMOTORAD_TEST_OMS_PG_DSN to any Postgres connection string (a
throwaway local one is enough). No tables are read: the query's five tables
are replaced by inline rows of made-up numbers.
"""

import os
import unittest
from datetime import date

from emotorad_ai.tools.oms_db import ORDERS_SQL

DSN_ENV = "EMOTORAD_TEST_OMS_PG_DSN"

FIXTURES = """WITH em_franchise(id, mobile, secondary_contact, deleted_at) AS (VALUES
  ('D1', '9876500002', NULL, NULL::timestamptz)),
em_users(mobile, user_type, related_id, deleted_at) AS (VALUES
  ('9876500003', 'franchise_manager', 'D2', NULL::timestamptz)),
em_orders(order_code, mobile, order_source, invoice_at, cancel_at, is_return, deleted_at) AS (VALUES
  ('O1', '+91 98765-00001', 'End Customer', '2026-07-13 07:03:51+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O2', '09876500001', 'Dealer', '2025-01-01 20:00:00+00'::timestamptz, NULL::timestamptz, NULL::boolean, NULL::timestamptz),
  ('O3', '9876500001', 'Stock Transfer', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O4', '9876500001', 'End Customer', '2025-01-01 05:00:00+00'::timestamptz, now(), false, NULL::timestamptz),
  ('O5', '9876500001', 'End Customer', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, true, NULL::timestamptz),
  ('O6', '9876500001', 'End Customer', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, now()),
  ('O7', '9876500001', 'Website', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O8', '9000000001', 'Website', '2026-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O9', '9876500001', 'Website', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O10', '919876500001', 'AMAZON_IN_API', NULL::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O11', '9876500002', 'End Customer', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz),
  ('O12', '9876500003', 'End Customer', '2025-01-01 05:00:00+00'::timestamptz, NULL::timestamptz, false, NULL::timestamptz)),
em_stock_transactions(frame_number, frame_status, order_code, product_name, created_at) AS (VALUES
  ('F1', 'UNSOLD', NULL, 'EMX+', '2024-08-21'::timestamptz),
  (' F1 ', 'SOLD', 'O1', 'EMX+', '2026-07-13'::timestamptz),
  ('F1', 'SOLD', NULL, 'EMX+', '2026-08-01'::timestamptz),
  ('F2', 'SOLD', 'O2', 'Doodle', '2025-01-01'::timestamptz),
  ('F3', 'SOLD', 'O3', 'X', '2025-01-01'::timestamptz),
  ('F4', 'SOLD', 'O4', 'X', '2025-01-01'::timestamptz),
  ('F5', 'SOLD', 'O5', 'X', '2025-01-01'::timestamptz),
  ('F6', 'SOLD', 'O6', 'X', '2025-01-01'::timestamptz),
  ('F7', 'SOLD', 'O7', 'X', '2025-01-01'::timestamptz),
  ('F7', 'SOLD', 'O8', 'X', '2026-01-01'::timestamptz),
  ('F9', 'SOLD', 'O9', 'X', '2025-01-01'::timestamptz),
  ('F10', 'SOLD', 'O10', 'X', '2025-01-01'::timestamptz),
  ('F11', 'SOLD', 'O11', 'X', '2025-01-01'::timestamptz),
  ('F12', 'SOLD', 'O12', 'X', '2025-01-01'::timestamptz)),
em_purchase(frame_number, deleted_at) AS (VALUES
  ('f9 ', NULL::timestamptz),
  ('F10', now())),
dealer_franchises AS ("""


def fixture_query() -> str:
    return ORDERS_SQL.replace("WITH dealer_franchises AS (", FIXTURES, 1)


@unittest.skipUnless(os.environ.get(DSN_ENV), "set %s to run the SQL against Postgres" % DSN_ENV)
class RealSqlTests(unittest.TestCase):
    def rows_for(self, m10):
        import psycopg

        with psycopg.connect(os.environ[DSN_ENV], options="-c default_transaction_read_only=on -c TimeZone=UTC") as conn:
            return [tuple(row[:3]) for row in conn.execute(fixture_query(), {"m10": m10}).fetchall()]

    def test_the_riders_order_bikes_with_india_dates(self):
        # F1: every phone shape, a later SOLD row without an order code ignored.
        # F2: an invoice at 20:00 UTC is the next day in India. F10: no
        # invoice date, and a deleted registration does not count.
        self.assertEqual(self.rows_for("9876500001"), [("F1", "EMX+", date(2026, 7, 13)),
                                                       ("F10", "X", None),
                                                       ("F2", "Doodle", date(2025, 1, 2))])

    def test_a_resold_frame_goes_to_its_newest_buyer(self):
        self.assertEqual(self.rows_for("9000000001"), [("F7", "X", date(2026, 1, 1))])

    def test_a_dealers_phone_gets_nothing(self):
        self.assertEqual(self.rows_for("9876500002"), [])
        self.assertEqual(self.rows_for("9876500003"), [])


class FixtureTests(unittest.TestCase):
    def test_the_fixture_query_is_the_real_query_with_inline_tables(self):
        query = fixture_query()
        self.assertTrue(query.endswith(ORDERS_SQL.split("WITH dealer_franchises AS (", 1)[1]))
        self.assertIn("em_orders(order_code, mobile", query)


if __name__ == "__main__":
    unittest.main()
