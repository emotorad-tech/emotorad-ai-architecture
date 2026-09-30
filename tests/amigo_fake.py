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
