"""Prompt blocks shared by every sub-agent of a persona.

These are persona-level concerns — what the platform verified about the person,
what enrichment inferred, how they arrived, what a dealer's account looks like —
not battery- or motor-specific ones. One copy, imported everywhere: a second copy
would drift, and the copy that drifts is the one that starts stating coverage it
should not. `tests/test_blocks.py` asserts the identity, not just the behaviour.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..contract import InboundMessage
from ..identity import ResolvedIdentity


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


def _account_block(resolved: ResolvedIdentity) -> str:
    profile = resolved.profile or {}
    if not profile:
        return (
            "\nDealer context: unavailable. Do not quote or place anything until "
            "get_dealer_account succeeds."
        )

    available = max(profile.get("credit_limit", 0) - profile.get("credit_used", 0), 0)
    lines = [
        "\nDealer context (verified — treat as fact):",
        "- Dealer: %s (%s), %s" % (profile.get("name"), profile.get("dealer_id"), profile.get("city")),
        "- Credit available: %d of %d" % (available, profile.get("credit_limit", 0)),
        "- Payment terms: %d days" % profile.get("payment_terms_days", 0),
    ]
    if profile.get("overdue_amount"):
        lines.append(
            "- OVERDUE: %d. New orders are blocked until this is cleared. Say so early rather "
            "than after quoting, so the dealer is not led on."
            % profile["overdue_amount"]
        )
    if profile.get("status") != "active":
        lines.append("- Account status: %s. Orders are blocked." % profile.get("status"))
    return "\n".join(lines)
