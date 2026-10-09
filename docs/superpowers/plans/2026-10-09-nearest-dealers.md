# The Nearest Dealers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A customer agent can show the three EMotorad dealer stores nearest the customer's pin code, as cards carrying each store's full address and its manager's name and phone, with the customer's area taken from the Amiigo app's location or a typed pin code.

**Architecture:** A bundled pin-code centres file (built from India Post's open directory) gives every pin code a point. `tools/dealer_stores.py` reads OMS's active Dealers stores read-only, places each at its pin code's centre and ranks them by straight-line distance. A read-only tool, `find_nearest_dealers`, gives the model names and distances only and hands the full cards to code through a side channel (`StoreCards`), which the runtime puts on `Reply.stores`. The Amiigo socket, the history, the transcript and the website chat carry the cards. A location becomes a pin code and district at once and only those are kept.

**Tech Stack:** Python 3, stdlib `unittest`, psycopg 3 (already a dependency), FastAPI, the existing registry and runtime.

**Spec:** `docs/superpowers/specs/2026-10-09-nearest-dealers-design.md`

## Global Constraints

- Store types: `franchise_type_name = 'Dealers'` only; `is_active`, `deleted_at IS NULL`, not `is_distributor`.
- Manager: the store's `em_users` row with `user_type = 'franchise_manager'` and `related_id` = the store id (newest `updated_at`, active, not deleted, with a mobile); otherwise `poc_name` and the store's `mobile`. A store with neither is left out.
- Distance: the centre of the customer's pin code to the centre of the store's pin code, haversine, rounded to whole kilometres. Three stores, nearest first, ties by store name. `far` when the nearest is over 100 km.
- Pin-code centres file: `src/emotorad_ai/data/pincode_centres.csv`, columns `pincode,latitude,longitude,basis`, 4 decimal places, basis `offices` or `district`. India box latitude 6 to 37.5, longitude 68 to 97.5. Outliers over 25 km from the median are dropped. `nearest` returns None beyond 50 km.
- Dealer stores cache: 10 minutes; breaker 60 seconds after a failure; serve the last good list for up to 1 hour; read-only connection options as `tools/oms_db.py` (`-c statement_timeout=5000 -c default_transaction_read_only=on -c TimeZone=UTC`).
- No coordinates are ever stored, logged or shown to the model. Manager names and phones never reach the model or a log.
- The model sees only `store_ref` (`D1`..`D3`), `store_name`, `locality`, `distance_km`, `far`, `area` and a note. Code puts the cards on the reply.
- Card shape, everywhere: `{"ref", "name", "address", "pincode", "manager_name", "phone", "distance_km"}`.
- Phone shown as `+91 ` and ten digits when it has ten (after dropping a leading `0` or `91`); otherwise as stored, trimmed.
- The tool is offered only to the customer persona's battery, motor and narrow agents, and only when the registry was built with a dealer directory.
- Never edit anything under `knowledge/` (the knowledge freeze).
- Never write a regex through a shell heredoc. Use `[\wऀ-ॿ]` and never bare `\b` or `\w` on text that may be Hindi (none of this plan's regexes touch customer prose).
- Tests: `python3 -m unittest discover -s tests -t .` must stay green. Test data is fake: dealer phones `9000000001` and up, customer phone `+919999999999`.
- British English, no em dashes, in prompts, docs and comments.

## Review Focus

1. A rider sends words and a location together: the words must reach the agent unchanged (today they are replaced). Pinned in Task 3.
2. A location the app sends badly formed (string, out of range, missing a field) must never refuse the message on the socket. Pinned in Task 3.
3. A typed pin code India Post does not know, or one with no centre, must give `bad_pincode`, never three random stores. Pinned in Task 4.
4. OMS unreadable on the first load (no cached list yet) must give `oms_unavailable`, never an empty `ok`. Pinned in Tasks 2 and 4.
5. A safety report must never carry store cards, and cards from a turn whose reply was replaced by a guardrail must not leak into the next turn. Pinned in Task 5.

Rulings taken while planning (the executor carries them):

- The data file lives at `src/emotorad_ai/data/pincode_centres.csv`, not `knowledge/_geo/` (knowledge freeze); the spec was updated to match.
- `no_area` is returned as an `ok` result with `outcome: "no_area"` and the `request_location` action, because the agent loop collects actions only from successful results. `bad_pincode` and `oms_unavailable` are errors.
- The stores travel on the bot's message object (`message.stores`), which the live `reply` frame and the history both use, rather than as a separate frame-level key.
- "Never offer request_location while the chat holds an area" applies to `find_nearest_dealers` only. The replacement-address `offer_location_share` keeps offering, because a delivery address can differ from where the rider is.
- The `tool_call` event logs the model's view of the result (store names, distances, the customer's pin code), as every other tool's result is logged. Manager names and phones are never in it.
- The website's `LocationIn` keeps refusing an out-of-range location with 422 (its own contract); only the Amiigo socket ignores a bad one, per the app team's addendum.

---

### Task 1: Pin-code centres and distance

**Files:**
- Create: `src/emotorad_ai/geo.py`
- Create: `scripts/build_pincode_centres.py`
- Create (generated): `src/emotorad_ai/data/pincode_centres.csv`
- Test: `tests/test_geo.py`

**Interfaces:**
- Consumes: `address.PincodeDirectory.load().lookup(pincode) -> List[Place]` (`Place.district`, `Place.state`).
- Produces:
  - `geo.Point = Tuple[float, float]`
  - `geo.distance_km(a: Point, b: Point) -> float`
  - `geo.in_india(point: Point) -> bool`
  - `geo.build_centres(offices: Iterable[Tuple[str, Optional[float], Optional[float]]], district_of: Dict[str, str]) -> Dict[str, Tuple[float, float, str]]`
  - `geo.PincodeCentres(centres: Dict[str, Point])`, `.load(path=None) -> PincodeCentres`, `.centre(pincode) -> Optional[Point]`, `.nearest(latitude, longitude) -> Optional[str]`, `len()`
  - constants `CENTRES_PATH`, `NEAREST_LIMIT_KM = 50.0`, `OUTLIER_KM = 25.0`

- [ ] **Step 1: Write the failing tests**

`tests/test_geo.py`:

```python
"""Pin-code centres and straight-line distance (spec 2026-10-09, section 1)."""

import unittest

from emotorad_ai import geo
from emotorad_ai.geo import PincodeCentres, build_centres, distance_km


class DistanceTests(unittest.TestCase):
    def test_known_city_pairs(self):
        pune, mumbai = (18.5204, 73.8567), (19.0760, 72.8777)
        delhi, bengaluru = (28.6139, 77.2090), (12.9716, 77.5946)
        self.assertTrue(115 <= distance_km(pune, mumbai) <= 125, distance_km(pune, mumbai))
        self.assertTrue(1730 <= distance_km(delhi, bengaluru) <= 1750, distance_km(delhi, bengaluru))
        self.assertEqual(distance_km(pune, pune), 0.0)


class BuildTests(unittest.TestCase):
    def test_the_median_of_the_offices_without_a_far_outlier(self):
        offices = [("411014", 18.560, 73.910), ("411014", 18.562, 73.912), ("411014", 18.558, 73.915),
                   ("411014", 20.50, 75.50)]  # one office's point is 300 km off
        centres = build_centres(offices, {})
        lat, lon, basis = centres["411014"]
        self.assertEqual(basis, "offices")
        self.assertAlmostEqual(lat, 18.560, places=2)
        self.assertAlmostEqual(lon, 73.912, places=2)

    def test_points_outside_india_and_malformed_pincodes_are_dropped(self):
        offices = [("110001", 0.0, 0.0), ("110001", 28.632, 77.219), ("12345", 28.6, 77.2), ("ABCDEF", 28.6, 77.2)]
        centres = build_centres(offices, {})
        self.assertEqual(set(centres), {"110001"})
        self.assertAlmostEqual(centres["110001"][0], 28.632, places=3)

    def test_a_pincode_with_no_point_takes_its_districts_centre(self):
        offices = [("411014", 18.56, 73.91), ("411001", 18.52, 73.86), ("411099", None, None)]
        district_of = {"411014": "Pune|Maharashtra", "411001": "Pune|Maharashtra", "411099": "Pune|Maharashtra"}
        centres = build_centres(offices, district_of)
        lat, lon, basis = centres["411099"]
        self.assertEqual(basis, "district")
        self.assertAlmostEqual(lat, 18.54, places=2)
        self.assertAlmostEqual(lon, 73.885, places=2)

    def test_values_are_rounded_to_four_places(self):
        centres = build_centres([("411014", 18.123456789, 73.987654321)], {})
        self.assertEqual(centres["411014"], (18.1235, 73.9877, "offices"))


class NearestTests(unittest.TestCase):
    def setUp(self):
        self.centres = PincodeCentres({"411014": (18.56, 73.91), "400054": (19.06, 72.84)})

    def test_the_nearest_centre_names_the_pincode(self):
        self.assertEqual(self.centres.nearest(18.55, 73.92), "411014")
        self.assertEqual(self.centres.nearest(19.07, 72.83), "400054")

    def test_nothing_within_fifty_km_or_outside_india_is_none(self):
        self.assertIsNone(self.centres.nearest(21.0, 79.0))  # Nagpur: far from both
        self.assertIsNone(self.centres.nearest(15.0, 65.0))  # the Arabian Sea, outside the box

    def test_centre_of_an_unknown_pincode_is_none(self):
        self.assertIsNone(self.centres.centre("999999"))
        self.assertEqual(self.centres.centre(" 411014 "), (18.56, 73.91))


class ShippedFileTests(unittest.TestCase):
    def test_the_file_covers_india_and_places_known_pincodes(self):
        centres = PincodeCentres.load()
        self.assertGreater(len(centres), 19000)
        self.assertLess(distance_km(centres.centre("110001"), (28.63, 77.22)), 10)
        self.assertLess(distance_km(centres.centre("411014"), (18.56, 73.91)), 15)

    def test_the_file_names_its_source_and_licence(self):
        head = geo.CENTRES_PATH.read_text().splitlines()[0]
        self.assertIn("data.gov.in", head)
        self.assertIn("Open Government Data", head)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_geo -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.geo'`.

- [ ] **Step 3: Write `src/emotorad_ai/geo.py`**

```python
"""Pin-code centres and straight-line distance (spec 2026-10-09, section 1).

Every pin code India Post knows gets one point: the median of its post
offices' coordinates, without any that fall outside India or more than
25 km from that median, or its district's centre when none of its offices
has a usable point. The file is built by scripts/build_pincode_centres.py
from India Post's open directory and is never edited by hand.

Distance is the haversine formula: a straight line, not a road.
"""

from __future__ import annotations

import csv
import math
import pathlib
import threading
from typing import Dict, Iterable, List, Optional, Tuple

Point = Tuple[float, float]

CENTRES_PATH = pathlib.Path(__file__).resolve().parent / "data" / "pincode_centres.csv"
HEADER = ["pincode", "latitude", "longitude", "basis"]
EARTH_RADIUS_KM = 6371.0088
# A point further than this from every centre (at sea, abroad) has no pin code.
NEAREST_LIMIT_KM = 50.0
# An office this far from its pin code's median is a typing error in the source.
OUTLIER_KM = 25.0
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 6.0, 37.5, 68.0, 97.5


def distance_km(a: Point, b: Point) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def in_india(point: Point) -> bool:
    return LAT_MIN <= point[0] <= LAT_MAX and LON_MIN <= point[1] <= LON_MAX


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def _median_point(points: List[Point]) -> Point:
    return _median([p[0] for p in points]), _median([p[1] for p in points])


def build_centres(offices: Iterable[Tuple[str, Optional[float], Optional[float]]],
                  district_of: Dict[str, str]) -> Dict[str, Tuple[float, float, str]]:
    """pincode -> (latitude, longitude, basis). `offices` is (pincode, lat,
    lon) per post office, lat and lon None where the source has none;
    `district_of` is pincode -> "district|state" for the district fallback."""
    points: Dict[str, List[Point]] = {}
    known = set()
    for pincode, lat, lon in offices:
        pincode = str(pincode).strip()
        if len(pincode) != 6 or not pincode.isdigit():
            continue
        known.add(pincode)
        if lat is None or lon is None or not in_india((lat, lon)):
            continue
        points.setdefault(pincode, []).append((lat, lon))
    result: Dict[str, Tuple[float, float, str]] = {}
    for pincode, found in points.items():
        middle = _median_point(found)
        kept = [p for p in found if distance_km(p, middle) <= OUTLIER_KM] or found
        lat, lon = _median_point(kept)
        result[pincode] = (round(lat, 4), round(lon, 4), "offices")
    by_district: Dict[str, List[Point]] = {}
    for pincode, (lat, lon, _) in result.items():
        district = district_of.get(pincode)
        if district:
            by_district.setdefault(district, []).append((lat, lon))
    for pincode in sorted(known | set(district_of)):
        district = district_of.get(pincode)
        if pincode in result or not district or not by_district.get(district):
            continue
        lat, lon = _median_point(by_district[district])
        result[pincode] = (round(lat, 4), round(lon, 4), "district")
    return result


class PincodeCentres:
    """pincode -> point, from the file, loaded once per process."""

    _shared: Optional["PincodeCentres"] = None
    _lock = threading.Lock()

    def __init__(self, centres: Dict[str, Point]) -> None:
        self._centres = dict(centres)

    @classmethod
    def load(cls, path: Optional[pathlib.Path] = None) -> "PincodeCentres":
        if path is not None:
            return cls._read(pathlib.Path(path))
        with cls._lock:
            if cls._shared is None:
                cls._shared = cls._read(CENTRES_PATH)
            return cls._shared

    @classmethod
    def _read(cls, path: pathlib.Path) -> "PincodeCentres":
        centres: Dict[str, Point] = {}
        with path.open(newline="") as handle:
            rows = csv.reader(line for line in handle if not line.startswith("#"))
            header = next(rows, None)
            if header != HEADER:
                raise ValueError("%s: expected columns %s; got %r" % (path, ",".join(HEADER), header))
            for pincode, lat, lon, _basis in rows:
                centres[pincode] = (float(lat), float(lon))
        return cls(centres)

    def __len__(self) -> int:
        return len(self._centres)

    def centre(self, pincode: Optional[str]) -> Optional[Point]:
        return self._centres.get(str(pincode or "").strip())

    def nearest(self, latitude: float, longitude: float) -> Optional[str]:
        """The pin code whose centre is nearest the point, or None at sea,
        abroad, or more than NEAREST_LIMIT_KM from every centre."""
        point = (latitude, longitude)
        if not in_india(point):
            return None
        # A flat approximation ranks the candidates; haversine checks the winner.
        squeeze = math.cos(math.radians(latitude))
        best, best_score = None, None
        for pincode, (lat, lon) in self._centres.items():
            score = (lat - latitude) ** 2 + ((lon - longitude) * squeeze) ** 2
            if best_score is None or score < best_score:
                best, best_score = pincode, score
        if best is None or distance_km(point, self._centres[best]) > NEAREST_LIMIT_KM:
            return None
        return best
```

- [ ] **Step 4: Write `scripts/build_pincode_centres.py`**

```python
#!/usr/bin/env python3
"""Build src/emotorad_ai/data/pincode_centres.csv (spec 2026-10-09, section 1).

The source is India Post's "All India Pincode Directory" (data.gov.in, Open
Government Data Licence India), from the public mirror our pincodes.csv came
from. Download it once, then run:

    curl -sSo /tmp/all-india-pincodes.csv \
      https://raw.githubusercontent.com/arobindo/pincode-india-csv/main/all-india-pincodes.csv
    python scripts/build_pincode_centres.py /tmp/all-india-pincodes.csv
"""

import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.address import PincodeDirectory  # noqa: E402
from emotorad_ai.geo import CENTRES_PATH, HEADER, build_centres  # noqa: E402

SOURCE = ("https://raw.githubusercontent.com/arobindo/pincode-india-csv/main/all-india-pincodes.csv")


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    offices = []
    with open(argv[1], newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            offices.append((row["pincode"], _number(row.get("latitude")), _number(row.get("longitude"))))
    directory = PincodeDirectory.load()
    district_of = {}
    for pincode, _lat, _lon in offices:
        places = directory.lookup(str(pincode).strip())
        if places:
            district_of[str(pincode).strip()] = "%s|%s" % (places[0].district, places[0].state)
    centres = build_centres(offices, district_of)
    CENTRES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CENTRES_PATH.open("w", newline="") as out:
        out.write("# India Post pincode centres. Source: data.gov.in \"All India Pincode Directory\" "
                  "(Open Government Data Licence India), via %s, built %s by "
                  "scripts/build_pincode_centres.py. Do not hand-edit.\n" % (SOURCE, date.today().isoformat()))
        writer = csv.writer(out)
        writer.writerow(HEADER)
        for pincode in sorted(centres):
            lat, lon, basis = centres[pincode]
            writer.writerow([pincode, "%.4f" % lat, "%.4f" % lon, basis])
    by_basis = {}
    for _lat, _lon, basis in centres.values():
        by_basis[basis] = by_basis.get(basis, 0) + 1
    print("wrote %d pincodes to %s: %s" % (len(centres), CENTRES_PATH, by_basis))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 5: Generate the data file**

Run:
```bash
mkdir -p /tmp/pincode-src && curl -sSo /tmp/pincode-src/all-india-pincodes.csv https://raw.githubusercontent.com/arobindo/pincode-india-csv/main/all-india-pincodes.csv
python3 scripts/build_pincode_centres.py /tmp/pincode-src/all-india-pincodes.csv
```
Expected: `wrote 195xx pincodes to .../src/emotorad_ai/data/pincode_centres.csv: {'offices': 195xx, 'district': ...}` with offices at least 19,500.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_geo -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/geo.py scripts/build_pincode_centres.py src/emotorad_ai/data/pincode_centres.csv tests/test_geo.py
git commit -m "feat(geo): pin-code centres from India Post's directory, and straight-line distance"
```

---

### Task 2: Dealer stores from OMS

**Files:**
- Create: `src/emotorad_ai/tools/dealer_stores.py`
- Test: `tests/test_dealer_stores.py`, `tests/test_dealer_stores_sql.py`

**Interfaces:**
- Consumes: `geo.PincodeCentres`, `geo.distance_km`, `geo.Point`; `tools.oms_db.DSN_ENV`, `CONNECT_TIMEOUT_SECONDS`, `STATEMENT_TIMEOUT_MS`, `APPLICATION_NAME`, `_psycopg_connect`.
- Produces:
  - `STORES_SQL: str`, `FIXTURE_ROWS: Tuple[dict, ...]`, constants `CACHE_SECONDS = 600`, `FAILURE_TTL_SECONDS = 60`, `STALE_SECONDS = 3600`, `NEAREST = 3`, `FAR_KM = 100`
  - `class DealerStoresUnavailable(Exception)`
  - `@dataclass(frozen=True) class Store: name, address, pincode, district, state, manager_name, phone: str; point: Point`
  - `display_phone(raw: Optional[str]) -> str`, `full_address(row: dict) -> str`, `store_from_row(row: dict, centres: PincodeCentres) -> Optional[Store]`
  - `store_card(ref: str, store: Store, km: int) -> dict` (the card shape in Global Constraints)
  - `class DealerDirectory(load: Callable[[], List[dict]], centres: PincodeCentres, clock=time.monotonic, log: Optional[Callable[[str, dict], None]] = None)` with `.centres`, `.stores() -> List[Store]`, `.nearest(point: Point, n: int = NEAREST) -> List[Tuple[Store, int]]`
  - `db_loader(dsn: str, connect=None) -> Callable[[], List[dict]]`
  - `directory_from_env(centres, environ=None, log=None) -> Tuple[DealerDirectory, str]` (source label `"oms_db"` or `"fixtures"`)
  - `class StoreCards` with `.put(conversation_id: str, cards: List[dict]) -> None`, `.take(conversation_id: str) -> List[dict]`

- [ ] **Step 1: Write the failing unit tests**

`tests/test_dealer_stores.py`:

```python
"""Dealer stores from OMS, placed at their pin codes' centres (spec 2026-10-09, section 2)."""

import unittest

from emotorad_ai.geo import PincodeCentres
from emotorad_ai.tools import dealer_stores
from emotorad_ai.tools.dealer_stores import (
    DealerDirectory, DealerStoresUnavailable, StoreCards, directory_from_env, display_phone, full_address,
    store_card, store_from_row,
)

CENTRES = PincodeCentres({"411014": (18.56, 73.91), "400054": (19.06, 72.84), "110016": (28.55, 77.20)})


def row(**fields):
    base = {"store_name": "Test Cycles", "address": "Shop 1, Test Road", "address2": None, "pin_code": "411014",
            "district_name": "Pune", "state_name": "Maharashtra", "manager_name": "Test Manager",
            "manager_mobile": "9000000001", "poc_name": "Test Owner", "store_mobile": "9000000009"}
    base.update(fields)
    return base


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RowTests(unittest.TestCase):
    def test_the_franchise_manager_is_the_contact(self):
        store = store_from_row(row(), CENTRES)
        self.assertEqual((store.manager_name, store.phone), ("Test Manager", "+91 9000000001"))
        self.assertEqual(store.point, (18.56, 73.91))

    def test_without_a_manager_the_stores_contact_person(self):
        store = store_from_row(row(manager_name=None, manager_mobile=None), CENTRES)
        self.assertEqual((store.manager_name, store.phone), ("Test Owner", "+91 9000000009"))

    def test_a_store_with_no_contact_or_no_centre_is_left_out(self):
        self.assertIsNone(store_from_row(row(manager_name=None, manager_mobile=None, poc_name="", store_mobile=""), CENTRES))
        self.assertIsNone(store_from_row(row(pin_code="999999"), CENTRES))

    def test_phones_are_shown_with_the_country_code(self):
        self.assertEqual(display_phone("09000000001"), "+91 9000000001")
        self.assertEqual(display_phone("+91 90000-00001"), "+91 9000000001")
        self.assertEqual(display_phone("919000000001"), "+91 9000000001")
        self.assertEqual(display_phone(" 020 1234 "), "020 1234")

    def test_the_address_joins_its_parts_once(self):
        self.assertEqual(full_address(row(address2="Near Test Chowk")),
                         "Shop 1, Test Road, Near Test Chowk, Pune, Maharashtra - 411014")
        self.assertEqual(full_address(row(address="Shop 1, Pune 411014", address2="pune")),
                         "Shop 1, Pune 411014, pune, Maharashtra")

    def test_the_card_shape(self):
        store = store_from_row(row(), CENTRES)
        self.assertEqual(store_card("D1", store, 3), {
            "ref": "D1", "name": "Test Cycles", "address": "Shop 1, Test Road, Pune, Maharashtra - 411014",
            "pincode": "411014", "manager_name": "Test Manager", "phone": "+91 9000000001", "distance_km": 3})


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.rows = [row(store_name="Pune Store"), row(store_name="Mumbai Store", pin_code="400054"),
                     row(store_name="Delhi Store", pin_code="110016"), row(store_name="Nowhere", pin_code="999999")]
        self.fail = False
        self.clock = Clock()
        self.events = []

        def load():
            self.calls += 1
            if self.fail:
                raise OSError("down")
            return [dict(r) for r in self.rows]

        self.directory = DealerDirectory(load, CENTRES, clock=self.clock,
                                         log=lambda event, fields: self.events.append((event, fields)))

    def test_nearest_three_in_order_with_whole_kilometres(self):
        found = self.directory.nearest((18.52, 73.86))
        self.assertEqual([s.name for s, _ in found], ["Pune Store", "Mumbai Store", "Delhi Store"])
        self.assertEqual(found[0][1], 7)
        self.assertTrue(all(isinstance(km, int) for _, km in found))

    def test_ties_are_broken_by_name(self):
        self.rows = [row(store_name="B Store"), row(store_name="A Store")]
        self.assertEqual([s.name for s, _ in self.directory.nearest((18.56, 73.91))], ["A Store", "B Store"])

    def test_loaded_once_for_ten_minutes_and_unplaced_counted_without_names(self):
        self.directory.stores()
        self.clock.now += dealer_stores.CACHE_SECONDS - 1
        self.directory.stores()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.events, [("dealer_stores_loaded", {"stores": 3, "unplaced": 1})])
        self.clock.now += 2
        self.directory.stores()
        self.assertEqual(self.calls, 2)

    def test_a_failed_refresh_serves_the_last_list_for_an_hour(self):
        self.directory.stores()
        self.fail = True
        self.clock.now += dealer_stores.CACHE_SECONDS + 1
        self.assertEqual(len(self.directory.stores()), 3)
        self.clock.now += dealer_stores.STALE_SECONDS
        with self.assertRaises(DealerStoresUnavailable):
            self.directory.stores()

    def test_a_failed_first_load_is_unavailable_and_the_breaker_holds_a_minute(self):
        self.fail = True
        with self.assertRaises(DealerStoresUnavailable):
            self.directory.stores()
        with self.assertRaises(DealerStoresUnavailable):
            self.directory.stores()
        self.assertEqual(self.calls, 1)
        self.clock.now += dealer_stores.FAILURE_TTL_SECONDS + 1
        self.fail = False
        self.assertEqual(len(self.directory.stores()), 3)

    def test_the_error_names_no_connection_detail(self):
        def load():
            raise OSError("postgresql://user:secret@host/db")

        with self.assertRaises(DealerStoresUnavailable) as caught:
            DealerDirectory(load, CENTRES).stores()
        self.assertNotIn("secret", str(caught.exception))


class EnvTests(unittest.TestCase):
    def test_without_a_dsn_the_fixtures(self):
        directory, source = directory_from_env(CENTRES, environ={})
        self.assertEqual(source, "fixtures")
        self.assertEqual(len(directory.stores()), 3)

    def test_with_a_dsn_the_database(self):
        _, source = directory_from_env(CENTRES, environ={dealer_stores.DSN_ENV: "postgresql://x"})
        self.assertEqual(source, "oms_db")


class CardsTests(unittest.TestCase):
    def test_taken_once(self):
        cards = StoreCards()
        cards.put("c1", [{"ref": "D1"}])
        self.assertEqual(cards.take("c1"), [{"ref": "D1"}])
        self.assertEqual(cards.take("c1"), [])
        self.assertEqual(cards.take("c2"), [])


if __name__ == "__main__":
    unittest.main()
```

`tests/test_dealer_stores_sql.py`:

```python
"""The dealer stores query run by Postgres (spec 2026-10-09, section 2).

Opt-in: set EMOTORAD_TEST_OMS_PG_DSN to any Postgres connection string. No
table is read: the query's tables are replaced by inline rows of made-up
data, so the check is the SQL's own behaviour.
"""

import os
import unittest

from emotorad_ai.tools.dealer_stores import STORES_SQL

DSN_ENV = "EMOTORAD_TEST_OMS_PG_DSN"

FIXTURES = """WITH em_franchise_type(id, franchise_type_name) AS (VALUES ('T1', 'Dealers'), ('T2', 'Others')),
em_pin_code(id, pin_code) AS (VALUES ('P1', '411014'), ('P2', '400054')),
em_district(id, district_name) AS (VALUES ('DI1', 'Pune')),
em_state(id, state_name) AS (VALUES ('S1', 'Maharashtra')),
em_franchise(id, customer_name, address, address2, pin_code_id, district_id, state_id, franchise_type_id,
             poc_name, mobile, is_active, deleted_at, is_distributor) AS (VALUES
  ('F1', 'Dealer One', 'Road 1', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner One', '9000000011', true, NULL::timestamptz, false),
  ('F2', 'Other Store', 'Road 2', NULL, 'P1', 'DI1', 'S1', 'T2', 'Owner Two', '9000000012', true, NULL::timestamptz, false),
  ('F3', 'Distributor', 'Road 3', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner Three', '9000000013', true, NULL::timestamptz, true),
  ('F4', 'Closed Dealer', 'Road 4', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner Four', '9000000014', false, NULL::timestamptz, false),
  ('F5', 'Deleted Dealer', 'Road 5', NULL, 'P1', 'DI1', 'S1', 'T1', 'Owner Five', '9000000015', true, now(), false),
  ('F6', 'Dealer Six', 'Road 6', NULL, 'P2', NULL, NULL, 'T1', 'Owner Six', '9000000016', true, NULL::timestamptz, NULL::boolean)),
em_users(full_name, mobile, user_type, related_id, is_active, deleted_at, updated_at) AS (VALUES
  ('Manager Old', '9000000021', 'franchise_manager', 'F1', true, NULL::timestamptz, now() - interval '1 day'),
  ('Manager New', '9000000022', 'franchise_manager', 'F1', true, NULL::timestamptz, now()),
  ('Sales Person', '9000000023', 'sale_franchise_person', 'F6', true, NULL::timestamptz, now()))
"""


@unittest.skipUnless(os.environ.get(DSN_ENV), "set %s to run the SQL against Postgres" % DSN_ENV)
class RealSqlTests(unittest.TestCase):
    def rows(self):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(os.environ[DSN_ENV], row_factory=dict_row,
                             options="-c default_transaction_read_only=on") as conn:
            return {r["id"]: r for r in conn.execute(FIXTURES + STORES_SQL).fetchall()}

    def test_only_active_dealers_and_the_newest_franchise_manager(self):
        rows = self.rows()
        self.assertEqual(set(rows), {"F1", "F6"})
        self.assertEqual((rows["F1"]["manager_name"], rows["F1"]["manager_mobile"]), ("Manager New", "9000000022"))
        self.assertIsNone(rows["F6"]["manager_name"])
        self.assertEqual((rows["F6"]["poc_name"], rows["F6"]["pin_code"]), ("Owner Six", "400054"))


class FixtureTests(unittest.TestCase):
    def test_the_query_starts_with_select_so_the_fixture_ctes_lead_it(self):
        self.assertTrue(STORES_SQL.lstrip().upper().startswith("SELECT"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_dealer_stores tests.test_dealer_stores_sql -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.tools.dealer_stores'`.

- [ ] **Step 3: Write `src/emotorad_ai/tools/dealer_stores.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_dealer_stores tests.test_dealer_stores_sql -v`
Expected: all PASS (the SQL class skipped without `EMOTORAD_TEST_OMS_PG_DSN`). If a local Postgres is available, also run with `EMOTORAD_TEST_OMS_PG_DSN=postgresql:///postgres` and expect PASS.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/tools/dealer_stores.py tests/test_dealer_stores.py tests/test_dealer_stores_sql.py
git commit -m "feat(dealers): OMS dealer stores placed at their pin codes, cached, with the store cards side channel"
```

---

### Task 3: The customer's area from a location

**Files:**
- Modify: `src/emotorad_ai/location.py` (add `CentresGeocoder`, `area_of`)
- Modify: `src/emotorad_ai/api.py` (prepare_turn around lines 1325-1331 and 1366-1370; the module-level `geocoder` at line 363)
- Modify: `src/emotorad_ai/amiigo/socket.py` (`MessageFrame`, `read_frame` around line 282, `_frame` around line 505)
- Modify: `src/emotorad_ai/conversation.py` (`ConversationState.area`)
- Modify: `src/emotorad_ai/runtime.py` (`TURN_FACT_FIELDS` at line 464, `_handle` after line 770)
- Test: `tests/test_location.py`, `tests/test_prepare_turn.py`, `tests/test_amiigo_socket.py`, `tests/test_area.py`

**Interfaces:**
- Consumes: `geo.PincodeCentres.nearest(lat, lon)`, `location.resolve_location`, `location.LocationResult`.
- Produces:
  - `location.CentresGeocoder(centres)` with `.reverse(latitude, longitude) -> Optional[dict]` (`{"postcode": pincode}`)
  - `location.area_of(result: Optional[LocationResult], source: str, at: str) -> Optional[dict]` returning `{"pincode", "district", "state", "source", "at"}`
  - `InboundMessage.entry_metadata["area"]` (that dict, `source: "location"`)
  - `ConversationState.area: Optional[Dict[str, Any]] = None`
  - `MessageFrame.location_ignored: Optional[str] = None` and event `location_ignored` with field `reason`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_location.py`:

```python
class CentresGeocoderTests(unittest.TestCase):
    def test_the_nearest_centre_is_the_postcode_and_no_third_party_is_asked(self):
        from emotorad_ai.geo import PincodeCentres
        from emotorad_ai.location import CentresGeocoder

        geocoder = CentresGeocoder(PincodeCentres({"122018": (28.41, 77.05)}))
        self.assertEqual(geocoder.reverse(28.415, 77.052), {"postcode": "122018"})
        self.assertIsNone(geocoder.reverse(15.0, 65.0))

    def test_area_of_keeps_the_pincode_district_and_state_only(self):
        from emotorad_ai.location import LocationResult, area_of

        result = LocationResult(pincode="122018", area="Sector 49", district="Gurugram", state="Haryana")
        self.assertEqual(area_of(result, "location", "2026-10-09T10:00:00+00:00"),
                         {"pincode": "122018", "district": "Gurugram", "state": "Haryana", "source": "location",
                          "at": "2026-10-09T10:00:00+00:00"})
        self.assertIsNone(area_of(None, "location", "2026-10-09T10:00:00+00:00"))
```

Add to `tests/test_prepare_turn.py`, inside `class SameMessageAsPostMessageTests(ApiCase)`:

```python
    def test_words_with_a_location_keep_the_words_and_carry_the_area(self):
        class Geocoder:
            def reverse(self, lat, lon):
                return {"postcode": "122018"}

        self.api.geocoder = Geocoder()
        made = self.api.prepare_turn(conversation_id="c9", text="my battery isn't charging", em_aid="aid-loc",
                                     location=self.api.LocationIn(latitude=28.41, longitude=77.05))
        self.assertEqual(made.message_text, "my battery isn't charging")
        area = made.entry_metadata["area"]
        self.assertEqual((area["pincode"], area["source"]), ("122018", "location"))
        self.assertNotIn("28.41", json.dumps(made.to_dict(), default=str))
        self.assertNotIn("77.05", json.dumps(made.to_dict(), default=str))

    def test_a_location_alone_is_still_the_customers_message_and_carries_the_area(self):
        class Geocoder:
            def reverse(self, lat, lon):
                return {"postcode": "122018", "suburb": "Sector 49"}

        self.api.geocoder = Geocoder()
        made = self.api.prepare_turn(conversation_id="c10", text="", em_aid="aid-loc",
                                     location=self.api.LocationIn(latitude=28.41, longitude=77.05))
        self.assertIn("Pincode 122018", made.message_text)
        self.assertEqual(made.entry_metadata["area"]["pincode"], "122018")
```

(Add `import json` at the top of `tests/test_prepare_turn.py` if it is not there.)

In `tests/test_amiigo_socket.py`, remove the three `message(location=...)` lines from `test_a_message_missing_a_field_or_with_a_wrong_one_is_bad_frame`, and add:

```python
    def test_a_bad_location_is_ignored_and_logged_without_its_values(self):
        cases = [{"latitude": 18.52}, {"latitude": 91, "longitude": 73.85},
                 {"latitude": True, "longitude": 73.85}, "18.52,73.85"]
        with self.socket() as ws:
            for n, location in enumerate(cases, start=1):
                with self.subTest(location=location):
                    _, ack, reply = self.exchange(ws, message(n=n, location=location))
                    self.assertEqual(reply["type"], "reply")
        self.assertEqual(len(self.turns.calls), len(cases))
        self.assertEqual(self.turns.calls[0].message_text, "my battery isn't charging")
        ignored = [e for e in self.api.log.events if e.get("event") == "location_ignored"]
        self.assertEqual(len(ignored), len(cases))
        self.assertNotIn("73.85", json.dumps(ignored))
```

Create `tests/test_area.py`:

```python
"""The chat keeps the customer's area, never coordinates (spec 2026-10-09, section 3)."""

import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.config import Settings
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import TURN_FACT_FIELDS, Runtime
from emotorad_ai.tools.mocks import build_registry

AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def runtime_with(responses):
    registry = build_registry(today=date(2026, 10, 9))
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry,
                      llm=ScriptedClaude(responses), log=EventLog(path=None), resolver=IdentityResolver(registry),
                      self_service_identity=True)
    return runtime, WebsiteChatAdapter(runtime.resolver)


def send(runtime, adapter, text, area=None):
    runtime.conversations.get("conv-a").route_to(AGENT_NAME)
    message = adapter.to_message({"conversation_id": "conv-a", "session_token": "sess-ananya", "text": text})
    if area is not None:
        message = replace(message, entry_metadata=dict(message.entry_metadata, area=area))
    return runtime.handle(message)


class AreaTests(unittest.TestCase):
    def test_the_area_is_a_turn_fact(self):
        self.assertIn("area", TURN_FACT_FIELDS)
        self.assertIsNone(ConversationState(conversation_id="x").area)

    def test_a_messages_area_is_kept_and_survives_the_next_message(self):
        runtime, adapter = runtime_with([say("Thanks."), say("Sure.")])
        send(runtime, adapter, "hi", area=AREA)
        self.assertEqual(runtime.conversations.get("conv-a").area, AREA)
        send(runtime, adapter, "and another thing")
        self.assertEqual(runtime.conversations.get("conv-a").area, AREA)

    def test_a_later_area_replaces_it(self):
        runtime, adapter = runtime_with([say("Thanks."), say("Sure.")])
        send(runtime, adapter, "hi", area=AREA)
        later = dict(AREA, pincode="400054", district="Mumbai")
        send(runtime, adapter, "I'm in Mumbai now", area=later)
        self.assertEqual(runtime.conversations.get("conv-a").area["pincode"], "400054")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_location tests.test_area tests.test_prepare_turn tests.test_amiigo_socket -v`
Expected: FAIL: `ImportError: cannot import name 'CentresGeocoder'`, `KeyError: 'area'`, the socket case answering `bad_frame`, and `AttributeError: ... 'area'`.

- [ ] **Step 3: Add `CentresGeocoder` and `area_of` to `src/emotorad_ai/location.py`**

Append after `describe_location`:

```python
class CentresGeocoder:
    """Our own reverse geocoder: the pin code whose centre is nearest the
    point (geo.PincodeCentres, spec 2026-10-09, section 3). No third party
    sees the point, and it answers at once, well inside the app team's
    two-second limit. It names no locality, only the postcode."""

    def __init__(self, centres: Any) -> None:
        self.centres = centres

    def reverse(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        pincode = self.centres.nearest(latitude, longitude)
        return {"postcode": pincode} if pincode else None


def area_of(result: Optional[LocationResult], source: str, at: str) -> Optional[Dict[str, Any]]:
    """What the chat keeps of a location: the pin code, district and state,
    where they came from and when. Never the coordinates."""
    if result is None:
        return None
    return {"pincode": result.pincode, "district": result.district, "state": result.state,
            "source": source, "at": at}
```

Also update the module docstring's last paragraph: replace "The production choice (MapmyIndia, Google) swaps in here without touching the flow." with "The API uses `CentresGeocoder`, our own pin-code centres, since 9 October 2026; `NominatimGeocoder` stays for a LAN test server."

- [ ] **Step 4: Use them in `src/emotorad_ai/api.py`**

Imports: change `from .location import NominatimGeocoder, describe_location, resolve_location` to `from .location import CentresGeocoder, NominatimGeocoder, area_of, describe_location, resolve_location`, and add `from .geo import PincodeCentres`. Make sure `utc_now_iso` is imported from `.conversation` (add it to the existing `from .conversation import ...` line if absent).

Directly after `OMS_DB = oms_db_tools.reader_from_env()` (line 281), add:

```python
# Every pin code's centre (geo.py, spec 2026-10-09): the shared-location
# geocoder below and the dealer stores both place things by it.
PINCODE_CENTRES = PincodeCentres.load()
```

Replace the module-level geocoder (`geocoder = NominatimGeocoder()`, line 363) and its comment with:

```python
# The reverse geocoder behind a shared location: our own pin-code centres,
# so no third party sees where a rider is. The tests replace it with a fake.
# See location.py for what is and is not trusted from it.
geocoder = CentresGeocoder(PINCODE_CENTRES)
```

Replace in `prepare_turn` (lines 1325-1331):

```python
    said = text
    area = None
    if location is not None:
        # Resolved here, before the message exists, so the coordinates never
        # become part of anything that is logged or handed to the model. Only
        # a message that is nothing but the location becomes the customer's
        # words; with words, a chip or a photo, the location is background
        # (the app team's addendum, 7 October 2026).
        located = resolve_location(location.latitude, location.longitude, geocoder, pincode_directory)
        area = area_of(located, "location", utc_now_iso())
        if not (text or "").strip() and not pill and not found:
            said = describe_location(located)
```

After the `if place is not None: extra["origin"] = place.as_dict()` lines (around 1370), add:

```python
    if area is not None:
        # The runtime keeps it on the conversation (Runtime._handle).
        extra["area"] = area
```

- [ ] **Step 5: Ignore a bad location on the socket (`src/emotorad_ai/amiigo/socket.py`)**

Add a field to `MessageFrame` after `location`:

```python
    # Why a location the frame carried was dropped, for the log (never its
    # values): the app team's addendum makes a bad location harmless.
    location_ignored: Optional[str] = None
```

In `read_frame`, replace:

```python
    try:
        location = _location(body.get("location"))
    except ValueError:
        raise refused() from None
```

with:

```python
    location_ignored = None
    try:
        location = _location(body.get("location"))
    except ValueError as exc:
        location, location_ignored = None, str(exc).replace(" ", "_")
```

and change the return to `return MessageFrame(client_message_id, conversation_id, text, tuple(upload_ids), screen, pill, location, location_ignored)`.

In `_frame`, replace the final `else: await self._message(read)` with:

```python
        else:
            if read.location_ignored:
                self._noted("location_ignored", "amiigo", reason=read.location_ignored)
            await self._message(read)
```

- [ ] **Step 6: Keep the area on the conversation**

In `src/emotorad_ai/conversation.py`, add to `ConversationState` after `origin`:

```python
    # Where the customer is, as an area only (spec 2026-10-09, section 3):
    # {"pincode", "district", "state", "source" ("location" or "typed"), "at"}.
    # Never coordinates. Lives the working state's 48 hours.
    area: Optional[Dict[str, Any]] = None
```

In `src/emotorad_ai/runtime.py`, add `"area",` to `TURN_FACT_FIELDS` with the comment `# The customer's area (spec 2026-10-09).`, and in `_handle`, directly after the line `facts_loaded = {name: getattr(state, name) for name in TURN_FACT_FIELDS}`, add:

```python
            # A location this message carried, as an area (api.prepare_turn).
            # After the snapshot, so a merge after a conflict keeps it.
            area = message.entry_metadata.get("area")
            if isinstance(area, dict) and area.get("pincode"):
                state.area = dict(area)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_location tests.test_area tests.test_prepare_turn tests.test_amiigo_socket tests.test_location_sharing -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/location.py src/emotorad_ai/api.py src/emotorad_ai/amiigo/socket.py src/emotorad_ai/conversation.py src/emotorad_ai/runtime.py tests/test_location.py tests/test_prepare_turn.py tests/test_amiigo_socket.py tests/test_area.py
git commit -m "feat(location): a location becomes the chat's area, words are kept, a bad location is ignored"
```

---

### Task 4: The tool `find_nearest_dealers`

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py` (constant near line 42; `build_registry` signature near line 735; registration after the `offer_location_share` block near line 1710)
- Test: `tests/test_find_nearest_dealers.py`

**Interfaces:**
- Consumes: `DealerDirectory.nearest(point)`, `.centres`, `DealerStoresUnavailable`, `store_card`, `StoreCards.put`, `FAR_KM` (Task 2); `PincodeDirectory.load().lookup`; `evidence_check.CONTACT_ENV` (`"EMOTORAD_CUSTOMER_CARE_CONTACT"`); `utc_now_iso` from `..conversation`.
- Produces:
  - `mocks.FIND_NEAREST_DEALERS = "find_nearest_dealers"`
  - `build_registry(..., dealers: Optional[Any] = None, store_cards: Optional[Any] = None)`
  - Tool outcomes: `ok` data `{"outcome": "ok", "area", "far", "stores": [{"store_ref", "store_name", "locality", "distance_km"}], "note"}` (plus `"care_contact"` when `far` and the contact is set); `ok` data `{"outcome": "no_area", "action": {"kind": "request_location", "label": "Share my location"}}`; errors `bad_pincode`, `oms_unavailable`.

- [ ] **Step 1: Write the failing tests**

`tests/test_find_nearest_dealers.py`:

```python
"""find_nearest_dealers through the registry (spec 2026-10-09, section 4)."""

import json
import os
import unittest
from unittest import mock

from emotorad_ai.agents import dealer_orders
from emotorad_ai.geo import PincodeCentres
from emotorad_ai.tools.dealer_stores import FIXTURE_ROWS, DealerDirectory, StoreCards
from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error

CENTRES = PincodeCentres({"411014": (18.56, 73.91), "411001": (18.52, 73.86), "400054": (19.06, 72.84),
                          "110016": (28.55, 77.20), "560001": (12.97, 77.59)})
AREA = {"pincode": "411001", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def registry(load=None, cards=None):
    directory = DealerDirectory(load or (lambda: [dict(r) for r in FIXTURE_ROWS]), CENTRES)
    return build_registry(dealers=directory, store_cards=cards if cards is not None else StoreCards()), cards


def call(reg, arguments=None, area=None):
    late = {"area": (lambda: area)} if area is not None else {}
    return reg.call(FIND_NEAREST_DEALERS, arguments or {}, ToolContext(conversation_id="c1", late=late))


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_directory(self):
        self.assertNotIn(FIND_NEAREST_DEALERS, build_registry().specs)

    def test_never_offered_to_the_dealer_persona(self):
        self.assertNotIn(FIND_NEAREST_DEALERS, dealer_orders.TOOL_NAMES)

    def test_the_model_cannot_supply_the_area(self):
        reg, _ = registry()
        self.assertEqual(set(reg.specs[FIND_NEAREST_DEALERS].parameters), {"pincode"})
        self.assertIn("area", reg.specs[FIND_NEAREST_DEALERS].optional_injects)

    def test_an_extra_argument_is_rejected(self):
        reg, _ = registry()
        envelope = call(reg, {"pincode": "411001", "phone": "9999999999"}, AREA)
        self.assertTrue(is_error(envelope))


class OutcomeTests(unittest.TestCase):
    def test_ok_gives_the_model_names_and_distances_and_code_the_cards(self):
        cards = StoreCards()
        reg, _ = registry(cards=cards)
        envelope = call(reg, area=AREA)
        data = envelope["data"]
        self.assertEqual(data["outcome"], "ok")
        self.assertEqual([s["store_ref"] for s in data["stores"]], ["D1", "D2", "D3"])
        self.assertEqual(data["stores"][0]["store_name"], "Test Cycles Pune")
        self.assertEqual(data["stores"][0]["locality"], "Pune, Maharashtra")
        self.assertFalse(data["far"])
        seen = json.dumps(envelope)
        for hidden in ("9000000001", "Test Manager One", "Shop 1, Test Road"):
            self.assertNotIn(hidden, seen)
        taken = cards.take("c1")
        self.assertEqual([c["ref"] for c in taken], ["D1", "D2", "D3"])
        self.assertEqual((taken[0]["manager_name"], taken[0]["phone"]), ("Test Manager One", "+91 9000000001"))

    def test_a_typed_pincode_wins_and_is_returned_as_the_area(self):
        reg, _ = registry()
        data = call(reg, {"pincode": " 400054 "}, AREA)["data"]
        self.assertEqual(data["stores"][0]["store_name"], "Test Cycles Mumbai")
        self.assertEqual((data["area"]["pincode"], data["area"]["source"]), ("400054", "typed"))

    def test_no_area_asks_with_the_location_button(self):
        reg, _ = registry()
        data = call(reg)["data"]
        self.assertEqual(data, {"outcome": "no_area",
                                "action": {"kind": "request_location", "label": "Share my location"}})

    def test_a_pincode_india_post_does_not_know_is_bad_pincode(self):
        reg, _ = registry()
        for typed in ("999999", "12345", "abcdef", "011001"):
            with self.subTest(typed=typed):
                envelope = call(reg, {"pincode": typed}, AREA)
                self.assertEqual(envelope["error"]["code"], "bad_pincode")

    def test_unreadable_stores_are_oms_unavailable_with_the_care_contact(self):
        def down():
            raise OSError("down")

        reg, _ = registry(load=down)
        with mock.patch.dict(os.environ, {"EMOTORAD_CUSTOMER_CARE_CONTACT": "1800 000 0000"}):
            envelope = call(reg, area=AREA)
        self.assertEqual(envelope["error"]["code"], "oms_unavailable")
        self.assertIn("1800 000 0000", envelope["error"]["message"])

    def test_far_when_the_nearest_is_over_100_km(self):
        reg, _ = registry()
        far_area = dict(AREA, pincode="560001", district="Bengaluru", state="Karnataka")
        with mock.patch.dict(os.environ, {"EMOTORAD_CUSTOMER_CARE_CONTACT": "1800 000 0000"}):
            data = call(reg, area=far_area)["data"]
        self.assertTrue(data["far"])
        self.assertEqual(data["care_contact"], "1800 000 0000")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_find_nearest_dealers -v`
Expected: FAIL with `ImportError: cannot import name 'FIND_NEAREST_DEALERS'`.

- [ ] **Step 3: Register the tool in `src/emotorad_ai/tools/mocks.py`**

Next to the other tool-name constants (near `FIND_SERVICE_SLOTS`, line 42):

```python
FIND_NEAREST_DEALERS = "find_nearest_dealers"
```

At the end of `build_registry`'s parameters (after `location_sharing`, before the closing parenthesis), add:

```python
    # The OMS dealer stores (tools/dealer_stores.DealerDirectory) and the side
    # channel their cards reach the reply by (StoreCards). Absent unless
    # supplied, so an agent that cannot look stores up is never told it can.
    dealers: Optional[Any] = None,
    store_cards: Optional[Any] = None,
```

After the `if location_sharing:` block (the one ending with `offer_location_share`), add:

```python
    if dealers is not None:
        from ..address import PincodeDirectory
        from ..conversation import utc_now_iso
        from ..evidence_check import CONTACT_ENV
        from .dealer_stores import FAR_KM, DealerStoresUnavailable, store_card

        pincodes = PincodeDirectory.load()

        @registry.register(
            FIND_NEAREST_DEALERS,
            "The three EMotorad dealer stores nearest the customer. Call it only when the customer should "
            "take the bike to a dealer: the issue is still unclear after your questions, or a fault you "
            "verified is in warranty and its part must be fitted at a dealer. Pass `pincode` only if the "
            "customer typed one in this chat; otherwise their area is supplied by the platform. The stores' "
            "addresses, managers and phone numbers are shown to the customer below your reply: never write "
            "a phone number or a street address yourself.",
            parameters={"pincode": {"type": "string",
                                    "description": "A six-digit pin code the customer typed in this chat."}},
            required=(),
            injects=("conversation_id",),
            optional_injects=("area",),
        )
        def find_nearest_dealers(conversation_id: str, area: Optional[Dict[str, Any]] = None,
                                 pincode: Optional[str] = None) -> Dict[str, Any]:
            contact = (os.environ.get(CONTACT_ENV) or "").strip()
            typed = "".join(str(pincode or "").split())
            if typed:
                places = pincodes.lookup(typed) if len(typed) == 6 and typed.isdigit() and typed[0] != "0" else []
                if not places or dealers.centres.centre(typed) is None:
                    raise ToolError("bad_pincode", "%s is not a pin code India Post delivers to. Ask the customer "
                                                   "for their six-digit pin code." % typed[:12])
                area = {"pincode": typed, "district": places[0].district, "state": places[0].state,
                        "source": "typed", "at": utc_now_iso()}
            centre = dealers.centres.centre((area or {}).get("pincode"))
            if centre is None:
                return ok({"outcome": "no_area",
                           "action": {"kind": "request_location", "label": "Share my location"}})
            try:
                nearest = dealers.nearest(centre)
            except DealerStoresUnavailable:
                nearest = []
            if not nearest:
                raise ToolError("oms_unavailable", "Dealer stores cannot be looked up right now."
                                + (" The customer can call customer care on %s." % contact if contact else ""),
                                retryable=True)
            cards = [store_card("D%d" % (n + 1), store, km) for n, (store, km) in enumerate(nearest)]
            if store_cards is not None:
                store_cards.put(conversation_id, cards)
            far = nearest[0][1] > FAR_KM
            data: Dict[str, Any] = {
                "outcome": "ok",
                "area": dict(area),
                "far": far,
                "stores": [{"store_ref": card["ref"], "store_name": card["name"],
                            "locality": ", ".join(x for x in (store.district, store.state) if x),
                            "distance_km": card["distance_km"]}
                           for card, (store, _km) in zip(cards, nearest)],
                "note": "Their addresses, managers and phone numbers are shown to the customer below your reply.",
            }
            if far and contact:
                data["care_contact"] = contact
            return ok(data, freshness_seconds=600)
```

(`os`, `Dict`, `Any`, `Optional`, `ok` and `ToolError` are already imported in `mocks.py`; add `import os` at the top if it is not.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_find_nearest_dealers -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/tools/mocks.py tests/test_find_nearest_dealers.py
git commit -m "feat(dealers): find_nearest_dealers, names and distances to the model, cards to code"
```

---

### Task 5: The runtime, the agents and Reply.stores

**Files:**
- Modify: `src/emotorad_ai/contract.py` (`Reply`)
- Modify: `src/emotorad_ai/runtime.py` (`__init__`, `_handle`, the `facts` dict at line 2490, the agent `Reply` at line 2768)
- Modify: `src/emotorad_ai/agents/base.py` (`DEALER_RULE`, appended in `Agent.run`)
- Modify: `src/emotorad_ai/agents/battery_support.py`, `motor_support.py`, `narrow_support.py` (`TOOL_NAMES`)
- Test: `tests/test_nearest_dealers_conversation.py`

**Interfaces:**
- Consumes: `FIND_NEAREST_DEALERS`, `build_registry(dealers=, store_cards=)` (Task 4); `StoreCards`, `DealerDirectory`, `FIXTURE_ROWS` (Task 2); `ConversationState.area` (Task 3).
- Produces:
  - `Reply.stores: List[Dict[str, Any]] = []`
  - `Runtime(..., store_cards: Optional[Any] = None)`
  - `agents.base.DEALER_RULE: str`, `agents.base.DEALER_TOOL = "find_nearest_dealers"`

- [ ] **Step 1: Write the failing conversation test**

`tests/test_nearest_dealers_conversation.py`:

```python
"""The nearest dealers through runtime.handle() (spec 2026-10-09, sections 4 to 6)."""

import json
import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import DEALER_RULE, DEALER_TOOL
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.agents import battery_support, motor_support, narrow_support
from emotorad_ai.config import Settings
from emotorad_ai.geo import PincodeCentres
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.dealer_stores import FIXTURE_ROWS, DealerDirectory, StoreCards
from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS, build_registry

CENTRES = PincodeCentres({"411014": (18.56, 73.91), "411001": (18.52, 73.86), "400054": (19.06, 72.84),
                          "110016": (28.55, 77.20)})
AREA = {"pincode": "411001", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def runtime_with(responses):
    cards = StoreCards()
    directory = DealerDirectory(lambda: [dict(r) for r in FIXTURE_ROWS], CENTRES)
    registry = build_registry(today=date(2026, 10, 9), dealers=directory, store_cards=cards)
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True,
                      store_cards=cards)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm, cards


def send(runtime, adapter, text, area=None):
    runtime.conversations.get("conv-d").route_to(AGENT_NAME)
    message = adapter.to_message({"conversation_id": "conv-d", "session_token": "sess-ananya", "text": text})
    if area is not None:
        message = replace(message, entry_metadata=dict(message.entry_metadata, area=area))
    return runtime.handle(message)


class AgentSliceTests(unittest.TestCase):
    def test_the_rules_tool_name_is_the_registrys(self):
        self.assertEqual(DEALER_TOOL, FIND_NEAREST_DEALERS)

    def test_the_three_customer_agents_have_the_tool(self):
        for module in (battery_support, motor_support, narrow_support):
            self.assertIn(FIND_NEAREST_DEALERS, module.TOOL_NAMES)

    def test_the_rule_is_given_only_with_the_tool(self):
        runtime, adapter, llm, _ = runtime_with([say("Hello.")])
        send(runtime, adapter, "hi", area=AREA)
        self.assertIn(DEALER_RULE.strip(), llm.requests[0]["system"])
        plain = build_registry(today=date(2026, 10, 9))
        llm2 = ScriptedClaude([say("Hello.")])
        runtime2 = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=plain, llm=llm2,
                           log=EventLog(path=None), resolver=IdentityResolver(plain), self_service_identity=True)
        adapter2 = WebsiteChatAdapter(runtime2.resolver)
        runtime2.conversations.get("conv-d").route_to(AGENT_NAME)
        runtime2.handle(adapter2.to_message({"conversation_id": "conv-d", "session_token": "sess-ananya", "text": "hi"}))
        self.assertNotIn(DEALER_RULE.strip(), llm2.requests[0]["system"])


class CardsTests(unittest.TestCase):
    def test_three_cards_on_the_reply_and_no_phone_in_the_models_context(self):
        runtime, adapter, llm, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {}),
            say("The nearest dealer is Test Cycles Pune in Pune. Their details are below."),
        ])
        reply = send(runtime, adapter, "it still won't start, where can I take it?", area=AREA)
        self.assertEqual([s["ref"] for s in reply.stores], ["D1", "D2", "D3"])
        self.assertEqual((reply.stores[0]["name"], reply.stores[0]["phone"]), ("Test Cycles Pune", "+91 9000000001"))
        context = json.dumps(llm.requests, default=str)
        self.assertNotIn("9000000001", context)
        self.assertNotIn("Test Manager One", context)

    def test_cards_never_carry_into_the_next_turn(self):
        runtime, adapter, _, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {}), say("Details below."), say("Anything else?"),
        ])
        self.assertEqual(len(send(runtime, adapter, "where can I take it?", area=AREA).stores), 3)
        self.assertEqual(send(runtime, adapter, "thanks").stores, [])

    def test_leftover_cards_from_a_replaced_reply_are_dropped(self):
        runtime, adapter, _, cards = runtime_with([say("Anything else?")])
        cards.put("conv-d", [{"ref": "D1"}])  # as if a guardrail replaced the turn that found them
        self.assertEqual(send(runtime, adapter, "thanks").stores, [])

    def test_no_area_puts_the_location_button_on_the_reply(self):
        runtime, adapter, _, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {}), say("What's your pin code? Or tap the button to share your location."),
        ])
        reply = send(runtime, adapter, "where can I take it?")
        self.assertEqual(reply.stores, [])
        self.assertIn({"kind": "request_location", "label": "Share my location"}, reply.actions)

    def test_a_typed_pincode_becomes_the_chats_area(self):
        runtime, adapter, _, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {"pincode": "400054"}), say("Details below."),
        ])
        reply = send(runtime, adapter, "I'm at 400054 this week", area=AREA)
        self.assertEqual(reply.stores[0]["name"], "Test Cycles Mumbai")
        area = runtime.conversations.get("conv-d").area
        self.assertEqual((area["pincode"], area["source"]), ("400054", "typed"))

    def test_a_safety_report_never_carries_cards(self):
        runtime, adapter, llm, cards = runtime_with([])
        cards.put("conv-d", [{"ref": "D1"}])
        reply = send(runtime, adapter, "my battery is swelling and smoking", area=AREA)
        self.assertEqual(reply.stores, [])
        self.assertEqual(llm.requests, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_nearest_dealers_conversation -v`
Expected: FAIL with `ImportError: cannot import name 'DEALER_RULE'`.

- [ ] **Step 3: Add `Reply.stores` in `src/emotorad_ai/contract.py`**

After `actions` in `Reply`:

```python
    # Dealer store cards code found (spec 2026-10-09): each {"ref", "name",
    # "address", "pincode", "manager_name", "phone", "distance_km"}, nearest
    # first. The surface decides how many to show. Never written by the model.
    stores: List[Dict[str, Any]] = field(default_factory=list)
```

- [ ] **Step 4: Add `DEALER_RULE` in `src/emotorad_ai/agents/base.py`**

After `INVOICE_RULE`:

```python
DEALER_RULE = """

Nearest dealers:
- Call find_nearest_dealers only when the customer should take the bike to a dealer: the issue is still unclear after your questions, or a fault you verified is in warranty and its part has to be fitted at a dealer (raise the ticket first and quote its reference).
- The stores' addresses, managers and phone numbers are shown to the customer below your reply. Name at most the nearest store and its distance. Never write a phone number or a street address.
- If it answers no_area, ask for their pin code; a button to share their location is shown. If it answers bad_pincode, ask for the pin code again.
- If `far` is true, say the nearest dealer is over 100 km away, and give the care_contact if there is one.
"""
```

In `Agent.run`, in the `if self.definition.one_step:` block, insert before `system += ONE_STEP_RULE`:

```python
            if DEALER_TOOL in self.definition.tool_names and DEALER_TOOL in self.registry.specs:
                system += DEALER_RULE
```

where `DEALER_TOOL` is a module constant defined next to `GUIDE_MEDIA_TOOL` (line 71), as that one is, so `agents/base.py` does not import `tools/mocks.py`:

```python
DEALER_TOOL = "find_nearest_dealers"  # tools.mocks.FIND_NEAREST_DEALERS
```

- [ ] **Step 5: Give the three agents the tool**

Append `FIND_NEAREST_DEALERS` to `TOOL_NAMES` in `src/emotorad_ai/agents/battery_support.py`, `motor_support.py` and `narrow_support.py`, importing it from `..tools.mocks` next to the other names they import.

- [ ] **Step 6: Wire the runtime (`src/emotorad_ai/runtime.py`)**

1. `__init__`: add the keyword parameter `store_cards: Any = None,` after `invoice: Any = None,`, and in the body after `self.invoice = invoice`:

```python
        # The dealer store cards a turn found (tools/dealer_stores.StoreCards,
        # spec 2026-10-09): put by find_nearest_dealers, taken for the reply.
        self.store_cards = store_cards
```

2. `_handle`: directly after `turn_mark = len(self.log.events)`:

```python
        if self.store_cards is not None:
            # Cards left by a turn whose reply never carried them (a guardrail
            # replaced it) must not reach this one.
            self.store_cards.take(cid)
```

3. In the `facts={...}` dict passed to `agent.run` (line 2490), add:

```python
                # The customer's area, for find_nearest_dealers (spec 2026-10-09).
                "area": lambda: state.area,
```

4. Add a method near `_finish`:

```python
    def _dealer_cards(self, message: InboundMessage, turn: Any, state: ConversationState) -> List[Dict[str, Any]]:
        """The store cards this turn found, and the area a typed pin code
        gave (spec 2026-10-09, sections 3 and 4)."""
        for call in turn.tool_calls:
            if call["tool"] != FIND_NEAREST_DEALERS or is_error(call["result"]):
                continue
            area = (call["result"].get("data") or {}).get("area")
            if isinstance(area, dict) and area.get("source") == "typed":
                state.area = dict(area)
        return self.store_cards.take(message.conversation_id) if self.store_cards is not None else []
```

adding `FIND_NEAREST_DEALERS` to the existing `from .tools.mocks import (...)` list (line 157). `is_error` is already imported from `.tools.registry` (line 169).

5. In the agent `Reply(...)` at line 2768, add `stores=self._dealer_cards(message, turn, state),` after `actions=list(turn.actions),`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_nearest_dealers_conversation tests.test_area -v`
Expected: all PASS.

- [ ] **Step 8: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -5`
Expected: `OK` (or only the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text` failure if ffmpeg speech is unavailable). Any other failure is fixed before the commit: a test that compares an agent's whole system prompt or tool list gains the new rule or tool name.

- [ ] **Step 9: Commit**

```bash
git add src/emotorad_ai/contract.py src/emotorad_ai/runtime.py src/emotorad_ai/agents/base.py src/emotorad_ai/agents/battery_support.py src/emotorad_ai/agents/motor_support.py src/emotorad_ai/agents/narrow_support.py tests/test_nearest_dealers_conversation.py
git commit -m "feat(dealers): the battery, motor and narrow agents find the nearest dealers; cards on the reply"
```

---

### Task 6: Cards to the app, the history and the website

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`TranscriptTurn.stores`, `transcript_turns`)
- Modify: `src/emotorad_ai/stores/mongo.py` (`record_turn`, every `TranscriptTurn(` it builds)
- Modify: `src/emotorad_ai/amiigo/history.py` (`message_view`)
- Modify: `src/emotorad_ai/amiigo/socket.py` (`receipt_of`, `_unrecorded_turn`)
- Modify: `src/emotorad_ai/api.py` (`MessageOut`, `_message_out`)
- Modify: `web/emotorad-support-chat-dev.html` (`renderAiTurnHtml`, the reply push, a `.store-card` style)
- Test: `tests/test_store_cards_delivery.py`

**Interfaces:**
- Consumes: `Reply.stores` (Task 5).
- Produces: `TranscriptTurn.stores: Tuple[Dict[str, Any], ...] = ()`; the bot message object gains `"stores"` when non-empty (live `reply` frame and `GET …/messages`); `MessageOut.stores: List[dict]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_store_cards_delivery.py`:

```python
"""Store cards reach the transcript, the Amiigo message and the website (spec 2026-10-09, section 5)."""

import unittest

import mongomock

from emotorad_ai.amiigo import history
from emotorad_ai.contract import Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationState, InMemoryConversationStore, TranscriptTurn, transcript_turns
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes

CARD = {"ref": "D1", "name": "Test Cycles Pune", "address": "Shop 1, Test Road, Pune, Maharashtra - 411014",
        "pincode": "411014", "manager_name": "Test Manager One", "phone": "+91 9000000001", "distance_km": 3}


def turn_pair():
    state = ConversationState(conversation_id="c1", turns=1)
    inbound = InboundMessage(conversation_id="c1", persona="customer", identity=Identity(),
                             channel="website_chat", message_text="where can I take it?")
    reply = Reply(conversation_id="c1", text="Details below.", handled_by="battery_support", stores=[CARD])
    return state, inbound, reply


class TranscriptTests(unittest.TestCase):
    def test_the_bot_turn_keeps_the_cards_and_the_customer_turn_none(self):
        state, inbound, reply = turn_pair()
        customer, bot = transcript_turns(state, inbound, reply, "2026-10-09T10:00:00+00:00")
        self.assertEqual(customer.stores, ())
        self.assertEqual(bot.stores, (CARD,))

    def test_kept_and_read_back_by_both_stores(self):
        for store in (InMemoryConversationStore(), MongoConversationStore(self._db())):
            with self.subTest(store=type(store).__name__):
                state, inbound, reply = turn_pair()
                store.record_turn(state, inbound, reply)
                self.assertEqual(store.transcript("c1")[1].stores, (CARD,))

    def _db(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        return db


class HistoryTests(unittest.TestCase):
    def test_the_bot_message_carries_its_cards_and_others_carry_none(self):
        signer = lambda url: (url, None)
        bot = TranscriptTurn(n=2, role="bot", text="Details below.", at="2026-10-09T10:00:00+00:00",
                             stores=(CARD,), conversation_id="c1")
        plain = TranscriptTurn(n=1, role="customer", text="hi", at="2026-10-09T10:00:00+00:00", conversation_id="c1")
        self.assertEqual(history.message_view(bot, signer)["stores"], [CARD])
        self.assertNotIn("stores", history.message_view(plain, signer))


class WebsiteTests(unittest.TestCase):
    def test_message_out_carries_the_cards(self):
        from tests.test_prepare_turn import fresh_api

        api = fresh_api()
        out = api._message_out("c1", Reply(conversation_id="c1", text="Details below.", handled_by="x", stores=[CARD]))
        self.assertEqual(out.stores, [CARD])

    def test_the_page_renders_store_cards(self):
        from pathlib import Path

        page = (Path(__file__).resolve().parents[1] / "web" / "emotorad-support-chat-dev.html").read_text()
        self.assertIn("turn.stores", page)
        self.assertIn("store-card", page)
        self.assertIn("stores: reply.stores", page)


if __name__ == "__main__":
    unittest.main()
```

Add to `tests/test_amiigo_socket.py`, next to `test_the_reply_carries_actions_escalation_and_the_ticket_it_raised`:

```python
    def test_the_reply_message_carries_the_store_cards(self):
        card = {"ref": "D1", "name": "Test Cycles Pune", "address": "Shop 1, Test Road, Pune, Maharashtra - 411014",
                "pincode": "411014", "manager_name": "Test Manager One", "phone": "+91 9000000001", "distance_km": 3}
        self.turns.reply_fields = {"stores": [card]}
        with self.socket() as ws:
            _, _, reply = self.exchange(ws, message())
        self.assertEqual(reply["message"]["stores"], [card])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_store_cards_delivery tests.test_amiigo_socket -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'stores'` and `KeyError: 'stores'`.

- [ ] **Step 3: The transcript (`src/emotorad_ai/conversation.py`)**

In `TranscriptTurn`, after `path: str = ""`:

```python
    # The dealer store cards the bot's reply carried (spec 2026-10-09).
    stores: Tuple[Dict[str, Any], ...] = ()
```

In `transcript_turns`, add to the bot turn: `stores=tuple(dict(s) for s in reply.stores),`.

- [ ] **Step 4: MongoDB (`src/emotorad_ai/stores/mongo.py`)**

In `record_turn`, change the doc line to:

```python
            doc = dict(asdict(turn), conversation_id=state.conversation_id, user_key=state.user_key,
                       attachments=[dict(a) for a in turn.attachments], stores=[dict(s) for s in turn.stores])
```

In every `TranscriptTurn(` built from a stored document in this file (find them with `grep -n "TranscriptTurn(" src/emotorad_ai/stores/mongo.py`), add `stores=tuple(d.get("stores") or ()),`.

- [ ] **Step 5: The Amiigo message (`src/emotorad_ai/amiigo/history.py` and `socket.py`)**

In `history.message_view`, replace the `if isinstance(item, TranscriptTurn): return {...}` block with:

```python
    if isinstance(item, TranscriptTurn):
        view = {
            "id": turn_id(item),
            "sender": "rider" if item.role == "customer" else "bot",
            "text": item.text,
            "sent_at": _shown(item.at),
            "attachments": [_attachment_view(a, signer) for a in item.attachments],
        }
        if item.stores:
            # Dealer store cards (spec 2026-10-09): additive within v1.
            view["stores"] = [dict(s) for s in item.stores]
        return view
```

In `socket.receipt_of`, add `stores=[dict(s) for s in reply.stores],` to the `answer = dict(live, ...)` call. In `_unrecorded_turn`, add `stores=tuple(kept.get("stores") or ()),` to the `TranscriptTurn(...)` it returns.

- [ ] **Step 6: The website (`src/emotorad_ai/api.py` and the page)**

In `MessageOut`, after `actions`:

```python
    # Dealer store cards code found (spec 2026-10-09), nearest first.
    stores: List[dict] = []
```

In `_message_out`, add `stores=[dict(s) for s in reply.stores],` after `actions=list(reply.actions),`.

In `web/emotorad-support-chat-dev.html`, in `renderAiTurnHtml`, after the `(turn.actions || []).forEach(...)` block, add:

```javascript
  // Dealer stores code found (spec 2026-10-09). The model never writes these.
  (turn.stores || []).forEach(function (s) {
    var tel = String(s.phone || "").replace(/\s+/g, "");
    html += '<div class="msg-row ai"><div class="store-card">' +
      '<div class="store-name">' + esc(s.name) + ' <span class="store-km">' + esc(String(s.distance_km)) + ' km</span></div>' +
      '<div class="store-address">' + esc(s.address) + '</div>' +
      '<div class="store-manager">' + esc(s.manager_name) + ' · <a href="tel:' + esc(tel) + '">' +
      esc(s.phone) + '</a></div></div></div>';
  });
```

In the reply push (`state.turns.push({ isUser: false, ...`), add `stores: reply.stores || [],` after `actions: reply.actions || [],`.

In the page's `<style>` block, after the `.action-chip` rules, add:

```css
.store-card { max-width: 320px; border: 1px solid rgba(127,127,127,.28); border-radius: 12px; padding: 10px 12px; display: grid; gap: 4px; font-size: 13px; }
.store-name { font-weight: 600; }
.store-km { font-weight: 400; opacity: .7; margin-left: 4px; }
.store-address { opacity: .85; }
.store-manager a { color: var(--accent); text-decoration: none; user-select: text; }
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_store_cards_delivery tests.test_amiigo_socket tests.test_amiigo_history -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/stores/mongo.py src/emotorad_ai/amiigo/history.py src/emotorad_ai/amiigo/socket.py src/emotorad_ai/api.py web/emotorad-support-chat-dev.html tests/test_store_cards_delivery.py tests/test_amiigo_socket.py
git commit -m "feat(dealers): store cards in the transcript, the Amiigo message, its history and the website chat"
```

---

### Task 7: Wiring, health and documents

**Files:**
- Modify: `src/emotorad_ai/api.py` (after `OMS_DB` at line 281; `_build_registry` return; `Runtime(...)` at line 383; `/health`)
- Create: `docs/contracts/amiigo-support-chat-stores.md`
- Modify: `docs/runbooks/config-store.md` (section 9)
- Modify: `CLAUDE.md` (Rules distilled from the build log)
- Test: `tests/test_api_dealers.py`

**Interfaces:**
- Consumes: `dealer_stores.directory_from_env`, `StoreCards` (Task 2); `build_registry(dealers=, store_cards=)` (Task 4); `Runtime(store_cards=)` (Task 5); `PINCODE_CENTRES` (Task 3).
- Produces: `api.DEALERS`, `api.DEALER_SOURCE`, `api.STORE_CARDS`; `/health` key `dealer_stores` (`"oms_db"` or `"fixtures"`).

- [ ] **Step 1: Write the failing test**

`tests/test_api_dealers.py`:

```python
"""The API wires the dealer stores (spec 2026-10-09)."""

import unittest

from fastapi.testclient import TestClient

from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS
from tests.test_prepare_turn import fresh_api


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api()

    def test_the_tool_is_registered_and_the_runtime_takes_its_cards(self):
        self.assertIn(FIND_NEAREST_DEALERS, self.api.registry.specs)
        self.assertIs(self.api.runtime.store_cards, self.api.STORE_CARDS)

    def test_health_names_the_source(self):
        body = TestClient(self.api.app).get("/health").json()
        self.assertEqual(body["dealer_stores"], "fixtures")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_api_dealers -v`
Expected: FAIL with `AssertionError: 'find_nearest_dealers' not found` or `AttributeError: ... 'STORE_CARDS'`.

- [ ] **Step 3: Wire `src/emotorad_ai/api.py`**

Import `from .tools import dealer_stores as dealer_stores_tools`. Directly after the `PINCODE_CENTRES = PincodeCentres.load()` line Task 3 added (after `OMS_DB`, line 281), add:

```python
# The dealer stores nearest a customer (spec 2026-10-09): OMS's Dealers,
# read through the same connection setting as the bikes, else three made-up
# stores. Their cards reach the reply beside the model (StoreCards).
DEALERS, DEALER_SOURCE = dealer_stores_tools.directory_from_env(
    PINCODE_CENTRES, log=lambda event, fields: log.emit(event, "dealer_stores", **fields))
STORE_CARDS = dealer_stores_tools.StoreCards()
```

In `_build_registry`'s `return build_registry(...)`, add `dealers=DEALERS, store_cards=STORE_CARDS,`. In `runtime = Runtime(...)`, add `store_cards=STORE_CARDS,`. In the `/health` response dict, after `"warranty_source": WARRANTY_SOURCE,`, add `"dealer_stores": DEALER_SOURCE,`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_api_dealers -v`
Expected: PASS.

- [ ] **Step 5: Write `docs/contracts/amiigo-support-chat-stores.md`**

```markdown
# Amiigo Support Chat: dealer store cards

9 October 2026 · from the server team · addendum to v1. Adds one optional key to the bot's message object. Nothing is removed or renamed.

## What changes

When the bot sends the rider to a dealer, the bot's message carries `stores`: the three EMotorad dealer stores nearest the rider's pin code, nearest first. The app decides how many to show. The bot's text says the stores are listed below and never writes a phone number or an address itself.

`stores` appears on the bot message in the live `reply` frame (`message.stores`) and in `GET …/messages`, so a reopened chat redraws the cards. A message without stores has no `stores` key.

## The field

```json
"stores": [
  {"ref": "D1", "name": "Test Cycles Pune", "address": "Shop 1, Test Road, Viman Nagar, Pune, Maharashtra - 411014",
   "pincode": "411014", "manager_name": "Test Manager One", "phone": "+91 9000000001", "distance_km": 3}
]
```

| Field | Type | Meaning |
| --- | --- | --- |
| `ref` | string | `D1`, `D2`, `D3` in order. The bot may say "the first store". |
| `name` | string | The store's name |
| `address` | string | The full address, pin code included |
| `pincode` | string | The store's pin code |
| `manager_name` | string | The store manager, or the store's contact person |
| `phone` | string | `+91` and ten digits where the number has ten, else as stored |
| `distance_km` | number | Straight line from the rider's pin code to the store's, whole kilometres |

## How to show a card

Name and distance on the first line, the address below, then the manager's name and a call button on the phone. Keep the order. Show at least one.

## Where the rider's area comes from

The location the app sends (the addendum of 7 October 2026), turned into a pin code at once, or a pin code the rider types. No coordinates are kept. With neither, the bot asks for a pin code and sends the `request_location` action, as in v1.
```

- [ ] **Step 6: Update `docs/runbooks/config-store.md` section 9**

After the three `GRANT SELECT` lines in step 1 of section 9, add:

```markdown
   The nearest-dealers tool (spec 2026-10-09) reads the dealer stores with the same connection setting,
   so the role also needs:
   ```sql
   GRANT SELECT (id, customer_name, address, address2, pin_code_id, district_id, state_id, franchise_type_id,
                 poc_name, mobile, is_active, deleted_at, is_distributor) ON em_franchise TO <role>;
   GRANT SELECT (id, franchise_type_name) ON em_franchise_type TO <role>;
   GRANT SELECT (id, pin_code) ON em_pin_code TO <role>;
   GRANT SELECT (id, district_name) ON em_district TO <role>;
   GRANT SELECT (id, state_name) ON em_state TO <role>;
   GRANT SELECT (full_name, mobile, user_type, related_id, is_active, deleted_at, updated_at) ON em_users TO <role>;
   ```
   `/health` shows `"dealer_stores":"oms_db"` once the setting is in; without it, three made-up stores.
   Before real customers: Sachin's yes to giving riders the dealer managers' mobile numbers.
```

- [ ] **Step 7: Add the rule to `CLAUDE.md`**

Under "Rules distilled from the build log", after the "Bikes and warranty from OMS production" entry, add:

```markdown
- **The nearest dealers** (spec 2026-10-09, `docs/superpowers/specs/2026-10-09-nearest-dealers-design.md`): `find_nearest_dealers`, offered to the battery, motor and narrow agents only when the registry has a dealer directory, returns the three OMS **Dealers** stores (active, not distributors) nearest the customer's pin code, by straight line between pin-code centres (`geo.py`, `src/emotorad_ai/data/pincode_centres.csv`, built by `scripts/build_pincode_centres.py` from India Post's open directory; never hand-edited). The contact is the store's `franchise_manager` login, else its `poc_name` and `mobile`. The model sees store names, localities and distances only; the cards (`ref`, name, address, pin code, manager, phone, distance) go to code through `StoreCards` and onto `Reply.stores`, the Amiigo message's `stores` (and its history) and the website's cards (contract `docs/contracts/amiigo-support-chat-stores.md`). The customer's area is `ConversationState.area`: a location becomes the nearest pin code at once (`location.CentresGeocoder`, no third party) and only the pin code, district and state are kept; a typed pin code wins. A location with words keeps the words; a bad location on the socket is ignored and logged as `location_ignored`. Stores are cached ten minutes, a failure serves the last list for an hour, else `oms_unavailable` with the customer care contact. `/health` shows `dealer_stores`. Sachin signs off before riders are given managers' numbers.
```

- [ ] **Step 8: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -5`
Expected: `OK` (or only the known speech-to-text failure where ffmpeg speech is unavailable). Note the test count for the PR.

- [ ] **Step 9: Commit**

```bash
git add src/emotorad_ai/api.py docs/contracts/amiigo-support-chat-stores.md docs/runbooks/config-store.md CLAUDE.md tests/test_api_dealers.py
git commit -m "feat(dealers): wired in the API with /health, the app contract addendum, the runbook grants"
```
