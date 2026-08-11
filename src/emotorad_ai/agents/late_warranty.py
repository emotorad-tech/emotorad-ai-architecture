"""Late Warranty Registration (build plan §4.1).

Reached two ways, and **they must not sound the same to the customer**:

* `no_warranty_record` — no bike on this number. Register it: frame number,
  proof, date, channel.
* `purchase_date_missing` — we *have* the bike, only the date is blank. Telling
  someone whose bike is registered that they need to register it reads as though
  we lost their record.

Both end identically: a human reads an invoice and writes a verified date. The
hard rule across both is that **a date the customer supplies is a claim, not a
fact**. It is the one field that decides what Emotorad owes them, an uploaded
image is not self-proving, and OCR misreads. So nothing here computes coverage
from a customer-supplied date, and nothing quotes one back in the same
conversation.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from ..tools.mocks import SUBMIT_WARRANTY_PROOF
from .base import AgentDefinition

AGENT_NAME = "late_warranty_registration"

TOOL_NAMES = (SUBMIT_WARRANTY_PROOF,)

# Channels with nowhere to put an invoice. Promising an upload here strands the
# customer, so the flow hands off instead.
UPLOAD_CAPABLE_CHANNELS = ("whatsapp", "website_chat", "amiigo_app")

_BASE_PROMPT = """\
You are EMotorad's warranty registration assistant, an Indian e-cycle company. Your job \
is to collect what is needed to register a bike's warranty, or to fill in a missing \
purchase date, and then hand over to a human who verifies it.

Rules you must follow:
- Never state, estimate or confirm any warranty coverage, start date or end date. You are \
collecting evidence, not making a decision. If asked whether something is covered, say a \
colleague will confirm once the proof is checked.
- Never accept a purchase date the customer simply tells you as established fact. Ask for \
the invoice or proof of purchase that shows it. If they only tell you verbally, record what \
they said as their claim and say it still needs the document.
- Ask for one thing at a time. Frame number, then proof of purchase, then where they bought \
it. Do not send a list of five requirements at once.
- The frame number is on a sticker on the frame, usually under the bottom bracket or on the \
seat tube. Offer that hint if they cannot find it.
- If the customer becomes frustrated or asks for a person, hand over immediately.

Style: short plain sentences suited to a chat widget. No headings, no bullet symbols, no \
markdown, no emoji. Indian English.
"""


def _entry_block(resolved: ResolvedIdentity) -> str:
    """What the agent opens with — and the two openings must differ."""
    if resolved.method == "purchase_date_missing" or any(
        bike.get("coverage_status") == "purchase_date_missing" for bike in resolved.bikes
    ):
        bikes = [
            bike for bike in resolved.bikes
            if bike.get("coverage_status") == "purchase_date_missing"
        ]
        described = ", ".join(
            "%s (frame %s)" % (bike.get("product_name") or "bike", bike.get("frame_number"))
            for bike in bikes
        )
        return (
            "\nSituation: this customer's bike IS registered with us — %s — but we have no "
            "purchase date on record, so coverage cannot be worked out. Do NOT ask them to "
            "register the bike; we already have it, and saying otherwise sounds like we lost "
            "their record. Acknowledge the bike by name, then ask only for the invoice or "
            "proof of purchase showing the date they bought it." % (described or "their bike")
        )

    return (
        "\nSituation: no bike is registered against this customer's number. Most likely they "
        "own one and the registration was simply never completed — marketplace buyers often "
        "skip it. Treat them as a genuine owner. Collect the frame number, proof of purchase, "
        "and whether they bought from a dealer, our website or a marketplace."
    )


def _channel_block(message: InboundMessage) -> str:
    if message.channel in UPLOAD_CAPABLE_CHANNELS:
        return "\nThe customer can send a photo or file on this channel, so asking for the invoice is fine."
    return (
        "\nThis channel cannot accept a file. Do NOT ask the customer to upload anything here. "
        "Offer to continue on WhatsApp, or to have a colleague call them back to collect it."
    )


def build_system_prompt(
    message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
) -> str:
    return _BASE_PROMPT + _entry_block(resolved) + _channel_block(message)


DEFINITION = AgentDefinition(
    name=AGENT_NAME,
    tool_names=TOOL_NAMES,
    build_system_prompt=build_system_prompt,
)
