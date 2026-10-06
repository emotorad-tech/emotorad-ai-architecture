"""The chat socket, /amiigo/v1/chat (docs/contracts/amiigo-support-chat.md,
"The chat socket": "Connecting", "Frames the app sends", "Frames the server
sends", "Resending", "Close codes").

Connecting. The handshake is accepted first and the `Authorization` header
read after, so a refusal reaches the app as a close code: a refused token
closes the socket 4401 with the code as the reason, and with the token check
off (no Amiigo key) it closes 1011 (the plan's Ruling 1), both before
`ready`. Refusing the handshake itself would reach the app as a bare HTTP
403. Then `ready`, and the socket is in the registry (sockets.py) until it
closes.

Frames. JSON text of at most 64 KB; anything else is `error bad_frame`, and
three bad frames in a row close the socket 1008. `ping` gets `pong`. Ten
minutes with no frame close it 4408 `idle`. When the token expires (its
`exp` plus the minute the token check allows, auth.LEEWAY) the socket closes
4401 `token_expired`, once every reply being prepared on it has been sent;
a message that arrives after the expiry is not handled (logged
`token_expired`), and the app sends it again on its next socket.

A message, once its fields are as the contract says, is admitted in this
order. A message already accepted (its receipt, receipts.py) is never
handled again: it gets the same `ack` and `reply`, once they exist, on any
socket of the rider. Then 20 messages a minute (`rate_limited`). A
conversation another rider is writing in right now, or whose records are not
this rider's app chat (history.rider_may_use: a new chat, or the rider's
own), is `conversation_not_found`, never saying which. One message at a
time per conversation (`conversation_busy`). An upload that is not there is
`upload_not_found`. Then `bot_typing` (`looking_at_video` when the message
carries a video, which is described before the turn), and the turn runs in
a worker thread so the socket keeps answering: api.prepare_turn with
`channel="amiigo_app"` and the token's phone, then the runtime's `handle`,
exactly as a website turn, so the safety branch, the disclosure, the
evidence check and the post-checks apply unchanged.

The ack and the reply. The runtime records the rider's message and the bot's
reply together, at the end of the turn. So the `ack` (the rider's message as
stored) goes out with the `reply`, after `bot_typing`, both read back from
the transcript (history.message_view): their ids are the ones history uses,
and their text is masked as history shows it. A turn the store could not
record still gets its one answer, with ids history does not know. The turn
finishes, and its receipt is written, in the worker thread: a socket that
closed mid-turn loses nothing, and the app fetches the reply with GET or by
sending the message again.

Logs: `amiigo_socket_open`, `amiigo_socket_close` (code, reason) and
`amiigo_message` (outcome), each with the rider's hash; never a token,
phone, text, link or the app's message id.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import anyio
from fastapi import HTTPException, WebSocket

from ..attachments import AttachmentError
from ..conversation import StoreUnavailable, TranscriptTurn, recorded_url
from ..observability import redact_pii
from . import history
from .auth import LEEWAY, TOKEN_EXPIRED, Rider, rider_from_header
from .common import (
    CONVERSATION_ID_PATTERN,
    CONVERSATION_NOT_FOUND,
    RATE_LIMITED,
    STORAGE_UNAVAILABLE,
    AmiigoContext,
)
from .receipts import BUSY, DONE, DUPLICATE, receipt_id
from .sockets import OpenSocket

PROTOCOL = 1
MAX_FRAME_BYTES = 64 * 1024
MAX_TEXT = 4000
MAX_ATTACHMENTS = 3
BAD_FRAMES_IN_A_ROW = 3

# Close codes and reasons ("Close codes").
TOKEN_CLOSE = 4401
IDLE_CLOSE = 4408
BAD_FRAMES_CLOSE = 1008
FAULT_CLOSE = 1011
IDLE = "idle"
BAD_FRAMES = "bad_frames"
SERVER_ERROR = "server_error"
UNAVAILABLE = "unavailable"

# The error frame's details ("Frames the server sends", `error`).
CONVERSATION_BUSY = "conversation_busy"
UPLOAD_NOT_FOUND = "upload_not_found"
UPLOAD_NOT_FINISHED = "upload_not_finished"
TOO_MANY_ATTACHMENTS = "too_many_attachments"
TEXT_TOO_LONG = "text_too_long"
BAD_FRAME = "bad_frame"

THINKING = "thinking"
LOOKING_AT_VIDEO = "looking_at_video"
OPEN = "open"

# api.prepare_turn's refusals of an upload, as the socket's details. Unknown
# or expired (404) and another rider's (403) are both upload_not_found, as
# the contract has it; no media storage on this server (503) is
# storage_unavailable, the HTTP endpoints' code for it (the plan's Ruling 15).
_UPLOAD_REFUSALS = {404: UPLOAD_NOT_FOUND, 403: UPLOAD_NOT_FOUND, 409: UPLOAD_NOT_FINISHED,
                    503: STORAGE_UNAVAILABLE}
# What each refusal is logged as.
_OUTCOMES = {CONVERSATION_NOT_FOUND: "not_found", CONVERSATION_BUSY: "busy"}

# A UUID as the app writes one, in either case, used exactly as sent.
_UUID = re.compile(CONVERSATION_ID_PATTERN)
PING = "ping"
# Put on the frame queue to wake the socket's loop when a turn ends.
_WAKE = {"type": "amiigo.wake"}
# The socket's two thread allowances (AmiigoContext.socket_turn_threads and
# socket_store_threads).
TURN_THREADS = "turns"
STORE_THREADS = "store"


def _threads(context: AmiigoContext, kind: str) -> anyio.CapacityLimiter:
    """The socket's own worker threads on this event loop. One pair per
    loop, made on first use: an anyio limiter belongs to the loop it waits
    on. A server runs one loop, so one pair."""
    loop = asyncio.get_running_loop()
    mine = context.thread_limiters.get(loop)
    if mine is None:
        mine = {TURN_THREADS: anyio.CapacityLimiter(context.socket_turn_threads),
                STORE_THREADS: anyio.CapacityLimiter(context.socket_store_threads)}
        context.thread_limiters[loop] = mine
    return mine[kind]


# -- frames the app sends ----------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A shared location, with the two fields prepare_turn reads (api.LocationIn's)."""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class MessageFrame:
    client_message_id: str
    conversation_id: str
    text: str
    upload_ids: Tuple[str, ...] = ()
    screen: Optional[str] = None
    pill: Optional[str] = None
    location: Optional[Location] = None


class Refused(Exception):
    """A frame the server will not act on: the error's detail, and the
    message's id when it had a good one."""

    def __init__(self, detail: str, client_message_id: Optional[str] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.client_message_id = client_message_id


def _text(value: Any) -> bool:
    """A JSON string that is UTF-8 all through: no lone surrogate from a
    `\\ud800` escape, which no store could keep."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _uuid(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _UUID.fullmatch(value) else None


def _coordinate(value: Any, limit: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and -limit <= value <= limit else None


def _location(value: Any) -> Optional[Location]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("not an object")
    latitude, longitude = _coordinate(value.get("latitude"), 90.0), _coordinate(value.get("longitude"), 180.0)
    if latitude is None or longitude is None:
        raise ValueError("not a point")
    return Location(latitude, longitude)


def read_frame(text: Optional[str]) -> Union[str, MessageFrame]:
    """PING, or the message a frame carries; Refused otherwise. Fields the
    contract does not name are ignored: v1 only gains fields."""
    if text is None or not _text(text) or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise Refused(BAD_FRAME)
    try:
        body = json.loads(text)
    except (ValueError, RecursionError):
        raise Refused(BAD_FRAME) from None
    if not isinstance(body, dict):
        raise Refused(BAD_FRAME)
    kind = body.get("type")
    if kind == PING:
        return PING
    client_message_id = _uuid(body.get("client_message_id"))
    if kind != "message" or client_message_id is None:
        raise Refused(BAD_FRAME, client_message_id)
    return _message(body, client_message_id)


def _message(body: Dict[str, Any], client_message_id: str) -> MessageFrame:
    def refused(detail: str = BAD_FRAME) -> Refused:
        return Refused(detail, client_message_id)

    conversation_id = _uuid(body.get("conversation_id"))
    text = body.get("text")
    attachments = body.get("attachments")
    attachments = [] if attachments is None else attachments
    screen, pill = body.get("screen"), body.get("pill")
    if conversation_id is None or not _text(text) or not isinstance(attachments, list):
        raise refused()
    if any(value is not None and not _text(value) for value in (screen, pill)):
        raise refused()
    upload_ids = []
    for item in attachments:
        # {"upload_id": "..."} and nothing else: nothing is sent inline.
        if not isinstance(item, dict) or set(item) != {"upload_id"} or not _text(item["upload_id"]) \
                or not item["upload_id"]:
            raise refused()
        upload_ids.append(item["upload_id"])
    try:
        location = _location(body.get("location"))
    except ValueError:
        raise refused() from None
    if len(text) > MAX_TEXT:
        raise refused(TEXT_TOO_LONG)
    if len(upload_ids) > MAX_ATTACHMENTS:
        raise refused(TOO_MANY_ATTACHMENTS)
    return MessageFrame(client_message_id, conversation_id, text, tuple(upload_ids), screen, pill, location)


# -- frames the server sends --------------------------------------------------------


def error_frame(client_message_id: Optional[str], detail: str) -> Dict[str, Any]:
    return {"type": "error", "client_message_id": client_message_id, "detail": detail}


def typing_frame(conversation_id: str, video: bool) -> Dict[str, Any]:
    return {"type": "bot_typing", "conversation_id": conversation_id, "state": LOOKING_AT_VIDEO if video else THINKING}


def unrecorded_ids(conversation_id: str, client_message_id: str) -> Tuple[str, str]:
    """The ids of a turn the store could not record. History does not know
    them: `after` with one is `cursor_invalid`, and the app loads the newest
    page again."""
    return ("%s#unsaved-%s" % (conversation_id, client_message_id),
            "%s#unsaved-%s-reply" % (conversation_id, client_message_id))


def _live_fields(reply: Any) -> Dict[str, Any]:
    """What only the live reply carries ("Frames the server sends", `reply`)."""
    return {
        "actions": [dict(action) for action in reply.actions],
        "escalated": bool(reply.escalated),
        "ticket": {"reference": reply.ticket_id, "status": OPEN, "closed_at": None} if reply.ticket_id else None,
        "handled_by": reply.handled_by or "",
        # Each attachment's type, caption and poster, in the order the turn
        # recorded them; the poster as a permanent record keeps a link.
        "media": [{"mime_type": a.mime_type, "caption": a.caption,
                   "poster": recorded_url(a.poster) if a.poster else None} for a in reply.attachments],
    }


def receipt_of(frame: MessageFrame, message: Any, reply: Any,
               recorded: Optional[Tuple[TranscriptTurn, TranscriptTurn]], at: str) -> Tuple[Dict, Dict]:
    """The receipt's `ack` and `reply`. A recorded turn: the stored turns'
    ids, the reply's text as sent and the live reply's fields; none of the
    rider's words. A turn not recorded: the rider's message too, masked as a
    transcript masks it."""
    live = _live_fields(reply)
    if recorded is not None:
        rider_turn, bot_turn = recorded
        return {"id": history.turn_id(rider_turn)}, dict(live, id=history.turn_id(bot_turn), text=reply.text or "")
    rider_id, bot_id = unrecorded_ids(frame.conversation_id, frame.client_message_id)
    said = reply.metadata.get("transcript_text", message.message_text) or ""
    ack = {"id": rider_id, "role": "customer", "text": redact_pii(said), "at": at,
           "attachments": [{"kind": a.kind, "url": recorded_url(a.url)} for a in message.attachments]}
    answer = dict(live, id=bot_id, role="bot", text=reply.text or "", at=at,
                  attachments=[{"kind": a.kind, "url": recorded_url(a.url)} for a in reply.attachments])
    return ack, answer


def _unrecorded_turn(kept: Dict[str, Any], conversation_id: str) -> TranscriptTurn:
    return TranscriptTurn(n=0, role=kept["role"], text=kept["text"], at=kept["at"],
                          attachments=tuple(kept["attachments"]), conversation_id=conversation_id)


def answer_frames(frame: MessageFrame, ack: Dict[str, Any], reply: Dict[str, Any],
                  turns: Optional[Tuple[TranscriptTurn, TranscriptTurn]], signer: history.Signer,
                  now: Any) -> List[Dict[str, Any]]:
    """The `ack` and the `reply`, with fresh links. The reply's text is as
    it was sent, what POST /message would have returned (the plan's Ruling
    16); history shows it as stored, masked."""
    if turns is not None:
        rider_view = history.message_view(turns[0], signer, now)
        bot_view = history.message_view(turns[1], signer, now)
    else:
        rider_view = dict(history.message_view(_unrecorded_turn(ack, frame.conversation_id), signer, now),
                          id=ack["id"])
        bot_view = dict(history.message_view(_unrecorded_turn(reply, frame.conversation_id), signer, now),
                        id=reply["id"])
    bot_view["text"] = reply["text"]
    media = list(reply.get("media") or [])
    for n, shown in enumerate(bot_view["attachments"]):
        extra = media[n] if n < len(media) else {}
        poster = signer(extra["poster"])[0] if extra.get("poster") else None
        shown.update(mime_type=extra.get("mime_type"), caption=extra.get("caption"), poster=poster)
    return [
        {"type": "ack", "client_message_id": frame.client_message_id, "conversation_id": frame.conversation_id,
         "message": rider_view},
        {"type": "reply", "conversation_id": frame.conversation_id, "in_reply_to": frame.client_message_id,
         "message": bot_view, "actions": reply["actions"], "escalated": reply["escalated"],
         "ticket": reply["ticket"], "handled_by": reply["handled_by"]},
    ]


# -- one socket ---------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    error: Optional[str] = None
    duplicate: bool = False
    video: bool = False


@dataclass(frozen=True)
class TurnResult:
    error: Optional[str] = None
    frames: Tuple[Dict[str, Any], ...] = ()


class ChatSocket:
    """One rider's open socket: its frames, read in order by `run`; each
    admitted message answered by a task of its own (`_answer`), whose
    blocking work runs in a worker thread."""

    def __init__(self, websocket: WebSocket, context: AmiigoContext, rider: Rider, sock: OpenSocket) -> None:
        self.websocket = websocket
        self.context = context
        self.rider = rider
        self.sock = sock
        self.frames: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.answering: Set["asyncio.Task[None]"] = set()
        # The message each answering task is for: one per message per socket.
        self.working: Dict[str, "asyncio.Task[None]"] = {}
        self.bad_in_a_row = 0
        self.fault = False

    # -- the loop ---------------------------------------------------------------

    async def run(self) -> Tuple[Optional[int], str]:
        """Until the socket closes: the close code and reason."""
        loop = asyncio.get_running_loop()
        context = self.context
        reader = asyncio.create_task(self._read())
        try:
            # The token check accepts a token until a minute after its `exp`.
            left = (self.rider.expires_at + LEEWAY - context.clock()).total_seconds()
            expires = loop.time() + left
            idle_at = loop.time() + context.socket_idle_seconds
            expiring = False
            while True:
                now = loop.time()
                if self.fault:
                    return await self._close(FAULT_CLOSE, SERVER_ERROR)
                if not expiring and now >= expires:
                    expiring = True
                if expiring and not self.answering:
                    return await self._close(TOKEN_CLOSE, TOKEN_EXPIRED)
                if now >= idle_at:
                    return await self._close(IDLE_CLOSE, IDLE)
                until = idle_at if expiring else min(idle_at, expires)
                try:
                    received = await asyncio.wait_for(self.frames.get(), timeout=max(until - now, 0.0))
                except asyncio.TimeoutError:
                    continue
                if received is _WAKE:
                    continue
                if received["type"] == "websocket.disconnect":
                    return received.get("code"), "client_closed"
                idle_at = loop.time() + context.socket_idle_seconds
                closing = await self._frame(received, expiring)
                if closing is not None:
                    return closing
        finally:
            reader.cancel()

    async def _read(self) -> None:
        while True:
            try:
                received = await self.websocket.receive()
            except Exception as exc:
                # The connection broke under the read: as good as the app leaving.
                self.context.log.emit("amiigo_socket_read_failed", "amiigo", rider_hash=self.rider.rider_hash,
                                      error=type(exc).__name__)
                received = {"type": "websocket.disconnect", "code": 1006}
            await self.frames.put(received)
            if received["type"] == "websocket.disconnect":
                return

    async def _close(self, code: int, reason: str) -> Tuple[int, str]:
        await self.sock.close(code, reason)
        return code, reason

    async def _frame(self, received: Dict[str, Any], expiring: bool) -> Optional[Tuple[int, str]]:
        try:
            read = read_frame(received.get("text"))  # a binary frame has no text
        except Refused as refused:
            self.bad_in_a_row = self.bad_in_a_row + 1 if refused.detail == BAD_FRAME else 0
            await self._refuse(refused.client_message_id, refused.detail)
            if self.bad_in_a_row >= BAD_FRAMES_IN_A_ROW:
                return await self._close(BAD_FRAMES_CLOSE, BAD_FRAMES)
            return None
        self.bad_in_a_row = 0
        if read == PING:
            await self.sock.send({"type": "pong"})
        elif expiring:
            # Not handled: the app sends it again on its next socket.
            self._logged("amiigo", TOKEN_EXPIRED)
        else:
            await self._message(read)
        return None

    # -- admitting a message -------------------------------------------------------

    async def _message(self, frame: MessageFrame) -> None:
        """Every message frame counts against the rider's allowance, resends
        too; a resend this socket is already answering is folded into that
        answer, so each socket waits on a message at most once."""
        if not self.context.message_limiter.allow(self.rider):
            await self._refuse(frame.client_message_id, RATE_LIMITED)
            return
        rid = receipt_id(self.rider.user_key, frame.client_message_id)
        if rid in self.working:
            self._logged("amiigo", "coalesced")
            return
        try:
            admission = await self._store(self._admit, frame, rid)
        except StoreUnavailable as exc:
            # The receipts cannot be read: exactly once cannot be kept, so
            # nothing runs (only the ownership check fails open, Ruling 13).
            self._logged("amiigo", "store_unavailable", error=type(exc).__name__)
            self._fail()
            return
        except Exception as exc:
            # A fault while admitting: logged by its class, the socket closes 1011.
            self._logged("amiigo", "fault", error=type(exc).__name__)
            self._fail()
            return
        if admission.error is not None:
            await self._refuse(frame.client_message_id, admission.error)
            return
        task = asyncio.create_task(self._answer(frame, rid, admission))
        self.answering.add(task)
        self.working[rid] = task
        task.add_done_callback(functools.partial(self._answered, rid))

    def _admit(self, frame: MessageFrame, rid: str) -> Admission:
        """Blocking: the receipts and the conversation's records."""
        context, rider, conversation_id = self.context, self.rider, frame.conversation_id
        receipts = context.receipts
        existing = receipts.answering(rid)
        if existing is not None:
            return self._same_message(existing, frame)
        holder = receipts.in_flight(conversation_id)
        if holder is not None:
            return self._held(frame, rid, holder)
        if not self._may_use(conversation_id):
            return Admission(error=CONVERSATION_NOT_FOUND)
        error, video = self._uploads(frame)
        if error is not None:
            return Admission(error=error)
        outcome, doc = receipts.claim(rid, rider.user_key, conversation_id)
        if outcome == DUPLICATE:
            return self._same_message(doc, frame)
        if outcome == BUSY:
            return self._held(frame, rid, receipts.in_flight(conversation_id))
        # The chat may have become someone else's between the check and the
        # claim: looked at again, and the claim let go if so.
        if not self._may_use(conversation_id):
            receipts.release(rid)
            return Admission(error=CONVERSATION_NOT_FOUND)
        return Admission(video=video)

    def _may_use(self, conversation_id: str) -> bool:
        """history.rider_may_use, failing open when the store cannot answer
        (the plan's Ruling 13): the turn then meets the same store, and the
        runtime's outage path answers, a handover or a safety report's steps
        and 112, rather than the socket closing on a rider reporting smoke."""
        try:
            return history.rider_may_use(self.context.stores, self.rider, conversation_id)
        except StoreUnavailable as exc:
            self._noted("amiigo_owner_check_failed", "amiigo", error=type(exc).__name__)
            return True

    def _held(self, frame: MessageFrame, rid: str, holder: Optional[str]) -> Admission:
        """The conversation has a message in flight. Another rider's: not
        found. The rider's own: this very message, claimed a moment ago on
        another socket, or another one, and busy."""
        if holder is not None and holder != self.rider.user_key:
            return Admission(error=CONVERSATION_NOT_FOUND)
        existing = self.context.receipts.answering(rid)
        if existing is not None:
            return self._same_message(existing, frame)
        return Admission(error=CONVERSATION_BUSY)

    @staticmethod
    def _same_message(receipt: Dict[str, Any], frame: MessageFrame) -> Admission:
        """A message id already used: the same message again, or, under
        another chat, an app that reused an id."""
        if receipt["conversation_id"] != frame.conversation_id:
            return Admission(error=BAD_FRAME)
        return Admission(duplicate=True)

    def _uploads(self, frame: MessageFrame) -> Tuple[Optional[str], bool]:
        """(the refusal, a video among them), by a look that claims nothing.
        prepare_turn checks each again as it claims it."""
        if not frame.upload_ids:
            return None, False
        uploads = self.context.uploads
        if uploads is None:
            # No media storage on this server (Ruling 15).
            return STORAGE_UNAVAILABLE, False
        video = False
        for upload_id in frame.upload_ids:
            pending = uploads.peek(upload_id)
            if pending is None:
                return UPLOAD_NOT_FOUND, False
            video = video or pending.kind == "videos"
        return None, video

    # -- answering a message -----------------------------------------------------------

    async def _store(self, fn: Any, *args: Any) -> Any:
        """Short blocking work (the receipts, the conversation's records) on
        the socket's own store threads."""
        return await anyio.to_thread.run_sync(fn, *args, limiter=_threads(self.context, STORE_THREADS))

    async def _answer(self, frame: MessageFrame, rid: str, admission: Admission) -> None:
        if not admission.duplicate:
            await self._turn(frame, rid, admission.video, typed=False)
            return
        # Accepted already, here or on another socket: wait for its answer,
        # reading the receipt once a poll, or admit it afresh when the first
        # attempt let it go or died with its server.
        typed = False
        receipts = self.context.receipts
        while not self.sock.closed:
            doc = await self._store(receipts.answering, rid)
            if doc is None:
                again = await self._store(self._admit, frame, rid)
                if again.error is not None:
                    await self._refuse(frame.client_message_id, again.error)
                    return
                if not again.duplicate:
                    await self._turn(frame, rid, again.video, typed=typed)
                    return
                continue
            if doc["conversation_id"] != frame.conversation_id:
                await self._refuse(frame.client_message_id, BAD_FRAME)
                return
            if doc["state"] == DONE:
                result = await self._store(self._replay, frame, doc)
                await self._deliver(frame, result)
                return
            if not typed:
                await self.sock.send(typing_frame(frame.conversation_id, False))
                typed = True
            await asyncio.sleep(self.context.socket_poll_seconds)

    async def _turn(self, frame: MessageFrame, rid: str, video: bool, typed: bool) -> None:
        if not typed:
            await self.sock.send(typing_frame(frame.conversation_id, video))
        # On the socket's own turn threads, never anyio's default pool that
        # POST /message shares. Abandoned, not cancelled, when the socket
        # goes: the turn finishes and its receipt is written in the thread.
        result = await anyio.to_thread.run_sync(self._run_turn, frame, rid, abandon_on_cancel=True,
                                                limiter=_threads(self.context, TURN_THREADS))
        await self._deliver(frame, result)

    async def _deliver(self, frame: MessageFrame, result: TurnResult) -> None:
        if result.error is not None:
            await self._refuse(frame.client_message_id, result.error, frame.conversation_id)
            return
        for sent in result.frames:
            await self.sock.send(sent)

    def _answered(self, rid: str, task: "asyncio.Task[None]") -> None:
        self.answering.discard(task)
        if self.working.get(rid) is task:
            del self.working[rid]
        if not task.cancelled() and task.exception() is not None:
            # A fault in the turn (_run_turn released the message, so it may
            # be sent again): the socket closes 1011.
            self._logged("amiigo", "fault", error=type(task.exception()).__name__)
            self.fault = True
        self.frames.put_nowait(_WAKE)

    def _fail(self) -> None:
        self.fault = True
        self.frames.put_nowait(_WAKE)

    # -- in the worker thread ------------------------------------------------------------

    def _run_turn(self, frame: MessageFrame, rid: str) -> TurnResult:
        """The turn, as POST /message runs one, then its receipt. Blocking."""
        context = self.context
        try:
            message = context.prepare_turn(
                conversation_id=frame.conversation_id, text=frame.text,
                attachments=[{"upload_id": upload_id} for upload_id in frame.upload_ids] or None,
                pill=frame.pill, screen=frame.screen, location=frame.location,
                channel=history.APP_CHANNEL, rider_phone=self.rider.phone)
        except HTTPException as exc:
            self._release(rid)
            detail = _UPLOAD_REFUSALS.get(exc.status_code)
            if detail is None:
                raise
            return TurnResult(error=detail)
        except AttachmentError:
            # The frame's checks leave none: the count is checked on arrival.
            self._release(rid)
            return TurnResult(error=BAD_FRAME)
        except BaseException:
            self._release(rid)
            raise
        last = self._last_turn(frame.conversation_id)
        try:
            reply = context.handle_turn(message)
        except BaseException:
            self._release(rid)
            raise
        # The turn happened: from here its message is never handled again.
        now = context.clock()
        recorded = self._recorded(frame.conversation_id, last)
        ack, answer = receipt_of(frame, message, reply, recorded, now.isoformat())
        try:
            if not context.receipts.finish(rid, ack, answer):
                self._noted("amiigo_receipt_not_saved", frame.conversation_id, error="receipt_gone")
        except StoreUnavailable as exc:
            # A resend after the lease runs the turn again: said, not hidden.
            self._noted("amiigo_receipt_not_saved", frame.conversation_id, error=type(exc).__name__)
        self._logged(frame.conversation_id, "ok" if recorded is not None else "not_recorded")
        signer = history.signer_for(context.media_store, now, context.log)
        return TurnResult(frames=tuple(answer_frames(frame, ack, answer, recorded, signer, now)))

    def _replay(self, frame: MessageFrame, receipt: Dict[str, Any]) -> TurnResult:
        """The answer a receipt holds, built again with fresh links. Blocking."""
        context = self.context
        ack, answer = receipt["ack"], receipt["reply"]
        turns: Optional[Tuple[TranscriptTurn, TranscriptTurn]] = None
        if "role" not in ack:
            by_id = {history.turn_id(t): t for t in context.stores.conversations.turns_of(frame.conversation_id)}
            if ack["id"] not in by_id or answer["id"] not in by_id:
                # Deleted since (the rider's request to erase the chat).
                return TurnResult(error=CONVERSATION_NOT_FOUND)
            turns = (by_id[ack["id"]], by_id[answer["id"]])
        self._logged(frame.conversation_id, "duplicate")
        now = context.clock()
        signer = history.signer_for(context.media_store, now, context.log)
        return TurnResult(frames=tuple(answer_frames(frame, ack, answer, turns, signer, now)))

    def _last_turn(self, conversation_id: str) -> Optional[int]:
        """The number of the conversation's last recorded turn, before this
        one; None when it cannot be read (the turn then cannot be told apart)."""
        try:
            return max((turn.n for turn in self.context.stores.conversations.turns_of(conversation_id)), default=0)
        except StoreUnavailable as exc:
            self._noted("amiigo_transcript_unread", conversation_id, error=type(exc).__name__)
            return None

    def _recorded(self, conversation_id: str, last: Optional[int]) -> Optional[Tuple[TranscriptTurn, TranscriptTurn]]:
        """The rider's turn and the bot's this turn recorded, or None."""
        if last is None:
            return None
        try:
            turns = [t for t in self.context.stores.conversations.turns_of(conversation_id) if t.n > last]
        except StoreUnavailable as exc:
            self._noted("amiigo_transcript_unread", conversation_id, error=type(exc).__name__)
            return None
        said = next((t for t in turns if t.role == "customer"), None)
        answered = next((t for t in turns if said is not None and t.role == "bot" and t.n == said.n + 1), None)
        return (said, answered) if answered is not None else None

    def _release(self, rid: str) -> None:
        try:
            self.context.receipts.release(rid)
        except StoreUnavailable as exc:
            # It runs out with its lease instead.
            self._noted("amiigo_receipt_not_released", "amiigo", error=type(exc).__name__)

    # -- refusals and logs -------------------------------------------------------------

    async def _refuse(self, client_message_id: Optional[str], detail: str, slot: str = "amiigo") -> None:
        # A chat that is not the rider's is never named in the log.
        self._logged("amiigo" if detail == CONVERSATION_NOT_FOUND else slot, _OUTCOMES.get(detail, detail))
        await self.sock.send(error_frame(client_message_id, detail))

    def _logged(self, slot: str, outcome: str, **fields: Any) -> None:
        """One `amiigo_message` per message frame. `slot` is the
        conversation once the message is the rider's to send there, "amiigo"
        before: an id the app sent is not logged."""
        self._noted("amiigo_message", slot, outcome=outcome, **fields)

    def _noted(self, event: str, slot: str, **fields: Any) -> None:
        self.context.log.emit(event, slot, rider_hash=self.rider.rider_hash, **fields)


# -- the route ------------------------------------------------------------------------


async def serve(websocket: WebSocket, context: AmiigoContext) -> None:
    """One chat socket, from the handshake to its close."""
    await websocket.accept()
    log = context.log
    if not context.tokens.enabled or context.prepare_turn is None or context.handle_turn is None:
        await websocket.close(code=FAULT_CLOSE, reason=UNAVAILABLE)
        log.emit("amiigo_socket_close", "amiigo", code=FAULT_CLOSE, reason=UNAVAILABLE)
        return
    found = rider_from_header(websocket.headers.get("authorization"), context.tokens)
    if isinstance(found, str):
        await websocket.close(code=TOKEN_CLOSE, reason=found)
        log.emit("amiigo_socket_close", "amiigo", code=TOKEN_CLOSE, reason=found)
        return
    rider = found
    sock = OpenSocket(websocket, rider.rider_hash, asyncio.get_running_loop(), log)
    chat = ChatSocket(websocket, context, rider, sock)
    code: Optional[int] = None
    reason = "ended"
    try:
        await sock.send({"type": "ready", "protocol": PROTOCOL, "server_time": history.utc_text(context.clock())})
        # Only after ready: nothing pushed (ticket_update) may come before it.
        context.sockets.add(rider.user_key, sock)
        log.emit("amiigo_socket_open", "amiigo", rider_hash=rider.rider_hash)
        code, reason = await chat.run()
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    finally:
        context.sockets.remove(rider.user_key, sock)
        sock.mark_closed()
        log.emit("amiigo_socket_close", "amiigo", code=code, reason=reason, rider_hash=rider.rider_hash)
