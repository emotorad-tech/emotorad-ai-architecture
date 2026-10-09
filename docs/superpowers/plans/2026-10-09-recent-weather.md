# Recent Weather Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The battery and narrow agents can look up the last 14 days' temperatures at the rider's pin code from Open-Meteo, state them, and ask the rider once, instead of asking the rider for the temperature.

**Architecture:** `weather.py` holds an Open-Meteo client (one request per pin code, cached an hour, a one-minute breaker, the key never in an error) and a `summary` the model reads (no calendar dates). A read-only tool, `get_recent_weather`, registered only when the key is set, uses the chat's area (or a pin code the rider typed) and the pin-code centres from `geo.py`. A prompt rule tells the agents when to call it and how to confirm with the rider. The API builds the client from the environment and `/health` names it.

**Tech Stack:** Python 3 stdlib (`urllib`, `json`, `threading`), stdlib `unittest`, the existing registry and runtime.

**Spec:** `docs/superpowers/specs/2026-10-09-recent-weather-design.md`

## Global Constraints

- Key: `EMOTORAD_OPEN_METEO_API_KEY`. Base URL: `EMOTORAD_OPEN_METEO_BASE_URL`, default `https://customer-api.open-meteo.com`. Without the key the tool is not registered and `/health` says `"weather": "not configured"`; with it, `"weather": "open-meteo"`.
- Request: `GET {base}/v1/forecast` with `latitude`, `longitude` (the pin-code centre, `%.2f`), `current=temperature_2m`, `daily=temperature_2m_max,temperature_2m_min`, `past_days=14`, `forecast_days=1`, `timezone=Asia/Kolkata`, `apikey`.
- Timeout 3 seconds; body read up to 256 KB (`MAX_BYTES = 262144`); cache 1 hour per pin code (`CACHE_SECONDS = 3600`); breaker 60 seconds (`FAILURE_TTL_SECONDS = 60`).
- Errors carry an exception class, `http_<status>`, `too_large`, `bad_response` or `breaker_open`: never the URL, never the key.
- Summary fields: `area` (`pincode`, `district`, `state`, `source`), `current_c`, `highest_c`, `lowest_c`, `days_above_40c` (daily high over 40), `days_below_10c` (daily low under 10), `last_7_days_avg_high_c` (days 0 to 6), `previous_7_days_avg_high_c` (days 7 to 13), `days` (`[{"days_ago", "max_c", "min_c"}]`, today first). Whole degrees. No calendar date anywhere.
- Tool `get_recent_weather`: optional `pincode` accepted only if the rider typed it in this chat (the dealer tool's rule); injects `conversation_id`; optional injects `area`, `customer_messages`. Outcomes `ok`, `no_area` (an `ok` result with the `request_location` action), errors `bad_pincode`, `weather_unavailable`.
- Offered to the customer persona's battery and narrow agents only. Never the dealer persona.
- Never edit `knowledge/` (the knowledge freeze). Never write a regex through a shell heredoc. British English, no em dashes.
- A Claude session never reads, prints or writes the key's value.
- `python3 -m unittest discover -s tests -t .` stays green (the known `tests.test_video` speech-to-text failure aside).

## Review Focus

1. Open-Meteo answers 200 with a body missing `daily` or `current`, or with `null` values: the tool must say `weather_unavailable` or skip the rows, never crash or give the model `None` temperatures as numbers. Pinned in Task 1.
2. The URL with the key must never reach an exception message, a log event or the model, including when `urlopen` raises an error whose text holds the URL. Pinned in Task 1.
3. A pin code the model invents (not in the rider's messages) must be refused, as for the dealer tool. Pinned in Task 2.
4. A rider with no area and no typed pin code must get the location button, not a guess. Pinned in Task 2.
5. Weather days must never carry calendar dates into the model's context, so the date post-check never blocks a weather reply. Pinned in Tasks 1 and 3.

---

### Task 1: The Open-Meteo client and the summary

**Files:**
- Create: `src/emotorad_ai/weather.py`
- Test: `tests/test_weather.py`

**Interfaces:**
- Consumes: `geo.Point` (`Tuple[float, float]`).
- Produces:
  - `weather.KEY_ENV = "EMOTORAD_OPEN_METEO_API_KEY"`, `BASE_URL_ENV = "EMOTORAD_OPEN_METEO_BASE_URL"`, `DEFAULT_BASE_URL = "https://customer-api.open-meteo.com"`, `CACHE_SECONDS`, `FAILURE_TTL_SECONDS`, `MAX_BYTES`, `TIMEOUT_SECONDS = 3.0`, `PAST_DAYS = 14`
  - `class WeatherUnavailable(Exception)` (message is a reason only)
  - `@dataclass(frozen=True) class WeatherReport: current_c: Optional[float]; days: Tuple[Dict[str, Any], ...]`
  - `class OpenMeteoClient(api_key: str, base_url: str = DEFAULT_BASE_URL, fetch: Optional[Callable[[str, float, int], Tuple[int, bytes]]] = None, clock: Callable[[], float] = time.monotonic)` with `.recent(pincode: str, point: Point) -> WeatherReport`
  - `parse(body: bytes) -> WeatherReport` (raises `WeatherUnavailable("bad_response")`)
  - `summary(report: WeatherReport, area: Mapping[str, Any]) -> Dict[str, Any]`
  - `client_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OpenMeteoClient]`

- [ ] **Step 1: Write the failing tests**

`tests/test_weather.py`:

```python
"""Recent weather from Open-Meteo (spec 2026-10-09, sections 2 and 3)."""

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from emotorad_ai import weather
from emotorad_ai.weather import OpenMeteoClient, WeatherReport, WeatherUnavailable, client_from_env, parse, summary

KEY = "test-key-not-real"
POINT = (18.5612, 73.9138)
AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def body(current=31.4, highs=None, lows=None, drop=()):
    highs = list(highs) if highs is not None else [30.0 + (i % 3) for i in range(15)]
    lows = list(lows) if lows is not None else [20.0] * 15
    data = {"current": {"time": "2026-10-09T10:00", "temperature_2m": current},
            "daily": {"time": ["2026-09-%02d" % (25 + i) if i < 6 else "2026-10-%02d" % (i - 5) for i in range(15)],
                      "temperature_2m_max": highs, "temperature_2m_min": lows}}
    for key in drop:
        data.pop(key)
    return json.dumps(data).encode()


class Transport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.urls = []

    def __call__(self, url, timeout, limit):
        self.urls.append((url, timeout, limit))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RequestTests(unittest.TestCase):
    def test_the_exact_query(self):
        transport = Transport([(200, body())])
        OpenMeteoClient(KEY, fetch=transport).recent("411014", POINT)
        [(url, timeout, limit)] = transport.urls
        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "customer-api.open-meteo.com", "/v1/forecast"))
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.assertEqual(query, {"latitude": "18.56", "longitude": "73.91", "current": "temperature_2m",
                                 "daily": "temperature_2m_max,temperature_2m_min", "past_days": "14",
                                 "forecast_days": "1", "timezone": "Asia/Kolkata", "apikey": KEY})
        self.assertEqual((timeout, limit), (weather.TIMEOUT_SECONDS, weather.MAX_BYTES))

    def test_the_key_is_never_in_an_error(self):
        cases = [OSError("GET https://customer-api.open-meteo.com/v1/forecast?apikey=%s failed" % KEY),
                 (403, b'{"error": true, "reason": "apikey %s invalid"}' % KEY.encode()),
                 (200, b"x" * (weather.MAX_BYTES + 1)),
                 (200, b"not json %s" % KEY.encode())]
        for answer in cases:
            with self.subTest(answer=str(answer)[:40]):
                client = OpenMeteoClient(KEY, fetch=Transport([answer]))
                with self.assertRaises(WeatherUnavailable) as caught:
                    client.recent("411014", POINT)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn("apikey", str(caught.exception))

    def test_the_reasons(self):
        for answer, reason in ((OSError("x"), "OSError"), ((503, b""), "http_503"),
                               ((200, b"x" * (weather.MAX_BYTES + 1)), "too_large"), ((200, b"{}"), "bad_response")):
            with self.subTest(reason=reason):
                with self.assertRaises(WeatherUnavailable) as caught:
                    OpenMeteoClient(KEY, fetch=Transport([answer])).recent("411014", POINT)
                self.assertEqual(str(caught.exception), reason)

    def test_the_client_hides_its_key(self):
        self.assertNotIn(KEY, repr(OpenMeteoClient(KEY)))


class CacheTests(unittest.TestCase):
    def test_one_request_per_pincode_an_hour(self):
        transport, clock = Transport([(200, body()), (200, body()), (200, body())]), Clock()
        client = OpenMeteoClient(KEY, fetch=transport, clock=clock)
        client.recent("411014", POINT)
        client.recent("411014", POINT)
        client.recent("400054", (19.06, 72.84))
        self.assertEqual(len(transport.urls), 2)
        clock.now += weather.CACHE_SECONDS + 1
        client.recent("411014", POINT)
        self.assertEqual(len(transport.urls), 3)

    def test_after_a_failure_a_minute_of_breaker(self):
        transport, clock = Transport([OSError("down"), (200, body())]), Clock()
        client = OpenMeteoClient(KEY, fetch=transport, clock=clock)
        with self.assertRaises(WeatherUnavailable):
            client.recent("411014", POINT)
        with self.assertRaises(WeatherUnavailable) as caught:
            client.recent("400054", (19.06, 72.84))
        self.assertEqual(str(caught.exception), "breaker_open")
        self.assertEqual(len(transport.urls), 1)
        clock.now += weather.FAILURE_TTL_SECONDS + 1
        self.assertEqual(len(client.recent("411014", POINT).days), 15)


class ParseTests(unittest.TestCase):
    def test_today_last_in_the_source_is_days_ago_zero(self):
        report = parse(body(highs=[float(i) for i in range(15)]))
        self.assertEqual(report.days[0], {"days_ago": 0, "max_c": 14.0, "min_c": 20.0})
        self.assertEqual(report.days[-1]["days_ago"], 14)
        self.assertEqual(report.current_c, 31.4)

    def test_rows_with_a_missing_value_are_skipped(self):
        highs = [30.0] * 15
        highs[14] = None
        report = parse(body(highs=highs))
        self.assertEqual(len(report.days), 14)
        self.assertEqual(report.days[0]["days_ago"], 1)

    def test_a_body_without_daily_is_bad_response_and_without_current_is_kept(self):
        with self.assertRaises(WeatherUnavailable):
            parse(body(drop=("daily",)))
        self.assertIsNone(parse(body(drop=("current",))).current_c)


class SummaryTests(unittest.TestCase):
    def test_the_figures(self):
        highs = [41.6, 42.0, 39.0, 38.0, 37.0, 36.0, 35.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0]
        lows = [25.0] * 14 + [8.4]
        report = WeatherReport(current_c=39.6, days=tuple(
            {"days_ago": n, "max_c": highs[n], "min_c": lows[n]} for n in range(15)))
        s = summary(report, AREA)
        self.assertEqual(s["area"], {"pincode": "411014", "district": "Pune", "state": "Maharashtra",
                                     "source": "location"})
        self.assertEqual((s["current_c"], s["highest_c"], s["lowest_c"]), (40, 42, 8))
        self.assertEqual((s["days_above_40c"], s["days_below_10c"]), (2, 1))
        self.assertEqual((s["last_7_days_avg_high_c"], s["previous_7_days_avg_high_c"]), (38, 30))
        self.assertEqual(s["days"][0], {"days_ago": 0, "max_c": 42, "min_c": 25})

    def test_no_calendar_date_anywhere(self):
        s = summary(parse(body()), AREA)
        self.assertNotIn("2026", json.dumps(s))

    def test_no_rows_means_none_not_zero(self):
        s = summary(WeatherReport(current_c=None, days=()), AREA)
        self.assertEqual((s["current_c"], s["highest_c"], s["lowest_c"], s["last_7_days_avg_high_c"]),
                         (None, None, None, None))
        self.assertEqual((s["days_above_40c"], s["days_below_10c"]), (0, 0))


class EnvTests(unittest.TestCase):
    def test_no_key_no_client(self):
        self.assertIsNone(client_from_env({}))
        self.assertIsNone(client_from_env({weather.KEY_ENV: "  "}))

    def test_the_key_and_a_base_url(self):
        client = client_from_env({weather.KEY_ENV: KEY, weather.BASE_URL_ENV: "https://example.test/"})
        self.assertEqual(client.base_url, "https://example.test")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_weather -v`
Expected: FAIL with `ImportError: cannot import name 'weather'`.

- [ ] **Step 3: Write `src/emotorad_ai/weather.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_weather -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/weather.py tests/test_weather.py
git commit -m "feat(weather): an Open-Meteo client for the last 14 days at a pin code, and the model's summary"
```

---

### Task 2: The tool `get_recent_weather`

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py` (constants next to `FIND_NEAREST_DEALERS`; a module-level `_typed_area`; `build_registry(..., weather=None)`; the dealer tool uses `_typed_area`; the new tool after the dealer tool)
- Test: `tests/test_get_recent_weather.py`

**Interfaces:**
- Consumes: `weather.WeatherUnavailable`, `weather.summary`, `weather.WeatherReport` (Task 1); `geo.PincodeCentres.load()`; `PincodeDirectory.load()`; `_TYPED_PINCODE`, `ascii_digits`.
- Produces:
  - `mocks.GET_RECENT_WEATHER = "get_recent_weather"`
  - `mocks._typed_area(pincode: Optional[str], customer_messages: Optional[List[str]], centres: Any) -> Optional[Dict[str, Any]]` (raises `ToolError("bad_pincode", ...)`; returns `{"pincode", "district", "state", "source": "typed", "at"}` or None when no pin code was passed)
  - `build_registry(..., weather: Optional[Any] = None)`: an object with `.recent(pincode, point) -> WeatherReport`
  - Tool data on `ok`: the `summary` dict plus `"outcome": "ok"`

- [ ] **Step 1: Write the failing tests**

`tests/test_get_recent_weather.py`:

```python
"""get_recent_weather through the registry (spec 2026-10-09 recent weather, section 4)."""

import json
import unittest

from emotorad_ai.agents import dealer_orders
from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS, GET_RECENT_WEATHER, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.weather import WeatherReport, WeatherUnavailable

AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}
REPORT = WeatherReport(current_c=39.6, days=tuple(
    {"days_ago": n, "max_c": 41.0 if n < 3 else 33.0, "min_c": 26.0} for n in range(15)))


class FakeWeather:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def recent(self, pincode, point):
        self.calls.append((pincode, point))
        if self.fail:
            raise WeatherUnavailable(self.fail)
        return REPORT


def call(reg, arguments=None, area=None, said=None):
    late = {}
    if area is not None:
        late["area"] = lambda: area
    if said is not None:
        late["customer_messages"] = lambda: list(said)
    return reg.call(GET_RECENT_WEATHER, arguments or {}, ToolContext(conversation_id="c1", late=late))


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_client(self):
        self.assertNotIn(GET_RECENT_WEATHER, build_registry().specs)

    def test_never_offered_to_the_dealer_persona(self):
        self.assertNotIn(GET_RECENT_WEATHER, dealer_orders.TOOL_NAMES)

    def test_the_model_supplies_only_a_pincode(self):
        spec = build_registry(weather=FakeWeather()).specs[GET_RECENT_WEATHER]
        self.assertEqual(set(spec.parameters), {"pincode"})
        self.assertEqual(set(spec.optional_injects), {"area", "customer_messages"})

    def test_an_extra_argument_is_rejected(self):
        self.assertTrue(is_error(call(build_registry(weather=FakeWeather()), {"city": "Pune"}, AREA)))


class OutcomeTests(unittest.TestCase):
    def test_ok_is_the_summary_at_the_areas_centre(self):
        client = FakeWeather()
        data = call(build_registry(weather=client), area=AREA)["data"]
        self.assertEqual(data["outcome"], "ok")
        self.assertEqual((data["highest_c"], data["days_above_40c"]), (41, 3))
        self.assertEqual(data["area"]["pincode"], "411014")
        [(pincode, point)] = client.calls
        self.assertEqual(pincode, "411014")
        self.assertEqual(len(point), 2)
        self.assertNotIn("2026", json.dumps(data))

    def test_no_area_asks_with_the_location_button(self):
        client = FakeWeather()
        data = call(build_registry(weather=client))["data"]
        self.assertEqual(data, {"outcome": "no_area",
                                "action": {"kind": "request_location", "label": "Share my location"}})
        self.assertEqual(client.calls, [])

    def test_a_typed_pincode_wins_and_comes_back_as_the_area(self):
        data = call(build_registry(weather=FakeWeather()), {"pincode": "400054"}, AREA,
                    said=["I'm at 400054 this week"])["data"]
        self.assertEqual((data["area"]["pincode"], data["area"]["source"]), ("400054", "typed"))

    def test_a_pincode_the_customer_never_typed_or_india_post_does_not_know_is_refused(self):
        reg = build_registry(weather=FakeWeather())
        for arguments, said in (({"pincode": "110016"}, ["I am in Pune"]), ({"pincode": "999999"}, ["999999"])):
            with self.subTest(arguments=arguments):
                self.assertEqual(call(reg, arguments, AREA, said)["error"]["code"], "bad_pincode")

    def test_a_failed_lookup_is_weather_unavailable_with_its_reason(self):
        envelope = call(build_registry(weather=FakeWeather(fail="http_503")), area=AREA)
        self.assertEqual(envelope["error"]["code"], "weather_unavailable")
        self.assertIn("http_503", envelope["error"]["message"])


class SharedRuleTests(unittest.TestCase):
    def test_the_dealer_tool_still_refuses_a_pincode_never_typed(self):
        from tests.test_find_nearest_dealers import AREA as DEALER_AREA, call as dealer_call, registry

        reg, _ = registry()
        envelope = dealer_call(reg, {"pincode": "110016"}, DEALER_AREA, said=["I am in Pune"])
        self.assertEqual(envelope["error"]["code"], "bad_pincode")
        self.assertIn(FIND_NEAREST_DEALERS, reg.specs)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_get_recent_weather -v`
Expected: FAIL with `ImportError: cannot import name 'GET_RECENT_WEATHER'`.

- [ ] **Step 3: Implement in `src/emotorad_ai/tools/mocks.py`**

1. Next to `FIND_NEAREST_DEALERS = "find_nearest_dealers"`:

```python
GET_RECENT_WEATHER = "get_recent_weather"
```

2. Add `utc_now_iso` to the module's `from ..conversation import ...` line, and after the `_TYPED_PINCODE = re.compile(...)` line add (edit the file directly, not through a shell heredoc):

```python
def _typed_area(pincode: Optional[str], customer_messages: Optional[List[str]],
                centres: Any) -> Optional[Dict[str, Any]]:
    """The area a pin code the model passed gives, or None when it passed
    none. The model may pass only a pin code the customer typed in this chat,
    never one it chose (the dealer tool's final review, 9 October 2026), and
    only one India Post knows and that has a centre. Raises bad_pincode."""
    typed = "".join(ascii_digits(str(pincode or "")).split())
    if not typed:
        return None
    said = {"".join(found.split()) for text in (customer_messages or ())
            for found in _TYPED_PINCODE.findall(ascii_digits(text))}
    if typed not in said:
        raise ToolError("bad_pincode", "The customer has not typed that pin code in this chat. Use "
                                       "their area, or ask them for their six-digit pin code.")
    places = PincodeDirectory.load().lookup(typed) if len(typed) == 6 and typed.isdigit() and typed[0] != "0" else []
    if not places or centres.centre(typed) is None:
        raise ToolError("bad_pincode", "%s is not a pin code India Post delivers to. Ask the customer "
                                       "for their six-digit pin code." % typed[:12])
    return {"pincode": typed, "district": places[0].district, "state": places[0].state,
            "source": "typed", "at": utc_now_iso()}
```

3. In the dealer tool, delete `from ..conversation import utc_now_iso` and `dealer_pincodes = PincodeDirectory.load()`, and replace the whole `typed = ...` through the `area = {...}` block (the `if typed:` branch) with:

```python
            typed_area = _typed_area(pincode, customer_messages, dealers.centres)
            if typed_area is not None:
                area = typed_area
```

4. Add the parameter to `build_registry` after `store_cards`:

```python
    # The Open-Meteo client (weather.OpenMeteoClient). Absent unless the key
    # is set, so no agent is told it can look the weather up.
    weather: Optional[Any] = None,
```

5. After the dealer tool block, add:

```python
    if weather is not None:
        from ..geo import PincodeCentres
        from ..weather import WeatherUnavailable, summary as weather_summary

        weather_centres = PincodeCentres.load()

        @registry.register(
            GET_RECENT_WEATHER,
            "The temperature now and over the last 14 days around the customer's area. Call it when the "
            "temperature matters (the battery will not charge, charges slowly, range has dropped, or "
            "storage) instead of asking the customer how hot or cold it is. Pass `pincode` only if the "
            "customer typed one in this chat; otherwise their area is supplied by the platform.",
            parameters={"pincode": {"type": "string",
                                    "description": "A six-digit pin code the customer typed in this chat."}},
            required=(),
            injects=("conversation_id",),
            optional_injects=("area", "customer_messages"),
        )
        def get_recent_weather(conversation_id: str, area: Optional[Dict[str, Any]] = None,
                               customer_messages: Optional[List[str]] = None,
                               pincode: Optional[str] = None) -> Dict[str, Any]:
            typed_area = _typed_area(pincode, customer_messages, weather_centres)
            if typed_area is not None:
                area = typed_area
            where = (area or {}).get("pincode")
            point = weather_centres.centre(where)
            if point is None:
                return ok({"outcome": "no_area",
                           "action": {"kind": "request_location", "label": "Share my location"}})
            try:
                report = weather.recent(where, point)
            except WeatherUnavailable as exc:
                raise ToolError("weather_unavailable", "The weather cannot be looked up right now (%s). Ask the "
                                                       "customer about the temperature instead." % exc,
                                retryable=True)
            return ok(dict(weather_summary(report, area), outcome="ok"), freshness_seconds=3600)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_get_recent_weather tests.test_find_nearest_dealers -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/tools/mocks.py tests/test_get_recent_weather.py
git commit -m "feat(weather): get_recent_weather, one shared rule for a typed pin code"
```

---

### Task 3: The agents and the runtime

**Files:**
- Modify: `src/emotorad_ai/agents/base.py` (`WEATHER_TOOL`, `WEATHER_RULE`, appended in `Agent.run`)
- Modify: `src/emotorad_ai/agents/battery_support.py`, `src/emotorad_ai/agents/narrow_support.py` (`TOOL_NAMES`)
- Modify: `src/emotorad_ai/runtime.py` (`_dealer_cards` keeps a typed area from either tool)
- Modify: `tests/test_narrow_support.py` (the pinned tool slice)
- Test: `tests/test_recent_weather_conversation.py`

**Interfaces:**
- Consumes: `GET_RECENT_WEATHER`, `build_registry(weather=)` (Task 2); `WeatherReport` (Task 1).
- Produces: `agents.base.WEATHER_TOOL = "get_recent_weather"`, `agents.base.WEATHER_RULE: str`; a typed pin code from `get_recent_weather` becomes `ConversationState.area`.

- [ ] **Step 1: Write the failing test**

`tests/test_recent_weather_conversation.py`:

```python
"""Recent weather through runtime.handle() (spec 2026-10-09 recent weather, sections 4 and 5)."""

import json
import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents import battery_support, motor_support, narrow_support
from emotorad_ai.agents.base import WEATHER_RULE, WEATHER_TOOL
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import GET_RECENT_WEATHER, build_registry
from emotorad_ai.weather import WeatherReport

AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}
REPORT = WeatherReport(current_c=39.6, days=tuple(
    {"days_ago": n, "max_c": 41.0 if n < 3 else 33.0, "min_c": 26.0} for n in range(15)))


class FakeWeather:
    def __init__(self):
        self.calls = []

    def recent(self, pincode, point):
        self.calls.append(pincode)
        return REPORT


def runtime_with(responses, weather=True):
    registry = build_registry(today=date(2026, 10, 9), weather=FakeWeather() if weather else None)
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm


def send(runtime, adapter, text, area=None):
    runtime.conversations.get("conv-w").route_to(AGENT_NAME)
    message = adapter.to_message({"conversation_id": "conv-w", "session_token": "sess-ananya", "text": text})
    if area is not None:
        message = replace(message, entry_metadata=dict(message.entry_metadata, area=area))
    return runtime.handle(message)


class SliceTests(unittest.TestCase):
    def test_the_battery_and_narrow_agents_have_it_and_the_motor_agent_not(self):
        self.assertEqual(WEATHER_TOOL, GET_RECENT_WEATHER)
        self.assertIn(GET_RECENT_WEATHER, battery_support.TOOL_NAMES)
        self.assertIn(GET_RECENT_WEATHER, narrow_support.TOOL_NAMES)
        self.assertNotIn(GET_RECENT_WEATHER, motor_support.TOOL_NAMES)

    def test_the_rule_comes_only_with_the_tool(self):
        runtime, adapter, llm = runtime_with([say("Hello.")])
        send(runtime, adapter, "hi", area=AREA)
        self.assertIn(WEATHER_RULE.strip(), llm.requests[0]["system"])
        runtime2, adapter2, llm2 = runtime_with([say("Hello.")], weather=False)
        send(runtime2, adapter2, "hi", area=AREA)
        self.assertNotIn(WEATHER_RULE.strip(), llm2.requests[0]["system"])


class ConversationTests(unittest.TestCase):
    def test_the_agent_states_the_weather_and_asks_once(self):
        runtime, adapter, llm = runtime_with([
            call_tool(GET_RECENT_WEATHER, {}),
            say("It has been up to 41 °C around Pune this week. Is it about that hot where you charge it?"),
        ])
        reply = send(runtime, adapter, "my battery won't charge", area=AREA)
        self.assertIn("41 °C", reply.text)
        self.assertFalse(reply.handled_by.startswith("guardrail"), reply.handled_by)
        result = json.dumps(llm.requests[1]["messages"][-1], default=str)
        self.assertIn("days_above_40c", result)
        self.assertNotIn("2026-", result)

    def test_a_typed_pincode_becomes_the_chats_area(self):
        runtime, adapter, _ = runtime_with([
            call_tool(GET_RECENT_WEATHER, {"pincode": "400054"}), say("Is it about that hot where you charge it?"),
        ])
        send(runtime, adapter, "I'm at 400054 this week, it won't charge", area=AREA)
        area = runtime.conversations.get("conv-w").area
        self.assertEqual((area["pincode"], area["source"]), ("400054", "typed"))
        self.assertTrue(area.get("at"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_recent_weather_conversation -v`
Expected: FAIL with `ImportError: cannot import name 'WEATHER_RULE'`.

- [ ] **Step 3: The rule in `src/emotorad_ai/agents/base.py`**

Next to `DEALER_TOOL = "find_nearest_dealers"  # tools.mocks.FIND_NEAREST_DEALERS`:

```python
WEATHER_TOOL = "get_recent_weather"  # tools.mocks.GET_RECENT_WEATHER
```

After `DEALER_RULE`:

```python
WEATHER_RULE = """

Recent weather:
- When the temperature matters (the battery will not charge, charges slowly, range has dropped, or storage), call get_recent_weather instead of asking the customer how hot or cold it is.
- Say what it found with their area in one short sentence, for example "It has been up to 41 °C around Pune this week", then ask once whether it is about that hot or cold where they charge or keep the bike.
- Their answer wins: if they charge indoors, in an air-conditioned room or a basement, go by what they say.
- Never blame a fault on the weather alone, and never use the weather to refuse or hold up a ticket.
- If it answers no_area, ask for their pin code; a button to share their location is shown. If it answers weather_unavailable, ask the customer about the temperature instead.
- Never give a calendar date for the weather; say "this week" or "a few days ago".
"""
```

In `Agent.run`, after the two `DEALER_TOOL` lines and before `system += ONE_STEP_RULE`:

```python
            if WEATHER_TOOL in self.definition.tool_names and WEATHER_TOOL in self.registry.specs:
                system += WEATHER_RULE
```

- [ ] **Step 4: Give the battery and narrow agents the tool**

In `src/emotorad_ai/agents/battery_support.py`, add `GET_RECENT_WEATHER,` to the `from ..tools.mocks import (...)` list and append `GET_RECENT_WEATHER,` to `TOOL_NAMES` after `FIND_NEAREST_DEALERS,`. In `src/emotorad_ai/agents/narrow_support.py`, add it to the import list and to the end of `TOOL_NAMES`. In `tests/test_narrow_support.py`, import `GET_RECENT_WEATHER` and add it to the set in `test_the_tool_slice_has_no_lookups_and_the_writes_the_flow_needs`.

- [ ] **Step 5: Keep a typed area from either tool (`src/emotorad_ai/runtime.py`)**

Add `GET_RECENT_WEATHER` to the `from .tools.mocks import (...)` list. In `_dealer_cards`, replace:

```python
        last = None
        for call in turn.tool_calls:
            if call["tool"] != FIND_NEAREST_DEALERS:
                continue
            last = call
            if is_error(call["result"]):
                continue
            area = (call["result"].get("data") or {}).get("area")
            if isinstance(area, dict) and area.get("source") == "typed":
                state.area = dict(area)
```

with:

```python
        last = None
        for call in turn.tool_calls:
            if call["tool"] not in (FIND_NEAREST_DEALERS, GET_RECENT_WEATHER):
                continue
            if call["tool"] == FIND_NEAREST_DEALERS:
                last = call
            if is_error(call["result"]):
                continue
            # A pin code the customer typed becomes the chat's area, from
            # either tool (spec 2026-10-09 recent weather, section 4). The
            # weather summary leaves out `at`, so it is stamped here.
            area = (call["result"].get("data") or {}).get("area")
            if isinstance(area, dict) and area.get("source") == "typed" and area.get("pincode"):
                state.area = dict(area)
                state.area.setdefault("at", utc_now_iso())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_recent_weather_conversation tests.test_nearest_dealers_conversation tests.test_narrow_support -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/agents/base.py src/emotorad_ai/agents/battery_support.py src/emotorad_ai/agents/narrow_support.py src/emotorad_ai/runtime.py tests/test_recent_weather_conversation.py tests/test_narrow_support.py
git commit -m "feat(weather): the battery and narrow agents look the weather up and ask the rider once"
```

---

### Task 4: Wiring, health and documents

**Files:**
- Modify: `src/emotorad_ai/api.py` (build the client; `build_registry(weather=...)`; `/health`)
- Modify: `tests/test_api_health.py` (the pinned body and its env)
- Modify: `docs/runbooks/config-store.md` (a new section 10)
- Modify: `CLAUDE.md` (Rules distilled from the build log)
- Test: `tests/test_api_weather.py`

**Interfaces:**
- Consumes: `weather.client_from_env`, `weather.KEY_ENV` (Task 1); `build_registry(weather=)` (Task 2).
- Produces: `api.WEATHER`; `/health` key `weather` (`"open-meteo"` or `"not configured"`).

- [ ] **Step 1: Write the failing test**

`tests/test_api_weather.py`:

```python
"""The API wires the weather (spec 2026-10-09 recent weather, section 7)."""

import unittest

from emotorad_ai.tools.mocks import GET_RECENT_WEATHER
from tests.test_api_health import fresh_api, zoho_blank


class WiringTests(unittest.TestCase):
    def tearDown(self):
        fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_OPEN_METEO_API_KEY": ""}, **zoho_blank()))

    def test_without_the_key_the_tool_is_absent(self):
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_OPEN_METEO_API_KEY": ""}, **zoho_blank()))
        self.assertNotIn(GET_RECENT_WEATHER, api.registry.specs)
        self.assertEqual(api.health()["weather"], "not configured")

    def test_with_the_key_the_tool_is_there(self):
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_OPEN_METEO_API_KEY": "test-key-not-real"},
                             **zoho_blank()))
        self.assertIn(GET_RECENT_WEATHER, api.registry.specs)
        self.assertEqual(api.health()["weather"], "open-meteo")
        self.assertNotIn("test-key-not-real", str(api.health()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_api_weather -v`
Expected: FAIL with `KeyError: 'weather'` or the tool missing.

- [ ] **Step 3: Wire `src/emotorad_ai/api.py`**

Import `from . import weather as weather_tools`. After the `STORE_CARDS = dealer_stores_tools.StoreCards()` line, add:

```python
# Recent weather at the rider's area (spec 2026-10-09): Open-Meteo, on with
# its key only. The key is never logged; the client hides it.
WEATHER = weather_tools.client_from_env()
```

In `_build_registry`'s `return build_registry(...)`, add `weather=WEATHER,` after `store_cards=STORE_CARDS,`. In the `/health` dict, after `"dealer_stores": DEALER_SOURCE,`, add:

```python
        "weather": "open-meteo" if WEATHER is not None else "not configured",
```

In `tests/test_api_health.py`, in `test_offline_reports_no_secret`, add `"EMOTORAD_OPEN_METEO_API_KEY": ""` to the env dict and `"weather": "not configured"` after `"dealer_stores": "fixtures"` in the expected body.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_api_weather tests.test_api_health -v`
Expected: all PASS.

- [ ] **Step 5: Add the runbook section to `docs/runbooks/config-store.md`**

Append at the end of the file:

```markdown
## 10. Recent weather from Open-Meteo

Spec: `docs/superpowers/specs/2026-10-09-recent-weather-design.md`. Off until the key is set.

1. **The key (a person, in AWS CloudShell, region `ap-south-1`).** Add `EMOTORAD_OPEN_METEO_API_KEY`
   to `/emotorad/stage/ai/app` with the same script pattern as section 9: it asks for the value
   without echoing it, so nothing is pasted into a command or a chat.
2. **Deploy** and check `/health`: `"weather":"open-meteo"`.
3. **Check** in a battery chat with a pin code known (location shared or typed): say the battery
   will not charge. The bot states the last two weeks' temperatures around that area and asks once
   whether it is that hot or cold where the bike is charged.

Only a pin-code centre (about 1 km) is sent to Open-Meteo, never the rider's position or anything
that names them. Rollback: remove the key and redeploy; the agents go back to asking the rider.
```

- [ ] **Step 6: Add the rule to `CLAUDE.md`**

Under "Rules distilled from the build log", after the "The nearest dealers" entry, add:

```markdown
- **Recent weather** (spec 2026-10-09, `docs/superpowers/specs/2026-10-09-recent-weather-design.md`): with `EMOTORAD_OPEN_METEO_API_KEY` set, `get_recent_weather` (battery and narrow agents only) reads Open-Meteo's forecast endpoint with `past_days=14` at the centre of the chat's pin code (`ConversationState.area`, or a pin code the rider typed, under the dealer tool's typed-pin-code rule, `tools/mocks._typed_area`), never the rider's position. `weather.py` caches an hour per pin code, breaks for a minute after a failure, and never puts the URL or the key into an error. The model gets `weather.summary`: whole degrees, days by `days_ago`, no calendar dates, the count of days over 40 °C and under 10 °C (the charging band in `wont-charge`). `agents/base.WEATHER_RULE`: call it instead of asking how hot or cold it is, state it with the area, ask once, the rider's answer wins, never blame a fault on the weather alone. `/health` shows `weather`. Rollback: remove the key.
```

- [ ] **Step 7: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -5`
Expected: `OK`, or only the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text` failure where ffmpeg speech is unavailable. Note the count.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/api.py tests/test_api_weather.py tests/test_api_health.py docs/runbooks/config-store.md CLAUDE.md
git commit -m "feat(weather): wired in the API with /health, the runbook section and the CLAUDE.md rule"
```
