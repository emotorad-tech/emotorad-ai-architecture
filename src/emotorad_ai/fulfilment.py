"""Replacement fulfilment: the half the model cannot argue with.

Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.

When the bot is sure a part needs replacing, this module decides whether a
technician is needed, what the item code is, whether an order is already in
flight, whether the bot is "sure" in the code-checkable sense, and whether the
configured approval mode lets the bot approve. The model reaches all of it
through one tool and may only confirm the address with the customer.

Every write here is a mock. The OMS, the ERP and Razorpay are not called.
"""

from __future__ import annotations

import itertools
import pathlib
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

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
    except OSError:
        raise PartsTableError("%s: not found" % path)
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


# How long an order or an unpaid link counts as "in flight" for the duplicate
# check. Given once by the owner on 2026-09-20 and applied to both.
IN_FLIGHT_SECONDS = 48 * 60 * 60


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


class ItemCodes:
    """Bike model and part -> replacement item code.

    A mock in the exact shape the ERP read will have. The real one takes the
    record's product_id to the ERP Item, its BOM, and the component's item code,
    and is 8848's to expose. Until then this dictionary is the whole world, and
    a part it cannot resolve makes the bot "not sure" by definition.
    """

    _TABLE: Dict[str, Dict[str, str]] = {
        "EMX Plus": {"battery": "BAT-EMX-48V", "charger": "CHG-EMX-2A", "display": "DSP-EMX-LCD"},
        "X1 C": {"battery": "BAT-X1C-36V", "charger": "CHG-X1-2A", "display": "DSP-X1-LED"},
        "Doodle V3": {"battery": "BAT-DDL-36V", "charger": "CHG-DDL-2A"},
    }

    def resolve(self, product_name: Optional[str], part: str) -> Optional[str]:
        if not product_name:
            return None
        return self._TABLE.get(_model_name(product_name), {}).get(part)


@dataclass
class ReplacementOrders:
    """Stands in for the OMS order the bot will place on the customer's behalf.

    In-memory, like every other store here. Its one piece of logic is the
    duplicate check: an order for the same frame and part inside the window is
    reported back rather than placed again. That is idempotency, not suspicion.
    The chat page loses its conversation on reload, requests retry, and a
    customer asking "did that go through?" tomorrow must not get two batteries.
    """

    clock: Callable[[], float] = time.monotonic
    _orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _counter: Any = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, **payload: Any) -> Dict[str, Any]:
        with self._lock:
            order_id = "RO-%05d" % next(self._counter)
            order = dict(payload, order_id=order_id, placed_at=self.clock())
            order.setdefault("status", "pending_approval")
            self._orders[order_id] = order
            return dict(order)

    def approve(self, order_id: str) -> Dict[str, Any]:
        with self._lock:
            self._orders[order_id]["status"] = "approved"
            return dict(self._orders[order_id])

    def in_flight(self, frame_number: str, part: str) -> Optional[Dict[str, Any]]:
        cutoff = self.clock() - IN_FLIGHT_SECONDS
        with self._lock:
            for order in self._orders.values():
                if (
                    order.get("frame_number") == frame_number
                    and order.get("part") == part
                    and order.get("placed_at", 0) > cutoff
                    and order.get("status") in ("pending_approval", "approved")
                ):
                    return dict(order)
        return None
