# Bikes from OMS orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A verified rider also gets the bikes sold on OMS orders carrying their phone, behind `EMOTORAD_OMS_ORDERS=on`.

**Architecture:** A second read-only query in `tools/oms_db.py` (`ORDERS_SQL`) finds the rider's orders by the phone's last ten digits, takes each frame whose latest SOLD stock transaction is one of those orders, and drops frames registered in `em_purchase`. `OMSDatabase.orders()` runs it with its own cache and breaker; `db_warranty_source` appends the order bikes after the registrations, and an orders failure is logged and never stops registrations.

**Tech Stack:** Python 3, stdlib `unittest`, psycopg 3 (production only; tests use a fake connection).

**Spec:** `docs/superpowers/specs/2026-10-10-oms-order-bikes-design.md`

## Global Constraints

- The switch is `EMOTORAD_OMS_ORDERS`, exactly `on`, and only with `EMOTORAD_OMS_PG_DSN` set.
- Orders: `mobile` matched by last ten digits; `deleted_at` null; `cancel_at` null; `is_return` not true; `order_source` not `Stock Transfer`; none at all when the phone is a dealer's.
- A frame counts only when its latest SOLD transaction with an order code is one of the rider's orders.
- A frame with any `em_purchase` row (`deleted_at` null), under any number, is dropped. Frames compared trimmed and upper-cased.
- `purchase_date` is `(invoice_at AT TIME ZONE 'Asia/Kolkata')::date`.
- Columns returned: `frame_number`, `product_name`, `purchase_date`, `order_source`. No order code, name, address or amount.
- Records: `ownership_source` is `oms_order` for orders and `oms_purchase` for registrations.
- The phone and the connection string are never logged; `oms_orders_unavailable` carries `error=<class or breaker_open>` only.
- `/health` gains `"oms_orders": "on" | "off"`.
- Rollback: remove `-e EMOTORAD_OMS_ORDERS=on` from `deploy-staging.yml` and redeploy.
- Tests: `python3 -m unittest discover -s tests -t .`; no network, fake connections only; the real SQL only in the opt-in test.

## Review Focus

1. The orders query is slow or times out for one phone (a company buyer with many orders): registrations must still be returned, and the failure logged without the phone. Pinned in Task 2 (`test_an_orders_failure_returns_the_registrations_and_logs_the_class`).
2. A frame stored with different case or spaces in `em_purchase` and `em_stock_transactions`: registration must still win. Pinned in Task 1's SQL test (`f9 ` against `F9`).
3. An invoice in the late evening India time: the purchase date must be the India date, not the UTC one. Pinned in Task 1's SQL test (O2, 20:00 UTC).
4. A foreign number: the orders query must never run. Pinned in Task 1 (`test_a_foreign_number_never_reaches_the_orders_query`).
5. Registrations' breaker open while orders are healthy, or the reverse: neither blocks the other. Pinned in Task 1 (`test_the_two_breakers_are_separate`).

---

### Task 1: The orders query, its reader and its record

**Files:**
- Modify: `src/emotorad_ai/tools/oms_db.py`
- Create: `tests/test_oms_orders.py`
- Create: `tests/test_oms_orders_sql.py`

**Interfaces:**
- Produces:
  - `oms_db.ORDERS_ENV = "EMOTORAD_OMS_ORDERS"`
  - `oms_db.ORDERS_SQL: str` (starts with `"WITH dealer_franchises AS ("`)
  - `OMSDatabase.__init__(self, dsn, connect=None, clock=time.monotonic, today=date.today, orders: bool = False)`; attribute `orders_on: bool`
  - `OMSDatabase.orders(phone: str) -> List[Dict[str, Any]]` (`[]` when `orders_on` is False; raises `ValueError` for a non-Indian mobile and `OMSDatabaseUnavailable` on failure)
  - `oms_db.order_to_record(row: Dict[str, Any], today: date) -> Dict[str, Any]`
  - `to_record(...)` now also returns `"ownership_source": "oms_purchase"`
  - `reader_from_env(environ)` passes `orders=(environ.get(ORDERS_ENV, "").strip() == "on")`

- [ ] **Step 1: Write the failing unit tests** (`tests/test_oms_orders.py`)

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_oms_orders -v`
Expected: FAIL/ERROR: `AttributeError: module 'emotorad_ai.tools.oms_db' has no attribute 'ORDERS_SQL'` (and `unexpected keyword argument 'orders'`).

- [ ] **Step 3: Implement in `src/emotorad_ai/tools/oms_db.py`**

Add after `APPLICATION_NAME`:

```python
ORDERS_ENV = "EMOTORAD_OMS_ORDERS"
```

Replace the `REGISTRATIONS_SQL` definition with a shared dealer check and both queries (the registrations query's text is unchanged):

```python
_LAST10 = "right(regexp_replace(coalesce({col}, ''), '[^0-9]', '', 'g'), 10)"
# The phone's own dealerships: its em_franchise number, or a dealer login in
# em_users. Registrations sold by them are left out; orders give nothing.
_DEALER_FRANCHISES = (
    "WITH dealer_franchises AS ("
    " SELECT id FROM em_franchise WHERE deleted_at IS NULL"
    " AND (" + _LAST10.format(col="mobile") + " = %(m10)s"
    " OR " + _LAST10.format(col="secondary_contact") + " = %(m10)s)"
    " UNION"
    " SELECT related_id FROM em_users WHERE deleted_at IS NULL AND related_id IS NOT NULL"
    " AND user_type IN ('franchise_manager', 'sale_franchise_person')"
    " AND " + _LAST10.format(col="mobile") + " = %(m10)s)"
)
REGISTRATIONS_SQL = (
    _DEALER_FRANCHISES +
    " SELECT DISTINCT ON (p.frame_number)"
    " p.id, p.frame_number, p.product_name, p.product_id, p.product_color, p.purchase_date, p.created_at,"
    " p.franchise_id, p.franchise_name, p.invoice_image, p.status, p.customer_name, p.full_address"
    " FROM em_purchase p"
    " WHERE p.deleted_at IS NULL"
    " AND " + _LAST10.format(col="p.mobile") + " = %(m10)s"
    " AND coalesce(p.frame_number, '') <> ''"
    " AND (p.franchise_id IS NULL OR p.franchise_id NOT IN (SELECT id FROM dealer_franchises))"
    " ORDER BY p.frame_number, (p.purchase_date IS NULL), p.updated_at DESC"
)
# Bikes from OMS orders (spec 2026-10-10): the phone on an order is taken as
# the rider's. A frame is the rider's when its latest SOLD transaction with an
# order code is one of their orders, and nobody has registered it.
ORDERS_SQL = (
    _DEALER_FRANCHISES + ","
    " rider_orders AS ("
    " SELECT o.order_code, o.invoice_at, o.order_source FROM em_orders o"
    " WHERE o.deleted_at IS NULL AND o.cancel_at IS NULL AND o.is_return IS NOT TRUE"
    " AND coalesce(o.order_source, '') <> 'Stock Transfer'"
    " AND coalesce(o.order_code, '') <> ''"
    " AND " + _LAST10.format(col="o.mobile") + " = %(m10)s"
    " AND NOT EXISTS (SELECT 1 FROM dealer_franchises)),"
    " frames AS ("
    " SELECT DISTINCT upper(trim(t.frame_number)) AS fr FROM em_stock_transactions t"
    " WHERE t.order_code IN (SELECT order_code FROM rider_orders) AND t.frame_status = 'SOLD'"
    " AND coalesce(trim(t.frame_number), '') <> ''),"
    " latest AS ("
    " SELECT DISTINCT ON (upper(trim(t.frame_number))) upper(trim(t.frame_number)) AS fr,"
    " trim(t.frame_number) AS frame_number, t.product_name, t.order_code"
    " FROM em_stock_transactions t"
    " WHERE upper(trim(t.frame_number)) IN (SELECT fr FROM frames) AND t.frame_status = 'SOLD'"
    " AND coalesce(t.order_code, '') <> ''"
    " ORDER BY upper(trim(t.frame_number)), t.created_at DESC)"
    " SELECT l.frame_number, l.product_name, (r.invoice_at AT TIME ZONE 'Asia/Kolkata')::date AS purchase_date,"
    " r.order_source FROM latest l JOIN rider_orders r ON r.order_code = l.order_code"
    " WHERE NOT EXISTS (SELECT 1 FROM em_purchase p WHERE p.deleted_at IS NULL"
    " AND upper(trim(p.frame_number)) = l.fr)"
    " ORDER BY l.frame_number"
)
```

Replace `OMSDatabase.__init__` and `registrations` with a shared reader:

```python
class OMSDatabase:
    def __init__(self, dsn: str, connect: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.monotonic, today: Callable[[], date] = date.today,
                 orders: bool = False) -> None:
        self._dsn = dsn
        self._connect = connect or _psycopg_connect
        self._clock = clock
        self.today = today
        # Bikes from OMS orders (spec 2026-10-10), behind EMOTORAD_OMS_ORDERS.
        self.orders_on = orders
        # One cache and one breaker per query, so a failing orders query never
        # stops registrations, nor the reverse.
        self._cache: Dict[str, Dict[str, Tuple[float, List[Dict[str, Any]]]]] = {"registrations": {},
                                                                                 "orders": {}}
        self._failed_at: Dict[str, Optional[float]] = {"registrations": None, "orders": None}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "OMSDatabase(dsn=set)"

    def registrations(self, phone: str) -> List[Dict[str, Any]]:
        """The phone's registrations as a customer, one per frame. Raises
        ValueError for a number that is not an Indian mobile (never sent) and
        OMSDatabaseUnavailable when the database cannot be read."""
        return self._read("registrations", REGISTRATIONS_SQL, phone)

    def orders(self, phone: str) -> List[Dict[str, Any]]:
        """The bikes on the phone's orders that nobody registered (spec
        2026-10-10), one per frame; [] with the switch off. Raises as
        `registrations` does."""
        if not self.orders_on:
            return []
        return self._read("orders", ORDERS_SQL, phone)

    def _read(self, name: str, sql: str, phone: str) -> List[Dict[str, Any]]:
        m10 = last_ten(phone)
        now = self._clock()
        with self._lock:
            failed_at = self._failed_at[name]
            if failed_at is not None and now - failed_at < FAILURE_TTL_SECONDS:
                raise OMSDatabaseUnavailable("breaker_open")
            cached = self._cache[name].get(m10)
            if cached and now - cached[0] < CACHE_SECONDS:
                return [dict(row) for row in cached[1]]
        try:
            with self._connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS,
                               application_name=APPLICATION_NAME,
                               options="-c statement_timeout=%d -c default_transaction_read_only=on -c TimeZone=UTC"
                                       % STATEMENT_TIMEOUT_MS) as conn:
                rows = [dict(row) for row in conn.execute(sql, {"m10": m10}).fetchall()]
        except Exception as exc:
            with self._lock:
                self._failed_at[name] = now
            raise OMSDatabaseUnavailable(type(exc).__name__) from None
        with self._lock:
            self._failed_at[name] = None
            self._cache[name][m10] = (now, rows)
        return [dict(row) for row in rows]
```

(`invoice_file` and `row` stay as they are; they call `registrations`.)

In `to_record`, add after `"term_source": "oms_terms",`:

```python
        "ownership_source": "oms_purchase",
```

Add after `to_record`:

```python
def order_to_record(row: Dict[str, Any], today: date) -> Dict[str, Any]:
    """A bike from an order (spec 2026-10-10) as the record the lookup tool
    reads, in `to_record`'s shape: the invoice date as the purchase date, and
    no invoice on file (an order bike has no OMS invoice file)."""
    bought = _day(row.get("purchase_date"))
    return {
        "frame_number": row.get("frame_number"),
        "product_name": row.get("product_name"),
        "product_color": None,
        "product_id": None,
        "franchise_name": None,
        "full_address": None,
        "customer_name": None,
        "purchase_date": bought.isoformat() if bought else None,
        "registration_status": "active",
        "invoice_on_file": False,
        "invoice_with_support": False,
        "term_source": "oms_terms",
        "ownership_source": "oms_order",
        "warranty_api": warranty_terms.coverage(bought, today),
    }
```

Replace `reader_from_env`:

```python
def reader_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OMSDatabase]:
    env = os.environ if environ is None else environ
    dsn = (env.get(DSN_ENV) or "").strip()
    if not dsn:
        return None
    return OMSDatabase(dsn, orders=(env.get(ORDERS_ENV) or "").strip() == "on")
```

Add to the module docstring, after its first paragraph:

```
With EMOTORAD_OMS_ORDERS=on, a second query (ORDERS_SQL, spec 2026-10-10)
gives the bikes on the phone's orders that nobody registered, with its own
cache and breaker.
```

- [ ] **Step 4: Run the unit tests and the existing OMS tests**

Run: `python3 -m unittest tests.test_oms_orders tests.test_oms_db tests.test_oms_db_sql tests.test_oms_db_tool -v`
Expected: all PASS (`test_oms_db_sql`'s real-SQL class skipped without `EMOTORAD_TEST_OMS_PG_DSN`).

- [ ] **Step 5: Write the opt-in SQL test** (`tests/test_oms_orders_sql.py`)

```python
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
```

(F3 stock transfer, F4 cancelled, F5 returned, F6 deleted and F9 registered as `f9 ` are all absent from the first assertion.)

- [ ] **Step 6: Run it**

Run: `python3 -m unittest tests.test_oms_orders_sql -v`
Expected: `FixtureTests` PASS; `RealSqlTests` skipped without the DSN. With a local Postgres (`EMOTORAD_TEST_OMS_PG_DSN=postgresql://localhost/postgres`), all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/tools/oms_db.py tests/test_oms_orders.py tests/test_oms_orders_sql.py
git commit -m "feat(oms-orders): the orders query, its reader with its own breaker, and the order record"
```

---

### Task 2: Merge order bikes into the warranty source

**Files:**
- Modify: `src/emotorad_ai/tools/oms_db.py` (`db_warranty_source`)
- Modify: `tests/test_oms_orders.py`
- Create: `tests/test_oms_orders_runtime.py`

**Interfaces:**
- Consumes: `OMSDatabase.orders`, `order_to_record`, `ORDERS_SQL`, `REGISTRATIONS_SQL` (Task 1)
- Produces: `db_warranty_source(reader, invoice_state=None, log: Optional[Callable[[str, Dict[str, Any]], None]] = None)`; logs `("oms_orders_unavailable", {"error": <str>})`

- [ ] **Step 1: Write the failing merge tests** (append to `tests/test_oms_orders.py`, before `if __name__`)

```python
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

    def test_an_orders_failure_with_no_registrations_is_no_record(self):
        db, _, _ = reader(errors={oms_db.ORDERS_SQL: OSError("x")})
        self.assertIsNone(oms_db.db_warranty_source(db, log=lambda event, fields: None)(PHONE))

    def test_a_registrations_failure_is_an_outage_whatever_the_orders_did(self):
        db, _, _ = reader(orders=[ORDER], errors={oms_db.REGISTRATIONS_SQL: OSError("x")})
        with self.assertRaises(ToolError) as caught:
            oms_db.db_warranty_source(db)(PHONE)
        self.assertEqual(caught.exception.code, "oms_unavailable")

    def test_a_foreign_number_is_no_record(self):
        db, fake, _ = reader(orders=[ORDER])
        self.assertIsNone(oms_db.db_warranty_source(db)("+6591234567"))
        self.assertEqual(fake.calls, [])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_oms_orders.MergeTests -v`
Expected: FAIL: `test_registrations_first_then_order_bikes` (only one record), `test_order_bikes_alone...` (None), `...orders_failure...` (`unexpected keyword argument 'log'`).

- [ ] **Step 3: Implement** — replace `db_warranty_source` in `oms_db.py`:

```python
def db_warranty_source(reader: OMSDatabase, invoice_state: Optional[Callable[[Optional[str]], str]] = None,
                       log: Optional[Callable[[str, Dict[str, Any]], None]] = None
                       ) -> Callable[[str], Optional[List[Dict[str, Any]]]]:
    """Registered bikes from the OMS database, then the bikes on the phone's
    orders that nobody registered (spec 2026-10-10), mapped onto the tool's
    own outcomes: rows, None for no rows, and oms_unavailable when the
    registrations cannot be read. An orders failure is logged (its class,
    never the phone) and the registrations stand alone."""
    from .registry import ToolError  # local: registry imports tools, not the reverse

    def source(phone: str) -> Optional[List[Dict[str, Any]]]:
        try:
            rows = reader.registrations(phone)
        except ValueError:
            return None
        except OMSDatabaseUnavailable as exc:
            raise ToolError("oms_unavailable", "The warranty system is not responding (%s)." % exc,
                            retryable=True)
        try:
            order_rows = reader.orders(phone)
        except OMSDatabaseUnavailable as exc:
            order_rows = []
            if log is not None:
                log("oms_orders_unavailable", {"error": str(exc)})
        today = reader.today()
        records = ([to_record(row, today, invoice_state) for row in rows]
                   + [order_to_record(row, today) for row in order_rows])
        return records or None

    return source
```

- [ ] **Step 4: Write the runtime test** (`tests/test_oms_orders_runtime.py`)

```python
"""A rider whose only bike is on an OMS order, through runtime.handle()
(spec 2026-10-10, section 7)."""

import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import oms_db
from emotorad_ai.tools.mocks import build_registry
from tests.test_oms_orders import FakeConnection

TODAY = date(2026, 10, 10)
ORDER = {"frame_number": "EMORD0001", "product_name": "EMX+ Red White - M042BV01C52",
         "purchase_date": date(2026, 7, 13), "order_source": "End Customer"}


def make(responses, orders=(ORDER,), on=True):
    db = oms_db.OMSDatabase("postgresql://ro@oms/emotorad", connect=FakeConnection(orders=orders),
                            today=lambda: TODAY, orders=on)
    registry = build_registry(today=TODAY, warranty_source=oms_db.db_warranty_source(db))
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True,
                      warranty_step=True)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm


def send(runtime, adapter, text, seen=False):
    state = runtime.conversations.get("conv-ord")
    state.route_to(BATTERY)
    state.selected_frame = "EMORD0001"
    state.evidence_seen = seen
    return runtime.handle(adapter.to_message({"conversation_id": "conv-ord", "session_token": "sess-ananya",
                                              "text": text}))


class OrderBikeTests(unittest.TestCase):
    def test_the_order_bike_is_the_riders_bike(self):
        runtime, adapter, llm = make([say("Let's check the charger first.")])
        send(runtime, adapter, "my battery won't charge")
        self.assertIn("EMORD0001", llm.requests[0]["system"])

    def test_the_warranty_step_is_case_one_not_unregistered(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault in your video.")])
        reply = send(runtime, adapter, "here is the video", seen=True)
        state = runtime.conversations.get("conv-ord")
        self.assertEqual(state.warranty_step_frames, ["EMORD0001"])
        self.assertIn("from_warranty_api", json.dumps(state.coverage_result))
        self.assertNotIn("isn't registered", reply.text)
        self.assertEqual(reply.actions, [])

    def test_with_the_switch_off_the_rider_has_no_bike(self):
        runtime, adapter, llm = make([say("Tell me what is happening.")], on=False)
        send(runtime, adapter, "my battery won't charge")
        self.assertNotIn("EMORD0001", llm.requests[0]["system"])
```

- [ ] **Step 5: Run the merge and runtime tests**

Run: `python3 -m unittest tests.test_oms_orders tests.test_oms_orders_runtime tests.test_oms_db -v`
Expected: all PASS. If `test_the_order_bike_is_the_riders_bike` fails because a selected frame outside the resolved list is dropped, the merge is not reaching the resolver: check `build_registry(warranty_source=...)` is the source the resolver reads.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/tools/oms_db.py tests/test_oms_orders.py tests/test_oms_orders_runtime.py
git commit -m "feat(oms-orders): order bikes after the registrations; an orders failure never stops them"
```

---

### Task 3: The switch in the API, /health, the deploy and the docs

**Files:**
- Modify: `src/emotorad_ai/api.py` (the `db_warranty_source` call around line 342; `health()` around line 657)
- Modify: `tests/test_api_health.py` (pinned body around line 49, and its env blanks)
- Modify: `tests/test_api_oms_db.py`
- Modify: `.github/workflows/deploy-staging.yml` (line 102)
- Modify: `docs/runbooks/config-store.md` (section 9)
- Modify: `docs/Emotorad_Edge_Case_Register.md` (new section 9 before "What to do with CAPTURE items")
- Modify: `CLAUDE.md` (a rule after "Bikes and warranty from OMS production")

**Interfaces:**
- Consumes: `OMSDatabase.orders_on`, `db_warranty_source(..., log=...)` (Tasks 1 and 2)
- Produces: `/health` `"oms_orders"`

- [ ] **Step 1: Write the failing API tests** (append to `SourceTests` in `tests/test_api_oms_db.py`, and add `"EMOTORAD_OMS_ORDERS": ""` to its `setUp` cleanup dict)

```python
    def test_orders_are_on_with_the_switch_and_the_database(self):
        with mock.patch.object(oms_db, "_psycopg_connect", never_connect):
            api = fresh_api(dict(DSN, EMOTORAD_OMS_ORDERS="on"))
        self.assertTrue(api.OMS_DB.orders_on)
        self.assertEqual(api.health()["oms_orders"], "on")

    def test_orders_are_off_without_the_switch(self):
        with mock.patch.object(oms_db, "_psycopg_connect", never_connect):
            api = fresh_api(dict(DSN, EMOTORAD_OMS_ORDERS=""))
        self.assertEqual(api.health()["oms_orders"], "off")

    def test_orders_are_off_without_the_database(self):
        api = fresh_api({"EMOTORAD_OMS_PG_DSN": "", "EMOTORAD_OMS_ORDERS": "on", "EMOTORAD_OMS_API_KEY": "",
                         "EMOTORAD_WARRANTY_API_KEY": ""})
        self.assertEqual(api.health()["oms_orders"], "off")
```

In `tests/test_api_health.py`, add `"EMOTORAD_OMS_ORDERS": ""` to the env dict of `test_offline_reports_no_secret`, and add `"oms_orders": "off",` to the pinned body right after `"warranty_source": "fixtures",`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_api_oms_db tests.test_api_health -v`
Expected: FAIL with `KeyError: 'oms_orders'` and the pinned-body mismatch.

- [ ] **Step 3: Implement in `src/emotorad_ai/api.py`**

In `_build_registry`, pass the log to `db_warranty_source`:

```python
        source = oms_db_tools.db_warranty_source(
            OMS_DB, invoice_state=lambda file_id: (INVOICE.invoice_state(file_id) if INVOICE is not None
                                                   else invoice_ocr.InvoiceService.UNREADABLE),
            # Bikes from OMS orders (spec 2026-10-10): a failing orders query
            # is logged by its class and the registrations stand alone.
            log=lambda event, fields: log.emit(event, "oms_orders", **fields))
```

In `health()`, after the `"warranty_source": WARRANTY_SOURCE,` line:

```python
        "oms_orders": "on" if OMS_DB is not None and OMS_DB.orders_on else "off",
```

- [ ] **Step 4: Run them to verify they pass**

Run: `python3 -m unittest tests.test_api_oms_db tests.test_api_health -v`
Expected: PASS.

- [ ] **Step 5: The deploy flag**

In `.github/workflows/deploy-staging.yml` line 102, insert `-e EMOTORAD_OMS_ORDERS=on ` right after `-e EMOTORAD_WARRANTY_STEP=on `.

- [ ] **Step 6: The runbook** — in `docs/runbooks/config-store.md` section 9, after the nearest-dealers grants block and its `/health` paragraph (before "2. **The network path (Sachin).**"), add:

````markdown
   Bikes from OMS orders (spec 2026-10-10, `EMOTORAD_OMS_ORDERS=on`, set by `deploy-staging.yml`)
   read the phone on each order, so the role also needs:
   ```sql
   GRANT SELECT (order_code, mobile, order_source, invoice_at, cancel_at, is_return, deleted_at) ON em_orders TO <role>;
   GRANT SELECT (frame_number, frame_status, order_code, product_name, created_at) ON em_stock_transactions TO <role>;
   ```
   If a column name is wrong the `GRANT` fails: tell the server team the right name before going on.
   Without these grants every orders read fails on permission, `oms_orders_unavailable` is logged
   (`error` `InsufficientPrivilege`, then `breaker_open` for a minute), and riders get their registered
   bikes only. `/health` shows `"oms_orders":"on"`. Before real riders: Sachin's yes to taking the
   phone on an order as the rider's. Rollback: remove `-e EMOTORAD_OMS_ORDERS=on` and redeploy.
````

- [ ] **Step 7: The edge case register** — in `docs/Emotorad_Edge_Case_Register.md`, before `## What to do with CAPTURE items`, add:

```markdown
## 9. Bikes from OMS orders (2026-10-10)

Spec 2026-10-10: the phone on an `em_orders` row is taken as the rider's. Where that is wrong, a rider sees a bike that is not theirs, or misses their own.

| # | Case | Disposition | Notes |
|---|---|---|---|
| 9.1 | The phone on the order is the buyer's, not the rider's: a gift, a company purchase, a family member | **CAPTURE** | The rider who bought it sees the bike; the one who rides it does not until it is registered |
| 9.2 | A dealer's number missing from `em_franchise` and `em_users` | **CAPTURE** | That dealer, verifying on it, sees the bikes on their orders as their own |
| 9.3 | A marketplace order whose phone is masked or a relay number | **CAPTURE** | Nobody's verified phone matches it, so the bike is found by nobody |
| 9.4 | A frame swapped out in a replacement (`em_order_rr`) still shows on the original order | **CAPTURE** | The rider may see the old frame beside the new one |
| 9.5 | For a dealer order, `invoice_at` is when the dealer bought the bike | **CAPTURE** | The cover shown may end before the real one; a rider who disagrees sends their invoice |
```

- [ ] **Step 8: CLAUDE.md** — after the "Bikes and warranty from OMS production" rule, add:

```markdown
- **Bikes from OMS orders** (spec 2026-10-10, `docs/superpowers/specs/2026-10-10-oms-order-bikes-design.md`): with `EMOTORAD_OMS_ORDERS=on` (exactly, set by `deploy-staging.yml`) and the OMS database, a verified rider also gets the bikes sold on orders carrying their phone (`tools/oms_db.ORDERS_SQL`): `em_orders.mobile` by its last ten digits, not deleted, cancelled or returned, not `Stock Transfer`, nothing when the phone is a dealer's; a frame counts when its latest SOLD `em_stock_transactions` row with an order code is one of those orders; a frame in `em_purchase` under any number is left out (registration wins). The purchase date is the order's `invoice_at` as an India date, so cover is worked out at once; no `invoice_at` is an undated bike with no invoice on file. Records carry `ownership_source` (`oms_order` or `oms_purchase`) and come after the registrations; the orders query has its own cache and breaker, and a failure logs `oms_orders_unavailable` (the class only) and leaves the registrations. The phone on an order is assumed to be the rider's: the gaps are in the Edge Case Register §9, and **Sachin signs off before real riders**. Grants: `docs/runbooks/config-store.md` section 9. SQL pinned by `tests/test_oms_orders_sql.py` (opt-in, `EMOTORAD_TEST_OMS_PG_DSN`). `/health` `oms_orders`. Rollback: remove the deploy flag.
```

- [ ] **Step 9: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass except the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`.

- [ ] **Step 10: Commit**

```bash
git add src/emotorad_ai/api.py tests/test_api_health.py tests/test_api_oms_db.py .github/workflows/deploy-staging.yml docs/runbooks/config-store.md docs/Emotorad_Edge_Case_Register.md CLAUDE.md
git commit -m "feat(oms-orders): the switch, /health, the deploy, the grants and the edge cases"
```
