"""Replacement fulfilment: the half the model cannot argue with.

Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.

When the bot is sure a part needs replacing, this module decides whether a
technician is needed, what the item code is, whether an order is already in
flight, whether the bot is "sure" in the code-checkable sense, and whether the
configured approval mode lets the bot approve. The model reaches all of it
through one tool and may only confirm the address with the customer.

Orders are recorded in a ledger (spec 2026-10-10 replacement orders) and sent to OMS by order_worker.py when the switch is on.
"""

from __future__ import annotations

import copy
import pathlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

PARTS_TABLE_PATH = "_replacement/parts.yaml"


class PartsTableError(Exception):
    """A malformed parts table. Raised at load, never at order time."""


@dataclass(frozen=True)
class PartRule:
    part: str
    technician: bool
    ask: bool = False


def load_parts_table(directory: Optional[Any] = None) -> Dict[str, PartRule]:
    """part -> rule, validated the way knowledge records and the media
    catalogue are: a bad file fails loudly here rather than becoming
    'no such part' in a customer's conversation."""
    import yaml

    root = pathlib.Path(directory) if directory else pathlib.Path(__file__).resolve().parents[2] / "knowledge"
    path = root / PARTS_TABLE_PATH
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise PartsTableError("%s: not found" % path) from exc
    if not isinstance(raw, Mapping):
        raise PartsTableError("%s: expected a mapping of part -> rule" % path)
    table: Dict[str, PartRule] = {}
    for part, rule in raw.items():
        where = "%s: %s" % (path.name, part)
        if not isinstance(rule, Mapping):
            raise PartsTableError("%s: each part must be a mapping" % where)
        technician = rule.get("technician")
        if not isinstance(technician, bool):
            raise PartsTableError("%s: technician must be true or false" % where)
        ask = rule.get("ask", False)
        if not isinstance(ask, bool):
            raise PartsTableError("%s: ask must be true or false" % where)
        table[str(part)] = PartRule(part=str(part), technician=technician, ask=ask)
    return table


# The warranty_terms component each orderable part is judged by (spec
# 2026-10-10 replacement orders, section 1, step 4): a charger by its own six
# months, never by the battery's twelve.
PART_COMPONENTS: Dict[str, str] = {"battery": "battery", "charger": "charger", "display": "display",
                                   "controller": "controller", "motor": "motor"}
# The evidence check's fault component each part belongs to (section 1, step 2).
PART_FAULT: Dict[str, str] = {"battery": "battery", "charger": "battery", "controller": "motor", "motor": "motor"}


def _model_name(product_name: str) -> str:
    """'X1 C Red-XX01EB0007/EM01AV01C19' -> 'X1 C'.

    Live records carry the colour and two codes after the model. The fixture
    records carry the model alone. Both have to resolve.
    """
    head = product_name.split("-", 1)[0].strip()
    words = head.split()
    # Drop a trailing colour word if there is one; models are one or two tokens.
    if len(words) > 2:
        words = words[:2]
    return " ".join(words)


# Replacement orders (spec 2026-10-10 replacement orders, section 3).
FIRST_ORDER_NUMBER = 1000001
OPEN_STATUSES = ("recorded", "queued", "sent", "failed")


def order_reference(number: int) -> str:
    return "RO-%07d" % number


def open_key(frame_number: str, part: str) -> str:
    """One open order per bike and part: the frame upper-cased, no spaces."""
    return "%s|%s" % (re.sub(r"\s+", "", frame_number or "").upper(), part)


class ProductIds:
    """(bike model, part) -> OMS product id (em_product.id) for afs_order_add.
    Empty until the product ids are agreed (spec section 6); an order with no
    product id is recorded and never sent."""

    def __init__(self, table: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._table = {model: dict(parts) for model, parts in (table or {}).items()}

    def resolve(self, product_name: Optional[str], part: str) -> Optional[str]:
        if not product_name:
            return None
        return self._table.get(_model_name(product_name), {}).get(part)

    def has_part(self, parts: Iterable[str]) -> bool:
        wanted = set(parts)
        return any(wanted & set(entries) for entries in self._table.values())


class ReplacementOrders:
    """The order ledger in this process's memory: tests, offline, and until
    mongo_setup.py has made the index (stores.mongo.MongoOrderLedger is the
    same contract). One open order per bike and part."""

    mode = "memory"

    def __init__(self) -> None:
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def next_reference(self) -> str:
        with self._lock:
            self._seq += 1
            return order_reference(FIRST_ORDER_NUMBER - 1 + self._seq)

    def insert(self, order: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        with self._lock:
            key = order.get("open_key")
            for existing in self._orders.values():
                if key and existing.get("open_key") == key:
                    return copy.deepcopy(existing), False
            self._orders[order["_id"]] = copy.deepcopy(order)
            return copy.deepcopy(order), True

    def open_order(self, frame_number: str, part: str) -> Optional[Dict[str, Any]]:
        key = open_key(frame_number, part)
        with self._lock:
            found = next((o for o in self._orders.values() if o.get("open_key") == key), None)
            return copy.deepcopy(found) if found else None

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._orders.get(reference)
            return copy.deepcopy(found) if found else None

    def due(self, now_iso: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            ready = [o for o in self._orders.values()
                     if o.get("status") == "queued" and (o.get("next_attempt_at") or "") <= now_iso]
            ready.sort(key=lambda o: o.get("next_attempt_at") or "")
            return [copy.deepcopy(o) for o in ready[:limit]]

    def claim(self, reference: str, now_iso: str, lease_until_iso: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._orders.get(reference)
            if found is None or found.get("status") != "queued":
                return None
            if found.get("lease_until") and found["lease_until"] > now_iso:
                return None
            found["lease_until"] = lease_until_iso
            return copy.deepcopy(found)

    def save(self, order: Dict[str, Any]) -> None:
        with self._lock:
            self._orders[order["_id"]] = copy.deepcopy(order)

    def cancel(self, reference: str) -> None:
        with self._lock:
            found = self._orders.get(reference)
            if found is not None:
                found["status"] = "cancelled"
                found.pop("open_key", None)
