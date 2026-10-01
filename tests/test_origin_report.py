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
