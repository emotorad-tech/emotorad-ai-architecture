"""Amigo, EMotorad's connected-bike app: a rider's bikes, service status and
recent trips, read-only (the person's decisions, 2026-09-30).

Read through a Postgres role that can only SELECT a handful of columns
(scripts/amigo_staging/). The connection string names the `userbike`
database; `garage` and `ride` are on the same server, named the same way, so
the old set (`userbike`) and the new platform's (`revalt_userbike`) both work.

Every read is by the rider's verified phone and nothing else, so the bot can
only ever see the person it is talking to. Errors carry the exception's class
and never the connection string.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)

DSN_ENV = "EMOTORAD_AMIGO_PG_DSN"
# Hydration asks for a rider's bikes every turn; the database is a db.t3.micro.
BIKES_TTL_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 3
APPLICATION_NAME = "emotorad-ai-chatbot"

_BIKES_SQL = (
    "SELECT e.emuserid::text AS emuserid, e.username, b.vin, b.model, b.color, b.framenumber, b.imei, b.nickname "
    "FROM emuser e "
    "LEFT JOIN userbikemap m ON m.emuserid = e.emuserid AND m.primarymapping "
    "LEFT JOIN bike b ON b.vin = m.bikevin "
    "WHERE e.phone = ANY(%s) ORDER BY m.createdat NULLS LAST, b.vin"
)
_SERVICE_SQL = "SELECT vin, bikemodel, odometer, services FROM servicedetail WHERE emuserid = %s::uuid"
_DONE_SQL = "SELECT servicetype FROM servicehistory WHERE emuserid = %s::uuid"
_TYPES_SQL = "SELECT id, servicename, kmtravelled, months FROM servicetype ORDER BY kmtravelled"
_TRIPS_SQL = (
    "SELECT tripid, vin, startsat, endsat, distance, duration FROM trips "
    "WHERE emuserid = %s ORDER BY startsat DESC LIMIT %s"
)


class AmigoUnavailable(Exception):
    """Amigo could not be read. The message is the underlying class name only."""


def database_names(userbike_db: str) -> Dict[str, str]:
    if not userbike_db.endswith("userbike"):
        raise ValueError("%s must name the userbike database, not %r" % (DSN_ENV, userbike_db))
    prefix = userbike_db[: -len("userbike")]
    return {"userbike": userbike_db, "garage": prefix + "garage", "ride": prefix + "ride"}


def _dbname_of(dsn: str) -> str:
    if "://" in dsn:
        return urlsplit(dsn).path.lstrip("/")
    match = re.search(r"(?:^|\s)dbname=(\S+)", dsn)
    return match.group(1) if match else ""


def phone_forms(phone: str) -> List[str]:
    """Amigo stores E.164 (+91...); a row written by hand may hold ten digits."""
    digits = re.sub(r"\D", "", phone or "")
    national = digits[-10:]
    return ["+91" + national, national] if len(national) == 10 else []


def _psycopg_connect(dsn: str, **kwargs: Any) -> Any:
    # Imported here: tests inject a connection, and a server without the DSN
    # never needs the driver.
    import psycopg

    return psycopg.connect(dsn, **kwargs)


class AmigoReader:
    def __init__(self, dsn: str, connect: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._dsn = dsn
        self._databases = database_names(_dbname_of(dsn))
        self._connect = connect or _psycopg_connect
        self._clock = clock
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def _query(self, database: str, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        try:
            with self._connect(self._dsn, dbname=self._databases[database],
                               connect_timeout=CONNECT_TIMEOUT_SECONDS, application_name=APPLICATION_NAME) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    names = [column[0] for column in cursor.description]
                    return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as exc:
            # The class only: a driver error can quote the connection string.
            raise AmigoUnavailable(type(exc).__name__) from None

    def bikes(self, phone: str) -> Optional[Dict[str, Any]]:
        forms = phone_forms(phone)
        if not forms:
            return None
        with self._lock:
            cached = self._cache.get(forms[0])
            if cached and self._clock() - cached[0] < BIKES_TTL_SECONDS:
                return cached[1]
        rows = self._query("userbike", _BIKES_SQL, [forms])
        rider: Optional[Dict[str, Any]] = None
        if rows:
            rider = {"emuserid": rows[0]["emuserid"], "username": rows[0]["username"], "bikes": [
                {key: row[key] for key in ("vin", "model", "color", "framenumber", "imei", "nickname")}
                for row in rows if row.get("vin")
            ]}
        with self._lock:
            self._cache[forms[0]] = (self._clock(), rider)
        return rider

    def service_status(self, phone: str) -> Optional[Dict[str, Any]]:
        rider = self.bikes(phone)
        if rider is None:
            return None
        detail = self._query("garage", _SERVICE_SQL, [rider["emuserid"]])
        if not detail:
            return None
        done = self._query("garage", _DONE_SQL, [rider["emuserid"]])
        types = self._query("garage", _TYPES_SQL, [])
        row = detail[0]
        return {"vin": row["vin"], "bikemodel": row["bikemodel"], "odometer": row["odometer"],
                "services": row["services"] or {}, "done_types": {d["servicetype"] for d in done}, "types": types}

    def recent_trips(self, phone: str, limit: int = 5) -> List[Dict[str, Any]]:
        rider = self.bikes(phone)
        if rider is None:
            return []
        return self._query("ride", _TRIPS_SQL, [rider["emuserid"], limit])


def from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[AmigoReader]:
    env = environ if environ is not None else os.environ
    dsn = (env.get(DSN_ENV) or "").strip()
    return AmigoReader(dsn) if dsn else None


# Amigo stores a model code; riders know the name. The knowledge base filters
# by name (a "doodle" record applies to "Doodle Pro"), so this matters beyond
# display. An unknown code is shown as it is.
MODEL_NAMES = {
    "EMXPLUS": "EMX Plus", "EMX": "EMX", "DOODLEPRO": "Doodle Pro", "TREXAIR": "T-Rex Air",
    "TREXPLUS": "T-Rex Plus", "TREXPLUSV2": "T-Rex Plus V2", "TREXPLUSV3": "T-Rex Plus V3",
    "TREXSMART": "T-Rex Smart", "X1": "X1", "X2": "X2", "X3": "X3", "S2": "S2", "DYNEM": "Dynem", "DY": "Dynem",
}
# The Amigo app's name for a rider who never set one.
_DEFAULT_USERNAME = "User"


def display_model(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return MODEL_NAMES.get(code.strip().upper(), code.strip())


def frame_on_record(bike: Mapping[str, Any]) -> bool:
    """Since July 2026 the app registers by IMEI and stores it as the frame
    number: that is not a frame number, and must never be shown as one."""
    frame = (bike.get("framenumber") or "").strip()
    if not frame or frame == (bike.get("imei") or "").strip():
        return False
    return not (frame.isdigit() and len(frame) == 15)


def _norm(frame: Optional[str]) -> str:
    return re.sub(r"\s+", "", frame or "").upper()


def amigo_records(rider: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The rider's app bikes, shaped like OMS records so the warranty tool,
    tickets and orders handle them without knowing where they came from."""
    if not rider:
        return []
    username = (rider.get("username") or "").strip()
    name = username if username and username != _DEFAULT_USERNAME else None
    records = []
    for bike in rider.get("bikes") or []:
        on_record = frame_on_record(bike)
        frame = (bike.get("framenumber") or "").strip() if on_record else None
        records.append({
            "customer_name": name,
            "frame_number": frame,
            "bike_ref": frame or "vin:%s" % bike["vin"],
            "frame_on_record": on_record,
            "product_name": display_model(bike.get("model")),
            "product_color": (bike.get("color") or "").strip().capitalize(),
            "warranty_on_record": False,
            "in_app": True,
            "vin": bike["vin"],
        })
    return records


def merged_source(oms_source: Callable[[str], Optional[List[Dict[str, Any]]]], reader: Any
                  ) -> Callable[[str], Optional[List[Dict[str, Any]]]]:
    """The OMS's bikes and the rider's app bikes, one list (spec section 2).

    OMS records come first and keep their warranty; an app bike with the same
    frame number (ignoring case and spaces) only marks it `in_app`. The rest
    of the app bikes follow, with no warranty on record. Amigo down leaves the
    OMS list; the OMS down leaves the app bikes, with warranty unavailable.
    """
    from .registry import ToolError  # local: registry imports tools, not the reverse

    def source(phone: str) -> Optional[List[Dict[str, Any]]]:
        oms_error = None
        try:
            oms = list(oms_source(phone) or [])
        except ToolError as exc:
            if exc.code != "oms_unavailable":
                raise
            oms, oms_error = [], exc
        try:
            app = amigo_records(reader.bikes(phone))
        except AmigoUnavailable as exc:
            _logger.warning("amigo_unavailable (%s): carrying on with the OMS bikes only", exc)
            if oms_error is not None:
                raise oms_error
            return oms or None
        unmatched = {_norm(a["frame_number"]): a for a in app if a["frame_on_record"]}
        merged = []
        for record in oms:
            match = unmatched.pop(_norm(record.get("frame_number")), None)
            merged.append(dict(record, in_app=True) if match else record)
        extra = [a for a in app if not a["frame_on_record"] or _norm(a["frame_number"]) in unmatched]
        if oms_error is not None:
            if not extra:
                raise oms_error
            extra = [dict(a, warranty_unavailable=True) for a in extra]
        return (merged + extra) or None

    return source
