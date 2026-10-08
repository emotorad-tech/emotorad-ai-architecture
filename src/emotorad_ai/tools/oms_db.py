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
    """The phone's last ten digits; ValueError for anything but an Indian mobile."""
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
        """The phone's registrations as a customer, one per frame. Raises
        ValueError for a number that is not an Indian mobile (never sent) and
        OMSDatabaseUnavailable when the database cannot be read."""
        m10 = last_ten(phone)
        now = self._clock()
        with self._lock:
            if self._failed_at is not None and now - self._failed_at < FAILURE_TTL_SECONDS:
                raise OMSDatabaseUnavailable("breaker_open")
            cached = self._cache.get(m10)
            if cached and now - cached[0] < CACHE_SECONDS:
                return [dict(row) for row in cached[1]]
        try:
            with self._connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS,
                               application_name=APPLICATION_NAME,
                               options="-c statement_timeout=%d -c default_transaction_read_only=on"
                                       % STATEMENT_TIMEOUT_MS) as conn:
                rows = [dict(row) for row in conn.execute(REGISTRATIONS_SQL, {"m10": m10}).fetchall()]
        except Exception as exc:
            with self._lock:
                self._failed_at = now
            raise OMSDatabaseUnavailable(type(exc).__name__) from None
        with self._lock:
            self._failed_at = None
            self._cache[m10] = (now, rows)
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


def to_record(row: Dict[str, Any], today: date) -> Dict[str, Any]:
    """One registration as the record the lookup tool reads, in the shape the
    warranty service's records had (tools/warranty_api.to_record)."""
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
    """Registered bikes from the OMS database, mapped onto the tool's own
    outcomes: rows, None for no rows, and oms_unavailable for any failure."""
    from .registry import ToolError  # local: registry imports tools, not the reverse

    def source(phone: str) -> Optional[List[Dict[str, Any]]]:
        try:
            rows = reader.registrations(phone)
        except ValueError:
            return None
        except OMSDatabaseUnavailable as exc:
            raise ToolError("oms_unavailable", "The warranty system is not responding (%s)." % exc,
                            retryable=True)
        if not rows:
            return None
        today = reader.today()
        return [to_record(row, today) for row in rows]

    return source


def reader_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OMSDatabase]:
    env = os.environ if environ is None else environ
    dsn = (env.get(DSN_ENV) or "").strip()
    return OMSDatabase(dsn) if dsn else None
