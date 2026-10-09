"""The EMotorad dealer stores nearest a customer (spec 2026-10-09, section 2).

OMS's active Dealers stores, read once every ten minutes through the same
read-only connection as the bike lookup (tools/oms_db.py), each placed at
the centre of its pin code (geo.py) and ranked by straight-line distance.
The contact is the store's franchise manager, or its contact person when it
has none. Errors carry the exception's class, never the connection string.

The model never sees a manager's name or phone: the tool hands the full
cards to code through StoreCards, and the runtime puts them on the reply.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import geo
from ..geo import PincodeCentres, Point
from . import oms_db

DSN_ENV = oms_db.DSN_ENV
CACHE_SECONDS = 600
FAILURE_TTL_SECONDS = 60
STALE_SECONDS = 3600
NEAREST = 3
FAR_KM = 100

STORES_SQL = (
    "SELECT f.id, f.customer_name AS store_name, f.address, f.address2,"
    " p.pin_code, d.district_name, s.state_name,"
    " m.full_name AS manager_name, m.mobile AS manager_mobile,"
    " f.poc_name, f.mobile AS store_mobile"
    " FROM em_franchise f"
    " JOIN em_franchise_type t ON t.id = f.franchise_type_id AND t.franchise_type_name = 'Dealers'"
    " JOIN em_pin_code p ON p.id = f.pin_code_id"
    " LEFT JOIN em_district d ON d.id = f.district_id"
    " LEFT JOIN em_state s ON s.id = f.state_id"
    " LEFT JOIN LATERAL ("
    " SELECT u.full_name, u.mobile FROM em_users u"
    " WHERE u.related_id = f.id AND u.user_type = 'franchise_manager'"
    " AND u.is_active AND u.deleted_at IS NULL AND coalesce(u.mobile, '') <> ''"
    " ORDER BY u.updated_at DESC LIMIT 1"
    " ) m ON true"
    " WHERE f.is_active AND f.deleted_at IS NULL AND NOT coalesce(f.is_distributor, false)"
)

# Three made-up stores for tests and offline mode. Never real dealers.
FIXTURE_ROWS: Tuple[Dict[str, Any], ...] = (
    {"store_name": "Test Cycles Pune", "address": "Shop 1, Test Road, Viman Nagar", "address2": None,
     "pin_code": "411014", "district_name": "Pune", "state_name": "Maharashtra",
     "manager_name": "Test Manager One", "manager_mobile": "9000000001", "poc_name": None, "store_mobile": None},
    {"store_name": "Test Cycles Mumbai", "address": "Shop 2, Test Lane, Bandra West", "address2": None,
     "pin_code": "400054", "district_name": "Mumbai", "state_name": "Maharashtra",
     "manager_name": "Test Manager Two", "manager_mobile": "9000000002", "poc_name": None, "store_mobile": None},
    {"store_name": "Test Cycles Delhi", "address": "Shop 3, Test Market, Hauz Khas", "address2": None,
     "pin_code": "110016", "district_name": "South", "state_name": "Delhi",
     "manager_name": None, "manager_mobile": None, "poc_name": "Test Owner Three", "store_mobile": "9000000003"},
)


class DealerStoresUnavailable(Exception):
    """The stores could not be read and no recent list is held. The message is a reason, never a DSN."""


@dataclass(frozen=True)
class Store:
    name: str
    address: str
    pincode: str
    district: str
    state: str
    manager_name: str
    phone: str
    point: Point


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip(" ,")


def display_phone(raw: Optional[str]) -> str:
    digits = re.sub(r"[^0-9]", "", raw or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10:
        return "+91 " + digits
    return (raw or "").strip()


def full_address(row: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for value in (row.get("address"), row.get("address2"), row.get("district_name"), row.get("state_name")):
        text = _clean(value)
        if text and text.lower() not in [p.lower() for p in parts]:
            parts.append(text)
    joined = ", ".join(parts)
    pincode = _clean(row.get("pin_code"))
    if pincode and pincode not in joined:
        joined = (joined + " - " if joined else "") + pincode
    return joined


def store_from_row(row: Mapping[str, Any], centres: PincodeCentres) -> Optional[Store]:
    pincode = _clean(row.get("pin_code"))
    point = centres.centre(pincode)
    if point is None:
        return None
    name, phone = _clean(row.get("manager_name")), _clean(row.get("manager_mobile"))
    if not (name and phone):
        name, phone = _clean(row.get("poc_name")), _clean(row.get("store_mobile"))
    if not (name and phone):
        return None
    return Store(name=_clean(row.get("store_name")) or "EMotorad dealer", address=full_address(row), pincode=pincode,
                 district=_clean(row.get("district_name")), state=_clean(row.get("state_name")),
                 manager_name=name, phone=display_phone(phone), point=point)


def store_card(ref: str, store: Store, km: int) -> Dict[str, Any]:
    return {"ref": ref, "name": store.name, "address": store.address, "pincode": store.pincode,
            "manager_name": store.manager_name, "phone": store.phone, "distance_km": km}


class DealerDirectory:
    def __init__(self, load: Callable[[], List[Dict[str, Any]]], centres: PincodeCentres,
                 clock: Callable[[], float] = time.monotonic,
                 log: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        self._load = load
        self.centres = centres
        self._clock = clock
        self._log = log
        self._stores: List[Store] = []
        self._loaded_at: Optional[float] = None
        self._failed_at: Optional[float] = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "DealerDirectory(stores=%d)" % len(self._stores)

    def _stale_or_raise(self, now: float, reason: str) -> List[Store]:
        if self._loaded_at is not None and now - self._loaded_at < STALE_SECONDS:
            return list(self._stores)
        raise DealerStoresUnavailable(reason)

    def stores(self) -> List[Store]:
        now = self._clock()
        with self._lock:
            if self._loaded_at is not None and now - self._loaded_at < CACHE_SECONDS:
                return list(self._stores)
            if self._failed_at is not None and now - self._failed_at < FAILURE_TTL_SECONDS:
                return self._stale_or_raise(now, "breaker_open")
        try:
            rows = self._load()
        except Exception as exc:
            with self._lock:
                self._failed_at = now
                return self._stale_or_raise(now, type(exc).__name__)
        stores = [s for s in (store_from_row(r, self.centres) for r in rows) if s is not None]
        with self._lock:
            self._stores, self._loaded_at, self._failed_at = stores, now, None
        if self._log is not None:
            self._log("dealer_stores_loaded", {"stores": len(stores), "unplaced": len(rows) - len(stores)})
        return list(stores)

    def nearest(self, point: Point, n: int = NEAREST) -> List[Tuple[Store, int]]:
        ranked = sorted(((geo.distance_km(point, s.point), s.name, s) for s in self.stores()),
                        key=lambda item: (item[0], item[1]))
        return [(store, int(round(km))) for km, _name, store in ranked[:n]]


def db_loader(dsn: str, connect: Optional[Callable[..., Any]] = None) -> Callable[[], List[Dict[str, Any]]]:
    opener = connect or oms_db._psycopg_connect

    def load() -> List[Dict[str, Any]]:
        with opener(dsn, connect_timeout=oms_db.CONNECT_TIMEOUT_SECONDS, application_name=oms_db.APPLICATION_NAME,
                    options="-c statement_timeout=%d -c default_transaction_read_only=on -c TimeZone=UTC"
                            % oms_db.STATEMENT_TIMEOUT_MS) as conn:
            return [dict(r) for r in conn.execute(STORES_SQL).fetchall()]

    return load


def directory_from_env(centres: PincodeCentres, environ: Optional[Mapping[str, str]] = None,
                       log: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Tuple[DealerDirectory, str]:
    env = os.environ if environ is None else environ
    dsn = (env.get(DSN_ENV) or "").strip()
    if dsn:
        return DealerDirectory(db_loader(dsn), centres, log=log), "oms_db"
    return DealerDirectory(lambda: [dict(r) for r in FIXTURE_ROWS], centres, log=log), "fixtures"


class StoreCards:
    """The cards a turn found, kept beside the model: the tool puts them, the
    runtime takes them for the reply. Taken once; the last call in a turn wins."""

    def __init__(self) -> None:
        self._cards: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put(self, conversation_id: str, cards: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._cards[conversation_id] = [dict(c) for c in cards]

    def take(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return self._cards.pop(conversation_id, [])
