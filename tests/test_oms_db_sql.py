"""The OMS reader's real SQL run by Postgres (final review, finding 10).

Opt-in: set EMOTORAD_TEST_OMS_PG_DSN to any Postgres connection string (a
throwaway local one is enough). No tables are read or needed: the query's
three tables are replaced by inline rows of made-up numbers, so the check is
the SQL's own behaviour: every stored phone shape found, the dealer's own
shop left out (through em_franchise and through an em_users dealer login),
another seller kept, a frame registered twice keeping its dated row, a
deleted row dropped. Run on 8 October 2026 against the OMS server's Postgres
with inline rows only (no table read): all held.
"""

import os
import unittest

from emotorad_ai.tools.oms_db import REGISTRATIONS_SQL

DSN_ENV = "EMOTORAD_TEST_OMS_PG_DSN"

FIXTURES = """WITH em_franchise(id, mobile, secondary_contact, deleted_at) AS (VALUES
  ('D1', '9876500002', NULL, NULL::timestamptz), ('D2', '9000000099', NULL, NULL::timestamptz)),
em_users(mobile, user_type, related_id, deleted_at) AS (VALUES
  ('9876500003', 'franchise_manager', 'D2', NULL::timestamptz)),
em_purchase(id, mobile, frame_number, product_name, product_id, product_color, purchase_date, created_at,
            updated_at, deleted_at, franchise_id, franchise_name, invoice_image, status, customer_name,
            full_address) AS (VALUES
  ('p1', '+91 98765-00001', 'F1', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p2', '09876500001', 'F2', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p3', '919876500001', 'F3', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p4', '9876500001', 'F4', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p5', '9876500002', 'F5', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'D1', 'Own', NULL, NULL, NULL, NULL),
  ('p6', '9876500002', 'F6', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p7', '9876500003', 'F7', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'D2', 'Own', NULL, NULL, NULL, NULL),
  ('p8', '9876500003', 'F8', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p9a', '9876500001', 'F9', 'X', NULL, NULL, NULL::timestamptz, now(), now() + interval '1 day', NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p9b', '9876500001', 'F9', 'X', NULL, NULL, '2024-01-05'::timestamptz, now(), now(), NULL::timestamptz, 'S9', 'Shop', NULL, NULL, NULL, NULL),
  ('p10', '9876500001', 'F10', 'X', NULL, NULL, '2025-03-12'::timestamptz, now(), now(), now(), 'S9', 'Shop', NULL, NULL, NULL, NULL)),
dealer_franchises AS ("""


def fixture_query() -> str:
    return REGISTRATIONS_SQL.replace("WITH dealer_franchises AS (", FIXTURES, 1)


@unittest.skipUnless(os.environ.get(DSN_ENV), "set %s to run the SQL against Postgres" % DSN_ENV)
class RealSqlTests(unittest.TestCase):
    def ids_for(self, m10):
        import psycopg

        with psycopg.connect(os.environ[DSN_ENV], options="-c default_transaction_read_only=on") as conn:
            return [row[0] for row in conn.execute(fixture_query(), {"m10": m10}).fetchall()]

    def test_every_stored_phone_shape_is_found_and_a_repeated_frame_keeps_its_dated_row(self):
        self.assertEqual(sorted(self.ids_for("9876500001")), ["p1", "p2", "p3", "p4", "p9b"])

    def test_the_dealers_own_shop_is_left_out_and_another_seller_kept(self):
        self.assertEqual(self.ids_for("9876500002"), ["p6"])
        self.assertEqual(self.ids_for("9876500003"), ["p8"])


class FixtureTests(unittest.TestCase):
    def test_the_fixture_query_is_the_real_query_with_inline_tables(self):
        query = fixture_query()
        self.assertTrue(query.endswith(REGISTRATIONS_SQL.split("WITH dealer_franchises AS (", 1)[1]))
        self.assertIn("em_purchase(id, mobile", query)


if __name__ == "__main__":
    unittest.main()
