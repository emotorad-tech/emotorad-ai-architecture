"""The Amiigo app's HTTP routes under /amiigo/v1/ (docs/contracts/amiigo-support-chat.md).

Included by api.py, which puts the process's `AmiigoContext` on
`app.state.amiigo` (common.py). Every route takes the rider from the token
(`require_rider`), and every answer is `Cache-Control: no-store`
(`common.NoStoreMiddleware`).

History: `GET /conversations` and `GET /conversations/{conversation_id}/messages`
(history.py), 60 requests a minute per rider across both. Logged as
`amiigo_history_list` and `amiigo_history_messages` with the outcome, the
count and the rider's hash; never a phone, a token, text or a link.

Uploads: `POST /uploads` ("Photos and videos"), a presigned PUT into the
`customers` tree under the rider's cluster, for a new chat or the rider's own
app chat, through the website's upload registry, so the socket claims it as
the website claims its own. 20 a minute per rider. Logged as `amiigo_upload`
with the outcome and the rider's hash; never the link or the key.

Deletion: `POST /erasure-requests`, `/status` and `/cancel` ("Deleting
conversation data"), the website's bodies (erasure.py) for the rider the
token proves: the app's channel, and the app's sign-in as the proof.

While the token check is off, uploads and deletion answer 503
`storage_unavailable` before the token is read (the plan's Ruling 1).
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from .. import erasure
from ..conversation import StoreUnavailable
from ..storage.uploads import UploadError
from . import history
from .auth import Rider
from .common import (
    CONFIRM_REQUIRED,
    CONVERSATION_ID_PATTERN,
    CONVERSATION_NOT_FOUND,
    CURSOR_INVALID,
    FILE_TOO_LARGE,
    FILE_TYPE_NOT_ACCEPTED,
    HISTORY_UNAVAILABLE,
    NOTHING_PENDING,
    PREFIX,
    RATE_LIMITED,
    STORAGE_UNAVAILABLE,
    AmiigoContext,
    amiigo_error,
    context_of,
    require_rider,
    require_storage_rider,
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


# -- uploads ----------------------------------------------------------------------

# The contract's photo and video types ("Photos and videos"). The bucket also
# takes the website's PDFs; the app's list does not. Sizes are the registry's
# (storage/keys.py SIZE_CAPS): photos up to 10 MB, videos up to 100 MB.
APP_UPLOAD_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime", "video/3gpp"})
_UPLOAD_REFUSALS = {413: FILE_TOO_LARGE, 415: FILE_TYPE_NOT_ACCEPTED}


class AppUploadIn(BaseModel):
    """The tree is always `customers`, and the rider is the token's."""

    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    mime_type: str
    size_bytes: int = Field(gt=0)


def upload_rider(request: Request, rider: Rider = Depends(require_storage_rider)) -> Rider:
    """The rider, within their upload allowance."""
    context = context_of(request)
    if not context.upload_limiter.allow(rider):
        context.log.emit("amiigo_rate_limited", "amiigo", rider_hash=rider.rider_hash, limit="uploads")
        raise amiigo_error(429, RATE_LIMITED)
    return rider


def _upload_logged(context: AmiigoContext, slot: str, rider: Rider, outcome: str, **fields: Any) -> None:
    context.log.emit("amiigo_upload", slot, outcome=outcome, rider_hash=rider.rider_hash, **fields)


@router.post("/uploads")
def post_upload(body: AppUploadIn, request: Request, rider: Rider = Depends(upload_rider)) -> Dict[str, Any]:
    context = context_of(request)
    if context.uploads is None or context.cluster_for_phone is None:
        _upload_logged(context, "amiigo", rider, "storage_not_configured")
        raise amiigo_error(503, STORAGE_UNAVAILABLE)
    try:
        allowed = history.rider_may_use(context.stores, rider, body.conversation_id)
    except StoreUnavailable as exc:
        _upload_logged(context, "amiigo", rider, "store_unavailable", error=str(exc))
        raise amiigo_error(503, STORAGE_UNAVAILABLE) from None
    if not allowed:
        # The id is whatever the app sent: not logged.
        _upload_logged(context, "amiigo", rider, "not_found")
        raise amiigo_error(404, CONVERSATION_NOT_FOUND)
    if body.mime_type not in APP_UPLOAD_TYPES:
        _upload_logged(context, "amiigo", rider, FILE_TYPE_NOT_ACCEPTED)
        raise amiigo_error(415, FILE_TYPE_NOT_ACCEPTED)
    try:
        pending, presign = context.uploads.begin_customer(
            context.cluster_for_phone(rider.phone), body.conversation_id, body.mime_type, body.size_bytes)
    except UploadError as exc:
        code = _UPLOAD_REFUSALS.get(exc.status)
        if code is None:
            # The body's checks leave no other refusal: a fault, not the app's.
            raise
        _upload_logged(context, "amiigo", rider, code)
        raise amiigo_error(exc.status, code) from None
    except Exception as exc:
        # Signing the PUT failed (no credentials, say): our storage, not the
        # rider. The class only: a message can name the key.
        _upload_logged(context, "amiigo", rider, "presign_failed", error=type(exc).__name__)
        raise amiigo_error(503, STORAGE_UNAVAILABLE) from None
    _upload_logged(context, body.conversation_id, rider, "ok", kind=pending.kind)
    return {"upload_id": pending.upload_id, "url": presign["url"], "headers": presign["headers"],
            "expires_in": presign["expires_in"]}


# -- deletion requests --------------------------------------------------------------


class AppErasureIn(BaseModel):
    # Only JSON `true` confirms; anything else, or nothing, is 400.
    confirm: Any = None
    conversation_id: Optional[str] = Field(default=None, pattern=CONVERSATION_ID_PATTERN)


def _now(context: AmiigoContext) -> str:
    return context.clock().isoformat()


@router.post("/erasure-requests", status_code=201)
def post_erasure_request(request: Request, response: Response, body: Optional[AppErasureIn] = None,
                         rider: Rider = Depends(require_storage_rider)) -> Dict[str, Any]:
    """The app's "Delete my conversation data" button, after its dialog.
    Records a request; a person deletes it with erasure_admin."""
    if body is None or body.confirm is not True:
        raise amiigo_error(400, CONFIRM_REQUIRED)
    context = context_of(request)
    user_key, channel, proof = erasure.app_requester(rider.phone)
    try:
        recorded, answer = erasure.record_request(context.stores.conversations, context.log, user_key, channel,
                                                  proof, body.conversation_id, _now(context),
                                                  rider_hash=rider.rider_hash)
    except erasure.RequestsUnavailable:
        raise amiigo_error(503, STORAGE_UNAVAILABLE) from None
    if not recorded:
        response.status_code = 200
    return answer


@router.post("/erasure-requests/status")
def post_erasure_status(request: Request, rider: Rider = Depends(require_storage_rider)) -> Dict[str, Any]:
    context = context_of(request)
    user_key, _, _ = erasure.app_requester(rider.phone)
    try:
        return erasure.request_status(context.stores.conversations, context.log, user_key, None,
                                      rider_hash=rider.rider_hash)
    except erasure.RequestsUnavailable:
        raise amiigo_error(503, STORAGE_UNAVAILABLE) from None


@router.post("/erasure-requests/cancel")
def post_erasure_cancel(request: Request, rider: Rider = Depends(require_storage_rider)) -> Dict[str, Any]:
    context = context_of(request)
    user_key, _, _ = erasure.app_requester(rider.phone)
    try:
        answer = erasure.cancel_request(context.stores.conversations, context.log, user_key, None, _now(context),
                                        rider_hash=rider.rider_hash)
    except erasure.RequestsUnavailable:
        raise amiigo_error(503, STORAGE_UNAVAILABLE) from None
    if answer is None:
        raise amiigo_error(404, NOTHING_PENDING)
    return answer
