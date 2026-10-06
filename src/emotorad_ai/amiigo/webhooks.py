"""The Zoho Desk webhook: `POST /webhooks/zoho/tickets/<secret>`
(docs/contracts/amiigo-support-chat.md, "For the server team: the Zoho Desk
webhook"; the plan's Task 6 and Ruling 19).

Zoho Desk calls it for its `Ticket_Update` event when support changes a
ticket. A closure goes to `close_ticket` (tickets.py); anything else is
ignored in v1.

Authentication. Zoho Desk's webhooks take no custom header (its knowledge
base: only open webhooks that need no authentication), so the secret, from
EMOTORAD_ZOHO_WEBHOOK_SECRET, is the last segment of the path the webhook is
set up with, compared in constant time; never a query parameter. It is 32 to
256 letters, digits, `-` or `_` (`secrets.token_urlsafe(32)` makes one); any
other value counts as not configured. The path is in uvicorn's access log, so
`hide_secret_in_access_log` writes it there as `/webhooks/zoho/tickets/[secret]`.
A proxy in front (nginx on staging) logs the path too unless told not to.
Zoho also signs every request with a JWT in `X-ZDesk-JWT` (RS256, checked
against Zoho's published keys); that is not checked here yet.

The payload (docs/api-shapes/zoho-webhook-ticket-update.json, from Zoho's
documentation): a JSON list of events, each `{"eventType", "payload",
"prevState", "eventTime", "orgId"}`. Only these are read: `eventType`,
`payload.id` (the Zoho ticket id), `payload.statusType` (Closed is a
closure; `status` is a name each department chooses, so it is never read),
`payload.closedTime`, and `eventTime` when the ticket carries no closing time.

Answers (Ruling 19): 200 for every event handled, whatever its outcome, and
for a body it cannot read (logged); 401 `secret_invalid` for a wrong or
missing secret, before the body is read; 503 `not_configured` without a
secret; 503 `store_unavailable` when the store fails, so Zoho may try again
(every step is safe to repeat). Zoho counts anything but a 200 within 5
seconds as a failed delivery, but documents no retry: a closure the store
could not record may never come back, so each one is also logged as
`zoho_webhook_store_unavailable`, which infra/zoho-alarms.yaml alarms on at
once (Ruling 25).

Logs: `zoho_webhook` with the `outcome` and `ticket_hash`, the first 12 hex
characters of the SHA-256 of the Zoho ticket id; never the payload, the id
or the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

import anyio
from fastapi import APIRouter, HTTPException, Request

from ..conversation import StoreUnavailable
from ..zoho.settings import WEBHOOK_SECRET as SECRET_ENV
from . import tickets
from .common import context_of

WEBHOOK_PATH = "/webhooks/zoho/tickets"
# An unauthenticated caller is never read, and an authenticated one is read
# to this size at most. A documented event is about 6 KB.
MAX_BODY_BYTES = 2 * 1024 * 1024

# /health's words (api.py).
ON = "on"
NOT_CONFIGURED = "not configured"
MISCONFIGURED = "misconfigured: secret must be 32 to 256 letters, digits, - or _"

# The answers' details.
SECRET_INVALID = "secret_invalid"
UNAVAILABLE = "not_configured"
STORE_UNAVAILABLE = "store_unavailable"

# Outcomes logged besides close_ticket's.
IGNORED = "ignored"
UNPARSABLE = "unparsable"
TOO_LARGE = "too_large"

TICKET_UPDATE = "Ticket_Update"
# Zoho's statusType for a closed ticket: CLOSED in the Tickets API's list,
# Closed in its answers and in the webhook's sample.
CLOSED_STATUS_TYPE = "CLOSED"

# A secret as configured: URL-safe ASCII, so it is one path segment as typed.
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,256}")
# A Zoho id, and a time in milliseconds: ASCII digits only.
_DIGITS = re.compile(r"[0-9]{1,30}")
_MILLIS = re.compile(r"[0-9]{1,15}")


def webhook_secret_from_env(environ: Mapping[str, str] = os.environ) -> Optional[str]:
    """The webhook's secret, or None when it is not set or not one we accept."""
    value = (environ.get(SECRET_ENV) or "").strip()
    return value if _SECRET.fullmatch(value) else None


def webhook_status(environ: Mapping[str, str] = os.environ) -> str:
    """For /health: whether the webhook can be called; never the secret."""
    if webhook_secret_from_env(environ) is not None:
        return ON
    return MISCONFIGURED if (environ.get(SECRET_ENV) or "").strip() else NOT_CONFIGURED


def ticket_hash(zoho_ticket_id: str) -> str:
    """How logs name a Zoho ticket: never its id."""
    return hashlib.sha256(zoho_ticket_id.encode("utf-8")).hexdigest()[:12]


# -- the access log -----------------------------------------------------------------


class HideWebhookSecret(logging.Filter):
    """uvicorn's access log line for the webhook, with the secret replaced."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn's access record: (client, method, path with query, HTTP version, status).
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and args[2].startswith(WEBHOOK_PATH + "/"):
            record.args = args[:2] + (WEBHOOK_PATH + "/[secret]",) + args[3:]
        return True


def hide_secret_in_access_log(logger: Optional[logging.Logger] = None) -> None:
    """Put the filter on uvicorn's access logger, once."""
    access = logger or logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HideWebhookSecret) for f in access.filters):
        access.addFilter(HideWebhookSecret())


# -- the payload -----------------------------------------------------------------------


class Unparsable(ValueError):
    """A body that is not a JSON list of events."""


@dataclass(frozen=True)
class WebhookEvent:
    """One event of a delivery: a ticket's change, or why it is skipped
    (`skip`: "ignored" for another kind of event, "unparsable")."""

    skip: Optional[str] = None
    zoho_ticket_id: Optional[str] = None
    closed: bool = False
    closed_at: Optional[datetime] = None


def _iso_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.isascii():
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _millis_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not _MILLIS.fullmatch(value):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _ticket_id(value: Any) -> Optional[str]:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        value = str(value)
    return value if isinstance(value, str) and _DIGITS.fullmatch(value) else None


def _event(item: Any) -> WebhookEvent:
    if not isinstance(item, dict) or not isinstance(item.get("eventType"), str):
        return WebhookEvent(skip=UNPARSABLE)
    if item["eventType"] != TICKET_UPDATE:
        return WebhookEvent(skip=IGNORED)
    payload = item.get("payload")
    zoho_ticket_id = _ticket_id(payload.get("id")) if isinstance(payload, dict) else None
    if zoho_ticket_id is None:
        return WebhookEvent(skip=UNPARSABLE)
    status_type = payload.get("statusType")
    closed = isinstance(status_type, str) and status_type.replace(" ", "").upper() == CLOSED_STATUS_TYPE
    closed_at = (_iso_time(payload.get("closedTime")) or _millis_time(item.get("eventTime"))) if closed else None
    return WebhookEvent(zoho_ticket_id=zoho_ticket_id, closed=closed, closed_at=closed_at)


def parse_events(raw: bytes) -> List[WebhookEvent]:
    """The events of one delivery, in order. Raises Unparsable for a body
    that is not a JSON list."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unparsable() from None
    if not isinstance(body, list):
        raise Unparsable()
    return [_event(item) for item in body]


# -- the route ---------------------------------------------------------------------------

router = APIRouter()


def _logged(context: Any, outcome: str, zoho_ticket_id: Optional[str] = None, **fields: Any) -> None:
    if zoho_ticket_id is not None:
        fields["ticket_hash"] = ticket_hash(zoho_ticket_id)
    context.log.emit("zoho_webhook", "zoho", outcome=outcome, **fields)


def _refused(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers={"Cache-Control": "no-store"})


def _handle(context: Any, events: List[WebhookEvent]) -> None:
    """Every event, in order, in a worker thread: the stores block. Stops at
    the first store failure, which the caller answers 503."""
    for event in events:
        if event.skip is not None:
            _logged(context, event.skip, event.zoho_ticket_id)
            continue
        if not event.closed:
            _logged(context, IGNORED, event.zoho_ticket_id)
            continue
        try:
            outcome = tickets.close_ticket(context.stores, context.sockets, zoho_ticket_id=event.zoho_ticket_id,
                                           closed_at=event.closed_at, now=context.clock(), log=context.log)
        except StoreUnavailable as exc:
            _logged(context, STORE_UNAVAILABLE, event.zoho_ticket_id, error=str(exc))
            # One closure the rider may never hear about: Zoho documents no
            # retry. Alarmed on its own (Ruling 25).
            context.log.emit("zoho_webhook_store_unavailable", "zoho", ticket_hash=ticket_hash(event.zoho_ticket_id),
                             error=str(exc))
            raise
        _logged(context, outcome, event.zoho_ticket_id)


async def _read(request: Request) -> Optional[bytes]:
    """The body, or None once it is over MAX_BODY_BYTES."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _answer(request: Request, given: Optional[str]) -> dict:
    context = context_of(request)
    secret = context.zoho_webhook_secret
    if not secret:
        _logged(context, UNAVAILABLE)
        raise _refused(503, UNAVAILABLE)
    if given is None or not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8")):
        _logged(context, SECRET_INVALID)
        raise _refused(401, SECRET_INVALID)
    raw = await _read(request)
    if raw is None:
        _logged(context, TOO_LARGE)
        return {"status": "ok"}
    try:
        events = parse_events(raw)
    except Unparsable:
        _logged(context, UNPARSABLE)
        return {"status": "ok"}
    try:
        await anyio.to_thread.run_sync(_handle, context, events)
    except StoreUnavailable:
        raise _refused(503, STORE_UNAVAILABLE) from None
    return {"status": "ok"}


@router.post(WEBHOOK_PATH)
async def zoho_tickets_without_secret(request: Request) -> dict:
    return await _answer(request, None)


@router.post(WEBHOOK_PATH + "/{secret}")
async def zoho_tickets(request: Request, secret: str) -> dict:
    return await _answer(request, secret)
