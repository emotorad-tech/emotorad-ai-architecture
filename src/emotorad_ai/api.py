"""HTTP entrypoint — the same skeleton `cli.py` drives, served over HTTP so it
can run as an ECS/EC2 service instead of only from a terminal.

The model path is chosen by `EMOTORAD_AI_MODE` — matching `cli.py`'s own
`--offline`/`--anthropic`/`--bedrock` flags: `offline` for local runs with no
credentials (the default, and what a bare `uvicorn emotorad_ai.api:app`
gives you), `anthropic` on the deploy, and `bedrock` via the instance role.

    EMOTORAD_AI_MODE=anthropic ANTHROPIC_API_KEY=... uvicorn emotorad_ai.api:app   # deploy default
    EMOTORAD_AI_MODE=bedrock uvicorn emotorad_ai.api:app                          # instance role

Or Jev routing with the OpenRouter models (needs OPENROUTER_API_KEY; sends
customer text outside AWS, so not for real customers until signed off):

    EMOTORAD_AI_MODE=openrouter uvicorn emotorad_ai.api:app

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
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .adapters import WebsiteChatAdapter
from .client_ip import client_ip, trusted_from_env
from . import origin as origin_place
from . import photo_check
from . import erasure as erasure_rules
from .attachments import MAX_ATTACHMENTS, AttachmentError, validate as validate_attachments
from .config import load_settings
from .config_store import SECRET_ID_ENV
from .contract import new_conversation_id
from .fulfilment import ItemCodes, ReplacementOrders
from .media import load_catalogue
from .media import sendable as media_sendable
from .identity import IdentityResolver
from .address import PincodeDirectory
from .location import NominatimGeocoder, describe_location, resolve_location
from .observability import EventLog
from .ratelimit import RateLimiter
from .conversation import StoreUnavailable, utc_now_iso
from .media_records import media_record
from .runtime import Runtime
from .storage import keys
from .storage.keys import KeyValidationError, cluster_of, is_customer_key, is_valid_key
from .storage.s3 import StorageError, store_from_env
from . import tracing
from .storage.uploads import UploadError, UploadRegistry
from .tools import amigo as amigo_tools
from .tools import fixtures
from .tools.mocks import build_registry
from .tools.oms import OMSClient, live_account_finder, live_warranty_source
from .tools.verification import MockOtpSender, VerificationStore, apply_verified_identity
from .video_summary import VideoSummaryError, summariser_from_env
from .wiring import build_models, build_stores

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
MODE = settings.mode
log = EventLog(path=settings.log_path, to_stdout=settings.log_to_stdout)
# Where conversations and write receipts live: memory by default, MongoDB with
# EMOTORAD_STORE=mongodb (it survives restarts; needs EMOTORAD_MONGO_URI).
stores = build_stores(settings, log=log)
# The models for the mode: one Claude client for offline, anthropic and
# bedrock (select_llm, which raises LLMConfigError at import when the mode
# cannot be served, so the deploy's health check fails instead of the first
# customer message), or Jev plus the OpenRouter models for openrouter.
models = build_models(settings)

# Media: None when EMOTORAD_AI_MEDIA_BUCKET is unset. Then /uploads and /media
# answer 503 with the reason, and the runtime sends no S3 evidence to the model.
MEDIA_STORE = store_from_env()
UPLOADS = UploadRegistry(MEDIA_STORE) if MEDIA_STORE is not None else None

# Video evidence: described once here at ingest, and only the text travels
# further. Through OpenRouter when OPENROUTER_API_KEY is set (the person's
# choice, 2026-09-29), Gemini direct with EMOTORAD_VIDEO_SUMMARY=gemini or when
# only GEMINI_API_KEY is set; with neither, a claimed clip reaches the model as
# sampled frames as before.
VIDEO_SUMMARISER = summariser_from_env()
# The safety look at every photo (photo_check.py), or None without the
# OpenRouter key: photos then go on as before.
PHOTO_CHECKER = photo_check.photo_checker_from_env()
# One deadline for all the photos in a message, checked at the same time: the
# reply is never held longer than this, however many photos there are.
PHOTO_CHECK_DEADLINE_SECONDS = 15.0

_logger = logging.getLogger(__name__)

# Verification is per conversation and has to outlive a single request, so the
# store is module-level. Without one, `build_registry` does not register
# `request_identity_verification` or `verify_identity` at all, and an anonymous
# visitor asked for their number has no way to prove it — the flow dead-ends.
verification_store = VerificationStore()

# Sends the one-time code. A stand-in until the OTP service is wired (the
# person, 2026-09-30): it sends nothing and logs the masked number; the code
# itself is read from /dev/verification on a test server.
OTP_SENDER = MockOtpSender()

# Amigo, read-only (tools/amigo.py), when EMOTORAD_AMIGO_PG_DSN is set; the
# config store exports it from the staging secret. None: behaviour as before.
AMIGO = amigo_tools.from_env()

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
# Only the pictures this server can actually send reach the model; with none,
# the tool is not registered at all, so a picture it does not have is never
# offered (the person's rule, 2026-09-29). /health says how many.
SENDABLE_MEDIA, UNSENDABLE_MEDIA = media_sendable(GUIDE_MEDIA, MEDIA_STORE)
if UNSENDABLE_MEDIA:
    _logger.warning(
        "guide pictures this server cannot send, left out: %d of %d (%s)",
        len(UNSENDABLE_MEDIA), len(GUIDE_MEDIA), "; ".join(sorted(UNSENDABLE_MEDIA.values()))[:500],
    )

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
            send_code=OTP_SENDER,
            # Test order numbers (fixtures.ORDER_CODES), so the fallback can
            # be tried without the OMS key.
            account_finder=fixtures.find_account_by_order_code,
            # The fixture bikes merged with the rider's app bikes, when Amigo
            # can be read; the fixtures alone otherwise.
            warranty_source=amigo_tools.merged_source(fixtures.WARRANTY_RECORDS.get, AMIGO) if AMIGO else None,
            amigo=AMIGO,
            guide_media=SENDABLE_MEDIA,
            sent_media=sent_media,
            replacement_orders=replacement_orders,
            item_codes=ItemCodes(),
            approval_mode=settings.approval_mode,
            location_sharing=True,
            idempotency=stores.idempotency,
        )
    client = OMSClient()
    return build_registry(
        verification=verification_store,
        send_code=OTP_SENDER,
        warranty_source=(amigo_tools.merged_source(live_warranty_source(client), AMIGO)
                         if AMIGO else live_warranty_source(client)),
        amigo=AMIGO,
        account_finder=live_account_finder(client),
        guide_media=SENDABLE_MEDIA,
        sent_media=sent_media,
        replacement_orders=replacement_orders,
        item_codes=ItemCodes(),
        approval_mode=settings.approval_mode,
        location_sharing=True,
        idempotency=stores.idempotency,
    )


registry = _build_registry()

# The reverse geocoder behind "Share my location". OpenStreetMap's public
# service for this LAN test server; a production provider swaps in here. The
# tests replace it with a fake. See location.py for what is and is not trusted
# from it.
geocoder = NominatimGeocoder()
pincode_directory = PincodeDirectory.load()
resolver = IdentityResolver(registry)
# Langfuse, when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are in the
# environment (the config store exports them on staging). Attached as a sink so
# it only ever sees redacted events; see tracing.py. `tracing.` rather than a
# bare import so the tests can patch the factory.
TRACING = tracing.langfuse_sink_from_env()
if TRACING is not None:
    log.sinks.append(TRACING)
runtime = Runtime(
    settings=settings,
    registry=registry,
    # offline   -> the fixed planner: no model, no key, no tokens spent.
    # anthropic -> Claude via the Anthropic API, keyed from the environment.
    #              Temporary, and the transport every tuned prompt was tuned
    #              against. See AnthropicClaude for why it exists.
    # bedrock   -> BedrockClaude through the instance role.
    # openrouter -> Jev routing, the narrow model and the full agent on
    #              OpenRouter (wiring.build_models); off on staging until signed off.
    llm=models.llm,
    narrow_llm=models.narrow_llm,
    jev=models.jev,
    conversations=stores.conversations,
    log=log,
    resolver=resolver,
    # Website chat is the one surface that arrives anonymous. Every other
    # channel resolves identity upstream — WhatsApp and Amiigo supply a verified
    # phone natively — so the agents' own TOOL_NAMES stay right for them and the
    # verification tools are added only here, where the agent has to establish
    # identity inside the conversation.
    self_service_identity=True,
    # Verify first: an anonymous visitor gives their number, types the code and
    # picks a bike before triage or any model (the person's decision,
    # 2026-09-30). Signed-in visitors and the Amiigo session skip it.
    verify_first=True,
    # The phone this conversation proves mid-turn. The model verifies a code and
    # looks the customer up in the same assistant turn, so a phone snapshotted
    # before the first tool ran is already stale by the second one.
    phone_resolver=verification_store.verified_phone,
    otp_verified_at=verification_store.verified_on,
    media_store=MEDIA_STORE,
)
adapter = WebsiteChatAdapter(resolver)

# Claimed-attachment kind, as the adapter expects it — never string tricks.
_ATTACHMENT_KIND = {"images": "image", "videos": "video", "docs": "document"}



@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # The SDK batches in a background thread; a container stopped mid-batch
    # would otherwise lose the last turns of every open conversation.
    if TRACING is not None:
        TRACING.flush()


app = FastAPI(title="Emotorad AI — battery support", lifespan=_lifespan)


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
    #       Sent inline. The evidence gate asks for a picture of the terminal
    #       before it will conclude a fault, and this is how the chat page
    #       answers it. `attachments.validate` holds the limits. With media
    #       configured the server stores the photo in S3 itself, under the
    #       caller's cluster, records it permanently, and the turn carries the
    #       `s3://` reference; without it, or when storing fails, the photo
    #       stays inline for this turn and is not kept.
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
    #
    # A pin also skips Jev and the narrow path (Runtime._node_classify), so
    # since 2026-09-29 the chat page sends one only when its URL has
    # ?agent=...; without it, openrouter mode routes as staging does. The
    # keyword triage above still answers first for text it cannot classify.
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
# The proxies whose X-Real-IP is believed (client_ip.py). Without this every
# customer behind nginx shared one limit.
TRUSTED_PROXIES = trusted_from_env()
# The DB-IP file the image was built with (origin.py), or None. Each message's
# IP becomes a place here; the IP itself goes no further than this module.
IP_LOCATOR = origin_place.ip_locator_from_env()


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


# The commit this image was built from. The deploy passes it and checks for it,
# so an older image left running by a failed build fails the deploy.
BUILD = os.environ.get("EMOTORAD_AI_BUILD") or "unknown"


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": MODE,
        "store": settings.store,
        "secrets": SECRETS_STATE,
        "media": "configured" if MEDIA_STORE is not None else "not configured",
        "guide_media": "%d of %d sendable" % (len(SENDABLE_MEDIA), len(GUIDE_MEDIA)),
        # A summariser without a provider label predates the OpenRouter one: Gemini.
        "video_summary": getattr(VIDEO_SUMMARISER, "provider", "gemini") if VIDEO_SUMMARISER is not None else "frames",
        "photo_check": PHOTO_CHECKER.provider if PHOTO_CHECKER is not None else "off",
        "tracing": "on" if TRACING is not None else "off",
        "amigo": "configured" if AMIGO is not None else "not configured",
        "build": BUILD,
        "ip_location": IP_LOCATOR.db if IP_LOCATOR is not None else "not configured",
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
    if not upload_limiter.allow(client_ip(request, TRUSTED_PROXIES)):
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
            try:
                existing = runtime.conversations.peek(body.conversation_id)
            except StoreUnavailable:
                raise HTTPException(503, "Conversation storage is unavailable; try again shortly.")
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


def _persist_media(
    conversation_id: str,
    cluster_id: str,
    key: str,
    kind: str,
    mime_type: str,
    size_bytes: int,
    source: str,
) -> None:
    """The permanent record of an object already in the bucket (spec §2): an
    inline photo just written by `_inbound_attachments`, or an upload just
    claimed. Called only once the bytes are safely stored, so a failure here
    is never raised to the customer: the object is in the bucket either way,
    and `media_record_failed` is how that gap becomes visible.
    """
    try:
        record = media_record(
            bucket=MEDIA_STORE.bucket,
            key=key,
            kind=kind,
            mime_type=mime_type,
            size_bytes=size_bytes,
            conversation_id=conversation_id,
            cluster_id=cluster_id,
            source=source,
            stored_at=utc_now_iso(),
        )
        stores.conversations.record_media(record)
    except (StoreUnavailable, ValueError) as exc:
        log.emit(
            "media_record_failed",
            conversation_id,
            key=key,
            kind=kind,
            error=type(exc).__name__,
        )


def _inbound_attachments(body: MessageIn, conversation_id: str) -> List[Dict[str, Any]]:
    """What the customer sent, in the shape the adapter takes, in the order they
    sent it.

    Two ways in, and a message may mix them. An inline `data:` URL is the
    website chat's photo-evidence path: validated here against
    `attachments.validate` (type, size, count, base64 that decodes). When a
    bucket is configured and the caller resolves to a cluster, the decoded
    photo is written to S3 here (`MEDIA_STORE.put_bytes`) and turned into an
    `s3://` attachment, recorded exactly as a claimed upload is; it is no
    longer "stored nowhere" once a bucket exists. With no bucket, or no
    cluster to key it under, it stays inline and unstored, as before. An
    `{"upload_id": ...}` is an object already PUT to S3 through
    `POST /uploads`; it is checked against this session and claimed once, and
    becomes an `s3://` key the runtime reads with the instance role. No S3
    URL ever reaches the model either way.

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

    # Validated as a batch, because that is where the count and type rules
    # live. `validated` lines up position for position with `inline`
    # (`attachments.validate` promises the same order back), which is what
    # lets the loop below pair a decoded photo with the raw item it replaces.
    inline = [item for item in items if not item.get("upload_id")]
    validated = validate_attachments(inline)

    # Raw inline items the storage loop below replaced, keyed by object
    # identity rather than mutated in place: a bucket-less deployment, a
    # missing cluster or a store failure must leave `items` exactly as the
    # customer sent it, and nothing else touches these dicts to tell apart.
    # Every photo gets a safety look (photo_check.py), inline or uploaded,
    # whether or not it can be stored. Gathered here, checked together below.
    photo_jobs: List[Tuple[Tuple[str, Any], Callable[[], bytes], str]] = [
        (("inline", id(raw_item)), (lambda data=item["data"]: base64.b64decode(data)), item["media_type"])
        for raw_item, item in zip(inline, validated) if item["media_type"].startswith("image/")
    ]
    stored: Dict[int, Dict[str, Any]] = {}
    if MEDIA_STORE is not None and inline:
        try:
            inline_cluster = _cluster_for_session(body.session_token, body.em_aid)
        except HTTPException:
            # No session, no cookie: nowhere to derive a customer key from.
            # Not the customer's fault and not worth a 400 for: the photo
            # simply is not stored this turn.
            inline_cluster = None
            log.emit("media_not_stored", conversation_id, reason="no_cluster", kind="image")
        if inline_cluster is not None:
            for raw_item, item in zip(inline, validated):
                mime = item["media_type"]
                data = base64.b64decode(item["data"])
                try:
                    key = keys.customer_key(inline_cluster, conversation_id, "images", keys.new_upload_id(), mime)
                except KeyValidationError as exc:
                    # `conversation_id` is client-supplied on the wire: the
                    # chat page always echoes back the UUID it was minted, but
                    # nothing stops a caller sending something outside the key
                    # grammar. Not worth failing the turn over, and not a store
                    # failure either: nothing was sent to S3, so it has its
                    # own reason and nobody goes checking the bucket for it.
                    log.emit(
                        "media_not_stored", conversation_id, reason="bad_key",
                        error=type(exc).__name__, kind="image",
                    )
                    continue
                try:
                    MEDIA_STORE.put_bytes(key, data, mime)
                except StorageError as exc:
                    # The key's last segment only, as the video summary line
                    # logs it: the full key names the cluster and conversation.
                    log.emit(
                        "media_not_stored", conversation_id, reason="store_failed",
                        error=type(exc).__name__, kind="image", key=key.rsplit("/", 1)[-1],
                    )
                    continue
                _persist_media(
                    conversation_id=conversation_id, cluster_id=inline_cluster, key=key,
                    kind="image", mime_type=mime, size_bytes=len(data), source="inline",
                )
                stored[id(raw_item)] = {"kind": "image", "url": "s3://" + key, "mime_type": mime}

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
                _persist_media(
                    conversation_id=conversation_id, cluster_id=caller_cluster, key=claimed.key,
                    kind=_ATTACHMENT_KIND[claimed.kind], mime_type=claimed.mime,
                    size_bytes=claimed.size, source="upload",
                )
                if claimed.kind == "videos":
                    summary = _summarise_video(claimed.key, claimed.mime)
                    if summary is not None:
                        claims[upload_id]["summary"] = summary
                        # The description is customer content — a picture of
                        # their bike, their garage, whoever is standing in it,
                        # narrated to text — so it belongs in the log only on a
                        # staging box with dev codes on. Production must never
                        # write it, not even its length.
                        if DEV_CODES:
                            log.emit(
                                "video_summary",
                                conversation_id,
                                key=claimed.key.rsplit("/", 1)[-1],
                                chars=len(summary),
                                text=summary,
                            )
                if claimed.kind == "images":
                    photo_jobs.append(
                        (("upload", upload_id), (lambda key=claimed.key: MEDIA_STORE.get_bytes(key)), claimed.mime))
        except UploadError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    # The description of a live hazard becomes the attachment's summary,
    # which the safety gate scans; a photo with none gets no summary.
    notes = _check_photos(photo_jobs, conversation_id)
    for (where, key), note in notes.items():
        if where == "upload":
            claims[key]["summary"] = note
    attachments = []
    for item in items:
        if item.get("upload_id"):
            attachments.append(claims[item["upload_id"]])
            continue
        base = stored.get(id(item), item)
        note = notes.get(("inline", id(item)))
        # A copy: the customer's own item is never changed.
        attachments.append(dict(base, summary=note) if note else base)
    return attachments


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


def _check_photos(
    jobs: Sequence[Tuple[Tuple[str, Any], Callable[[], bytes], str]], conversation_id: str
) -> Dict[Tuple[str, Any], str]:
    """{job key: description} for the photos showing a live hazard.

    All checked at the same time, under one deadline. Never stops the turn: a
    check past the deadline, a failure or a store error is logged and the
    photo goes on as before."""
    if PHOTO_CHECKER is None or not jobs:
        return {}
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="photo-check")
    futures = {pool.submit(_one_photo, load, mime): key for key, load, mime in jobs}
    done, pending = wait(futures, timeout=PHOTO_CHECK_DEADLINE_SECONDS)
    # A check still running is abandoned, not waited for.
    pool.shutdown(wait=False, cancel_futures=True)
    for _ in pending:
        log.emit("photo_check_skipped", conversation_id, error="timeout")
    notes: Dict[Tuple[str, Any], str] = {}
    for future in done:
        try:
            hazards = future.result()
        except photo_check.PhotoCheckError as exc:
            log.emit("photo_check_skipped", conversation_id, error=str(exc))
            continue
        except Exception as exc:
            log.emit("photo_check_skipped", conversation_id, error=type(exc).__name__)
            continue
        # Customer content, as a video's description is: only on a staging
        # box with dev codes on.
        if DEV_CODES:
            log.emit("photo_check", conversation_id, hazards=list(hazards))
        note = photo_check.describe(hazards)
        if note:
            notes[futures[future]] = note
    return notes


def _one_photo(load: Callable[[], bytes], mime: str) -> List[str]:
    return PHOTO_CHECKER.check(load(), mime)


@app.post("/message", response_model=MessageOut)
def post_message(body: MessageIn, request: Request) -> MessageOut:
    if not message_limiter.allow(client_ip(request, TRUSTED_PROXIES)):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Wait a moment and try again.",
        )
    # Before the attachments: `_inbound_attachments` stores and records inline
    # photos and claims uploads, and a request refused here must leave no
    # object, no record and no spent upload id behind.
    if body.agent and body.agent not in CHAT_AGENTS:
        raise HTTPException(
            status_code=400,
            detail="Unknown agent %r. Expected one of: %s"
            % (body.agent, ", ".join(CHAT_AGENTS)),
        )
    conversation_id = body.conversation_id or new_conversation_id()
    try:
        attachments = _inbound_attachments(body, conversation_id)
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
    # The cluster that started this conversation, and a pinned agent, are
    # applied by the runtime inside the turn (Runtime._node_prepare), not
    # written to the state here: with a durable store, a change made outside
    # the turn is to a copy the turn never saves.
    extra = {key: value for key, value in (("cluster_id", message_cluster), ("pinned_agent", body.agent)) if value}
    # Where the customer is, as a place: the runtime keeps the run's first one.
    place = IP_LOCATOR.place(client_ip(request, TRUSTED_PROXIES)) if IP_LOCATOR is not None else None
    if place is not None:
        extra["origin"] = place.as_dict()
    if extra:
        message = replace(message, entry_metadata=dict(message.entry_metadata, **extra))
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


class ErasureIn(BaseModel):
    # The app always sends it (empty when signed out); the website chat never
    # does, so its absence picks the right 403 wording.
    session_token: Optional[str] = None
    confirm: bool = False
    conversation_id: Optional[str] = None


def _erasure_person(request: Request, body: "ErasureIn") -> Tuple[str, str, Dict[str, str]]:
    """Who is asking, on which channel and how they were proven, or 403. Never
    from the URL.

    The Amiigo app's signed-in rider (session_token), or a website visitor
    whose chat verified a number in the last 12 hours (conversation_id): the
    HTML chat's delete button (2026-10-01)."""
    if not message_limiter.allow(client_ip(request, TRUSTED_PROXIES)):
        raise HTTPException(status_code=429, detail="Too many requests. Wait a moment and try again.")
    persona, identity = resolver.resolve_website(None, body.session_token or None)
    if persona == "customer" and identity.may_disclose and identity.phone:
        return "PHONE#" + identity.phone, "amiigo_app", erasure_rules.proof_of(None)
    phone = verification_store.verified_phone(body.conversation_id) if body.conversation_id else None
    if phone:
        return ("PHONE#" + phone, "website_chat",
                erasure_rules.proof_of(verification_store.verified_on(body.conversation_id)))
    detail = erasure_rules.ERASURE_SIGN_IN if body.session_token is not None else erasure_rules.ERASURE_VERIFY_FIRST
    raise HTTPException(status_code=403, detail=detail)


def _erasure_store_down(exc: Exception, conversation_id: Optional[str]) -> HTTPException:
    log.emit("erasure_request_failed", conversation_id or "erasure", error=type(exc).__name__)
    return HTTPException(status_code=503, detail=erasure_rules.ERASURE_FAILED)


@app.post("/erasure-requests", status_code=201)
def post_erasure_request(body: ErasureIn, request: Request, response: Response) -> Dict[str, Any]:
    """The Amiigo app's "Delete my conversation data" button, after its own
    confirmation dialog. Records a request; the nightly job deletes."""
    user_key, channel, proof = _erasure_person(request, body)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Send confirm: true once the rider has confirmed.")
    try:
        pending = stores.conversations.pending_erasure_of(user_key)
        reference = pending["_id"] if pending else stores.conversations.request_erasure(
            user_key, channel, body.conversation_id, utc_now_iso(), proof=proof)
    except Exception as exc:
        raise _erasure_store_down(exc, body.conversation_id) from None
    if pending:
        response.status_code = 200
        return {"reference": reference, "status": "pending",
                "text": erasure_rules.ERASURE_EXISTING.format(reference=reference)}
    log.emit("erasure_requested", body.conversation_id or "erasure", reference=reference)
    return {"reference": reference, "status": "pending",
            "text": erasure_rules.ERASURE_REQUESTED.format(reference=reference)}


@app.post("/erasure-requests/status")
def post_erasure_status(body: ErasureIn, request: Request) -> Dict[str, Any]:
    user_key, _, _ = _erasure_person(request, body)
    try:
        pending = stores.conversations.pending_erasure_of(user_key)
    except Exception as exc:
        raise _erasure_store_down(exc, body.conversation_id) from None
    if pending is None:
        return {"reference": None, "status": "none"}
    return {"reference": pending["_id"], "status": "pending", "requested_at": pending["requested_at"]}


@app.post("/erasure-requests/cancel")
def post_erasure_cancel(body: ErasureIn, request: Request) -> Dict[str, Any]:
    user_key, _, _ = _erasure_person(request, body)
    try:
        reference = stores.conversations.cancel_erasure(user_key, utc_now_iso())
    except Exception as exc:
        raise _erasure_store_down(exc, body.conversation_id) from None
    if reference is None:
        raise HTTPException(status_code=404, detail=erasure_rules.ERASURE_NOTHING_TO_CANCEL)
    log.emit("erasure_cancelled", body.conversation_id or "erasure", reference=reference)
    return {"reference": reference, "status": "cancelled",
            "text": erasure_rules.ERASURE_CANCELLED.format(reference=reference)}


# The playground has no auth of its own and takes an Anthropic API key as
# input, so — since the deployed instance's security group has 443 open to
# the whole internet, not just the internal team (see the deployment plan) —
# it must never be reachable without a credential check in front of it.
#
# Defined here, ahead of /dev/verification below, because that route's
# dependency binds to this name at import time, not at request time — moved up
# from beside the playground proxy it also guards, further down this file.
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


# Reading the code off the screen replaces the SMS that is not wired yet, the
# way the playground shows it in its sidebar. It is a verification bypass by
# definition: it hands the pending code for any conversation to anyone who asks.
#
# So it is off unless switched on, never on unless switched off. A deployment
# that forgets to set anything gets 404, and the only way to enable it is to
# have decided to. Delete the whole route once an SMS provider is wired; nothing
# else depends on it.
#
# The flag alone is not enough, though: staging is public on the internet and
# runs with the live OMS key behind it, so a code readable by anyone is a login
# as any phone number on the real customer base, not a toy on a laptop. It sits
# behind the same playground credential as every other internal route. Auth
# runs first — an unauthenticated caller gets 401 whether the flag is on or
# off — and only once that passes does the flag decide between the data and a
# 404, exactly as before.
DEV_CODES = os.environ.get("EMOTORAD_AI_DEV_CODES") == "1"


@app.get("/dev/verification/{conversation_id}", dependencies=[Depends(require_playground_auth)])
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

# The end-to-end test console and what it reads, behind the same two locks as
# /dev/verification: the playground login, and off unless DEV_CODES is on.
E2E_CONSOLE_FILE = WEB_DIR / "e2e-console.html"
E2E_SAMPLE_PHOTO = WEB_DIR.parent / "tests" / "data" / "live_media" / "smoke-battery.jpg"
# A console run is saved here (git-ignored, like the event log), so it outlives
# the browser tab and scripts/e2e_report.py can turn it into a report.
E2E_RESULTS_DIR = WEB_DIR.parent / "logs" / "e2e"
E2E_RESULTS_LIMIT = 2 * 1024 * 1024


@app.post("/dev/e2e/results", dependencies=[Depends(require_playground_auth)])
async def dev_e2e_save_results(request: Request) -> dict:
    if not DEV_CODES:
        raise HTTPException(status_code=404, detail="not found")
    raw = await request.body()
    if len(raw) > E2E_RESULTS_LIMIT:
        raise HTTPException(status_code=413, detail="A run is capped at %d bytes." % E2E_RESULTS_LIMIT)
    try:
        run = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Not JSON.") from None
    if not isinstance(run, dict) or not isinstance(run.get("results"), list):
        raise HTTPException(status_code=400, detail="Expected an object with a results list.")
    E2E_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = "run-%s.json" % time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = E2E_RESULTS_DIR / name
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"saved": "logs/e2e/" + name}


@app.get("/dev/e2e", response_class=HTMLResponse, dependencies=[Depends(require_playground_auth)])
def dev_e2e_console() -> HTMLResponse:
    if not DEV_CODES:
        raise HTTPException(status_code=404, detail="not found")
    return HTMLResponse(E2E_CONSOLE_FILE.read_text(encoding="utf-8"))


@app.get("/dev/e2e/sample.jpg", dependencies=[Depends(require_playground_auth)])
def dev_e2e_sample_photo() -> Response:
    if not DEV_CODES or not E2E_SAMPLE_PHOTO.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return Response(E2E_SAMPLE_PHOTO.read_bytes(), media_type="image/jpeg")


@app.get("/dev/media/{conversation_id}", dependencies=[Depends(require_playground_auth)])
def dev_media(conversation_id: str) -> dict:
    """The permanent media records of one conversation: where its photos and
    videos are stored, as the `media` collection holds them. Read-only."""
    if not DEV_CODES:
        raise HTTPException(status_code=404, detail="not found")
    try:
        return {"conversation_id": conversation_id, "media": stores.conversations.media_of(conversation_id)}
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="The conversation store is unavailable.") from None


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
