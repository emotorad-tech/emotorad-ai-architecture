# Bikes and warranty from OMS production, with invoice OCR: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a customer's bikes and per-part warranty come from the OMS production database, and a missing purchase date is resolved by reading the invoice (from OMS, or uploaded by the customer) with Gemini Flash and raising a warranty-proof ticket.

**Architecture:** a read-only Postgres reader (`tools/oms_db.py`) returns a phone's registrations, less the dealer's own-shop sales, in the record shape the warranty service already used, with coverage computed by `warranty_terms.py`. An `InvoiceOCR` service (`invoice_ocr.py`) reads an invoice (downloaded from OMS with the API key, or a customer upload), keeps the reading in `invoice_readings`, and the runtime raises the ticket and writes the customer's line in code.

**Tech Stack:** Python 3.12, psycopg 3 (already a dependency), OpenRouter (Gemini Flash, the `photo_check.py` transport), MongoDB/mongomock, stdlib `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-08-oms-warranty-source-design.md`

## Global Constraints

- Tests: `env -u EMOTORAD_AI_MODE -u EMOTORAD_STORE -u EMOTORAD_SERIAL_ASK -u EMOTORAD_MELT_ASK -u EMOTORAD_EVIDENCE_CHECK -u EMOTORAD_MONGO_URI -u OPENROUTER_API_KEY -u EMOTORAD_ZOHO_WEBHOOK_SECRET -u EMOTORAD_JEV -u EMOTORAD_OMS_PG_DSN -u EMOTORAD_OMS_API_KEY .venv/bin/python -m unittest discover -s tests -t .` from the worktree, using the main checkout's `.venv`. The one known macOS failure is `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`.
- No network, no AWS, no real database in the default suite. A real Postgres check is opt-in only.
- Never use `\b` or `\w` alone on text that may be Indic; never write a regex through a shell heredoc (edit the file directly).
- Never log a phone, a serial, an invoice date, an amount or a connection string; log outcomes and codes.
- The four warranty outcomes never collapse: coverage computed, `purchase_date_missing`, `no_warranty_record`, `oms_unavailable`.
- Terms, verbatim from the spec: display 6 months, charger 6, controller 12, battery 12, motor 12, frame 60. The bike's `in_warranty` follows the battery.
- `valid_until` = the day before the same calendar date `months` later.
- Confident OCR = one day-first date, not in the future, not before 1 January 2019; one frame number on the invoice equal to the bike's (ignoring case, spaces and dashes); `legible` true.
- Customer lines (verbatim, English): confident: "Going by your invoice dated {date}, your {lead part} is covered until {lead end}. A support executive will confirm this." Not confident: "Thanks, I have passed your invoice to our support team. A support executive will confirm your warranty." Dates as "12 March 2025".
- The OMS API key is used only for invoice downloads; the order-number account finder does not use it while `EMOTORAD_AI_DEV_CODES=1`.
- British English, no em dashes in new prose.

## Rulings made while planning

- **`full_address` is selected too**: the replacement flow reads it back to the customer before placing an order (`tools/mocks._coverage`, `delivery_address`), as the OMS API path did. Cost if wrong: one more personal column read for the customer's own record; drop it and replacements lose the address read-back.
- **The date post-check from `feat/warranty-status` (spec section 3, "comes across with this work") is deferred to its own plan.** It is 1,134 changed lines in `guardrails.py`, a Tier-1 file needing human review. This plan ports only `invoice_dates.py` and `textfold.py` (pure, tested) for the OCR. Mitigation meanwhile: the OCR date never enters a tool result; the model sees it only in the code-written line in history. Cost if wrong: a model could restate or rework the date in a later turn unchecked.
- **OMS `status`**: `CANCELLED_REQUEST` maps to a registration under review (no coverage stated); null, `CREATED` and `UPDATE_REQUEST` are active.
- **The confident line names the battery's cover only**, not "the part the chat is about" and not "the other parts as their terms run" (spec section 3): the battery is the bike's lead part everywhere else, and one date is clearer in chat. Cost if wrong: a motor chat hears the battery's date; widen `customer_line` to take a part.
- **The reader returns `is_invoice`** so a fault photo sent while an invoice is awaited never raises a warranty ticket.

## Review Focus

1. A phone stored as `+91…`, `0…`, `91…` or with spaces must still find its bikes (Task 2 test).
2. A dealer's own-shop registrations must vanish while another seller's stay (Task 2 test).
3. An OMS that is down must read as `oms_unavailable`, never "no record" (Task 2 test).
4. A fault photo sent while the bot awaits an invoice must not raise a warranty ticket (Task 6 and Task 8 tests).
5. The same invoice read twice raises one ticket and one line (Task 8 test).

---

## File structure

| File | Responsibility |
|---|---|
| `src/emotorad_ai/warranty_terms.py` (new) | Terms per part; cover from a purchase date |
| `src/emotorad_ai/tools/oms_db.py` (new) | Read-only OMS Postgres reader; the warranty source |
| `src/emotorad_ai/tools/oms.py` | Add `download_file`; account finder respects dev codes |
| `src/emotorad_ai/tools/mocks.py` | `purchase_date_missing` carries `invoice_on_file`; `submit_warranty_proof` takes code-injected findings |
| `src/emotorad_ai/textfold.py`, `invoice_dates.py` (ported) | Fold text; read dates day-first |
| `src/emotorad_ai/invoice_ocr.py` (new) | Gemini reader, confidence, the service, the customer's line |
| `src/emotorad_ai/conversation.py`, `stores/mongo.py` | `invoice_readings` store, erasure |
| `src/emotorad_ai/api.py` | Source order, `/health`, the reads before the turn |
| `src/emotorad_ai/runtime.py` | Ticket and line by code after the agent |
| `scripts/mongo_setup.py`, `erasure_admin.py` | Permanent collection |
| docs | CLAUDE.md, runbook |

---

### Task 1: Warranty terms

**Files:**
- Create: `src/emotorad_ai/warranty_terms.py`
- Test: `tests/test_warranty_terms.py`

**Interfaces:**
- Produces: `TERMS: Dict[str, int]`; `LEAD_PART = "battery"`; `valid_until(start: date, months: int) -> date`; `coverage(purchase_date: Optional[date], today: date) -> Dict[str, Any]` returning the warranty-service coverage shape `{"status": "active"|"expired"|"unknown", "remedy": None|"collect_purchase_proof", "components": [{"component", "months", "validUntil" (ISO str), "active"}]}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Warranty from the purchase date, per part (spec 2026-10-08, section 2)."""

import unittest
from datetime import date

from emotorad_ai.warranty_terms import LEAD_PART, TERMS, coverage, valid_until


class TermsTests(unittest.TestCase):
    def test_the_terms_are_the_persons(self):
        self.assertEqual(TERMS, {"display": 6, "charger": 6, "controller": 12, "battery": 12, "motor": 12,
                                 "frame": 60})
        self.assertEqual(LEAD_PART, "battery")

    def test_cover_ends_the_day_before_the_same_date(self):
        self.assertEqual(valid_until(date(2025, 3, 12), 12), date(2026, 3, 11))
        self.assertEqual(valid_until(date(2025, 1, 31), 6), date(2025, 7, 30))
        self.assertEqual(valid_until(date(2024, 2, 29), 12), date(2025, 2, 27))
        self.assertEqual(valid_until(date(2025, 8, 31), 6), date(2026, 2, 27))

    def test_each_part_is_active_until_its_own_end(self):
        result = coverage(date(2025, 3, 12), today=date(2025, 10, 1))
        parts = {p["component"]: p for p in result["components"]}
        self.assertTrue(parts["battery"]["active"])
        self.assertFalse(parts["display"]["active"])  # ended 11 September 2025
        self.assertEqual(parts["frame"]["validUntil"], "2030-03-11")
        self.assertEqual(result["status"], "active")

    def test_the_status_follows_the_battery(self):
        self.assertEqual(coverage(date(2024, 1, 1), today=date(2025, 6, 1))["status"], "expired")

    def test_the_last_day_is_still_covered(self):
        self.assertTrue({p["component"]: p for p in coverage(date(2025, 3, 12), today=date(2026, 3, 11))[
            "components"]}["battery"]["active"])

    def test_no_purchase_date_is_unknown_with_the_remedy(self):
        self.assertEqual(coverage(None, today=date(2025, 6, 1)),
                         {"status": "unknown", "remedy": "collect_purchase_proof", "components": []})
```

- [ ] **Step 2: Run, expect failure**: `... -m unittest tests.test_warranty_terms` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
"""Warranty from the purchase date, per part (the person's terms, 8 October 2026).

The one place the terms live. Cover starts on `purchase_date` (never
`created_at`, which is when the customer registered) and each part runs its
own term: `valid_until` is the day before the same calendar date `months`
later, and the last day is covered. The bike's status follows the battery.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Dict, Optional

TERMS: Dict[str, int] = {"display": 6, "charger": 6, "controller": 12, "battery": 12, "motor": 12, "frame": 60}
LEAD_PART = "battery"


def _add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def valid_until(start: date, months: int) -> date:
    return _add_months(start, months) - timedelta(days=1)


def coverage(purchase_date: Optional[date], today: date) -> Dict[str, Any]:
    if purchase_date is None:
        return {"status": "unknown", "remedy": "collect_purchase_proof", "components": []}
    components = []
    for part, months in TERMS.items():
        end = valid_until(purchase_date, months)
        components.append({"component": part, "months": months, "validUntil": end.isoformat(),
                           "active": today <= end})
    lead = next(p for p in components if p["component"] == LEAD_PART)
    return {"status": "active" if lead["active"] else "expired", "remedy": None, "components": components}
```

Check `valid_until(date(2024,2,29),12)`: `_add_months` → 2025-02-28, minus a day → 2025-02-27 (as the test says). `valid_until(2025-08-31, 6)` → 2026-02-28 − 1 = 2026-02-27.

- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `feat(warranty): per-part terms from the purchase date`.

---

### Task 2: The OMS database reader

**Files:**
- Create: `src/emotorad_ai/tools/oms_db.py`
- Test: `tests/test_oms_db.py`

**Interfaces:**
- Consumes: `warranty_terms.coverage`.
- Produces: `DSN_ENV = "EMOTORAD_OMS_PG_DSN"`; `configured(environ=None) -> bool`; `class OMSDatabase(dsn, connect=None, clock=None, today=None)` with `registrations(phone: str) -> List[Dict]` (raw rows, cached 60 s per phone, breaker 60 s), `invoice_file(phone: str, frame_number: str) -> Optional[str]`; `class OMSDatabaseUnavailable(Exception)`; `db_warranty_source(reader) -> Callable[[str], Optional[List[Dict]]]` returning warranty-service-shaped records (`frame_number`, `product_name`, `product_color`, `purchase_date`, `full_address`, `franchise_name`, `product_id`, `registration_status`, `invoice_on_file`, `term_source: "oms_terms"`, `warranty_api: coverage(...)`) and raising `ToolError("oms_unavailable", ..., retryable=True)` on any failure, `None` for no rows.

- [ ] **Step 1: Write the failing tests** (a fake connection records the SQL and parameters and returns rows):

```python
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
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Implement** `src/emotorad_ai/tools/oms_db.py`:

```python
"""Bikes from OMS production, read-only (spec 2026-10-08, section 1).

One query per phone, by its last ten digits so every stored shape is found
(`+91…`, `0…`, `91…`, spaces), less the registrations sold by the phone's own
dealership when it is also a dealer's. Read through a role that can select
only the listed columns. Errors carry the exception's class, never the
connection string; after a failure reads fail at once for a minute.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import warranty_terms

DSN_ENV = "EMOTORAD_OMS_PG_DSN"
CACHE_SECONDS = 60
FAILURE_TTL_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 5000
APPLICATION_NAME = "emotorad-ai-chatbot"

_LAST10 = "right(regexp_replace(coalesce({col}, ''), '[^0-9]', '', 'g'), 10)"
REGISTRATIONS_SQL = (
    "WITH dealer_franchises AS ("
    " SELECT id FROM em_franchise WHERE deleted_at IS NULL"
    " AND (" + _LAST10.format(col="mobile") + " = %(m10)s"
    " OR " + _LAST10.format(col="secondary_contact") + " = %(m10)s)"
    " UNION"
    " SELECT related_id FROM em_users WHERE deleted_at IS NULL AND related_id IS NOT NULL"
    " AND user_type IN ('franchise_manager', 'sale_franchise_person')"
    " AND " + _LAST10.format(col="mobile") + " = %(m10)s)"
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
_NOT_DIGITS = re.compile(r"[^0-9]")
_MOBILE = re.compile(r"[6-9][0-9]{9}")


class OMSDatabaseUnavailable(Exception):
    """The OMS database could not be read. The message is a class name only."""


def configured(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return bool((env.get(DSN_ENV) or "").strip())


def last_ten(phone: str) -> str:
    digits = _NOT_DIGITS.sub("", phone or "")[-10:]
    if not _MOBILE.fullmatch(digits):
        raise ValueError("not an Indian mobile")
    return digits


def _psycopg_connect(dsn: str, **kwargs: Any) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row, **kwargs)


class OMSDatabase:
    def __init__(self, dsn: str, connect: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.monotonic, today: Callable[[], date] = date.today) -> None:
        self._dsn = dsn
        self._connect = connect or _psycopg_connect
        self._clock = clock
        self.today = today
        self._cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._failed_at: Optional[float] = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "OMSDatabase(dsn=set)"

    def registrations(self, phone: str) -> List[Dict[str, Any]]:
        m10 = last_ten(phone)
        now = self._clock()
        with self._lock:
            if self._failed_at is not None and now - self._failed_at < FAILURE_TTL_SECONDS:
                raise OMSDatabaseUnavailable("breaker_open")
            cached = self._cache.get(m10)
            if cached and now - cached[0] < CACHE_SECONDS:
                return [dict(r) for r in cached[1]]
        try:
            with self._connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS,
                               application_name=APPLICATION_NAME,
                               options="-c statement_timeout=%d -c default_transaction_read_only=on"
                                       % STATEMENT_TIMEOUT_MS) as conn:
                rows = [dict(r) for r in conn.execute(REGISTRATIONS_SQL, {"m10": m10}).fetchall()]
        except Exception as exc:
            with self._lock:
                self._failed_at = now
            raise OMSDatabaseUnavailable(type(exc).__name__) from None
        with self._lock:
            self._failed_at = None
            self._cache[m10] = (now, rows)
        return [dict(r) for r in rows]

    def invoice_file(self, phone: str, frame_number: str) -> Optional[str]:
        for row in self.registrations(phone):
            if row.get("frame_number") == frame_number:
                return (str(row.get("invoice_image") or "").strip() or None)
        return None


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def to_record(row: Dict[str, Any], today: date) -> Dict[str, Any]:
    bought = _day(row.get("purchase_date"))
    status = "cancel_requested" if (row.get("status") or "").upper() == "CANCELLED_REQUEST" else "active"
    return {
        "frame_number": row.get("frame_number"),
        "product_name": row.get("product_name"),
        "product_color": row.get("product_color"),
        "product_id": row.get("product_id"),
        "franchise_name": row.get("franchise_name"),
        "full_address": row.get("full_address"),
        "customer_name": row.get("customer_name"),
        "purchase_date": bought.isoformat() if bought else None,
        "registration_status": status,
        "invoice_on_file": bool(str(row.get("invoice_image") or "").strip()),
        "term_source": "oms_terms",
        "warranty_api": warranty_terms.coverage(bought, today),
    }


def db_warranty_source(reader: OMSDatabase) -> Callable[[str], Optional[List[Dict[str, Any]]]]:
    from .registry import ToolError  # local: registry imports tools, not the reverse

    def source(phone: str) -> Optional[List[Dict[str, Any]]]:
        try:
            rows = reader.registrations(phone)
        except ValueError:
            return None
        except OMSDatabaseUnavailable as exc:
            raise ToolError("oms_unavailable",
                            "The warranty system is not responding (%s)." % exc, retryable=True)
        if not rows:
            return None
        today = reader.today()
        return [to_record(row, today) for row in rows]

    return source


def reader_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OMSDatabase]:
    env = os.environ if environ is None else environ
    dsn = (env.get(DSN_ENV) or "").strip()
    return OMSDatabase(dsn) if dsn else None
```

Note: the `ValueError` branch returns None only for a malformed phone, which the resolver never sends (a verified phone is a mobile); the test for that path calls `registrations` directly.

- [ ] **Step 4: Run, expect pass.** Fix the breaker test if `registrations` re-raises inside the lock (it must not hold the lock while connecting).
- [ ] **Step 5: Commit** `feat(oms): bikes from OMS production, read-only, less the dealer's own shop`.

---

### Task 3: The record in the tool, and the OMS file download

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py` (`_api_coverage`, around line 334; `submit_warranty_proof`, around line 1427)
- Modify: `src/emotorad_ai/tools/oms.py` (add `download_file`; `live_account_finder` unchanged)
- Test: `tests/test_oms_db_tool.py`

**Interfaces:**
- Consumes: Task 2 record shape.
- Produces: the lookup tool's bike gets `invoice_on_file: bool` when `purchase_date_missing`; `term_source` taken from the record (`"oms_terms"` or `"warranty_api"`); a `cancel_requested` registration reads like `pending_review`. `OMSClient.download_file(file_id: str) -> Tuple[bytes, str]` (raises `OMSNoRecord` on 400/404, `OMSUnavailable` otherwise; the URL is `<origin>/file/download/<file_id>` where origin is the base URL with its `/purchase` path removed). `submit_warranty_proof` gains code-only `optional_injects` `invoice_findings: Optional[str]` (appended to the description) and `invoice_purchase_date: Optional[str]` (used for `claimed_purchase_date` when given).

- [ ] **Step 1: Failing tests**

```python
"""The OMS database's records through lookup_warranty_record, and the invoice download."""

import io
import unittest
import urllib.error
from datetime import date, datetime, timezone

from emotorad_ai.tools import oms_db
from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, SUBMIT_WARRANTY_PROOF, build_registry
from emotorad_ai.tools.oms import OMSClient, OMSNoRecord, OMSUnavailable
from emotorad_ai.tools.registry import ToolContext
from tests.test_oms_db import ROW, reader

PHONE = "+919876543210"


def lookup(rows):
    db, _, _ = reader(rows)
    registry = build_registry(today=date(2025, 10, 1), warranty_source=oms_db.db_warranty_source(db))
    return registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=PHONE))


class LookupTests(unittest.TestCase):
    def test_a_dated_bike_gets_per_part_cover(self):
        [bike] = lookup([ROW])["data"]["bikes"]
        self.assertEqual(bike["coverage_status"], "from_warranty_api")
        self.assertEqual(bike["term_source"], "oms_terms")
        self.assertTrue(bike["in_warranty"])
        self.assertEqual({c["component"] for c in bike["components"]},
                         {"display", "charger", "controller", "battery", "motor", "frame"})

    def test_an_undated_bike_says_whether_an_invoice_is_on_file(self):
        for invoice, on_file in ((None, False), ("0b6c6e3e-0000-4000-8000-000000000001", True)):
            [bike] = lookup([dict(ROW, purchase_date=None, invoice_image=invoice)])["data"]["bikes"]
            self.assertEqual(bike["coverage_status"], "purchase_date_missing")
            self.assertIs(bike["invoice_on_file"], on_file)

    def test_the_file_id_is_never_given_to_the_model(self):
        envelope = lookup([dict(ROW, purchase_date=None, invoice_image="0b6c6e3e-0000-4000-8000-000000000001")])
        self.assertNotIn("0b6c6e3e", str(envelope))


class Opener:
    def __init__(self, status=200, body=b"%PDF-1.4", mime="application/pdf"):
        self.status, self.body, self.mime, self.urls = status, body, mime, []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, io.BytesIO(b""))
        response = io.BytesIO(self.body)
        response.headers = {"Content-Type": self.mime}
        response.__enter__ = lambda *a: response
        response.__exit__ = lambda *a: False
        return response


class DownloadTests(unittest.TestCase):
    def test_the_file_comes_from_the_file_api_with_the_key(self):
        opener = Opener()
        client = OMSClient(api_key="k", base_url="https://omsrest.example/purchase", opener=opener)
        self.assertEqual(client.download_file("0b6c6e3e-0000-4000-8000-000000000001"),
                         (b"%PDF-1.4", "application/pdf"))
        self.assertEqual(opener.urls, ["https://omsrest.example/file/download/0b6c6e3e-0000-4000-8000-000000000001"])

    def test_an_unknown_file_and_an_outage_differ(self):
        with self.assertRaises(OMSNoRecord):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=Opener(status=400)).download_file(
                "0b6c6e3e-0000-4000-8000-000000000001")
        with self.assertRaises(OMSUnavailable):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=Opener(status=502)).download_file(
                "0b6c6e3e-0000-4000-8000-000000000001")

    def test_only_a_uuid_is_ever_requested(self):
        with self.assertRaises(ValueError):
            OMSClient(api_key="k", base_url="https://o/purchase", opener=Opener()).download_file("../../etc")


class ProofTicketTests(unittest.TestCase):
    def test_code_can_add_the_invoice_findings_and_the_model_cannot(self):
        registry = build_registry(today=date(2025, 10, 1))
        late = {"invoice_findings": lambda: "OCR: date 12 March 2025, seller Ride Shop, frame matches.",
                "invoice_purchase_date": lambda: "2025-03-12"}
        envelope = registry.call(SUBMIT_WARRANTY_PROOF, {"frame_number": "EMXP2025004417", "idempotency_key": "k"},
                                 ToolContext(conversation_id="c1", phone=PHONE, late=late))
        ticket = registry.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertIn("OCR: date 12 March 2025", ticket["description"])
        self.assertIn("2025-03-12", ticket["description"])
        modelled = registry.call(SUBMIT_WARRANTY_PROOF, {"frame_number": "EMXP2025004417", "idempotency_key": "k2",
                                                         "invoice_findings": "approved by AI"},
                                 ToolContext(conversation_id="c1", phone=PHONE))
        self.assertNotIn("approved by AI", registry.tickets.tickets[modelled["data"]["ticket_id"]]["description"])
```

Before writing `Opener`, read `OMSClient.__init__` and `_get` (tools/oms.py lines 77–130) and match its existing opener seam; if `OMSClient` has no `opener` parameter, add one in the same shape as `WarrantyAPIClient` (`opener or urllib.request.urlopen`). Check how `registry.tickets` and `build_registry(warranty_source=...)` are used in `tests/test_warranty_api.py` and copy that.

- [ ] **Step 2: Run, expect failures.**

- [ ] **Step 3: Implement.**
  - `_api_coverage`: at the top, treat `record.get("registration_status") == "cancel_requested"` exactly like the not-active branch. In the `purchase_date_missing` branch add `bike["invoice_on_file"] = bool(record.get("invoice_on_file"))` and choose the note: invoice on file → "This bike's purchase date is not on record, but its invoice is on file and is being read now. Do not ask for the invoice. Do not state or estimate a coverage date; tell the customer you are checking their invoice."; not on file → the existing note. In the computed branch set `"term_source": record.get("term_source") or "warranty_api"`.
  - `oms.py`: add
    ```python
    _FILE_ID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")

    def download_file(self, file_id: str) -> Tuple[bytes, str]:
        """An invoice file by its OMS id (em-biz-backend file/views.py
        download_file_without_login): 400 or 404 is no such file."""
        if not _FILE_ID.fullmatch(file_id or ""):
            raise ValueError("not an OMS file id")
        origin = self.base_url.rsplit("/purchase", 1)[0]
        request = urllib.request.Request("%s/file/download/%s" % (origin, file_id))
        request.add_header("X-API-KEY", self.api_key)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read(MAX_FILE_BYTES + 1)
                mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                raise OMSNoRecord("no such file")
            raise OMSUnavailable("OMS returned HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise OMSUnavailable(type(exc).__name__)
        if len(body) > MAX_FILE_BYTES:
            raise OMSUnavailable("file_too_large")
        return body, mime
    ```
    with `MAX_FILE_BYTES = 12 * 1024 * 1024` and `Tuple` imported.
  - `submit_warranty_proof`: add `"invoice_findings", "invoice_purchase_date"` to its `optional_injects` and parameters `invoice_findings: Optional[str] = None, invoice_purchase_date: Optional[str] = None`; when `invoice_purchase_date` is given use it for `claimed_purchase_date`; append `"\n\n" + invoice_findings` to the description when given.

- [ ] **Step 4: Run these tests and `tests.test_warranty_api`, `tests.test_late_warranty*`; expect pass.**
- [ ] **Step 5: Commit** `feat(oms): the OMS database's records in the lookup, and the invoice download`.

---

### Task 4: Wiring, health and the account finder

**Files:**
- Modify: `src/emotorad_ai/api.py` (`_warranty_source_label` line 277, `_build_registry` line 283, `health`)
- Test: `tests/test_api_oms_db.py`; update `tests/test_api_health.py` expected dict.

**Interfaces:**
- Consumes: `oms_db.reader_from_env`, `oms_db.db_warranty_source`.
- Produces: module globals `OMS_DB` (an `OMSDatabase` or None) and `OMS_CLIENT` (an `OMSClient` or None); `/health` `warranty_source` is `oms_db` when `EMOTORAD_OMS_PG_DSN` is set; the account finder is the fixtures' while `EMOTORAD_AI_DEV_CODES=1`, even with the OMS key.

- [ ] **Step 1: Failing tests** — with `fresh_api` (as in `tests/test_api_media_persistence.py`) and `mock.patch.dict(os.environ, {...})`:
  - `EMOTORAD_OMS_PG_DSN` set → `api.WARRANTY_SOURCE == "oms_db"` and `api.OMS_DB` is an `OMSDatabase` (no connection is made at import: assert the fake `connect` was never called, by patching `oms_db._psycopg_connect` to raise).
  - Both the DSN and `EMOTORAD_WARRANTY_API_KEY` set → still `oms_db`.
  - `EMOTORAD_OMS_API_KEY` and `EMOTORAD_AI_DEV_CODES=1` → `api.registry.account_finder is fixtures.find_account_by_order_code` (find the attribute name the registry keeps; if none, assert by calling the verification fallback with a fixtures order code and checking no OMS request was attempted via a patched `OMSClient._get`).
  - `/health` includes `"warranty_source": "oms_db"`.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement**: at module level before `_build_registry`, `OMS_DB = oms_db.reader_from_env()` and `OMS_CLIENT = OMSClient() if os.environ.get("EMOTORAD_OMS_API_KEY") else None`. In `_build_registry`: `if OMS_DB is not None: source = oms_db.db_warranty_source(OMS_DB)` first, then the existing chain; `account_finder=live_account_finder(OMS_CLIENT) if OMS_CLIENT is not None and os.environ.get("EMOTORAD_AI_DEV_CODES") != "1" else fixtures.find_account_by_order_code`. `_warranty_source_label`: return `"oms_db"` first when `OMS_DB is not None`.
- [ ] **Step 4: Run the new tests and the whole suite.**
- [ ] **Step 5: Commit** `feat(api): OMS production first for bikes; the OMS key never verifies while dev codes are on`.

---

### Task 5: Port the date reader

**Files:**
- Create (from `feat/warranty-status`): `src/emotorad_ai/textfold.py`, `src/emotorad_ai/invoice_dates.py`, `tests/test_textfold.py` (if it exists there), `tests/test_invoice_dates.py`

**Interfaces:**
- Produces: `invoice_dates.parse_printed_date(text) -> ParsedDate` with `.date` (a `datetime.date` or None) and `.reason` (None, `"unparsed"`, `"invalid_date"`, `"not_day_first"`); `find_dates`, `redact_dates`.

- [ ] **Step 1:** `git show feat/warranty-status:src/emotorad_ai/textfold.py > src/emotorad_ai/textfold.py`, the same for `invoice_dates.py` and their tests (`git ls-tree --name-only feat/warranty-status tests/ | grep -E 'textfold|invoice_dates'`). Check `invoice_dates.py` imports only `digits` and `textfold` (both now present); if `tests/test_invoice_dates.py` imports anything from `guardrails` or `runtime`, delete those test classes and note it in the commit (they belong to the deferred date post-check).
- [ ] **Step 2:** Run `tests.test_invoice_dates` (and `tests.test_textfold`); expect pass.
- [ ] **Step 3: Commit** `feat(dates): port the day-first date reader from feat/warranty-status`.

---

### Task 6: The invoice reader

**Files:**
- Create: `src/emotorad_ai/invoice_ocr.py`
- Test: `tests/test_invoice_ocr.py`

**Interfaces:**
- Consumes: `invoice_dates.parse_printed_date`; `warranty_terms.coverage`, `LEAD_PART`; the OpenRouter transport (`openrouter.CHAT_PATH`, `OpenRouterTransport`, `OpenRouterError`; `llm.from_openai_response`), as `serial_read.py` uses them.
- Produces: `class InvoiceReadError(Exception)`; `class OpenRouterInvoiceReader(transport, model=OPENROUTER_INVOICE_MODEL, zdr=True)` with `provider = "openrouter"`, `.read(data: bytes, mime: str, frame_number: str) -> Dict` returning `{"is_invoice", "invoice_date" (str or None, as printed), "seller", "frame_numbers": [...], "product", "legible"}`; `parse(text) -> Dict`; `assess(found: Dict, frame_number: str, today: date) -> Dict` returning `{"confident": bool, "purchase_date": Optional[date], "reason": Optional[str]}`; `customer_line(assessed: Dict, today: date, hindi: bool) -> str`; `findings_text(found, assessed, source) -> str`; `reader_from_env(environ=None)` (on with `EMOTORAD_INVOICE_OCR == "on"` and the OpenRouter key).

- [ ] **Step 1: Failing tests** covering:

```python
"""Reading an invoice (spec 2026-10-08, section 3)."""

import json
import unittest
from datetime import date

from emotorad_ai import invoice_ocr
from emotorad_ai.invoice_ocr import InvoiceReadError, assess, customer_line, parse
from tests.test_serial_read import FakeTransport  # posts recorded; .text is the model's answer

TODAY = date(2025, 10, 1)
GOOD = {"is_invoice": True, "invoice_date": "12/03/2025", "seller": "Ride Shop",
        "frame_numbers": ["EMXP2025004417"], "product": "EMX Plus", "legible": True}


class ParseTests(unittest.TestCase):
    def test_a_clear_invoice(self):
        self.assertEqual(parse(json.dumps(GOOD)), GOOD)

    def test_a_photo_that_is_not_an_invoice(self):
        self.assertEqual(parse(json.dumps({"is_invoice": False}))["is_invoice"], False)

    def test_bad_json_is_an_error(self):
        with self.assertRaises(InvoiceReadError):
            parse("the date is 12 March")


class AssessTests(unittest.TestCase):
    def test_confident_when_the_date_is_real_and_the_frame_matches(self):
        result = assess(GOOD, "EMXP2025004417", TODAY)
        self.assertEqual((result["confident"], result["purchase_date"]), (True, date(2025, 3, 12)))

    def test_the_frame_matches_ignoring_case_spaces_and_dashes(self):
        self.assertTrue(assess(dict(GOOD, frame_numbers=["emxp-2025 004417"]), "EMXP2025004417", TODAY)["confident"])

    def test_not_confident(self):
        cases = {
            "future": dict(GOOD, invoice_date="12/03/2026"),
            "before 2019": dict(GOOD, invoice_date="12/03/2018"),
            "another frame": dict(GOOD, frame_numbers=["EMXP2025009999"]),
            "no frame": dict(GOOD, frame_numbers=[]),
            "illegible": dict(GOOD, legible=False),
            "no date": dict(GOOD, invoice_date=None),
            "not an invoice": dict(GOOD, is_invoice=False),
            "nonsense date": dict(GOOD, invoice_date="31/02/2025"),
        }
        for name, found in cases.items():
            with self.subTest(name):
                self.assertFalse(assess(found, "EMXP2025004417", TODAY)["confident"])


class LineTests(unittest.TestCase):
    def test_confident_names_the_date_and_the_batterys_end(self):
        self.assertEqual(customer_line(assess(GOOD, "EMXP2025004417", TODAY), TODAY, hindi=False),
                         "Going by your invoice dated 12 March 2025, your battery is covered until 11 March 2026. "
                         "A support executive will confirm this.")

    def test_not_confident_names_no_date(self):
        line = customer_line(assess(dict(GOOD, legible=False), "EMXP2025004417", TODAY), TODAY, hindi=False)
        self.assertEqual(line, "Thanks, I have passed your invoice to our support team. A support executive will "
                               "confirm your warranty.")


class ReaderTests(unittest.TestCase):
    def test_the_invoice_goes_inline_with_the_frame_and_no_customer_words(self):
        transport = FakeTransport(answer=GOOD)
        invoice_ocr.OpenRouterInvoiceReader(transport).read(b"%PDF", "application/pdf", "EMXP2025004417")
        body = transport.posts[0]["body"]
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        texts = [p.get("text") for p in body["messages"][0]["content"] if p["type"] == "text"]
        self.assertIn("EMXP2025004417", " ".join(texts))

    def test_off_unless_switched_on_with_the_key(self):
        self.assertIsNone(invoice_ocr.reader_from_env({"OPENROUTER_API_KEY": "k"}))
        self.assertIsNotNone(invoice_ocr.reader_from_env({"OPENROUTER_API_KEY": "k", "EMOTORAD_INVOICE_OCR": "on"}))
```

(Check `tests/test_serial_read.FakeTransport`'s constructor; if its `answer` default differs, construct it with `text=json.dumps(GOOD)`.)

- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** in the shape of `serial_read.py`: `OPENROUTER_INVOICE_MODEL = "google/gemini-3.8-flash"`, `TIMEOUT_SECONDS = 30`, `INLINE_LIMIT = 12 MiB`; `PROMPT` asking for JSON with exactly the keys above, the date "exactly as printed", every frame number printed, `is_invoice` false for anything that is not a purchase invoice or bill; the frame number given as a separate text part ("The bike's frame number: …; report every frame number printed on the invoice, whether or not it matches"). A PDF goes as `{"type": "file", "file": {"filename": "invoice.pdf", "file_data": "data:application/pdf;base64,…"}}`, an image as `image_url`. `parse` keeps only the listed keys, coerces `frame_numbers` to a list of strings, and `legible`/`is_invoice` to `x is True`. `assess` uses `parse_printed_date` (reason must be None), the 2019 floor, `today`, and the frame normalisation `re.sub(r"[^A-Z0-9]", "", s.upper())`. `customer_line` uses `warranty_terms.coverage(purchase_date, today)` to find the `LEAD_PART` end date, formats dates as `"%d %B %Y"` without a leading zero (`f"{d.day} {d:%B %Y}"`), and for `hindi=True` returns the Hindi drafts:
  - confident: `"आपके इनवॉइस की तारीख {date} के हिसाब से, आपकी बैटरी {end} तक कवर है। सपोर्ट टीम इसकी पुष्टि करेगी।"`
  - not confident: `"धन्यवाद, मैंने आपका इनवॉइस हमारी सपोर्ट टीम को भेज दिया है। सपोर्ट टीम आपकी वारंटी की पुष्टि करेगी।"`
  `findings_text(found, assessed, source)` returns `"Invoice read by AI (source: OMS|customer upload): date <as printed>, seller <seller>, frame numbers <list>, product <product>; confident: yes|no (<reason>). Warranty check: not confirmed yet."`
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `feat(invoice): read an invoice with Gemini Flash and decide confidence in code`.

---

### Task 7: The `invoice_readings` store

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`InMemoryConversationStore`, beside `_serials`), `src/emotorad_ai/stores/mongo.py` (beside `SERIAL_READINGS`), `src/emotorad_ai/erasure_admin.py` (PERMANENT), `scripts/mongo_setup.py` (PERMANENT, import)
- Test: `tests/test_invoice_readings_store.py`; update `tests/test_mongo_store.py`, `tests/test_mongo_scripts.py`, `tests/test_audit_persistence.py` as `serial_readings` was updated in commit `0268180`.

**Interfaces:**
- Produces: `add_invoice_reading(reading: Dict) -> None` (upsert by `_id`), `invoice_readings_of(conversation_id) -> List[Dict]` sorted by `read_at`, `update_invoice_reading(conversation_id, reading_id, fields) -> None`; `INVOICE_READINGS = "invoice_readings"` with indexes `conversation_read_at` and `user`; counted and erased in `conversations_of`, `delete_person` (dry run too) and `delete_conversation`.

- [ ] **Step 1:** Copy `tests/test_serial_read.py`'s `StoreErasureContract`, `InMemoryErasureTests` and `MongoErasureTests` into the new test file, renamed to the invoice methods and the key `"invoice_readings"`.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3:** Implement exactly as `serial_readings` was in commit `0268180` (`git show 0268180 -- src/emotorad_ai/conversation.py src/emotorad_ai/stores/mongo.py src/emotorad_ai/erasure_admin.py scripts/mongo_setup.py` is the pattern), plus `update_invoice_reading` (in-memory `dict.update`; Mongo `update_one({"_id": id, "conversation_id": cid}, {"$set": fields})` under `_guard`).
- [ ] **Step 4: Run the new tests and the store tests; expect pass.**
- [ ] **Step 5: Commit** `feat(store): invoice_readings, permanent and erased with the person`.

---

### Task 8: The invoice service, the reads before the turn, and the ticket and line after it

**Files:**
- Modify: `src/emotorad_ai/invoice_ocr.py` (add `InvoiceService`), `src/emotorad_ai/api.py`, `src/emotorad_ai/runtime.py`, `src/emotorad_ai/conversation.py` (state fields)
- Test: `tests/test_invoice_flow.py`

**Interfaces:**
- Consumes: Tasks 2, 3, 6, 7.
- Produces:
  - `InvoiceService(reader, oms_db, oms_client, media_store, conversations, emit, pool=None, wait_seconds=15.0, today=date.today)`:
    - `bike(phone, frame) -> Optional[Dict]`: the raw OMS row for the frame (from `oms_db.registrations`), or None.
    - `read_from_oms(conversation_id, user_key, phone, frame) -> None`: if no reading for this frame exists, downloads `oms_db.invoice_file(phone, frame)` with `oms_client.download_file`, stores a copy at `keys.customer_key(cluster or user_key, conversation_id, "documents", new id, mime)` with `media_store.put_bytes` (skip storing when no media store), reads it, `assess`es it and adds the reading; never raises (logs `invoice_read_failed` with a code).
    - `read_upload(conversation_id, user_key, frame, key, mime) -> None`: loads `media_store.get_bytes(key)`, reads, and stores a reading only when `is_invoice` is true.
    - `start(jobs: List[Callable[[], None]]) -> None`: submits to the pool and waits up to `wait_seconds`; late ones finish in the background.
    - Reading document: `{"_id": <s3 key or "oms:"+file_id>, "conversation_id", "user_key", "frame_number", "source": "oms"|"customer", "found": {...}, "confident", "purchase_date" (ISO or None), "read_at", "ticket_id": None, "told": False}`.
  - `ConversationState`: `invoice_asked_frames: List[str]` (frames whose OMS invoice read started), merged as a union in `_merge_onto_fresh`.
  - `Runtime(invoice=InvoiceService or None)`; `Runtime._with_invoice_result(message, resolved, state, turn)`, called after `_with_serial_confirm`: for each reading of this conversation with `told` false, raise `SUBMIT_WARRANTY_PROOF` by code (`idempotency_key = "invoice:" + frame`; late facts `invoice_findings` = `findings_text(...)`, `invoice_purchase_date` = the ISO date when confident), append `customer_line(...)` to the reply (Hindi for a Devanagari reply), and update the reading `{"told": True, "ticket_id": <ref>}`.
  - `api._start_invoice_reads(message)` beside `_start_serial_reads`, called before `runtime.handle` in both turn paths.

- [ ] **Step 1: Failing tests** through `runtime.handle()` with the in-memory store, a fake OMS database (Task 2's `reader`), a fake `OMSClient` with `download_file` returning `(b"%PDF", "application/pdf")`, a `FakeReader` returning `GOOD` (Task 6) and an inline pool (`tests/test_serial_read.InlinePool`):
  - **Invoice on file:** a chosen undated bike with `invoice_image` set; the customer writes "is my battery covered?"; the scripted model calls `lookup_warranty_record` and replies "Let me check." → the reply ends with the confident line, one `warranty_proof` ticket exists with "Invoice read by AI (source: OMS)" in its description, the reading is `told`, and a second turn adds no second ticket or line.
  - **Not confident:** `FakeReader` returns `dict(GOOD, legible=False)` → the not-confident line, and the ticket says "confident: no".
  - **No invoice on file:** `invoice_image` None; the customer sends a stored photo that the reader calls an invoice → the line and the ticket with "source: customer upload".
  - **A fault photo while an invoice is awaited:** the reader returns `{"is_invoice": False}` → no ticket, no line.
  - **A dated bike:** nothing is read (the fake reader records no calls).
  - **OMS file download fails** (`OMSUnavailable`) → no reading, `invoice_read_failed` logged with `error`, the turn's reply is unchanged.
  - **Values never logged:** no event carries the invoice date or the frame number.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement.**
  - `_start_invoice_reads(message)`: return unless `INVOICE` (the service) is set and `message.identity.phone`; peek the state; the frame is `state.selected_frame`; `row = INVOICE.bike(phone, frame)`; return when no row or it has a `purchase_date`. If `row["invoice_image"]` and the frame is not in `state.invoice_asked_frames`: the job is `read_from_oms`. Else: one `read_upload` job per stored image or PDF attachment (`url` starts `s3://`). Then `INVOICE.start(jobs)`. Wrap all in try/except that logs `invoice_read_failed` with `error="state:" + type(exc).__name__`.
  - The state flag: the runtime adds the frame to `state.invoice_asked_frames` in `_with_invoice_result` when a reading for it exists or when the chosen bike's lookup shows `invoice_on_file` (so the OMS read starts once per bike per run).
  - `Runtime._with_invoice_result`: only for customer persona replies (any agent), never on a reply about a hazard (`check_safety_in_description(turn.text).triggered`); readings via `self.conversations.invoice_readings_of(cid)` guarded like `_serial_readings`; build the `ToolContext` as `_raise_safety_ticket` does (runtime.py around line 2760); `self.registry.call(SUBMIT_WARRANTY_PROOF, {"frame_number": frame, "idempotency_key": "invoice:" + frame, "purchase_channel": "unknown"}, context, run_without_idempotency=False)`.
  - `api.py`: `INVOICE = invoice_ocr.InvoiceService(...)` when `invoice_ocr.reader_from_env()` and `OMS_DB` are both set, else None; pass `invoice=INVOICE` to the `Runtime`; `/health` `"invoice_ocr": "openrouter"` or `"off"`.
  - `deploy-staging.yml`: add `-e EMOTORAD_INVOICE_OCR=on` to the `docker run` line.
- [ ] **Step 4: Run the new tests, then the whole suite.**
- [ ] **Step 5: Commit** `feat(invoice): read the OMS invoice or the customer's, raise the warranty-proof ticket and tell the customer, in code`.

---

### Task 9: Prompts

**Files:**
- Modify: `src/emotorad_ai/agents/late_warranty.py` and `src/emotorad_ai/agents/battery_support.py` (the coverage-line helper that reads `purchase_date_missing`), `src/emotorad_ai/agents/motor_support.py` (one line)
- Test: extend `tests/test_invoice_flow.py` with prompt assertions.

- [ ] **Step 1: Failing tests:** each prompt mentions that with `invoice_on_file` true the bot does not ask for the invoice and says it is checking it; with it false it asks for a photo or PDF of the invoice; none of them tells the model to state an invoice date.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3:** Add to each prompt: "When a bike's purchase date is missing: if `invoice_on_file` is true, do not ask for the invoice; say you are checking the invoice on file. If it is false, ask for a clear photo or PDF of the purchase invoice. Code reads the invoice, raises the ticket and tells the customer what it found; never state an invoice date or a cover end date yourself."
- [ ] **Step 4: Run, expect pass; run the whole suite.**
- [ ] **Step 5: Commit** `feat(prompts): the agents leave the invoice to code`.

---

### Task 10: Documentation and the runbook

**Files:**
- Modify: `CLAUDE.md` (Confirmed section's Ownership + warranty line; a rule entry "Bikes and warranty from OMS production"), `docs/runbooks/config-store.md` (a new section "OMS production read" with the rollout steps from the spec)

- [ ] **Step 1:** CLAUDE.md: replace the warranty-service-first statement with the new source order and terms; add the rule entry (oms_db, terms, invoice OCR, the two switches `EMOTORAD_OMS_PG_DSN` and `EMOTORAD_INVOICE_OCR`, the deferred date post-check, Sachin's sign-offs).
- [ ] **Step 2:** Runbook: the column grants verbatim from the spec, the network path, the config-store keys (add `EMOTORAD_OMS_PG_DSN`, `EMOTORAD_OMS_API_KEY`; remove `EMOTORAD_WARRANTY_API_KEY`, `EMOTORAD_AMIGO_PG_DSN`), a CloudShell script that adds or removes keys while keeping the others (region `ap-south-1`), the restart, the `/health` check, the rollback.
- [ ] **Step 3:** Whole suite; record the count.
- [ ] **Step 4: Commit** `docs: OMS production as the bikes source, and its runbook`.

---

## Deferred (its own plan)

- The date post-check (`guardrails.check_dates` and its runtime hook) from `feat/warranty-status`: blocks a model reply that states an invoice date or a cover end date it was not given. Tier-1; human review before merge.
- An index on `em_purchase (mobile)` through a reviewed em-biz-backend migration.
