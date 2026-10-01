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
