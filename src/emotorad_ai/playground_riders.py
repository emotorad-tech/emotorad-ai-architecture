"""Who the "customer" is for a playground turn.

Presets are named fixtures from tools/fixtures.py — the same data the automated
tests run against — hydrated through the real IdentityResolver so they stay
honest as the mocks evolve. Custom riders are typed in on the spot; coverage is
still computed by the real `_coverage()` helper, never re-implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import streamlit as st

from .contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from .identity import IdentityResolver, ResolvedIdentity
from .tools import fixtures
from .tools.mocks import _coverage, build_registry


@dataclass(frozen=True)
class Scenario:
    label: str
    persona: str
    channel: str
    phone: Optional[str]
    strength: str
    oms_available: bool = True


CUSTOMER_SCENARIOS = [
    Scenario("Single bike, in warranty (Ananya)", "customer", "website_chat", "+919876543210", VERIFIED),
    Scenario("Single bike, out of warranty (Rohit)", "customer", "website_chat", "+919812345678", VERIFIED),
    Scenario("Multi-bike customer (Priya, 3 bikes)", "customer", "whatsapp", "+919700000001", VERIFIED),
    Scenario("Unregistered — no warranty record", "customer", "website_chat", fixtures.PHONE_WITH_NO_RECORD, VERIFIED),
    Scenario("OMS system down (outage)", "customer", "website_chat", "+919876543210", VERIFIED, oms_available=False),
    Scenario("Anonymous / not signed in", "customer", "website_chat", None, ANONYMOUS),
]

DEALER_SCENARIOS = [
    Scenario("Dealer, healthy credit (Royal Cycle Stores)", "dealer", "dealer_app", "+919000000001", VERIFIED),
    Scenario("Dealer, near credit limit with overdue", "dealer", "dealer_app", "+919000000002", VERIFIED),
    Scenario("Dealer on hold", "dealer", "dealer_app", "+919000000003", VERIFIED),
]


def scenarios_for(persona: str) -> List[Scenario]:
    return DEALER_SCENARIOS if persona == "dealer" else CUSTOMER_SCENARIOS


def resolved_for_preset(scenario: Scenario) -> ResolvedIdentity:
    identity = Identity(strength=scenario.strength, phone=scenario.phone)
    message = InboundMessage(
        conversation_id="preview",
        persona=scenario.persona,
        identity=identity,
        channel=scenario.channel,
        message_text="",
    )
    registry = build_registry(oms_available=scenario.oms_available, today=date.today())
    return IdentityResolver(registry).hydrate(message)


def _resolved_for_custom_customer(
    name: str, phone: Optional[str], verified: bool, bike_rows: List[Dict[str, Any]]
) -> ResolvedIdentity:
    identity = Identity(strength=VERIFIED if verified else ANONYMOUS, phone=phone if verified else None)
    if not verified:
        return ResolvedIdentity(persona="customer", method="unverified", identity=identity)
    if not bike_rows:
        return ResolvedIdentity(
            persona="customer", method="no_warranty_record", identity=identity, error="no_warranty_record"
        )
    bikes = [_coverage(row, date.today()) for row in bike_rows]
    return ResolvedIdentity(
        persona="customer", method="verified", identity=identity, profile={"name": name}, bikes=bikes
    )


def custom_customer_form() -> ResolvedIdentity:
    name = st.text_input("Name", "Test Customer")
    phone = st.text_input("Phone", "+919999999999")
    verified = st.checkbox("Verified (signed in)", value=True)
    bike_rows: List[Dict[str, Any]] = []
    if verified:
        bike_count = st.number_input("Bikes owned", min_value=0, max_value=5, value=1, step=1)
        for i in range(int(bike_count)):
            with st.expander("Bike %d" % (i + 1), expanded=(bike_count == 1)):
                product_name = st.text_input("Model", "EMX Plus", key="custom_bike_model_%d" % i)
                purchase_date = st.date_input("Purchase date", value=date(2025, 6, 1), key="custom_bike_date_%d" % i)
                frame_number = st.text_input("Frame number", "CUSTOM%03d" % i, key="custom_bike_frame_%d" % i)
                battery_variant = st.text_input("Battery variant (optional)", "", key="custom_bike_batt_%d" % i)
                bike_rows.append(
                    {
                        "frame_number": frame_number,
                        "product_name": product_name,
                        "purchase_date": purchase_date.isoformat(),
                        "battery_variant": battery_variant,
                        "product_color": "",
                    }
                )
    return _resolved_for_custom_customer(name, phone, verified, bike_rows)


def custom_dealer_form() -> ResolvedIdentity:
    name = st.text_input("Dealer name", "Test Cycle Stores")
    phone = st.text_input("Phone", "+919999999999")
    city = st.text_input("City", "Pune")
    credit_limit = st.number_input("Credit limit (₹)", min_value=0, value=500000, step=10000)
    credit_used = st.number_input("Credit used (₹)", min_value=0, value=100000, step=10000)
    overdue = st.number_input("Overdue amount (₹)", min_value=0, value=0, step=1000)
    terms_days = st.number_input("Payment terms (days)", min_value=0, value=30, step=5)
    profile = {
        "dealer_id": "CUSTOM-%s" % phone[-4:],
        "name": name,
        "city": city,
        "credit_limit": int(credit_limit),
        "credit_used": int(credit_used),
        "payment_terms_days": int(terms_days),
        "overdue_amount": int(overdue),
        "status": "active",
    }
    return ResolvedIdentity(
        persona="dealer", method="verified", identity=Identity(strength=VERIFIED, phone=phone), profile=profile
    )
