"""A shared location, turned into a pincode the directory knows.

The website chat offers "Share my location" the way delivery apps do. The
browser sends coordinates once; this module turns them into the pincode and
area the address flow starts from, and nothing else survives. Coordinates are
not stored, not logged and not shown to the model. Only the derived text
reaches the conversation, as the customer's own message, so the address
provenance check sees the pincode as typed by them.

The geocoder is a seam. `NominatimGeocoder` is OpenStreetMap's public service:
no key, no cost, fine for a LAN test server and explicitly not for production
volume (its usage policy is one request a second and a real User-Agent). The
production choice (MapmyIndia, Google) swaps in here without touching the
flow. Whatever the geocoder says, the postcode is trusted only if the India
Post directory knows it, and the district and state come from the directory,
never from the geocoder.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from .address import PincodeDirectory

log = logging.getLogger(__name__)

# Fields a reverse geocoder may use for the locality, most specific first.
_AREA_FIELDS = ("suburb", "neighbourhood", "quarter", "city_district", "village", "town")


class ReverseGeocoder(Protocol):
    def reverse(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        """Address fields for a point, in Nominatim's vocabulary, or None."""


@dataclass(frozen=True)
class LocationResult:
    pincode: str
    area: Optional[str]
    district: str
    state: str


def resolve_location(
    latitude: float,
    longitude: float,
    geocoder: ReverseGeocoder,
    directory: PincodeDirectory,
) -> Optional[LocationResult]:
    """The pincode and area for a point, or None when nothing trustworthy came back.

    None is a normal outcome, not an error: the customer is asked to type the
    pincode instead. A geocoder that is down, returns no postcode, or returns
    one India Post does not deliver to all land here.
    """
    try:
        fields = geocoder.reverse(latitude, longitude) or {}
    except Exception as exc:  # the geocoder is a third party; its failure is our fallback
        log.warning("reverse geocoding failed: %s: %s", type(exc).__name__, exc)
        return None
    postcode = str(fields.get("postcode") or "").strip().replace(" ", "")
    places = directory.lookup(postcode) if postcode else []
    if not places:
        return None
    area = next((str(fields[name]).strip() for name in _AREA_FIELDS if fields.get(name)), None)
    return LocationResult(pincode=postcode, area=area or None,
                          district=places[0].district, state=places[0].state)


def describe_location(result: Optional[LocationResult]) -> str:
    """The customer's message for a shared location. First person on purpose:
    it sits in the transcript as their turn, and every word in it is theirs to
    have given, which is what the address provenance check requires."""
    if result is None:
        return "I shared my location, but the pincode could not be worked out from it."
    parts = ["Pincode " + result.pincode]
    if result.area:
        parts.append(result.area)
    parts += [result.district, result.state]
    return "I shared my location. " + ", ".join(parts) + "."


class NominatimGeocoder:
    """OpenStreetMap's reverse geocoder. Test and LAN use only; see the module note."""

    URL = "https://nominatim.openstreetmap.org/reverse"

    def __init__(self, user_agent: str = "emotorad-ai-dev/0.1 (LAN test server)", timeout: float = 6.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def reverse(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"lat": "%.6f" % latitude, "lon": "%.6f" % longitude, "format": "jsonv2", "zoom": 18}
        )
        request = urllib.request.Request(
            "%s?%s" % (self.URL, query), headers={"User-Agent": self.user_agent}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        address = payload.get("address")
        return dict(address) if isinstance(address, dict) else None
