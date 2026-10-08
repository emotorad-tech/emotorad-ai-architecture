"""Mocked implementations of the use-case-#1 tools (build plan §3.4, §5 step 2).

Every tool here answers with the real envelope and the real argument shape, so
swapping one for its live integration is a change inside `build_registry` and
nothing else. Nothing in this module talks to a real system.
"""

from __future__ import annotations

import itertools
import re
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import media as media_module
from ..address import AddressError, PincodeDirectory, assemble, parse_address
from ..contract import ASSERTED, VERIFIED
from ..conversation import StoreUnavailable, address_tokens
from ..evidence_check import DEFAULT_MISSING
from ..fulfilment import ItemCodes, ReplacementOrders, decide, is_sure, load_parts_table
from ..guardrails import check_safety_in_description
from ..knowledge import BatteryKnowledgeBase
from ..tickets.caps import CAP_TEXTS, cap_reached
from ..tickets.clock import now_iso
from . import fixtures
from .verification import VerificationStore, register_verification_tools
from .registry import ToolError, ToolRegistry, ok

# Customer-facing tool names, so agents and tests refer to one spelling.
# One call, not two: the OMS answers "what do they own" and "is it covered"
# from the same record, and coverage is a date comparison in code rather than
# a second network hop.
LOOKUP_WARRANTY_RECORD = "lookup_warranty_record"
GET_BATTERY_DIAGNOSTICS = "get_battery_diagnostics"
# From the Amigo app, read-only (tools/amigo.py); registered only with a reader.
GET_SERVICE_STATUS = "get_service_status"
GET_RECENT_TRIPS = "get_recent_trips"
SEARCH_KNOWLEDGE = "search_knowledge"
# The battery agent was written against the old name.
SEARCH_BATTERY_KNOWLEDGE = SEARCH_KNOWLEDGE
CREATE_SUPPORT_TICKET = "create_support_ticket"
FIND_SERVICE_SLOTS = "find_service_slots"
BOOK_SERVICE_SLOT = "book_service_slot"
SUBMIT_WARRANTY_PROOF = "submit_warranty_proof"
# The unverified counterpart of create_support_ticket. Separate on purpose:
# see its registration below for why the two must not be one tool.
RAISE_INTAKE_TICKET = "raise_intake_ticket"
# Sends one of a fixed set of guide pictures. Interim: media belongs on the
# knowledge record whose steps it illustrates, and moves there once the flow
# is lifted out of the prompt.
SEND_GUIDE_MEDIA = "send_guide_media"
OFFER_LOCATION_SHARE = "offer_location_share"
# Display error code -> what it means on *this* bike. Exact lookup, never a
# near match: a confidently wrong diagnosis is the worst output available.
LOOKUP_ERROR_CODE = "lookup_error_code"

# --- dealer tools (W2) -------------------------------------------------------
# Split into quote / place deliberately. Quoting is a read and can be repeated
# freely; placing is a write that spends real credit. One tool that did both
# would let a model commit an order while it thought it was showing a price.
GET_DEALER_ACCOUNT = "get_dealer_account"
QUOTE_ORDER = "quote_order"
PLACE_ORDER = "place_order"

# --- replacement fulfilment --------------------------------------------------
PLACE_REPLACEMENT_ORDER = "place_replacement_order"

TICKET_CATEGORIES = ("battery_charging", "battery_range", "battery_power", "battery_safety",
                     # The motor cases (AFS §5f, 8 October 2026): a fault or physical damage
                     # goes to the Approval team; a jam or a motor that fails under load to
                     # the Service team (zoho/payload.NEXT_ACTIONS).
                     "motor_fault", "motor_damage", "motor_jam", "motor_under_load",
                     "other")
# A hazard is raised on the customer's word, never held behind a photograph.
EVIDENCE_EXEMPT_CATEGORIES = ("battery_safety",)
# Categories whose ticket never waits for evidence. None since 8 October 2026:
# a motor jam is a motor replacement ticket now, with its video like the rest.
NO_EVIDENCE_CATEGORIES: Tuple[str, ...] = ()
# Every motor ticket waits for photos of these parts (the person's rule, 8
# October 2026), for the serials' OCR and the evidence check: a reading of each
# in the conversation's serial_readings (serial_read.py), legible or not. A
# hazard is never held back. Only with the serial ask on (the runtime injects
# the readings; None means off).
SERIAL_PHOTOS_REQUIRED = {
    category: ("motor", "controller", "frame")
    for category in ("motor_fault", "motor_damage", "motor_jam", "motor_under_load")
}
_SERIAL_LABELS = {"battery": "Battery serial", "motor": "Motor serial", "controller": "Controller S/N",
                  "frame": "Frame number", "warranty_seal": "Battery warranty seal"}
_SERIAL_ASKED_FOR = {"motor": "the serial number printed on the rear hub motor",
                     "controller": "the controller's label, on the frame where the battery slots in",
                     "frame": "the frame number sticker on the seat tube"}


def _serial_photos_missing(category: str, readings: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    if readings is None:
        return []
    seen = {r.get("part") for r in readings}
    return [part for part in SERIAL_PHOTOS_REQUIRED.get(category, ()) if part not in seen]


def serials_line(readings: Optional[Sequence[Mapping[str, Any]]]) -> Optional[str]:
    """What the photos showed, for the ticket: each part once, the confirmed
    or latest reading, and how it is known. Never the model's words."""
    if not readings:
        return None
    best: Dict[str, Mapping[str, Any]] = {}
    for reading in readings:
        part = reading.get("part")
        if part not in _SERIAL_LABELS:
            continue
        kept = best.get(part)
        if kept is None or reading.get("confirmed") or not kept.get("confirmed"):
            best[part] = reading
    pieces = []
    for part in ("battery", "motor", "controller", "frame", "warranty_seal"):
        reading = best.get(part)
        if reading is None:
            continue
        if part == "warranty_seal":
            pieces.append("%s: %s (read by AI)" % (_SERIAL_LABELS[part], reading.get("seal") or "unclear"))
        elif not reading.get("serial"):
            pieces.append("%s: photo received, not readable" % _SERIAL_LABELS[part])
        elif reading.get("source") == "typed":
            pieces.append("%s: %s (typed by the customer)" % (_SERIAL_LABELS[part], reading["serial"]))
        elif reading.get("confirmed"):
            pieces.append("%s: %s (read by AI, confirmed by the customer)" % (_SERIAL_LABELS[part], reading["serial"]))
        else:
            pieces.append("%s: %s (read by AI, not confirmed)" % (_SERIAL_LABELS[part], reading["serial"]))
    return "From the customer's photos: " + "; ".join(pieces) + "." if pieces else None
TICKET_SEVERITIES = ("low", "normal", "high", "critical")


def _evidence_refusal(missing: Optional[str]) -> ToolError:
    """With the evidence check on (evidence_check.py): no fault ticket until
    the customer's media has passed, and the model is told what is missing."""
    return ToolError(
        "evidence_not_accepted",
        "The customer's photos and videos have not shown the problem itself, so a fault ticket "
        "cannot be raised yet. What is missing: %s. Ask the customer for a short video that shows "
        "it, or a clear photo if they cannot take a video."
        % ((missing or "").strip().rstrip(".") or DEFAULT_MISSING),
        remedy="collect_evidence",
    )


def _hazard_ticket(category: str, description: str, ticket_kind: Optional[str],
                   hazard_reported: Optional[bool]) -> bool:
    """Whether a support ticket is about a hazard, and so never waits for the
    evidence check. The safety branch's own ticket, whose kind only code sets;
    or the safety category on a hazard the customer's own words reported
    (`hazard_reported`, from the runtime) or the description names, negation
    aware. The category alone is the model's choice, and a customer can ask
    for it ("raise it as a safety issue"), so on its own it opens nothing
    (the review of 6 October 2026)."""
    if ticket_kind == "safety":
        return True
    return category in EVIDENCE_EXEMPT_CATEGORIES and (
        hazard_reported is True or check_safety_in_description(description).triggered)

# An Indian pincode. Six digits, first digit 1 to 9.
_PINCODE = re.compile(r"\b[1-9]\d{5}\b")
# An Indian mobile as the runtime keeps a typed one (ConversationState.typed_number,
# from verify_first.find_phone): ten ASCII digits, the first 6 to 9. Matched
# whole with fullmatch, so no word boundary is needed, and [0-9] rather than \d
# so a Devanagari digit is never a number to call.
_TEN_DIGIT_MOBILE = re.compile(r"[6-9][0-9]{9}")


def _clean(value: Any) -> Any:
    """Upstream's empty values, normalised at the boundary and nowhere else.

    The OMS returns `""` for absent strings and, in the orders payload, the
    *string* `"None"` alongside real nulls. Those are upstream defects we report
    rather than fix (edge case register §2), so they get translated once, here,
    instead of every caller learning to recognise them.
    """
    return None if value in ("", "None", "null") else value


def _date_only(value: Any) -> Optional[str]:
    """Upstream's dates, whatever shape they arrive in, as plain ``YYYY-MM-DD``.

    The OMS is not consistent with itself: `purchase_date` comes back as
    ``2026-07-12T00:00:00Z`` and `created_at` as
    ``2025-09-05 04:43:48.345000+00:00``, while the recorded fixtures carry bare
    dates. Coverage is a whole-day comparison, so the time and offset are noise —
    but noise that raises rather than degrades if it reaches `parse_date`.
    Translated once, at the boundary, per the same rule as `_clean()`.
    """
    cleaned = _clean(value)
    if cleaned is None:
        return None
    text = str(cleaned).strip()
    # Both separators appear in live payloads, from the same endpoint.
    head = text.replace("T", " ").split(" ", 1)[0]
    return head or None


def _coverage(record: Dict[str, Any], today: Optional[date]) -> Dict[str, Any]:
    """One bike, with coverage computed rather than read.

    The OMS carries no warranty dates at all — verified against the real 60-field
    response — so start and end are derived here, from `purchase_date` when it is
    present and `created_at` when it is not (see below). Coverage is resolved per
    bike, not per call: on a three-bike number one row can be missing its dates
    while the others are fine, and failing the whole lookup for that would deny
    the customer help with bikes we can answer for.
    """
    bike = {
        "frame_number": record.get("frame_number"),
        # The key selection and ownership checks use: the frame number, or an
        # opaque `app:` reference for a bike whose frame number is not on
        # record (an app bike registered by IMEI, tools/amigo.app_ref). Never
        # the VIN, and never shown to the rider.
        "bike_ref": record.get("bike_ref") or record.get("frame_number"),
        "frame_on_record": record.get("frame_on_record", True),
        "in_app": bool(record.get("in_app")),
        "product_name": _clean(record.get("product_name")),
        "product_color": _clean(record.get("product_color")),
        "battery_variant": _clean(record.get("battery_variant")),
        "franchise_name": _clean(record.get("franchise_name")),
        "purchase_date": _date_only(record.get("purchase_date")),
        # Where a replacement ships. The real response carries full_address;
        # the fixtures now carry one too. Read back to the customer before any
        # order is placed, never assumed.
        "delivery_address": _clean(record.get("full_address")),
        # For the ERP item-code read later. Absent on fixtures, present live.
        # An integer id, not a string, so it is deliberately not passed
        # through _clean() — that helper's job is normalising upstream's
        # empty-string conventions, and an id is never one of those.
        "product_id": record.get("product_id"),
    }

    # Three states only an Amigo bike can be in (tools/amigo.merged_source).
    if record.get("warranty_unavailable"):
        bike.update({
            "in_warranty": None,
            "coverage_status": "warranty_unavailable",
            "note": ("The warranty system is not responding, so coverage cannot be checked right now. "
                     "Say so plainly. Do not state or estimate coverage."),
        })
        return bike
    if record.get("warranty_unknown"):
        bike.update({
            "in_warranty": None,
            "coverage_status": "warranty_unknown",
            "note": ("This bike is in the EMotorad app with no frame number on record, and this number has "
                     "more than one bike of the same model with EMotorad, so which warranty record is this "
                     "bike's cannot be told. Do not state or estimate coverage. If warranty matters to what "
                     "they need, ask them to read the frame number from the sticker on the frame."),
        })
        return bike
    if record.get("warranty_on_record") is False:
        bike.update({
            "in_warranty": None,
            "coverage_status": "not_registered",
            "remedy": "late_warranty_registration",
            "note": ("This bike is in the EMotorad app but is not registered for warranty with EMotorad. "
                     "Do not state or estimate coverage. Registering it cannot be done in this chat: if "
                     "warranty matters to what they need, say the support team will help register it, and "
                     "raise a support ticket that says so."),
        })
        return bike

    if "warranty_api" in record:
        return _api_coverage(bike, record, today)

    # `purchase_date` is the right answer and `created_at` is the available one.
    # Verified against live OMS 2026-08-29: purchase_date and ocr_date were null
    # on every row returned, and docs/api-shapes/warranty.json (recorded
    # 2026-08-01) is null too. Refusing to compute without a purchase date
    # therefore dead-ends essentially every real conversation into "send us your
    # invoice", which is not support.
    #
    # So we fall back — and label it. created_at is when the customer
    # *registered*, always at or after the purchase, so coverage derived from it
    # runs LONGER than policy: a bike bought in January and registered in June
    # gains five months. That is a real liability, which is why the fallback is
    # never silent. `warranty_start_source` and the note say where the date came
    # from so the agent can hedge and a human can settle a disputed claim.
    started = bike["purchase_date"] or _date_only(record.get("created_at"))
    if not started:
        bike.update(
            {
                "in_warranty": None,
                "coverage_status": "purchase_date_missing",
                "remedy": "collect_purchase_proof",
                "note": (
                    "This bike is registered but has no recorded purchase or registration "
                    "date, so coverage cannot be computed. Ask for the invoice or any proof "
                    "of purchase showing the date it was bought. Do not state or estimate a "
                    "coverage date."
                ),
            }
        )
        return bike

    from_registration = not bike["purchase_date"]
    term_months = fixtures.warranty_term_months("battery", bike["product_name"])
    purchased = fixtures.parse_date(started)
    elapsed = fixtures.months_between(purchased, today or date.today())
    bike.update(
        {
            "in_warranty": elapsed < term_months,
            "coverage_status": "computed_from_registration" if from_registration else "computed",
            "warranty_start": purchased.isoformat(),
            "warranty_start_source": "registration_date" if from_registration else "purchase_date",
            "warranty_end": fixtures.add_months(purchased, term_months).isoformat(),
            "term_months": term_months,
            # Flags a date we derived rather than one an authoritative system gave
            # us. The term is provisional until real per-product terms exist.
            "term_source": "provisional",
            "months_remaining": max(term_months - elapsed, 0),
        }
    )
    if from_registration:
        bike["note"] = (
            "No purchase date on record, so coverage is measured from the registration "
            "date instead. Treat it as provisional: say it is based on when the bike was "
            "registered, and that the exact date can be confirmed from their invoice. Do "
            "not refuse a claim on this basis alone."
        )
    return bike


# The part the bike-level answer follows. The battery agent came first, and the
# OMS path used the battery's term too. Reading the bike's overall status
# instead would be unsafe: the frame runs 60 months and the battery 12, so a
# bike whose battery cover has ended is still "active" as a whole.
_LEAD_COMPONENT = "battery"


def _api_coverage(bike: Dict[str, Any], record: Dict[str, Any], today: Optional[date]) -> Dict[str, Any]:
    """One bike from the warranty API (tools/warranty_api.py): the service's own
    coverage, quoted rather than computed."""
    coverage = record.get("warranty_api") or {}
    if record.get("registration_status") != "active":
        # pending_review, or anything new the service adds: only an active
        # registration may lead to a coverage answer.
        bike.update({
            "in_warranty": None,
            "coverage_status": "pending_review",
            "note": ("EMotorad is still reviewing this bike's warranty registration (usually the invoice). "
                     "Do not state or estimate coverage. Say the registration is being reviewed and that "
                     "the support team will confirm the warranty once it is done."),
        })
        return bike
    if coverage.get("status") not in ("active", "expired"):
        # With the OMS database (tools/oms_db.py), whether OMS holds the
        # invoice: code reads it (invoice_ocr.py), so the model must not ask.
        on_file = bool(record.get("invoice_on_file"))
        with_support = bool(record.get("invoice_with_support"))
        bike.update({
            "in_warranty": None,
            "coverage_status": "purchase_date_missing",
            "remedy": coverage.get("remedy") or "collect_purchase_proof",
            "invoice_on_file": on_file,
            "invoice_with_support": with_support,
            "note": (
                "This bike's purchase date is not on record, but its invoice is on file and is being read "
                "now. Do not ask for the invoice. Do not state or estimate a coverage date; tell the "
                "customer you are checking their invoice."
                if on_file else
                "This bike's invoice is already with our support team, who will confirm the warranty. Do "
                "not ask for the invoice and do not raise another ticket. Do not state or estimate a "
                "coverage date."
                if with_support else
                "This bike is registered but its purchase date is not on record, so coverage cannot be "
                "known. Ask for the invoice or any proof of purchase showing the date it was bought. Do "
                "not state or estimate a coverage date."
            ),
        })
        return bike

    components = [
        {
            "component": part.get("component"),
            "months": part.get("months"),
            "valid_until": _date_only(part.get("validUntil")),
            "active": part.get("active") is True,
        }
        for part in coverage.get("components") or []
        if isinstance(part, dict)
    ]
    lead = next((part for part in components if part["component"] == _LEAD_COMPONENT), None)
    on = today or date.today()
    bike.update({
        "in_warranty": lead["active"] if lead else coverage.get("status") == "active",
        "coverage_status": "from_warranty_api",
        "warranty_start": bike["purchase_date"],
        "warranty_start_source": "purchase_date",
        # "oms_terms" when the cover was worked out from OMS's purchase date
        # (warranty_terms.py); "warranty_api" when the service gave it.
        "term_source": record.get("term_source") or "warranty_api",
        "components": components,
    })
    if lead and lead["valid_until"]:
        ends = fixtures.parse_date(lead["valid_until"])
        bike.update({
            "warranty_end": ends.isoformat(),
            "term_months": lead["months"],
            "months_remaining": max(fixtures.months_between(on, ends), 0) if ends > on else 0,
        })
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


# A frame number as riders read it off the sticker; the same shape triage
# listens for (triage._FRAME), whole-string here because it is an argument.
_FRAME_SHAPE = re.compile(r"[A-Z]{2,5}\d{6,}")


def _within_two_edits(a: str, b: str) -> bool:
    """Whether two frame numbers are at most two edits apart."""
    if abs(len(a) - len(b)) > 2:
        return False
    previous = list(range(len(b) + 1))
    for i, char in enumerate(a, start=1):
        current = [i]
        for j, other in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char != other)))
        previous = current
    return previous[-1] <= 2


# A ticket on a bike the customer gave because it is not in their list (spec
# 2026-10-01, unlisted bike).
UNLISTED_SOURCE = "given by the customer; not registered on this number"


def _unlisted_ticket_bike(
    frame_number: Optional[str], unlisted_bike: Optional[Dict[str, Optional[str]]]
) -> Optional[Dict[str, Any]]:
    """The conversation's unlisted bike, for a ticket that names no frame
    number or names that bike's; None otherwise (a listed bike's frame number
    is resolved as before)."""
    if not unlisted_bike:
        return None
    frame = unlisted_bike.get("frame_number")
    if frame_number and re.sub(r"\s+", "", frame_number).upper() != (frame or ""):
        return None
    return {"frame_number": frame, "product_name": unlisted_bike.get("model"),
            "frame_number_source": UNLISTED_SOURCE if frame else None}


def _owned_bike(
    phone: str,
    frame_number: Optional[str],
    bikes_on: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    allow_rider_read: bool = False,
    selected: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
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

    A bike whose frame number is not on record (an app bike registered by
    IMEI) is matched by its internal reference. For a ticket, a frame number
    the customer reads off it is accepted and marked as read by the rider, but
    only for the bike the conversation chose (`selected`, or the only bike on
    the number), only if it is shaped like a frame number, and never when it
    is one or two characters from a frame number that is on record: that is a
    typo of a bike we know, not a reading of one we do not.
    """
    records = bikes_on(phone) if bikes_on else (fixtures.WARRANTY_RECORDS.get(phone) or [])
    if not records:
        return None  # no record at all; the ticket is still worth raising

    def ref(record: Dict[str, Any]) -> Optional[str]:
        return record.get("bike_ref") or record.get("frame_number")

    if frame_number:
        wanted = re.sub(r"\s+", "", frame_number).upper()
        for record in records:
            if frame_number == ref(record) or wanted == re.sub(r"\s+", "", record.get("frame_number") or "").upper():
                return record
        unknown = [r for r in records if r.get("frame_on_record") is False]
        chosen = [r for r in unknown if selected and ref(r) == selected] or (unknown if len(records) == 1 else [])
        on_record = [re.sub(r"\s+", "", r.get("frame_number") or "").upper() for r in records]
        if (allow_rider_read and len(chosen) == 1 and _FRAME_SHAPE.fullmatch(wanted)
                and not any(_within_two_edits(wanted, known) for known in on_record if known)):
            return dict(chosen[0], frame_number=wanted, frame_number_source="read by the rider")
        raise ToolError(
            "frame_number_not_owned",
            "Frame number %s is not registered to this customer. Do not use a frame number "
            "the customer typed without checking it against lookup_warranty_record; ask them "
            "to confirm it from the sticker on the frame." % frame_number,
        )

    if selected:
        # The conversation's chosen bike, when the call names none. If this
        # read no longer lists it (Amigo stopped answering since it was
        # chosen), the ticket is raised without a bike rather than refused or
        # put on a bike the rider did not mean.
        return next((record for record in records if ref(record) == selected), None)

    if len(records) > 1:
        raise ToolError(
            "frame_number_required",
            "This customer owns %d bikes, so the ticket needs a frame number. Ask which bike "
            "they mean and pass its frame number." % len(records),
        )
    return records[0]


# Where a warranty proof's frame number came from: the customer read it out
# for a registration nobody has checked yet (spec 2026-10-05, section 3).
CLAIMED_FRAME_SOURCE = "given by the customer for registration; not checked"


def ticket_source_key(conversation_id: str, started_at: Optional[str], tool: str, idempotency_key: str) -> str:
    """A ticket's own key (spec 2026-10-05, section 2): the run, the tool and
    the call's idempotency key. The ticket system answers a key it has seen
    with the first ticket, so a retry whose receipt was lost, or a second
    server, never raises a second one, and a new run (a new person after
    restart_for) never gets the last run's."""
    return "%s:%s:%s:%s" % (conversation_id, started_at or "", tool, idempotency_key)


def _record_name(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """The customer's name from an OMS warranty record, or None. An app record
    carries the rider's username instead (tools/amigo.amigo_records marks it
    warranty_on_record False), and a name typed in the chat is a claim:
    neither is ever a ticket's customer name."""
    if not record or record.get("warranty_on_record") is False:
        return None
    return _clean(record.get("customer_name"))


class MockTicketSystem:
    """Stands in for Zoho Desk: in tests, the playground, the CLI and the live
    evaluation, and for every persona but the customer's when Zoho is on
    (tickets/seam.py). Its EM-00001 numbers exist nowhere else."""

    # Nothing it records reaches a person (TicketRouter says True).
    records_real_tickets = False

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.tickets: Dict[str, Dict[str, Any]] = {}
        # source_key -> ticket id: the same key always returns the same ticket.
        self._by_source_key: Dict[str, str] = {}

    def create(self, source_key: Optional[str] = None, persona: Optional[str] = None, **payload: Any) -> Dict[str, Any]:
        if source_key and source_key in self._by_source_key:
            return self.tickets[self._by_source_key[source_key]]
        ticket_id = "EM-%05d" % next(self._counter)
        ticket = dict(payload, ticket_id=ticket_id, status="open")
        if source_key:
            ticket["source_key"] = source_key
            self._by_source_key[source_key] = ticket_id
        if persona is not None:
            ticket["persona"] = persona
        self.tickets[ticket_id] = ticket
        return ticket

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        """The conversation thread on the ticket, for whoever picks it up.
        Zoho will implement this as a thread or comment; the mock keeps it."""
        if ticket_id not in self.tickets:
            raise KeyError("no ticket %s" % ticket_id)
        self.tickets[ticket_id]["transcript"] = transcript

    def add_note(self, ticket_id: str, text: str) -> None:
        """A short line for the ticket, kept with it."""
        if ticket_id not in self.tickets:
            raise KeyError("no ticket %s" % ticket_id)
        self.tickets[ticket_id].setdefault("notes", []).append(text)

    def close_runs(self, conversation_id: str, new_started_at: str) -> None:
        """Nothing to mark: the mock sends nothing anywhere."""


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
    # A MockTicketSystem, or the TicketRouter (tickets/seam.py) when Zoho is on.
    ticket_system: Optional[Any] = None,
    booking_system: Optional[MockBookingSystem] = None,
    order_system: Optional["MockOrderSystem"] = None,
    diagnostics_available: bool = False,
    oms_available: bool = True,
    # The bike retrieval filters against — a dict, or a callable returning one.
    # `applies_to` is a hard filter, so a record scoped to a model is
    # unretrievable while this is empty. Callable for the same reason
    # owned_bikes is: identity arrives mid-turn.
    knowledge_bike: Optional[Any] = None,
    today: Optional[date] = None,
    # Where registered bikes come from. Defaults to the fixtures; the playground
    # passes a reader backed by the live OMS. The seam is deliberately a plain
    # callable so the tool's contract, its four outcomes and the coverage math
    # stay identical whichever side of the swap you are on.
    warranty_source: Optional[Callable[[str], Optional[List[Dict[str, Any]]]]] = None,
    verification: Optional["VerificationStore"] = None,
    # key -> media item. Absent unless a catalogue is supplied, so an agent
    # with no pictures is never told it can send one.
    guide_media: Optional[Dict[str, Dict[str, Any]]] = None,
    # conversation_id -> the keys already sent in it. Kept by the caller so it
    # survives across turns, which is the only scope at which "already sent"
    # means anything.
    sent_media: Optional[Dict[str, set]] = None,
    # The published error-code table, and the bikes on this conversation's
    # account. Both absent unless supplied, so an agent with no table is never
    # told it can look codes up.
    error_codes: Optional[Any] = None,
    # A list, or a callable returning one. Callable where the answer can change
    # mid-turn: a customer who verifies and then asks about a code does both in
    # one assistant turn, and bikes captured at wiring time are still empty.
    owned_bikes: Optional[Any] = None,
    # Order/invoice code -> registered phone, for a customer who cannot recall
    # their number. Absent unless a real orders API is wired.
    account_finder: Optional[Callable[[str], Optional[str]]] = None,
    # Sends the one-time code (api.py passes MockOtpSender until the OTP
    # service is wired). None: the code is only stored, as before.
    send_code: Optional[Callable[[str, str], None]] = None,
    # The Amigo reader (tools/amigo.py), read-only. None: the two Amigo tools
    # are not registered, so no agent is told it can call them.
    amigo: Optional[Any] = None,
    # The replacement order the bot places on the customer's behalf. Absent
    # unless a store is supplied, so an agent that cannot place one is never
    # told it can. Item codes default to the mock resolver.
    replacement_orders: Optional["ReplacementOrders"] = None,
    item_codes: Optional["ItemCodes"] = None,
    approval_mode: str = "reasonable",
    # True only on a surface with a location button (the website chat).
    # WhatsApp shares location natively and IVR cannot; neither is offered a
    # button that does not exist there.
    location_sharing: bool = False,
    # Where write results are remembered. In memory by default; a durable store
    # with the same claim/get/put/release methods shares it across servers.
    idempotency: Optional[Any] = None,
) -> ToolRegistry:
    """Wire the mocked tools into a registry.

    `diagnostics_available` encodes an open item from the build plan: if Amiigo
    or the bikes do not report battery health yet, the diagnostics tool must not
    exist at all rather than exist and return nothing — an absent tool is a fact
    the model can reason about, an empty one invites it to guess.
    """

    kb = knowledge_base or BatteryKnowledgeBase()
    # A code-only picture (the melt ask's, 6 October 2026) is attached by code
    # to its own fixed reply and is never offered to a model: not in the
    # send_guide_media enum or description, not in the narrow prompt's list,
    # not named by a search result. Dropped here, before any of them is built.
    guide_media = media_module.model_offered(guide_media or {})

    def _default_knowledge_bike() -> Optional[Mapping[str, Any]]:
        # search_knowledge's own parameter shadows the name, so read it here.
        return knowledge_bike() if callable(knowledge_bike) else knowledge_bike

    tickets = ticket_system if ticket_system is not None else MockTicketSystem()
    bookings = booking_system or MockBookingSystem()
    orders = order_system or MockOrderSystem()
    registry = ToolRegistry(idempotency=idempotency) if idempotency is not None else ToolRegistry()
    # The guide pictures this registry can send (api.py passes only those
    # media.sendable kept), for the narrow prompt and the search results.
    registry.guide_media = dict(guide_media or {})  # type: ignore[attr-defined]

    # Every write validates a frame against the same source the lookup used.
    # Before this, _owned_bike read the fixtures directly, and with a live
    # warranty source a ticket for a real customer validated against fixtures,
    # found nothing, and silently dropped the frame number.
    def bikes_on(phone: str) -> List[Dict[str, Any]]:
        if warranty_source is not None:
            return warranty_source(phone) or []
        return fixtures.WARRANTY_RECORDS.get(phone) or []

    registry.tickets = tickets  # type: ignore[attr-defined]  # test/inspection handle
    registry.bookings = bookings  # type: ignore[attr-defined]
    registry.orders = orders  # type: ignore[attr-defined]

    # Only offered when a store is supplied. An agent that cannot verify anyone
    # should not be told it can — an absent tool is a fact the model can reason
    # about, a present one that never works invites it to keep trying.
    if error_codes is not None:

        @registry.register(
            LOOKUP_ERROR_CODE,
            "Look up a display error code the customer has read out — E-07, E30, and so on. "
            "The answer depends on which bike they own as well as the code, so this resolves "
            "both. Call it as soon as a customer mentions a code, before troubleshooting "
            "anything: it is a published table and it will tell you more in one call than "
            "several questions will. Never guess what a code means and never assume a code "
            "behaves like a neighbouring one.",
            parameters={
                "code": {
                    "type": "string",
                    "description": "The code exactly as the customer read it, e.g. 'E-07' or 'E30'.",
                }
            },
            required=("code",),
        )
        def lookup_error_code(code: str) -> Dict[str, Any]:
            """Resolve a code against the customer's own bike.

            Four outcomes, and they are not interchangeable. `found` is a
            documented diagnosis. `unknown_code` means it is not in the table at
            all. `not_possible_on_this_model` means it is documented elsewhere
            but not for this bike — which usually means the display was misread,
            and is a different conversation from a fault. `unknown_model` means
            no table is published for what they own.
            """
            bikes = (owned_bikes() if callable(owned_bikes) else owned_bikes) or []
            if not bikes:
                raise ToolError(
                    "no_bike_resolved",
                    "No bike is resolved for this conversation, so a code cannot be looked up. "
                    "Confirm who you are speaking to first.",
                )
            if len(bikes) > 1:
                # Compared by resolved model *group*, not by product name. Two X2
                # variants are spelled differently in the OMS and give the same
                # answer; an X2 and a T-REX + V3 do not. Only a real disagreement
                # is worth interrupting the customer for.
                groups = {
                    (error_codes.group_for(b.get("product_name")) or {}).get("id")
                    for b in bikes
                }
                if len(groups) > 1:
                    # Same discipline as everywhere else: never pick a bike for them.
                    names = sorted({b.get("product_name") or "?" for b in bikes})
                    raise ToolError(
                        "frame_number_required",
                        "This customer owns bikes that answer this code differently (%s). Ask "
                        "which one is showing it before looking it up." % ", ".join(names),
                    )
            result = error_codes.lookup(code, bikes[0].get("product_name"))
            if result["status"] == "found":
                return ok(result["entry"])
            return ok({"status": result["status"], "detail": result["detail"], "code": code})

    if guide_media:

        @registry.register(
            SEND_GUIDE_MEDIA,
            "Show the customer a guide photo or short clip — where a button is, what a light "
            "looks like, how a step is performed. Use it whenever a step is easier to point at "
            "than to describe. Only once it returns sent: true, say in your reply what you are "
            "showing them; if it returns an error, nothing was sent, so say you cannot show it "
            "right now and describe the step in words. Choose a key "
            "from the list below; you cannot send anything else, and there is no way to supply "
            "a file or a link. Available:\n%s"
            % "\n".join(
                "  %s — %s" % (key, item.get("caption", "")) for key, item in sorted(guide_media.items())
            ),
            parameters={
                "key": {
                    "type": "string",
                    "enum": sorted(guide_media),
                    "description": "Which guide picture to send.",
                }
            },
            required=("key",),
            injects=("conversation_id",),
        )
        def send_guide_media(conversation_id: str, key: str) -> Dict[str, Any]:
            """Resolve one catalogue key into something the channel can render.

            The key is an enum in the schema, so an invalid one is rejected before
            it reaches here — the model chooses from a set rather than naming a
            file. That is the same rule that keeps frame numbers out of its hands:
            a URL it composed itself would render as a broken image in front of a
            customer, and it would have no way to know.
            """
            already = sent_media.setdefault(conversation_id, set()) if sent_media is not None else set()
            if key in already:
                # The customer already has this picture. Sending it again is what
                # a model does when it has lost its place — it restates the step
                # it just gave, and the transcript reads as though nothing the
                # customer said registered. Refusing the duplicate makes that
                # visible to the model instead of only to the person reading it.
                return ok(
                    {
                        "already_sent": True,
                        "media": [],
                        # Worded as an instruction and nothing else. It used to
                        # open "you already sent X, so the customer has it" —
                        # a statement about the customer, in customer-facing
                        # phrasing, sitting in the model's context. It came back
                        # out of the model's mouth twice: "you've already seen
                        # that", "you already have that image", neither of which
                        # answers anything the customer asked.
                        #
                        # It also used to end at "move the case forward" without
                        # saying where forward is. Told not to repeat the step
                        # and given no destination, the model invented one — on
                        # an SOC button that had not lit, it asked whether the
                        # pack made a sound. Naming the record as the next
                        # destination costs nothing and is the whole fix.
                        "note": (
                            "Internal, not for the customer: %r was already sent and nothing "
                            "was sent now. Say nothing about the picture, whether it was sent, "
                            "or what they have already been shown — none of that answers them. "
                            "Take the next step from the knowledge record you retrieved, chosen "
                            "by the answer they just gave. If you are unsure which step that "
                            "is, search again rather than asking a question of your own." % key
                        ),
                    }
                )

            item = guide_media.get(key)
            if item is None:
                raise ToolError(
                    "unknown_guide_media",
                    "There is no guide picture called %r. Choose one of: %s."
                    % (key, ", ".join(sorted(guide_media))),
                )
            found = media_module.resolve(item)
            if found.get("unresolved"):
                # Say so rather than claiming to have sent something. The reply
                # can then describe the step instead of referring to a picture
                # the customer never received.
                raise ToolError(
                    "guide_media_unavailable",
                    "%r could not be prepared (%s). Describe the step in words instead, using "
                    "only the documented steps and the caption, and do not tell the customer "
                    "you have sent a picture."
                    % (key, found.get("reason", "unknown")),
                )
            already.add(key)
            return ok({"sent": True, "kind": found["kind"], "caption": found["caption"], "media": [found]})

    if verification is not None:
        register_verification_tools(registry, verification, send=send_code, account_finder=account_finder)
        registry.verification = verification  # type: ignore[attr-defined]

        @registry.register(
            RAISE_INTAKE_TICKET,
            "Raise a ticket for a customer whose identity could NOT be confirmed: they could "
            "not complete the one-time code, or could not give a number or order number at "
            "all. Use it so the conversation still ends with the case in a person's queue "
            "rather than nowhere. Everything you pass is what the customer TOLD you, not "
            "anything the system confirmed: pass their words. Do not state or imply any "
            "warranty outcome to the customer: a person verifies who they are before anyone "
            "acts on this. The team calls back on the customer's verified number, or else on "
            "the last mobile number they typed in this chat, never on anything you pass. If "
            "there is neither, this answers contact_number_required: ask the customer for a "
            "mobile number we can call, and call this again once they have typed it.",
            parameters={
                "stated_name": {"type": "string", "description": "Name as the customer gave it."},
                "stated_contact": {
                    "type": "string",
                    "description": (
                        "Any phone or email they offered, unverified, exactly as given. Kept as "
                        "their claim only: it is never the number we call back."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "The fault, what was already tried, and what the customer is asking for.",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Order or invoice number they read out, files they sent, anything else "
                        "a human can use to find them. Say so plainly if there is none."
                    ),
                },
                "idempotency_key": {"type": "string", "description": "Stable key for this request."},
            },
            required=("summary", "idempotency_key"),
            injects=("conversation_id",),
            # The number to call back and what the ticket record needs, from
            # the runtime (spec 2026-10-05, section 2). None is in the model's
            # schema, and anything it sends under these names is dropped.
            # With the evidence check on (evidence_check.py), whether media
            # that shows the problem has passed it, and what is missing:
            # for a verified phone this is a support ticket under another
            # name, so it waits the same way (the review of 6 October 2026).
            optional_injects=("phone", "identity_strength", "typed_number", "persona", "started_at",
                              "cluster_id", "channel", "evidence_accepted", "evidence_missing"),
            write=True,
        )
        def raise_intake_ticket(
            conversation_id: str,
            summary: str,
            idempotency_key: str,
            stated_name: str = "",
            stated_contact: str = "",
            evidence: str = "",
            phone: Optional[str] = None,
            identity_strength: Optional[str] = None,
            typed_number: Optional[str] = None,
            persona: Optional[str] = None,
            started_at: Optional[str] = None,
            cluster_id: Optional[str] = None,
            channel: Optional[str] = None,
            evidence_accepted: Optional[bool] = None,
            evidence_missing: Optional[str] = None,
        ) -> Dict[str, Any]:
            """A ticket that records claims, deliberately kept apart from the verified one.

            `create_support_ticket` needs a resolved phone and puts a bike and a
            coverage outcome on the ticket, which is right only for someone we
            know. This is the weaker ticket for everyone else, and the weakness
            is the point: it asserts nothing. Every field is what the customer
            said, labelled as such, and `identity` travels with it so triage
            cannot mistake it for a confirmed case.

            It does need a number to call back (the person's decision,
            2026-10-05): the verified phone when there is one, otherwise the
            last Indian mobile the customer typed in this run, which the
            runtime reads from their messages. Never `stated_contact`, which is
            the model's copy of their words. With neither it refuses, so the
            bot asks for a number rather than promising a call nobody can make.
            """
            if phone and identity_strength == VERIFIED:
                callback, identity = phone, "verified"
            elif typed_number and _TEN_DIGIT_MOBILE.fullmatch(typed_number):
                callback, identity = "+91" + typed_number, "unverified"
            else:
                raise ToolError(
                    "contact_number_required",
                    "There is no number to call this customer back on: ask the customer for a mobile "
                    "number we can call, then raise the ticket again once they have typed it.",
                    remedy="ask_for_callback_number",
                )
            if identity == "verified" and evidence_accepted is not None and evidence_accepted is not True:
                # The brief exempts the intake ticket because it is for
                # someone we cannot identify. On a verified phone in a fault
                # chat it would be the support ticket by another route.
                raise _evidence_refusal(evidence_missing)
            source_key = ticket_source_key(conversation_id, started_at, RAISE_INTAKE_TICKET, idempotency_key)
            # The caps on unverified tickets (spec 2026-10-05, section 6), the
            # same helper the runtime's gates use. A verified customer's intake
            # is never capped, and nor is a retry of a recorded one. A store
            # that cannot count does not cap, as for the gates
            # (Runtime._cap_refusal): the write below then succeeds or fails
            # on its own.
            if identity != "verified" and persona == "customer" and getattr(tickets, "records_real_tickets", False):
                try:
                    capped = cap_reached(tickets.store, phone=callback, source_key=source_key, now=now_iso())
                except StoreUnavailable:
                    capped = None
                if capped is not None:
                    raise ToolError(
                        "unverified_ticket_capped",
                        "No ticket was raised. Tell the customer exactly this, and promise no call: %s"
                        % CAP_TEXTS[capped],
                    )
            ticket = tickets.create(
                source_key=source_key,
                persona=persona,
                kind="intake",
                conversation_id=conversation_id,
                started_at=started_at,
                cluster_id=cluster_id,
                channel=channel,
                phone=callback,
                identity=identity,
                category="intake_unverified",
                severity="normal",
                stated_name=_clean(stated_name) or "not given",
                stated_contact=_clean(stated_contact) or "not given",
                evidence=_clean(evidence) or "none offered",
                description=summary,
            )
            return ok(
                {
                    "ticket_id": ticket["ticket_id"],
                    "status": ticket["status"],
                    "identity": identity,
                    "expected_response": "a person will verify the customer before acting on this",
                }
            )

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

        records = warranty_source(phone) if warranty_source else fixtures.WARRANTY_RECORDS.get(phone)
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

    if amigo is not None:
        from .amigo import AmigoUnavailable, describe_service, describe_trips

        def _amigo_down(exc: Exception) -> ToolError:
            return ToolError("amigo_unavailable",
                             "The app's records cannot be read right now (%s). Carry on without them." % exc,
                             retryable=True)

        @registry.register(
            GET_SERVICE_STATUS,
            "The customer's service stages from the EMotorad app (250 km / 1 month, 1000 km / 6 months, "
            "2000 km / 12 months): done, due or upcoming, and the odometer. Use it when a motor, brake or "
            "noise problem might come from a missed service. When the customer asks when a service is due "
            "or what has been done, call it and answer from it first, then go back to their issue. It is the "
            "app's record, not a booking: never book or promise a service from it.",
            parameters={},
            injects=("phone",),
        )
        def get_service_status(phone: str) -> Dict[str, Any]:
            try:
                return ok(describe_service(amigo.service_status(phone)), freshness_seconds=300)
            except AmigoUnavailable as exc:
                raise _amigo_down(exc)

        @registry.register(
            GET_RECENT_TRIPS,
            "The customer's last five rides from the EMotorad app, newest first: when, which bike, distance "
            "in km, duration in minutes and average speed. Use it to check a range or power complaint "
            "against real rides. When the customer asks to see their rides, call it and list them from it "
            "first, then go back to their issue. It holds no locations.",
            parameters={},
            injects=("phone",),
        )
        def get_recent_trips(phone: str) -> Dict[str, Any]:
            try:
                return ok(describe_trips(amigo.recent_trips(phone), amigo.bikes(phone)), freshness_seconds=300)
            except AmigoUnavailable as exc:
                raise _amigo_down(exc)

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
                "enum": ["battery", "motor"],
                "description": "Narrows the search. Omit only if the symptom genuinely spans both.",
            },
        },
        required=("query",),
        # The bike this conversation chose (Runtime._selected_bike), injected
        # when the tool runs. Absent from the model's schema, and a value the
        # model supplies is dropped, so it can never widen the filter. Before
        # 6 October 2026 nothing passed it on the web chat and a Doodle owner
        # was given the standard flow.
        optional_injects=("knowledge_bike",),
    )
    def search_knowledge(query: str, topic: Optional[str] = None,
                         knowledge_bike: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        # `bike` drives the applies_to filter, so a record written for a bike with
        # a throttle is unretrievable for one without. The model cannot widen this
        # by phrasing the query differently — the filter is applied here, not by
        # the search terms. The conversation's chosen bike wins; the registry's
        # own (the playground's, the live evaluation's) is the fallback.
        bike = knowledge_bike if knowledge_bike is not None else _default_knowledge_bike()
        passages = kb.search(query, topic=topic, bike=bike or {})
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
        return ok({"passages": [_with_sendable_media(p.to_dict()) for p in passages]}, freshness_seconds=86400)

    def _with_sendable_media(passage: Dict[str, Any]) -> Dict[str, Any]:
        """A passage names only the pictures this server can send, by the key
        send_guide_media takes; one it cannot send is not mentioned at all, so
        the model never offers it (2026-09-29)."""
        key_for = {item.get("id"): key for key, item in registry.guide_media.items() if item.get("id")}
        media_items = [{"key": key_for[item["id"]], "kind": item.get("kind", "image"), "caption": item.get("caption", "")}
                       for item in passage.pop("media", None) or [] if item.get("id") in key_for]
        if media_items:
            passage["media"] = media_items
        return passage

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
                    "customer typed without checking it against that list. The one exception: "
                    "for the chosen bike when its frame number is not on record, ask the "
                    "customer to read it from the sticker on the frame and pass what they read."
                ),
            },
        },
        required=("category", "description", "severity", "idempotency_key"),
        injects=("phone", "conversation_id"),
        # Whether any photo or video has arrived in the conversation, from the
        # runtime's facts; absent for a caller that has none (the safety branch).
        # Then what the ticket record needs (spec 2026-10-05, section 2): the
        # run, the persona (absent: the mock), the channel, how well the phone
        # is known (absent: unverified), the cover code worked out, and the
        # kind, which only the safety branch sets. None is in the model's
        # schema, and anything it sends under these names is dropped.
        # With the evidence check on (evidence_check.py), whether media that
        # shows the problem has passed it, what a better video would need, and
        # what Gemini saw; absent with it off, when the old evidence_seen rule
        # applies.
        optional_injects=("evidence_seen", "selected_bike", "unlisted_bike", "persona", "started_at",
                          "cluster_id", "channel", "identity_strength", "coverage", "ticket_kind",
                          "evidence_accepted", "evidence_missing", "evidence_checked", "hazard_reported",
                          "serial_readings"),
        write=True,
    )
    def create_support_ticket(
        phone: str,
        conversation_id: str,
        category: str,
        description: str,
        severity: str,
        idempotency_key: str,
        frame_number: Optional[str] = None,
        evidence_seen: Optional[bool] = None,
        selected_bike: Optional[str] = None,
        unlisted_bike: Optional[Dict[str, Optional[str]]] = None,
        persona: Optional[str] = None,
        started_at: Optional[str] = None,
        cluster_id: Optional[str] = None,
        channel: Optional[str] = None,
        identity_strength: Optional[str] = None,
        coverage: Optional[str] = None,
        ticket_kind: Optional[str] = None,
        evidence_accepted: Optional[bool] = None,
        evidence_missing: Optional[str] = None,
        evidence_checked: Optional[str] = None,
        hazard_reported: Optional[bool] = None,
        serial_readings: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if category not in TICKET_CATEGORIES:
            raise ToolError("invalid_category", "Unknown ticket category %r." % category)
        if severity not in TICKET_SEVERITIES:
            raise ToolError("invalid_severity", "Unknown severity %r." % severity)
        hazard = _hazard_ticket(category, description, ticket_kind, hazard_reported)
        if evidence_accepted is not None:
            # The evidence check is on (the person's brief, 6 October 2026):
            # a passing verdict replaces the evidence_seen test below. Never
            # for a hazard, which is raised on the customer's word.
            if evidence_accepted is not True and not hazard and category not in NO_EVIDENCE_CATEGORIES:
                raise _evidence_refusal(evidence_missing)
        elif (evidence_seen is False and category not in EVIDENCE_EXEMPT_CATEGORIES
              and category not in NO_EVIDENCE_CATEGORIES):
            # The rule the evidence post-check enforces on the reply, enforced
            # here on the write too: before, the ticket was created and only the
            # reply saying so was blocked, so the customer never heard of it.
            raise ToolError(
                "evidence_required",
                "No photo or video has arrived in this conversation, and a fault ticket needs one. "
                "Ask the customer for a photo or a short video of the fault. If they cannot send one "
                "(a voice call, or they say they cannot), hand the conversation to a person instead.",
                remedy="collect_evidence",
            )
        missing_photos = [] if hazard else _serial_photos_missing(category, serial_readings)
        if missing_photos:
            raise ToolError(
                "serial_photos_required",
                "A motor ticket needs a clear photo of each of these first, and they have not arrived: %s. Ask "
                "the customer for them, one message with all of them, saying the example pictures already sent "
                "show where each is. If a photo arrived but was not of the right part, ask for that one again."
                % "; ".join(_SERIAL_ASKED_FOR[part] for part in missing_photos),
                remedy="collect_serial_photos",
            )
        serials = serials_line(serial_readings)
        if serials:
            description = description.rstrip() + "\n\n" + serials
        verified = identity_strength == VERIFIED
        if identity_strength == ASSERTED:
            # Caller ID, which anyone can send (identity.resolve_voice): no
            # bike is looked up for it, so the real owner's bike never lands on
            # a stranger's ticket, and no frame number is checked against it.
            bike = None
        else:
            unlisted = _unlisted_ticket_bike(frame_number, unlisted_bike)
            try:
                bike = unlisted or _owned_bike(
                    phone, frame_number, bikes_on, allow_rider_read=True, selected=selected_bike)
            except ToolError as exc:
                # The safety branch fires before a bike is chosen. On a number
                # with several bikes its ticket is raised with no bike, never
                # refused for want of a frame number (spec 2026-10-05, section
                # 6). Only the kind code sets: the model's call is refused as before.
                if ticket_kind != "safety" or exc.code != "frame_number_required":
                    raise
                bike = None
            if (evidence_accepted is True and not hazard and frame_number and selected_bike and bike is not None
                    and not unlisted and (bike.get("bike_ref") or bike.get("frame_number")) != selected_bike):
                # The evidence that passed was about the bike this
                # conversation chose (the review of 6 October 2026).
                raise ToolError(
                    "evidence_for_another_bike",
                    "The video or photo that showed the problem was about the bike this conversation chose, "
                    "not this one. Ask the customer to go back to their bike list, choose this bike, and send "
                    "a short video that shows its problem.",
                    remedy="choose_bike",
                )
        ticket = tickets.create(
            source_key=ticket_source_key(conversation_id, started_at, CREATE_SUPPORT_TICKET, idempotency_key),
            persona=persona,
            # Set by code, never from the model's category: only the safety
            # branch's fact makes a safety ticket (spec 2026-10-05, section 3).
            kind="safety" if ticket_kind == "safety" else "support",
            conversation_id=conversation_id,
            started_at=started_at,
            cluster_id=cluster_id,
            channel=channel,
            phone=phone,
            identity="verified" if verified else "unverified",
            category=category,
            description=description,
            severity=severity,
            frame_number=bike.get("frame_number") if bike else None,
            frame_number_source=bike.get("frame_number_source") if bike else None,
            bike_model=bike.get("product_name") if bike else None,
            # A person's cover and name go on a ticket only for a phone
            # someone proved.
            coverage=coverage if verified else None,
            customer_name=_record_name(bike) if verified else None,
            # What Gemini saw, only after a pass (zoho/payload.py says so).
            **({"evidence_check": evidence_checked}
               if evidence_accepted is True and evidence_checked and category not in EVIDENCE_EXEMPT_CATEGORIES
               else {}),
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
        "set any coverage — it queues the evidence for a support executive to check.",
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
        injects=("phone", "conversation_id"),
        # What the ticket record needs, from the runtime (spec 2026-10-05,
        # section 2). None is in the model's schema, and anything it sends
        # under these names is dropped.
        optional_injects=("persona", "started_at", "cluster_id", "channel", "identity_strength", "coverage",
                          # Code only (invoice_ocr.py, spec 2026-10-08): what reading the
                          # invoice found, and the date when the reading was confident.
                          "invoice_findings", "invoice_purchase_date",
                          # Frames whose invoice code is reading or has passed on: the
                          # model may not raise its own ticket for them.
                          "invoice_frames"),
        write=True,
    )
    def submit_warranty_proof(
        phone: str,
        conversation_id: str,
        frame_number: str,
        idempotency_key: str,
        proof_url: Optional[str] = None,
        claimed_purchase_date: Optional[str] = None,
        purchase_channel: str = "unknown",
        persona: Optional[str] = None,
        started_at: Optional[str] = None,
        cluster_id: Optional[str] = None,
        channel: Optional[str] = None,
        identity_strength: Optional[str] = None,
        coverage: Optional[str] = None,
        invoice_findings: Optional[str] = None,
        invoice_purchase_date: Optional[str] = None,
        invoice_frames: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        if invoice_findings is None and frame_number in (invoice_frames or ()):
            raise ToolError(
                "invoice_with_code",
                "This bike's invoice is being read, or is already with our support team, and the ticket is "
                "raised for it automatically. Do not raise one. Tell the customer the support team will "
                "confirm their warranty.",
            )
        if purchase_channel not in ("dealer", "website", "marketplace", "unknown"):
            raise ToolError("invalid_channel", "Unknown purchase channel %r." % purchase_channel)

        # `proof_url` stays in the schema and is ignored (spec 2026-10-05,
        # section 3): the model never sees a URL, so one here is one it wrote.
        # The customer's own photo reaches the ticket from the conversation.
        verified = identity_strength == VERIFIED
        submission = tickets.create(
            source_key=ticket_source_key(conversation_id, started_at, SUBMIT_WARRANTY_PROOF, idempotency_key),
            persona=persona,
            kind="warranty_proof",
            conversation_id=conversation_id,
            started_at=started_at,
            cluster_id=cluster_id,
            channel=channel,
            phone=phone,
            identity="verified" if verified else "unverified",
            category="late_warranty_registration",
            severity="normal",
            description=(
                "Warranty proof submitted for frame %s via %s. %s "
                "REQUIRES HUMAN VERIFICATION against the document before any coverage is set.%s"
                % (frame_number, purchase_channel,
                   ("Invoice date read by AI: %s." % invoice_purchase_date) if invoice_purchase_date else
                   "Customer states purchase date %s." % (claimed_purchase_date or "not given"),
                   ("\n\n" + invoice_findings) if invoice_findings else "")
            ),
            # The customer's claims, labelled as such: nobody has checked the
            # frame number, the date or where it was bought.
            frame_number=frame_number,
            frame_number_source=CLAIMED_FRAME_SOURCE,
            claimed_purchase_date=claimed_purchase_date,
            purchase_channel=purchase_channel,
            coverage=coverage if verified else None,
            # The proof, not the person: nobody has read the document yet.
            verified=False,
        )
        return ok(
            {
                "reference": submission["ticket_id"],
                # The same id under the name the runtime reads, so this ticket
                # gets the transcript and counts as this turn's ticket.
                "ticket_id": submission["ticket_id"],
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

    if location_sharing:

        @registry.register(
            OFFER_LOCATION_SHARE,
            "Put a 'Share my location' button under your reply. Call it in the same turn as "
            "asking for the delivery pincode, so the customer can tap instead of typing. "
            "If they use it, their next message carries the pincode and area it resolved "
            "to; treat that pincode as given and confirm the area. No arguments.",
            parameters={},
            required=(),
            injects=("conversation_id",),
        )
        def offer_location_share(conversation_id: str) -> Dict[str, Any]:
            # The action is what the page renders; the model gets told it is
            # there and nothing else. Code names the action, the way code
            # resolves a guide photo's URL: nothing here is a string the model
            # chose.
            return ok({"offered": True, "action": {"kind": "request_location", "label": "Share my location"}})

    if replacement_orders is not None:
        parts_table = load_parts_table()
        codes = item_codes or ItemCodes()
        # Loaded once per process and shared; 19,584 rows, under a megabyte.
        pincodes = PincodeDirectory.load()

        @registry.register(
            PLACE_REPLACEMENT_ORDER,
            "Place a replacement-part order to the customer's address, once a flow has "
            "concluded that a part needs replacing and the customer has confirmed where to "
            "send it. Pass the address exactly as they confirmed it. This decides on its own "
            "whether the part needs a technician, whether an order is already on its way, "
            "and whether it can be approved now; read the result and say what it says. It "
            "handles in-warranty only: anything chargeable is refused and goes to a person.",
            parameters={
                "frame_number": {
                    "type": "string",
                    "description": "The bike, from lookup_warranty_record. Required when the customer owns more than one.",
                },
                "part": {
                    "type": "string",
                    # This build ships no-technician, no-ask parts only.
                    # Technician parts need a dealer visit and ask parts (offer
                    # self-fit or dealer, let the customer choose) are not
                    # built yet; the tool still refuses either in code if a
                    # model names one regardless, since schemas are advisory.
                    "enum": sorted(p for p, r in parts_table.items() if not r.technician and not r.ask),
                    "description": "The part the flow concluded needs replacing.",
                },
                "use_record_address": {
                    "type": "boolean",
                    "description": (
                        "True when the customer said the delivery_address from lookup_warranty_record "
                        "is still right. Ships to that address as it stands on the record."
                    ),
                },
                "address": {
                    "type": "object",
                    "description": (
                        "A delivery address the customer gave in this conversation, field by field, "
                        "after you read the assembled address back and they agreed. City and state "
                        "are not fields: they come from the pincode. Every value must be the "
                        "customer's own words."
                    ),
                    "properties": {
                        "house_or_flat": {"type": "string", "description": "House, flat or plot number, with tower or wing if any."},
                        "building_or_street": {"type": "string", "description": "Building or society name, or the street."},
                        "area": {"type": "string", "description": "Area, sector, locality or village."},
                        "landmark": {"type": "string", "description": "Optional. A nearby landmark, in the customer's words."},
                        "pincode": {"type": "string", "description": "Six-digit pincode."},
                        "city": {
                            "type": "string",
                            "description": (
                                "Only when the tool answered city_required: the district the customer "
                                "picked from the options it listed."
                            ),
                        },
                    },
                    "required": ["house_or_flat", "building_or_street", "area", "pincode"],
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Stable key for this order, so a retry does not place it twice.",
                },
            },
            required=("part", "idempotency_key"),
            injects=("phone", "conversation_id", "evidence_seen", "coverage_result", "customer_messages"),
            optional_injects=("unlisted_bike",),
            write=True,
        )
        def place_replacement_order(
            phone: str,
            conversation_id: str,
            evidence_seen: bool,
            coverage_result: Dict[str, Any],
            customer_messages: List[str],
            part: str,
            idempotency_key: str,
            frame_number: Optional[str] = None,
            use_record_address: bool = False,
            address: Optional[Dict[str, Any]] = None,
            unlisted_bike: Optional[Dict[str, Optional[str]]] = None,
        ) -> Dict[str, Any]:
            if unlisted_bike:
                # The customer's bike is not registered on this number (the
                # final review, 2026-10-01): nothing is ordered for a bike with
                # no record, nor against the listed bike they said is not theirs.
                raise ToolError(
                    "unlisted_bike",
                    "The customer's bike is not registered on this number, so a replacement cannot be "
                    "ordered from this chat. Hand the conversation to a person.",
                    remedy="human_handoff",
                )
            rule = parts_table.get(part)
            if rule is None:
                raise ToolError("part_not_identified", "%r is not a part this flow can order." % part)
            if rule.technician:
                raise ToolError(
                    "technician_required",
                    "A %s needs a technician to fit. Route the customer to a dealer rather than "
                    "shipping it to their address." % part,
                    remedy="dealer_visit",
                )
            if rule.ask:
                raise ToolError(
                    "customer_choice_required",
                    "A %s can be fitted by the customer or by a dealer. Ask which they prefer; "
                    "ordering it to an address is a later build." % part,
                    remedy="human_handoff",
                )
            if not use_record_address and not address:
                raise ToolError(
                    "address_required",
                    "Pass use_record_address if the customer said the record's address is still "
                    "right, or the address they gave, field by field, after reading it back.",
                )

            bike = _owned_bike(phone, frame_number, bikes_on)
            if bike is None:
                raise ToolError("frame_number_required", "No bike could be resolved for this order.")
            if not bike.get("frame_number"):
                # An app bike registered by IMEI: no order goes out against a
                # frame number nobody has checked.
                raise ToolError(
                    "frame_number_not_on_record",
                    "This bike's frame number is not on record, so a replacement cannot be ordered here. "
                    "Ask the customer to read the frame number off the sticker and raise a support ticket instead.",
                )
            frame = bike["frame_number"]

            # The address backstop. Two ways in, both decided here.
            #
            # The record's address ships as it stands: the customer said it is
            # still right, and nothing in it is the model's. A record with no
            # address (Krishna's OMS row) is a refusal, not an empty line.
            #
            # An address from the conversation is structured, so every field
            # is required by name; on 2026-09-20 the model shipped an order
            # without a pincode because dropping it was the only way past the
            # old check, and on the 21st it shipped one without a city or
            # state because nothing asked for either. A refusal a model can
            # route around by degrading its input is worse than no check.
            #
            # Provenance is per field: every word the customer is said to have
            # given must have been typed by them somewhere in this
            # conversation, or be on the record. City and state are never
            # typed; the pincode directory supplies them, and where a pincode
            # straddles two states the customer picks from that set.
            #
            # `full_address` is the raw record's field name; `_coverage()`
            # renames it to `delivery_address` for the model-facing lookup
            # result, but this reads the same record directly.
            record_address = _clean(bike.get("full_address")) or ""
            if use_record_address:
                if not record_address.strip():
                    raise ToolError(
                        "record_address_missing",
                        "There is no delivery address on this customer's record. Collect one, "
                        "field by field, and pass it as address.",
                    )
                delivery_address = record_address.strip()
            else:
                try:
                    parsed = parse_address(address or {})
                except AddressError as exc:
                    raise ToolError(exc.code, exc.message)
                known = address_tokens(record_address)
                for message in customer_messages:
                    known |= address_tokens(message)
                unconfirmed = []
                for name, value in parsed.customer_fields().items():
                    unknown = sorted(address_tokens(value) - known)
                    if unknown:
                        unconfirmed.append("%s: %s" % (name, ", ".join(unknown)))
                if unconfirmed:
                    raise ToolError(
                        "address_unconfirmed",
                        "These parts of the address were not typed by the customer and are not on "
                        "the record (%s). Read the address back and pass what they confirmed."
                        % "; ".join(unconfirmed),
                    )
                places = pincodes.lookup(parsed.pincode)
                if not places:
                    raise ToolError(
                        "pincode_unknown",
                        "%s is not a pincode India Post delivers to. Ask the customer to check it."
                        % parsed.pincode,
                    )
                states = sorted({place.state for place in places})
                picked = (address or {}).get("city")
                if len(states) > 1:
                    options = "; ".join("%s, %s" % (p.district, p.state) for p in places)
                    match = [p for p in places if picked and p.district.lower() == str(picked).strip().lower()]
                    if not match:
                        raise ToolError(
                            "city_required",
                            "Pincode %s is served from more than one state. Ask the customer which of "
                            "these it is and pass the district as city: %s." % (parsed.pincode, options),
                        )
                    place = match[0]
                else:
                    place = places[0]
                delivery_address = assemble(parsed, place)

            # Coverage, from the lookup the runtime remembered. Chargeable is a
            # later build; refusing it here keeps the model from improvising a
            # payment it cannot take.
            covered = None
            for entry in (coverage_result.get("data") or {}).get("bikes", []):
                if entry.get("frame_number") == frame:
                    covered = entry.get("in_warranty")
            if covered is None:
                raise ToolError(
                    "coverage_undetermined",
                    "Coverage for this bike is not settled. Ask for the invoice before ordering.",
                    remedy="collect_purchase_proof",
                )
            if covered is False:
                raise ToolError(
                    "chargeable_not_supported",
                    "This bike is out of warranty, so the part is chargeable. Chargeable "
                    "replacements are handled by a person for now; hand over rather than quote.",
                    remedy="human_handoff",
                )

            existing = replacement_orders.in_flight(frame, part)
            if existing is not None:
                return ok(
                    {
                        "order_id": existing["order_id"],
                        "status": existing["status"],
                        "part": part,
                        "item_code": existing.get("item_code"),
                        "delivery_address": existing.get("delivery_address"),
                        "already_placed": True,
                        "placed_at_utc": existing.get("placed_at_utc"),
                        "note": (
                            "Nothing new was placed. A replacement for this bike and part was "
                            "placed earlier, at the time and to the address above. Tell the "
                            "customer it already exists, quote the order id and that address, "
                            "and if they want the address changed, hand over to the support team."
                        ),
                    }
                )

            item_code = codes.resolve(bike.get("product_name"), part)
            sure = is_sure(evidence_seen, covered, item_code, rule)
            # A missing item code is never a refusal and never an approval: the
            # spec has a not-sure case still placed as pending_approval, with
            # nothing dropped, and a human resolving the code. Approving it
            # would hand the OMS an order it cannot fulfil; refusing it drops a
            # customer's correct claim for 48 hours behind the in-flight check.
            status = decide(sure, approval_mode)
            if item_code is None:
                status = "pending_approval"
            order = replacement_orders.create(
                frame_number=frame,
                part=part,
                item_code=item_code,
                delivery_address=delivery_address,
                phone=phone,
                conversation_id=conversation_id,
                sure=sure,
                status=status,
            )
            return ok(
                {
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "part": part,
                    "item_code": item_code,
                    "delivery_address": order["delivery_address"],
                    "already_placed": False,
                }
            )

    return registry
