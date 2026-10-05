"""Ticket kinds, urgency, subject labels and the two reference shapes.

Kind is set in code by whoever records the ticket, never from the model's
category (spec section 3). Urgent tickets are taken first by the worker, are
late at ten minutes, are high priority, and are exempt from the credits floor
and the caps.
"""

from __future__ import annotations

import re
from typing import Optional

SUPPORT = "support"
SAFETY = "safety"
HANDOVER = "handover"
LOCKOUT = "lockout"
INTAKE = "intake"
WARRANTY_PROOF = "warranty_proof"
KINDS = (SUPPORT, SAFETY, HANDOVER, LOCKOUT, INTAKE, WARRANTY_PROOF)

# The model's category that makes a support ticket urgent.
SAFETY_CATEGORY = "battery_safety"

# Desk references start here. Seven digits, so one never matches an old mock
# number (EM-00001) and is never read as a six-digit one-time code.
FIRST_DESK_NUMBER = 1000001

# How long a record may wait from due_since before it is reported: an urgent
# one is late at ten minutes, any other is stuck at a day.
URGENT_LATE_SECONDS = 600
STUCK_SECONDS = 86400

# [0-9], not \d: in Python \d also matches Devanagari and other digits.
_DESK_REFERENCE = re.compile(r"EM-[0-9]{7,}")

_KIND_LABELS = {
    SAFETY: "SAFETY",
    HANDOVER: "Asked for a person",
    LOCKOUT: "Could not verify",
    INTAKE: "Unverified customer",
    WARRANTY_PROOF: "Late warranty registration",
}

_CATEGORY_LABELS = {
    "battery_charging": "Battery: charging",
    "battery_range": "Battery: range",
    "battery_power": "Battery: power",
    "battery_safety": "Battery: safety",
    "other": "Other",
}


def is_urgent(kind: str, category: Optional[str]) -> bool:
    return kind == SAFETY or category == SAFETY_CATEGORY


def is_desk_reference(ticket_id: Optional[str]) -> bool:
    """Whether the Desk record issued this id (EM-1000001 up), rather than
    the mock (EM-00001)."""
    return isinstance(ticket_id, str) and _DESK_REFERENCE.fullmatch(ticket_id) is not None


def desk_reference(number: int) -> str:
    return "EM-%d" % number


def subject_label(kind: str, category: Optional[str]) -> str:
    """The label in a Zoho subject. A support ticket's comes from its
    category; every other kind's is fixed, whatever the category says."""
    if kind not in KINDS:
        raise ValueError("unknown ticket kind: %r" % (kind,))
    if kind != SUPPORT:
        return _KIND_LABELS[kind]
    if not category:
        return "Support"
    return _CATEGORY_LABELS.get(category, category.replace("_", " ").capitalize())
