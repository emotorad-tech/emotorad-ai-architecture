# Conversation Origin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record, once per conversation run, roughly where it comes from (country, state, city) from the customer's IP or verified phone, permanently and erasable, for reporting.

**Architecture:** A real-client-IP helper reads nginx's `X-Real-IP` only from a trusted proxy (and fixes the shared rate limits). An `origin` module turns an IP (through a local DB-IP Lite file read with `maxminddb`) or a phone (calling code) into a `Place`. The API passes the IP's place in `entry_metadata["origin"]`; the runtime keeps the first one per run, fills an unknown country from a phone proven later, and writes one record per run to a new permanent `conversation_origins` collection that the erasure path deletes.

**Tech Stack:** Python 3.12, FastAPI, MongoDB (pymongo, mongomock in tests), `maxminddb`, unittest.

**Spec:** `docs/superpowers/specs/2026-10-01-conversation-origin-design.md`

## Global Constraints

- The raw IP is never stored, logged, traced or put in `entry_metadata`; only the derived place is.
- Nothing in this feature can stop or delay a reply: every failure gives `unknown` or a logged event, never an exception out of the turn.
- One record per conversation run, `_id` = `summary_key(conversation_id, started_at)`.
- `conversation_origins` is permanent: no TTL index. It is erased by `delete_person` and `delete_conversation`.
- Trusted proxies: `EMOTORAD_TRUSTED_PROXIES`, default `127.0.0.1,::1,172.17.0.1`.
- Database file: `EMOTORAD_GEO_DB`, default `/app/geo/dbip-city-lite.mmdb`; from `https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz`.
- Credit wherever results are shown: `IP Geolocation by DB-IP (https://db-ip.com)`.
- Phone calling codes: `+91` is `IN`, `+34` is `ES`; anything else gives no place.
- Log event names: `origin_lookup_failed`, `origin_db_unavailable`, `origin_record_failed`. Errors carry the exception class only.
- British English, no em dashes, in new comments, docs and output.
- Test command (whole suite): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;." python -m unittest discover -s tests -t .` Known on this Windows machine only: 3 failures (`test_video` x2, `test_start`), green on Linux CI.

## Review Focus

1. A request from an untrusted peer carrying `X-Real-IP`: the header must be ignored, so a client cannot choose its own location or rate-limit bucket.
2. IPv6 and IPv4-mapped IPv6 (`::ffff:49.36.1.1`) addresses: looked up as their IPv4 form; private and loopback ones give no place.
3. A turn that loses the save race (`Runtime` merge of a clashing save): the run's `origin` must carry over to the state that is kept, or the next turn records the run again from a later message.
4. A different person verifying on an expired conversation (`restart_for`): the new run records its own origin and never inherits the first person's `user_key`.
5. A first message that is a photo only, or a quick-reply chip with empty text: the origin is still recorded from it.

---

### Task 1: The real client IP, and per-customer rate limits

**Files:**
- Create: `src/emotorad_ai/client_ip.py`
- Modify: `src/emotorad_ai/api.py` (imports; `post_upload` and `post_message` limiter lines)
- Test: `tests/test_client_ip.py`

**Interfaces:**
- Produces: `client_ip(request, trusted: frozenset) -> Optional[str]`; `trusted_from_env(environ=None) -> frozenset`; `TRUSTED_PROXIES_ENV = "EMOTORAD_TRUSTED_PROXIES"`; in `api.py`, module constant `TRUSTED_PROXIES`.

- [ ] **Step 1: Write the failing tests**

```python
"""The customer's own address behind nginx. nginx sets X-Real-IP; the app
sees every request from Docker's bridge gateway, so without this every
customer shared one rate limit (checked on staging, 1 October 2026)."""

import unittest
from types import SimpleNamespace

from starlette.datastructures import Headers

from emotorad_ai.client_ip import client_ip, trusted_from_env
from emotorad_ai.ratelimit import RateLimiter

TRUSTED = frozenset({"127.0.0.1", "::1", "172.17.0.1"})


def request(peer, real_ip=None):
    headers = Headers({"x-real-ip": real_ip} if real_ip else {})
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None, headers=headers)


class ClientIpTests(unittest.TestCase):
    def test_nginx_header_is_used_from_a_trusted_peer(self):
        self.assertEqual(client_ip(request("172.17.0.1", "49.36.1.1"), TRUSTED), "49.36.1.1")

    def test_the_header_is_ignored_from_anyone_else(self):
        self.assertEqual(client_ip(request("203.0.113.9", "49.36.1.1"), TRUSTED), "203.0.113.9")

    def test_a_malformed_header_falls_back_to_the_peer(self):
        self.assertEqual(client_ip(request("172.17.0.1", "not-an-ip"), TRUSTED), "172.17.0.1")

    def test_no_peer_and_no_header_is_none(self):
        self.assertIsNone(client_ip(request(None), TRUSTED))

    def test_the_default_trusts_loopback_and_the_docker_gateway(self):
        self.assertEqual(trusted_from_env({}), TRUSTED)
        self.assertEqual(trusted_from_env({"EMOTORAD_TRUSTED_PROXIES": "10.0.0.2, 10.0.0.3"}),
                         frozenset({"10.0.0.2", "10.0.0.3"}))

    def test_two_customers_behind_the_proxy_get_their_own_limit(self):
        limiter = RateLimiter(limit=1, window_seconds=60.0)
        self.assertTrue(limiter.allow(client_ip(request("172.17.0.1", "49.36.1.1"), TRUSTED)))
        self.assertTrue(limiter.allow(client_ip(request("172.17.0.1", "49.36.1.2"), TRUSTED)))
        self.assertFalse(limiter.allow(client_ip(request("172.17.0.1", "49.36.1.1"), TRUSTED)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_client_ip`
Expected: ERROR, `ModuleNotFoundError: No module named 'emotorad_ai.client_ip'`

- [ ] **Step 3: Write the helper**

```python
"""The customer's own IP address, behind nginx.

nginx sets `X-Real-IP $remote_addr`, overwriting anything the client sent.
The container sees every request from Docker's bridge gateway, so the header
is the only place the customer's address is. It is trusted only when the
request comes straight from a trusted proxy; from anyone else it is ignored,
so a client cannot choose its own address (its rate-limit bucket, or the
place recorded for its conversation).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Mapping, Optional

TRUSTED_PROXIES_ENV = "EMOTORAD_TRUSTED_PROXIES"
DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1,172.17.0.1"


def trusted_from_env(environ: Optional[Mapping[str, str]] = None) -> frozenset:
    env = environ if environ is not None else os.environ
    raw = env.get(TRUSTED_PROXIES_ENV) or DEFAULT_TRUSTED_PROXIES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def client_ip(request: Any, trusted: frozenset) -> Optional[str]:
    peer = request.client.host if request.client else None
    if peer in trusted:
        forwarded = (request.headers.get("x-real-ip") or "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_client_ip`
Expected: `Ran 6 tests ... OK`

- [ ] **Step 5: Use it for both rate limits**

In `src/emotorad_ai/api.py`, add beside the other local imports:

```python
from .client_ip import client_ip, trusted_from_env
```

after `upload_limiter = RateLimiter(limit=20, window_seconds=60.0)`:

```python
# The proxies whose X-Real-IP is believed (client_ip.py). Without this every
# customer behind nginx shared one limit.
TRUSTED_PROXIES = trusted_from_env()
```

and in `post_upload` and `post_message` replace `request.client.host if request.client else None` with `client_ip(request, TRUSTED_PROXIES)`.

- [ ] **Step 6: Run the whole suite**

Run: the Global Constraints test command.
Expected: only the 3 known Windows-only failures.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/client_ip.py src/emotorad_ai/api.py tests/test_client_ip.py
git commit -m "The customer's own IP behind nginx; rate limits per customer"
```

---

### Task 2: The origin module (IP and phone to a place)

**Files:**
- Create: `src/emotorad_ai/origin.py`
- Modify: `requirements.txt` (add `maxminddb>=2.6`)
- Test: `tests/test_origin.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `Place(country, region, city, source, db)` with `as_dict() -> Dict[str, Optional[str]]`; `UNKNOWN`; `place_from_dict(Optional[Mapping]) -> Optional[Place]`; `from_phone(Optional[str]) -> Optional[Place]`; `choose(*Optional[Place]) -> Place`; `IpLocator(reader, db)` with `.db: str` and `.place(ip: Optional[str]) -> Optional[Place]`; `ip_locator_from_env(environ=None) -> Optional[IpLocator]`; `GEO_DB_ENV = "EMOTORAD_GEO_DB"`; `DEFAULT_GEO_DB = "/app/geo/dbip-city-lite.mmdb"`.

- [ ] **Step 1: Write the failing tests**

```python
"""IP and phone to a place, for reporting where conversations come from."""

import logging
import os
import unittest

from emotorad_ai.origin import (UNKNOWN, IpLocator, Place, choose, from_phone, ip_locator_from_env,
                                place_from_dict)

PUNE = {"country": {"iso_code": "IN", "names": {"en": "India"}},
        "subdivisions": [{"names": {"en": "Maharashtra"}}], "city": {"names": {"en": "Pune"}}}


class FakeReader:
    def __init__(self, records=None, fail=None):
        self.records, self.fail, self.asked = records or {}, fail, []

    def get(self, ip):
        self.asked.append(ip)
        if self.fail:
            raise self.fail
        return self.records.get(ip)


class IpTests(unittest.TestCase):
    def locator(self, **kw):
        return IpLocator(FakeReader(**kw), "dbip-city-lite-2026-10")

    def test_a_dbip_record_becomes_a_place(self):
        place = self.locator(records={"49.36.1.1": PUNE}).place("49.36.1.1")
        self.assertEqual(place, Place("IN", "Maharashtra", "Pune", "ip", "dbip-city-lite-2026-10"))

    def test_an_ipv4_mapped_ipv6_address_is_looked_up_as_ipv4(self):
        locator = self.locator(records={"49.36.1.1": PUNE})
        self.assertEqual(locator.place("::ffff:49.36.1.1").city, "Pune")

    def test_private_loopback_and_junk_are_not_looked_up(self):
        locator = self.locator(records={"49.36.1.1": PUNE})
        for ip in ("10.1.2.3", "127.0.0.1", "172.17.0.1", "::1", "testclient", "", None):
            self.assertIsNone(locator.place(ip))
        self.assertEqual(locator._reader.asked, [])

    def test_an_address_not_in_the_file_is_none(self):
        self.assertIsNone(self.locator().place("49.36.1.1"))

    def test_a_record_with_only_a_country(self):
        place = self.locator(records={"49.36.1.1": {"country": {"iso_code": "ES"}}}).place("49.36.1.1")
        self.assertEqual((place.country, place.region, place.city), ("ES", None, None))

    def test_a_read_error_is_logged_by_class_and_gives_none(self):
        locator = self.locator(fail=OSError("49.36.1.1 broke"))
        with self.assertLogs("emotorad_ai.origin", level="WARNING") as logs:
            self.assertIsNone(locator.place("49.36.1.1"))
        self.assertIn("origin_lookup_failed (OSError)", logs.output[0])
        self.assertNotIn("49.36.1.1", " ".join(logs.output))


class PhoneTests(unittest.TestCase):
    def test_india_and_spain_by_calling_code(self):
        self.assertEqual(from_phone("+919700000031"), Place("IN", None, None, "phone", None))
        self.assertEqual(from_phone("+34612345678").country, "ES")

    def test_another_code_or_no_phone_is_none(self):
        for phone in ("+447700900123", "9700000031", "", None):
            self.assertIsNone(from_phone(phone))


class ChooseTests(unittest.TestCase):
    def test_the_first_resolved_place_wins(self):
        ip = Place("IN", "Maharashtra", "Pune", "ip", "db")
        self.assertEqual(choose(None, ip, from_phone("+34612345678")), ip)

    def test_nothing_resolved_is_unknown(self):
        self.assertEqual(choose(None, None), UNKNOWN)
        self.assertEqual(UNKNOWN.as_dict(),
                         {"country": "unknown", "region": None, "city": None, "source": "none", "db": None})

    def test_a_place_survives_the_round_trip_through_entry_metadata(self):
        place = Place("IN", "Maharashtra", "Pune", "ip", "db")
        self.assertEqual(place_from_dict(place.as_dict()), place)
        self.assertIsNone(place_from_dict(None))
        self.assertIsNone(place_from_dict({"city": "Pune"}))


class FileTests(unittest.TestCase):
    def test_no_file_means_no_locator(self):
        self.assertIsNone(ip_locator_from_env({"EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"}))

    def test_an_unreadable_file_is_logged_once_and_gives_no_locator(self):
        path = os.path.join(os.path.dirname(__file__), "test_origin.py")  # exists, not an mmdb
        with self.assertLogs("emotorad_ai.origin", level="WARNING") as logs:
            self.assertIsNone(ip_locator_from_env({"EMOTORAD_GEO_DB": path}))
        self.assertIn("origin_db_unavailable", logs.output[0])


@unittest.skipUnless(os.environ.get("EMOTORAD_TEST_GEO_DB"), "needs a real DB-IP .mmdb file")
class RealFileTests(unittest.TestCase):
    def test_a_known_public_address_resolves(self):
        locator = ip_locator_from_env({"EMOTORAD_GEO_DB": os.environ["EMOTORAD_TEST_GEO_DB"]})
        self.assertTrue(locator.db.startswith("dbip-city-lite-"))
        self.assertEqual(locator.place("8.8.8.8").country, "US")


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_origin`
Expected: ERROR, `ModuleNotFoundError: No module named 'emotorad_ai.origin'`

- [ ] **Step 3: Add the dependency and install it**

Append to `requirements.txt`:

```
# IP address to country, state and city, from a local DB-IP Lite file
# (origin.py). Reads MaxMind-format .mmdb files. Apache 2.0.
maxminddb>=2.6
```

Run: `python -m pip install --user "maxminddb>=2.6"`

- [ ] **Step 4: Write the module**

```python
"""Where a conversation comes from, for reporting (spec 2026-10-01).

A place is the country, and where known the state and city, from the first
signal that resolves: the customer's IP address through a local DB-IP Lite
file, then the verified phone's calling code. The raw IP is never kept: it
goes in, a place comes out. Credit wherever results are shown: "IP
Geolocation by DB-IP (https://db-ip.com)", CC BY 4.0.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

_logger = logging.getLogger(__name__)

GEO_DB_ENV = "EMOTORAD_GEO_DB"
DEFAULT_GEO_DB = "/app/geo/dbip-city-lite.mmdb"
CREDIT = "IP Geolocation by DB-IP (https://db-ip.com)"

# Our markets. A number from anywhere else gives no place rather than a guess.
_CALLING_CODES = (("91", "IN"), ("34", "ES"))


@dataclass(frozen=True)
class Place:
    country: str
    region: Optional[str]
    city: Optional[str]
    source: str
    db: Optional[str]

    def as_dict(self) -> Dict[str, Optional[str]]:
        return asdict(self)


UNKNOWN = Place("unknown", None, None, "none", None)


def place_from_dict(raw: Optional[Mapping[str, Any]]) -> Optional[Place]:
    if not raw or not raw.get("country") or not raw.get("source"):
        return None
    return Place(raw["country"], raw.get("region"), raw.get("city"), raw["source"], raw.get("db"))


def from_phone(phone: Optional[str]) -> Optional[Place]:
    number = (phone or "").strip()
    if not number.startswith("+"):
        return None
    for code, country in _CALLING_CODES:
        if number[1:].startswith(code):
            return Place(country, None, None, "phone", None)
    return None


def choose(*places: Optional[Place]) -> Place:
    return next((place for place in places if place is not None), UNKNOWN)


class IpLocator:
    """A DB-IP Lite file. `reader` is a maxminddb reader, or a fake in tests."""

    def __init__(self, reader: Any, db: str) -> None:
        self._reader = reader
        self.db = db

    def place(self, ip: Optional[str]) -> Optional[Place]:
        try:
            address = ipaddress.ip_address((ip or "").strip())
        except ValueError:
            return None
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            return None
        try:
            record = self._reader.get(str(address))
        except Exception as exc:
            # The class only: never the address.
            _logger.warning("origin_lookup_failed (%s)", type(exc).__name__)
            return None
        country = ((record or {}).get("country") or {}).get("iso_code")
        if not country:
            return None
        subdivisions = record.get("subdivisions") or [{}]
        region = (subdivisions[0].get("names") or {}).get("en")
        city = ((record.get("city") or {}).get("names") or {}).get("en")
        return Place(country, region, city, "ip", self.db)


def ip_locator_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[IpLocator]:
    """The file the image was built with, or None: then every IP lookup gives
    nothing and the phone, or unknown, stands."""
    env = environ if environ is not None else os.environ
    path = env.get(GEO_DB_ENV) or DEFAULT_GEO_DB
    if not os.path.exists(path):
        return None
    try:
        import maxminddb  # here, so a test run without the file needs no driver

        reader = maxminddb.open_database(path)
        built = datetime.fromtimestamp(reader.metadata().build_epoch, tz=timezone.utc)
    except Exception as exc:
        _logger.warning("origin_db_unavailable (%s)", type(exc).__name__)
        return None
    return IpLocator(reader, "dbip-city-lite-%s" % built.strftime("%Y-%m"))
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_origin`
Expected: `Ran 16 tests ... OK (skipped=1)`

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/origin.py tests/test_origin.py requirements.txt
git commit -m "Origin: IP (local DB-IP file) or phone calling code to a place"
```

---

### Task 3: The permanent, erasable record (stores)

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`InMemoryConversationStore`: `__init__`, new `record_origin`, `origins_of`, `delete_person`, `delete_conversation`)
- Modify: `src/emotorad_ai/stores/mongo.py` (`CONVERSATION_ORIGINS`, `INDEXES`, `record_origin`, `origins_of`, `conversations_of`, `delete_person`, `delete_conversation`)
- Test: `tests/store_contract.py` (both stores run it)

**Interfaces:**
- Produces: on both stores, `record_origin(record: Dict[str, Any]) -> None` (upsert by `record["_id"]`) and `origins_of(conversation_id: str) -> List[Dict[str, Any]]` (sorted by `started_at`); deletion counts gain the key `"conversation_origins"`. Mongo constant `CONVERSATION_ORIGINS = "conversation_origins"`.

- [ ] **Step 1: Write the failing contract tests**

Append to `class StoreContract` in `tests/store_contract.py`:

```python
    def origin(self, cid, started_at="2026-10-01T09:00:00+00:00", user_key=None, **place):
        record = {"_id": "%s#%s" % (cid, started_at), "conversation_id": cid, "started_at": started_at,
                  "channel": "amiigo_app", "country": "IN", "region": "Maharashtra", "city": "Pune",
                  "source": "ip", "db": "dbip-city-lite-2026-10", "user_key": user_key}
        record.update(place)
        return record

    def test_an_origin_is_upserted_per_run(self):
        store = self.make_store()
        store.record_origin(self.origin("c1"))
        store.record_origin(self.origin("c1", user_key="PHONE#+919700000031"))
        store.record_origin(self.origin("c1", started_at="2026-10-03T09:00:00+00:00", city="Mumbai"))
        records = store.origins_of("c1")
        self.assertEqual([r["city"] for r in records], ["Pune", "Mumbai"])
        self.assertEqual(records[0]["user_key"], "PHONE#+919700000031")

    def test_an_erasure_by_person_takes_their_origins(self):
        store = self.make_store()
        state = store.get("mine")
        state.user_key, state.turns = "PHONE#+919700000031", 1
        store.save(state)
        store.record_origin(self.origin("mine", user_key="PHONE#+919700000031"))
        store.record_origin(self.origin("theirs", user_key="PHONE#+919812345678"))
        counts = store.delete_person("PHONE#+919700000031")
        self.assertEqual(counts["conversation_origins"], 1)
        self.assertEqual(store.origins_of("mine"), [])
        self.assertEqual(len(store.origins_of("theirs")), 1)

    def test_an_origin_alone_ties_a_conversation_to_its_person(self):
        # A run that began anonymous and verified: only its origin may carry the key.
        store = self.make_store()
        store.record_origin(self.origin("web-1", user_key="PHONE#+919700000031"))
        self.assertEqual(store.delete_person("PHONE#+919700000031")["conversation_origins"], 1)

    def test_an_erasure_by_conversation_takes_an_anonymous_origin(self):
        store = self.make_store()
        store.record_origin(self.origin("anon"))
        self.assertEqual(store.delete_conversation("anon")["conversation_origins"], 1)
        self.assertEqual(store.origins_of("anon"), [])
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_mongo_store tests.test_memory_store`
Expected: ERROR, `AttributeError: ... has no attribute 'record_origin'`

- [ ] **Step 3: The in-memory store**

In `InMemoryConversationStore.__init__`, after `self._media = {}`:

```python
        # Where each run came from (origin.py), permanent: conversation id -> run key -> record.
        self._origins: Dict[str, Dict[str, Dict[str, Any]]] = {}
```

New methods after `media_of`:

```python
    def record_origin(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (one per run): filling in a run's country or person
        replaces its record."""
        self._origins.setdefault(record["conversation_id"], {})[record["_id"]] = dict(record)

    def origins_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        return sorted(self._origins.get(conversation_id, {}).values(), key=lambda r: r["started_at"])
```

In `delete_person`, add the conversations an origin ties to the person, and the count:

```python
        mine |= {cid for cid, runs in self._origins.items()
                 if any(r.get("user_key") == user_key for r in runs.values())}
        counts = {"conversations": 0, "transcript_turns": 0, "media": 0, "conversation_origins": 0,
                  "conversation_summaries": len(self._summaries.pop(user_key, {}))}
```

In `delete_conversation`'s returned dict add:

```python
            "conversation_origins": len(self._origins.pop(conversation_id, {})),
```

- [ ] **Step 4: The MongoDB store**

In `src/emotorad_ai/stores/mongo.py`, after `MEDIA = "media"`:

```python
# Where each run of a conversation came from (origin.py). Permanent, like the
# transcript, and erased with the person or the conversation.
CONVERSATION_ORIGINS = "conversation_origins"
```

In `INDEXES`, after the `MEDIA` entry:

```python
    CONVERSATION_ORIGINS: [
        ([("conversation_id", 1)], {"name": "conversation"}),
        ([("user_key", 1)], {"name": "user_key"}),
        ([("started_at", 1)], {"name": "started_at"}),
        ([("country", 1), ("region", 1)], {"name": "place"}),
    ],
```

After `media_of`:

```python
    def record_origin(self, record: Dict[str, Any]) -> None:
        """Upsert by `_id` (one per run)."""
        origins = self._collection(CONVERSATION_ORIGINS)
        self._guard("replace_one", lambda: origins.replace_one({"_id": record["_id"]}, record, upsert=True))

    def origins_of(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self._guard(
            "find",
            lambda: list(self._collection(CONVERSATION_ORIGINS).find({"conversation_id": conversation_id})
                         .sort("started_at", 1)),
        )
```

In `conversations_of`, add `(CONVERSATION_ORIGINS, "conversation_id")` to the tuple of collections searched. In `delete_person`, add `CONVERSATION_ORIGINS` to the names the counts start from. In `delete_conversation`'s dict add:

```python
            CONVERSATION_ORIGINS: self._remove(CONVERSATION_ORIGINS, {"conversation_id": conversation_id}, dry_run),
```

- [ ] **Step 5: Run the store tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_mongo_store tests.test_memory_store tests.test_mongo_scripts`
Expected: OK. `scripts/mongo_setup.py` creates the new collection's indexes through `ensure_indexes`, and `scripts/delete_person.py`'s dry run lists the new count, both without changes; `tests/test_mongo_scripts` confirms.

- [ ] **Step 6: Run the whole suite**

Run: the Global Constraints test command. Expected: only the 3 known failures.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/stores/mongo.py tests/store_contract.py
git commit -m "Stores: a permanent origin record per run, erased with the person or conversation"
```

---

### Task 4: Recording once per run (runtime)

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`ConversationState.origin`)
- Modify: `src/emotorad_ai/runtime.py` (`_node_prepare`, new `_note_origin`, the save-race merge)
- Test: `tests/test_origin_runtime.py`

**Interfaces:**
- Consumes: `origin.choose`, `origin.from_phone`, `origin.place_from_dict`, `origin.UNKNOWN` (Task 2); `store.record_origin`, `store.origins_of` (Task 3).
- Produces: `ConversationState.origin: Optional[Dict[str, Any]]` (the place fields plus `user_key`); reads `message.entry_metadata["origin"]` (a `Place.as_dict()`), which Task 5 supplies.

- [ ] **Step 1: Write the failing tests**

```python
"""The origin is recorded once per conversation run, from its first message."""

import itertools
import unittest
from unittest import mock

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.llm import say
from tests.test_amigo_flow import Chat

PUNE = {"country": "IN", "region": "Maharashtra", "city": "Pune", "source": "ip", "db": "dbip-city-lite-2026-10"}
DELHI = dict(PUNE, region="Delhi", city="New Delhi")


class OriginChat(Chat):
    def __init__(self, **kw):
        kw.setdefault("replies", [say("ok")] * 10)  # the agent turns a chat reaches
        super().__init__(**kw)
        ticks = ("2026-10-01T%02d:%02d:00+00:00" % divmod(n, 60) for n in itertools.count())
        self.conversations = InMemoryConversationStore(clock=lambda: next(ticks))
        self.runtime.conversations = self.conversations

    def send(self, text, origin=None, identity=None, channel="website_chat", cid="c1"):
        return self.runtime.handle(InboundMessage(
            conversation_id=cid, persona="customer", channel=channel, message_text=text,
            identity=identity or Identity(strength=ANONYMOUS, em_aid="aid-1"),
            entry_metadata={"origin": origin} if origin else {}))

    def verify_as(self, phone, origin=None):
        self.send("my battery isn't charging", origin)
        self.send(phone[3:])
        self.send(self.store.pending_code("c1"))
        return self.send("yes")


class OriginRuntimeTests(unittest.TestCase):
    def test_the_first_message_sets_the_runs_origin(self):
        chat = OriginChat()
        chat.send("hi", PUNE)
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["country"], record["region"], record["city"], record["source"]),
                         ("IN", "Maharashtra", "Pune", "ip"))
        self.assertEqual((record["channel"], record["user_key"]), ("website_chat", None))
        self.assertEqual(record["_id"], "c1#" + record["started_at"])

    def test_later_messages_do_not_change_it(self):
        chat = OriginChat()
        chat.send("hi", PUNE)
        chat.send("9700000031", DELHI)
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual(record["city"], "Pune")

    def test_an_unknown_origin_takes_the_country_of_a_phone_proven_later(self):
        chat = OriginChat()
        chat.verify_as("+919700000033")
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["country"], record["source"], record["city"]), ("IN", "phone", None))
        self.assertEqual(record["user_key"], "PHONE#+919700000033")

    def test_a_known_origin_takes_the_person_but_keeps_its_place(self):
        chat = OriginChat()
        chat.verify_as("+919700000033", origin=PUNE)
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["city"], record["source"]), ("Pune", "ip"))
        self.assertEqual(record["user_key"], "PHONE#+919700000033")

    def test_an_app_message_with_no_usable_ip_records_the_phones_country(self):
        chat = OriginChat()
        chat.send("my battery isn't charging", channel="amiigo_app",
                  identity=Identity(strength=VERIFIED, phone="+919700000031", em_aid="aid-1"))
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["country"], record["source"], record["channel"]), ("IN", "phone", "amiigo_app"))
        self.assertEqual(record["user_key"], "PHONE#+919700000031")

    def test_a_new_run_records_its_own(self):
        chat = OriginChat()
        chat.send("hi", PUNE)
        chat.conversations._states.pop("c1")  # the working state expired
        chat.send("hello again", DELHI)
        self.assertEqual([r["city"] for r in chat.conversations.origins_of("c1")], ["Pune", "New Delhi"])

    def test_a_photo_only_first_message_still_records(self):
        chat = OriginChat()
        chat.send("", PUNE)
        self.assertEqual(len(chat.conversations.origins_of("c1")), 1)

    def test_a_store_that_cannot_record_does_not_stop_the_reply(self):
        chat = OriginChat()
        with mock.patch.object(chat.conversations, "record_origin", side_effect=StoreUnavailable("down")):
            reply = chat.send("hi", PUNE)
        self.assertTrue(reply.text)
        (event,) = [e for e in chat.runtime.log.events if e["event"] == "origin_record_failed"]
        self.assertEqual(event["error"], "StoreUnavailable")
        chat.send("still there?", DELHI)  # the next turn tries again
        self.assertEqual(len(chat.conversations.origins_of("c1")), 1)
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_origin_runtime`
Expected: FAIL, `origins_of("c1")` is `[]` (nothing records yet).

- [ ] **Step 3: The state field**

In `ConversationState`, after `started_at: Optional[str] = None`:

```python
    # Where this run came from (origin.py): the place fields and the person
    # once known. Set from the run's first message; only filled in after.
    origin: Optional[Dict[str, Any]] = None
```

- [ ] **Step 4: Record in the prepare step**

In `src/emotorad_ai/runtime.py`, import beside the other local imports:

```python
from . import origin as origin_place
```

In `_node_prepare`, directly after the `self.log.identity_resolved(...)` call, add:

```python
        self._note_origin(message, state, resolved)
```

New method on `Runtime`, after `_node_prepare`:

```python
    def _note_origin(self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity) -> None:
        """Where this run comes from, for reporting (spec 2026-10-01): set from
        its first message, then only filled in. An unknown country takes the
        country of a phone proven later; the record takes the person once
        known, so an erasure by phone finds a run that began anonymous. Never
        stops the turn: a failed write is logged and tried again next turn."""
        phone = resolved.identity.phone if resolved.identity.may_disclose else None
        current = state.origin
        if current is None:
            place = origin_place.choose(origin_place.place_from_dict(message.entry_metadata.get("origin")),
                                        origin_place.from_phone(phone))
            updated = dict(place.as_dict(), user_key=state.user_key)
        else:
            updated = dict(current)
            if current["country"] == origin_place.UNKNOWN.country:
                by_phone = origin_place.from_phone(phone)
                if by_phone is not None:
                    updated.update(by_phone.as_dict())
            if state.user_key and not current.get("user_key"):
                updated["user_key"] = state.user_key
            if updated == current:
                return
        record = dict(updated, _id=summary_key(state.conversation_id, state.started_at),
                      conversation_id=state.conversation_id, started_at=state.started_at, channel=message.channel)
        try:
            self.conversations.record_origin(record)
        except Exception as exc:
            self.log.emit("origin_record_failed", message.conversation_id, error=type(exc).__name__)
            return
        state.origin = updated
```

- [ ] **Step 5: Carry it through a lost save race**

In the merge of a clashing save (the block that sets `fresh.evidence_seen = fresh.evidence_seen or ours.evidence_seen`), add:

```python
        fresh.origin = fresh.origin or ours.origin
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_origin_runtime`
Expected: `Ran 8 tests ... OK`

- [ ] **Step 7: Run the whole suite**

Run: the Global Constraints test command. Expected: only the 3 known failures. A test that pins a whole state or an event list and now sees `origin` is updated to expect it, and the change is named in the commit message.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/runtime.py tests/test_origin_runtime.py
git commit -m "Runtime: the run's origin from its first message, filled in by a proven phone"
```

---

### Task 5: The API passes the place, and /health shows the file

**Files:**
- Modify: `src/emotorad_ai/api.py` (import, `IP_LOCATOR`, `post_message` metadata, `health`)
- Modify: `tests/test_api_health.py` (pinned dict gains `ip_location`)
- Test: `tests/test_api_origin.py`

**Interfaces:**
- Consumes: `client_ip`, `TRUSTED_PROXIES` (Task 1); `origin.ip_locator_from_env`, `IpLocator.place`, `Place.as_dict` (Task 2); runtime reads `entry_metadata["origin"]` (Task 4).
- Produces: `api.IP_LOCATOR: Optional[IpLocator]`; `/health["ip_location"]`.

- [ ] **Step 1: Write the failing tests**

```python
"""The API turns the customer's IP into a place and passes only the place."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.contract import Reply
from emotorad_ai.origin import Place
from tests.test_api_health import fresh_api


class FakeLocator:
    db = "dbip-city-lite-2026-10"

    def __init__(self):
        self.seen = []

    def place(self, ip):
        self.seen.append(ip)
        return Place("IN", "Maharashtra", "Pune", "ip", self.db)


class ApiOriginTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.client = TestClient(self.api.app)

    def post(self, locator, headers):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api, "IP_LOCATOR", locator), \
                mock.patch.object(self.api, "TRUSTED_PROXIES", frozenset({"testclient"})), \
                mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                                   "text": "hi"}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return seen[-1]

    def test_the_place_goes_in_and_the_ip_does_not(self):
        locator = FakeLocator()
        message = self.post(locator, {"X-Real-IP": "49.36.1.1"})
        self.assertEqual(message.entry_metadata["origin"]["city"], "Pune")
        self.assertEqual(locator.seen, ["49.36.1.1"])
        self.assertNotIn("49.36.1.1", repr(message.to_dict()))

    def test_without_a_file_no_origin_is_passed(self):
        message = self.post(None, {"X-Real-IP": "49.36.1.1"})
        self.assertNotIn("origin", message.entry_metadata)

    def test_health_names_the_file_in_use(self):
        with mock.patch("emotorad_ai.origin.ip_locator_from_env", return_value=FakeLocator()):
            api = fresh_api({"EMOTORAD_AI_MODE": "offline"})
        self.assertEqual(api.health()["ip_location"], "dbip-city-lite-2026-10")
        self.assertEqual(self.api.health()["ip_location"], "not configured")
```

In `tests/test_api_health.py`'s `test_offline_reports_no_secret`, add `"EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"` to the environment and `"ip_location": "not configured"` to the expected dict.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_origin tests.test_api_health`
Expected: FAIL / ERROR: no `IP_LOCATOR` attribute, no `ip_location` key.

- [ ] **Step 3: Wire it**

In `src/emotorad_ai/api.py`, import beside the other local imports:

```python
from . import origin as origin_place
```

After `TRUSTED_PROXIES = trusted_from_env()`:

```python
# The DB-IP file the image was built with (origin.py), or None. Each message's
# IP becomes a place here; the IP itself goes no further than this module.
IP_LOCATOR = origin_place.ip_locator_from_env()
```

In `post_message`, replace the line that builds `extra` with:

```python
    extra = {key: value for key, value in (("cluster_id", message_cluster), ("pinned_agent", body.agent)) if value}
    # Where the customer is, as a place: the runtime keeps the run's first one.
    place = IP_LOCATOR.place(client_ip(request, TRUSTED_PROXIES)) if IP_LOCATOR is not None else None
    if place is not None:
        extra["origin"] = place.as_dict()
```

In `health()`, after the `"build"` entry:

```python
        "ip_location": IP_LOCATOR.db if IP_LOCATOR is not None else "not configured",
```

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_origin tests.test_api_health`
Expected: OK.

- [ ] **Step 5: Run the whole suite**

Run: the Global Constraints test command. Expected: only the 3 known failures.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/api.py tests/test_api_origin.py tests/test_api_health.py
git commit -m "API: the customer's place goes to the runtime, never their IP; /health names the file"
```

---

### Task 6: The file in the image, the report, and the docs

**Files:**
- Create: `docker/fetch_geo_db.py`
- Modify: `Dockerfile`
- Create: `scripts/origin_report.py`
- Modify: `CLAUDE.md` (one bullet)
- Test: `tests/test_fetch_geo_db.py`, `tests/test_origin_report.py`

**Interfaces:**
- Consumes: `CONVERSATION_ORIGINS`, `connect` (stores/mongo.py); `origin.CREDIT`.
- Produces: `fetch_geo_db.fetch(dest_dir: str, today: date, opener=urllib.request.urlopen) -> Optional[str]` (the month fetched); `origin_report.counts(collection, by: str, start: str, end: str, channel: Optional[str] = None) -> List[Tuple[str, int]]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_fetch_geo_db.py`:

```python
import gzip
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "docker"))
import fetch_geo_db  # noqa: E402


class FetchTests(unittest.TestCase):
    def opener(self, available):
        asked = []

        def open_(url, timeout):
            asked.append(url)
            for month, body in available.items():
                if url.endswith("dbip-city-lite-%s.mmdb.gz" % month):
                    return io.BytesIO(gzip.compress(body))
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        return open_, asked

    def test_the_current_month_is_fetched_and_unpacked(self):
        open_, asked = self.opener({"2026-10": b"MMDB-OCT"})
        with tempfile.TemporaryDirectory() as dest:
            self.assertEqual(fetch_geo_db.fetch(dest, date(2026, 10, 15), open_), "2026-10")
            with open(os.path.join(dest, "dbip-city-lite.mmdb"), "rb") as f:
                self.assertEqual(f.read(), b"MMDB-OCT")
        self.assertEqual(asked, ["https://download.db-ip.com/free/dbip-city-lite-2026-10.mmdb.gz"])

    def test_early_in_the_month_it_falls_back_to_last_months(self):
        open_, _ = self.opener({"2026-09": b"MMDB-SEP"})
        with tempfile.TemporaryDirectory() as dest:
            self.assertEqual(fetch_geo_db.fetch(dest, date(2026, 10, 1), open_), "2026-09")

    def test_january_falls_back_to_december(self):
        open_, asked = self.opener({})
        with tempfile.TemporaryDirectory() as dest:
            self.assertIsNone(fetch_geo_db.fetch(dest, date(2027, 1, 1), open_))
            self.assertFalse(os.path.exists(os.path.join(dest, "dbip-city-lite.mmdb")))
        self.assertTrue(asked[1].endswith("dbip-city-lite-2026-12.mmdb.gz"))
```

`tests/test_origin_report.py`:

```python
import os
import sys
import unittest

import mongomock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import origin_report  # noqa: E402


def run(cid, started_at, country="IN", region="Maharashtra", city="Pune", channel="amiigo_app"):
    return {"_id": "%s#%s" % (cid, started_at), "conversation_id": cid, "started_at": started_at,
            "channel": channel, "country": country, "region": region, "city": city}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.origins = mongomock.MongoClient()["emotorad_ai"]["conversation_origins"]
        self.origins.insert_many([
            run("a", "2026-10-01T09:00:00+00:00"),
            run("b", "2026-10-02T09:00:00+00:00"),
            run("c", "2026-10-02T10:00:00+00:00", region="Delhi", city="New Delhi"),
            run("d", "2026-10-02T11:00:00+00:00", country="ES", region="Madrid", city="Madrid", channel="website_chat"),
            run("e", "2026-11-01T09:00:00+00:00"),
        ])

    def test_by_region_within_the_dates(self):
        rows = origin_report.counts(self.origins, "region", "2026-10-01", "2026-10-31")
        self.assertEqual(rows, [("IN / Maharashtra", 2), ("ES / Madrid", 1), ("IN / Delhi", 1)])

    def test_by_country_for_one_channel(self):
        rows = origin_report.counts(self.origins, "country", "2026-10-01", "2026-10-31", channel="amiigo_app")
        self.assertEqual(rows, [("IN", 3)])
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_fetch_geo_db tests.test_origin_report`
Expected: ERROR, `ModuleNotFoundError` for both.

- [ ] **Step 3: The fetch script**

`docker/fetch_geo_db.py`:

```python
"""Fetch this month's DB-IP Lite city file into the image (origin.py).

Run at build time. The new file appears early in the month, so last month's
is the fallback. It never fails the build: without the file, /health says
"ip_location": "not configured" and conversations record the phone's country
or "unknown". DB-IP Lite is CC BY 4.0: "IP Geolocation by DB-IP".
"""

import gzip
import os
import shutil
import sys
import urllib.request
from datetime import date
from typing import Any, Callable, List, Optional

URL = "https://download.db-ip.com/free/dbip-city-lite-%s.mmdb.gz"
NAME = "dbip-city-lite.mmdb"


def months(today: date) -> List[str]:
    previous = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
    return [today.strftime("%Y-%m"), previous.strftime("%Y-%m")]


def fetch(dest_dir: str, today: date, opener: Callable[..., Any] = urllib.request.urlopen) -> Optional[str]:
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, NAME)
    for month in months(today):
        partial = target + ".part"
        try:
            with opener(URL % month, timeout=300) as response, gzip.GzipFile(fileobj=response) as unpacked, \
                    open(partial, "wb") as out:
                shutil.copyfileobj(unpacked, out)
            os.replace(partial, target)
            print("geo db: dbip-city-lite-%s" % month)
            return month
        except Exception as exc:
            print("geo db: %s not fetched (%s)" % (month, type(exc).__name__))
            if os.path.exists(partial):
                os.remove(partial)
    return None


if __name__ == "__main__":
    fetch(sys.argv[1] if len(sys.argv) > 1 else "/app/geo", date.today())
```

In `Dockerfile`, after the `RUN pip install ...` line:

```dockerfile
# The DB-IP Lite city file for origin.py, the newest each build. Never fails
# the build; /health says whether it is there.
COPY docker/fetch_geo_db.py /tmp/fetch_geo_db.py
RUN python /tmp/fetch_geo_db.py /app/geo && rm /tmp/fetch_geo_db.py
ENV EMOTORAD_GEO_DB=/app/geo/dbip-city-lite.mmdb
```

- [ ] **Step 4: The report script**

`scripts/origin_report.py`:

```python
"""Where conversations came from: runs counted by country, state or city.

    python scripts/origin_report.py --from 2026-10-01 --to 2026-10-31 --by region
    python scripts/origin_report.py --from 2026-10-01 --to 2026-10-31 --by country --channel amiigo_app

Read-only. Reads EMOTORAD_MONGO_URI (never printed). Run by a person.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.origin import CREDIT  # noqa: E402

LEVELS = {"country": ("country",), "region": ("country", "region"), "city": ("country", "region", "city")}


def counts(collection: Any, by: str, start: str, end: str, channel: Optional[str] = None) -> List[Tuple[str, int]]:
    """Runs per place, most first. `end` is inclusive (a date)."""
    stop = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    match = {"started_at": {"$gte": start, "$lt": stop}}
    if channel:
        match["channel"] = channel
    fields = LEVELS[by]
    rows = collection.aggregate([
        {"$match": match},
        {"$group": {"_id": {f: "$" + f for f in fields}, "n": {"$sum": 1}}},
    ])
    labelled = [(" / ".join(str(row["_id"].get(f) or "unknown") for f in fields), row["n"]) for row in rows]
    return sorted(labelled, key=lambda item: (-item[1], item[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--by", choices=sorted(LEVELS), default="region")
    parser.add_argument("--channel")
    args = parser.parse_args()

    from emotorad_ai.stores.mongo import CONVERSATION_ORIGINS, connect

    rows = counts(connect()[CONVERSATION_ORIGINS], args.by, args.start, args.end, args.channel)
    width = max([len(label) for label, _ in rows] + [5])
    for label, n in rows:
        print("%-*s %6d" % (width, label, n))
    print("%-*s %6d" % (width, "total", sum(n for _, n in rows)))
    print(CREDIT)


if __name__ == "__main__":
    main()
```

`connect()` (stores/mongo.py) reads `EMOTORAD_MONGO_URI` and returns the `emotorad_ai` database.

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_fetch_geo_db tests.test_origin_report`
Expected: `Ran 5 tests ... OK`

- [ ] **Step 6: The docs**

In `CLAUDE.md`, after the "Amigo, read-only" bullet, add:

```markdown
- **Where conversations come from** (spec 2026-10-01): one record per conversation run in `conversation_origins` (country, state, city; `source` ip or phone), from the run's first message. The IP comes from nginx's `X-Real-IP`, trusted only from `EMOTORAD_TRUSTED_PROXIES`, and is never stored or logged. The DB-IP Lite file is fetched at image build (`docker/fetch_geo_db.py`); `/health` shows `ip_location`. Permanent, erased by `scripts/delete_person.py`. Reports: `scripts/origin_report.py`, which prints the DB-IP credit the licence requires.
```

- [ ] **Step 7: Run the whole suite**

Run: the Global Constraints test command. Expected: only the 3 known failures.

- [ ] **Step 8: Commit**

```bash
git add docker/fetch_geo_db.py Dockerfile scripts/origin_report.py tests/test_fetch_geo_db.py tests/test_origin_report.py CLAUDE.md
git commit -m "The DB-IP file in the image, the origin report, and the docs"
```
