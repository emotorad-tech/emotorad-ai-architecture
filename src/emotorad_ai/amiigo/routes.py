"""The Amiigo app's HTTP routes under /amiigo/v1/ (docs/contracts/amiigo-support-chat.md).

Included by api.py, which puts the process's `AmiigoContext` on
`app.state.amiigo` (common.py). Every route takes the rider from the token
(`require_rider`), and every answer is `Cache-Control: no-store`
(`common.NoStoreMiddleware`).

History: `GET /conversations` and `GET /conversations/{conversation_id}/messages`
(history.py), 60 requests a minute per rider across both. Logged as
`amiigo_history_list` and `amiigo_history_messages` with the outcome, the
count and the rider's hash; never a phone, a token, text or a link.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError

from ..conversation import StoreUnavailable
from . import history
from .auth import Rider
from .common import (
    CONVERSATION_NOT_FOUND,
    CURSOR_INVALID,
    HISTORY_UNAVAILABLE,
    PREFIX,
    RATE_LIMITED,
    AmiigoContext,
    amiigo_error,
    context_of,
    require_rider,
)

router = APIRouter(prefix=PREFIX)

# The places a chat in the history happens ("Which chats are in the
# history"): the app only in v1 (the plan's Ruling 6). Anything else is 422.
Channel = Literal["amiigo_app"]


def history_rider(request: Request, rider: Rider = Depends(require_rider)) -> Rider:
    """The rider, within their history allowance."""
    context = context_of(request)
    if not context.history_limiter.allow(rider):
        context.log.emit("amiigo_rate_limited", "amiigo", rider_hash=rider.rider_hash, limit="history")
        raise amiigo_error(429, RATE_LIMITED)
    return rider


def _logged(context: AmiigoContext, event: str, slot: str, rider: Rider, outcome: str, count: int = 0,
            **fields: Any) -> None:
    context.log.emit(event, slot, outcome=outcome, count=count, rider_hash=rider.rider_hash, **fields)


@router.get("/conversations")
def get_conversations(
    request: Request,
    rider: Rider = Depends(history_rider),
    limit: int = Query(20, ge=1, le=50),
    cursor: Optional[str] = None,
    channel: Optional[Channel] = None,
) -> Dict[str, Any]:
    context = context_of(request)
    event = "amiigo_history_list"
    try:
        body = history.list_conversations(context.stores, rider, limit=limit, cursor=cursor, channel=channel,
                                          now=context.clock())
    except history.CursorInvalid:
        _logged(context, event, "amiigo", rider, "cursor_invalid")
        raise amiigo_error(400, CURSOR_INVALID) from None
    except StoreUnavailable as exc:
        _logged(context, event, "amiigo", rider, "store_unavailable", error=str(exc))
        raise amiigo_error(503, HISTORY_UNAVAILABLE) from None
    _logged(context, event, "amiigo", rider, "ok", count=len(body["conversations"]))
    return body


@router.get("/conversations/{conversation_id}/messages")
def get_messages(
    request: Request,
    conversation_id: str,
    rider: Rider = Depends(history_rider),
    limit: int = Query(50, ge=1, le=100),
    before: Optional[str] = None,
    after: Optional[str] = None,
) -> Dict[str, Any]:
    if before and after:
        raise RequestValidationError([{
            "type": "value_error", "loc": ("query", "after"), "input": after,
            "msg": "Value error, send before or after, not both",
        }])
    context = context_of(request)
    event = "amiigo_history_messages"
    now = context.clock()
    try:
        body = history.list_messages(
            context.stores, rider, conversation_id, limit=limit, before=before, after=after, now=now,
            signer=history.signer_for(context.media_store, now, context.log))
    except history.ConversationNotFound:
        # The id is whatever the app sent: not logged.
        _logged(context, event, "amiigo", rider, "not_found")
        raise amiigo_error(404, CONVERSATION_NOT_FOUND) from None
    except history.CursorInvalid:
        _logged(context, event, conversation_id, rider, "cursor_invalid")
        raise amiigo_error(400, CURSOR_INVALID) from None
    except StoreUnavailable as exc:
        _logged(context, event, "amiigo", rider, "store_unavailable", error=str(exc))
        raise amiigo_error(503, HISTORY_UNAVAILABLE) from None
    _logged(context, event, conversation_id, rider, "ok", count=len(body["messages"]))
    return body
