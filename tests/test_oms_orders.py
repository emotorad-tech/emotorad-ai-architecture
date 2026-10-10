"""Bikes from OMS orders (spec 2026-10-10): the reader and the record. No
database: a fake connection answers each query by its SQL."""

import unittest
from datetime import date

from emotorad_ai.tools import oms_db

PHONE = "+919876543210"
REG = {"id": "p1", "frame_number": "EMXP0001", "product_name": "EMX Plus", "purchase_date": date(2025, 3, 12),
       "invoice_image": None, "status": None}
ORDER = {"frame_number": "EMORD0001", "product_name": "EMX+ Red White - M042BV01C52",
         "purchase_date": date(2026, 7, 13), "order_source": "End Customer"}


class FakeConnection:
    """Answers REGISTRATIONS_SQL and ORDERS_SQL separately; `errors` makes one fail."""

    def __init__(self, registrations=(), orders=(), errors=None):
        self.answers = {oms_db.REGISTRATIONS_SQL: list(registrations), oms_db.ORDERS_SQL: list(orders)}
        self.errors = dict(errors or {})
        self.calls = []
        self._sql = None

    def __call__(self, dsn, **kwargs):
        self.calls.append(("connect",))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.calls.append(("execute", sql, dict(params)))
        if sql in self.errors:
            raise self.errors[sql]
        self._sql = sql
        return self

    def fetchall(self):
        return [dict(row) for row in self.answers[self._sql]]


def reader(registrations=(), orders=(), errors=None, on=True):
    fake = FakeConnection(registrations, orders, errors)
    clock = [0.0]
    db = oms_db.OMSDatabase("postgresql://ro@oms/emotorad", connect=fake, clock=lambda: clock[0],
                            today=lambda: date(2026, 10, 10), orders=on)
    return db, fake, clock


def executed(fake, sql):
    return [call for call in fake.calls if call[0] == "execute" and call[1] == sql]


class OrdersReaderTests(unittest.TestCase):
    def test_with_the_switch_off_the_orders_query_never_runs(self):
        db, fake, _ = reader(orders=[ORDER], on=False)
        self.assertEqual(db.orders(PHONE), [])
        self.assertEqual(fake.calls, [])

    def test_it_matches_on_the_last_ten_digits(self):
        for phone in ("+919876543210", "9876543210", "09876543210", "98765 43210"):
            db, fake, _ = reader(orders=[ORDER])
            self.assertEqual(db.orders(phone), [ORDER])
            self.assertEqual(executed(fake, oms_db.ORDERS_SQL)[-1][2], {"m10": "9876543210"}, phone)

    def test_a_foreign_number_never_reaches_the_orders_query(self):
        for phone in ("+6591234567", "+447911123456", "12345"):
            db, fake, _ = reader(orders=[ORDER])
            with self.assertRaises(ValueError, msg=phone):
                db.orders(phone)
            self.assertEqual(fake.calls, [], phone)

    def test_a_phone_is_read_once_a_minute(self):
        db, fake, clock = reader(orders=[ORDER])
        db.orders(PHONE)
        db.orders(PHONE)
        self.assertEqual(len(executed(fake, oms_db.ORDERS_SQL)), 1)
        clock[0] += oms_db.CACHE_SECONDS + 1
        db.orders(PHONE)
        self.assertEqual(len(executed(fake, oms_db.ORDERS_SQL)), 2)

    def test_the_two_breakers_are_separate(self):
        db, fake, _ = reader(registrations=[REG], orders=[ORDER], errors={oms_db.ORDERS_SQL: OSError("x")})
        with self.assertRaises(oms_db.OMSDatabaseUnavailable):
            db.orders(PHONE)
        with self.assertRaises(oms_db.OMSDatabaseUnavailable) as caught:
            db.orders(PHONE)
        self.assertEqual(str(caught.exception), "breaker_open")
        self.assertEqual(len(executed(fake, oms_db.ORDERS_SQL)), 1)
        self.assertEqual(db.registrations(PHONE)[0]["frame_number"], "EMXP0001")

        db, fake, _ = reader(registrations=[REG], orders=[ORDER],
                             errors={oms_db.REGISTRATIONS_SQL: OSError("x")})
        with self.assertRaises(oms_db.OMSDatabaseUnavailable):
            db.registrations(PHONE)
        self.assertEqual(db.orders(PHONE), [ORDER])

    def test_an_error_names_the_class_never_the_connection(self):
        db, _, _ = reader(errors={oms_db.ORDERS_SQL: OSError("postgresql://ro@oms/emotorad refused")})
        with self.assertRaises(oms_db.OMSDatabaseUnavailable) as caught:
            db.orders(PHONE)
        self.assertEqual(str(caught.exception), "OSError")


class OrdersSqlTests(unittest.TestCase):
    def test_the_query_keeps_to_the_spec(self):
        sql = oms_db.ORDERS_SQL
        self.assertTrue(sql.startswith("WITH dealer_franchises AS ("))
        for part in ("FROM em_orders o", "o.deleted_at IS NULL", "o.cancel_at IS NULL", "o.is_return IS NOT TRUE",
                     "'Stock Transfer'", "NOT EXISTS (SELECT 1 FROM dealer_franchises)",
                     "em_stock_transactions", "t.frame_status = 'SOLD'", "t.created_at DESC",
                     "FROM em_purchase p", "upper(trim(p.frame_number)) = l.fr",
                     "AT TIME ZONE 'Asia/Kolkata'"):
            self.assertIn(part, sql)

    def test_it_returns_only_the_four_columns(self):
        selected = oms_db.ORDERS_SQL.rsplit(" SELECT l.frame_number", 1)[1].split(" FROM latest")[0]
        self.assertEqual(selected.count(","), 3)
        for name in ("customer", "address", "order_code", "total", "mobile"):
            self.assertNotIn(name, selected)


class RecordTests(unittest.TestCase):
    def test_a_dated_order_bike_gets_its_cover_from_the_invoice_date(self):
        record = oms_db.order_to_record(ORDER, date(2026, 10, 10))
        self.assertEqual(record["frame_number"], "EMORD0001")
        self.assertEqual(record["product_name"], "EMX+ Red White - M042BV01C52")
        self.assertEqual(record["purchase_date"], "2026-07-13")
        self.assertEqual(record["registration_status"], "active")
        self.assertEqual(record["warranty_api"]["status"], "active")
        self.assertFalse(record["invoice_on_file"])
        self.assertFalse(record["invoice_with_support"])
        self.assertEqual(record["term_source"], "oms_terms")
        self.assertEqual(record["ownership_source"], "oms_order")
        for name in ("customer_name", "full_address", "franchise_name", "product_color", "product_id"):
            self.assertIsNone(record[name])

    def test_an_undated_order_bike_has_no_cover_and_no_invoice(self):
        record = oms_db.order_to_record(dict(ORDER, purchase_date=None), date(2026, 10, 10))
        self.assertIsNone(record["purchase_date"])
        self.assertEqual(record["warranty_api"]["status"], "unknown")
        self.assertFalse(record["invoice_on_file"])

    def test_a_registration_says_where_it_came_from(self):
        self.assertEqual(oms_db.to_record(REG, date(2026, 10, 10))["ownership_source"], "oms_purchase")


class SwitchTests(unittest.TestCase):
    def test_the_switch_is_exactly_on_and_needs_the_database(self):
        dsn = {"EMOTORAD_OMS_PG_DSN": "postgresql://x"}
        self.assertTrue(oms_db.reader_from_env(dict(dsn, EMOTORAD_OMS_ORDERS="on")).orders_on)
        self.assertTrue(oms_db.reader_from_env(dict(dsn, EMOTORAD_OMS_ORDERS=" on ")).orders_on)
        for value in ("", "yes", "ON", "true"):
            self.assertFalse(oms_db.reader_from_env(dict(dsn, EMOTORAD_OMS_ORDERS=value)).orders_on, value)
        self.assertIsNone(oms_db.reader_from_env({"EMOTORAD_OMS_ORDERS": "on"}))


from emotorad_ai.tools.registry import ToolError  # noqa: E402


class MergeTests(unittest.TestCase):
    def test_registrations_first_then_order_bikes(self):
        db, _, _ = reader(registrations=[REG], orders=[ORDER])
        records = oms_db.db_warranty_source(db)(PHONE)
        self.assertEqual([r["frame_number"] for r in records], ["EMXP0001", "EMORD0001"])
        self.assertEqual([r["ownership_source"] for r in records], ["oms_purchase", "oms_order"])

    def test_order_bikes_alone_are_the_riders_bikes(self):
        db, _, _ = reader(orders=[ORDER])
        [record] = oms_db.db_warranty_source(db)(PHONE)
        self.assertEqual(record["purchase_date"], "2026-07-13")

    def test_nothing_anywhere_is_no_record(self):
        db, _, _ = reader()
        self.assertIsNone(oms_db.db_warranty_source(db)(PHONE))

    def test_with_the_switch_off_only_registrations(self):
        db, fake, _ = reader(registrations=[REG], orders=[ORDER], on=False)
        records = oms_db.db_warranty_source(db)(PHONE)
        self.assertEqual([r["frame_number"] for r in records], ["EMXP0001"])
        self.assertEqual(executed(fake, oms_db.ORDERS_SQL), [])

    def test_an_orders_failure_returns_the_registrations_and_logs_the_class(self):
        logged = []
        db, _, _ = reader(registrations=[REG], errors={oms_db.ORDERS_SQL: TimeoutError("ro@oms 9876543210")})
        records = oms_db.db_warranty_source(db, log=lambda event, fields: logged.append((event, fields)))(PHONE)
        self.assertEqual([r["frame_number"] for r in records], ["EMXP0001"])
        self.assertEqual(logged, [("oms_orders_unavailable", {"error": "TimeoutError"})])

    def test_an_orders_failure_with_no_registrations_is_an_outage_never_no_record(self):
        """The final review, Important 1: an orders-only rider must not be
        told their bike is unregistered because the orders query failed."""
        logged = []
        db, _, _ = reader(errors={oms_db.ORDERS_SQL: OSError("x")})
        with self.assertRaises(ToolError) as caught:
            oms_db.db_warranty_source(db, log=lambda event, fields: logged.append(event))(PHONE)
        self.assertEqual(caught.exception.code, "oms_unavailable")
        self.assertEqual(logged, ["oms_orders_unavailable"])

    def test_the_bike_row_falls_back_to_an_order_bike(self):
        """The final review, Important 2: the invoice reader finds an order
        bike, so the rider's uploaded invoice is read by code."""
        undated = dict(ORDER, purchase_date=None)
        db, _, _ = reader(registrations=[REG], orders=[undated])
        self.assertEqual(db.row(PHONE, "EMXP0001")["id"], "p1")
        self.assertEqual(db.row(PHONE, "EMORD0001"), undated)
        self.assertIsNone(db.row(PHONE, "OTHER"))
        self.assertIsNone(db.invoice_file(PHONE, "EMORD0001"))

    def test_an_undated_order_bike_is_never_called_registered(self):
        from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, build_registry
        from emotorad_ai.tools.registry import ToolContext

        record = oms_db.order_to_record(dict(ORDER, purchase_date=None), date(2026, 10, 10))
        registry = build_registry(today=date(2026, 10, 10), warranty_source=lambda phone: [record])
        envelope = registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c", phone=PHONE))
        [bike] = envelope["data"]["bikes"]
        self.assertEqual(bike["coverage_status"], "purchase_date_missing")
        self.assertNotIn("registered", bike["note"])
        self.assertIn("Ask for the invoice", bike["note"])

    def test_a_registrations_failure_is_an_outage_whatever_the_orders_did(self):
        db, _, _ = reader(orders=[ORDER], errors={oms_db.REGISTRATIONS_SQL: OSError("x")})
        with self.assertRaises(ToolError) as caught:
            oms_db.db_warranty_source(db)(PHONE)
        self.assertEqual(caught.exception.code, "oms_unavailable")

    def test_a_foreign_number_is_no_record(self):
        db, fake, _ = reader(orders=[ORDER])
        self.assertIsNone(oms_db.db_warranty_source(db)("+6591234567"))
        self.assertEqual(fake.calls, [])

if __name__ == "__main__":
    unittest.main()
