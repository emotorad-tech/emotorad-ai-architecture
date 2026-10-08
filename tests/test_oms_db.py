"""The OMS production reader (spec 2026-10-08, section 1). No database: a fake
connection stands in, and the SQL itself is pinned."""

import unittest
from datetime import date, datetime, timezone

from emotorad_ai.tools import oms_db
from emotorad_ai.tools.registry import ToolError

ROW = {"id": "p1", "frame_number": "EMXP2025004417", "product_name": "EMX Plus", "product_id": "pr1",
       "product_color": "Black", "purchase_date": datetime(2025, 3, 12, tzinfo=timezone.utc),
       "created_at": datetime(2025, 3, 20, tzinfo=timezone.utc), "franchise_id": "f1",
       "franchise_name": "Ride Shop", "invoice_image": None, "status": None, "customer_name": "Test Rider",
       "full_address": "1 Test Road"}


class FakeConnection:
    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.calls = list(rows), error, []

    def __call__(self, dsn, **kwargs):
        self.calls.append(("connect", kwargs))
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.calls.append(("execute", sql, dict(params)))
        return self

    def fetchall(self):
        return [dict(r) for r in self.rows]


def reader(rows=(), error=None, now=0.0):
    fake = FakeConnection(rows, error)
    clock = [now]
    db = oms_db.OMSDatabase("postgresql://ro@oms/emotorad", connect=fake, clock=lambda: clock[0],
                            today=lambda: date(2025, 10, 1))
    return db, fake, clock


class SqlTests(unittest.TestCase):
    def test_it_matches_on_the_last_ten_digits_for_every_stored_shape(self):
        for phone in ("+919876543210", "9876543210", "919876543210", "09876543210", "98765 43210"):
            db, fake, _ = reader([ROW])
            db.registrations(phone)
            self.assertEqual(fake.calls[-1][2], {"m10": "9876543210"}, phone)

    def test_a_number_that_is_not_an_indian_mobile_never_reaches_the_database(self):
        db, fake, _ = reader([ROW])
        with self.assertRaises(ValueError):
            db.registrations("12345")
        self.assertEqual(fake.calls, [])

    def test_the_query_leaves_out_the_dealers_own_shop_and_deleted_rows(self):
        sql = oms_db.REGISTRATIONS_SQL
        for part in ("em_franchise", "secondary_contact", "em_users", "'franchise_manager'",
                     "'sale_franchise_person'", "related_id", "p.deleted_at IS NULL",
                     "NOT IN (SELECT id FROM dealer_franchises)", "DISTINCT ON (p.frame_number)",
                     "(p.purchase_date IS NULL), p.updated_at DESC"):
            self.assertIn(part, sql)

    def test_only_the_listed_columns_are_selected(self):
        selected = oms_db.REGISTRATIONS_SQL.split("SELECT DISTINCT ON (p.frame_number)")[1].split("FROM")[0]
        for personal in ("email", "date_of_birth", "referral_phone", "gender"):
            self.assertNotIn(personal, selected)

    def test_the_connection_is_read_only_with_a_statement_timeout(self):
        db, fake, _ = reader([ROW])
        db.registrations("+919876543210")
        kwargs = fake.calls[0][1]
        self.assertIn("default_transaction_read_only=on", kwargs["options"])
        self.assertIn("statement_timeout=", kwargs["options"])
        self.assertEqual(kwargs["application_name"], "emotorad-ai-chatbot")


class OutcomeTests(unittest.TestCase):
    def test_rows_become_records_with_cover_from_the_purchase_date(self):
        db, _, _ = reader([ROW])
        [record] = oms_db.db_warranty_source(db)("+919876543210")
        self.assertEqual(record["frame_number"], "EMXP2025004417")
        self.assertEqual(record["purchase_date"], "2025-03-12")
        self.assertEqual(record["registration_status"], "active")
        self.assertEqual(record["warranty_api"]["status"], "active")
        self.assertFalse(record["invoice_on_file"])
        self.assertEqual(record["term_source"], "oms_terms")

    def test_no_rows_is_no_record(self):
        db, _, _ = reader([])
        self.assertIsNone(oms_db.db_warranty_source(db)("+919876543210"))

    def test_a_database_error_is_an_outage_never_no_record(self):
        db, _, _ = reader(error=OSError("refused"))
        with self.assertRaises(ToolError) as caught:
            oms_db.db_warranty_source(db)("+919876543210")
        self.assertEqual(caught.exception.code, "oms_unavailable")
        self.assertNotIn("ro@oms", str(caught.exception))

    def test_after_a_failure_the_breaker_answers_at_once_then_retries(self):
        db, fake, clock = reader(error=OSError("refused"))
        for _ in range(2):
            with self.assertRaises(oms_db.OMSDatabaseUnavailable):
                db.registrations("+919876543210")
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "connect"), 1)
        clock[0] += oms_db.FAILURE_TTL_SECONDS + 1
        with self.assertRaises(oms_db.OMSDatabaseUnavailable):
            db.registrations("+919876543210")
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "connect"), 2)

    def test_a_phone_is_read_once_a_minute(self):
        db, fake, clock = reader([ROW])
        db.registrations("+919876543210")
        db.registrations("+919876543210")
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "execute"), 1)
        clock[0] += oms_db.CACHE_SECONDS + 1
        db.registrations("+919876543210")
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "execute"), 2)

    def test_no_purchase_date_and_an_invoice_on_file(self):
        row = dict(ROW, purchase_date=None, invoice_image="0b6c6e3e-0000-4000-8000-000000000001")
        db, _, _ = reader([row])
        [record] = oms_db.db_warranty_source(db)("+919876543210")
        self.assertEqual(record["warranty_api"]["status"], "unknown")
        self.assertTrue(record["invoice_on_file"])
        self.assertEqual(db.invoice_file("+919876543210", "EMXP2025004417"),
                         "0b6c6e3e-0000-4000-8000-000000000001")
        self.assertIsNone(db.invoice_file("+919876543210", "OTHERFRAME"))

    def test_a_cancel_request_is_under_review(self):
        db, _, _ = reader([dict(ROW, status="CANCELLED_REQUEST")])
        [record] = oms_db.db_warranty_source(db)("+919876543210")
        self.assertEqual(record["registration_status"], "cancel_requested")

    def test_configured_by_its_dsn(self):
        self.assertTrue(oms_db.configured({"EMOTORAD_OMS_PG_DSN": "postgresql://x"}))
        self.assertFalse(oms_db.configured({}))
        self.assertIsNone(oms_db.reader_from_env({}))
        self.assertNotIn("postgresql", repr(oms_db.reader_from_env({"EMOTORAD_OMS_PG_DSN": "postgresql://x"})))


if __name__ == "__main__":
    unittest.main()
