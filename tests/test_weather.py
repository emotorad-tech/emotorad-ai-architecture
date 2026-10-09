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


class NoDataTests(unittest.TestCase):
    """The final review's I2: nothing usable is unavailable, never an ok of zeros."""

    def test_an_all_null_body_is_unavailable_and_not_cached(self):
        raw = b'{"daily": {"temperature_2m_max": [null, null], "temperature_2m_min": [null, null]}, "current": null}'
        transport = Transport([(200, raw), (200, body())])
        client = OpenMeteoClient(KEY, fetch=transport, clock=Clock())
        with self.assertRaises(WeatherUnavailable) as caught:
            client.recent("411014", POINT)
        self.assertEqual(str(caught.exception), "no_data")

    def test_nan_infinity_and_huge_numbers_are_dropped_not_fatal(self):
        raw = (b'{"current": {"temperature_2m": NaN}, "daily": {"temperature_2m_max": [NaN, 1e400, 30.0, '
               + b"9" * 400 + b'], "temperature_2m_min": [20.0, 20.0, 20.0, 20.0]}}')
        report = parse(raw)
        self.assertIsNone(report.current_c)
        self.assertEqual([d["max_c"] for d in report.days], [30.0])
        self.assertEqual(summary(report, AREA)["highest_c"], 30)


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
