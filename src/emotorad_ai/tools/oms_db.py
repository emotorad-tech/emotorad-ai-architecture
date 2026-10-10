"""Bikes from OMS production, read-only (spec 2026-10-08, section 1).

One query per phone, by its last ten digits so every stored shape is found
(`+91…`, `0…`, `91…`, spaces), less the registrations sold by the phone's own
dealership when it is also a dealer's (its `em_franchise` number, or a dealer
login in `em_users` linked to the dealership). One row per frame: a frame
registered twice keeps the row with a purchase date, then the newest.

Read through a role that can select only the listed columns, on a read-only
connection with a statement timeout. Errors carry the exception's class, never
the connection string; after a failure reads fail at once for a minute (a
small circuit breaker), and a phone is read at most once a minute (hydration
and the lookup tool in the same turn share one read).

With EMOTORAD_OMS_ORDERS=on, a second query (ORDERS_SQL, spec 2026-10-10)
gives the bikes on the phone's orders that nobody registered, with its own
cache and breaker.
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
ORDERS_ENV = "EMOTORAD_OMS_ORDERS"

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
_NOT_DIGITS = re.compile(r"[^0-9]")
_MOBILE = re.compile(r"[6-9][0-9]{9}")


class OMSDatabaseUnavailable(Exception):
    """The OMS database could not be read. The message is a class name only."""


def configured(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return bool((env.get(DSN_ENV) or "").strip())


def last_ten(phone: str) -> str:
    """An Indian mobile's ten digits, written as ten digits, `0` and ten, `91`
    and ten, or `+91` and ten (spaces and dashes ignored); ValueError for
    anything else, a foreign number above all: the last ten digits of +65 or
    +44 numbers look like an Indian mobile and would read a stranger's bikes."""
    raw = (phone or "").strip()
    digits = _NOT_DIGITS.sub("", raw)
    if raw.startswith("+"):
        local = digits[2:] if digits.startswith("91") and len(digits) == 12 else None
    elif len(digits) == 10:
        local = digits
    elif len(digits) == 11 and digits.startswith("0"):
        local = digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        local = digits[2:]
    else:
        local = None
    if local is None or not _MOBILE.fullmatch(local):
        raise ValueError("not an Indian mobile")
    return local


def _psycopg_connect(dsn: str, **kwargs: Any) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row, **kwargs)


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

    def invoice_file(self, phone: str, frame_number: str) -> Optional[str]:
        """The OMS file id of the frame's invoice, or None."""
        for row in self.registrations(phone):
            if row.get("frame_number") == frame_number:
                return str(row.get("invoice_image") or "").strip() or None
        return None

    def row(self, phone: str, frame_number: str) -> Optional[Dict[str, Any]]:
        """The frame's registration for this phone, or None."""
        return next((row for row in self.registrations(phone) if row.get("frame_number") == frame_number), None)


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def to_record(row: Dict[str, Any], today: date,
              invoice_state: Optional[Callable[[Optional[str]], str]] = None) -> Dict[str, Any]:
    """One registration as the record the lookup tool reads, in the shape the
    warranty service's records had (tools/warranty_api.to_record).
    `invoice_state` (invoice_ocr.InvoiceService.invoice_state) says whether
    OMS's invoice can be read; without it a file on record counts."""
    bought = _day(row.get("purchase_date"))
    file_id = str(row.get("invoice_image") or "").strip() or None
    state = invoice_state(file_id) if invoice_state is not None else ("readable" if file_id else "unreadable")
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
        "invoice_on_file": state == "readable",
        "invoice_with_support": state == "with_support",
        "term_source": "oms_terms",
        "ownership_source": "oms_purchase",
        "warranty_api": warranty_terms.coverage(bought, today),
    }


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


def reader_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OMSDatabase]:
    env = os.environ if environ is None else environ
    dsn = (env.get(DSN_ENV) or "").strip()
    if not dsn:
        return None
    return OMSDatabase(dsn, orders=(env.get(ORDERS_ENV) or "").strip() == "on")
