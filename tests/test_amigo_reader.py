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
        r.bikes("+919700000031")
        r.service_status("+919700000031")
        r.recent_trips("+919700000031")
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
