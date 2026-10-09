"""A rider's chats and their messages, read back for the Amiigo app
(docs/contracts/amiigo-support-chat.md, "History", "Messages", "What is masked").

Reads only what the runtime already writes: one summary per run of a
conversation (`runs_of`, grouped here into one chat), the transcript turns
(`turns_of`) and the notices in a chat (`notices_of`, a ticket closed in Zoho
Desk, written by the ticket closure). Text is returned as stored, masked as
it was recorded.

In v1 the history holds the rider's Amiigo app chats only (the plan's
Ruling 6): a chat is listed, and read, only when every run of it is an
`amiigo_app` run and every run is the rider's: the summaries (`owner_of`)
and, for a run that wrote none (someone who never proved a number), the
record of where the run came from (`origins_of`, written for every run,
which must name the rider too: Ruling 10). Anything else, a website chat even where the rider
proved this number, or one in which someone else wrote, is
`conversation_not_found` and never listed: nothing of another person's
words is read out. A chat of the rider's whose summary was never written
(a record that failed, the final review's Important 1) is read by its id,
and written in, but listed only once a later turn writes its summary.

Cursors are opaque base64url JSON, issued here. They carry positions only,
nothing taken from the phone (Ruling 7): every read is by the rider's user
key, so a cursor can only ever move through the rider's own chats. The list
pages by keyset on (last message time, conversation id); the messages page
back from the oldest message of the page before, by its id, within the chat
the cursor names. So a turn recorded between two requests is neither skipped
nor repeated: new messages only ever come after the ones already read.

Times are compared as times, never as text: `utc_now_iso` drops the
microseconds when they are zero. They are written as the contract writes
them, `2026-10-06T09:12:44Z`.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import unquote, urlsplit

from ..conversation import SHARED_OWNER, ConversationSummaryItem, TranscriptTurn
from ..storage.keys import is_asset_key, is_valid_key
from ..tickets.record import SUPPORT_OPEN
from .auth import Rider

# "can_continue: true when the last message is under 48 hours old": the
# working state's life (stores/mongo.py, state_ttl_hours).
CAN_CONTINUE = timedelta(hours=48)
# "About 15 minutes after the response."
LINK_SECONDS = 900
# The general title, for a run that recorded none.
GENERAL_TITLE = "General question"
HANDED_TO_SUPPORT = "handed_to_support"
OPEN = "open"
# The one channel the v1 history holds (Ruling 6).
APP_CHANNEL = "amiigo_app"
S3_SCHEME = "s3://"
# What a time that cannot be read sorts as: before everything.
_NEVER = datetime.min.replace(tzinfo=timezone.utc)
CURSOR_VERSION = 1
CHATS = "chats"
MESSAGES = "messages"
# A cursor as issued: base64url, no padding, and short. An explicit ASCII
# class, so nothing from another script passes for one.
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,512}")
# An S3 endpoint host: s3.amazonaws.com, s3.<region>.amazonaws.com or
# s3-<region>.amazonaws.com. Hostnames are ASCII.
_S3_ENDPOINT = re.compile(r"s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com")

Signer = Callable[[Optional[str]], Tuple[Optional[str], Optional[str]]]


class CursorInvalid(Exception):
    """A `cursor`, `before` or `after` this server did not issue, or not for this chat."""


class ConversationNotFound(Exception):
    """The chat does not exist, or is not this rider's. Which, is never said."""


# -- times ---------------------------------------------------------------------


def _moment(text: Any) -> Optional[datetime]:
    """A stored ISO time as an aware UTC datetime, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def utc_text(moment: datetime) -> str:
    """A time as the contract writes one: `2026-10-06T09:12:44Z`."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shown(text: Any) -> Any:
    """A stored time as the contract writes it, or as stored when it cannot be read."""
    moment = _moment(text)
    return utc_text(moment) if moment is not None else text


# -- cursors -------------------------------------------------------------------


def _encode(kind: str, **fields: Any) -> str:
    """A position, and nothing about who asked: no phone, no hash of one."""
    payload = dict(fields, v=CURSOR_VERSION, k=kind)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str, kind: str) -> Dict[str, Any]:
    if not _CURSOR.fullmatch(text) or len(text) % 4 == 1:
        raise CursorInvalid()
    try:
        payload = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (ValueError, binascii.Error):
        # Not base64, not UTF-8, or not JSON.
        raise CursorInvalid() from None
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION or payload.get("k") != kind:
        raise CursorInvalid()
    return payload


# -- links -------------------------------------------------------------------------


def _key_of(url: Any, bucket: str) -> Optional[str]:
    """The key of a file in our bucket that may be shown, or None.

    `s3://<key>` (how a turn records a stored photo or video, api.py) or
    `s3://<bucket>/<key>`: any key of the bucket's grammar. An `https://` link
    to our bucket, virtual-hosted or path-style, as a guide picture's signed
    link is recorded without its signature: a key under `assets/` only.
    """
    if not isinstance(url, str) or not bucket:
        return None
    if url.startswith(S3_SCHEME):
        key = url[len(S3_SCHEME):]
        if key.startswith(bucket + "/"):
            key = key[len(bucket) + 1:]
        return key if is_valid_key(key) else None
    if not url.startswith("https://"):
        return None
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), unquote(parts.path)
    if host.startswith(bucket + ".") and _S3_ENDPOINT.fullmatch(host[len(bucket) + 1:]):
        key = path[1:]
    elif _S3_ENDPOINT.fullmatch(host) and path.startswith("/%s/" % bucket):
        key = path[len(bucket) + 2:]
    else:
        return None
    return key if is_valid_key(key) and is_asset_key(key) else None


def signer_for(media_store: Any, now: datetime, log: Any = None) -> Signer:
    """`(stored_url) -> (link, expires_at)`: a link to a file in our bucket,
    signed for fifteen minutes, and when it stops working; `(None, None)` for
    anything else: a photo that was never kept (`data:(inline, not kept)`),
    another host, or no bucket configured. A link that cannot be signed is
    `(None, None)` too, logged by the error's class, never the key."""
    expires_at = utc_text(now + timedelta(seconds=LINK_SECONDS))

    def sign(stored_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if media_store is None:
            return None, None
        key = _key_of(stored_url, getattr(media_store, "bucket", ""))
        if key is None:
            return None, None
        try:
            return media_store.presign_get(key, expires_in=LINK_SECONDS), expires_at
        except Exception as exc:
            if log is not None:
                log.emit("amiigo_media_unsigned", "amiigo", error=type(exc).__name__)
            return None, None

    return sign


# -- messages ------------------------------------------------------------------


def turn_id(turn: TranscriptTurn) -> str:
    """A turn's message id: its stored `_id`, `<conversation_id>#<n:05d>`."""
    if not turn.conversation_id:
        raise ValueError("a transcript turn read without its conversation id has no message id")
    return "%s#%05d" % (turn.conversation_id, turn.n)


def message_id(item: Union[TranscriptTurn, Mapping[str, Any]]) -> str:
    return turn_id(item) if isinstance(item, TranscriptTurn) else str(item["_id"])


def message_view(item: Union[TranscriptTurn, Mapping[str, Any]], signer: Signer,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """The contract's message object for a stored transcript turn or notice.

    A turn is the rider's (`customer`) or the bot's; a notice is `system`.
    Attachments keep their kind and get a fresh link from `signer`; a caption
    or a poster is a live reply's only, never history's. `now` is the instant
    the caller answers at, the same one its signer was made for; the view
    takes every time it shows from the item and the signer."""
    if isinstance(item, TranscriptTurn):
        view = {
            "id": turn_id(item),
            "sender": "rider" if item.role == "customer" else "bot",
            "text": item.text,
            "sent_at": _shown(item.at),
            "attachments": [_attachment_view(a, signer) for a in item.attachments],
        }
        if item.stores:
            # Dealer store cards (spec 2026-10-09): additive within v1.
            view["stores"] = [dict(s) for s in item.stores]
        return view
    return {"id": str(item["_id"]), "sender": "system", "text": item.get("text") or "",
            "sent_at": _shown(item.get("at")), "attachments": []}


def _attachment_view(stored: Mapping[str, Any], signer: Signer) -> Dict[str, Any]:
    url, expires_at = signer(stored.get("url"))
    return {"kind": stored.get("kind"), "url": url, "url_expires_at": expires_at}


def _notice_seq(notice_id: str) -> int:
    seq = notice_id.rsplit("#N", 1)[-1]
    return int(seq) if seq.isascii() and seq.isdigit() else 0


def _in_order(turns: Sequence[TranscriptTurn], notices: Sequence[Mapping[str, Any]]) -> List[Any]:
    """Turns and notices by time; at the same instant, turns before notices;
    then by turn number or notice sequence."""
    keyed: List[Tuple[Tuple[Any, ...], Any]] = [((_moment(t.at) or _NEVER, 0, t.n, ""), t) for t in turns]
    keyed += [((_moment(n.get("at")) or _NEVER, 1, _notice_seq(str(n["_id"])), str(n["_id"])), n) for n in notices]
    keyed.sort(key=lambda pair: pair[0])
    return [item for _, item in keyed]


def list_messages(stores: Any, rider: Rider, conversation_id: str, *, limit: int, before: Optional[str],
                  after: Optional[str], now: datetime, signer: Signer) -> Dict[str, Any]:
    """GET /amiigo/v1/conversations/{conversation_id}/messages.

    No `before` or `after`: the newest `limit` messages. `before` (an
    `older_cursor`): the page before. `after` (a message id): the messages
    after it, with `more_after`. Each page oldest first."""
    conversations = stores.conversations
    if not is_riders_app_chat(stores, rider, conversation_id):
        raise ConversationNotFound()
    anchor = None
    if before:
        cursor = _decode(before, MESSAGES)
        anchor = cursor.get("m")
        if cursor.get("c") != conversation_id or not isinstance(anchor, str):
            raise CursorInvalid()
    items = _in_order(conversations.turns_of(conversation_id), conversations.notices_of(conversation_id))
    position = {message_id(item): i for i, item in enumerate(items)}
    older_cursor, more_after = None, False
    if after:
        if after not in position:
            raise CursorInvalid()
        start = position[after] + 1
        page = items[start:start + limit]
        more_after = start + limit < len(items)
    else:
        if anchor is not None and anchor not in position:
            raise CursorInvalid()
        end = position[anchor] if anchor is not None else len(items)
        start = max(0, end - limit)
        page = items[start:end]
        if start > 0:
            older_cursor = _encode(MESSAGES, c=conversation_id, m=message_id(page[0]))
    return {
        "conversation_id": conversation_id,
        "messages": [message_view(item, signer, now) for item in page],
        "older_cursor": older_cursor,
        "more_after": more_after,
    }


# -- chats -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Chat:
    """The runs of one conversation, as one chat."""

    conversation_id: str
    runs: Tuple[ConversationSummaryItem, ...]
    latest: ConversationSummaryItem
    started_at: str
    last_at: str
    last: datetime


def _app_chat_of(conversations: Any, user_key: str, conversation_id: str,
                 runs: Sequence[ConversationSummaryItem]) -> bool:
    """Ruling 6: the rider's (`user_key`'s) summarised runs of the chat
    (`runs`) are all app runs, nobody else has one (`owner_of`), and every
    run, a run that wrote no summary too, came from the app and from this
    rider (`origins_of`; Ruling 10). A run's origin names its person from its first turn
    (Runtime._note_origin), so an origin with no user key, or another's, is
    a run in which someone else wrote."""
    if not runs or any(run.channel != APP_CHANNEL for run in runs):
        return False
    if conversations.owner_of(conversation_id) != user_key:
        return False
    return all(origin.get("channel") == APP_CHANNEL and origin.get("user_key") == user_key
               for origin in conversations.origins_of(conversation_id))


def is_riders_app_chat(stores: Any, rider: Rider, conversation_id: str) -> bool:
    """Whether this chat is the rider's to read and write in: an Amiigo app
    chat whose every run is theirs, or one whose summary was never written
    (`_unsummarised_app_chat_of`). Anything else is `conversation_not_found`."""
    if is_app_chat_of(stores, rider.user_key, conversation_id):
        return True
    return _unsummarised_app_chat_of(stores.conversations, rider.user_key, conversation_id)


def is_app_chat_of(stores: Any, user_key: str, conversation_id: str) -> bool:
    """`is_riders_app_chat` for the person with this user key, when there is
    no token to hand: a ticket closed in Zoho Desk (amiigo/tickets.py) is
    pushed to the rider only for a chat their history holds (Ruling 18)."""
    conversations = stores.conversations
    runs = [run for run in conversations.runs_of(user_key) if run.conversation_id == conversation_id]
    return _app_chat_of(conversations, user_key, conversation_id, runs)


def _unsummarised_app_chat_of(conversations: Any, user_key: str, conversation_id: str) -> bool:
    """The rider's own app chat whose summary was never written (the final
    review's Important 1). The runtime records where a run came from before
    the turn (Runtime._note_origin) and its working state before the record,
    so a record that failed leaves an origin and a state and no summary; one
    that failed half way leaves the turns too. Theirs when no summary names
    anyone (`owner_of`), every run's origin names this rider on the app (a
    run with no origin, or another's, is someone else's: Ruling 10), and
    the working state, if any, is this rider's app run.

    Not listed (the list is made from the summaries: such a chat has no
    title, bike or status to show) until the rider's next recorded message
    writes its summary; read and written by its id meanwhile."""
    if conversations.owner_of(conversation_id) is not None:
        return False
    origins = conversations.origins_of(conversation_id)
    if not origins or not all(origin.get("channel") == APP_CHANNEL and origin.get("user_key") == user_key
                              for origin in origins):
        return False
    state = conversations.peek(conversation_id)
    return state is None or (state.user_key == user_key and state.channel == APP_CHANNEL)


def is_new_conversation(conversations: Any, conversation_id: str) -> bool:
    """Nothing is recorded under this id: no turn, no summary, no record of
    where a run came from. The app makes a new chat's id itself, and uploads
    the chat's first photo or video under it before its first message."""
    return (conversations.count_turns(conversation_id) == 0
            and conversations.owner_of(conversation_id) is None
            and not conversations.origins_of(conversation_id))


def is_app_chat(conversations: Any, conversation_id: str) -> bool:
    """Whether any run of this conversation came from the Amiigo app, from
    the permanent records only (the plan's Rulings 8 and 9): the record of
    where each run came from (`origins_of`), and the summaries of the person
    the conversation belongs to (`owner_of`, `runs_of`). Never the working
    state, which expires after 48 hours: a website message under an expired
    app chat's id would otherwise start a website run in the rider's chat.

    The store reads summaries by person, so a conversation whose summaries
    name two people (SHARED_OWNER) is decided by its origins alone. Every
    run records where it came from, tried again each turn until it is
    written (Runtime._note_origin), and its channel is the app's for as long
    as only the rider writes in it."""
    if any(origin.get("channel") == APP_CHANNEL for origin in conversations.origins_of(conversation_id)):
        return True
    owner = conversations.owner_of(conversation_id)
    if owner is None or owner == SHARED_OWNER:
        return False
    return any(run.conversation_id == conversation_id for run in conversations.runs_of(owner, channel=APP_CHANNEL))


def rider_may_use(stores: Any, rider: Rider, conversation_id: str) -> bool:
    """Whether the rider may add to this chat: a new one, or their own app
    chat, summarised or not (`is_riders_app_chat`). Anything else is
    `conversation_not_found`, as in the history.

    A chat with nothing recorded may still have a working state, from a
    turn whose records failed to write, and that state holds what was said.
    It is new to the rider only when the state is their own app run."""
    conversations = stores.conversations
    if is_new_conversation(conversations, conversation_id):
        state = conversations.peek(conversation_id)
        return state is None or (state.user_key == rider.user_key and state.channel == APP_CHANNEL)
    return is_riders_app_chat(stores, rider, conversation_id)


def _chats(runs: Sequence[ConversationSummaryItem]) -> List[_Chat]:
    """One chat per conversation: started when its first run started, last
    active when its last run was, and otherwise as its latest run says. Most
    recent activity first, then by conversation id."""
    by_id: Dict[str, List[ConversationSummaryItem]] = {}
    for run in runs:
        by_id.setdefault(run.conversation_id, []).append(run)
    chats = []
    for conversation_id, mine in by_id.items():
        latest = max(mine, key=lambda r: (_moment(r.started_at) or _NEVER, _moment(r.last_at) or _NEVER))
        first = min(mine, key=lambda r: _moment(r.started_at) or _moment(r.last_at) or _NEVER)
        last = max(mine, key=lambda r: _moment(r.last_at) or _NEVER)
        chats.append(_Chat(conversation_id, tuple(mine), latest, first.started_at or first.last_at, last.last_at,
                           _moment(last.last_at) or _NEVER))
    chats.sort(key=lambda c: c.conversation_id)
    chats.sort(key=lambda c: c.last, reverse=True)  # stable: ties stay by id
    return chats


def _ticket_view(stores: Any, reference: Optional[str]) -> Optional[Dict[str, Any]]:
    """The chat's ticket. Open when no record has the reference (the mock's)
    or the record is from before support statuses."""
    if not reference:
        return None
    tickets = getattr(stores, "tickets", None)
    status = tickets.ticket_status(reference) if tickets is not None else None
    if status is None:
        return {"reference": reference, "status": SUPPORT_OPEN, "closed_at": None}
    return {"reference": status["reference"], "status": status["status"], "closed_at": _shown(status["closed_at"])}


def _chat_view(stores: Any, chat: _Chat, now: datetime) -> Dict[str, Any]:
    run, conversations = chat.latest, stores.conversations
    bike = ({"product_name": run.product_name, "frame_number": run.frame_number}
            if run.product_name or run.frame_number else None)
    return {
        "conversation_id": chat.conversation_id,
        "channel": run.channel,
        "title": run.title or GENERAL_TITLE,
        "started_at": _shown(chat.started_at),
        "last_message_at": _shown(chat.last_at),
        "bike": bike,
        "status": HANDED_TO_SUPPORT if run.outcome == "escalated" else OPEN,
        "ticket": _ticket_view(stores, run.ticket_id),
        "message_count": (conversations.count_turns(chat.conversation_id)
                          + len(conversations.notices_of(chat.conversation_id))),
        "can_continue": chat.last > now - CAN_CONTINUE,
    }


def list_conversations(stores: Any, rider: Rider, *, limit: int, cursor: Optional[str], channel: Optional[str],
                       now: datetime) -> Dict[str, Any]:
    """GET /amiigo/v1/conversations: the rider's app chats, most recent
    activity first, `limit` a page, from `cursor` (a `next_cursor`) when
    given. `channel` can only narrow: every chat listed is an app chat."""
    since: Optional[Tuple[datetime, str]] = None
    if cursor:
        fields = _decode(cursor, CHATS)
        moment, conversation_id = _moment(fields.get("t")), fields.get("c")
        if moment is None or not isinstance(conversation_id, str):
            raise CursorInvalid()
        since = (moment, conversation_id)
    conversations = stores.conversations
    page: List[_Chat] = []
    more = False
    if channel not in (None, APP_CHANNEL):
        return {"conversations": [], "next_cursor": None}
    # Every run, whatever its channel: a chat with any website run is left out.
    for chat in _chats(conversations.runs_of(rider.user_key)):
        if since is not None and not (chat.last < since[0] or (chat.last == since[0]
                                                               and chat.conversation_id > since[1])):
            continue
        if not _app_chat_of(conversations, rider.user_key, chat.conversation_id, chat.runs):
            continue
        if len(page) == limit:
            more = True
            break
        page.append(chat)
    next_cursor = _encode(CHATS, t=page[-1].last.isoformat(), c=page[-1].conversation_id) if more else None
    return {"conversations": [_chat_view(stores, chat, now) for chat in page], "next_cursor": next_cursor}
