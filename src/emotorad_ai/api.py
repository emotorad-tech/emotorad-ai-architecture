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
import os
import secrets
from typing import Any, Dict, List, Optional

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .adapters import WebsiteChatAdapter
from .config import load_settings
from .config_store import SECRET_ID_ENV
from .contract import new_conversation_id
from .identity import IdentityResolver
from .llm import select_llm
from .observability import EventLog
from .runtime import Runtime
from .storage.keys import cluster_of, is_asset_key, is_customer_key
from .storage.s3 import store_from_env
from .storage.uploads import UploadError, UploadRegistry
from .tools.mocks import build_registry

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

registry = build_registry()
resolver = IdentityResolver(registry)
log = EventLog(path=settings.log_path, to_stdout=settings.log_to_stdout)
runtime = Runtime(
    settings=settings,
    registry=registry,
    # Raises LLMConfigError at import when the mode cannot be served, so the
    # deploy's health check fails instead of the first customer message.
    llm=select_llm(MODE, settings),
    log=log,
    resolver=resolver,
    media_store=MEDIA_STORE,
)
adapter = WebsiteChatAdapter(resolver)

# Claimed-attachment kind, as the adapter expects it — never string tricks.
_ATTACHMENT_KIND = {"images": "image", "videos": "video", "docs": "document"}

app = FastAPI(title="Emotorad AI — battery support")


class AttachmentIn(BaseModel):
    upload_id: str


class MessageIn(BaseModel):
    conversation_id: Optional[str] = None
    session_token: str = "sess-ananya"
    text: str
    pill: Optional[str] = None
    attachments: List[AttachmentIn] = []


class MessageOut(BaseModel):
    conversation_id: str
    text: str
    escalated: bool
    ticket_id: Optional[str]
    handled_by: Optional[str]


class AssetPath(BaseModel):
    programme: str
    category: str
    kind: str
    slug: str


class UploadIn(BaseModel):
    session_token: str = ""
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
    }


def _require_media() -> None:
    if MEDIA_STORE is None or UPLOADS is None:
        raise HTTPException(503, "Media storage is not configured on this deployment (EMOTORAD_AI_MEDIA_BUCKET).")


def _cluster_for_session(session_token: str) -> str:
    """The identity-graph cluster the session resolves to. Never the phone."""
    _, identity = resolver.resolve_website(None, session_token or None)
    if not identity.cluster_id:
        raise HTTPException(400, "session does not resolve to a customer")
    return identity.cluster_id


@app.post("/uploads")
def post_upload(body: UploadIn, request: Request) -> Dict[str, Any]:
    _require_media()
    try:
        if body.tree == "customers":
            if not body.conversation_id:
                raise HTTPException(400, "conversation_id is required for customer uploads")
            cluster_id = _cluster_for_session(body.session_token)
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
    if is_customer_key(key):
        if cluster_of(key) != _cluster_for_session(session_token):
            raise HTTPException(403, "not your attachment")
    elif not is_asset_key(key):
        raise HTTPException(404, "no such media")
    # A fresh 15-minute link every time, so a transcript rendered later still loads.
    return RedirectResponse(MEDIA_STORE.presign_get(key), status_code=302)


@app.post("/message", response_model=MessageOut)
def post_message(body: MessageIn) -> MessageOut:
    conversation_id = body.conversation_id or new_conversation_id()

    attachments: List[Dict[str, Any]] = []
    if body.attachments:
        _require_media()
        try:
            for item in body.attachments:
                claimed = UPLOADS.claim(item.upload_id)
                attachments.append(
                    {
                        "kind": _ATTACHMENT_KIND[claimed.kind],
                        "url": "s3://" + claimed.key,
                        "mime_type": claimed.mime,
                    }
                )
        except UploadError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    message = adapter.to_message(
        {
            "conversation_id": conversation_id,
            "session_token": body.session_token,
            "text": body.text,
            "pill": body.pill,
            "attachments": attachments,
        }
    )
    reply = runtime.handle(message)
    return MessageOut(
        conversation_id=conversation_id,
        text=reply.text,
        escalated=reply.escalated,
        ticket_id=reply.ticket_id,
        handled_by=reply.handled_by,
    )


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
