"""Recent weather at the rider's area, from Open-Meteo (spec 2026-10-09).

One request per pin code, at the pin code's centre (geo.py), never the
rider's own position: the current temperature and the last 14 days' daily
highs and lows, in Indian time. Kept an hour per pin code; after a failure,
lookups fail at once for a minute.

The request URL carries the paid plan's key, so neither the URL nor any
exception text that might hold it ever leaves this module: errors carry a
short reason only (the exception class, http_<status>, too_large,
bad_response, breaker_open).

The model reads `summary`, which labels each day by how many days ago it
was, never by date, so the date post-check never has a weather date to judge.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

Point = Tuple[float, float]

KEY_ENV = "EMOTORAD_OPEN_METEO_API_KEY"
BASE_URL_ENV = "EMOTORAD_OPEN_METEO_BASE_URL"
DEFAULT_BASE_URL = "https://customer-api.open-meteo.com"
PAST_DAYS = 14
TIMEOUT_SECONDS = 3.0
MAX_BYTES = 262144
CACHE_SECONDS = 3600
FAILURE_TTL_SECONDS = 60
# The charging band in knowledge/battery/wont-charge.yaml.
HOT_C = 40
COLD_C = 10


class WeatherUnavailable(Exception):
    """The weather could not be read. The message is a short reason only."""


@dataclass(frozen=True)
class WeatherReport:
    current_c: Optional[float]
    # {"days_ago", "max_c", "min_c"}, today first.
    days: Tuple[Dict[str, Any], ...]


def _urllib_fetch(url: str, timeout: float, limit: int) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "emotorad-ai/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse(raw: bytes) -> WeatherReport:
    try:
        data = json.loads(raw.decode("utf-8"))
        daily = data["daily"]
        highs, lows = list(daily["temperature_2m_max"]), list(daily["temperature_2m_min"])
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        raise WeatherUnavailable("bad_response") from None
    if len(highs) != len(lows):
        raise WeatherUnavailable("bad_response")
    days: List[Dict[str, Any]] = []
    for index in range(len(highs) - 1, -1, -1):
        high, low = _number(highs[index]), _number(lows[index])
        if high is None or low is None:
            continue
        days.append({"days_ago": len(highs) - 1 - index, "max_c": high, "min_c": low})
    current = data.get("current") if isinstance(data, dict) else None
    current_c = _number(current.get("temperature_2m")) if isinstance(current, dict) else None
    return WeatherReport(current_c=current_c, days=tuple(days))


class OpenMeteoClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 fetch: Optional[Callable[[str, float, int], Tuple[int, bytes]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self._fetch = fetch or _urllib_fetch
        self._clock = clock
        self._cache: Dict[str, Tuple[float, WeatherReport]] = {}
        self._failed_at: Optional[float] = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "OpenMeteoClient(base_url=%r, key=set)" % self.base_url

    def _url(self, point: Point) -> str:
        query = urllib.parse.urlencode({
            "latitude": "%.2f" % point[0], "longitude": "%.2f" % point[1],
            "current": "temperature_2m", "daily": "temperature_2m_max,temperature_2m_min",
            "past_days": str(PAST_DAYS), "forecast_days": "1", "timezone": "Asia/Kolkata",
            "apikey": self._key,
        })
        return "%s/v1/forecast?%s" % (self.base_url, query)

    def recent(self, pincode: str, point: Point) -> WeatherReport:
        now = self._clock()
        with self._lock:
            if self._failed_at is not None and now - self._failed_at < FAILURE_TTL_SECONDS:
                raise WeatherUnavailable("breaker_open")
            cached = self._cache.get(pincode)
            if cached and now - cached[0] < CACHE_SECONDS:
                return cached[1]
        try:
            try:
                status, raw = self._fetch(self._url(point), TIMEOUT_SECONDS, MAX_BYTES)
            except Exception as exc:  # never its text: it can hold the URL and the key
                raise WeatherUnavailable(type(exc).__name__) from None
            if status != 200:
                raise WeatherUnavailable("http_%d" % status)
            if len(raw) > MAX_BYTES:
                raise WeatherUnavailable("too_large")
            report = parse(raw)
        except WeatherUnavailable:
            with self._lock:
                self._failed_at = now
            raise
        with self._lock:
            self._failed_at = None
            self._cache[pincode] = (now, report)
        return report


def _whole(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value))


def _mean(values: List[float]) -> Optional[int]:
    return _whole(sum(values) / len(values)) if values else None


def summary(report: WeatherReport, area: Mapping[str, Any]) -> Dict[str, Any]:
    """What the model reads: whole degrees, days by how long ago, no dates."""
    highs = [d["max_c"] for d in report.days]
    lows = [d["min_c"] for d in report.days]
    return {
        "area": {key: area.get(key) for key in ("pincode", "district", "state", "source")},
        "current_c": _whole(report.current_c),
        "highest_c": _whole(max(highs)) if highs else None,
        "lowest_c": _whole(min(lows)) if lows else None,
        "days_above_40c": sum(1 for h in highs if h > HOT_C),
        "days_below_10c": sum(1 for low in lows if low < COLD_C),
        "last_7_days_avg_high_c": _mean([d["max_c"] for d in report.days if d["days_ago"] <= 6]),
        "previous_7_days_avg_high_c": _mean([d["max_c"] for d in report.days if 7 <= d["days_ago"] <= 13]),
        "days": [{"days_ago": d["days_ago"], "max_c": _whole(d["max_c"]), "min_c": _whole(d["min_c"])}
                 for d in report.days],
    }


def client_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OpenMeteoClient]:
    env = os.environ if environ is None else environ
    key = (env.get(KEY_ENV) or "").strip()
    if not key:
        return None
    return OpenMeteoClient(key, base_url=(env.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL)
