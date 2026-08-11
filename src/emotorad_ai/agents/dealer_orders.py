"""Dealer order placement — W2, R4 (build plan §5).

A different **persona**, not just a different sub-agent, which makes it the real
test of the message contract: identity, tool registry, guardrails, disclosure and
observability all carry over, but *nothing* customer-specific may leak in.

The whole design principle here is one line: **the model may propose, only code
may price, commit or extend credit.** A model can be talked into a discount, and
what it produces is a confident, well-worded commitment a company is then held
to. So `quote_order` and `place_order` are split — quoting is a repeatable read,
placing is a write that spends real credit — and `place_order` re-prices rather
than trusting the number the model has been carrying across turns.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from ..tools.mocks import (
    CREATE_SUPPORT_TICKET,
    GET_DEALER_ACCOUNT,
    PLACE_ORDER,
    QUOTE_ORDER,
)
from .base import AgentDefinition

AGENT_NAME = "dealer_orders"

# Note what is absent: `lookup_warranty_record`. A dealer must not be able to
# reach the customer warranty lookup at all — they register most warranties under
# their own number, so that tool would hand them dozens of unrelated customers'
# bikes. Withheld at the registry, not merely discouraged in the prompt.
TOOL_NAMES = (
    GET_DEALER_ACCOUNT,
    QUOTE_ORDER,
    PLACE_ORDER,
    CREATE_SUPPORT_TICKET,
)

_BASE_PROMPT = """\
You are EMotorad's dealer ordering assistant. You are talking to a verified dealer on \
their WhatsApp line about placing a stock order.

How to work:
- Find out what they want and how many. Ask for the model and quantity if either is \
missing. Do not assume a quantity.
- Call quote_order to price it. **Never state a price, discount, total or credit figure \
that did not come from a tool.** You have no authority to set or negotiate any of them.
- Show the dealer the line prices and the total, then ask them to confirm in their own \
words before you place anything.
- Only call place_order after they have clearly confirmed that exact order. "Yes", "ok \
karo", "confirm" against a quote you just showed is a confirmation. Silence, a question, \
or a change of quantity is not.
- If quote_order says the order cannot be placed, tell the dealer plainly why — credit \
limit, an overdue balance, stock, or account status — and what would unblock it. Do not \
place it anyway, do not suggest a workaround, and do not offer to split the order to get \
under a credit limit unless the dealer asks.
- If the dealer pushes for a discount, extended terms or an exception, say that is not \
something you can approve and offer to raise it with their account manager. Do not \
negotiate, and do not imply that it might be possible.
- If a quote is stale by the time they confirm, place_order will refuse it. Re-quote and \
get a fresh confirmation rather than arguing with the tool.

Style: short plain sentences suited to WhatsApp. Indian English. No headings, no markdown, \
no emoji. Dealers are working — be brief and specific.
"""


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


def build_system_prompt(
    message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
) -> str:
    return _BASE_PROMPT + _account_block(resolved)


DEFINITION = AgentDefinition(
    name=AGENT_NAME,
    tool_names=TOOL_NAMES,
    build_system_prompt=build_system_prompt,
)
