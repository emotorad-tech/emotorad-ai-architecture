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
