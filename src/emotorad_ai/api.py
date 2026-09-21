"""HTTP entrypoint — the same skeleton `cli.py` drives, served over HTTP so it
can run as an ECS/EC2 service instead of only from a terminal.

Defaults to the offline planner (no Bedrock, no AWS credentials, no tokens
spent) via `EMOTORAD_AI_MODE` — matching `cli.py --offline` — because real
Bedrock access is not wired up yet (see docs/Emotorad_AWS_Deployment_Plan.md).
Flip to real Claude once that's ready:

    EMOTORAD_AI_MODE=bedrock uvicorn emotorad_ai.api:app

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
import os
import secrets
from pathlib import Path
from typing import List, Optional

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .adapters import WebsiteChatAdapter
from .attachments import AttachmentError, validate as validate_attachments
from .config import Settings, load_settings
from .contract import new_conversation_id
from .fulfilment import ItemCodes, ReplacementOrders
from .media import load_catalogue
from .identity import IdentityResolver
from .address import PincodeDirectory
from .llm import AnthropicClaude, OfflinePlanner
from .location import NominatimGeocoder, describe_location, resolve_location
from .observability import EventLog
from .ratelimit import RateLimiter
from .runtime import Runtime
from .tools.mocks import build_registry
from .tools.oms import OMSClient, live_account_finder, live_warranty_source
from .tools.verification import VerificationStore, apply_verified_identity

MODE = os.environ.get("EMOTORAD_AI_MODE", "offline")

def _build_llm(mode: str, settings: Settings) -> Optional[object]:
    if mode == "offline":
        return OfflinePlanner()
    if mode == "anthropic":
        return AnthropicClaude(settings)
    return None  # Runtime constructs BedrockClaude


settings = load_settings()
# Verification is per conversation and has to outlive a single request, so the
# store is module-level. Without one, `build_registry` does not register
# `request_identity_verification` or `verify_identity` at all, and an anonymous
# visitor asked for their number has no way to prove it — the flow dead-ends.
verification_store = VerificationStore()

# The guide photos and clips the agent may show. Loaded once: it is authored
# content in the repo, not per-request state.
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
    # bedrock   -> None, so Runtime builds BedrockClaude: the architecture's
    #              answer, waiting on AWS access.
    llm=_build_llm(MODE, settings),
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
)
adapter = WebsiteChatAdapter(resolver)

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
    # Photos the customer sent, inline and never stored. The evidence gate asks
    # for a picture of the terminal before it will conclude a fault, and until
    # this field existed there was no way to answer it. See attachments.py for
    # the limits, which are the whole security story for this path.
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


class AttachmentOut(BaseModel):
    kind: str
    url: str
    mime_type: Optional[str] = None


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": MODE}


@app.post("/message", response_model=MessageOut)
def post_message(body: MessageIn, request: Request) -> MessageOut:
    if not message_limiter.allow(request.client.host if request.client else None):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Wait a moment and try again.",
        )
    conversation_id = body.conversation_id or new_conversation_id()
    try:
        validate_attachments(body.attachments)
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
    message = adapter.to_message(
        {
            "conversation_id": conversation_id,
            "session_token": body.session_token,
            "em_aid": body.em_aid,
            "text": text,
            "pill": body.pill,
            "attachments": body.attachments or [],
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
            AttachmentOut(kind=a.kind, url=a.url, mime_type=a.mime_type)
            for a in reply.attachments
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


@app.api_route("/playground", methods=["GET", "POST", "HEAD"], dependencies=[Depends(require_playground_auth)])
@app.api_route(
    "/playground/{rest:path}", methods=["GET", "POST", "HEAD"], dependencies=[Depends(require_playground_auth)]
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
