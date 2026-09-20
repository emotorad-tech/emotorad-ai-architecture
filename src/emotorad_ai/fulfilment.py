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
