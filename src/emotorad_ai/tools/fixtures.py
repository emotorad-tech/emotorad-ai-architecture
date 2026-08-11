"""Fake data behind the mocked tools (build plan §5 step 2).

Shapes here are the contract the real OMS/ERP/ticketing integrations must satisfy
when they replace the mocks one tool at a time. Values are invented; field names
and types are the part to review against the real systems.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

# --- PROVISIONAL WARRANTY TERM — the single replacement seam -----------------
#
# Decided 2026-08-01: coverage runs 24 months from purchase_date, for every
# component. This is a stand-in, not a validated business rule. The OMS warranty
# response carries no coverage dates at all (see docs/api-shapes/warranty.json),
# and no system we have today holds per-product terms.
#
# When that source arrives, reimplement warranty_term_months() and change nothing
# else. Never write a term literal anywhere but here — a second copy is how the
# agent starts giving two different answers to the same question.
PROVISIONAL_TERM_MONTHS = 24


def warranty_term_months(component: str, product_name: str | None = None) -> int:
    """Coverage length for a component, in months from the purchase date.

    ``product_name`` is accepted but unused: real terms are expected to vary by
    product and battery variant, so callers pass it now and the real lookup
    lands without touching a single call site.
    """
    return PROVISIONAL_TERM_MONTHS

# The OMS warranty table, keyed on phone — which is how the real API is keyed,
# and what WhatsApp and Amiigo hand us natively. There is no customer ID in the
# real system; `frame_number` identifies the bike.
#
# Field names and nulls mirror the real 60-key response captured in
# docs/api-shapes/warranty.json. Only the fields the platform actually reads are
# kept; the rest exist upstream and are ignored deliberately. Note there is **no
# warranty date of any kind** in the payload — that is the finding this fixture
# is shaped to preserve, so nobody re-adds one by accident.
WARRANTY_RECORDS: Dict[str, List[Dict[str, Any]]] = {
    # One bike, comfortably in warranty.
    "+919876543210": [
        {
            "customer_name": "Ananya Rao",
            "mobile": "+919876543210",
            "frame_number": "EMXP2025004417",
            "product_name": "EMX Plus",
            "product_color": "",  # empty string, not null — real API behaviour
            "purchase_date": "2025-03-14",
            "franchise_name": "EMotorad D2C",
            "pin_code": "411045",
            "battery_variant": "48V 14.4Ah removable",
            "created_at": "2025-03-20",  # registration, NOT purchase. Never compute from this
        }
    ],
    # One bike, bought long enough ago to be out of warranty.
    "+919812345678": [
        {
            "customer_name": "Rohit Menon",
            "mobile": "+919812345678",
            "frame_number": "DDL32022119302",
            "product_name": "Doodle V3",
            "product_color": "Matte Black",
            "purchase_date": "2022-11-02",
            "franchise_name": "Bengaluru Cycle Mart",
            "pin_code": "560037",
            "battery_variant": "36V 12.75Ah removable",
            "created_at": "2022-11-05",
        }
    ],
    # Three bikes on one number. Confirmed in production, so disambiguation is a
    # happy-path requirement, not an edge case.
    "+919700000001": [
        {
            "customer_name": "Priya Nair",
            "mobile": "+919700000001",
            "frame_number": "TREX2024881201",
            "product_name": "T-Rex Air",
            "product_color": "Blue",
            "purchase_date": "2024-06-11",
            "franchise_name": "Kochi Wheels",
            "pin_code": "682024",
            "battery_variant": "36V 13Ah removable",
            "created_at": "2024-06-14",
        },
        {
            "customer_name": "Priya Nair",
            "mobile": "+919700000001",
            "frame_number": "EMXP2025004990",
            "product_name": "EMX Plus",
            "product_color": "Grey",
            "purchase_date": "2025-01-08",
            "franchise_name": "Kochi Wheels",
            "pin_code": "682024",
            "battery_variant": "48V 14.4Ah removable",
            "created_at": "2025-01-09",
        },
        {
            "customer_name": "Priya Nair",
            "mobile": "+919700000001",
            "frame_number": "DDL32021100455",
            "product_name": "Doodle V3",
            "product_color": "Red",
            "purchase_date": "2021-09-30",
            "franchise_name": "Kochi Wheels",
            "pin_code": "682024",
            "battery_variant": "36V 12.75Ah removable",
            "created_at": "2021-10-02",
        },
    ],
    # Registered, but the purchase date was never captured. Real rows look like
    # this: coverage is undeterminable and the customer is asked for their invoice.
    "+919700000002": [
        {
            "customer_name": "Imran Shaikh",
            "mobile": "+919700000002",
            "frame_number": "EMXP2024773311",
            "product_name": "EMX Plus",
            "product_color": "",
            "purchase_date": None,
            "franchise_name": "Nagpur Cycle Hub",
            "pin_code": "440010",
            "battery_variant": "48V 14.4Ah removable",
            "created_at": "2024-08-19",
        }
    ],
}

# A phone with no record at all — a real customer who never registered. Not
# "not a customer": this is the Late Warranty Registration path. Present here as
# a named constant so tests state their intent.
PHONE_WITH_NO_RECORD = "+919700000009"

# --- dealers -----------------------------------------------------------------
#
# Keyed on phone, like customers — but a **separate table**, and that separation
# is load-bearing rather than tidy. Dealers perform most warranty registrations
# and routinely enter their own number, so a dealer's phone can also appear all
# over WARRANTY_RECORDS. Resolving a dealer through the customer path would hand
# them dozens of unrelated customers' bikes as if they owned them.
DEALERS: Dict[str, Dict[str, Any]] = {
    "+919000000001": {
        "dealer_id": "DLR-PUN-014",
        "name": "Royal Cycle Stores",
        "city": "Pune",
        "credit_limit": 500000,
        "credit_used": 380000,
        "payment_terms_days": 30,
        "overdue_amount": 0,
        "status": "active",
    },
    "+919000000002": {
        "dealer_id": "DLR-BLR-007",
        "name": "Bengaluru Cycle Mart",
        "city": "Bengaluru",
        "credit_limit": 300000,
        "credit_used": 295000,   # almost exhausted — the interesting case
        "payment_terms_days": 30,
        "overdue_amount": 42000,
        "status": "active",
    },
    "+919000000003": {
        "dealer_id": "DLR-NAG-002",
        "name": "Nagpur Cycle Hub",
        "city": "Nagpur",
        "credit_limit": 200000,
        "credit_used": 10000,
        "payment_terms_days": 30,
        "overdue_amount": 118000,   # on hold for overdue, not for credit
        "status": "on_hold",
    },
}

# Dealer price list. **The model never sets a price**, so this is the only place
# a number can come from. Prices are ex-GST dealer landing prices, not MRP.
PRICE_LIST: Dict[str, Dict[str, Any]] = {
    "EMX Plus": {"sku": "EMXP-48V", "dealer_price": 32000, "mrp": 41999, "in_stock": 24},
    "Doodle V3": {"sku": "DDL3-36V", "dealer_price": 24500, "mrp": 32999, "in_stock": 8},
    "T-Rex Air": {"sku": "TREX-36V", "dealer_price": 27000, "mrp": 35999, "in_stock": 0},
}

# session token -> the verified phone behind that session. Stands in for the
# website/Amiigo session store.
SESSIONS: Dict[str, str] = {
    "sess-ananya": "+919876543210",
    "sess-rohit": "+919812345678",
}

SERVICE_CENTRES: List[Dict[str, Any]] = [
    {
        "centre_id": "SC-PUN-01",
        "name": "EMotorad Service — Baner",
        "pincode": "411045",
        "city": "Pune",
        "slots": ["2026-08-01T10:00:00+05:30", "2026-08-01T15:30:00+05:30"],
    },
    {
        "centre_id": "SC-BLR-04",
        "name": "EMotorad Service — Marathahalli",
        "pincode": "560037",
        "city": "Bengaluru",
        "slots": ["2026-08-02T11:00:00+05:30"],
    },
]

# Battery troubleshooting corpus. In deployment this is the pgvector-indexed
# battery manual / support FAQ; the text below is a stand-in with the same shape.
# BATTERY_KNOWLEDGE lived here until 2026-08-06. The corpus is now authored as
# files under knowledge/, so it is loaded and validated rather than hard-coded.

def parse_date(value: str) -> date:
    year, month, day = (int(part) for part in value.split("-"))
    return date(year, month, day)


def months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months


def add_months(start: date, months: int) -> date:
    """The same date `months` later, clamped to the end of a shorter month.

    A bike bought on 31 August expires on the 28th/29th of February, not on a
    date that does not exist.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    last_day = _DAYS_IN_MONTH[month - 1]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        last_day = 29
    return date(year, month, min(start.day, last_day))


_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
