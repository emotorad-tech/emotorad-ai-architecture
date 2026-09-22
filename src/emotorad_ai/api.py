"""HTTP entrypoint — the same skeleton `cli.py` drives, served over HTTP so it
can run as an ECS/EC2 service instead of only from a terminal.

The model path is chosen by `EMOTORAD_AI_MODE` — matching `cli.py`'s own
`--offline`/`--anthropic`/`--bedrock` flags: `offline` for local runs with no
credentials (the default, and what a bare `uvicorn emotorad_ai.api:app`
gives you), `anthropic` on the deploy, and `bedrock` via the instance role.

    EMOTORAD_AI_MODE=anthropic ANTHROPIC_API_KEY=... uvicorn emotorad_ai.api:app   # deploy default
    EMOTORAD_AI_MODE=bedrock uvicorn emotorad_ai.api:app                          # instance role

Run locally:

    pip install -r requirements-dev.txt
    export EMOTORAD_AI_PLAYGROUND_USER=dev EMOTORAD_AI_PLAYGROUND_PASSWORD=dev
    PYTHONPATH=src streamlit run src/emotorad_ai/playground.py \\
        --server.port 8501 --server.address 127.0.0.1 \\
        --server.baseUrlPath playground --server.headless true \\
        --server.enableCORS false --server.enableXsrfProtection false &
    PYTHONPATH=src uvicorn emotorad_ai.api:app --reload
    # then open http://127.0.0.1:8000/ — redirects into the playground,
    # reverse-proxied at /playground (see below) rather than its own port,
    # so the same single exposed port works unchanged once deployed.
    # Without EMOTORAD_AI_PLAYGROUND_USER/PASSWORD set, /playground answers
    # 503 rather than opening unauthenticated — see require_playground_auth.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .adapters import WebsiteChatAdapter
from .attachments import MAX_ATTACHMENTS, AttachmentError, validate as validate_attachments
from .config import load_settings
from .config_store import SECRET_ID_ENV
from .contract import new_conversation_id
from .fulfilment import ItemCodes, ReplacementOrders
from .media import load_catalogue
from .identity import IdentityResolver
from .address import PincodeDirectory
from .llm import select_llm
from .location import NominatimGeocoder, describe_location, resolve_location
from .observability import EventLog
from .ratelimit import RateLimiter
from .runtime import Runtime
from .storage.keys import cluster_of, is_customer_key, is_valid_key
from .storage.s3 import StorageError, store_from_env
from .storage.uploads import UploadError, UploadRegistry
from .tools.mocks import build_registry
from .tools.oms import OMSClient, live_account_finder, live_warranty_source
from .tools.verification import VerificationStore, apply_verified_identity
from .video_summary import VideoSummaryError, summariser_from_env

MODE = os.environ.get("EMOTORAD_AI_MODE", "offline")
# Set by docker/start.py's loader to the number of names it exported (as a
# string), after load_into_environ() succeeds. Reported by /health so a
# container that started without its secret — or whose secret exported
# nothing — is visible from the deploy log, distinct from one that never had
# a secret configured at all. The secret ID must be set for a secret to be
# configured; no secret ID means nothing was configured, so we check that
# first before looking at the export count.
_CONFIG_EXPORTED = os.environ.get("EMOTORAD_AI_CONFIG_EXPORTED")
if not os.environ.get(SECRET_ID_ENV):
    SECRETS_STATE = "not configured"
elif _CONFIG_EXPORTED and int(_CONFIG_EXPORTED) > 0:
    SECRETS_STATE = "loaded"
else:
    SECRETS_STATE = "empty"

settings = load_settings()

# Media: None when EMOTORAD_AI_MEDIA_BUCKET is unset. Then /uploads and /media
# answer 503 with the reason, and the runtime sends no S3 evidence to the model.
MEDIA_STORE = store_from_env()
UPLOADS = UploadRegistry(MEDIA_STORE) if MEDIA_STORE is not None else None

# Video evidence: None when GEMINI_API_KEY is unset, and then a claimed clip
# reaches the model as sampled frames as before. With a key, the clip is
# described once here at ingest and only the text travels further.
VIDEO_SUMMARISER = summariser_from_env()

_logger = logging.getLogger(__name__)

# Verification is per conversation and has to outlive a single request, so the
# store is module-level. Without one, `build_registry` does not register
# `request_identity_verification` or `verify_identity` at all, and an anonymous
# visitor asked for their number has no way to prove it — the flow dead-ends.
verification_store = VerificationStore()

# The guide photos and clips the agent may show. Loaded once: it is authored
# content in the repo, not per-request state. load_catalogue() raises on a
# malformed catalogue, and that is deliberate: a broken catalogue should fail
# the deploy's health check, not silently drop guide pictures from every
# conversation.
#
# `send_guide_media` was named in the agents' TOOL_NAMES all along, but no
# catalogue was ever passed here, so the tool was never registered and was
# filtered straight back out of the slice. The prompt told the model to point at
# the button it was describing, and it had nothing to point with.
GUIDE_MEDIA = load_catalogue()

# conversation_id -> the keys already shown in it. Module-level because "already
# sent" only means anything across turns, and a request-scoped dict would let
# the agent send the same photo every turn.
sent_media: dict = {}

# The replacement orders the bot places. Mocked: nothing reaches the OMS from
# here yet. Module-level so "already on its way" holds across conversations.
replacement_orders = ReplacementOrders()


def _build_registry():
    """Real OMS reads when a key is configured, fixtures when it is not.

    The key is the only switch. Without it every lookup is a fixture, which is
    what the tests and a fresh clone get, and `/chat` will happily name a bike
    that belongs to nobody. With it, a phone number reaches the live purchase
    table and the bikes, frame numbers and purchase dates are the customer's own.

    Ticketing stays mocked either way. That combination is worth knowing about:
    real bike details followed by a ticket number that exists nowhere is more
    convincing, and therefore worse, than fixtures all the way through.
    """
    if not os.environ.get("EMOTORAD_OMS_API_KEY"):
        return build_registry(
            verification=verification_store,
            guide_media=GUIDE_MEDIA,
            sent_media=sent_media,
            replacement_orders=replacement_orders,
            item_codes=ItemCodes(),
            approval_mode=settings.approval_mode,
            location_sharing=True,
        )
    client = OMSClient()
    return build_registry(
        verification=verification_store,
        warranty_source=live_warranty_source(client),
        account_finder=live_account_finder(client),
        guide_media=GUIDE_MEDIA,
        sent_media=sent_media,
        replacement_orders=replacement_orders,
        item_codes=ItemCodes(),
        approval_mode=settings.approval_mode,
        location_sharing=True,
    )


registry = _build_registry()

# The reverse geocoder behind "Share my location". OpenStreetMap's public
# service for this LAN test server; a production provider swaps in here. The
# tests replace it with a fake. See location.py for what is and is not trusted
# from it.
geocoder = NominatimGeocoder()
pincode_directory = PincodeDirectory.load()
resolver = IdentityResolver(registry)
log = EventLog(path=settings.log_path, to_stdout=settings.log_to_stdout)
runtime = Runtime(
    settings=settings,
    registry=registry,
    # offline   -> the fixed planner: no model, no key, no tokens spent.
    # anthropic -> Claude via the Anthropic API, keyed from the environment.
    #              Temporary, and the transport every tuned prompt was tuned
    #              against. See AnthropicClaude for why it exists.
    # bedrock   -> BedrockClaude through the instance role.
    # select_llm raises LLMConfigError at import when the mode cannot be served,
    # so the deploy's health check fails instead of the first customer message.
    llm=select_llm(MODE, settings),
    log=log,
    resolver=resolver,
    # Website chat is the one surface that arrives anonymous. Every other
    # channel resolves identity upstream — WhatsApp and Amiigo supply a verified
    # phone natively — so the agents' own TOOL_NAMES stay right for them and the
    # verification tools are added only here, where the agent has to establish
    # identity inside the conversation.
    self_service_identity=True,
    # The phone this conversation proves mid-turn. The model verifies a code and
    # looks the customer up in the same assistant turn, so a phone snapshotted
    # before the first tool ran is already stale by the second one.
    phone_resolver=verification_store.verified_phone,
    media_store=MEDIA_STORE,
)
adapter = WebsiteChatAdapter(resolver)

# Claimed-attachment kind, as the adapter expects it — never string tricks.
_ATTACHMENT_KIND = {"images": "image", "videos": "video", "docs": "document"}

app = FastAPI(title="Emotorad AI — battery support")


class MessageIn(BaseModel):
    conversation_id: Optional[str] = None
    # No default. It used to be "sess-ananya", a fixture session mapping to
    # +919876543210, which meant every caller who did not set one arrived
    # already verified as a test customer. Harmless while the tools were
    # fixtures; not harmless once the OMS key is set, because that fixture phone
    # is then looked up for real and returns somebody's actual bikes to whoever
    # opened the page.
    session_token: Optional[str] = None
    # The website's first-party cookie: present for every visitor, logged in or
    # not, and the identifier this channel is specified to arrive with. An
    # anonymous visitor is a valid identity, not a failure — they get a cluster
    # and generic help, and must verify a phone before anything personal.
    em_aid: Optional[str] = None
    text: str
    pill: Optional[str] = None
    # What the customer sent with the message. Two shapes, one field:
    #
    #   {"kind": "image", "url": "data:image/jpeg;base64,..."}
    #       Inline and never stored. The evidence gate asks for a picture of the
    #       terminal before it will conclude a fault, and this is how the chat
    #       page answers it. `attachments.validate` holds the limits, which are
    #       the whole security story for that path.
    #   {"upload_id": "upl_..."}
    #       An object already PUT to S3 through `POST /uploads`. The id is minted
    #       by the server, claimed once, and becomes an `s3://` attachment the
    #       runtime fetches with the instance role. Needs media configured.
    #
    # They mix freely in one message; `_inbound_attachments` splits them.
    attachments: Optional[List[dict]] = None
    # Pin the conversation to one sub-agent, the way the playground's sidebar
    # picks one. Triage only runs while `state.agent is None`, so naming an
    # agent skips it and the tuned prompt runs the conversation end to end —
    # identification, diagnosis and all — which is what it was written to do and
    # what every transcript we tuned against exercised.
    #
    # Routing through triage instead put an unfinished component in front of it:
    # `classify_issue` is keyword-only, returns None for "bike nahi chal rahi",
    # and the None branch re-asks the same sentence forever. Its own docstring
    # says None means "the model decides", and nothing asks the model yet.
    agent: Optional[str] = None
    # A tapped "Share my location". Turned into the customer's own message
    # (pincode and area) before anything else sees it; the coordinates are
    # used once for that and kept nowhere. See location.py.
    location: Optional["LocationIn"] = None


class LocationIn(BaseModel):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


MessageIn.model_rebuild()


# The agents this surface may pin. Website chat serves customers, so the dealer
# agent is not on the list: its tools all inject a dealer_id that a customer
# message does not carry, so nothing leaks, but it would answer a customer in a
# dealer's voice and spend tokens doing it.
#
# Anything outside this set is refused here rather than written into the
# conversation. Writing first was the bug: an unknown name went into the state,
# every later turn read it back, and the conversation returned HTTP 500 forever
# with no way for the customer to recover.
CHAT_AGENTS = ("battery_support", "motor_support", "late_warranty")


# Every /message call reaches a real model and the real OMS, and the endpoint has
# no authentication, so an open loop against it spends money. Twenty a minute is
# far above what a person typing can produce and far below what a script can.
#
# This is not authentication and does not make the endpoint safe to expose. Who
# may use the chat is still undecided; this only bounds what an anonymous caller
# can cost while that decision is outstanding.
message_limiter = RateLimiter(limit=20, window_seconds=60.0)
# Same bound on presigns. Each one costs a signature and a pending entry in
# memory, and a message can carry at most a few attachments, so a caller
# minting presigns faster than they can send messages is not a customer.
upload_limiter = RateLimiter(limit=20, window_seconds=60.0)


class AttachmentOut(BaseModel):
    kind: str
    url: str
    mime_type: Optional[str] = None
    caption: Optional[str] = None
    poster: Optional[str] = None


class MessageOut(BaseModel):
    conversation_id: str
    text: str
    escalated: bool
    ticket_id: Optional[str]
    handled_by: Optional[str]
    # Guide photos and clips the agent chose to send. `Reply` has carried these
    # all along; this layer computed them and then dropped them on the floor, so
    # over HTTP the bot could never show anyone the SOC button it was describing.
    attachments: List[AttachmentOut] = []
    # Controls to render under the reply, such as the "Share my location"
    # button: {"kind": "request_location", "label": ...}. Named by code in a
    # tool result, never typed by the model.
    actions: List[dict] = []


class AssetPath(BaseModel):
    programme: str
    category: str
    kind: str
    slug: str


class UploadIn(BaseModel):
    session_token: str = ""
    # The website cookie, as on MessageIn: a visitor who has not verified a
    # phone still resolves to their anonymous cluster, so they can send a video
    # before the OTP step. Either this or a session that resolves is enough.
    em_aid: Optional[str] = None
    conversation_id: Optional[str] = None
    tree: str
    mime_type: str
    size_bytes: int
    path: Optional[AssetPath] = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": MODE,
        "secrets": SECRETS_STATE,
        "media": "configured" if MEDIA_STORE is not None else "not configured",
        "video_summary": "gemini" if VIDEO_SUMMARISER is not None else "frames",
    }


def _require_media() -> None:
    if MEDIA_STORE is None or UPLOADS is None:
        raise HTTPException(503, "Media storage is not configured on this deployment (EMOTORAD_AI_MEDIA_BUCKET).")


def _cluster_for_session(session_token: str, em_aid: Optional[str] = None) -> str:
    """The identity-graph cluster the caller resolves to. Never the phone.

    Resolved exactly as the website adapter does for /message: a session that
    maps to a verified phone wins, otherwise the cookie's anonymous cluster.
    """
    _, identity = resolver.resolve_website(em_aid or None, session_token or None)
    if not identity.cluster_id:
        raise HTTPException(400, "session does not resolve to a customer")
    return identity.cluster_id


@app.post("/uploads")
def post_upload(body: UploadIn, request: Request) -> Dict[str, Any]:
    if not upload_limiter.allow(request.client.host if request.client else None):
        raise HTTPException(
            status_code=429,
            detail="Too many uploads. Wait a moment and try again.",
        )
    _require_media()
    try:
        if body.tree == "customers":
            if not body.conversation_id:
                raise HTTPException(400, "conversation_id is required for customer uploads")
            cluster_id = _cluster_for_session(body.session_token, body.em_aid)
            # A conversation id that already exists must have been started under
            # the same cluster; a brand new one is accepted as-is (minted by the
            # client on turn one, before /message has ever seen it).
            existing = runtime.conversations.peek(body.conversation_id)
            if existing is not None and existing.cluster_id is not None and existing.cluster_id != cluster_id:
                raise HTTPException(403, "not your conversation")
            pending, presign = UPLOADS.begin_customer(cluster_id, body.conversation_id, body.mime_type, body.size_bytes)
        elif body.tree == "assets":
            require_playground_auth(request)
            if body.path is None:
                raise HTTPException(400, "path is required for asset uploads")
            pending, presign = UPLOADS.begin_asset(
                body.path.programme, body.path.category, body.path.kind, body.path.slug, body.mime_type, body.size_bytes
            )
        else:
            raise HTTPException(400, "tree must be 'customers' or 'assets'")
    except UploadError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return {"upload_id": pending.upload_id, "key": pending.key, **presign}


@app.get("/media/{key:path}")
def get_media(key: str, session_token: str = "") -> RedirectResponse:
    _require_media()
    if not is_valid_key(key):
        # A prefix test (is_customer_key/is_asset_key) lets a path like
        # `assets/../customers/...` through; the full grammar does not.
        raise HTTPException(404, "no such media")
    if is_customer_key(key):
        # Session token only, no em_aid: the chat page never reads /media (it
        # renders its local preview), and a cookie identifies a browser, not
        # a person, so it must not unlock a verified customer's objects.
        if cluster_of(key) != _cluster_for_session(session_token):
            raise HTTPException(403, "not your attachment")
    # A fresh 15-minute link every time, so a transcript rendered later still loads.
    return RedirectResponse(MEDIA_STORE.presign_get(key), status_code=302)


def _inbound_attachments(body: MessageIn) -> List[Dict[str, Any]]:
    """What the customer sent, in the shape the adapter takes, in the order they
    sent it.

    Two ways in, and a message may mix them. An inline `data:` URL is the
    website chat's photo-evidence path: validated here against
    `attachments.validate` — type, size, count, base64 that decodes — carried in
    the request and stored nowhere. An `{"upload_id": ...}` is an object already
    PUT to S3 through `POST /uploads`; it is checked against this session and
    claimed once, and becomes an `s3://` key the runtime reads with the instance
    role. No S3 URL ever reaches the model either way.

    The count limit is on the total, not on each path, so adding the presigned
    route cannot be used to send more pictures than the inline one allows.
    """
    items = [item for item in (body.attachments or []) if item]
    if not items:
        return []
    if len(items) > MAX_ATTACHMENTS:
        raise AttachmentError(
            "Too many attachments: %d sent, %d allowed." % (len(items), MAX_ATTACHMENTS)
        )

    # Validated as a batch, because that is where the count and type rules live.
    # The originals are then passed through unchanged, exactly as the inline path
    # did before: `user_content` decodes the data URL when the turn is built.
    inline = [item for item in items if not item.get("upload_id")]
    validate_attachments(inline)

    uploaded = [item for item in items if item.get("upload_id")]
    claims: Dict[str, Dict[str, Any]] = {}
    if uploaded:
        _require_media()
        caller_cluster = _cluster_for_session(body.session_token, body.em_aid)
        try:
            for item in uploaded:
                upload_id = item["upload_id"]
                # Check ownership on the read-only `peek` before ever calling
                # `claim`: `claim` pops the pending entry, so if we claimed
                # first, a 403 for the wrong session (or a stale/foreign
                # token) would have already destroyed the id and the
                # rightful owner's retry would 404.
                pending = UPLOADS.peek(upload_id)
                if pending is None:
                    raise HTTPException(404, "unknown or expired upload id")
                if pending.tree != "customers" or cluster_of(pending.key) != caller_cluster:
                    # Not a customer upload at all (e.g. an assets/ upload id) or
                    # a customer upload from a different cluster: either way,
                    # this session did not upload it. No claim happens, so the
                    # id is still there for whoever actually owns it.
                    raise HTTPException(403, "not your upload")
                claimed = UPLOADS.claim(upload_id)
                assert is_customer_key(claimed.key) and cluster_of(claimed.key) == caller_cluster
                claims[upload_id] = {
                    "kind": _ATTACHMENT_KIND[claimed.kind],
                    "url": "s3://" + claimed.key,
                    "mime_type": claimed.mime,
                }
                if claimed.kind == "videos":
                    summary = _summarise_video(claimed.key, claimed.mime)
                    if summary is not None:
                        claims[upload_id]["summary"] = summary
        except UploadError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    return [claims[item["upload_id"]] if item.get("upload_id") else item for item in items]


def _summarise_video(key: str, mime: str) -> Optional[str]:
    """The clip described once, or None so the frames fallback runs later.

    Failures are logged by class name only: a provider error can echo request
    content, and `StorageError` can carry a key. Neither belongs in a log line
    that the customer's message did not put there.
    """
    if VIDEO_SUMMARISER is None:
        return None
    try:
        data = MEDIA_STORE.get_bytes(key)
        return VIDEO_SUMMARISER.summarise(data, mime, name=key.rsplit("/", 1)[-1])
    except StorageError as exc:
        _logger.warning("video summary skipped: %s (store)", type(exc).__name__)
    except VideoSummaryError as exc:
        _logger.warning("video summary skipped: %s", exc)
    return None


@app.post("/message", response_model=MessageOut)
def post_message(body: MessageIn, request: Request) -> MessageOut:
    if not message_limiter.allow(request.client.host if request.client else None):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Wait a moment and try again.",
        )
    conversation_id = body.conversation_id or new_conversation_id()
    try:
        attachments = _inbound_attachments(body)
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    text = body.text
    if body.location is not None:
        # Resolved here, before the message exists, so the coordinates never
        # become part of anything that is logged or handed to the model.
        located = resolve_location(
            body.location.latitude, body.location.longitude, geocoder, pincode_directory
        )
        text = describe_location(located)

    # Record which cluster started this conversation, the first time we see
    # it, so a later presign under the same conversation id can be checked
    # against who actually owns it (see post_upload). Best-effort: a session
    # that does not resolve to a customer is a legitimate anonymous chat and
    # must not 400 here just because we tried to attribute a cluster to it.
    try:
        message_cluster = _cluster_for_session(body.session_token)
    except HTTPException:
        message_cluster = None
    if message_cluster is not None:
        state = runtime.conversations.get(conversation_id)
        if state.cluster_id is None:
            state.cluster_id = message_cluster

    message = adapter.to_message(
        {
            "conversation_id": conversation_id,
            "session_token": body.session_token,
            "em_aid": body.em_aid,
            "text": text,
            "pill": body.pill,
            "attachments": attachments,
        }
    )
    # The proof this conversation has already given. Without this the customer
    # types a correct code and the very next turn still resolves anonymous, so
    # the warranty lookup is refused for want of a phone exactly as it was
    # before they bothered.
    message = apply_verified_identity(message, verification_store)
    if body.agent:
        if body.agent not in CHAT_AGENTS:
            raise HTTPException(
                status_code=400,
                detail="Unknown agent %r. Expected one of: %s"
                % (body.agent, ", ".join(CHAT_AGENTS)),
            )
        runtime.conversations.get(conversation_id).route_to(body.agent)
    reply = runtime.handle(message)
    return MessageOut(
        conversation_id=conversation_id,
        text=reply.text,
        escalated=reply.escalated,
        ticket_id=reply.ticket_id,
        handled_by=reply.handled_by,
        attachments=[
            AttachmentOut(
                kind=attachment.kind,
                url=attachment.url,
                mime_type=attachment.mime_type,
                caption=attachment.caption,
                poster=attachment.poster,
            )
            for attachment in reply.attachments
        ],
        actions=list(reply.actions),
    )


# Reading the code off the screen replaces the SMS that is not wired yet, the
# way the playground shows it in its sidebar. It is a verification bypass by
# definition: it hands the pending code for any conversation to anyone who asks.
#
# So it is off unless switched on, never on unless switched off. A deployment
# that forgets to set anything gets 404, and the only way to enable it is to
# have decided to. Delete the whole route once an SMS provider is wired; nothing
# else depends on it.
DEV_CODES = os.environ.get("EMOTORAD_AI_DEV_CODES") == "1"


@app.get("/dev/verification/{conversation_id}")
def dev_verification(conversation_id: str) -> dict:
    if not DEV_CODES:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "conversation_id": conversation_id,
        "pending_code": verification_store.pending_code(conversation_id),
        "verified_phone": verification_store.verified_phone(conversation_id),
        "attempts_left": verification_store.attempts_left(conversation_id),
    }


WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
CHAT_FILE = WEB_DIR / "emotorad-support-chat-dev.html"


@app.get("/chat", response_class=HTMLResponse)
def chat() -> HTMLResponse:
    """The customer chat UI, talking to /message on this same origin.

    Served from `web/` rather than bundled, so the file stays the one a designer
    opens directly in a browser. There is no auth on it yet and it must not be
    exposed publicly: every tool behind it is still a fixture, so a real customer
    would be told about a bike that is not theirs and promised a ticket that does
    not exist. Local and internal use only until the OMS key and a real ticketing
    integration are in place.
    """
    try:
        return HTMLResponse(CHAT_FILE.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(status_code=404, detail="chat UI not found at web/")


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/playground/")


# --- Prompt-tuning playground, reverse-proxied ------------------------------
#
# playground.py is a Streamlit app — it needs Streamlit's own server, so it
# can't be rendered inline by FastAPI. It runs as a second process in the
# same container (see Dockerfile), bound to localhost only and started with
# `--server.baseUrlPath playground` so every URL it generates already carries
# the /playground prefix. This app just forwards matching requests to it
# byte-for-byte, so the one port this service already exposes (see
# docs/Emotorad_AWS_Deployment_Plan.md — security group allows 443/80 only)
# is enough; nothing new needs opening for the internal team to reach it.
PLAYGROUND_UPSTREAM = os.environ.get("EMOTORAD_AI_PLAYGROUND_UPSTREAM", "127.0.0.1:8501")
_playground_client = httpx.AsyncClient(base_url="http://%s" % PLAYGROUND_UPSTREAM)

# The playground has no auth of its own and takes an Anthropic API key as
# input, so — since the deployed instance's security group has 443 open to
# the whole internet, not just the internal team (see the deployment plan) —
# it must never be reachable without a credential check in front of it.
PLAYGROUND_USER = os.environ.get("EMOTORAD_AI_PLAYGROUND_USER", "")
PLAYGROUND_PASSWORD = os.environ.get("EMOTORAD_AI_PLAYGROUND_PASSWORD", "")


def _basic_auth_ok(header_value: Optional[str]) -> bool:
    # Fail closed: unconfigured credentials must never mean "let everyone in."
    if not PLAYGROUND_USER or not PLAYGROUND_PASSWORD:
        return False
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[len("Basic ") :]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, _, password = decoded.partition(":")
    # constant-time comparisons — a timing difference on a login endpoint is
    # itself a way to brute-force a credential one character at a time.
    return secrets.compare_digest(username, PLAYGROUND_USER) and secrets.compare_digest(
        password, PLAYGROUND_PASSWORD
    )


def require_playground_auth(request: Request) -> None:
    if not PLAYGROUND_USER or not PLAYGROUND_PASSWORD:
        raise HTTPException(503, "Playground auth is not configured on this deployment.")
    if not _basic_auth_ok(request.headers.get("authorization")):
        raise HTTPException(
            401,
            "Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="Emotorad AI playground"'},
        )

# Response headers that describe the hop from Streamlit to us, not to the
# browser — passing them through would leave the client trying to decode a
# body we've already decoded, or reusing a connection that doesn't exist.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "upgrade",
}


# Streamlit's file uploader (1.64) sends the file with PUT to
# /_stcore/upload_file/<session>/<file_id> and removes it with DELETE; PATCH
# and OPTIONS are included so the proxy doesn't have to be revisited for the
# next Streamlit upstream call it happens to use. A proxy that only forwards
# GET/POST/HEAD answers those with 405 before Streamlit ever sees them,
# silently breaking every file upload in the playground (admin media-upload
# form and the chat's photo/video attachment alike).
@app.api_route(
    "/playground",
    methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(require_playground_auth)],
)
@app.api_route(
    "/playground/{rest:path}",
    methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(require_playground_auth)],
)
async def playground_http_proxy(request: Request, rest: str = "") -> StreamingResponse:
    upstream_request = _playground_client.build_request(
        request.method,
        httpx.URL(path=request.url.path, query=request.url.query.encode("utf-8")),
        headers=[(k, v) for k, v in request.headers.raw if k.lower() not in (b"host", b"connection")],
        content=await request.body(),
    )
    upstream_response = await _playground_client.send(upstream_request, stream=True)
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers={k: v for k, v in upstream_response.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS},
        background=BackgroundTask(upstream_response.aclose),
    )


@app.websocket("/playground/{rest:path}")
async def playground_ws_proxy(websocket: WebSocket, rest: str) -> None:
    # Depends()-based auth isn't reliable on websocket routes in FastAPI, so
    # this checks the same handshake header by hand before ever accepting —
    # a browser that's already passed Basic Auth on the HTTP routes replays
    # the cached credential here automatically, so no separate login step.
    if not _basic_auth_ok(websocket.headers.get("authorization")):
        await websocket.close(code=1008)  # policy violation
        return

    # Streamlit's live-reactivity channel — without this it loads once and
    # never updates, which looks like a working page until you click anything.
    #
    # The browser's Streamlit client always offers a Sec-WebSocket-Protocol
    # value, and RFC 6455 requires the response to echo back exactly one of
    # them when the client offers any — silently dropping the header (as an
    # earlier version of this function did) makes Chrome reject the upgrade
    # ("sent non-empty header but no response was received"). Echoing the
    # *raw* header back verbatim is the other failure mode: some
    # uvicorn/websockets version combinations then emit the header twice in
    # the same response ("must not appear more than once"). Splitting off
    # just the first offered value avoids both.
    requested_protocol = websocket.headers.get("sec-websocket-protocol")
    subprotocol = requested_protocol.split(",")[0].strip() if requested_protocol else None
    await websocket.accept(subprotocol=subprotocol)
    upstream_url = "ws://%s/playground/%s" % (PLAYGROUND_UPSTREAM, rest)
    async with websockets.connect(upstream_url) as upstream:

        async def from_client() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if "text" in message and message["text"] is not None:
                        await upstream.send(message["text"])
                    elif "bytes" in message and message["bytes"] is not None:
                        await upstream.send(message["bytes"])
            finally:
                await upstream.close()

        async def from_upstream() -> None:
            try:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            finally:
                await websocket.close()

        await asyncio.gather(from_client(), from_upstream(), return_exceptions=True)
