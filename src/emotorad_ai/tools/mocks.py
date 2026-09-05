"""Mocked implementations of the use-case-#1 tools (build plan §3.4, §5 step 2).

Every tool here answers with the real envelope and the real argument shape, so
swapping one for its live integration is a change inside `build_registry` and
nothing else. Nothing in this module talks to a real system.
"""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..knowledge import BatteryKnowledgeBase
from . import fixtures
from .registry import ToolError, ToolRegistry, ok

# Customer-facing tool names, so agents and tests refer to one spelling.
# One call, not two: the OMS answers "what do they own" and "is it covered"
# from the same record, and coverage is a date comparison in code rather than
# a second network hop.
LOOKUP_WARRANTY_RECORD = "lookup_warranty_record"
GET_BATTERY_DIAGNOSTICS = "get_battery_diagnostics"
SEARCH_KNOWLEDGE = "search_knowledge"
# The battery agent was written against the old name.
SEARCH_BATTERY_KNOWLEDGE = SEARCH_KNOWLEDGE
CREATE_SUPPORT_TICKET = "create_support_ticket"
FIND_SERVICE_SLOTS = "find_service_slots"
BOOK_SERVICE_SLOT = "book_service_slot"
SUBMIT_WARRANTY_PROOF = "submit_warranty_proof"

# --- dealer tools (W2) -------------------------------------------------------
# Split into quote / place deliberately. Quoting is a read and can be repeated
# freely; placing is a write that spends real credit. One tool that did both
# would let a model commit an order while it thought it was showing a price.
GET_DEALER_ACCOUNT = "get_dealer_account"
QUOTE_ORDER = "quote_order"
PLACE_ORDER = "place_order"

TICKET_CATEGORIES = ("battery_charging", "battery_range", "battery_power", "battery_safety", "other")
TICKET_SEVERITIES = ("low", "normal", "high", "critical")


def _clean(value: Any) -> Any:
    """Upstream's empty values, normalised at the boundary and nowhere else.

    The OMS returns `""` for absent strings and, in the orders payload, the
    *string* `"None"` alongside real nulls. Those are upstream defects we report
    rather than fix (edge case register §2), so they get translated once, here,
    instead of every caller learning to recognise them.
    """
    return None if value in ("", "None", "null") else value


def _coverage(record: Dict[str, Any], today: Optional[date]) -> Dict[str, Any]:
    """One bike, with coverage computed rather than read.

    The OMS carries no warranty dates at all — verified against the real 60-field
    response — so start and end are derived from `purchase_date` here. Coverage is
    resolved per bike, not per call: on a three-bike number one row can be missing
    its purchase date while the others are fine, and failing the whole lookup for
    that would deny the customer help with bikes we can answer for.
    """
    bike = {
        "frame_number": record["frame_number"],
        "product_name": _clean(record.get("product_name")),
        "product_color": _clean(record.get("product_color")),
        "battery_variant": _clean(record.get("battery_variant")),
        "franchise_name": _clean(record.get("franchise_name")),
        "purchase_date": _clean(record.get("purchase_date")),
    }

    if not bike["purchase_date"]:
        # Never fall back to `created_at`: that is when the customer *registered*,
        # so a January purchase registered in June would gain five months of free
        # coverage. Undeterminable is the honest answer, and it has a fix — the
        # customer has the date on their invoice.
        bike.update(
            {
                "in_warranty": None,
                "coverage_status": "purchase_date_missing",
                "remedy": "collect_purchase_proof",
                "note": (
                    "This bike is registered but has no recorded purchase date, so coverage "
                    "cannot be computed. Ask for the invoice or any proof of purchase showing "
                    "the date it was bought. Do not state or estimate a coverage date."
                ),
            }
        )
        return bike

    term_months = fixtures.warranty_term_months("battery", bike["product_name"])
    purchased = fixtures.parse_date(bike["purchase_date"])
    elapsed = fixtures.months_between(purchased, today or date.today())
    bike.update(
        {
            "in_warranty": elapsed < term_months,
            "coverage_status": "computed",
            "warranty_start": bike["purchase_date"],
            "warranty_end": fixtures.add_months(purchased, term_months).isoformat(),
            "term_months": term_months,
            # Flags a date we derived rather than one an authoritative system gave
            # us. The term is provisional until real per-product terms exist.
            "term_source": "provisional",
            "months_remaining": max(term_months - elapsed, 0),
        }
    )
    return bike


def _dealer_by_id(dealer_id: str) -> Optional[Dict[str, Any]]:
    for dealer in fixtures.DEALERS.values():
        if dealer["dealer_id"] == dealer_id:
            return dealer
    return None


def _price_order(dealer: Dict[str, Any], lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Price an order and decide whether it may be placed. **All of it in code.**

    The model may propose items and quantities. It may not set a price, invent a
    discount, decide that an overdue account is fine "just this once", or judge
    that a dealer is good for it. Those are money decisions, and a model that has
    been talked into one produces a confident, well-worded commitment that a
    company is then held to.
    """
    priced: List[Dict[str, Any]] = []
    blockers: List[str] = []
    total = 0

    for line in lines:
        name = (line.get("product_name") or "").strip()
        quantity = int(line.get("quantity") or 0)
        product = fixtures.PRICE_LIST.get(name)

        if product is None:
            raise ToolError(
                "unknown_product",
                "%r is not in the price list. Do not guess a price — ask the dealer which "
                "model they mean." % name,
            )
        if quantity <= 0:
            raise ToolError("invalid_quantity", "Quantity for %s must be at least 1." % name)
        if quantity > product["in_stock"]:
            blockers.append(
                "%s: only %d in stock, %d requested" % (name, product["in_stock"], quantity)
            )

        line_total = product["dealer_price"] * quantity
        total += line_total
        priced.append(
            {
                "product_name": name,
                "sku": product["sku"],
                "quantity": quantity,
                "unit_price": product["dealer_price"],
                "line_total": line_total,
                "in_stock": product["in_stock"],
            }
        )

    credit_available = max(dealer["credit_limit"] - dealer["credit_used"], 0)
    if dealer["status"] != "active":
        blockers.append("account is %s" % dealer["status"])
    if dealer["overdue_amount"] > 0:
        blockers.append("overdue balance of %d must be cleared first" % dealer["overdue_amount"])
    if total > credit_available:
        blockers.append("order total %d exceeds available credit %d" % (total, credit_available))

    return {
        "lines": priced,
        "total": total,
        "credit_available": credit_available,
        "can_place": not blockers,
        "blockers": blockers,
    }


def _owned_bike(phone: str, frame_number: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve which bike a write is about, refusing anything not owned.

    Two failures this exists to stop, both of which the model will otherwise
    commit confidently:

    * **A frame number the customer does not own.** Customers mistype them, and a
      model will happily echo one back out of the conversation. Checking against
      the record is the difference between a ticket on the right bike and a
      ticket on a stranger's.
    * **Silently picking one of several.** With three bikes on the number, an
      unspecified frame number is missing information, not a default — so this
      refuses rather than guessing, and the refusal tells the agent to ask.
    """
    records = fixtures.WARRANTY_RECORDS.get(phone) or []
    if not records:
        return None  # no record at all; the ticket is still worth raising

    owned = {record["frame_number"] for record in records}
    if frame_number:
        if frame_number not in owned:
            raise ToolError(
                "frame_number_not_owned",
                "Frame number %s is not registered to this customer. Do not use a frame number "
                "the customer typed without checking it against lookup_warranty_record; ask them "
                "to confirm it from the sticker on the frame." % frame_number,
            )
        return next(r for r in records if r["frame_number"] == frame_number)

    if len(records) > 1:
        raise ToolError(
            "frame_number_required",
            "This customer owns %d bikes, so the ticket needs a frame number. Ask which bike "
            "they mean and pass its frame number." % len(records),
        )
    return records[0]


class MockTicketSystem:
    """Stands in for Zoho Desk (confirmed for dealer-side W1; customer side is an open item)."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.tickets: Dict[str, Dict[str, Any]] = {}

    def create(self, **payload: Any) -> Dict[str, Any]:
        ticket_id = "EM-%05d" % next(self._counter)
        ticket = dict(payload, ticket_id=ticket_id, status="open")
        self.tickets[ticket_id] = ticket
        return ticket


class MockOrderSystem:
    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.orders: Dict[str, Dict[str, Any]] = {}

    def create(self, **payload: Any) -> Dict[str, Any]:
        order_id = "SO-%05d" % next(self._counter)
        order = dict(payload, order_id=order_id, status="placed")
        self.orders[order_id] = order
        return order


class MockBookingSystem:
    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.bookings: Dict[str, Dict[str, Any]] = {}
        self.taken: set = set()

    def book(self, centre_id: str, slot: str, customer_id: str) -> Dict[str, Any]:
        if (centre_id, slot) in self.taken:
            raise ToolError("slot_unavailable", "That slot has just been taken.", retryable=True)
        self.taken.add((centre_id, slot))
        booking_id = "BK-%05d" % next(self._counter)
        booking = {
            "booking_id": booking_id,
            "centre_id": centre_id,
            "slot": slot,
            "customer_id": customer_id,
        }
        self.bookings[booking_id] = booking
        return booking


def build_registry(
    knowledge_base: Optional[BatteryKnowledgeBase] = None,
    ticket_system: Optional[MockTicketSystem] = None,
    booking_system: Optional[MockBookingSystem] = None,
    order_system: Optional["MockOrderSystem"] = None,
    diagnostics_available: bool = False,
    oms_available: bool = True,
    knowledge_bike: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
    topics: Optional[Sequence[str]] = None,
) -> ToolRegistry:
    """Wire the mocked tools into a registry.

    `diagnostics_available` encodes an open item from the build plan: if Amiigo
    or the bikes do not report battery health yet, the diagnostics tool must not
    exist at all rather than exist and return nothing — an absent tool is a fact
    the model can reason about, an empty one invites it to guess.
    """

    kb = knowledge_base or BatteryKnowledgeBase()
    tickets = ticket_system or MockTicketSystem()
    bookings = booking_system or MockBookingSystem()
    orders = order_system or MockOrderSystem()
    registry = ToolRegistry()
    # The knowledge topics the model may narrow to. Runtime passes the bot
    # catalogue's list; a topic that has no bot is not an enum value, so the
    # model cannot search for a component nothing here can help with.
    topic_enum = list(topics) if topics else ["battery", "motor"]
    registry.tickets = tickets  # type: ignore[attr-defined]  # test/inspection handle
    registry.bookings = bookings  # type: ignore[attr-defined]
    registry.orders = orders  # type: ignore[attr-defined]

    @registry.register(
        LOOKUP_WARRANTY_RECORD,
        "The customer's registered bikes and their warranty coverage, from the OMS. Call this "
        "before saying anything about what someone owns or whether a repair is covered — never "
        "estimate either. The customer's phone is supplied by the platform, so this takes no "
        "arguments. Returns every bike on that number: if more than one comes back, ask which "
        "bike they mean rather than assuming.",
        parameters={},
        injects=("phone",),
    )
    def lookup_warranty_record(phone: str) -> Dict[str, Any]:
        if oms_available is False:
            # "The OMS is down" and "this person has no record" must never look
            # alike: one is retryable and says so, the other routes a genuine
            # customer to Late Warranty Registration. Conflating them either tells
            # a registered customer to re-register, or tells an unregistered one
            # to come back later forever.
            raise ToolError(
                "oms_unavailable",
                "The warranty system is not responding.",
                retryable=True,
            )

        records = fixtures.WARRANTY_RECORDS.get(phone)
        if not records:
            raise ToolError(
                "no_warranty_record",
                "No bike is registered against this number. The customer may still be a genuine "
                "owner — warranty registration is often skipped — so offer to register the bike "
                "now rather than suggesting they are not a customer.",
                remedy="late_warranty_registration",
            )

        bikes = [_coverage(record, today) for record in records]
        return ok(
            {
                "customer_name": records[0].get("customer_name"),
                "bike_count": len(bikes),
                "bikes": bikes,
            },
            freshness_seconds=300,
        )

    if diagnostics_available:

        @registry.register(
            GET_BATTERY_DIAGNOSTICS,
            "Latest telematics reading for the signed-in customer's battery: state of health, "
            "cycle count and any stored BMS error codes.",
            parameters={},
            injects=("phone",),
        )
        def get_battery_diagnostics(phone: str) -> Dict[str, Any]:
            # Shape only. Replace with the real telematics read once it exists.
            return ok(
                {
                    "state_of_health_pct": 91,
                    "cycle_count": 142,
                    "error_codes": [],
                    "last_seen": "2026-07-27T19:04:00+05:30",
                },
                freshness_seconds=1800,
            )

    @registry.register(
        SEARCH_KNOWLEDGE,
        "Search Emotorad's service documentation for troubleshooting steps. Use this for any "
        "factual guidance about how the bike behaves — do not answer from memory. Results are "
        "filtered to the customer's own bike, so a step that comes back is safe to give them.",
        parameters={
            "query": {
                "type": "string",
                "description": "The customer's symptom in your own words, e.g. 'charger LED does not turn on'.",
            },
            "topic": {
                "type": "string",
                "enum": topic_enum,
                "description": "Narrows the search. Omit only if the symptom genuinely spans more than one topic.",
            },
        },
        required=("query",),
    )
    def search_knowledge(query: str, topic: Optional[str] = None) -> Dict[str, Any]:
        # `bike` drives the applies_to filter, so a record written for a bike with
        # a throttle is unretrievable for one without. The model cannot widen this
        # by phrasing the query differently — the filter is applied here, not by
        # the search terms.
        passages = kb.search(query, topic=topic, bike=knowledge_bike or {})
        if not passages:
            # An explicit empty answer, not a shrug. Without this the model fills
            # the silence from its own training data, which is exactly the
            # failure the tool exists to prevent.
            return ok(
                {
                    "passages": [],
                    "note": (
                        "Nothing in our documentation covers this. Say you are not certain and "
                        "raise a ticket rather than answering from general knowledge."
                    ),
                },
                freshness_seconds=86400,
            )
        return ok({"passages": [p.to_dict() for p in passages]}, freshness_seconds=86400)

    @registry.register(
        CREATE_SUPPORT_TICKET,
        "Raise a support ticket for the signed-in customer when the issue cannot be resolved in chat. "
        "Summarise the symptom and the troubleshooting already attempted.",
        parameters={
            "category": {"type": "string", "enum": list(TICKET_CATEGORIES)},
            "description": {
                "type": "string",
                "description": "Symptom, steps already tried, and their result.",
            },
            "severity": {"type": "string", "enum": list(TICKET_SEVERITIES)},
            "idempotency_key": {
                "type": "string",
                "description": "Stable key for this ticket, so a retry does not create a duplicate.",
            },
            "frame_number": {
                "type": "string",
                "description": (
                    "Frame number of the bike this ticket is about. Required when the customer "
                    "owns more than one bike. Must be one of the frame numbers returned by "
                    "lookup_warranty_record — never invented, and never taken from what the "
                    "customer typed without checking it against that list."
                ),
            },
        },
        required=("category", "description", "severity", "idempotency_key"),
        injects=("phone",),
        write=True,
    )
    def create_support_ticket(
        phone: str,
        category: str,
        description: str,
        severity: str,
        idempotency_key: str,
        frame_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        if category not in TICKET_CATEGORIES:
            raise ToolError("invalid_category", "Unknown ticket category %r." % category)
        if severity not in TICKET_SEVERITIES:
            raise ToolError("invalid_severity", "Unknown severity %r." % severity)
        bike = _owned_bike(phone, frame_number)
        ticket = tickets.create(
            phone=phone,
            category=category,
            description=description,
            severity=severity,
            frame_number=bike.get("frame_number") if bike else None,
            bike_model=bike.get("product_name") if bike else None,
        )
        return ok(
            {
                "ticket_id": ticket["ticket_id"],
                "status": ticket["status"],
                "expected_response": "within 24 hours on working days",
            }
        )

    @registry.register(
        FIND_SERVICE_SLOTS,
        "Find service centres near a pincode and the slots they have open.",
        parameters={"pincode": {"type": "string", "description": "Six-digit Indian pincode."}},
        required=("pincode",),
    )
    def find_service_slots(pincode: str) -> Dict[str, Any]:
        centres: List[Dict[str, Any]] = []
        for centre in fixtures.SERVICE_CENTRES:
            open_slots = [s for s in centre["slots"] if (centre["centre_id"], s) not in bookings.taken]
            # Same-city match stands in for a real geo lookup.
            if centre["pincode"][:3] == pincode[:3] and open_slots:
                centres.append(dict(centre, slots=open_slots))
        return ok({"centres": centres}, freshness_seconds=60)

    @registry.register(
        BOOK_SERVICE_SLOT,
        "Book one of the slots returned by find_service_slots for the signed-in customer.",
        parameters={
            "centre_id": {"type": "string"},
            "slot": {"type": "string", "description": "Exact slot timestamp from find_service_slots."},
            "idempotency_key": {"type": "string", "description": "Stable key so a retry does not double-book."},
        },
        required=("centre_id", "slot", "idempotency_key"),
        injects=("phone",),
        write=True,
    )
    def book_service_slot(phone: str, centre_id: str, slot: str, idempotency_key: str) -> Dict[str, Any]:
        booking = bookings.book(centre_id, slot, phone)
        return ok(booking)

    @registry.register(
        SUBMIT_WARRANTY_PROOF,
        "Submit a customer's warranty registration or proof of purchase for a human to verify. "
        "Use this once you have what they can give you. This does NOT register the warranty or "
        "set any coverage — it queues the evidence for a colleague to check.",
        parameters={
            "frame_number": {
                "type": "string",
                "description": "Frame number read off the bike by the customer.",
            },
            "proof_url": {
                "type": "string",
                "description": "The invoice or proof-of-purchase image the customer sent, if any.",
            },
            "claimed_purchase_date": {
                "type": "string",
                "description": (
                    "The date the customer SAYS they bought it, ISO format. Recorded as their "
                    "claim only — it never sets coverage and must never be quoted back as one."
                ),
            },
            "purchase_channel": {
                "type": "string",
                "enum": ["dealer", "website", "marketplace", "unknown"],
            },
            "idempotency_key": {
                "type": "string",
                "description": "Stable key so a retry does not queue the same proof twice.",
            },
        },
        required=("frame_number", "idempotency_key"),
        injects=("phone",),
        write=True,
    )
    def submit_warranty_proof(
        phone: str,
        frame_number: str,
        idempotency_key: str,
        proof_url: Optional[str] = None,
        claimed_purchase_date: Optional[str] = None,
        purchase_channel: str = "unknown",
    ) -> Dict[str, Any]:
        if purchase_channel not in ("dealer", "website", "marketplace", "unknown"):
            raise ToolError("invalid_channel", "Unknown purchase channel %r." % purchase_channel)

        submission = tickets.create(
            phone=phone,
            category="late_warranty_registration",
            severity="normal",
            description=(
                "Warranty proof submitted for frame %s via %s. Customer states purchase date %s. "
                "REQUIRES HUMAN VERIFICATION against the document before any coverage is set."
                % (frame_number, purchase_channel, claimed_purchase_date or "not given")
            ),
            frame_number=frame_number,
            proof_url=proof_url,
            claimed_purchase_date=claimed_purchase_date,
            verified=False,
        )
        return ok(
            {
                "reference": submission["ticket_id"],
                "status": "awaiting_human_verification",
                # Stated in the payload so the model cannot read this as a
                # completed registration and congratulate the customer on being
                # covered from a date nobody has checked.
                "coverage_set": False,
                "note": (
                    "Evidence queued only. No coverage has been set and no date has been "
                    "verified. Do not tell the customer their warranty is now active or quote "
                    "any coverage dates."
                ),
            }
        )

    # --- dealer tools --------------------------------------------------------

    @registry.register(
        GET_DEALER_ACCOUNT,
        "The signed-in dealer's account: credit limit, credit used, overdue amount and status. "
        "Call this before discussing any order. The dealer is supplied by the platform.",
        parameters={},
        injects=("dealer_id",),
    )
    def get_dealer_account(dealer_id: str) -> Dict[str, Any]:
        dealer = _dealer_by_id(dealer_id)
        if dealer is None:
            raise ToolError("dealer_not_found", "No dealer account for this number.")
        available = max(dealer["credit_limit"] - dealer["credit_used"], 0)
        return ok(
            {
                "dealer_id": dealer["dealer_id"],
                "name": dealer["name"],
                "credit_limit": dealer["credit_limit"],
                "credit_used": dealer["credit_used"],
                "credit_available": available,
                "overdue_amount": dealer["overdue_amount"],
                "payment_terms_days": dealer["payment_terms_days"],
                "status": dealer["status"],
            },
            freshness_seconds=60,
        )

    @registry.register(
        QUOTE_ORDER,
        "Price an order for the signed-in dealer and check whether it can be placed. This is a "
        "read: it commits nothing and can be called as often as needed. It returns the line "
        "prices, the total, and whether credit and account status allow it. **You must never "
        "state a price, discount or total that did not come from this tool.**",
        parameters={
            "lines": {
                "type": "array",
                "description": "Requested items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["product_name", "quantity"],
                },
            }
        },
        required=("lines",),
        injects=("dealer_id",),
    )
    def quote_order(dealer_id: str, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        dealer = _dealer_by_id(dealer_id)
        if dealer is None:
            raise ToolError("dealer_not_found", "No dealer account for this number.")
        return ok(_price_order(dealer, lines), freshness_seconds=60)

    @registry.register(
        PLACE_ORDER,
        "Place an order the dealer has explicitly confirmed. Only call this after quoting it and "
        "after the dealer has said yes to that exact quote in their own words. Re-prices and "
        "re-checks credit before committing, so a quote the dealer sat on for an hour cannot "
        "commit a stale price.",
        parameters={
            "lines": {
                "type": "array",
                "description": "The confirmed items — must match what was quoted.",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["product_name", "quantity"],
                },
            },
            "quoted_total": {
                "type": "integer",
                "description": "The total from quote_order that the dealer agreed to.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Stable key so a retry does not place the order twice.",
            },
        },
        required=("lines", "quoted_total", "idempotency_key"),
        injects=("dealer_id",),
        write=True,
    )
    def place_order(
        dealer_id: str, lines: List[Dict[str, Any]], quoted_total: int, idempotency_key: str
    ) -> Dict[str, Any]:
        dealer = _dealer_by_id(dealer_id)
        if dealer is None:
            raise ToolError("dealer_not_found", "No dealer account for this number.")

        priced = _price_order(dealer, lines)

        # Re-priced here, not trusted from the conversation. The model has held a
        # number across several turns and may have mistyped, rounded, or applied a
        # discount nobody authorised.
        if priced["total"] != quoted_total:
            raise ToolError(
                "quote_mismatch",
                "The order now prices at %d, not the %d that was quoted. Re-quote it and get the "
                "dealer to confirm the new total before placing anything."
                % (priced["total"], quoted_total),
            )
        if not priced["can_place"]:
            raise ToolError("order_blocked", "; ".join(priced["blockers"]))

        order = orders.create(
            dealer_id=dealer["dealer_id"],
            lines=priced["lines"],
            total=priced["total"],
        )
        return ok(
            {
                "order_id": order["order_id"],
                "total": priced["total"],
                "status": "placed",
                "credit_available_after": priced["credit_available"] - priced["total"],
            }
        )

    return registry
