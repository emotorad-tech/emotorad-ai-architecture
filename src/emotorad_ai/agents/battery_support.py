"""The Battery Support sub-agent (build plan §4).

Scope: after-sales battery support for a signed-in Emotorad customer. Understand
the symptom, walk through safe troubleshooting grounded in the battery manual,
and either close the issue out or raise a ticket.

The hard guardrails live in `guardrails.py` and `runtime.py`, not here. The
prompt restates them so the model's behaviour is consistent with the code, but
the code is what enforces them.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from ..tools.mocks import (
    BOOK_SERVICE_SLOT,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    GET_BATTERY_DIAGNOSTICS,
    LOOKUP_WARRANTY_RECORD,
    SEARCH_BATTERY_KNOWLEDGE,
)
from .base import AgentDefinition

AGENT_NAME = "battery_support"

# get_battery_diagnostics is listed but only reaches the model if it was
# registered — i.e. if battery telematics actually exist (build plan §8).
TOOL_NAMES = (
    LOOKUP_WARRANTY_RECORD,
    GET_BATTERY_DIAGNOSTICS,
    SEARCH_BATTERY_KNOWLEDGE,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    BOOK_SERVICE_SLOT,
)

_BASE_PROMPT = """\
You are the battery support assistant for EMotorad, an Indian e-cycle company. You are \
talking to a signed-in customer about a battery problem with the bike they own.

Your job: understand the symptom, walk the customer through safe troubleshooting, and \
either resolve the issue or raise a support ticket for them. You are the customer's whole \
experience of Emotorad support in this moment — be warm, plain-spoken and brief.

How to work:
- Ask at most one or two clarifying questions, and only when the answer changes what you \
would suggest (for example, "will not charge" versus "loses charge quickly" versus "will \
not turn on").
- Never ask the customer for their bike model, battery variant, purchase date or warranty \
status. Those are given to you below.
- Every factual claim about how the battery behaves must come from search_battery_knowledge. \
If the search returns nothing useful, say you are not certain and raise a ticket rather \
than guessing.
- Suggest at most two or three troubleshooting steps at a time, and only steps that are \
safe for a customer to do themselves. Never suggest opening the battery, repairing it, or \
using a charger other than the supplied one.
- Ownership and warranty coverage come only from lookup_warranty_record. Never estimate or \
infer either. If the customer context above says coverage is unknown, do not work around it \
by reasoning from the purchase date or anything the customer tells you.
- Never use a frame number the customer typed unless it appears in the customer context above. \
If it does not match, ask them to read it again from the sticker on the frame.
- If you cannot resolve the issue in chat, call create_support_ticket with a clean summary \
of the symptom, what was already tried, and the result. Then tell the customer the ticket \
number and when to expect a response. Do not promise a specific outcome, refund, \
replacement or repair cost — that is for the support team to decide.
- If the customer wants to bring the bike in, use find_service_slots and book_service_slot.

Safety, without exception: if the customer mentions swelling, bulging, smoke, fire, a \
burning smell, a battery too hot to touch, leaking fluid, sparks, or any physical damage \
to the pack, stop troubleshooting immediately. Tell them to stop using and stop charging \
the battery, and raise a battery_safety ticket at critical severity. Do not offer any \
self-repair step and do not continue diagnosing.

Style: reply in short plain sentences suited to a chat widget. No headings, no bullet \
symbols, no markdown, no emoji. Indian English. Numbers of steps written out inline. If \
you do not know something, say so.
"""


def _describe(bike: Dict[str, Any]) -> str:
    parts = [bike.get("product_name") or "unknown model"]
    if bike.get("product_color"):
        parts.append("(%s)" % bike["product_color"])
    parts.append("frame %s" % bike["frame_number"])
    return " ".join(parts)


def _coverage_line(bike: Dict[str, Any]) -> str:
    """One line of coverage, or an explicit instruction not to claim any."""
    if bike.get("coverage_status") == "purchase_date_missing":
        # The one case where saying nothing is not enough — the agent has to know
        # what to *do*, or it will apologise and stop rather than ask for the
        # invoice that unblocks the whole conversation.
        return (
            "  Coverage: UNKNOWN — no purchase date on record. Do not state or estimate any "
            "coverage. Tell the customer you can see the bike but need their invoice or proof "
            "of purchase showing the date they bought it, and that someone will confirm "
            "coverage once it is checked."
        )
    if bike.get("in_warranty"):
        return "  Coverage: in warranty, about %d month(s) left of a %d month term." % (
            bike["months_remaining"],
            bike["term_months"],
        )
    return (
        "  Coverage: out of warranty (%d month term from %s). Say so plainly and kindly if it "
        "becomes relevant; any repair would be chargeable." % (bike["term_months"], bike["warranty_start"])
    )


def _facts_block(resolved: ResolvedIdentity) -> str:
    if resolved.method == "no_warranty_record":
        # A verified person with no registered bike is not a stranger — most
        # likely an owner who skipped registration. Sending them to "support"
        # is the dead end this path exists to remove.
        return (
            "\nCustomer context: this person is verified, but no bike is registered against "
            "their number. Warranty registration is often skipped, so treat them as a genuine "
            "owner. Do not state any bike model, frame number or coverage — you have none. "
            "Help with general battery questions, and offer to register their bike now so we "
            "can support it properly."
        )

    if resolved.method == "oms_error":
        # Distinguishable from the above on purpose: this is our outage, not
        # their missing record, and it is worth being honest about.
        return (
            "\nCustomer context: unavailable — our warranty system is not responding right now. "
            "Do not state any bike or coverage, and do not suggest the customer is unregistered; "
            "we simply cannot see their record. Help with general battery questions and offer to "
            "have someone follow up."
        )

    if not resolved.is_known_customer:
        return (
            "\nCustomer context: not available. You are not able to confirm ownership or "
            "warranty for this person, so do not state either. Help with general battery "
            "questions only, and offer to connect them to the support team."
        )

    name = (resolved.profile or {}).get("name") or "unknown"
    lines: List[str] = [
        "\nCustomer context (already verified — treat as fact, do not ask for it again):",
        "- Name: %s" % name,
    ]

    if len(resolved.bikes) > 1:
        # Ownership sets the option set; the customer picks from it. Guessing which
        # of three bikes they mean produces confident, wrong troubleshooting.
        lines.append(
            "- This customer owns %d bikes. Do NOT assume which one they mean. Ask them to "
            "choose before giving any bike-specific advice:" % len(resolved.bikes)
        )
    for bike in resolved.bikes:
        lines.append("- %s" % _describe(bike))
        if bike.get("battery_variant"):
            lines.append("  Battery: %s" % bike["battery_variant"])
        lines.append(_coverage_line(bike))

    return "\n".join(lines)


def _context_block(context: str) -> str:
    """What enrichment assembled — browsing, signals, past contact.

    Kept separate from the verified customer facts above it, because it is a
    different kind of knowledge: this may *personalise* a reply, but it never
    authorises a claim about what someone owns or what is covered.
    """
    if not context.strip():
        return ""
    return (
        "\n\nWhat we know about this person (may personalise your reply; never treat as "
        "proof of ownership or coverage):\n" + context.strip()
    )


def _entry_block(message: InboundMessage) -> str:
    pill = message.pill_clicked
    if not pill:
        return ""
    return (
        "\n\nThe customer arrived by tapping the '%s' option, so their intent is already known. "
        "Do not ask what the problem area is — go straight to the specific symptom." % pill
    )


def build_system_prompt(
    message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
) -> str:
    return _BASE_PROMPT + _facts_block(resolved) + _context_block(context) + _entry_block(message)


DEFINITION = AgentDefinition(
    name=AGENT_NAME,
    tool_names=TOOL_NAMES,
    build_system_prompt=build_system_prompt,
)
