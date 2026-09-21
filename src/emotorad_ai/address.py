"""A delivery address the code can check, and the pincode directory behind it.

The order tool used to take one free-text line and require a pincode in it.
On 2026-09-21 the model shipped "A1102, Park View City 1, 122018": no city, no
state, and nothing to refuse it. This module is the Zomato shape instead. The
customer gives what only they know (house or flat, building or street, area,
an optional landmark, the pincode); the pincode gives the district and state
from India Post's directory; code assembles the line the customer hears read
back and the order ships to.

The directory is `knowledge/_replacement/pincodes.csv`, one row per pincode,
reduced from the data.gov.in "All India Pincode Directory". A few hundred
pincodes span two states; those are returned as several places and the tool
asks the customer to pick, from that set.
"""

from __future__ import annotations

import csv
import pathlib
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

PINCODE_DIRECTORY_PATH = "_replacement/pincodes.csv"

# Six digits, first non-zero: the shape of every Indian pincode.
_PINCODE = re.compile(r"^[1-9]\d{5}$")

REQUIRED_FIELDS = ("house_or_flat", "building_or_street", "area", "pincode")
OPTIONAL_FIELDS = ("landmark",)


class AddressError(Exception):
    """A refused address. `code` is the tool error code; `message` is for the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Place:
    district: str
    state: str


@dataclass(frozen=True)
class Address:
    house_or_flat: str
    building_or_street: str
    area: str
    landmark: Optional[str]
    pincode: str

    def customer_fields(self) -> Dict[str, str]:
        """The parts the customer typed, by name. City and state are not here
        because the customer never types them."""
        fields = {
            "house_or_flat": self.house_or_flat,
            "building_or_street": self.building_or_street,
            "area": self.area,
            "pincode": self.pincode,
        }
        if self.landmark:
            fields["landmark"] = self.landmark
        return fields


def parse_address(raw: Mapping[str, Any]) -> Address:
    """Validate the model's structured address. Refuses rather than guesses."""
    cleaned: Dict[str, str] = {}
    for name in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        value = raw.get(name)
        cleaned[name] = str(value).strip() if value is not None else ""
    missing = [name for name in REQUIRED_FIELDS if not cleaned[name]]
    if missing:
        raise AddressError(
            "address_incomplete",
            "The address is missing: %s. Ask the customer for it and pass every field."
            % ", ".join(missing),
        )
    if not _PINCODE.match(cleaned["pincode"]):
        raise AddressError(
            "pincode_invalid",
            "%r is not a six-digit pincode. Ask the customer to check it." % cleaned["pincode"],
        )
    return Address(
        house_or_flat=cleaned["house_or_flat"],
        building_or_street=cleaned["building_or_street"],
        area=cleaned["area"],
        landmark=cleaned["landmark"] or None,
        pincode=cleaned["pincode"],
    )


def assemble(address: Address, place: Place) -> str:
    """The postal line: what is read back to the customer and what ships."""
    parts = [address.house_or_flat, address.building_or_street, address.area]
    if address.landmark:
        parts.append(address.landmark)
    parts += [place.district, place.state, address.pincode]
    return ", ".join(parts)


class PincodeDirectory:
    """pincode -> places, from the file, loaded once per process."""

    _shared: Optional["PincodeDirectory"] = None
    _lock = threading.Lock()

    def __init__(self, places: Dict[str, List[Place]]) -> None:
        self._places = places

    @classmethod
    def load(cls, directory: Optional[Any] = None) -> "PincodeDirectory":
        if directory is not None:
            return cls._read(pathlib.Path(directory) / PINCODE_DIRECTORY_PATH)
        with cls._lock:
            if cls._shared is None:
                root = pathlib.Path(__file__).resolve().parents[2] / "knowledge"
                cls._shared = cls._read(root / PINCODE_DIRECTORY_PATH)
            return cls._shared

    @classmethod
    def _read(cls, path: pathlib.Path) -> "PincodeDirectory":
        places: Dict[str, List[Place]] = {}
        with path.open(newline="") as handle:
            rows = csv.reader(line for line in handle if not line.startswith("#"))
            header = next(rows, None)
            if header != ["pincode", "places"]:
                raise ValueError("%s: expected columns pincode,places; got %r" % (path, header))
            for pincode, joined in rows:
                places[pincode] = [
                    Place(*entry.split("|", 1)) for entry in joined.split(";") if "|" in entry
                ]
        return cls(places)

    def lookup(self, pincode: str) -> List[Place]:
        return list(self._places.get(pincode, ()))

    def __len__(self) -> int:
        return len(self._places)
