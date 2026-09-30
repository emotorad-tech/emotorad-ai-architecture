# Amigo Read-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The chatbot reads a verified rider's Amigo bikes, service status and recent trips, read-only, merges the bikes with the OMS's, and gives the battery and motor agents two troubleshooting tools.

**Architecture:** A new module `src/emotorad_ai/tools/amigo.py` holds a read-only `AmigoReader` (psycopg, three SQL reads by phone, a 5-minute bike cache) and `merged_source`, which wraps the existing warranty source so every consumer (hydration, verify first, triage, context, tickets) sees one merged list. Bikes get a `bike_ref` (frame number, or `vin:<VIN>` when the frame is not on record) that selection and ownership checks use. Two tools are registered only when a reader exists.

**Tech Stack:** Python 3.12, psycopg 3 (new), unittest, the existing registry and runtime.

**Spec:** `docs/superpowers/specs/2026-09-30-amigo-read-only-design.md`

## Global Constraints

- Read-only, always: only SELECT statements; the `ro_chatbot` role enforces it too. Nothing is written to Amigo.
- Never log, print or return the DSN, a password, a VIN, an IMEI or a `vin:` reference in anything a rider or the model reads. Errors carry the exception class only.
- Never connect a test to the real Amigo database. Unit tests use `tests/amigo_fake.py`. The real SQL is checked only by `scripts/amigo_staging/local_check.sh` against a local Postgres.
- `EMOTORAD_AMIGO_PG_DSN` unset means behaviour exactly as today.
- British English; no em dashes in anything added or edited.
- Never push; commit locally after each task. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Full suite: `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN PYTHONPATH="src;." python -m unittest discover -s tests -t .` (three failures in `test_video` and `test_start` are pre-existing on this machine).
- Edit files with the Edit and Write tools, not shell heredocs (the Bash tool mangles backslashes).

## Review Focus

1. Amigo stores phones as E.164 (`+91…`), but a hand-written row may hold ten digits: the reader must match both forms. Tested in Task 1.
2. The same bike in the OMS and Amigo with the frame number in different case or with spaces must appear once. Tested in Task 2.
3. Amigo slow or down during verification must not stop the rider verifying: the OMS list (or none) stands, and `amigo_unavailable` is logged. Tested in Task 2 and Task 5.
4. Rider B's IMEI and generated VIN must never reach a reply, the context block, a model request or a summary. Tested in Task 3 and Task 5.
5. A ticket for a bike whose frame number is not on record accepts the frame number the rider reads out, marked as rider-read; a replacement order for it is refused. Tested in Task 3.

---

### Task 1: The Amigo reader

**Files:**
- Create: `src/emotorad_ai/tools/amigo.py`
- Create: `tests/amigo_fake.py` (the shared fake, used from Task 2 on)
- Modify: `requirements.txt` (psycopg)
- Test: `tests/test_amigo_reader.py`

**Interfaces:**
- Produces:
  - `DSN_ENV = "EMOTORAD_AMIGO_PG_DSN"`, `BIKES_TTL_SECONDS = 300`
  - `class AmigoUnavailable(Exception)`; `str(exc)` is the underlying exception's class name.
  - `database_names(userbike_db: str) -> Dict[str, str]` (keys `userbike`, `garage`, `ride`)
  - `phone_forms(phone: str) -> List[str]` (`["+919700000031", "9700000031"]`)
  - `AmigoReader(dsn: str, connect=None, clock=time.monotonic)` with:
    - `bikes(phone) -> Optional[Dict]`: `{"emuserid": str, "username": Optional[str], "bikes": [{"vin", "model", "color", "framenumber", "imei", "nickname"}]}`, or None when no rider has that phone; cached 5 minutes, None included.
    - `service_status(phone) -> Optional[Dict]`: `{"vin", "bikemodel", "odometer", "services": {name: status}, "done_types": set(int), "types": [{"id", "servicename", "kmtravelled", "months"}]}`, or None.
    - `recent_trips(phone, limit=5) -> List[Dict]`: `[{"tripid", "vin", "startsat", "endsat", "distance", "duration"}]`, newest first.
    - Every failure raises `AmigoUnavailable`.
  - `from_env(environ=None) -> Optional[AmigoReader]`

- [ ] **Step 1: Write the fake connection and the failing tests**

Create `tests/amigo_fake.py`:

```python
"""A stand-in for Amigo, for the tests: riders A, B and C of
scripts/amigo_staging, the same shapes the reader returns. Never a database."""

from emotorad_ai.tools.amigo import AmigoUnavailable

RIDER_A = "+919700000031"
RIDER_B = "+919700000032"
RIDER_C = "+919700000033"

RIDERS = {
    RIDER_A: {"emuserid": "00000000-0000-4000-8000-00000000a001", "username": "TEST Rider A", "bikes": [
        {"vin": "TESTVIN00000000001", "model": "EMXPLUS", "color": "aqua", "framenumber": "TESTEMXP0000001",
         "imei": None, "nickname": "TEST city bike"},
        {"vin": "TESTVIN00000000002", "model": "DOODLEPRO", "color": "nativepop", "framenumber": "TESTDDLP0000002",
         "imei": None, "nickname": None}]},
    RIDER_B: {"emuserid": "00000000-0000-4000-8000-00000000a002", "username": "TEST Rider B", "bikes": [
        {"vin": "FRPVINTEST0000000000000b", "model": "TREXSMART", "color": "grey", "framenumber": "860000000000032",
         "imei": "860000000000032", "nickname": None}]},
    RIDER_C: {"emuserid": "00000000-0000-4000-8000-00000000a003", "username": "TEST Rider C", "bikes": [
        {"vin": "TESTVIN00000000003", "model": "TREXAIR", "color": "green", "framenumber": "TESTTREX0000003",
         "imei": None, "nickname": None}]},
}

SERVICE_TYPES = [
    {"id": 1, "servicename": "serviceOne", "kmtravelled": 250, "months": 1},
    {"id": 2, "servicename": "serviceTwo", "kmtravelled": 1000, "months": 6},
    {"id": 3, "servicename": "serviceThree", "kmtravelled": 2000, "months": 12},
]

SERVICE_C = {"vin": "TESTVIN00000000003", "bikemodel": "TREXAIR", "odometer": 1180,
             "services": {"serviceOne": "complete", "serviceTwo": "pending", "serviceThree": "upcoming"},
             "done_types": {1}, "types": SERVICE_TYPES}

TRIPS_C = [
    {"tripid": "TESTTRIP-0003", "vin": "TESTVIN00000000003", "startsat": 1790647800000, "endsat": 1790650320000,
     "distance": 12.6, "duration": 2520},
    {"tripid": "TESTTRIP-0002", "vin": "TESTVIN00000000003", "startsat": 1790598900000, "endsat": 1790600460000,
     "distance": 11.9, "duration": 1560},
    {"tripid": "TESTTRIP-0001", "vin": "TESTVIN00000000003", "startsat": 1790476800000, "endsat": 1790479260000,
     "distance": 12.4, "duration": 2460},
]


class FakeAmigo:
    """The reader's interface over the data above. `down=True` makes every
    read fail as the real reader does when the database cannot be reached."""

    def __init__(self, down=False):
        self.down = down
        self.calls = []

    def _check(self, name, phone):
        self.calls.append((name, phone))
        if self.down:
            raise AmigoUnavailable("OperationalError")

    def bikes(self, phone):
        self._check("bikes", phone)
        return RIDERS.get(phone)

    def service_status(self, phone):
        self._check("service_status", phone)
        return SERVICE_C if phone == RIDER_C else None

    def recent_trips(self, phone, limit=5):
        self._check("recent_trips", phone)
        return TRIPS_C[:limit] if phone == RIDER_C else []
```

Create `tests/test_amigo_reader.py`:

```python
"""The Amigo reader: its SQL goes to the right database with the right
parameters, it caches a rider's bikes, and it fails as AmigoUnavailable with
nothing secret in the message. A fake connection stands in for psycopg."""

import unittest

from emotorad_ai.tools.amigo import (
    AmigoReader,
    AmigoUnavailable,
    database_names,
    from_env,
    phone_forms,
)

DSN = "postgresql://ro_chatbot:s3cretpass@amigo-stage-db.example:5432/userbike?sslmode=require"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.conn.executed.append((self.conn.dbname, " ".join(sql.split()), list(params)))
        columns, rows = self.conn.answer(self.conn.dbname, sql, params)
        self.description = [(name,) for name in columns]
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, owner, dbname):
        self.owner, self.dbname = owner, dbname
        self.executed, self.answer = owner.executed, owner.answer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self)


class FakeServer:
    """Answers the reader's three kinds of query from canned rows."""

    def __init__(self, fail=None):
        self.executed, self.connects, self.fail = [], [], fail

    def connect(self, dsn, **kwargs):
        self.connects.append(kwargs)
        if self.fail:
            raise self.fail
        return FakeConnection(self, kwargs["dbname"])

    def answer(self, dbname, sql, params):
        if "FROM emuser" in sql:
            return (["emuserid", "username", "vin", "model", "color", "framenumber", "imei", "nickname"],
                    [("u-1", "TEST Rider A", "V1", "EMXPLUS", "aqua", "F1", None, None),
                     ("u-1", "TEST Rider A", "V2", "DOODLEPRO", "nativepop", "F2", None, None)])
        if "FROM servicedetail" in sql:
            return (["vin", "bikemodel", "odometer", "services"],
                    [("V1", "EMXPLUS", 300, {"serviceOne": "pending"})])
        if "FROM servicehistory" in sql:
            return (["servicetype"], [(1,)])
        if "FROM servicetype" in sql:
            return (["id", "servicename", "kmtravelled", "months"], [(1, "serviceOne", 250, 1)])
        if "FROM trips" in sql:
            return (["tripid", "vin", "startsat", "endsat", "distance", "duration"],
                    [("T1", "V1", 1790647800000, 1790650320000, 12.6, 2520)])
        raise AssertionError("unexpected SQL: " + sql)


def reader(server, clock=None):
    return AmigoReader(DSN, connect=server.connect, **({"clock": clock} if clock else {}))


class NamesTests(unittest.TestCase):
    def test_the_other_databases_follow_the_userbike_name(self):
        self.assertEqual(database_names("userbike"), {"userbike": "userbike", "garage": "garage", "ride": "ride"})
        self.assertEqual(database_names("revalt_userbike")["ride"], "revalt_ride")

    def test_a_dsn_for_another_database_is_refused(self):
        with self.assertRaises(ValueError):
            AmigoReader("postgresql://u:p@h:5432/garage", connect=FakeServer().connect)

    def test_both_phone_forms(self):
        self.assertEqual(phone_forms("+919700000031"), ["+919700000031", "9700000031"])
        self.assertEqual(phone_forms("9700000031"), ["+919700000031", "9700000031"])


class ReadTests(unittest.TestCase):
    def test_bikes_come_from_userbike_by_both_phone_forms(self):
        server = FakeServer()
        rider = reader(server).bikes("+919700000031")
        self.assertEqual(rider["emuserid"], "u-1")
        self.assertEqual([b["vin"] for b in rider["bikes"]], ["V1", "V2"])
        dbname, sql, params = server.executed[0]
        self.assertEqual(dbname, "userbike")
        self.assertIn("primarymapping", sql)
        self.assertEqual(params, [["+919700000031", "9700000031"]])

    def test_every_connection_is_short_and_named(self):
        server = FakeServer()
        reader(server).bikes("+919700000031")
        self.assertEqual(server.connects[0]["connect_timeout"], 3)
        self.assertEqual(server.connects[0]["application_name"], "emotorad-ai-chatbot")

    def test_bikes_are_cached_for_five_minutes(self):
        server, now = FakeServer(), [0.0]
        r = reader(server, clock=lambda: now[0])
        r.bikes("+919700000031")
        r.bikes("+919700000031")
        self.assertEqual(len(server.executed), 1)
        now[0] += 301
        r.bikes("+919700000031")
        self.assertEqual(len(server.executed), 2)

    def test_service_status_reads_garage_by_the_riders_id(self):
        server = FakeServer()
        status = reader(server).service_status("+919700000031")
        self.assertEqual(status["odometer"], 300)
        self.assertEqual(status["done_types"], {1})
        garage = [e for e in server.executed if e[0] == "garage"]
        self.assertEqual(garage[0][2], ["u-1"])

    def test_recent_trips_read_ride_newest_first(self):
        server = FakeServer()
        trips = reader(server).recent_trips("+919700000031", limit=5)
        self.assertEqual(trips[0]["tripid"], "T1")
        dbname, sql, params = [e for e in server.executed if e[0] == "ride"][0]
        self.assertIn("ORDER BY startsat DESC", sql)
        self.assertEqual(params, ["u-1", 5])

    def test_only_selects(self):
        server = FakeServer()
        r = reader(server)
        r.bikes("+919700000031"); r.service_status("+919700000031"); r.recent_trips("+919700000031")
        for _, sql, _ in server.executed:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)


class FailureTests(unittest.TestCase):
    def test_a_failure_is_amigo_unavailable_with_the_class_name_only(self):
        server = FakeServer(fail=ConnectionError("could not connect to ro_chatbot:s3cretpass@amigo-stage-db"))
        with self.assertRaises(AmigoUnavailable) as caught:
            reader(server).bikes("+919700000031")
        self.assertEqual(str(caught.exception), "ConnectionError")
        self.assertNotIn("s3cretpass", repr(caught.exception))


class FromEnvTests(unittest.TestCase):
    def test_no_dsn_no_reader(self):
        self.assertIsNone(from_env({}))
        self.assertIsNone(from_env({"EMOTORAD_AMIGO_PG_DSN": "  "}))

    def test_a_dsn_makes_a_reader(self):
        self.assertIsInstance(from_env({"EMOTORAD_AMIGO_PG_DSN": DSN}), AmigoReader)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_reader -v`
Expected: `ModuleNotFoundError: No module named 'emotorad_ai.tools.amigo'`.

- [ ] **Step 3: Write the reader**

Create `src/emotorad_ai/tools/amigo.py`:

```python
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

import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

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
```

In `requirements.txt`, after the `pymongo` line:

```
# Amigo, read-only (src/emotorad_ai/tools/amigo.py), when EMOTORAD_AMIGO_PG_DSN is set.
psycopg[binary]>=3.1
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_reader -v`
Expected: all pass. (psycopg itself is not needed: the tests inject the connection.)

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/tools/amigo.py tests/amigo_fake.py tests/test_amigo_reader.py requirements.txt
git commit -m "Amigo: a read-only reader for a rider's bikes, service status and trips

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: One bike list from the OMS and Amigo

**Files:**
- Modify: `src/emotorad_ai/tools/amigo.py` (model names, `amigo_records`, `merged_source`)
- Modify: `src/emotorad_ai/tools/mocks.py` (`_coverage`: `bike_ref`, `frame_on_record`, `in_app`, two new coverage states)
- Test: `tests/test_amigo_source.py`

**Interfaces:**
- Consumes: Task 1 (`AmigoUnavailable`, the reader's `bikes`), `tests/amigo_fake.py`.
- Produces:
  - `display_model(code: Optional[str]) -> Optional[str]`
  - `frame_on_record(bike: Dict) -> bool` (false when the frame number is empty, equals the IMEI, or is 15 digits)
  - `amigo_records(rider: Optional[Dict]) -> List[Dict]`: OMS-shaped records with `frame_number` (None when not on record), `bike_ref`, `frame_on_record`, `product_name`, `product_color`, `warranty_on_record: False`, `in_app: True`, `vin`, `customer_name`
  - `merged_source(oms_source: Callable[[str], Optional[List[Dict]]], reader) -> Callable[[str], Optional[List[Dict]]]`
  - `_coverage` output gains `bike_ref`, `frame_on_record`, `in_app`; `coverage_status` may be `"not_registered"` or `"warranty_unavailable"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_amigo_source.py`:

```python
"""One bike list from the OMS and Amigo (spec 2026-09-30, section 2)."""

import unittest
from datetime import date

from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import amigo_records, display_model, frame_on_record, merged_source
from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, build_registry
from emotorad_ai.tools.registry import ToolContext, ToolError
from tests.amigo_fake import RIDER_A, RIDER_B, RIDER_C, RIDERS, FakeAmigo

TODAY = date(2026, 9, 30)


def fixture_oms(phone):
    return fixtures.WARRANTY_RECORDS.get(phone)


def lookup(source, phone):
    registry = build_registry(today=TODAY, warranty_source=source)
    return registry.call(LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1", phone=phone))


class ModelNameTests(unittest.TestCase):
    def test_codes_become_the_names_riders_know(self):
        self.assertEqual(display_model("EMXPLUS"), "EMX Plus")
        self.assertEqual(display_model("DOODLEPRO"), "Doodle Pro")
        self.assertEqual(display_model("TREXSMART"), "T-Rex Smart")
        self.assertEqual(display_model("dy"), "Dynem")
        self.assertEqual(display_model("NEWMODEL9"), "NEWMODEL9")


class FrameOnRecordTests(unittest.TestCase):
    def test_an_imei_is_not_a_frame_number(self):
        self.assertFalse(frame_on_record({"framenumber": "860000000000032", "imei": "860000000000032"}))
        self.assertFalse(frame_on_record({"framenumber": "123456789012345", "imei": None}))
        self.assertFalse(frame_on_record({"framenumber": "", "imei": None}))
        self.assertTrue(frame_on_record({"framenumber": "TESTEMXP0000001", "imei": None}))


class AmigoRecordsTests(unittest.TestCase):
    def test_rider_b_has_no_frame_number_and_a_vin_reference(self):
        [record] = amigo_records(RIDERS[RIDER_B])
        self.assertIsNone(record["frame_number"])
        self.assertFalse(record["frame_on_record"])
        self.assertEqual(record["bike_ref"], "vin:FRPVINTEST0000000000000b")
        self.assertEqual(record["product_name"], "T-Rex Smart")
        self.assertEqual(record["product_color"], "Grey")
        self.assertFalse(record["warranty_on_record"])

    def test_the_default_username_is_not_a_name(self):
        rider = dict(RIDERS[RIDER_A], username="User")
        self.assertIsNone(amigo_records(rider)[0]["customer_name"])
        self.assertEqual(amigo_records(RIDERS[RIDER_A])[0]["customer_name"], "TEST Rider A")


class MergeTests(unittest.TestCase):
    def test_amigo_only_rider_gets_their_app_bikes_with_no_warranty_on_record(self):
        envelope = lookup(merged_source(fixture_oms, FakeAmigo()), RIDER_A)
        bikes = envelope["data"]["bikes"]
        self.assertEqual([b["product_name"] for b in bikes], ["EMX Plus", "Doodle Pro"])
        self.assertEqual({b["coverage_status"] for b in bikes}, {"not_registered"})
        self.assertTrue(all(b["in_warranty"] is None for b in bikes))
        self.assertEqual(envelope["data"]["customer_name"], "TEST Rider A")

    def test_oms_only_rider_is_unchanged(self):
        plain = lookup(None, "+919876543210")["data"]["bikes"]
        merged = lookup(merged_source(fixture_oms, FakeAmigo()), "+919876543210")["data"]["bikes"]
        self.assertEqual([b["frame_number"] for b in merged], [b["frame_number"] for b in plain])
        self.assertEqual(merged[0]["coverage_status"], plain[0]["coverage_status"])
        self.assertFalse(merged[0]["in_app"])

    def test_a_bike_in_both_is_listed_once_ignoring_case_and_spaces(self):
        def oms(phone):
            return [dict(fixtures.WARRANTY_RECORDS["+919876543210"][0], frame_number="testemxp 0000001",
                         mobile=RIDER_A, customer_name="Ananya Rao")]
        bikes = lookup(merged_source(oms, FakeAmigo()), RIDER_A)["data"]["bikes"]
        self.assertEqual(len(bikes), 2)  # the EMX Plus once, the Doodle Pro from the app
        self.assertTrue(bikes[0]["in_app"])
        self.assertNotEqual(bikes[0]["coverage_status"], "not_registered")
        self.assertEqual(bikes[1]["product_name"], "Doodle Pro")

    def test_amigo_down_leaves_the_oms_list(self):
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING") as logs:
            bikes = lookup(merged_source(fixture_oms, FakeAmigo(down=True)), "+919876543210")["data"]["bikes"]
        self.assertEqual(len(bikes), 1)
        self.assertIn("amigo_unavailable", "\n".join(logs.output))

    def test_amigo_down_and_no_oms_record_is_no_record(self):
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING"):
            envelope = lookup(merged_source(fixture_oms, FakeAmigo(down=True)), RIDER_A)
        self.assertEqual(envelope["error"]["code"], "no_warranty_record")

    def test_oms_down_still_lists_the_app_bikes(self):
        def down(phone):
            raise ToolError("oms_unavailable", "The warranty system is not responding.", retryable=True)
        bikes = lookup(merged_source(down, FakeAmigo()), RIDER_A)["data"]["bikes"]
        self.assertEqual({b["coverage_status"] for b in bikes}, {"warranty_unavailable"})

    def test_oms_down_and_no_app_bikes_is_the_oms_error(self):
        def down(phone):
            raise ToolError("oms_unavailable", "The warranty system is not responding.", retryable=True)
        envelope = lookup(merged_source(down, FakeAmigo()), "+919876543210")
        self.assertEqual(envelope["error"]["code"], "oms_unavailable")

    def test_neither_is_no_record(self):
        self.assertEqual(lookup(merged_source(fixture_oms, FakeAmigo()), "+919700000099")["error"]["code"],
                         "no_warranty_record")

    def test_bike_ref_is_the_frame_number_when_on_record(self):
        bikes = lookup(merged_source(fixture_oms, FakeAmigo()), RIDER_C)["data"]["bikes"]
        self.assertEqual(bikes[0]["bike_ref"], "TESTTREX0000003")
        self.assertTrue(bikes[0]["frame_on_record"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_source -v`
Expected: ImportError for `amigo_records`.

- [ ] **Step 3: Add the merge to `tools/amigo.py`**

Add `import logging` to the imports, `_logger = logging.getLogger(__name__)` after them, and at the end of the module:

```python
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
    """The OMS's bikes and the rider's app bikes, one list (spec section 2)."""
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
        by_frame = {_norm(a["frame_number"]): a for a in app if a["frame_on_record"]}
        merged = []
        for record in oms:
            match = by_frame.pop(_norm(record.get("frame_number")), None)
            merged.append(dict(record, in_app=True) if match else record)
        extra = [a for a in app if not a["frame_on_record"] or _norm(a["frame_number"]) in by_frame]
        if oms_error is not None:
            if not extra:
                raise oms_error
            extra = [dict(a, warranty_unavailable=True) for a in extra]
        records = merged + extra
        return records or None

    return source
```

- [ ] **Step 4: Teach `_coverage` the new fields**

In `src/emotorad_ai/tools/mocks.py`, `_coverage`, change `"frame_number": record["frame_number"],` to:

```python
        "frame_number": record.get("frame_number"),
        # The key selection and ownership checks use: the frame number, or
        # `vin:<VIN>` for a bike whose frame number is not on record (an app
        # bike registered by IMEI). Never shown to the rider.
        "bike_ref": record.get("bike_ref") or record.get("frame_number"),
        "frame_on_record": record.get("frame_on_record", True),
        "in_app": bool(record.get("in_app")),
```

and insert, straight after the `bike = {...}` dict and before `started = ...`:

```python
    if record.get("warranty_unavailable"):
        bike.update({
            "in_warranty": None,
            "coverage_status": "warranty_unavailable",
            "note": ("The warranty system is not responding, so coverage cannot be checked right now. "
                     "Say so plainly. Do not state or estimate coverage."),
        })
        return bike
    if record.get("warranty_on_record") is False:
        bike.update({
            "in_warranty": None,
            "coverage_status": "not_registered",
            "remedy": "late_warranty_registration",
            "note": ("This bike is in the EMotorad app but is not registered for warranty with EMotorad. "
                     "Do not state or estimate coverage. If warranty matters to what they need, offer to "
                     "register it."),
        })
        return bike
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_source tests.test_amigo_reader tests.test_agent_and_runtime -v 2>&1 | tail -3`
Expected: OK. Then the full suite; expected only the three pre-existing failures.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/tools/amigo.py src/emotorad_ai/tools/mocks.py tests/test_amigo_source.py
git commit -m "Amigo: one bike list from the OMS and the app, warranty from the OMS only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Bikes without a frame number, chosen and shown correctly

**Files:**
- Modify: `src/emotorad_ai/triage.py` (`bike_ref`, `describe_bike`, selection)
- Modify: `src/emotorad_ai/runtime.py` (`_selected_bike`, `_redaction_terms`, the safety branch's ticket argument)
- Modify: `src/emotorad_ai/enrichment.py` (`_bikes_block`)
- Modify: `src/emotorad_ai/agents/battery_support.py` (`_describe`)
- Modify: `src/emotorad_ai/tools/mocks.py` (`_owned_bike`, `create_support_ticket`, `place_replacement_order`)
- Test: `tests/test_amigo_bikes_without_frame.py`

**Interfaces:**
- Consumes: Task 2 (`bike_ref`, `frame_on_record` on bikes and raw records).
- Produces: `triage.bike_ref(bike) -> Optional[str]` (`bike.get("bike_ref") or bike.get("frame_number")`); `_owned_bike(phone, frame_number, bikes_on, allow_rider_read=False)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_amigo_bikes_without_frame.py`:

```python
"""A bike whose frame number is not on record (an app bike registered by
IMEI): chosen by its internal reference, shown as "frame number not on
record", never showing its IMEI or VIN (spec section 2)."""

import unittest
from datetime import date

from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, ConversationState
from emotorad_ai.enrichment import ContextEnricher
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import merged_source
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.triage import TOPIC_KEYWORDS, TriageAgent, bike_ref, describe_bike, which_bike_text
from tests.amigo_fake import RIDER_A, RIDER_B, FakeAmigo

TODAY = date(2026, 9, 30)
SECRET_BITS = ("860000000000032", "FRPVINTEST", "vin:")


def registry(**kwargs):
    return build_registry(today=TODAY, warranty_source=merged_source(fixtures.WARRANTY_RECORDS.get, FakeAmigo()),
                          **kwargs)


def resolved_for(phone, reg):
    message = InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="hi",
                             identity=Identity(strength=VERIFIED, phone=phone, em_aid="a"))
    return IdentityResolver(reg).hydrate(message)


class ShownTests(unittest.TestCase):
    def setUp(self):
        self.reg = registry()
        self.b = resolved_for(RIDER_B, self.reg)

    def test_the_list_says_frame_number_not_on_record(self):
        self.assertEqual(describe_bike(self.b.bikes[0]), "T-Rex Smart (Grey), frame number not on record")
        text = which_bike_text(self.b.bikes)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, text)

    def test_the_context_never_shows_the_imei_or_vin(self):
        block = ContextEnricher().build(self.b).render()
        self.assertIn("frame number not on record", block)
        self.assertIn("warranty not on record", block)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, block)


class ChosenTests(unittest.TestCase):
    def test_a_bike_without_a_frame_is_selected_by_its_reference(self):
        reg = registry()
        who = resolved_for(RIDER_B, reg)
        state = ConversationState("c1")
        state.move_to(AWAITING_BIKE_SELECTION, "verified")
        state.pending_topic = "battery"
        outcome = TriageAgent({"battery": "battery_support"}, unlisted_agent="late").handle(
            InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="yes",
                           identity=Identity(strength=VERIFIED, phone=RIDER_B, em_aid="a")), who, state)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.selected_frame, "vin:FRPVINTEST0000000000000b")
        self.assertEqual(bike_ref(who.bikes[0]), state.selected_frame)


class TicketTests(unittest.TestCase):
    def call(self, reg, name, arguments, phone=RIDER_B):
        return reg.call(name, arguments, ToolContext(conversation_id="c1", phone=phone))

    def test_a_ticket_takes_the_frame_the_rider_reads_out_and_marks_it(self):
        reg = registry()
        envelope = self.call(reg, CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "description": "Battery not charging.", "severity": "normal",
            "idempotency_key": "k1", "frame_number": "TRSM2026009911"})
        self.assertNotIn("error", envelope, envelope)
        ticket = reg.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertEqual(ticket["frame_number"], "TRSM2026009911")
        self.assertEqual(ticket["frame_number_source"], "read by the rider")

    def test_a_ticket_without_a_frame_is_raised_without_one(self):
        reg = registry()
        envelope = self.call(reg, CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "description": "Battery not charging.", "severity": "normal",
            "idempotency_key": "k2"})
        ticket = reg.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertIsNone(ticket["frame_number"])

    def test_a_listed_bikes_frame_is_still_checked(self):
        reg = registry()
        envelope = self.call(reg, CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "description": "x", "severity": "normal", "idempotency_key": "k3",
            "frame_number": "NOTMINE123"}, phone=RIDER_A)
        self.assertEqual(envelope["error"]["code"], "frame_number_not_owned")

    def test_a_replacement_for_a_bike_without_a_frame_is_refused(self):
        from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders, load_parts_table

        reg = registry(replacement_orders=ReplacementOrders(), item_codes=ItemCodes(), approval_mode="reasonable")
        part = next(p for p, r in load_parts_table().items() if not r.technician and not r.ask)
        envelope = reg.call(
            PLACE_REPLACEMENT_ORDER, {"part": part, "use_record_address": True, "idempotency_key": "o1"},
            ToolContext(conversation_id="c1", phone=RIDER_B, late={
                "evidence_seen": lambda: True, "coverage_result": lambda: {}, "customer_messages": lambda: []}))
        self.assertEqual(envelope["error"]["code"], "frame_number_not_on_record")


if __name__ == "__main__":
    unittest.main()
```

(The mock ticket store is `registry.tickets`, a `MockTicketSystem` whose `tickets` dict holds each ticket's payload. `ToolContext.late` supplies the replacement tool's conversation facts, as `Agent.run` does.)

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_bikes_without_frame -v`
Expected: ImportError for `bike_ref`.

- [ ] **Step 3: Triage**

In `src/emotorad_ai/triage.py`, add after `describe_bike`'s imports section (above `describe_bike`):

```python
def bike_ref(bike: Dict[str, Any]) -> Optional[str]:
    """The key a bike is chosen by: its frame number, or an internal reference
    for a bike whose frame number is not on record. Never shown."""
    return bike.get("bike_ref") or bike.get("frame_number")
```

Replace `describe_bike` with:

```python
def describe_bike(bike: Dict[str, Any]) -> str:
    """One line of the list. The whole frame number, so the customer can match
    it to the sticker on the frame (the person's rule, 2026-09-30). A bike
    whose frame number is not on record says so, and never shows its IMEI."""
    name = bike.get("product_name") or "Your bike"
    if bike.get("product_color"):
        name += " (%s)" % bike["product_color"]
    frame = bike.get("frame_number")
    if frame:
        return "%s, frame %s" % (name, frame)
    if bike.get("frame_on_record") is False:
        return "%s, frame number not on record" % name
    return name
```

In `TriageAgent.handle`, `metadata={"bikes": [b["frame_number"] for b in bikes]}` becomes `metadata={"bikes": [bike_ref(b) for b in bikes]}` and `state.select_bike(bikes[0]["frame_number"])` becomes `state.select_bike(bike_ref(bikes[0]))`. In `_resolve_selection`, `state.select_bike(bike["frame_number"])` becomes `state.select_bike(bike_ref(bike))`.

- [ ] **Step 4: Runtime**

In `src/emotorad_ai/runtime.py` import `bike_ref` from `.triage` (with `TriageAgent`). Then:

- `_selected_bike`: `if bike.get("frame_number") == state.selected_frame:` becomes `if state.selected_frame and bike_ref(bike) == state.selected_frame:`.
- `_redaction_terms`: `terms.extend(bike.get("frame_number") or "" for bike in resolved.bikes)` becomes
  `terms.extend(term for bike in resolved.bikes for term in (bike.get("frame_number") or "", bike_ref(bike) or ""))`.
- The safety branch: `arguments["frame_number"] = resolved.single_bike["frame_number"]` becomes `arguments["frame_number"] = bike_ref(resolved.single_bike)`.

- [ ] **Step 5: The context and the battery prompt's bike line**

In `src/emotorad_ai/enrichment.py`, `_bikes_block`: before the final `lines.append(...)`, compute the frame text and the two new coverage states, replacing that append with:

```python
            if bike.get("coverage_status") == "not_registered":
                coverage = "warranty not on record (in the app only; offer to register it if warranty matters)"
            elif bike.get("coverage_status") == "warranty_unavailable":
                coverage = "warranty cannot be checked right now"
            frame = "frame %s" % bike["frame_number"] if bike.get("frame_number") else "frame number not on record"
            lines.append("- %s, %s, %s" % (descriptor, frame, coverage))
```

(The two new `coverage` cases go after the existing `if/elif` chain, so they win for those states.)

In `src/emotorad_ai/agents/battery_support.py`, `_describe`: `parts.append("frame %s" % bike["frame_number"])` becomes

```python
    parts.append("frame %s" % bike["frame_number"] if bike.get("frame_number") else "frame number not on record")
```

- [ ] **Step 6: Ownership checks**

In `src/emotorad_ai/tools/mocks.py`, replace `_owned_bike`'s signature and body from `owned = {...}` down with:

```python
def _owned_bike(
    phone: str,
    frame_number: Optional[str],
    bikes_on: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    allow_rider_read: bool = False,
) -> Optional[Dict[str, Any]]:
```

(keep the docstring, adding: "A bike whose frame number is not on record is matched by its internal reference; for a ticket, a frame number the customer reads off it is accepted and marked as rider-read.")

```python
    records = bikes_on(phone) if bikes_on else (fixtures.WARRANTY_RECORDS.get(phone) or [])
    if not records:
        return None  # no record at all; the ticket is still worth raising

    def ref(record: Dict[str, Any]) -> Optional[str]:
        return record.get("bike_ref") or record.get("frame_number")

    if frame_number:
        wanted = re.sub(r"\s+", "", frame_number).upper()
        for record in records:
            if frame_number == ref(record) or wanted == re.sub(r"\s+", "", record.get("frame_number") or "").upper():
                return record
        unknown = [r for r in records if r.get("frame_on_record") is False]
        if allow_rider_read and len(unknown) == 1:
            return dict(unknown[0], frame_number=frame_number.strip(), frame_number_source="read by the rider")
        raise ToolError(
            "frame_number_not_owned",
            "Frame number %s is not registered to this customer. Do not use a frame number "
            "the customer typed without checking it against lookup_warranty_record; ask them "
            "to confirm it from the sticker on the frame." % frame_number,
        )

    if len(records) > 1:
        raise ToolError(
            "frame_number_required",
            "This customer owns %d bikes, so the ticket needs a frame number. Ask which bike "
            "they mean and pass its frame number." % len(records),
        )
    return records[0]
```

Add `import re` to `mocks.py` if absent. In `create_support_ticket`: `bike = _owned_bike(phone, frame_number, bikes_on)` becomes `bike = _owned_bike(phone, frame_number, bikes_on, allow_rider_read=True)`, and `tickets.create(...)` gains `frame_number_source=bike.get("frame_number_source") if bike else None,`. In `place_replacement_order`, after `bike = _owned_bike(...)` and its None check, before `frame = bike["frame_number"]`:

```python
            if not bike.get("frame_number"):
                raise ToolError(
                    "frame_number_not_on_record",
                    "This bike's frame number is not on record, so a replacement cannot be ordered here. "
                    "Ask the customer to read the frame number off the sticker and raise a support ticket instead.",
                )
```

(The refusal is covered by `test_a_replacement_for_a_bike_without_a_frame_is_refused` from Step 1.)

- [ ] **Step 7: Run the tests**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_bikes_without_frame tests.test_triage tests.test_amigo_source tests.test_verify_first -v 2>&1 | tail -3`
Expected: OK. Then the full suite; expected only the three pre-existing failures.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/triage.py src/emotorad_ai/runtime.py src/emotorad_ai/enrichment.py src/emotorad_ai/agents/battery_support.py src/emotorad_ai/tools/mocks.py tests/test_amigo_bikes_without_frame.py
git commit -m "Bikes without a frame number on record: chosen by reference, never shown by IMEI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Two troubleshooting tools

**Files:**
- Modify: `src/emotorad_ai/tools/amigo.py` (`describe_service`, `describe_trips`)
- Modify: `src/emotorad_ai/tools/mocks.py` (`build_registry(amigo=...)`, `GET_SERVICE_STATUS`, `GET_RECENT_TRIPS`)
- Modify: `src/emotorad_ai/agents/battery_support.py`, `src/emotorad_ai/agents/motor_support.py` (`TOOL_NAMES`)
- Test: `tests/test_amigo_tools.py`

**Interfaces:**
- Consumes: Task 1 reader interface, `tests/amigo_fake.py`.
- Produces: `GET_SERVICE_STATUS = "get_service_status"`, `GET_RECENT_TRIPS = "get_recent_trips"`; `build_registry(..., amigo=None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_amigo_tools.py`:

```python
"""The two Amigo tools the battery and motor agents may call (spec section 3)."""

import unittest

from emotorad_ai.agents import battery_support, motor_support
from emotorad_ai.tools.mocks import GET_RECENT_TRIPS, GET_SERVICE_STATUS, build_registry
from emotorad_ai.tools.registry import ToolContext
from tests.amigo_fake import RIDER_A, RIDER_C, FakeAmigo


def call(reg, name, phone):
    return reg.call(name, {}, ToolContext(conversation_id="c1", phone=phone))


class ServiceStatusTests(unittest.TestCase):
    def test_rider_c(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_SERVICE_STATUS, RIDER_C)["data"]
        self.assertEqual(data["bike"], "T-Rex Air")
        self.assertEqual(data["odometer_km"], 1180)
        self.assertEqual(data["stages"], [
            {"stage": "250 km / 1 month", "status": "done"},
            {"stage": "1000 km / 6 months", "status": "due"},
            {"stage": "2000 km / 12 months", "status": "upcoming"},
        ])

    def test_no_record(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_SERVICE_STATUS, RIDER_A)["data"]
        self.assertIsNone(data["stages"])
        self.assertIn("No service record", data["note"])

    def test_amigo_down(self):
        envelope = call(build_registry(amigo=FakeAmigo(down=True)), GET_SERVICE_STATUS, RIDER_C)
        self.assertEqual(envelope["error"]["code"], "amigo_unavailable")
        self.assertTrue(envelope["error"]["retryable"])


class RecentTripsTests(unittest.TestCase):
    def test_rider_c_newest_first_in_ist(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_RECENT_TRIPS, RIDER_C)["data"]
        first = data["trips"][0]
        self.assertEqual(first["when"], "29 Sep 2026, 07:40")
        self.assertEqual(first["bike"], "T-Rex Air")
        self.assertEqual(first["distance_km"], 12.6)
        self.assertEqual(first["duration_min"], 42)
        self.assertEqual(first["average_kmh"], 18.0)
        self.assertEqual(len(data["trips"]), 3)

    def test_no_rides(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_RECENT_TRIPS, RIDER_A)["data"]
        self.assertEqual(data["trips"], [])
        self.assertIn("No rides", data["note"])

    def test_nothing_identifying_in_the_output(self):
        reg = build_registry(amigo=FakeAmigo())
        said = repr(call(reg, GET_RECENT_TRIPS, RIDER_C)) + repr(call(reg, GET_SERVICE_STATUS, RIDER_C))
        for bit in ("TESTVIN", "TESTTREX", "9700000033", "emuserid"):
            self.assertNotIn(bit, said)


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_reader(self):
        reg = build_registry()
        self.assertNotIn(GET_SERVICE_STATUS, reg.specs)
        self.assertNotIn(GET_RECENT_TRIPS, reg.specs)

    def test_the_battery_and_motor_agents_list_them(self):
        for agent in (battery_support, motor_support):
            self.assertIn(GET_SERVICE_STATUS, agent.TOOL_NAMES)
            self.assertIn(GET_RECENT_TRIPS, agent.TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_tools -v`
Expected: ImportError for `GET_RECENT_TRIPS`.

- [ ] **Step 3: The describers in `tools/amigo.py`**

Add `from datetime import datetime, timedelta, timezone` to the imports and at the end:

```python
IST = timezone(timedelta(hours=5, minutes=30))
_STATUS_WORDS = {"complete": "done", "pending": "due", "upcoming": "upcoming"}


def describe_service(status: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The rider's service stages in words, for the model. No identifiers."""
    if not status:
        return {"bike": None, "odometer_km": None, "stages": None, "note": "No service record in the app."}
    stages = []
    for kind in status["types"]:
        word = "done" if kind["id"] in status["done_types"] else _STATUS_WORDS.get(
            (status["services"] or {}).get(kind["servicename"], ""), "unknown")
        months = kind["months"]
        stages.append({"stage": "%d km / %d month%s" % (kind["kmtravelled"], months, "" if months == 1 else "s"),
                       "status": word})
    return {"bike": display_model(status["bikemodel"]), "odometer_km": status["odometer"], "stages": stages}


def _epoch_seconds(value: Any) -> float:
    value = float(value)
    return value / 1000.0 if value > 1e11 else value  # milliseconds, as the app writes them


def describe_trips(trips: Sequence[Mapping[str, Any]], rider: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The rider's recent rides in words, for the model. No identifiers or places."""
    models = {b["vin"]: display_model(b.get("model")) for b in (rider or {}).get("bikes") or []}
    rows = []
    for trip in trips:
        started = datetime.fromtimestamp(_epoch_seconds(trip["startsat"]), tz=IST)
        minutes = round(float(trip["duration"] or 0) / 60)
        distance = round(float(trip["distance"] or 0), 1)
        hours = float(trip["duration"] or 0) / 3600
        rows.append({
            "when": started.strftime("%d %b %Y, %H:%M"),
            "bike": models.get(trip["vin"]),
            "distance_km": distance,
            "duration_min": minutes,
            "average_kmh": round(distance / hours, 1) if hours else None,
        })
    return {"trips": rows, **({} if rows else {"note": "No rides in the app."})}
```

- [ ] **Step 4: The two tools in `build_registry`**

In `src/emotorad_ai/tools/mocks.py`: add the constants beside `GET_BATTERY_DIAGNOSTICS`:

```python
GET_SERVICE_STATUS = "get_service_status"
GET_RECENT_TRIPS = "get_recent_trips"
```

add the parameter to `build_registry` (beside `send_code`):

```python
    # The Amigo reader (tools/amigo.py), read-only. None: the two Amigo tools
    # are not registered, so no agent is told it can call them.
    amigo: Optional[Any] = None,
```

and, after the `if diagnostics_available:` block, register:

```python
    if amigo is not None:
        from .amigo import AmigoUnavailable, describe_service, describe_trips

        def _amigo_down(exc: Exception) -> ToolError:
            return ToolError("amigo_unavailable",
                             "The app's records cannot be read right now (%s). Carry on without them." % exc,
                             retryable=True)

        @registry.register(
            GET_SERVICE_STATUS,
            "The customer's service stages from the EMotorad app (250 km / 1 month, 1000 km / 6 months, "
            "2000 km / 12 months): done, due or upcoming, and the odometer. Use it when a motor, brake or "
            "noise problem might come from a missed service. It is the app's record, not a booking: never "
            "book or promise a service from it.",
            parameters={},
            injects=("phone",),
        )
        def get_service_status(phone: str) -> Dict[str, Any]:
            try:
                return ok(describe_service(amigo.service_status(phone)), freshness_seconds=300)
            except AmigoUnavailable as exc:
                raise _amigo_down(exc)

        @registry.register(
            GET_RECENT_TRIPS,
            "The customer's last five rides from the EMotorad app, newest first: when, which bike, distance "
            "in km, duration in minutes and average speed. Use it to check a range or power complaint "
            "against real rides. It holds no locations.",
            parameters={},
            injects=("phone",),
        )
        def get_recent_trips(phone: str) -> Dict[str, Any]:
            try:
                return ok(describe_trips(amigo.recent_trips(phone), amigo.bikes(phone)), freshness_seconds=300)
            except AmigoUnavailable as exc:
                raise _amigo_down(exc)
```

In `src/emotorad_ai/agents/battery_support.py` and `motor_support.py`, import `GET_RECENT_TRIPS, GET_SERVICE_STATUS` from `..tools.mocks` alongside the existing imports and append both to `TOOL_NAMES`.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_tools tests.test_self_service_identity -v 2>&1 | tail -3`
Expected: OK. Then the full suite.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/tools/amigo.py src/emotorad_ai/tools/mocks.py src/emotorad_ai/agents/battery_support.py src/emotorad_ai/agents/motor_support.py tests/test_amigo_tools.py
git commit -m "Amigo: service status and recent trips for the battery and motor agents

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Wire it into the web chat, with a flow test

**Files:**
- Modify: `src/emotorad_ai/api.py` (`AMIGO`, `_build_registry`, `/health`)
- Modify: `scripts/chat_local.py` (`OPTIONAL`)
- Test: `tests/test_amigo_flow.py`

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces: `api.AMIGO`; `/health` key `amigo`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_amigo_flow.py`:

```python
"""Amigo through a whole conversation, and the web chat's wiring."""

import importlib
import os
import unittest
from datetime import date
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.agents import battery_support
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import merged_source
from emotorad_ai.tools.mocks import GET_RECENT_TRIPS, build_registry
from emotorad_ai.tools.verification import VerificationStore
from tests.amigo_fake import RIDER_A, RIDER_B, RIDER_C, FakeAmigo

SECRET_BITS = ("860000000000032", "FRPVINTEST", "vin:", "TESTVIN")


class Chat:
    def __init__(self, replies=(), amigo=None):
        self.amigo = amigo or FakeAmigo()
        self.store = VerificationStore()
        self.registry = build_registry(
            verification=self.store, today=date(2026, 9, 30), amigo=self.amigo,
            warranty_source=merged_source(fixtures.WARRANTY_RECORDS.get, self.amigo))
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=self.registry,
                               llm=self.llm, log=EventLog(path=None), resolver=IdentityResolver(self.registry),
                               conversations=self.conversations, self_service_identity=True,
                               phone_resolver=self.store.verified_phone, verify_first=True)

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

    def verify(self, phone, first="my battery isn't charging"):
        self.say(first)
        self.say(phone[3:])
        return self.say(self.store.pending_code("c1"))


class FlowTests(unittest.TestCase):
    def test_rider_a_sees_both_app_bikes_and_the_agent_reads_trips(self):
        chat = Chat(replies=[call_tool(GET_RECENT_TRIPS, {}, "t1"), say("Thanks. How far do you usually ride?")])
        listed = chat.verify(RIDER_A)
        self.assertIn("EMX Plus (Aqua), frame TESTEMXP0000001", listed.text)
        self.assertIn("Doodle Pro (Nativepop), frame TESTDDLP0000002", listed.text)
        chat.say("2")
        self.assertEqual(chat.conversations.peek("c1").selected_frame, "TESTDDLP0000002")
        self.assertEqual(chat.conversations.peek("c1").agent, battery_support.AGENT_NAME)
        self.assertIn(("recent_trips", RIDER_A), chat.amigo.calls)

    def test_rider_b_never_sees_the_imei_or_vin(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        listed = chat.verify(RIDER_B)
        self.assertIn("T-Rex Smart (Grey), frame number not on record", listed.text)
        chat.say("yes")
        said = " ".join(t.text for t in chat.conversations.transcript("c1")) + repr(chat.llm.requests)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, said)

    def test_amigo_down_still_lets_the_rider_verify(self):
        chat = Chat(amigo=FakeAmigo(down=True))
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING"):
            listed = chat.verify(RIDER_C)
        self.assertEqual(listed.handled_by, "verify_first:verified_no_bikes")


def fresh_api(amigo_dsn=""):
    env = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory", "EMOTORAD_OMS_API_KEY": "",
           "EMOTORAD_AI_MEDIA_BUCKET": "", "EMOTORAD_AMIGO_PG_DSN": amigo_dsn}
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
        import emotorad_ai.api as api
        return importlib.reload(api)


class WiringTests(unittest.TestCase):
    def tearDown(self):
        fresh_api()

    def test_health_says_whether_amigo_is_configured(self):
        self.assertEqual(TestClient(fresh_api().app).get("/health").json()["amigo"], "not configured")
        api = fresh_api("postgresql://ro_chatbot:x@localhost:5433/userbike?sslmode=require")
        self.assertEqual(TestClient(api.app).get("/health").json()["amigo"], "configured")

    def test_with_a_dsn_the_tools_exist(self):
        api = fresh_api("postgresql://ro_chatbot:x@localhost:5433/userbike?sslmode=require")
        self.assertIn(GET_RECENT_TRIPS, api.registry.specs)
        self.assertNotIn(GET_RECENT_TRIPS, fresh_api().registry.specs)


if __name__ == "__main__":
    unittest.main()
```

(`test_amigo_down_still_lets_the_rider_verify`: rider C has no OMS record and Amigo is down, so verification succeeds with no bikes. That is the expected outcome, not a failure.)

- [ ] **Step 2: Run to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_flow -v`
Expected: the flow tests pass or fail depending on Tasks 2 to 4 (they should pass); the two wiring tests fail (`KeyError: 'amigo'`).

- [ ] **Step 3: Wire the API**

In `src/emotorad_ai/api.py`:
- `from .tools import amigo as amigo_tools` with the other `.tools` imports.
- After `OTP_SENDER = MockOtpSender()`:

```python
# Amigo, read-only (tools/amigo.py), when EMOTORAD_AMIGO_PG_DSN is set; the
# config store exports it from the staging secret. None: behaviour as before.
AMIGO = amigo_tools.from_env()
```

- In `_build_registry`, both `build_registry(...)` calls get `amigo=AMIGO,`. The no-OMS branch gets `warranty_source=amigo_tools.merged_source(fixtures.WARRANTY_RECORDS.get, AMIGO) if AMIGO else None,`; the live branch's `warranty_source=live_warranty_source(client),` becomes

```python
        warranty_source=(amigo_tools.merged_source(live_warranty_source(client), AMIGO)
                         if AMIGO else live_warranty_source(client)),
```

- In `/health`, add `"amigo": "configured" if AMIGO is not None else "not configured",`.

In `scripts/chat_local.py`, add to `OPTIONAL`:

```python
    ("EMOTORAD_AMIGO_PG_DSN", "Amigo bikes, service records and trips, read-only"),
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH="src;." python -m unittest tests.test_amigo_flow tests.test_api_verify_first tests.test_e2e_console -v 2>&1 | tail -3`. Expected: OK. Then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/api.py scripts/chat_local.py tests/test_amigo_flow.py
git commit -m "Web chat: Amigo bikes, service status and trips when the DSN is set

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The real SQL, checked against a local Postgres, and the docs

**Files:**
- Create: `scripts/amigo_staging/local_check.sh`
- Create: `tests/test_amigo_local_pg.py`
- Modify: `scripts/amigo_staging/README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: the reader (Task 1), the seed files, the backend repo at `C:\Users\user\emotorad\backend`.

- [ ] **Step 1: Install psycopg locally, with the person's yes**

The reader's real SQL needs the driver. Ask the person before running (a download from PyPI of about 3 MB):

```bash
python -m pip install "psycopg[binary]>=3.1"
```

If the person declines, skip Steps 2 to 4 and say so in the task report; the unit tests do not need it.

- [ ] **Step 2: The check script**

Create `scripts/amigo_staging/local_check.sh` with the Write tool (the heredoc lives in the file, not typed through Bash):

```bash
#!/usr/bin/env bash
# The reader's real SQL against a throwaway local Postgres holding the Amigo
# tables as the amigo-stage-deployment migrations create them, seeded with
# the three test riders. Nothing here reaches AWS. Needs Postgres binaries
# (PG_BIN) and the backend repo ($1).
set -u
BACKEND="${1:-C:/Users/user/emotorad/backend}"
PG_BIN="${PG_BIN:-/c/Program Files/PostgreSQL/17/bin}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
BRANCH=origin/amigo-stage-deployment
export PGHOST=localhost PGPORT=55432

cleanup() { "$PG_BIN/pg_ctl" -D "$WORK/data" -m fast stop >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT

"$PG_BIN/initdb" -D "$WORK/data" -U postgres --auth=trust -E UTF8 >/dev/null || exit 1
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-p 55432" -l "$WORK/pg.log" -w start >/dev/null || exit 1
q() { "$PG_BIN/psql" -X -q -v ON_ERROR_STOP=1 -U "$1" -d "$2" "${@:3}"; }

for db in userbike garage ride; do q postgres postgres -c "CREATE DATABASE $db" || exit 1; done
git -C "$BACKEND" show "$BRANCH:server/userbike-server/db/migration/000001_schema.up.sql" > "$WORK/userbike.sql"
git -C "$BACKEND" show "$BRANCH:server/garage-server/db/migration/000001_schema.up.sql" > "$WORK/garage.sql"
git -C "$BACKEND" show "$BRANCH:server/trip-server/db/migration/000001_schema.up.sql" > "$WORK/ride1.sql"
git -C "$BACKEND" show "$BRANCH:server/trip-server/db/migration/000002_add_merge_columns.up.sql" > "$WORK/ride2.sql"
q postgres userbike -f "$WORK/userbike.sql" >/dev/null || exit 1
q postgres garage -f "$WORK/garage.sql" >/dev/null || exit 1
q postgres ride -f "$WORK/ride1.sql" >/dev/null 2>&1 && q postgres ride -f "$WORK/ride2.sql" >/dev/null 2>&1 || exit 1

q postgres postgres -c "CREATE ROLE ro_chatbot LOGIN CONNECTION LIMIT 10" \
  -c "ALTER ROLE ro_chatbot SET default_transaction_read_only = on" -c "ALTER ROLE ro_chatbot SET statement_timeout = '5s'"
for db in userbike garage ride; do q postgres "$db" -f "$HERE/$db.sql" >/dev/null || exit 1; done

PYTHONPATH="$ROOT/src" python - <<'PY'
from emotorad_ai.tools.amigo import AmigoReader, describe_service, describe_trips
r = AmigoReader("postgresql://ro_chatbot@localhost:55432/userbike")
a = r.bikes("+919700000031"); b = r.bikes("9700000032"); c = "+919700000033"
print("A bikes:", [x["model"] for x in a["bikes"]])
print("B frame:", b["bikes"][0]["framenumber"], "imei:", b["bikes"][0]["imei"])
print("C service:", describe_service(r.service_status(c))["stages"])
print("C trips:", [t["distance_km"] for t in describe_trips(r.recent_trips(c), r.bikes(c))["trips"]])
PY
```

- [ ] **Step 3: The optional test**

Create `tests/test_amigo_local_pg.py`:

```python
"""Runs scripts/amigo_staging/local_check.sh when EMOTORAD_TEST_LOCAL_PG=1:
the reader's real SQL against a local Postgres with the Amigo tables."""

import os
import pathlib
import subprocess
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "amigo_staging" / "local_check.sh"


@unittest.skipUnless(os.environ.get("EMOTORAD_TEST_LOCAL_PG") == "1", "needs a local Postgres and the backend repo")
class LocalPostgresTests(unittest.TestCase):
    def test_the_reader_against_the_real_tables(self):
        out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=300).stdout
        self.assertIn("A bikes: ['EMXPLUS', 'DOODLEPRO']", out)
        self.assertIn("B frame: 860000000000032 imei: 860000000000032", out)
        self.assertIn("{'stage': '1000 km / 6 months', 'status': 'due'}", out)
        self.assertIn("C trips: [12.6, 11.9, 12.4]", out)
```

- [ ] **Step 4: Run it**

Run: `EMOTORAD_TEST_LOCAL_PG=1 PYTHONPATH="src;." python -m unittest tests.test_amigo_local_pg -v`
Expected: OK. Fix the reader's SQL, never the test, if it fails.

- [ ] **Step 5: Docs**

`CLAUDE.md`, after the "Verify first" bullet:

```markdown
- **Amigo, read-only** (2026-09-30): with `EMOTORAD_AMIGO_PG_DSN` set (the staging secret; the old `userbike` database on `amigo-stage-db`), `tools/amigo.py` reads a verified rider's app bikes, service status and last five trips through the `ro_chatbot` role, by phone only, and `merged_source` merges the bikes with the OMS's (one entry per frame number; warranty from the OMS only; an app-only bike is "warranty not on record"). A bike registered by IMEI has no frame number on record: it is chosen by `bike_ref` and never shown by IMEI or VIN. `get_service_status` and `get_recent_trips` are the battery and motor agents'. Test riders and cleanup: `scripts/amigo_staging/`; the real SQL is checked by `local_check.sh`.
```

`scripts/amigo_staging/README.md`, a new section "Chat with the test riders from a laptop": the Session Manager plugin, the port forward (`aws ssm start-session --profile emotorad-staging --target i-02e7dc2874e0fdacb --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters "host=amigo-stage-db.c94k446ourh7.ap-south-1.rds.amazonaws.com,portNumber=5432,localPortNumber=5433"`), `$env:EMOTORAD_AMIGO_PG_DSN` with `localhost:5433` and the `ro_chatbot` password, `python scripts/chat_local.py`, `/health` showing `"amigo": "configured"`, and the three riders' numbers.

- [ ] **Step 6: Full suite and commit**

Run the full suite. Expected: only the three pre-existing failures.

```bash
git add scripts/amigo_staging tests/test_amigo_local_pg.py CLAUDE.md
git commit -m "Amigo: the real SQL checked against a local Postgres; docs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
