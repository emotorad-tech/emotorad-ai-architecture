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
