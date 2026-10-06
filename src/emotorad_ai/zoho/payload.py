"""What goes on a Zoho Desk ticket (spec 2026-10-05, section 5).

These are pure functions over a ticket record (tickets/record.py) and the
settings: no network, no store. Code chooses every field Zoho routes on: the
department, priority, status, channel and contact. Model text reaches only
the description, under a heading that says the AI wrote it and nobody
checked it. The customer's own claims reach it too, redacted, under a heading
that says they are claims.

The Desk has no room for custom fields, so the payload has no `cf` at all.
The chat reference ends the subject instead, in square brackets, and
zoho/desk.find_adoptable finds an earlier attempt's ticket by that suffix.
The phone belongs only on the contact and the ticket's phone field: it is
never in the subject or the description.

Never set here: the assignee, the team, a due date (the OMS sends IST time
marked as UTC), the deprecated customFields, or any of the OMS's dealer
fields. "Dealer Principle Name" matters most, because the OMS webhook uses it
to copy Zoho tickets into the OMS (em-biz-backend zoho/views.py).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..conversation import TranscriptTurn, render_transcript
from ..observability import redact_pii
from ..tickets.kinds import is_urgent, subject_label
from .errors import ZohoConfigError
from .settings import ZohoSettings

SUBJECT_LIMIT = 255
# Zoho allows 65,535 (v1.0/Ticket.json). We keep a margin, as for comments.
DESCRIPTION_LIMIT = 60000
# Zoho allows 32,000 a comment (https://desk.zoho.com/DeskAPIDocument#TicketsComments).
TRANSCRIPT_LIMIT = 30000
CLAIM_LIMIT = 500
SOURCE = "AI chatbot"
STATUS = "Open"
NO_BIKE = "bike not given"
# Every subject contains this. An unverified ticket's subject puts
# UNVERIFIED_PREFIX before it, so a Zoho rule or webhook criterion filters on
# the subject containing "[AI chat]", never on "starts with".
SUBJECT_PREFIX = "[AI chat] "
UNVERIFIED_PREFIX = "[Unverified] "

# The kinds whose summary the model wrote (spec section 3's table). The others
# are built by code, and their description says what built them.
MODEL_RAISED = ("support", "intake", "warranty_proof")
AI_HEADING = "Summary written by the AI from the customer's words, not checked:"
CODE_HEADINGS = {
    "safety": ("Safety report, recorded by code from the customer's words, the safety terms matched and what "
               "the photo or video check saw:"),
    "handover": "Recorded by code when the customer asked for a person:",
    "lockout": "Recorded by code when the customer could not verify their number:",
}
CODE_HEADING = "Recorded by code:"
EMPTY_SUMMARIES = {
    "handover": "Customer asked for a person.",
    "lockout": "The customer could not verify their number in this chat.",
}
TAKEOVER_LINE = ("Possible takeover attempt: verify only through the number on record, "
                 "never a number from this chat.")
VERIFIED = "Identity: verified"
UNVERIFIED = "Identity: number given in chat, not verified"
UTC_LINE = "Chat with the AI chatbot for %s. Times are in UTC."
CUT = "\n[cut to fit Zoho's limit]"
COVERAGE_PREFIX = "Warranty, from our systems, not the AI:"
# A support or handover ticket raised after the evidence check passed
# (evidence_check.py). Written by code from Gemini's one sentence, redacted.
EVIDENCE_LINE = "Evidence checked by Gemini: %s. Shows the problem and matches the complaint."
EVIDENCE_LIMIT = 200

CHANNELS = {"whatsapp": "WhatsApp", "amiigo_app": "Amiigo app", "website_chat": "website chat", "voice": "phone call"}
# What each coverage outcome means to a person reading the ticket. The code
# follows in brackets, so the line can be searched.
COVERAGE_TEXT = {
    "computed": "worked out from the purchase date on record",
    "computed_from_registration": "worked out from the registration date, as no purchase date is on record",
    "purchase_date_missing": "registered, with no purchase or registration date on record",
    "not_registered": "in the EMotorad app, not registered for warranty",
    "warranty_unknown": "in the EMotorad app with no frame number, so its warranty record cannot be told",
    "warranty_unavailable": "the warranty system did not answer",
    "no_warranty_record": "no warranty record for this number",
    "oms_unavailable": "the warranty system did not answer",
}
# The customer's claims, in the order a person reads them. Nothing else in
# `claims` reaches Zoho.
CLAIM_LABELS = (
    ("stated_name", "Name they gave"),
    ("stated_contact", "Contact they gave"),
    ("evidence", "Evidence they offered"),
    ("claimed_purchase_date", "Purchase date they gave"),
    ("purchase_channel", "Where they say they bought it"),
)

_NOT_DIGITS = re.compile(r"[^0-9]")
_INDIAN_MOBILE = re.compile(r"[6-9][0-9]{9}")


def _one_line(value: Any) -> str:
    """A value as one line: a newline in quoted text must not break the
    subject or a labelled line."""
    return " ".join(str(value).split()) if value is not None else ""


def _bike_model(bike: Any) -> str:
    if not isinstance(bike, dict):
        return ""
    # tickets/seam.py maps the tools' bike_model onto the record's bike.
    return _one_line(bike.get("model") or bike.get("bike_model"))


def _channel(channel: Optional[str]) -> str:
    if not channel:
        return "not recorded"
    return CHANNELS.get(channel, _one_line(channel))


def _kind_line(kind: str, category: Optional[str]) -> str:
    line = "Kind: %s" % kind
    if category:
        line += ". Category: %s" % _one_line(category)
    return line


def _bike_line(bike: Any) -> Optional[str]:
    if not isinstance(bike, dict):
        return None
    model = _bike_model(bike)
    frame = _one_line(bike.get("frame_number"))
    if not (model or frame):
        return None
    line = "Bike: %s" % (model or "model not known")
    if frame:
        source = _one_line(bike.get("frame_number_source"))
        line += ", frame number %s" % frame + (" (%s)" % source if source else "")
    return line


def _coverage_line(coverage: Optional[str]) -> str:
    if not coverage:
        return "%s no result recorded in this chat" % COVERAGE_PREFIX
    code = _one_line(coverage)
    if code in COVERAGE_TEXT:
        return "%s %s (%s)" % (COVERAGE_PREFIX, COVERAGE_TEXT[code], code)
    return "%s %s" % (COVERAGE_PREFIX, code)


def _claim_lines(claims: Any) -> List[str]:
    """The customer's claims, one line each. Each value is redacted first and
    cut second, so a number typed into a claim is hidden whole and never left
    as half a number at the cut. A typed phone or email belongs on the contact,
    not in text a whole support team reads."""
    if not isinstance(claims, dict):
        return []
    lines = []
    for key, label in CLAIM_LABELS:
        value = redact_pii(_one_line(claims.get(key)))[:CLAIM_LIMIT]
        if value:
            lines.append("- %s: %s" % (label, value))
    return lines


def plus91(phone: Optional[str]) -> Optional[str]:
    """The number in +91 form, or None for anything but an Indian mobile. The
    worker (zoho/worker.py) searches and makes contacts with the same form."""
    digits = _NOT_DIGITS.sub("", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return "+91" + digits if _INDIAN_MOBILE.fullmatch(digits) else None


def subject(record: Dict[str, Any]) -> str:
    """`[AI chat] <label> - <bike model> [<chat reference>]`, with
    "[Unverified] " first when the number was not verified. No phone, no name,
    at most 255 characters.

    The chat reference is how an earlier attempt's ticket is found again, so
    it is never cut: only the label and bike part give way. A reference so
    long that nothing is left for them is refused, not shortened.
    """
    label = _one_line(subject_label(record["kind"], record.get("category")))
    middle = "%s - %s" % (label, _bike_model(record.get("bike")) or NO_BIKE)
    prefix = SUBJECT_PREFIX if record.get("identity") == "verified" else UNVERIFIED_PREFIX + SUBJECT_PREFIX
    suffix = " [%s]" % record["chat_reference"]
    room = SUBJECT_LIMIT - len(prefix) - len(suffix)
    if room < 1:
        # The message names the limit and not the reference.
        raise ZohoConfigError("the chat reference leaves no room in a %d-character subject" % SUBJECT_LIMIT,
                              error="subject_too_long")
    # rstrip: a cut just after a space would leave two before the suffix.
    return prefix + middle[:room].rstrip() + suffix


def description(record: Dict[str, Any]) -> str:
    """The ticket's description, as plain text in the spec's order (section 5)."""
    kind = record["kind"]
    by_model = kind in MODEL_RAISED
    lines = [
        "Reference: %s" % record["_id"],
        "Source: %s" % SOURCE,
        "Channel: %s" % _channel(record.get("channel")),
        VERIFIED if record.get("identity") == "verified" else UNVERIFIED,
        _kind_line(kind, record.get("category")),
    ]
    bike = _bike_line(record.get("bike"))
    if bike:
        lines.append(bike)
    lines.append(_coverage_line(record.get("coverage")))
    if by_model and record.get("ai_severity"):
        # Only the model's own tickets: a safety ticket's severity is set by
        # code, and calling it the AI's view would be untrue.
        lines.append("AI's view of severity: %s" % _one_line(record["ai_severity"]))
    seen = redact_pii(_one_line(record.get("evidence_check")))[:EVIDENCE_LIMIT].rstrip(" .")
    if seen:
        lines.append(EVIDENCE_LINE % seen)
    claims = _claim_lines(record.get("claims"))
    if claims:
        lines += ["", "Customer's claims, not checked:"] + claims
    heading = AI_HEADING if by_model else CODE_HEADINGS.get(kind, CODE_HEADING)
    before = "\n".join(lines + ["", heading, ""])
    after = "\n\n" + TAKEOVER_LINE if kind == "lockout" else ""
    summary = (record.get("summary") or "").strip() or EMPTY_SUMMARIES.get(kind, "(none given)")
    # Only the summary is ever cut, so the lines above it and the takeover
    # warning below it always arrive.
    room = DESCRIPTION_LIMIT - len(before) - len(after)
    if len(summary) > room:
        summary = summary[: max(room - len(CUT), 0)] + CUT
    return before + summary + after


def ticket_payload(record: Dict[str, Any], settings: ZohoSettings, contact_id: str) -> Dict[str, Any]:
    """The body of POST /api/v1/tickets. Only the fields in section 5, and no
    `cf`: there are no custom fields to carry the chat reference."""
    if record.get("mode") == "live":
        # A live record goes to the real department only under live settings.
        # Test settings that happen to name the real department never send there.
        if not (settings.live and settings.department_id):
            raise ZohoConfigError("a live record cannot be sent under test settings", error="mode_mismatch")
        department = settings.department_id
    else:
        department = settings.test_department_id
    urgent = bool(record.get("urgent")) or is_urgent(record["kind"], record.get("category"))
    payload: Dict[str, Any] = {
        "subject": subject(record),
        "departmentId": department,
        "contactId": contact_id,
        "priority": settings.priority_high if urgent else settings.priority_medium,
        "status": STATUS,
        # A system channel from the settings, never an integration channel,
        # which would carry its own reply route. Our channel is in the description.
        "channel": settings.channel,
        "description": description(record),
    }
    if settings.layout_id:
        # Only when the department has more than one layout and the person has
        # chosen one (EMOTORAD_ZOHO_LAYOUT_ID).
        payload["layoutId"] = settings.layout_id
    phone = plus91(record.get("phone"))
    if phone:
        payload["phone"] = phone
    return payload


def _marker(chat_reference: str, reference: str, first: int, last: int, opening: bool,
            part: Optional[int] = None) -> str:
    text = "[%s transcript, turns %d-%d%s]" % (chat_reference, first, last, ", part %d" % part if part else "")
    return text + "\n" + UTC_LINE % reference if opening else text


def _chunk(chat_reference: str, reference: str, group: List[TranscriptTurn], opening: bool) -> Tuple[str, List[int]]:
    head = _marker(chat_reference, reference, group[0].n, group[-1].n, opening)
    return head + "\n" + render_transcript(group), [turn.n for turn in group]


def _parts(chat_reference: str, reference: str, turn: TranscriptTurn, opening: bool,
           limit: int) -> List[Tuple[str, List[int]]]:
    """One turn too long for a comment, cut into parts. Only the last part
    lists the turn, so a resume after an early part posts the rest."""
    line = render_transcript([turn])
    parts: List[Tuple[str, List[int]]] = []
    part = 1
    while line:
        head = _marker(chat_reference, reference, turn.n, turn.n, opening and part == 1, part)
        room = limit - len(head) - 1
        if room < 1:
            raise ValueError("a transcript comment limit of %d leaves no room after its marker" % limit)
        piece, line = line[:room], line[room:]
        parts.append((head + "\n" + piece, [] if line else [turn.n]))
        part += 1
    return parts


def transcript_chunks(reference: str, chat_reference: str, turns: Sequence[TranscriptTurn],
                      limit: int = TRANSCRIPT_LIMIT) -> List[Tuple[str, List[int]]]:
    """The turns as private comments, each at most `limit` characters.

    They are split on turn boundaries. Each comment starts with a marker such
    as "[stage:EM-1000001 transcript, turns 7-12]", so a resume that lists the
    ticket's comments can tell what is already there. The first comment says
    the times are in UTC, as render_transcript prints them. Each result is
    (text, turn numbers in it); the worker adds those to posted_turns once the
    comment is posted. The same turns always give the same comments.
    """
    ordered = sorted(turns, key=lambda turn: turn.n)
    chunks: List[Tuple[str, List[int]]] = []
    group: List[TranscriptTurn] = []
    size = 0  # the group's rendered lines, with the newlines between them
    for turn in ordered:
        line = len(render_transcript([turn]))
        if group:
            grown = size + 1 + line
            if len(_marker(chat_reference, reference, group[0].n, turn.n, not chunks)) + 1 + grown <= limit:
                group.append(turn)
                size = grown
                continue
            chunks.append(_chunk(chat_reference, reference, group, not chunks))
            group, size = [], 0
        if len(_marker(chat_reference, reference, turn.n, turn.n, not chunks)) + 1 + line <= limit:
            group, size = [turn], line
            continue
        chunks.extend(_parts(chat_reference, reference, turn, not chunks, limit))
    if group:
        chunks.append(_chunk(chat_reference, reference, group, not chunks))
    return chunks
