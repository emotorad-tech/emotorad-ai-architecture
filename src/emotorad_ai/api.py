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
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait
from typing import IO, Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .adapters import WebsiteChatAdapter
from .amiigo import history as amiigo_history
from .amiigo import routes as amiigo_routes
from .amiigo import webhooks as zoho_webhooks
from .amiigo.auth import token_check_from_env
from .amiigo.common import AmiigoContext, NoStoreMiddleware
from .client_ip import client_ip, trusted_from_env
from . import origin as origin_place
from . import evidence_check
from . import melt_ask as melt_ask_module
from . import serial_ask as serial_ask_module
from . import invoice_ocr
from . import serial_read
from . import photo_check
from . import erasure as erasure_rules
from .attachments import MAX_ATTACHMENTS, AttachmentError, validate as validate_attachments
from .config import load_settings
from .config_store import SECRET_ID_ENV
from .contract import VERIFIED, Attachment, Identity, InboundMessage, Reply, new_conversation_id
from .fulfilment import ItemCodes, ReplacementOrders
from .media import load_catalogue, model_offered
from .media import sendable as media_sendable
from .guardrails import check_safety, check_safety_in_description
from .identity import PHONE, IdentityResolver, normalise
from .address import PincodeDirectory
from .geo import PincodeCentres
from .location import CentresGeocoder, area_of, describe_location, resolve_location
from .observability import EventLog
from .ratelimit import RateLimiter
from .conversation import StoreUnavailable, customer_texts, utc_now_iso
from .media_records import media_record
from .runtime import Runtime
from .storage import keys
from .storage.keys import KeyValidationError, cluster_of, is_customer_key, is_valid_key
from .storage.s3 import StorageError, store_from_env
from . import tracing
from .storage.assets import finish_asset
from .storage.uploads import UploadError, UploadRegistry
from .tickets.clock import now_iso
from .tools import amigo as amigo_tools
from .tools import dealer_stores as dealer_stores_tools
from . import weather as weather_tools
from .tools import oms_db as oms_db_tools
from .tools import warranty_api as warranty_api_tools
from .tools import fixtures
from .tools.mocks import build_registry
from .tools.oms import OMSClient, live_account_finder, live_warranty_source
from .tools.verification import MockOtpSender, VerificationStore, apply_verified_identity, proved_owner
from .triage import classify_issue, topic_from_pill
from .video_summary import TIMEOUT_SECONDS as VIDEO_SUMMARY_SECONDS
from .video_summary import VideoSummaryError, summariser_from_env
from .wiring import build_models, build_stores
from .zoho.wiring import build_zoho, ticket_health, zoho_status

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
# Two tries of photo_check.TIMEOUT_SECONDS fit inside it. One 15-second try
# under a 15-second deadline timed out on staging and a smoking bike went
# unseen (2026-10-01).
PHOTO_CHECK_DEADLINE_SECONDS = 30.0
# Evidence is checked before a ticket (evidence_check.py, the person's brief of
# 6 October 2026): Gemini says whether a fault chat's photos and videos show
# the problem. None unless EMOTORAD_EVIDENCE_CHECK is on and the OpenRouter key
# is set. The runtime's switch (runtime.evidence_check, below) is the one that
# matters: on with no checker, nothing can pass, so no fault ticket goes
# through unchecked.
EVIDENCE_CHECKER = evidence_check.evidence_checker_from_env()

_logger = logging.getLogger(__name__)

# Verification is per conversation and has to outlive a single request, so the
# store is module-level. Without one, `build_registry` does not register
# `request_identity_verification` or `verify_identity` at all, and an anonymous
# visitor asked for their number has no way to prove it — the flow dead-ends.
# Proved numbers are also saved with the stores (MongoDB `verification_sessions`
# with EMOTORAD_STORE=mongodb, once mongo_setup.py has made its TTL index), so
# a deploy does not make every verified chat anonymous and ask for the number
# and the bike again (staging, 2026-10-06). The turn takes a saved one back
# only when the saved conversation agrees (VerificationStore.restore). Codes
# in transit stay in this process's memory: one process, not several servers.
verification_store = VerificationStore(sessions=stores.verified_sessions, log=log)

# Sends the one-time code. A stand-in until the OTP service is wired (the
# person, 2026-09-30): it sends nothing and logs the masked number; the code
# itself is read from /dev/verification on a test server.
OTP_SENDER = MockOtpSender()

# Amigo, read-only (tools/amigo.py), when EMOTORAD_AMIGO_PG_DSN is set; the
# config store exports it from the staging secret. None: behaviour as before.
AMIGO = amigo_tools.from_env()

# The Amiigo app's token check (amiigo/auth.py): Amiigo's public key from
# EMOTORAD_AMIIGO_PUBLIC_KEY, which a person sets in the config store. Without
# it the check is off, /health says so and amiigo_tokens_not_configured is
# logged once.
AMIIGO_TOKENS = token_check_from_env()

# The Zoho Desk webhook (amiigo/webhooks.py): its secret, from
# EMOTORAD_ZOHO_WEBHOOK_SECRET, which a person sets in the config store and
# in the webhook's path in Zoho Desk. Without a secret we accept, the webhook
# answers 503 and /health says so, never the value. The secret is in the path,
# so uvicorn's access log writes the path without it.
ZOHO_WEBHOOK_SECRET = zoho_webhooks.webhook_secret_from_env()
ZOHO_WEBHOOK_STATUS = zoho_webhooks.webhook_status()
if ZOHO_WEBHOOK_STATUS == zoho_webhooks.MISCONFIGURED:
    log.emit("zoho_webhook_misconfigured", "zoho", error=ZOHO_WEBHOOK_STATUS)
zoho_webhooks.hide_secret_in_access_log()

# Zoho Desk tickets (spec 2026-10-05). Decided here, once, and nowhere else:
# the CLI, the playground and the live evaluation keep the mock. Zoho is off
# without EMOTORAD_ZOHO_REFRESH_TOKEN. A failed start-up check also keeps
# the mock, says why on /health and logs zoho_misconfigured. The worker is
# built here, but only the lifespan starts it, never the import.
ZOHO = build_zoho(
    os.environ,
    ticket_store=stores.tickets,
    store_kind=settings.store,
    conversations=stores.conversations,
    media_reader=MEDIA_STORE,
    log=log,
    otp_is_mock=isinstance(OTP_SENDER, MockOtpSender),
)

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
CATALOGUE = load_catalogue()
# What a model may be offered: the code-only pictures (the melt ask's, which
# code attaches to its own reply) are left out here, and again by
# media.sendable and build_registry.
GUIDE_MEDIA = model_offered(CATALOGUE)
# Only the pictures this server can actually send reach the model; with none,
# the tool is not registered at all, so a picture it does not have is never
# offered (the person's rule, 2026-09-29). /health says how many.
SENDABLE_MEDIA, UNSENDABLE_MEDIA = media_sendable(GUIDE_MEDIA, MEDIA_STORE)
if UNSENDABLE_MEDIA:
    _logger.warning(
        "guide pictures this server cannot send, left out: %d of %d (%s)",
        len(UNSENDABLE_MEDIA), len(GUIDE_MEDIA), "; ".join(sorted(UNSENDABLE_MEDIA.values()))[:500],
    )

# The melt ask (melt_ask.py, the person's brief of 6 October 2026): one fixed
# reply asking for all three items at once, with a picture of each. Decided
# here, once: on only with EMOTORAD_MELT_ASK=on and all three pictures in the
# catalogue, code-only and resolvable. Until the battery serial sticker photo
# exists it stays off, and /health says why.
MELT_ASK, MELT_ASK_STATUS = melt_ask_module.from_env(CATALOGUE, MEDIA_STORE)
# The battery serial-photo ask (serial_ask.py, 7 October 2026): on with
# EMOTORAD_SERIAL_ASK=on and its three library pictures in the catalogue.
SERIAL_ASK, SERIAL_ASK_STATUS = serial_ask_module.from_env(CATALOGUE, MEDIA_STORE)
# The warranty step after the issue is verified (warranty_step.py, spec
# 2026-10-09), exactly "on"; deploy-staging.yml sets it.
WARRANTY_STEP = os.environ.get("EMOTORAD_WARRANTY_STEP", "").strip() == "on"
# Reads the serial off each photo the customer sends once asked (serial_read.py),
# after the reply. On with the ask, the media bucket and the OpenRouter key.
SERIAL_READER = (serial_read.serial_reader_from_env()
                 if SERIAL_ASK is not None and MEDIA_STORE is not None else None)
# Four at a time across the server; a turn waits for its own photos' reads
# at most this long (each read is one Gemini call on one photo).
SERIAL_READ_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="serial-read")
SERIAL_READ_WAIT_SECONDS = 15.0
# Where each label is, and an intact and a torn seal, shown to the reader
# before the customer's photo (serial_read.REFERENCE_KEYS).
SERIAL_REFERENCES = (evidence_check.References(CATALOGUE, MEDIA_STORE, keys=serial_read.REFERENCE_KEYS)
                     if SERIAL_READER is not None else None)
# The library's melted and normal comparisons, sent with a battery chat's
# evidence (evidence_check.REFERENCE_KEYS), read from the bucket on first use.
EVIDENCE_REFERENCES = (evidence_check.References(CATALOGUE, MEDIA_STORE)
                       if EVIDENCE_CHECKER is not None and MEDIA_STORE is not None else None)

# conversation_id -> the keys already shown in it. Module-level because "already
# sent" only means anything across turns, and a request-scoped dict would let
# the agent send the same photo every turn.
sent_media: dict = {}

# The replacement orders the bot places. Mocked: nothing reaches the OMS from
# here yet. Module-level so "already on its way" holds across conversations.
replacement_orders = ReplacementOrders()


# Bikes from OMS production (tools/oms_db.py, spec 2026-10-08): first among the
# sources when its connection string is set. Nothing connects at import.
OMS_DB = oms_db_tools.reader_from_env()
# Every pin code's centre (geo.py, spec 2026-10-09): the shared-location
# geocoder below and the dealer stores both place things by it.
PINCODE_CENTRES = PincodeCentres.load()
# The dealer stores nearest a customer (spec 2026-10-09): OMS's Dealers,
# read through the same connection setting as the bikes; offline, three
# made-up stores; otherwise none, and the tool is not offered. Their cards
# reach the reply beside the model (StoreCards).
DEALERS, DEALER_SOURCE = dealer_stores_tools.directory_from_env(
    PINCODE_CENTRES, log=lambda event, fields: log.emit(event, "dealer_stores", **fields),
    offline=settings.mode == "offline")
STORE_CARDS = dealer_stores_tools.StoreCards()
# Recent weather at the rider's area (spec 2026-10-09): Open-Meteo, on with
# its key only. The key is never logged; the client hides it.
WEATHER = weather_tools.client_from_env()
# The OMS API: invoice downloads, and the order-number fallback in
# verification, which never uses it while the dev-code page is on (a code read
# off that page plus an order number would verify anyone as the order's owner).
OMS_CLIENT = OMSClient() if os.environ.get("EMOTORAD_OMS_API_KEY") else None
ACCOUNT_FINDER = (live_account_finder(OMS_CLIENT)
                  if OMS_CLIENT is not None and os.environ.get("EMOTORAD_AI_DEV_CODES") != "1"
                  else fixtures.find_account_by_order_code)


def _warranty_source_label() -> str:
    if OMS_DB is not None:
        return "oms_db"
    if warranty_api_tools.configured():
        return "warranty_api: %s" % warranty_api_tools.WarrantyAPIClient().host
    return "oms" if os.environ.get("EMOTORAD_OMS_API_KEY") else "fixtures"


def _build_registry():
    """The warranty API, the real OMS or the fixtures, by which key is set.

    The keys are the only switch. Without it every lookup is a fixture, which is
    what the tests and a fresh clone get, and `/chat` will happily name a bike
    that belongs to nobody. With it, a phone number reaches the live purchase
    table and the bikes, frame numbers and purchase dates are the customer's own.

    Tickets do not follow this switch. With Zoho on (ZOHO, above), a
    customer's ticket is recorded for Zoho Desk and a dealer's stays on the
    mock. With Zoho off, every ticket is the mock's, and its number exists
    nowhere. That combination is worth knowing about: real bike details
    followed by a ticket number that exists nowhere is more convincing, and
    therefore worse, than fixtures all the way through.
    """
    # Registered bikes and their coverage come from the warranty API when its
    # key is set (7 October 2026: it replaces the OMS warranty lookup), else
    # from the OMS when its key is set, else from the fixtures. The order or
    # invoice code look-up stays with the OMS, which alone holds orders.
    oms = OMS_CLIENT
    if OMS_DB is not None:
        # Whether OMS's invoice can be read is the invoice service's to say
        # (defined below, read when a lookup runs); without it, none can.
        source = oms_db_tools.db_warranty_source(
            OMS_DB, invoice_state=lambda file_id: (INVOICE.invoice_state(file_id) if INVOICE is not None
                                                   else invoice_ocr.InvoiceService.UNREADABLE),
            # Bikes from OMS orders (spec 2026-10-10): a failing orders query
            # is logged by its class and the registrations stand alone.
            log=lambda event, fields: log.emit(event, "oms_orders", **fields))
    elif warranty_api_tools.configured():
        source = warranty_api_tools.api_warranty_source(warranty_api_tools.WarrantyAPIClient())
    elif oms is not None:
        source = live_warranty_source(oms)
    else:
        source = fixtures.WARRANTY_RECORDS.get
    if AMIGO:
        # The rider's app bikes merged in, when Amigo can be read.
        source = amigo_tools.merged_source(source, AMIGO)
    elif source is fixtures.WARRANTY_RECORDS.get:
        source = None  # build_registry's own default: the fixtures
    return build_registry(
        verification=verification_store,
        send_code=OTP_SENDER,
        warranty_source=source,
        amigo=AMIGO,
        # Test order numbers (fixtures.ORDER_CODES) without the OMS key, so the
        # fallback can be tried.
        account_finder=ACCOUNT_FINDER,
        guide_media=SENDABLE_MEDIA,
        sent_media=sent_media,
        replacement_orders=replacement_orders,
        item_codes=ItemCodes(),
        approval_mode=settings.approval_mode,
        location_sharing=True,
        idempotency=stores.idempotency,
        ticket_system=ZOHO.router,
        dealers=DEALERS,
        store_cards=STORE_CARDS,
        weather=WEATHER,
    )


registry = _build_registry()
# Read once, with the registry, so /health names the source actually in use.
WARRANTY_SOURCE = _warranty_source_label()

# The reverse geocoder behind a shared location: our own pin-code centres,
# so no third party sees where a rider is. The tests replace it with a fake.
# See location.py for what is and is not trusted from it.
geocoder = CentresGeocoder(PINCODE_CENTRES)
pincode_directory = PincodeDirectory.load()
resolver = IdentityResolver(registry)
# Langfuse, when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are in the
# environment (the config store exports them on staging). Attached as a sink so
# it only ever sees redacted events; see tracing.py. `tracing.` rather than a
# bare import so the tests can patch the factory.
TRACING = tracing.langfuse_sink_from_env()
if TRACING is not None:
    log.sinks.append(TRACING)
# Invoices read for a missing purchase date (invoice_ocr.py, spec 2026-10-08):
# on with EMOTORAD_INVOICE_OCR=on and the OpenRouter key, and only with the OMS
# database, whose registrations say which bikes have no date and which invoice
# OMS holds. The copy of an OMS invoice is recorded like any customer file.
_INVOICE_READER = invoice_ocr.reader_from_env() if OMS_DB is not None else None
INVOICE = (invoice_ocr.InvoiceService(
    reader=_INVOICE_READER, oms_db=OMS_DB, oms_client=OMS_CLIENT, media_store=MEDIA_STORE,
    conversations=stores.conversations, emit=log.emit, persist=lambda **record: _persist_media(**record),
) if _INVOICE_READER is not None else None)

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
    # Evidence before a ticket (evidence_check.py): on with
    # EMOTORAD_EVIDENCE_CHECK=on, and EMotorad's customer care contact for
    # when the evidence never passes. Off, every path is as before.
    evidence_check=evidence_check.switch_on(),
    customer_care_contact=evidence_check.customer_care_contact(),
    # The melt ask, or None (off).
    melt_ask=MELT_ASK,
    serial_ask=SERIAL_ASK,
    warranty_step=WARRANTY_STEP,
    invoice=INVOICE,
    store_cards=STORE_CARDS,
)
adapter = WebsiteChatAdapter(resolver)

# Claimed-attachment kind, as the adapter expects it — never string tricks.
_ATTACHMENT_KIND = {"images": "image", "videos": "video", "docs": "document"}



@asynccontextmanager
async def _lifespan(_: FastAPI):
    # The Zoho worker runs only in a process that is serving. It starts here,
    # never at import, so the tests, a reload and the playground start nothing.
    if ZOHO.worker is not None:
        ZOHO.worker.start()
    yield
    if ZOHO.worker is not None:
        ZOHO.worker.stop()
    # The SDK batches in a background thread; a container stopped mid-batch
    # would otherwise lose the last turns of every open conversation.
    if TRACING is not None:
        TRACING.flush()


app = FastAPI(title="Emotorad AI — battery support", lifespan=_lifespan)


def _cluster_for_phone(phone: str) -> str:
    """The identity-graph cluster of a phone the caller has proved, with no
    cookie: what `_cluster_for_session` gives for a session that maps to this
    phone (IdentityResolver.resolve_website), so an Amiigo rider's uploads are
    keyed under the same person either way. Never the phone itself."""
    return resolver.graph.link(None, PHONE, phone, verified=True)


# The Amiigo app's routes under /amiigo/v1/ (amiigo/routes.py), with what
# they read from this process, and Cache-Control: no-store on every answer
# under that path. Nothing outside /amiigo/v1/ is touched. The upload
# registry is the website's own: one place an upload id is minted and claimed.
app.state.amiigo = AmiigoContext(stores=stores, tokens=AMIIGO_TOKENS, log=log, media_store=MEDIA_STORE,
                                 uploads=UPLOADS, cluster_for_phone=_cluster_for_phone,
                                 receipts=stores.amiigo_receipts, zoho_webhook_secret=ZOHO_WEBHOOK_SECRET)
app.include_router(amiigo_routes.router)
# Zoho Desk's call when support closes a ticket: the ticket's chat gets a
# notice and the rider's open sockets a ticket_update (amiigo/tickets.py).
app.include_router(zoho_webhooks.router)
app.add_middleware(NoStoreMiddleware)


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
    # Dealer store cards code found (spec 2026-10-09), nearest first.
    stores: List[dict] = []


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
    report = {
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
        # Where bikes and coverage come from; the warranty API's host, never its key.
        "warranty_source": WARRANTY_SOURCE,
        "oms_orders": "on" if OMS_DB is not None and OMS_DB.orders_on else "off",
        "dealer_stores": DEALER_SOURCE,
        "weather": "open-meteo" if WEATHER is not None else "not configured",
        "warranty_step": "on" if WARRANTY_STEP else "off",
        "build": BUILD,
        "ip_location": IP_LOCATOR.db if IP_LOCATOR is not None else "not configured",
        # Zoho Desk: on, off, or why not (zoho/wiring.py).
        "zoho": zoho_status(ZOHO),
        # Where proved numbers outlive a restart: memory, mongodb, or memory
        # and why (wiring.build_stores: no TTL index, no saving).
        "verification_sessions": stores.verified_sessions_status,
        # Where the chat socket's receipts are kept: memory, mongodb, or
        # memory and why (wiring.build_stores: their indexes missing).
        "amiigo_receipts": stores.amiigo_receipts_status,
        # Whether Amiigo access tokens can be checked: on, or not configured.
        "amiigo_tokens": "on" if AMIIGO_TOKENS.enabled else "not configured",
        # Whether Zoho Desk can tell us a ticket closed: on, not configured,
        # or why the secret was refused. Never the secret.
        "zoho_webhook": ZOHO_WEBHOOK_STATUS,
        # The melt ask: on, off, or off and why (melt_ask.from_env).
        "melt_ask": MELT_ASK_STATUS,
        # The battery serial-photo ask: on, off, or off and why.
        "serial_ask": SERIAL_ASK_STATUS,
        # Who reads the serial photos: the provider, or off.
        "serial_read": SERIAL_READER.provider if SERIAL_READER is not None else "off",
        # The Jev kill switch (config.jev_switch): on, off, or off and why;
        # only openrouter mode runs Jev at all.
        "jev": settings.jev_status if settings.mode == "openrouter" else "off: mode %s" % settings.mode,
        # Who reads invoices for a missing purchase date: the provider, or off.
        "invoice_ocr": INVOICE.provider if INVOICE is not None else "off",
    }
    # Tickets waiting, stuck and held, and the worker's state. Shown while
    # Zoho is on, or while any record is outstanding.
    report.update(ticket_health(ZOHO, now_iso()))
    # Safety reports this server could not record a ticket for (spec
    # 2026-10-05, section 6), since it started. Alarmed by their event too.
    if runtime.safety_not_recorded > 0:
        report["safety_tickets_not_recorded"] = runtime.safety_not_recorded
    if runtime.evidence_check:
        # Shown only while the switch is on: who checks, or that nothing can pass.
        report["evidence_check"] = (EVIDENCE_CHECKER.provider if EVIDENCE_CHECKER is not None
                                    else "on, no checker: nothing can pass")
    return report


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


class _TurnIn(NamedTuple):
    """What the attachment and evidence steps read of a turn, whichever way
    it arrived (prepare_turn): the words as typed, the chip, the pinned
    agent, the attachments as sent, and the caller's identity-graph cluster,
    a call that raises HTTPException when the caller resolves to none."""

    text: str
    pill: Optional[str]
    agent: Optional[str]
    attachments: Optional[List[dict]]
    cluster: Callable[[], str]


def _inbound_attachments(
    turn: _TurnIn, conversation_id: str
) -> Tuple[List[Dict[str, Any]], int, Optional[Dict[str, Any]]]:
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
    `POST /uploads` (or the app's `POST /amiigo/v1/uploads`); it is checked
    against the caller's cluster (`turn.cluster`: the session's, or the
    rider's phone's) and claimed once, and becomes an `s3://` key the runtime
    reads with the instance role. No S3 URL ever reaches the model either way.

    The count limit is on the total, not on each path, so adding the presigned
    route cannot be used to send more pictures than the inline one allows.

    Also returns how many photos the safety check could not answer, and the
    evidence check's verdict on a fault chat's photos and videos (None when
    nothing was checked).
    """
    items = [item for item in (turn.attachments or []) if item]
    if not items:
        return [], 0, None
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
    # The evidence check's media, in the order the customer sent it: each
    # item's position, how to read it, its type, name and size (None for an
    # inline photo, already within its 4 MB limit). A photo is read; a clip
    # is handed over still in the bucket (evidence_check.StoredClip).
    position = {id(item): n for n, item in enumerate(items)}
    evidence_jobs: List[_EvidenceJob] = [
        (position[id(raw_item)], (lambda data=item["data"]: base64.b64decode(data)), item["media_type"], "photo",
         None)
        for raw_item, item in zip(inline, validated) if item["media_type"].startswith("image/")
    ]
    stored: Dict[int, Dict[str, Any]] = {}
    if MEDIA_STORE is not None and inline:
        try:
            inline_cluster = turn.cluster()
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
    # Described after every upload is claimed, so the evidence check can start
    # first and run beside them.
    videos: List[Tuple[str, str, str]] = []
    if uploaded:
        _require_media()
        caller_cluster = turn.cluster()
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
                    videos.append((upload_id, claimed.key, claimed.mime))
                if claimed.kind == "images":
                    photo_jobs.append(
                        (("upload", upload_id), (lambda key=claimed.key: MEDIA_STORE.get_bytes(key)), claimed.mime))
                    evidence_jobs.append((position[id(item)], (lambda key=claimed.key: MEDIA_STORE.get_bytes(key)),
                                          claimed.mime, claimed.key.rsplit("/", 1)[-1], claimed.size))
                if claimed.kind == "videos":
                    # Up to 100 MB: streamed by the checker into the file
                    # ffmpeg reads when it is shrunk, never read whole here.
                    evidence_jobs.append((position[id(item)], (lambda key=claimed.key, size=claimed.size:
                                                               _stored_clip(key, size)),
                                          claimed.mime, claimed.key.rsplit("/", 1)[-1], claimed.size))
        except UploadError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    # The evidence check starts now, and runs while the clips are described
    # and the photos safety-checked below. A turn that fails before it reads
    # the verdict tells the check to stop.
    evidence = _start_evidence_check(turn, conversation_id, sorted(evidence_jobs, key=lambda job: job[0]))
    try:
        attachments, unchecked, hazard_seen = _described(items, claims, stored, videos, photo_jobs,
                                                         conversation_id)
    except BaseException:
        if evidence is not None:
            evidence.cancel.set()
        raise
    if evidence is not None and hazard_seen:
        # A safety report is immediate and never needs evidence: the turn
        # does not wait for the check (the review of 6 October 2026), and the
        # check stops: no transcode, nothing sent (the re-review).
        log.emit("evidence_check_skipped", conversation_id, error="safety")
        evidence.cancel.set()
        evidence = None
    return attachments, unchecked, _evidence_verdict(evidence, conversation_id)


def _described(
    items: List[Dict[str, Any]], claims: Dict[str, Dict[str, Any]], stored: Dict[int, Dict[str, Any]],
    videos: List[Tuple[str, str, str]], photo_jobs: List[Tuple[Tuple[str, Any], Callable[[], bytes], str]],
    conversation_id: str,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """The turn's attachments, each clip described and each photo safety-
    checked; how many photos the safety check could not answer; and whether
    any of them shows a live hazard."""
    for upload_id, key, mime in videos:
        summary = _summarise_video(key, mime)
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
                    key=key.rsplit("/", 1)[-1],
                    chars=len(summary),
                    text=summary,
                )

    # The description of a live hazard becomes the attachment's summary,
    # which the safety gate scans; a photo with none gets no summary.
    notes, unchecked = _check_photos(photo_jobs, conversation_id)
    hazard_seen = bool(notes) or any(
        check_safety_in_description(claim.get("summary") or "").triggered for claim in claims.values())
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
    return attachments, unchecked, hazard_seen


def _evidence_subject(turn: _TurnIn, conversation_id: str) -> Optional[Tuple[str, str]]:
    """(the customer's complaint, "battery" or "motor") when this turn's
    media is evidence of a bike fault, else None.

    A fault chat by evidence_check.is_fault_chat, read with `peek`, which never
    creates a conversation: nothing is written outside the turn. A first
    message, before triage has run, counts when its pill or its words name the
    battery or the motor."""
    try:
        state = stores.conversations.peek(conversation_id)
    except StoreUnavailable as exc:
        log.emit("evidence_check_skipped", conversation_id, error="store", why=type(exc).__name__)
        state = None
    component = evidence_check.fault_component(state)
    if component is None and (state is None or state.agent is None):
        # A tester's pin (/chat?agent=battery_support) routes this turn to
        # that agent, so its media is the fault's evidence; then the pill or
        # the words, as triage reads them.
        topic = (evidence_check.FAULT_AGENTS.get(turn.agent or "") or topic_from_pill(turn.pill)
                 or classify_issue(turn.text or ""))
        component = topic if topic in evidence_check.COMPONENTS else None
    if component is None:
        return None
    said = customer_texts(state.history) if state is not None else []
    return evidence_check.complaint_from(said + [turn.text or ""]), component


# One item for the evidence check: its position in the message, how to get
# it (a photo's bytes, or a clip still in the bucket), its type, name and
# size (None for an inline photo).
_EvidenceJob = Tuple[int, Callable[[], evidence_check.Media], str, str, Optional[int]]


class _EvidenceRun(NamedTuple):
    """A check under way: its future, the moment the turn stops waiting
    (time.monotonic()), and the event the turn sets when it stops waiting
    (its deadline, a safety report, or a failure), so the check's thread
    stops too: no transcode and no request once nobody is waiting."""

    future: Any
    deadline: float
    cancel: threading.Event


def _stored_clip(key: str, size: int) -> evidence_check.StoredClip:
    """An uploaded clip as the evidence check takes it: still in the bucket."""
    return evidence_check.StoredClip(size, lambda handle: _copy_object(key, handle))


def _copy_object(key: str, handle: IO[bytes]) -> None:
    """A stored clip written into an open file a part at a time
    (S3Store.copy_to); a store that cannot stream (the local test page's)
    reads it whole, one clip at a time."""
    copy_to = getattr(MEDIA_STORE, "copy_to", None)
    if copy_to is None:
        handle.write(MEDIA_STORE.get_bytes(key))
    else:
        copy_to(key, handle)


def _start_evidence_check(
    turn: _TurnIn, conversation_id: str, jobs: Sequence[_EvidenceJob],
) -> Optional[_EvidenceRun]:
    """The check, started on its own thread, and the moment the turn stops
    waiting for it: the photo check's deadline, or the video summary's when a
    clip is among the media, so a turn waits no longer than it does today.
    The checker is given that moment, which is taken before any clip is read
    from the bucket, and the cancel. None when nothing is checked: the switch
    off, no photo or video, or a chat that is not about a bike fault."""
    if not runtime.evidence_check or not jobs:
        return None
    if check_safety(turn.text or "").triggered:
        # A safety report (battery or motor terms, the safety gate's own plain
        # scan): immediate, and exempt from evidence, so nothing is checked
        # and the turn never waits (the review of 6 October 2026).
        return None
    subject = _evidence_subject(turn, conversation_id)
    if subject is None:
        return None
    if EVIDENCE_CHECKER is None:
        return _EvidenceRun(_done({"error": "not_configured"}), 0.0, threading.Event())
    complaint, component = subject
    # A clip over the inline limit is fetched: the checker sends a smaller
    # copy of it (evidence_check.fit_inline). A photo is never shrunk, so
    # photos over the limit by themselves are refused before anything is
    # fetched, as the checker would refuse them.
    limit = getattr(EVIDENCE_CHECKER, "inline_limit", evidence_check.INLINE_LIMIT)
    photo_sizes = [size for _, _, mime, _, size in jobs if size is not None and not mime.startswith("video/")]
    if sum(photo_sizes) > limit:
        return _EvidenceRun(_done({"error": "too_large"}), 0.0, threading.Event())
    has_video = any(mime.startswith("video/") for _, _, mime, _, _ in jobs)
    wait_for = VIDEO_SUMMARY_SECONDS if has_video else PHOTO_CHECK_DEADLINE_SECONDS
    deadline, cancel = time.monotonic() + wait_for, threading.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evidence-check")
    future = pool.submit(_check_evidence, list(jobs), complaint, component, deadline, cancel)
    # A check still running at the deadline is abandoned, not waited for,
    # and told to stop (_evidence_verdict).
    pool.shutdown(wait=False)
    return _EvidenceRun(future, deadline, cancel)


def _done(result: Dict[str, Any]) -> Any:
    """A finished future holding `result`."""
    from concurrent.futures import Future

    future: Any = Future()
    future.set_result(result)
    return future


def _check_evidence(jobs: Sequence[_EvidenceJob], complaint: str, component: str, deadline: float,
                    cancel: threading.Event) -> Dict[str, Any]:
    # Photos are read here (14 MiB at most together, or refused above); a
    # clip stays in the bucket until the checker streams or reads it.
    media = [(load(), mime, name) for _, load, mime, name, _ in jobs]
    extra: Dict[str, Any] = {}
    if EVIDENCE_REFERENCES is not None:
        references, missing = EVIDENCE_REFERENCES.for_component(component)
        if missing:
            log.emit("evidence_references_missing", "evidence_check", keys=missing)
        if references:
            extra["references"] = references
    notes = evidence_check.reference_notes(CATALOGUE, component)
    if notes:
        extra["reference_notes"] = notes
    verdict = EVIDENCE_CHECKER.check(media, complaint, component, deadline_at=deadline, cancel=cancel, **extra)
    # With the fault it was checked for: the runtime keeps a pass to it.
    return dict(verdict.as_dict(), component=component)


def _evidence_verdict(started: Optional[_EvidenceRun], conversation_id: str) -> Optional[Dict[str, Any]]:
    """The verdict as the turn carries it, or {"error": code} when the check
    could not be made: never a pass. Logged by outcome and code only; what
    Gemini wrote is customer content and never reaches the log."""
    if started is None:
        return None
    done, _ = wait([started.future], timeout=max(0.0, started.deadline - time.monotonic()))
    if not done:
        # Nobody reads it now: no transcode and no request after this.
        started.cancel.set()
        log.emit("evidence_check_skipped", conversation_id, error="timeout")
        return {"error": "timeout"}
    future = started.future
    try:
        verdict = future.result()
    except evidence_check.EvidenceCheckError as exc:
        verdict = {"error": str(exc)}
    except StorageError as exc:
        verdict = {"error": "store", "why": type(exc).__name__}
    except Exception as exc:  # the class only: a provider message can echo the request
        verdict = {"error": type(exc).__name__}
    if "error" in verdict:
        log.emit("evidence_check_skipped", conversation_id, error=verdict["error"])
        return {"error": verdict["error"]}
    log.emit("evidence_check_done", conversation_id, passed=verdict.get("passed") is True)
    return verdict


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
) -> Tuple[Dict[Tuple[str, Any], str], int]:
    """({job key: description} for the photos showing a live hazard, how many
    photos got no answer).

    All checked at the same time, under one deadline, each with up to two
    tries. Never stops the turn: a photo with no answer is logged and counted,
    and the runtime tells the agent (spec 2026-10-02)."""
    if PHOTO_CHECKER is None or not jobs:
        return {}, 0
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="photo-check")
    futures = {pool.submit(_one_photo, load, mime, conversation_id): key for key, load, mime in jobs}
    done, pending = wait(futures, timeout=PHOTO_CHECK_DEADLINE_SECONDS)
    # A check still running is abandoned, not waited for.
    pool.shutdown(wait=False, cancel_futures=True)
    unchecked = len(pending)
    for _ in pending:
        log.emit("photo_check_skipped", conversation_id, error="timeout")
    notes: Dict[Tuple[str, Any], str] = {}
    for future in done:
        try:
            hazards = future.result()
        except photo_check.PhotoCheckError as exc:
            log.emit("photo_check_skipped", conversation_id, error=str(exc))
            unchecked += 1
            continue
        except Exception as exc:
            log.emit("photo_check_skipped", conversation_id, error=type(exc).__name__)
            unchecked += 1
            continue
        # Customer content, as a video's description is: only on a staging
        # box with dev codes on.
        if DEV_CODES:
            log.emit("photo_check", conversation_id, hazards=list(hazards))
        note = photo_check.describe(hazards)
        if note:
            notes[futures[future]] = note
    return notes, unchecked


def _one_photo(load: Callable[[], bytes], mime: str, conversation_id: str) -> List[str]:
    """The photo's live hazards, with a second try when the first fails (the
    first try timed out on staging, 2026-10-01). A photo too large to send is
    not tried again: it would fail the same way."""
    data = load()
    try:
        return PHOTO_CHECKER.check(data, mime)
    except photo_check.PhotoCheckError as exc:
        if str(exc) == "too_large":
            raise
        log.emit("photo_check_retry", conversation_id, error=str(exc))
    except Exception as exc:
        log.emit("photo_check_retry", conversation_id, error=type(exc).__name__)
    return PHOTO_CHECKER.check(data, mime)


# The ways a turn arrives through prepare_turn: the website chat's
# POST /message, and the Amiigo app's chat socket, whose turns are always a
# rider's, proved by the token.
WEBSITE_CHANNEL = "website_chat"
APP_CHANNEL = amiigo_history.APP_CHANNEL
TURN_CHANNELS = (WEBSITE_CHANNEL, APP_CHANNEL)


def prepare_turn(
    *,
    conversation_id: str,
    text: str,
    attachments: Optional[List[dict]] = None,
    pill: Optional[str] = None,
    screen: Optional[str] = None,
    location: Optional[LocationIn] = None,
    channel: str = WEBSITE_CHANNEL,
    session_token: Optional[str] = None,
    em_aid: Optional[str] = None,
    rider_phone: Optional[str] = None,
    agent: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> InboundMessage:
    """The turn `runtime.handle` takes, from what a message carried, for
    every way one arrives: POST /message and the Amiigo app's chat socket.

    In this order, as POST /message always did it: the attachments (inline
    photos stored and recorded, uploads claimed and recorded, each photo
    safety-checked, each clip described, the evidence check started at
    ingest beside them), then a shared location turned into the customer's
    words, then the identity and the cluster that started the conversation,
    then the pinned agent, the screen, the place and the checks' results in
    `entry_metadata` for the runtime to apply inside the turn.

    Who is writing: with `rider_phone` (the phone an Amiigo token proved),
    a verified customer with that phone and its cluster, on the identity
    itself, as a website session's phone is (IdentityResolver.
    resolve_website); no session or cookie is looked up, and nothing is
    added to the session table. Otherwise the website's own path: the
    session or the cookie, then a number this chat proved
    (apply_verified_identity).

    The caller refuses what it must before this (the rate limit, an unknown
    agent, a conversation that is not the caller's): an attachment is stored
    or an upload claimed here, and a refused message must leave neither.
    Raises AttachmentError for attachments outside `attachments.validate`;
    HTTPException for an upload that cannot be claimed (404 unknown or
    expired, 403 not the caller's, 409 not finished or not as presigned, 503
    no media storage) or uploads from a caller with no cluster (400); and
    ValueError for a channel it does not serve, an app turn with no rider,
    or a rider's phone on any channel but the app's.
    `client_ip` is used for the place only, and goes no further.
    """
    if channel not in TURN_CHANNELS:
        raise ValueError("prepare_turn serves %s, not %r" % (" and ".join(TURN_CHANNELS), channel))
    if channel == APP_CHANNEL and not rider_phone:
        raise ValueError("an Amiigo app turn is a rider's: rider_phone, from the token, is required")
    if rider_phone and channel != APP_CHANNEL:
        # A token-proved phone belongs to the app's turns only (the plan's
        # Ruling 11): on any other channel it would verify a visitor who
        # proved nothing.
        raise ValueError("rider_phone is the Amiigo app's, not %r's" % (channel,))
    phone = normalise(PHONE, rider_phone) if rider_phone else None
    if phone is not None:
        def cluster() -> str:
            return _cluster_for_phone(phone)
    else:
        def cluster() -> str:
            return _cluster_for_session(session_token, em_aid)
    turn = _TurnIn(text=text, pill=pill, agent=agent, attachments=attachments, cluster=cluster)
    found, unchecked, evidence_verdict = _inbound_attachments(turn, conversation_id)
    said = text
    area = None
    if location is not None:
        # Resolved here, before the message exists, so the coordinates never
        # become part of anything that is logged or handed to the model. Only
        # a message that is nothing but the location becomes the customer's
        # words; with words, a chip or a photo, the location is background
        # (the app team's addendum, 7 October 2026).
        located = resolve_location(location.latitude, location.longitude, geocoder, pincode_directory)
        area = area_of(located, "location", utc_now_iso())
        if not (text or "").strip() and not pill and not found:
            said = describe_location(located)

    if phone is not None:
        message_cluster: Optional[str] = cluster()
        message = _rider_message(conversation_id, channel, phone, message_cluster, said, pill, found)
    else:
        # Record which cluster started this conversation, the first time we
        # see it, so a later presign under the same conversation id can be
        # checked against who actually owns it (see post_upload).
        # Best-effort: a session that does not resolve to a customer is a
        # legitimate anonymous chat and must not 400 here just because we
        # tried to attribute a cluster to it.
        try:
            message_cluster = _cluster_for_session(session_token)
        except HTTPException:
            message_cluster = None
        message = adapter.to_message(
            {
                "conversation_id": conversation_id,
                "session_token": session_token,
                "em_aid": em_aid,
                "text": said,
                "pill": pill,
                "attachments": found,
            }
        )
    # The proof this conversation has already given. Without this the customer
    # types a correct code and the very next turn still resolves anonymous, so
    # the warranty lookup is refused for want of a phone exactly as it was
    # before they bothered. A rider's turn already carries its phone, which
    # this never overwrites.
    message = apply_verified_identity(message, verification_store)
    # The cluster that started this conversation, and a pinned agent, are
    # applied by the runtime inside the turn (Runtime._node_prepare), not
    # written to the state here: with a durable store, a change made outside
    # the turn is to a copy the turn never saves.
    extra = {key: value for key, value in (("cluster_id", message_cluster), ("pinned_agent", agent),
                                           ("screen", screen)) if value}
    # Where the customer is, as a place: the runtime keeps the run's first one.
    place = IP_LOCATOR.place(client_ip) if IP_LOCATOR is not None else None
    if place is not None:
        extra["origin"] = place.as_dict()
    if area is not None:
        # The runtime keeps it on the conversation (Runtime._handle).
        extra["area"] = area
    if unchecked:
        # The runtime tells the agent (Runtime._run); never a summary.
        extra["photos_unchecked"] = unchecked
    if evidence_verdict is not None:
        # The runtime keeps it on the conversation, inside the turn.
        extra["evidence_verdict"] = evidence_verdict
    if extra:
        message = replace(message, entry_metadata=dict(message.entry_metadata, **extra))
    return message


def _handle_app_turn(message: InboundMessage) -> Reply:
    """The chat socket's turn: the same runtime, read when the turn runs."""
    _start_serial_reads(message)
    _start_invoice_reads(message)
    return runtime.handle(message)


def _start_invoice_reads(message: InboundMessage) -> None:
    """Reads the chosen bike's invoice before the turn when OMS has no
    purchase date for it (invoice_ocr.py): OMS's copy when it holds one,
    otherwise each photo or PDF the customer sent in this message. Waits up to
    the service's limit; never raises."""
    if INVOICE is None or not message.identity.phone:
        return
    cid = message.conversation_id
    try:
        state = stores.conversations.peek(cid)
        frame = state.selected_frame if state is not None else None
        if not frame:
            return
        # Only once the lookup has shown this bike undated, or in the
        # late-registration chat, whose whole subject it is: never for a
        # chat about something else (final review, finding 5).
        looked_up = any(
            bike.get("coverage_status") == "purchase_date_missing" and frame in (bike.get("frame_number"),
                                                                                  bike.get("bike_ref"))
            for bike in ((state.coverage_result or {}).get("data") or {}).get("bikes") or [])
        if not looked_up and state.agent != "late_warranty_registration":
            return
        row = INVOICE.bike(message.identity.phone, frame)
        if row is None or row.get("purchase_date"):
            return
        user_key = state.user_key
        cluster = message.entry_metadata.get("cluster_id") or state.cluster_id
        file_state = INVOICE.invoice_state(row.get("invoice_image"))
        if file_state == invoice_ocr.InvoiceService.WITH_SUPPORT:
            return
        if file_state == invoice_ocr.InvoiceService.READABLE:
            jobs = [lambda: INVOICE.read_from_oms(cid, user_key, cluster, message.identity.phone, frame)]
        else:
            jobs = [
                (lambda key=item.url[len("s3://"):], mime=item.mime_type:
                 INVOICE.read_upload(cid, user_key, frame, key, mime))
                for item in message.attachments
                if (item.url or "").startswith("s3://")
                and ((item.mime_type or "").startswith("image/") or item.mime_type == "application/pdf")
            ]
        INVOICE.start(jobs)
    except Exception as exc:
        log.emit("invoice_read_failed", cid, error="state:" + type(exc).__name__, source="api")


def _start_serial_reads(message: InboundMessage) -> None:
    """Reads the turn's stored photos with the serial reader before the turn
    runs, when the bike the chat is about was asked for its serial photos
    (read from the state before the turn): each photo at the same time, and
    the turn waits for them up to SERIAL_READ_WAIT_SECONDS, so the ticket
    tool sees a motor photo that has just arrived and the reply can confirm
    what was read (serial_confirm.py). A read still running then finishes on
    its own and is confirmed on a later turn. Never raises."""
    if SERIAL_READER is None or MEDIA_STORE is None or not message.attachments:
        return
    try:
        state = stores.conversations.peek(message.conversation_id)
        photos = serial_read.photos_to_read(message.attachments, state)
    except Exception as exc:
        log.emit("serial_read_failed", message.conversation_id, error="state:" + type(exc).__name__)
        return
    if not photos:
        return
    futures = [SERIAL_READ_POOL.submit(
        serial_read.read_photos, SERIAL_READER, [photo], MEDIA_STORE.get_bytes, stores.conversations,
        conversation_id=message.conversation_id, user_key=state.user_key, frame_number=state.selected_frame,
        emit=log.emit, references=SERIAL_REFERENCES,
    ) for photo in photos]
    done, pending = wait(futures, timeout=SERIAL_READ_WAIT_SECONDS)
    if pending:
        log.emit("serial_read_late", message.conversation_id, photos=len(pending))


# The chat socket's turn (amiigo/socket.py): prepare_turn with the app's
# channel and the token's phone, then the runtime, as POST /message does.
app.state.amiigo.prepare_turn = prepare_turn
app.state.amiigo.handle_turn = _handle_app_turn


def _rider_message(
    conversation_id: str, channel: str, phone: str, cluster_id: str, text: str, pill: Optional[str],
    attachments: List[Dict[str, Any]],
) -> InboundMessage:
    """A rider's turn, shaped as the website adapter shapes a turn
    (WebsiteChatAdapter.to_message), with the identity the token proved: a
    verified customer, their phone and its cluster, and no session, cookie or
    token on it (the plan's Ruling 2)."""
    return InboundMessage(
        conversation_id=conversation_id,
        persona="customer",
        identity=Identity(cluster_id=cluster_id, strength=VERIFIED, phone=phone),
        channel=channel,
        message_text=(text or "").strip(),
        entry_metadata={"pill_clicked": pill} if pill else {},
        attachments=[
            Attachment(kind=a.get("kind", "image"), url=a["url"], mime_type=a.get("mime_type"),
                       summary=a.get("summary"))
            for a in attachments
            if a.get("url")
        ],
    )


def _message_out(conversation_id: str, reply: Reply) -> MessageOut:
    """The reply as POST /message answers it."""
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
        stores=[dict(s) for s in reply.stores],
    )


def _is_app_chat(conversation_id: str) -> bool:
    """Whether this conversation is an Amiigo app chat, which the website
    never writes in (the plan's Rulings 8 and 9; amiigo_history.is_app_chat
    reads the permanent records). A store that cannot answer is logged and
    counts as no: the turn then meets the same store and answers as it does
    any outage (a handover, or a safety report's steps), and a safety report
    never gets a 503 instead."""
    try:
        return amiigo_history.is_app_chat(stores.conversations, conversation_id)
    except StoreUnavailable as exc:
        log.emit("app_chat_check_failed", conversation_id, error=type(exc).__name__)
        return False


@app.post("/message", response_model=MessageOut)
def post_message(body: MessageIn, request: Request) -> MessageOut:
    caller_ip = client_ip(request, TRUSTED_PROXIES)
    if not message_limiter.allow(caller_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Wait a moment and try again.",
        )
    # Before the attachments: `prepare_turn` stores and records inline photos
    # and claims uploads, and a request refused here must leave no object, no
    # record and no spent upload id behind.
    if body.agent and body.agent not in CHAT_AGENTS:
        raise HTTPException(
            status_code=400,
            detail="Unknown agent %r. Expected one of: %s"
            % (body.agent, ", ".join(CHAT_AGENTS)),
        )
    if body.conversation_id and _is_app_chat(body.conversation_id):
        # A rider's app chat is written in only through the app, with the
        # rider's token: anyone else holding its id could otherwise add to
        # the rider's history from here.
        raise HTTPException(status_code=403, detail="not your conversation")
    conversation_id = body.conversation_id or new_conversation_id()
    try:
        message = prepare_turn(
            conversation_id=conversation_id,
            text=body.text,
            attachments=body.attachments,
            pill=body.pill,
            location=body.location,
            session_token=body.session_token,
            em_aid=body.em_aid,
            agent=body.agent,
            client_ip=caller_ip,
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _start_serial_reads(message)
    _start_invoice_reads(message)
    return _message_out(conversation_id, runtime.handle(message))


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
        return erasure_rules.app_requester(identity.phone)
    phone = _proved_phone(body.conversation_id)
    if phone:
        return ("PHONE#" + phone, "website_chat",
                erasure_rules.proof_of(verification_store.verified_on(body.conversation_id)))
    detail = erasure_rules.ERASURE_SIGN_IN if body.session_token is not None else erasure_rules.ERASURE_VERIFY_FIRST
    raise HTTPException(status_code=403, detail=detail)


def _proved_phone(conversation_id: Optional[str]) -> Optional[str]:
    """The number this chat proved. After a restart, its saved session is
    taken back first, as a turn would, and only if the saved conversation
    says the same person finished verifying (VerificationStore.restore).
    A store that cannot be read proves nothing."""
    if not conversation_id:
        return None
    phone = verification_store.verified_phone(conversation_id)
    if phone is not None or verification_store.sessions is None:
        return phone
    try:
        state = stores.conversations.peek(conversation_id)
    except StoreUnavailable:
        return None
    return verification_store.restore(conversation_id, proved_owner(state))


# The bodies of these three are erasure.py's (record_request, request_status,
# cancel_request), shared with the app's /amiigo/v1/erasure-requests*: this
# path finds the person by session or verified chat, and words its own errors.
@app.post("/erasure-requests", status_code=201)
def post_erasure_request(body: ErasureIn, request: Request, response: Response) -> Dict[str, Any]:
    """The Amiigo app's "Delete my conversation data" button, after its own
    confirmation dialog. Records a request; a person deletes it with erasure_admin."""
    user_key, channel, proof = _erasure_person(request, body)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Send confirm: true once the rider has confirmed.")
    try:
        recorded, answer = erasure_rules.record_request(stores.conversations, log, user_key, channel, proof,
                                                        body.conversation_id, utc_now_iso())
    except erasure_rules.RequestsUnavailable:
        raise HTTPException(status_code=503, detail=erasure_rules.ERASURE_FAILED) from None
    if not recorded:
        response.status_code = 200
    return answer


@app.post("/erasure-requests/status")
def post_erasure_status(body: ErasureIn, request: Request) -> Dict[str, Any]:
    user_key, _, _ = _erasure_person(request, body)
    try:
        return erasure_rules.request_status(stores.conversations, log, user_key, body.conversation_id)
    except erasure_rules.RequestsUnavailable:
        raise HTTPException(status_code=503, detail=erasure_rules.ERASURE_FAILED) from None


@app.post("/erasure-requests/cancel")
def post_erasure_cancel(body: ErasureIn, request: Request) -> Dict[str, Any]:
    user_key, _, _ = _erasure_person(request, body)
    try:
        answer = erasure_rules.cancel_request(stores.conversations, log, user_key, body.conversation_id,
                                              utc_now_iso())
    except erasure_rules.RequestsUnavailable:
        raise HTTPException(status_code=503, detail=erasure_rules.ERASURE_FAILED) from None
    if answer is None:
        raise HTTPException(status_code=404, detail=erasure_rules.ERASURE_NOTHING_TO_CANCEL)
    return answer


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


# The playground's admin upload, step three. The browser presigned an asset
# (`POST /uploads`, tree "assets"), PUT the bytes straight to S3, and now asks
# for the derivatives (a 900px WebP for a photo, a poster frame for a clip) and
# the id to paste into a knowledge record. Behind the playground login, like
# the presign: a customer upload is never finished this way, and the id stays
# claimable by the message it belongs to.
@app.post("/uploads/{upload_id}/finish", dependencies=[Depends(require_playground_auth)])
def finish_upload(upload_id: str) -> Dict[str, Any]:
    _require_media()
    pending = UPLOADS.peek(upload_id)
    if pending is None:
        raise HTTPException(404, "unknown or expired upload id")
    if pending.tree != "assets":
        raise HTTPException(403, "not an asset upload")
    try:
        claimed = UPLOADS.claim(upload_id)
    except UploadError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    try:
        return finish_asset(MEDIA_STORE, claimed.key)
    except Exception as exc:
        # The original is in the bucket and the id is spent, so the admin
        # has to upload again under the same slug; say so rather than hide it.
        _logger.exception("asset derivatives failed for %s", claimed.key)
        raise HTTPException(500, "stored %s but could not make its derivatives (%s); upload it again" % (claimed.key, type(exc).__name__)) from None


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
    # aiter_bytes, not aiter_raw: Content-Encoding is dropped below, so the
    # body must go out decoded. Streamlit gzips the files of a custom component
    # (the admin uploader's index.html); forwarded raw, the browser rendered
    # the gzip stream as text and the component never came up.
    return StreamingResponse(
        upstream_response.aiter_bytes(),
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
