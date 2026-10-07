"""The narrow support agent: one known issue, one record, a short prompt.

Reached only when Jev has confidently named the issue (decisions.route). It
runs through the same Agent loop as every other sub-agent, with the same
iteration cap, stuck detection, idempotency and post-checks, but it is handed
the one knowledge record that applies instead of searching, and the lookups
the runtime already made instead of making them itself. That is where the
saving is: a prompt a fraction of the size, on a cheaper model, in fewer
round trips.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from ..knowledge import KnowledgeRecord
from ..media import model_offered
from ..tools.mocks import (
    BOOK_SERVICE_SLOT,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    GET_RECENT_TRIPS,
    GET_SERVICE_STATUS,
    SEND_GUIDE_MEDIA,
)
from .base import AgentDefinition
from .battery_support import _context_block, _entry_block, _facts_block

AGENT_NAME = "narrow_support"

# No lookups: the record is given and the reads were prefetched. The writes a
# troubleshooting flow ends in stay, requested by the model and enforced by
# the registry exactly as in the full agents. The app's two reads stay too:
# "when is my next service due?" mid-flow got "I don't have your service
# schedule" (staging, 2026-10-01). Without Amigo they are not registered,
# and the agent drops them.
TOOL_NAMES = (SEND_GUIDE_MEDIA, CREATE_SUPPORT_TICKET, FIND_SERVICE_SLOTS, BOOK_SERVICE_SLOT,
              GET_SERVICE_STATUS, GET_RECENT_TRIPS)

_RULES = """\
You are EMotorad's support assistant. The customer's issue has already been identified, \
and the documented steps for it are below. Be warm, plain-spoken and brief.

Rules that always apply:
- Work through the documented steps, one or two at a time. Do not invent steps that are not listed.
- Ask at most one or two questions at a time, and only when the answer changes what you would suggest.
- Never say a repair or part is covered, free or chargeable unless the warranty result below says so for this bike.
- Do not conclude that a part is dead or faulty, and do not raise a ticket for a fault, until the customer \
has sent a photo or video of it.
- When you need to see something, ask for a short video first, and a photo only if they cannot take one.
- If the customer describes smoke, swelling, heat, sparks or a burning smell, tell them to stop using and \
stop charging the bike now.
- If the documented steps do not resolve it, say so plainly and offer to raise a support ticket.
- Reply in the language the customer writes in."""


def _record_block(record: KnowledgeRecord, sendable: Optional[Mapping[str, Mapping[str, Any]]] = None) -> str:
    lines = ["\n\nThe documented steps for this issue (%s):" % record.title]
    for number, step in enumerate(record.steps, start=1):
        lines.append("%d. %s" % (number, step))
    if record.escalate_when:
        lines.append("Escalate when: %s" % record.escalate_when)
    # Only a picture this server can send, and by the catalogue key the tool
    # takes. The record names its media by file path, which the tool rejects,
    # and listing a picture that cannot be sent is how the bot came to offer
    # one it did not have (2026-09-29). Never a code-only picture (the melt
    # ask's, 6 October 2026): no model is offered one.
    key_for = {item.get("id"): key for key, item in model_offered(sendable or {}).items() if item.get("id")}
    media = [(key_for[item["id"]], item) for item in record.media if item.get("id") in key_for]
    if media:
        lines.append("Guide media you can send with send_guide_media, by key:")
        for key, item in media:
            lines.append("- %s: %s" % (key, item.get("caption", "")))
    return "\n".join(lines)


def _prefetched_block(prefetched: Sequence[Dict[str, Any]]) -> str:
    if not prefetched:
        return ""
    lines = ["\n\nLooked up for this turn (treat as fact, and do not look these up again):"]
    for call in prefetched:
        lines.append("- %s: %s" % (call["tool"], json.dumps(call["result"], default=str, ensure_ascii=False)))
    return "\n".join(lines)


def build_narrow_definition(
    record: KnowledgeRecord,
    prefetched: Sequence[Dict[str, Any]],
    sendable: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> AgentDefinition:
    frozen: List[Dict[str, Any]] = list(prefetched)

    def build_system_prompt(message: InboundMessage, resolved: ResolvedIdentity, context: str = "") -> str:
        return (
            _RULES
            + _facts_block(resolved)
            + _context_block(context)
            + _entry_block(message)
            + _record_block(record, sendable)
            + _prefetched_block(frozen)
        )

    return AgentDefinition(name=AGENT_NAME, tool_names=TOOL_NAMES, build_system_prompt=build_system_prompt)
