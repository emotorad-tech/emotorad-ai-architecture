# Real Zoho Desk tickets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mock ticket system with Zoho Desk for the customer persona: a durable ticket record written in the turn, a background worker that sends it to Zoho after the reply, and real tickets for the five handovers in the spec.

**Architecture:** The ticket tools and the runtime gates write a record to a `tickets` collection through a seam (`DeskTicketSystem`, behind a `TicketRouter` that keeps dealers on the mock). A daemon worker in the API process takes due records under a lease and sends them to Zoho Desk (India data centre) with an OAuth refresh token, in resumable steps: contact, ticket, transcript, notes, attachments. With Zoho unset or misconfigured, the mock is used exactly as today.

**Tech Stack:** Python 3.12 (`.venv/bin/python`), stdlib `urllib` for HTTP (the repo's norm: `tools/oms.py`, `openrouter.py`), `unittest`, `mongomock` for the store tests, FastAPI lifespan for the worker thread.

**Spec:** `docs/superpowers/specs/2026-10-05-zoho-desk-tickets-design.md` (read it before any task; section numbers below are the spec's).

## Global Constraints

- Branch `feat/zoho-desk-tickets`; commits only, never push. PR target `feat/integrate-jev-mongo`.
- Test command (whole suite, offline): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .` Single module: replace `discover -s tests -t .` with `tests.test_<name>`. Baseline before this plan: 2,165 tests, one known environmental failure (`tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`).
- No network in tests. Zoho is faked by `tests/fake_zoho.py` (an `opener` double, the pattern of `tests/test_oms.py:32-40`). Never call Zoho, MongoDB Atlas, Postgres or AWS from a test or a Claude session.
- Never read, print or log a secret value. Zoho failures are logged by error class and Zoho's error code as `error=`, never `code=` (`observability.py` exempts only an error envelope's `code`). No request or response body is ever logged. Exception messages never carry a token, secret, phone or body.
- Never write a regex through a shell heredoc; edit files with the editor and add a test that the pattern matches a known phrase. Never rely on `\b` or `\w` alone on text that may be Devanagari; use `[\wऀ-ॿ]` and run `ascii_digits` before reading digits.
- Indian mobiles only for call-back numbers (`find_phone` after `ascii_digits`).
- References: Desk tickets `EM-` + seven digits from 1000001 (`EM-1000001`); mock tickets keep `EM-%05d`. `is_desk_reference(ticket_id)` decides which system issued an id.
- Times in the ticket store are ISO-8601 UTC strings with microseconds (`tickets.clock.now_iso()`), compared as strings.
- Zoho constants: accounts `https://accounts.zoho.in`, Desk `https://desk.zoho.in`; per-call timeout 8 s, uploads 60 s; transcript comment chunks ≤ 30,000 characters; attachment limit 20 MB unless `EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB` says otherwise; lease 300 s; first attempt 120 s after `create` unless the turn's end wakes it; retry waits 30 s, 60 s, 120 s, 300 s, 600 s, 1800 s, then 3600 s; urgent records late at 600 s, others stuck at 86,400 s.
- The probe-only values are settings (spec "Later the same day"): `EMOTORAD_ZOHO_CF_CHAT_REFERENCE`, `EMOTORAD_ZOHO_CF_SOURCE` (required); `EMOTORAD_ZOHO_PRIORITY_HIGH` (default `High`), `EMOTORAD_ZOHO_PRIORITY_MEDIUM` (default `Medium`), `EMOTORAD_ZOHO_CHANNEL` (default `Chat`), `EMOTORAD_ZOHO_CREDITS_FLOOR` (default `1000`), `EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB` (default `20`).
- Erasure (spec section 11) is out of scope for this plan (the person's decision, 5 October 2026).
- British English, plain short sentences, no em dashes, in code comments, texts and docs. Match the surrounding code's comment density and idiom.
- Tier-1 files touched (`tools/registry.py`, `observability.py`, `guardrails.py`, `graph.py`, the safety branch in `runtime.py`): every change ships with its test, and safety paths assert the model was never called.

## Review Focus

- A safety report from an anonymous web visitor: the reply must never promise a call unless a ticket was recorded, and a typed number must become a ticket, not an OTP send. (Owned by Task 13 and Task 14.)
- A second person on the same browser after `restart_for`: none of the first person's turns, photos or ticket reference may reach the second person's ticket, and the reverse. (Owned by Task 1 and Task 5.)
- A Zoho timeout after the ticket was created: exactly one Zoho ticket, found by the contact's ticket list. (Owned by Task 9.)
- Settings partly present, the index missing, or an `eu-` region: the mock is used and nothing is written to `tickets`. (Owned by Task 6 and Task 10.)
- A dealer's safety report or "talk to my account manager": never a Desk record. (Owned by Task 5 and Task 14.)

---

## File structure

New:
- `src/emotorad_ai/digits.py`: `ascii_digits` (moved from `verify_first.py`, which re-exports it), no dependencies.
- `src/emotorad_ai/tickets/__init__.py`: package marker.
- `src/emotorad_ai/tickets/clock.py`: `now_iso()`, `iso(dt)`, `parse(text)`, `plus(text, seconds)`.
- `src/emotorad_ai/tickets/kinds.py`: kinds, urgency, subject labels, reference patterns.
- `src/emotorad_ai/tickets/record.py`: `new_record(...)`, the document shape (spec section 3).
- `src/emotorad_ai/tickets/store.py`: `InMemoryTicketStore`.
- `src/emotorad_ai/tickets/seam.py`: `DeskTicketSystem`, `TicketRouter`.
- `src/emotorad_ai/zoho/__init__.py`, `zoho/settings.py`, `zoho/errors.py`, `zoho/http.py`, `zoho/auth.py`, `zoho/desk.py`, `zoho/payload.py`, `zoho/worker.py`, `zoho/wiring.py`.
- `scripts/zoho/_common.py`, `consent_url.py`, `exchange_code.py`, `probe.py`, `test_ticket.py`, `revoke.py`, `tickets_report.py`.
- `docs/api-shapes/zoho-ticket.json`, `zoho-contact-search.json`, `zoho-contact-tickets.json`, `zoho-comment.json`, `zoho-attachment.json`, `zoho-token.json`, `zoho-errors.json` (from Zoho's published OAS, each with a `"_source"` note saying the probe replaces it).
- `infra/zoho-alarms.yaml`.
- Tests: `tests/fake_zoho.py`, `tests/ticket_store_contract.py`, `tests/test_ticket_kinds.py`, `tests/test_ticket_store.py`, `tests/test_ticket_seam.py`, `tests/test_ticket_tools_desk.py`, `tests/test_runtime_tickets.py`, `tests/test_zoho_settings.py`, `tests/test_zoho_http.py`, `tests/test_zoho_auth.py`, `tests/test_zoho_desk.py`, `tests/test_zoho_payload.py`, `tests/test_zoho_worker.py`, `tests/test_zoho_wiring.py`, `tests/test_zoho_scripts.py`, `tests/test_handover_tickets.py`, `tests/test_callback_gate.py`, `tests/test_safety_without_phone.py`, `tests/test_lockout_tickets.py`.

Modified: `tools/registry.py`, `tools/mocks.py`, `agents/base.py`, `runtime.py`, `conversation.py`, `graph.py`, `guardrails.py`, `verify_first.py`, `observability.py`, `enrichment.py`, `stores/mongo.py`, `wiring.py`, `api.py`, `scripts/chat_local.py`, `scripts/mongo_setup.py`, `tests/test_api_health.py`, `docs/contracts/amiigo-support-chat.md`, `docs/runbooks/config-store.md`, `docs/Emotorad_Edge_Case_Register.md`.

## Shared interfaces (every task uses exactly these names)

```python
# tickets/clock.py
def now_iso() -> str: ...                      # datetime.now(timezone.utc).isoformat(timespec="microseconds")
def plus(at: str, seconds: float) -> str: ...  # same format

# tickets/kinds.py
KINDS = ("support", "safety", "handover", "lockout", "intake", "warranty_proof")
def is_urgent(kind: str, category: Optional[str]) -> bool: ...   # kind == "safety" or category == "battery_safety"
def is_desk_reference(ticket_id: Optional[str]) -> bool: ...     # re.fullmatch(r"EM-\d{7,}", ...)
def subject_label(kind: str, category: Optional[str]) -> str: ...
FIRST_DESK_NUMBER = 1000001

# tickets/record.py
def new_record(*, reference: str, chat_reference: str, source_key: str, mode: str, kind: str,
               conversation_id: str, started_at: Optional[str], cluster_id: Optional[str],
               channel: Optional[str], phone: Optional[str], identity: str, category: Optional[str],
               ai_severity: Optional[str], summary: str, claims: Dict[str, Any],
               bike: Optional[Dict[str, Any]], coverage: Optional[str], customer_name: Optional[str],
               created_at: str) -> Dict[str, Any]: ...
# Document keys (spec section 3): "_id" (= reference), chat_reference, source_key, mode, kind, urgent,
# conversation_id, cluster_id, started_at, ended_at (None), channel, created_at, phone, identity,
# category, ai_severity, summary, claims, bike, coverage, customer_name, notes ([] of {"text","at"}),
# zoho ({}: contact_id, ticket_id, ticket_number, web_url, comment_ids [], attachment_ids []),
# posted_turns ([] of int n), posted_media ([] of media key), posted_notes ([] of int index),
# state ("waiting"), due_since (= created_at), wake (0), attempts (0),
# next_attempt_at (= plus(created_at, 120)), lease_until (None), lease_token (None),
# intent (None), last_error (None).

# The ticket store protocol: InMemoryTicketStore (tickets/store.py) and MongoTicketStore
# (stores/mongo.py) implement exactly this. Both raise conversation.StoreUnavailable when unreachable.
class TicketStore(Protocol):
    def next_reference(self) -> str: ...                                  # "EM-1000001", "EM-1000002", ...
    def insert(self, record: Dict[str, Any]) -> Dict[str, Any]: ...       # returns the existing doc on a duplicate source_key
    def get(self, reference: str) -> Optional[Dict[str, Any]]: ...
    def by_source_key(self, source_key: str) -> Optional[Dict[str, Any]]: ...
    def wake(self, reference: str, now: str) -> bool: ...                 # wake += 1; sent -> waiting with due_since=now; next_attempt_at=now; never for "gone"
    def add_note(self, reference: str, text: str, now: str) -> bool: ...  # append {"text","at"} then wake
    def close_runs(self, conversation_id: str, new_started_at: str) -> int: ...  # ended_at=new_started_at where started_at < it and ended_at is None
    def take_due(self, now: str, mode: str, lease_seconds: float, token: str) -> Optional[Dict[str, Any]]: ...
        # one atomic update: state in (waiting, stuck), mode == mode, next_attempt_at <= now,
        # lease_until is None or < now; urgent first, then oldest next_attempt_at; sets lease_until, lease_token
    def renew_lease(self, reference: str, token: str, until: str) -> bool: ...
    def save(self, reference: str, token: str, changes: Dict[str, Any],
             add_to_set: Optional[Dict[str, List[Any]]] = None,
             push: Optional[Dict[str, List[Any]]] = None,
             expect_wake: Optional[int] = None) -> bool: ...              # conditional on _id + lease_token (+ wake); never upserts
    def overdue(self, now: str, mode: str) -> List[Dict[str, Any]]: ...   # waiting/stuck records past their late/stuck threshold
    def counts(self, mode: str, now: str) -> Dict[str, Any]: ...          # {"waiting","stuck","held","oldest_due_seconds"}
    def unverified_since(self, since: str, phone: Optional[str] = None) -> int: ...  # non-urgent unverified records created since
    def contact_for(self, phone: str) -> Optional[str]: ...               # zoho.contact_id of a live, verified record with this phone
    def listing(self, mode: str) -> List[Dict[str, Any]]: ...             # waiting, stuck and held: reference, zoho number, state, mode, last four
    def has_unique_source_key(self) -> bool: ...                          # memory: True; Mongo: reads index_information()

# The ticket seam. MockTicketSystem (tools/mocks.py), DeskTicketSystem and TicketRouter (tickets/seam.py).
#   create(source_key: Optional[str] = None, persona: Optional[str] = None, **fields) -> {"ticket_id", "status"}
#   attach_transcript(ticket_id: str, transcript: str) -> None
#   add_note(ticket_id: str, text: str) -> None
#   close_runs(conversation_id: str, new_started_at: str) -> None
# create() keyword fields (the names the tools already pass, plus new ones):
#   kind, conversation_id, started_at, cluster_id, channel, phone, identity, category, severity,
#   description, frame_number, frame_number_source, bike_model, coverage, customer_name,
#   stated_name, stated_contact, evidence, claimed_purchase_date, purchase_channel
# DeskTicketSystem maps severity -> ai_severity, description -> summary (after redact_pii),
# frame_number/frame_number_source/bike_model -> bike, the stated_*/evidence/claimed_*/purchase_channel
# fields -> claims. Unknown keywords are ignored by Desk and kept by the mock.
class DeskTicketSystem:
    def __init__(self, store: TicketStore, mode: str, environment: str,
                 clock: Callable[[], str] = now_iso, wake: Callable[[], None] = lambda: None) -> None: ...
class TicketRouter:
    records_real_tickets = True
    def __init__(self, desk: DeskTicketSystem, mock: "MockTicketSystem") -> None: ...
    tickets: Dict[str, Dict[str, Any]]  # property: the mock's dict, so tests that read registry.tickets.tickets keep working
    store: TicketStore                  # property: desk.store

# tools/registry.py
@dataclass(frozen=True)
class ToolContext:
    ...existing fields...
    persona: Optional[str] = None
    started_at: Optional[str] = None
# scoped receipt key: "%s:%s:%s:%s" % (conversation_id, started_at, name, key) when
# context.value_for("started_at") is not None, else "%s:%s:%s" % (conversation_id, name, key) as today.

# Ticket tools' new optional injects: "persona", "started_at", "channel", "identity_strength",
# "coverage" (create_support_ticket, submit_warranty_proof), "typed_number" (raise_intake_ticket).
# "conversation_id" becomes a required inject of create_support_ticket and submit_warranty_proof.
# source_key built in each tool: "%s:%s:%s:%s" % (conversation_id, started_at or "", tool_name, idempotency_key).

# conversation.ConversationState new fields (all default None/0, tolerated by from_json):
#   typed_number: Optional[str]      # latest Indian mobile the customer typed in this run, ten digits
#   lookup_error: Optional[str]      # "no_warranty_record" | "oms_unavailable" from the last failed lookup
#   awaiting_callback: Optional[str] # "handover" | "safety" while the callback gate waits (Task 14)
#   callback_asks: int = 0
#   last_code_phone: Optional[str]   # last number a verification code was sent to (Task 15)

# zoho/settings.py
@dataclass(frozen=True)
class ZohoSettings:
    client_id: str; client_secret: str; refresh_token: str; org_id: str
    test_department_id: str; test_contact_id: str
    department_id: Optional[str]; unverified_contact_id: Optional[str]; live: bool
    environment: str; cf_chat_reference: str; cf_source: str
    priority_high: str; priority_medium: str; channel: str
    credits_floor: int; attachment_limit_bytes: int
    @property
    def mode(self) -> str: ...            # "live" if live else "test"
    @property
    def active_department_id(self) -> str: ...
def load_zoho_settings(env: Mapping[str, str]) -> Tuple[Optional[ZohoSettings], str]: ...
    # (None, "not configured") when EMOTORAD_ZOHO_REFRESH_TOKEN is unset;
    # (None, "misconfigured: missing <NAMES>") when a needed name is absent; else (settings, "ok")
def startup_problem(settings: ZohoSettings, *, region: str, store_kind: str, ticket_store: Any,
                    dev_codes: bool, otp_is_mock: bool) -> Optional[str]: ...
    # "not allowed in this region" | "misconfigured: store is not mongodb" |
    # "misconfigured: tickets index missing" | "misconfigured: live refused: test verification in use" | None

# zoho/errors.py: ZohoError(Exception) with .error (a short code safe to log), subclasses:
#   ZohoUnavailable, ZohoUnknownOutcome, ZohoRejected(.fields), ZohoConfigError, ZohoAuthExpired,
#   ZohoGone, ZohoTooLarge, ZohoBusy, ZohoCreditsExhausted(.retry_after_seconds),
#   ZohoTokenRefused, ZohoTokenThrottled
# zoho/http.py
class DeskHTTP:
    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 8.0) -> None: ...
    last_credits_remaining: Optional[int]
    def call(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None,
             *, write: bool, timeout: Optional[float] = None) -> Tuple[int, Any]: ...  # (status, parsed JSON or None)
# zoho/auth.py
class TokenSource:
    def __init__(self, settings: ZohoSettings, http: DeskHTTP, clock: Callable[[], float] = time.monotonic) -> None: ...
    def token(self) -> str: ...
    def invalidate(self) -> None: ...
    state: str   # "ok" | "token refused: <error>" | "throttled"
# zoho/desk.py
class DeskClient:
    def __init__(self, settings: ZohoSettings, tokens: TokenSource, http: DeskHTTP) -> None: ...
    def search_contacts(self, field: str, last_ten: str) -> List[Dict[str, Any]]: ...  # field "phone" | "mobile"
    def create_contact(self, last_name: str, mobile: str) -> str: ...
    def contact_tickets(self, contact_id: str, department_id: str, limit: int = 50) -> List[Dict[str, Any]]: ...
    def create_ticket(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...    # {"id","ticketNumber","webUrl"}
    def add_comment(self, ticket_id: str, content: str) -> str: ...
    def comments(self, ticket_id: str) -> List[Dict[str, Any]]: ...
    def upload_attachment(self, ticket_id: str, filename: str, data: bytes, mime: str) -> str: ...
    def attachments(self, ticket_id: str) -> List[Dict[str, Any]]: ...
def find_adoptable(tickets: List[Dict[str, Any]], cf_api_name: str, chat_reference: str) -> Optional[Dict[str, Any]]: ...
# zoho/payload.py
def subject(record: Dict[str, Any]) -> str: ...
def description(record: Dict[str, Any]) -> str: ...
def ticket_payload(record: Dict[str, Any], settings: ZohoSettings, contact_id: str) -> Dict[str, Any]: ...
def transcript_chunks(reference: str, chat_reference: str, turns: Sequence[TranscriptTurn],
                      limit: int = 30000) -> List[Tuple[str, List[int]]]: ...  # (comment text, turn numbers in it)
# zoho/worker.py
class ZohoWorker:
    def __init__(self, store: TicketStore, client: DeskClient, conversations: Any, media_reader: Any,
                 settings: ZohoSettings, log: Any, clock: Callable[[], str] = now_iso) -> None: ...
    def run_once(self) -> bool: ...        # one record, True if one was taken
    def check_overdue(self) -> None: ...   # logs safety_ticket_late / zoho_ticket_stuck, marks stuck
    def start(self) -> None: ...; def stop(self) -> None: ...; def wake(self) -> None: ...
    status: Dict[str, Any]                 # {"running": bool, "last_pass_at": Optional[str]}
# zoho/wiring.py
@dataclass
class ZohoWiring:
    status: str                            # the /health "zoho" value
    router: Optional[TicketRouter]
    worker: Optional[ZohoWorker]
    store: Optional[TicketStore]
def build_zoho(env: Mapping[str, str], *, ticket_store: Any, store_kind: str, conversations: Any,
               media_reader: Any, log: Any, otp_is_mock: bool, opener: Optional[Callable[..., Any]] = None) -> ZohoWiring: ...
```

## Tasks

Group A, the ticket record (Zoho off throughout):
1. Run on the tool context; receipts scoped by run (`registry.py`, `agents/base.py`, `runtime.py`).
2. The ticket store: clock, kinds, record, `InMemoryTicketStore`, `MongoTicketStore`, indexes, `Stores.tickets`.
3. The seam: `MockTicketSystem` additions, `DeskTicketSystem`, `TicketRouter`.
4. The ticket tools: new injects, `source_key`, kinds, persona, ASSERTED phones, the intake number rule, warranty proof.
5. The runtime: facts, new state fields, run-scoped transcript on every turn, `close_runs`, dealer isolation.

Group B, Zoho client and worker:
6. Zoho settings and start-up checks.
7. Zoho HTTP, errors, token source, Desk client, documented shapes and the fake.
8. The payload: subject, description, fields, transcript chunks.
9. The worker.
10. Wiring: `api.py`, health, lifespan, `chat_local`, entry points on the mock, old mock references.
11. Redaction (`digits.py`, `observability.py`).
12. Person-run scripts, the alarm stack, runbook and contract updates.

Group C, conversation changes:
13. Safety without a known phone, safety record failure, store-down safety, one safety ticket per run, the texts.
14. The callback-number gate and handover tickets.
15. Lock-out tickets, caps, the edge case register rows.

(Task bodies follow, after the amendments.)

---

## Amendments (binding: they override the task bodies wherever they disagree)

Read this whole section before starting any task. The task bodies below were drafted in parallel and then
cross-checked; the cross-check's findings and resolved decisions (Amendment A) and the person's later
decisions (Amendment B) both win over a task body. Where Amendment B contradicts Amendment A, B wins.

### Amendment B: the person's decisions after drafting (5 October 2026)

B1. **No custom fields.** The Desk's text-type custom fields are at their limit, so "Chat reference" and
"Source" do not exist and are never sent.
- Drop `EMOTORAD_ZOHO_CF_CHAT_REFERENCE` and `EMOTORAD_ZOHO_CF_SOURCE` everywhere: settings, `ZohoSettings`
  (`cf_chat_reference`, `cf_source` removed), the required-names list, the test command, `chat_local`
  `WITHHELD`, the runbook rows, the health test, the scripts.
- `ticket_payload` sends **no `cf` key at all**.
- The subject ends with the chat reference in square brackets: `"[AI chat] <label> - <bike> [<chat_reference>]"`,
  for example `[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]`. `[Unverified] ` still comes first
  when unverified. Truncation to 255 characters shortens the label and bike part, never the
  ` [<chat_reference>]` suffix. Add a test with a 400-character label.
- `find_adoptable(tickets, chat_reference)` (the `cf_api_name` parameter is removed): a ticket matches when
  its `subject` (after `.rstrip()`) ends with `" [%s]" % chat_reference` exactly. It still skips tickets
  created before the record when `createdTime` is present. Update every caller (worker step 2,
  `test_ticket.py`).
- The description's second line stays `Source: AI chatbot`. The subject's `[AI chat]` prefix is what any Zoho
  rule or webhook criterion filters on (runbook and contract text say so).
- Docs and shape files lose every `cf_chat_reference` / `cf_source` example.

B2. **Optional layout.** New optional setting `EMOTORAD_ZOHO_LAYOUT_ID` (`ZohoSettings.layout_id: Optional[str]`,
not secret, default None). When set, `ticket_payload` adds `"layoutId": settings.layout_id`. Zoho's OAS
`createTicketRequest` accepts `layoutId`. Add it to the test command's `-u` list and to `chat_local` `WITHHELD`.

B3. **The test department is the existing "Inkodop technologies Pvt.Ltd",** identified only by id
(Amendment A finding 8 and decision 20 stand). No department name is hard-coded anywhere.

B4. **Required layout fields.** The test department's ticket layout marks 11 custom fields as required
(Product Name, Warranty Status, ISSUE, Replacement/Repair/Sale/No Spares sent, Location, Account, Frame No.,
Reverse Pick Up - Spare, Priority Pick-up, Reverse pick up request sent?, Spares Reverse pick up done?).
Whether Zoho enforces "Mark as required" on API-created tickets is unknown.
- `probe.py` records, for every active ticket layout of the test and real departments: the layout id, whether
  it is the default, each field's `apiName`, `displayLabel`, `type`, `isMandatory`, and the allowed values of
  pick lists (pick lists whose label names people or dealers are counted, never listed).
- `test_ticket.py` creates its first ticket **without** any custom field. If Zoho answers 422 `INVALID_DATA`, it
  prints the field names Zoho named (never values), saves the masked error shape, and stops with
  "Zoho enforces the layout's required fields: the support lead must choose a value for each before part C
  continues." If Zoho accepts it, it carries on with the rest of its checks.
- The worker treats a 422 naming fields as `ZohoRejected` (hourly, `zoho_rejected` with field names), as
  already planned. No default values for these fields are invented in this plan.

B5. **The whole-suite command** is therefore:
`env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_LAYOUT_ID -u EMOTORAD_ZOHO_PRIORITY_HIGH -u EMOTORAD_ZOHO_PRIORITY_MEDIUM -u EMOTORAD_ZOHO_CHANNEL -u EMOTORAD_ZOHO_CREDITS_FLOOR -u EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB .venv/bin/python -m unittest discover -s tests -t .`
Use `zoho.settings.ENV_NAMES` (every `EMOTORAD_ZOHO_*` name) wherever code or tests need the list.

B6. **Erasure stays out** (spec section 11 deferred). `erasure_admin.PERMANENT` and `delete_person` are not
changed in this plan.

### Amendment A: the cross-check of the drafts (findings, resolutions, resolved interface decisions)

Finding 1 is resolved: the full drafts of Tasks 6, 7 and 8 were recovered and are below. Findings 2 to 33 and
the resolved interface decisions apply as written, except where Amendment B changes them (custom fields:
decision 15's `ticket["cf"][cf_api_name]` becomes the subject-suffix match of B1; decision 22's count of
names becomes B5's list).

The six drafts can't go to engineers as they stand. 8 blockers, 11 major and 14 minor problems are listed below, with the fix for each, then my resolution for every drafter's interface issue, then the decisions to apply across all tasks. I checked everything against the real code at 9cc10af without running any tests, so the drafters' test counts are unconfirmed.

## Findings

1. **Blocker. Tasks 6, 7 and the start of 8: the drafts are missing.** The "tasks-6-8" draft starts mid-file (`ike's cannot be told",`).
   - Task 6 (settings and start-up checks) is absent.
   - Task 7 (http, errors, auth, Desk client, `tests/fake_zoho.py`, the shapes, the shape contract test) is absent.
   - Task 8's Files, Interfaces, tests and Steps 1 to 3 are absent, as are the start of `zoho/payload.py` and the constants it uses (`CHANNELS`, `COVERAGE_PREFIX`, `COVERAGE_TEXT`, `NO_BIKE`, `SOURCE`, `STATUS`, `MODEL_RAISED`, `AI_HEADING`, `CODE_HEADING(S)`, `EMPTY_SUMMARIES`, `TAKEOVER_LINE`, `VERIFIED`/`UNVERIFIED`, `CLAIM_LIMIT`, `SUBJECT_LIMIT`, `DESCRIPTION_LIMIT`, `CUT`, `CF_LIMIT`, `UTC_LINE`, `TRANSCRIPT_LIMIT`).
   - Tasks 9, 10 and 12 import all of these.
   - Fix: re-draft Tasks 6 and 7 and the whole of Task 8 before execution. They must match the "Resolved interface decisions" at the end, especially items 13 to 17 and 26.

2. **Blocker. Tasks 2 and 9: `check_overdue` cannot mark a record stuck.**
   - Task 9 calls `save(reference, record.get("lease_token"), {"state": "stuck"})`. The token is None for any record not under lease, and Task 2's `save` returns False for a falsy token.
   - So nothing is ever marked stuck, and `OverdueTests.test_a_safety_ticket_is_late_at_ten_minutes...` and `test_any_other_ticket_is_stuck_after_a_day...` fail.
   - Fix in Task 2: add `def mark_stuck(self, reference: str) -> bool: ...` to the TicketStore protocol and both stores. It is a conditional update `{"_id": reference, "state": "waiting"}` → `$set {"state": "stuck"}`, with no lease condition, returning matched == 1. Add contract tests: waiting becomes stuck; sent, gone and unknown are refused; a leased record can be marked.
   - Fix in Task 9: replace the save in `check_overdue` with `self.store.mark_stuck(reference)`.

3. **Blocker. Task 9: `zoho_number` is redacted in the log.** `EventLog.emit` runs `redact_fields` → `redact_pii`. A bare Zoho ticket number such as "1000" or "102345" matches `_OTP_ALONE`, so it is logged as "[4 digits]".
   - `FieldTests.test_a_sent_ticket_is_logged_with_its_number_and_the_credits_left` fails, and in production the number never reaches the log.
   - Fix: in `zoho_ticket_sent`, `zoho_ticket_adopted` and `zoho_ticket_gone`, log `zoho_number="#%s" % number if number else None`. In the test, expect `"#1000"`.

4. **Blocker. Task 9: a test that cannot pass.** In `TicketTests.test_a_timeout_after_the_ticket_was_made_finds_it_and_makes_no_second`, `self.assertNotIn("ticket_id", saved["zoho"])` always fails, because `new_record` writes `zoho.ticket_id = None`.
   - Fix: `self.assertIsNone(saved["zoho"]["ticket_id"])`.

5. **Blocker. Tasks 5 and 13: an import edit that no longer matches.**
   - Task 5 changes the runtime line to `from .verify_first import CONFIRMED, NUMBER, VerifyFirst, ascii_digits, find_phone`.
   - Task 13's old_string is still `from .verify_first import CONFIRMED, NUMBER, VerifyFirst`, which no longer exists. Task 13 also adds `from .digits import ascii_digits`, which imports the name twice.
   - Fix: Task 13 replaces Task 5's line with `from .verify_first import CONFIRMED, NUMBER, VerifyFirst, find_phone, looks_like_a_number, redact` and adds `from .digits import ascii_digits`. Task 14's later edit of this line then matches.

6. **Blocker. Tasks 5 and 14: two mechanisms for the same merge, and Task 14's edits won't apply.**
   - Task 5 already adds `TURN_FACT_FIELDS`, `facts_loaded` and `_merge_onto_fresh(..., loaded=None)`.
   - Task 14 steps 3d, 3f and 3g add the same five fields again as `CALLBACK_FIELDS`/`changed`. Their old_strings are the pre-Task-5 text (`looked_up=state.coverage_result != coverage_loaded\n` and the old signature), so the edits fail.
   - Fix: delete Task 14 steps 3d, 3f and 3g, and remove `CALLBACK_FIELDS` and `changed` from Task 14's Produces. Task 14's ConflictTests pass on Task 5's mechanism.

7. **Blocker. Tasks 9, 12 and 13: the alarm-stack test fails on events nobody classified.**
   - `AlarmStackTests.test_every_zoho_event_the_code_emits_has_a_decision` scans for `emit("zoho_…" | "safety_ticket_…" | "unverified_ticket_…")`.
   - Task 9 emits `zoho_ticket_gone`, `zoho_ticket_adopted` and `zoho_record_dropped`. Task 13 emits `safety_ticket_failed`. None of the four is in `ALARMED` or `NOT_ALARMED`, and Task 13 wrongly says no existing test changes.
   - Fix in Task 12: `NOT_ALARMED = ("zoho_ticket_sent", "zoho_retry", "zoho_rejected", "zoho_ticket_gone", "zoho_ticket_adopted", "zoho_record_dropped", "safety_ticket_failed")`. Each of those cases is either already alarmed (`safety_ticket_not_recorded` follows `safety_ticket_failed`) or is an end state.

8. **Blocker. Task 12: `test_ticket.py` will refuse the real test department.**
   - The spec's 5 October decision: the test department is the existing "Inkodop technologies Pvt.Ltd", identified by `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID`. `test_ticket.py` must refuse unless the id given is the configured test department and the person types its name.
   - Task 12 hard-codes `TEST_DEPARTMENT_NAME = "AI chatbot test"`, so person step 6 can never run.
   - Fix:
     - Drop `TEST_DEPARTMENT_NAME`.
     - Add `--test-department-id` (required) to `test_ticket.py` and `probe.py`.
     - Change the signature to `department_refusal(names, department_id, test_department_id, real, typed_name)`:
       - without `--real-department`: refuse unless `department_id == test_department_id` and `typed_name.strip() == names[department_id]`;
       - with it: refuse if `department_id == test_department_id`, and require the typed name.
     - Always prompt for the name.
     - Remove the probe line "writes only to a department named 'AI chatbot test'".
     - In the runbook row for `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID`, say "the configured test department (Inkodop technologies Pvt.Ltd), by id".
     - Update `DepartmentGuardTests` to match.

9. **Major. Tasks 5 and 9: a second person's words and photos can reach the first person's Zoho ticket.** This breaks the Review Focus rule for a second person on the same browser after `restart_for`.
   - Between person one's proof lapsing and `restart_for`, the next visitor's turns ("hello…", `[phone]`, `[code]`) and any photo they send fall inside person one's run window.
   - Task 5 keeps them off the mock, but the worker posts by time window only. The cluster filter does not help, because a shared browser is the same cluster.
   - Fix in Task 5: in `_attach_transcript`, when `not self._speaker_owns_run(state, resolved)` and the run holds a Desk ticket (`is_desk_reference(state.ticket_id)`), call `tickets.close_runs(state.conversation_id, <this turn's customer TranscriptTurn.at>)`, then return. Accepted cost: if the same owner re-proves without a restart, their later turns are not posted.
   - Add a Desk test to `two_people`: person one's record has `ended_at` at or before the stranger's first turn `at`, and Task 9's `_in_run` excludes it.

10. **Major. Task 5: `close_runs` runs after the turn is recorded.** `_close_earlier_runs` runs after `record_turn`. A worker pass on any server in between can post the new run's first turn to the previous run's ticket.
    - Fix: in `_handle`, call `self._close_earlier_runs(state)` straight after the successful save (before `record_turn`). `_attach_transcript` stays after.

11. **Major. Task 13: two safety tickets can exist in one run.** Safety uses two source keys: `…:safety_callback` (gate) and `…:create_support_ticket:safety:<cid>:<started_at>` (tool).
    - An anonymous visitor who records via a typed number, then verifies in the same run (no `restart_for`, since `user_key` was None), then reports again, gets a second urgent record. The spec requires one safety ticket per run.
    - Fix in `_safety_with_phone`: when `self._desk_store()` is not None, first look up `by_source_key(self._gate_key(state, PURPOSE_SAFETY))`. If it is found and not gone, add `_again_note` and reply `SAFETY_MESSAGE` plus that reference.
    - Fix in `_safety_without_phone`: also check `"%s:%s:create_support_ticket:safety:%s:%s" % (cid, started, cid, started)`.
    - Add a test.

12. **Major. Spec §6: `safety_ticket_not_recorded` must be "counted on /health"; no task does it.**
    - Fix in Task 13: add `self.safety_not_recorded = 0` in `Runtime.__init__` and a helper `_note_safety_not_recorded(cid, why)` that increments it and emits the event. `_store_down`, `_safety_not_recorded` and Task 14's callback gate use it.
    - In `api.health`, add `"safety_tickets_not_recorded": runtime.safety_not_recorded` only when it is above 0, so the pinned health test is unchanged.

13. **Major. Task 13 (Review Focus, Tier 1): the safety backstop with no known phone still promises a hand-over.**
    - When a model reply claims a ticket about a live hazard, there is no phone and Zoho is on, `_safety_backstop` returns None and the turn becomes `HANDOVER_TEXT` ("pass you to someone…"). Nothing is recorded and no number is asked for.
    - Task 15's register row 7.3 wrongly says the hazard case "is backed by the safety backstop".
    - Fix:
      - With no phone, a customer, and `_desk_store()` not None: replace the turn text with `SAFETY_NO_CONTACT_MESSAGE` and set `state.awaiting_callback="safety"`, `callback_asks=0`.
      - With Zoho off: `SAFETY_NOT_RECORDED_MESSAGE` plus `_note_safety_not_recorded(cid, "backstop_no_contact")`.
      - Test that the model was called once only (for the original reply) and that no promise words appear.
      - Reword row 7.3 to "not about a live hazard, or with no number known before Zoho".

14. **Major. Global constraints: the test command leaves five Zoho names set.** Spec §9 says every name is blanked.
    - Fix: add `-u EMOTORAD_ZOHO_PRIORITY_HIGH -u EMOTORAD_ZOHO_PRIORITY_MEDIUM -u EMOTORAD_ZOHO_CHANNEL -u EMOTORAD_ZOHO_CREDITS_FLOOR -u EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB` to the Global Constraints command and every task's commands.

15. **Major. Tasks 6 and 10: two strings for an unreadable index.**
    - Task 6 (per Task 8's drafter, issue 2) returns `"misconfigured: tickets index not readable (<Class>)"`.
    - Task 10 maps `StoreUnavailable` to `"misconfigured: store unreachable"` and tests that string.
    - Fix: define `STORE_UNREACHABLE = "misconfigured: store unreachable"` in `zoho/settings.py`. `startup_problem` lets `StoreUnavailable` propagate. `build_zoho` catches it and uses the imported constant. Remove Task 10's local definition.

16. **Major. Tasks 7 and 12: `DeskHTTP.call` gains a `classify` keyword the skeleton doesn't have.**
    - Task 12's `post_form` and `FakeAccounts.call` don't know about it, so a token-endpoint 400 (`invalid_code`) is classified as `ZohoRejected`.
    - Fix: skeleton signature `call(self, method, url, headers, body=None, *, write, timeout=None, classify=True)`. Task 12's `post_form` passes `classify=False`. `FakeAccounts.call` accepts `classify=True`. Accounts-host calls return `(status, body)` for any status.

17. **Major. Task 3 (privacy): a Desk record with no run start pulls in every earlier run.**
    - `DeskTicketSystem.create` accepts `started_at=None` because `started_at` is an optional inject. Task 9's `_in_run` then treats the record as covering the whole conversation, every earlier run and person included. Spec §2 makes `started_at` required.
    - Fix: raise `ValueError("a Desk ticket needs started_at")` before taking a reference when `not fields.get("started_at")`. Add `support_fields(started_at=None)` to the refused list in `test_only_customer_tickets_with_a_key_kind_and_conversation_are_recorded`.

18. **Major. Task 14: a gone ticket can be quoted again.** `_record_ticket` re-creates with the same source key. Desk returns the existing record even when its state is "gone", so the callback gate or handover quotes a reference deleted in Desk.
    - Fix in Task 13's `_record_ticket`: after `create`, read `store.get(reference)`. If its state is "gone", emit `ticket_record_failed error="gone"` and return `Recorded(None)`.

19. **Major. Task 13: confirm the change to `escalated`.** Anonymous safety replies with Zoho off now have `escalated=False` and no escalation event, which changes metrics.
    - This matches spec §6 ("escalated: true … when a ticket is recorded or the handover wait ends"). Record it in Task 13's commit message and the PR's behaviour changes.
    - Before merging, grep `tests/test_e2e_*`, `test_metrics` and `test_video_first` for anonymous safety turns that assert `escalated`.

20. **Minor. Task 5: duplicate `started_at` facts.** Task 5 adds `"started_at"` to the agent facts dict (Task 1 already did) and to the safety branch's late facts (Task 1 sets it on the ToolContext).
    - Fix: remove the `"started_at"` lines from Task 5's two replacement blocks.

21. **Minor. Task 4: `\d` in a mobile pattern.** `_TEN_DIGIT_MOBILE = re.compile(r"[6-9]\d{9}")` can match Devanagari digits.
    - Fix: `re.compile(r"[6-9][0-9]{9}")`.

22. **Minor. Task 10: `\d` in the mock-ticket pattern.** `_MOCK_TICKET = re.compile(r"EM-\d{5}")`.
    - Fix: `re.compile(r"EM-[0-9]{5}")`.

23. **Minor. Task 15: the intake cap block duplicates Task 4's logic.** It uses `identity_strength != VERIFIED`, a hand-built key and `"+91" + typed_number`.
    - Fix: cap when `identity != "verified"`, with `phone=callback` and `source_key=ticket_source_key(conversation_id, started_at, RAISE_INTAKE_TICKET, idempotency_key)`.
    - Imports: add only `from ..tickets.caps import CAP_TEXTS, cap_reached` and `from ..tickets.clock import now_iso`, because Task 4 already imports `VERIFIED`.

24. **Minor. Tasks 10 and 13: two "is Zoho on" checks.** `_desk_store()` uses a truthy `getattr`, while Task 10 adds `_records_real_tickets()` with `is True`.
    - Fix: `_desk_store` returns `self.registry.tickets.store if self._records_real_tickets() else None`.

25. **Minor. Task 3: `build_registry` uses `ticket_system or MockTicketSystem()`.**
    - Fix: `ticket_system if ticket_system is not None else MockTicketSystem()`, as Task 5's drafter (issue 11) suggested.

26. **Minor. Task 9: string literals for states.**
    - Fix: import `WAITING`, `SENT`, `STUCK`, `GONE` from `tickets.record`, as Task 2 asked.

27. **Minor. Task 9: a deleted media object retries for ever.** A media key whose S3 object is gone raises `StorageError` and goes on the retry schedule indefinitely.
    - Fix: a not-found `StorageError` posts a media note ("could not be read") and marks the key posted. Other errors keep the schedule.

28. **Minor. Task 15: a capped lock-out re-alarms on every message.** While locked out over a cap, every later message retries the record and emits `unverified_ticket_capped` again.
    - Fix: on the first refusal, append `"lockout_capped"` to `state.transitions`. `_lockout` skips recording when it is present.

29. **Minor. Task 10: `build_zoho` can crash at import.** With `ticket_store=None` (tests build `Stores` without tickets, as in `test_audit_edges`) and Zoho settings set, it raises `AttributeError` at import.
    - Fix: treat `ticket_store is None` as `"misconfigured: store is not mongodb"`.

30. **Minor. Task 14: handover tickets lack coverage and name.** Verified handover tickets carry no `coverage` or `customer_name`.
    - Fix: pass `coverage=coverage_fact(state, resolved)` and the OMS name (the selected bike record's `customer_name` when `warranty_on_record` is not False) through `_record_ticket(**extra)`.

31. **Minor. Plan file list.**
    - Add new files: `tests/test_run_scoped_receipts.py`, `tests/test_digits.py`, `src/emotorad_ai/tickets/caps.py`.
    - Add modified files: `tests/test_mongo_store.py`, `tests/test_mongo_scripts.py`, `tests/test_verification.py`, `tests/test_late_warranty.py`, `tests/test_memory.py`, `tests/test_graph.py`, `tests/test_log_redaction.py`.
    - Task 14 should update the turn order in the repo `CLAUDE.md` (callback_gate after safety).

32. **Minor. Task 11: Devanagari digits change in the permanent transcript.** `redact_pii` now rewrites Devanagari digits as ASCII in every transcript turn it records. That is acceptable, but say so in Task 11's commit message and the PR's behaviour changes.

33. **Minor. All tasks: test counts are unconfirmed.** Every "N tests" figure in Steps 2 and 4 comes from reading the drafts. Engineers paste the real counts.

## The drafters' interface issues, and my resolution for each

**Tasks 1 to 3**
1. `held` is not stored: accept. In the skeleton, `held` is a listing and health label only.
2. `wake`/`add_note` move `next_attempt_at` to `min(current, now)`: accept, and update the skeleton comment.
3. The Mongo wake uses two updates: accept. Task 9 relies on `expect_wake`, which it does.
4. A falsy token makes `save`/`renew_lease` return False: accept. Add `mark_stuck` (finding 2).
5. Tie-breaks in `take_due` and the listing: accept.
6. New names: accept. Tasks 9, 10 and 12 import them (finding 26).
7. Row and count shapes: accept as written.
8. `is_desk_reference` uses `[0-9]`: accept. Change the skeleton.
9. Desk `create` is stricter: accept, and also require `started_at` (finding 17).
10. Extra attributes: accept. Use one check, `_records_real_tickets` (finding 24).
11. The `started_at` format: accept. Store fields are compared as strings; turn and media times, `started_at` and `ended_at` are always parsed.
12. `erasure_admin.PERMANENT` is not changed: accept, since erasure is deferred.
13. New test file and `Optional[Any]` annotation: accept. Add to the file list.
14. Counts unverified: engineers confirm them.

**Tasks 4 and 5**
1. `identity_strength` is wired in Task 4: accept.
2. `ticket_kind` as an inject: accept, set only in `_raise_safety_ticket`.
3. `cluster_id` injected on all three tools: accept.
4. Extra injects on intake: accept.
5. The `remedy` text: use `remedy="ask_for_callback_number"`, following the registry convention that a remedy is a machine-readable action, and keep the spec's sentence in the message. Update the two tests.
6. Run bounds against the worker: fix as in finding 9, not with `attached_until`, which re-includes a stranger's turns once the owner returns.
7. A failed `close_runs` is not retried: accept, together with findings 9 and 10.
8. `coverage_fact` falls back to this turn's identity look-up: accept.
9. Unverified tickets drop coverage and name: accept.
10. `typed_number` for customers only: accept.
11. `build_registry`'s `or`: fix as in finding 25.
12. Time formats: as in item 11 above.
13. Fixture phones for the two-person tests: accept (invented numbers).
14. Persona on contexts: confirmed. Task 1 sets it in `Agent.run`, the narrow prefetch and the safety context.

**Tasks 6 to 8**
1. The `classify` keyword: accept, as in finding 16.
2. Extra outcome strings: accept "misconfigured: bad number: <NAMES>". For an unreadable index, use `STORE_UNREACHABLE` (finding 15).
3. Bike keys: `{model, frame_number, frame_number_source}`. Task 3 already writes these.
4. `TokenSource` waits on its own: accept. Task 9 also waits hourly, which is harmless.
5. urllib behaviour: accept, with a test pinning `URLError` → `ZohoUnavailable` and bare timeout or reset on a write → `ZohoUnknownOutcome`.
6. Whether the list carries `cf`: `find_adoptable` reads `ticket["cf"][cf_api_name]`. `test_ticket.py` verifies it on the real ticket.
7. Every 404 is `ZohoGone`: accept. Task 9 decides by step.
8. Chunk details: accept. Task 9 stores and compares the exact intent marker.
9. Missing names in the test command: finding 14.
10. Limits: accept.
11. Placeholder custom field names: accept.
12. Newlines in the description: `test_ticket.py` reads the ticket back and checks.
13. `mode_mismatch` refusal: accept.

**Tasks 9 and 10**
1. FakeDesk instead of `fake_zoho`: accept. Optionally add one test running the real `DeskClient` over `fake_zoho` once Task 7 lands.
2. Error constructors: fixed in decision 13 below.
3. `status["failing"]`: accept. Add it to the skeleton.
4. The whole `zoho` sub-document per save: accept.
5. Stuck through `save` with a None token: rejected. Use `mark_stuck` (finding 2).
6. `counts` has no total: accept the approximation (shown while Zoho is on or anything is outstanding).
7. Assumptions about `load_zoho_settings`: binding on Task 6 (decision 15).
8. `startup_problem` can raise: finding 15.
9. An unknown outcome is resolved on the next pass: accept.
10. Extra events: add them to `NOT_ALARMED` (finding 7). The `level="error"` field is accepted.
11. `DeskClient.http`: make it a public attribute in the skeleton.
12. `find_adoptable` and `cf`: as Tasks 6 to 8, item 6.
13. Times parsed: accept as the rule.
14. No media bucket: accept. Not-found handling is finding 27.
15. `stop()` waits up to 10 seconds: accept.
16. Dependencies: satisfied.
17. The test command: finding 14.

**Tasks 11 and 12**
1. `fake_zoho` not used: accept.
2. `DeskHTTP` on the accounts host: `classify=False` (finding 16).
3. Header building in the scripts: Task 7 exports `ACCOUNTS_URL`, `DESK_URL` and `DeskClient.headers()`. `_common.py` imports them instead of repeating hosts and headers.
4. Listing keys: they match Task 2.
5. Desk `create` identity, and `InMemoryTicketStore()` with no arguments: confirmed.
6. Building `ZohoSettings` directly: no `__post_init__` validation, so `script_settings` keeps working.
7. Exact `cf` match: confirmed.
8. The revoke address: keep the spec's. The person checks Zoho's current page first.
9. Zoho ids redacted as long digit runs: log our reference and `"#<ticketNumber>"` (finding 3).
10. Event decisions: finding 7.
11. Devanagari digits in the transcript: finding 32.

**Tasks 13 to 15**
1. `InMemoryTicketStore()` with no arguments: confirmed.
2. All five state fields come from Task 5: confirmed.
3. Save conflicts: use Task 5's mechanism and drop Task 14's (finding 6).
4. `tickets/caps.py`: accept. Add it to the file list.
5. Intake names: they match Task 4. Apply finding 23.
6. `escalated` false when nothing was recorded: accept (finding 19).
7. Repeated safety detection: also check both keys (finding 11).
8. Draft texts: confirmed by the support lead at person step 10.
9. `guardrail:callback:*` handled_by values: accept.
10. The `level` field: accept. Alarms match on event name.
11. Turn order in `CLAUDE.md`: Task 14 updates it.
12. Gate tickets lack coverage and name: finding 30.

## Resolved interface decisions

1. **TicketStore protocol:** the skeleton methods plus `mark_stuck(reference) -> bool` (waiting becomes stuck, no lease condition). `save` and `renew_lease` with a falsy token return False. `held` is never stored. `listing` rows are `{"reference", "zoho_number", "state", "mode", "last_four"}`. `counts` returns `{"waiting", "stuck", "held", "oldest_due_seconds"}`.

2. **`wake` and `add_note`:** `wake` += 1. A sent record becomes waiting with `due_since=now` and `next_attempt_at=now`. A waiting or stuck record gets `next_attempt_at=min(current, now)`. Gone or unknown returns False. In MongoDB, the `$inc`/`$min` update runs first, then the sent-to-waiting update.

3. **Digits:** `is_desk_reference` uses `EM-[0-9]{7,}`. Every new digit pattern uses `[0-9]`, and code calls `ascii_digits` (from `emotorad_ai.digits` after Task 11) before reading digits.

4. **ToolContext:** gains `persona` and `started_at`. The receipt key is `"%s:%s:%s:%s" % (cid, started_at, tool, key)` when `value_for("started_at")` is not None, otherwise `"%s:%s:%s" % (cid, tool, key)`.

5. **Ticket tool injects:**

   | Tool | `injects` | `optional_injects` |
   |---|---|---|
   | `create_support_ticket` | `("phone", "conversation_id")` | evidence_seen, selected_bike, unlisted_bike, persona, started_at, cluster_id, channel, identity_strength, coverage, ticket_kind |
   | `raise_intake_ticket` | `("conversation_id",)` | phone, identity_strength, typed_number, persona, started_at, cluster_id, channel |
   | `submit_warranty_proof` | `("phone", "conversation_id")` | persona, started_at, cluster_id, channel, identity_strength, coverage |

   - `ticket_kind="safety"` is set only in `_raise_safety_ticket`.
   - The `identity_strength` fact is wired in Task 4. `channel`, `coverage` and `typed_number` are wired in Task 5. `started_at` is wired in Task 1.
   - `source_key = "%s:%s:%s:%s" % (cid, started_at or "", tool, idempotency_key)`. Gate keys are `"%s:%s:%s" % (cid, started_at or "", purpose)`, with purpose `safety_callback`, `handover` or `lockout`.

6. **`DeskTicketSystem.create`:** raises `ValueError` before taking a reference when the persona is not "customer", or `source_key`, a known `kind`, `conversation_id` or `started_at` is missing. A bike is kept only when verified, as `{model, frame_number, frame_number_source}`. It returns `{"ticket_id", "status": "open"}`. `attach_transcript` and `add_note` raise `KeyError` for an unknown id and do nothing for a gone one.

7. **Which system is in use:** `MockTicketSystem.records_real_tickets = False` and `TicketRouter.records_real_tickets = True`. The only check is `Runtime._records_real_tickets()`, which `_desk_store()` uses. `build_registry` uses `is not None`.

8. **Conversation state:** the five fields come only from Task 5 (`typed_number`, `lookup_error`, `awaiting_callback`, `callback_asks`, `last_code_phone`). A lost save race carries them through Task 5's `TURN_FACT_FIELDS` and `loaded`. There is no `CALLBACK_FIELDS`.

9. **Times:** ticket-store fields use the `now_iso()` format and are compared as strings. Turn `at`, media `stored_at`, `started_at` and `ended_at` are parsed before any comparison.

10. **Run bounds:** `close_runs(cid, new_started_at)` runs at a run's first turn, after the save and before `record_turn`. It also runs at the first turn by someone who does not own the run, at that turn's `at`.

11. **One safety ticket per run:** both safety keys (the gate's and the tool's) are checked before recording. A repeat adds a note.

12. **`escalated`:** true only when a ticket is recorded or a handover wait ends. Every safety reply that recorded nothing has `escalated=False` and the steps plus the 112 line. `safety_ticket_not_recorded` is counted on `/health` as `safety_tickets_not_recorded`.

13. **Zoho errors:** `ZohoError(message, *, error)`, where `.error` is a short code safe to log. Subclasses: `ZohoRejected(..., fields=())`, `ZohoCreditsExhausted(..., retry_after_seconds=None)`, and the rest as in the skeleton. Messages never carry a body, token, secret or phone.

14. **HTTP:** `DeskHTTP.call(method, url, headers, body=None, *, write, timeout=None, classify=True)`. Accounts-host calls use `classify=False` and return `(status, body)` for any status. It exposes `last_credits_remaining`.

15. **Desk client:** `DeskClient(settings, tokens, http)` keeps public `.http` and `.tokens`. Task 7 exports `ACCOUNTS_URL`, `DESK_URL` and `DeskClient.headers()`. `find_adoptable` reads `ticket["cf"][cf_api_name]` (exact match) and skips tickets created before the record when `createdTime` is present.

16. **Settings:** `ZohoSettings` has no `__post_init__` validation; all checks are in `load_zoho_settings`. A blank variable counts as unset. Only `EMOTORAD_ZOHO_LIVE == "yes"` means live. The status strings are exactly:
    - "not configured"
    - "misconfigured: missing <NAMES>"
    - "misconfigured: bad number: <NAMES>"
    - "not allowed in this region"
    - "misconfigured: store is not mongodb"
    - "misconfigured: tickets index missing"
    - "misconfigured: store unreachable" (`STORE_UNREACHABLE` in `zoho/settings.py`; `build_zoho` maps a `StoreUnavailable` to it)
    - "misconfigured: live refused: test verification in use"
    - "test department", "live", "token refused: <error>", "sending failing: <code>"

17. **Worker status:** `ZohoWorker.status = {"running", "last_pass_at", "failing"}`. `zoho_status()` shows `failing` before the start-up status. `ZohoWiring` defaults `router`, `worker` and `store` to None. A `ticket_store` of None counts as not mongodb.

18. **Events:** the alarmed seven are zoho_misconfigured, zoho_token_refused, zoho_worker_error, zoho_ticket_stuck, safety_ticket_late, safety_ticket_not_recorded and unverified_ticket_capped, each with `level="error"`. Not alarmed: zoho_ticket_sent, zoho_retry, zoho_rejected, zoho_ticket_gone, zoho_ticket_adopted, zoho_record_dropped, safety_ticket_failed. `zoho_number` is logged as `"#<n>"`, never as bare digits.

19. **`contact_number_required`:** `remedy="ask_for_callback_number"`, with the spec's sentence in the message.

20. **Test department:** identified only by id (`EMOTORAD_ZOHO_TEST_DEPARTMENT_ID` / `--test-department-id`). The person types its name at run time; no department name is hard-coded.

21. **Test doubles and shapes:** worker tests use a DeskClient-level FakeDesk, the scripts use FakeAccounts, and `tests/fake_zoho.py` (Task 7) is the opener-level double for the http, auth and Desk tests and the shape contract test. Shape files are `{"_source": ..., **masked}` for an object, otherwise `{"_source": ..., "body": ...}`. Task 7's contract test and Task 12's `save_shape` both use this format.

22. **Test command:** the whole-suite command unsets all 16 `EMOTORAD_ZOHO_*` names.

23. **Task order:** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, unchanged. Task 13 swaps the runtime's `verify_first` import line left by Task 5 for one that pulls `ascii_digits` from `.digits`. Task 14 drops its merge edits.

---

<!-- drafted as tasks-1-3 -->

### Task 1: Put the run and the persona on the tool context, and scope receipts by run

**Files:**
- Modify: `src/emotorad_ai/tools/registry.py:9-10` (docstring item 2), `:76-79` (`ToolContext` gains `persona` and `started_at`), `:262-269` (receipt key scoped by run)
- Modify: `src/emotorad_ai/agents/base.py:200-205` (`Agent.run` sets `persona`)
- Modify: `src/emotorad_ai/runtime.py:1006-1010` (narrow prefetch context), `:1223-1229` (facts gain `started_at`), `:1514-1519` (safety ticket context)
- Modify: `src/emotorad_ai/stores/mongo.py:138-141` (docstring only: the receipt key shapes erasure matches)
- Test: `tests/test_run_scoped_receipts.py` (new)

**Interfaces:**
- Consumes: `ConversationState.started_at` and `ConversationState.restart_for(user_key, started_at)` (existing), `ResolvedIdentity.persona` (existing), `ToolContext.value_for(name)` (existing).
- Produces: `ToolContext.persona: Optional[str] = None` and `ToolContext.started_at: Optional[str] = None`. The receipt key is `"%s:%s:%s:%s" % (conversation_id, started_at, name, key)` when `context.value_for("started_at") is not None`, and otherwise `"%s:%s:%s" % (conversation_id, name, key)` as today. The runtime's agent facts gain `"started_at": lambda: state.started_at`. `Agent.run` sets `persona=resolved.persona`. The narrow prefetch and `_raise_safety_ticket` set `persona=resolved.persona` and `started_at=state.started_at` directly. Task 4 reads both through `optional_injects`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_scoped_receipts.py`:

```python
"""Write receipts scoped by run, and the persona and run on the tool context
(spec 2026-10-05-zoho-desk-tickets-design.md, section 2; plan Task 1).

A conversation id can hold several runs: a thread that went quiet past its
working-state expiry and started again, or a second person on the same
browser after verify-first's restart_for. A key the model reuses in a new
run must raise a new ticket, never hand back the first run's.
"""

import unittest
from dataclasses import replace
from datetime import date

import mongomock

from emotorad_ai.agents.base import Agent, AgentDefinition
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY_SUPPORT
from emotorad_ai.config import Settings
from emotorad_ai.contract import Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.jev import JevDecision
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import MongoConversationStore, MongoIdempotencyStore, ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, MockTicketSystem, build_registry
from emotorad_ai.tools.registry import IdempotencyStore, ToolContext, ok
from emotorad_ai.tools.verification import REQUEST_IDENTITY_VERIFICATION
from tests.test_agent_and_runtime import make_runtime
from tests.test_agent_and_runtime import send as send_web
from tests.test_jev_runtime import NARROW_WITH_WARRANTY
from tests.test_jev_runtime import build as build_jev
from tests.test_jev_runtime import send as send_jev
from tests.test_runtime_persistence import runtime_on
from tests.test_runtime_persistence import send as send_text
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

TODAY = date(2026, 7, 28)
RUN_A = "2026-10-05T09:00:00.000000+00:00"
RUN_B = "2026-10-05T11:00:00.000000+00:00"
PHONE = "+919999999999"
TICKET = {"category": "battery_charging", "severity": "normal", "description": "LED stays off.",
          "idempotency_key": "k1"}


class RecordingReceipts(IdempotencyStore):
    """The in-memory receipt store, noting every key it is asked to claim."""

    def __init__(self):
        super().__init__()
        self.claimed = []

    def claim(self, key):
        self.claimed.append(key)
        return super().claim(key)


def spy_on(registry):
    """Every (tool, context) the registry is called with, in order."""
    calls, real = [], registry.call

    def call(name, arguments, context, **kwargs):
        calls.append((name, context))
        return real(name, arguments, context, **kwargs)

    registry.call = call
    return calls


def context(**fields):
    return ToolContext(conversation_id="c1", phone=PHONE, **fields)


class ToolContextTests(unittest.TestCase):
    def test_persona_and_run_default_to_unknown(self):
        bare = ToolContext(conversation_id="c1")
        self.assertIsNone(bare.persona)
        self.assertIsNone(bare.value_for("started_at"))

    def test_a_run_given_late_is_read_at_call_time(self):
        run = [RUN_A]
        late = ToolContext(conversation_id="c1", late={"started_at": lambda: run[0]})
        run[0] = RUN_B  # restart_for moved the run after the context was built
        self.assertEqual(late.value_for("started_at"), RUN_B)

    def test_a_run_set_directly_wins_over_a_late_one(self):
        both = ToolContext(conversation_id="c1", started_at=RUN_A, late={"started_at": lambda: RUN_B})
        self.assertEqual(both.value_for("started_at"), RUN_A)


class ReceiptKeyTests(unittest.TestCase):
    def setUp(self):
        self.receipts, self.tickets = RecordingReceipts(), MockTicketSystem()
        self.registry = build_registry(ticket_system=self.tickets, idempotency=self.receipts)

    def raise_ticket(self, ctx):
        return self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), ctx)

    def test_a_context_with_a_run_scopes_the_key_by_it(self):
        self.raise_ticket(context(started_at=RUN_A))
        self.assertEqual(self.receipts.claimed, ["c1:%s:create_support_ticket:k1" % RUN_A])

    def test_a_run_given_late_scopes_it_the_same_way(self):
        self.raise_ticket(context(late={"started_at": lambda: RUN_A}))
        self.assertEqual(self.receipts.claimed, ["c1:%s:create_support_ticket:k1" % RUN_A])

    def test_a_context_without_a_run_keeps_todays_key(self):
        # Verify-first's calls, the playground's and the smoke script's carry no run.
        self.raise_ticket(context())
        self.assertEqual(self.receipts.claimed, ["c1:create_support_ticket:k1"])

    def test_a_retry_in_the_same_run_returns_the_first_ticket(self):
        first = self.raise_ticket(context(started_at=RUN_A))
        again = self.raise_ticket(context(started_at=RUN_A))
        self.assertEqual(again, first)
        self.assertEqual(len(self.tickets.tickets), 1)

    def test_the_same_key_in_a_new_run_raises_a_new_ticket(self):
        first = self.raise_ticket(context(started_at=RUN_A))
        second = self.raise_ticket(context(started_at=RUN_B))
        self.assertNotEqual(first["data"]["ticket_id"], second["data"]["ticket_id"])
        self.assertEqual(len(self.tickets.tickets), 2)

    def test_a_read_tool_claims_nothing(self):
        self.registry.call(LOOKUP_WARRANTY_RECORD, {}, context(started_at=RUN_A))
        self.assertEqual(self.receipts.claimed, [])


class RunScopedReceiptErasureTests(unittest.TestCase):
    def test_erasing_a_conversation_still_removes_its_run_scoped_receipts(self):
        """The erasure pattern is `^<id>:`, and a run-scoped key still starts
        with it. Erasure itself is not changed by this plan."""
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        receipts = MongoIdempotencyStore(db)
        receipts.put("c1:%s:create_support_ticket:k1" % RUN_A, ok({"ticket_id": "EM-00001"}))
        receipts.put("c10:%s:create_support_ticket:k1" % RUN_A, ok({"ticket_id": "EM-00002"}))
        counts = MongoConversationStore(db).delete_conversation("c1")
        self.assertEqual(counts["idempotency_keys"], 1)
        self.assertEqual([d["_id"] for d in db["idempotency_keys"].find()],
                         ["c10:%s:create_support_ticket:k1" % RUN_A])


def probe_registry(seen):
    registry = build_registry(today=TODAY)

    @registry.register("probe", "Shows what the platform injected.", parameters={},
                       optional_injects=("persona", "started_at"))
    def probe(persona=None, started_at=None):
        seen.append((persona, started_at))
        return ok({"seen": True})

    return registry


PROBE_AGENT = AgentDefinition(
    name="probe_agent", tool_names=("probe",),
    build_system_prompt=lambda message, resolved, context="": "Test agent.", one_step=False,
)


class AgentContextTests(unittest.TestCase):
    def run_probe(self, persona, channel, facts=None):
        seen = []
        agent = Agent(PROBE_AGENT, probe_registry(seen), ScriptedClaude([call_tool("probe", {}, "toolu_1"), say("ok")]),
                      EventLog(path=None), Settings(log_path="", log_to_stdout=False))
        message = InboundMessage(conversation_id="c1", persona=persona, identity=Identity(), channel=channel,
                                 message_text="hello")
        agent.run(message, ResolvedIdentity(persona=persona, method="test"), [], facts=facts)
        return seen

    def test_a_customer_agents_tools_are_told_the_persona(self):
        self.assertEqual(self.run_probe("customer", "website_chat"), [("customer", None)])

    def test_a_dealer_agents_tools_are_told_theirs(self):
        self.assertEqual(self.run_probe("dealer", "dealer_app"), [("dealer", None)])

    def test_the_run_comes_from_the_facts(self):
        self.assertEqual(self.run_probe("customer", "website_chat", facts={"started_at": lambda: RUN_A}),
                         [("customer", RUN_A)])


class RuntimeContextTests(unittest.TestCase):
    def test_an_agent_turn_gives_tools_the_persona_and_this_run(self):
        runtime, adapter, _ = make_runtime([call_tool("probe", {}, "toolu_1"), say("ok")])
        seen = []

        @runtime.registry.register("probe", "test", parameters={}, optional_injects=("persona", "started_at"))
        def probe(persona=None, started_at=None):
            seen.append((persona, started_at))
            return ok({"seen": True})

        agent = runtime.agents[BATTERY_SUPPORT]
        agent.definition = replace(agent.definition, tool_names=tuple(agent.definition.tool_names) + ("probe",))
        runtime.conversations.get("conv-1").route_to(BATTERY_SUPPORT)
        send_web(runtime, adapter, "here")
        self.assertEqual(seen, [("customer", runtime.conversations.peek("conv-1").started_at)])

    def test_the_narrow_paths_prefetch_carries_them_too(self):
        runtime, adapter, *_ = build_jev([JevDecision(answers=NARROW_WITH_WARRANTY)],
                                         narrow=[say("Try another wall socket first.")])
        calls = spy_on(runtime.registry)
        send_jev(runtime, adapter, "my battery won't charge, is it under warranty")
        # The last lookup is the prefetch: identity resolution's own comes first.
        prefetch = [ctx for name, ctx in calls if name == LOOKUP_WARRANTY_RECORD][-1]
        self.assertEqual(prefetch.persona, "customer")
        self.assertEqual(prefetch.value_for("started_at"), runtime.conversations.peek("conv-1").started_at)

    def test_the_safety_ticket_carries_them_and_its_receipt_is_scoped_by_the_run(self):
        receipts = RecordingReceipts()
        runtime = runtime_on(InMemoryConversationStore(), [], registry=build_registry(today=TODAY, idempotency=receipts))
        calls = spy_on(runtime.registry)
        reply = send_text(runtime, "my battery is swollen")
        started = runtime.conversations.peek("conv-1").started_at
        [ctx] = [c for name, c in calls if name == CREATE_SUPPORT_TICKET]
        self.assertEqual((ctx.persona, ctx.value_for("started_at")), ("customer", started))
        self.assertEqual(receipts.claimed,
                         ["conv-1:%s:create_support_ticket:safety:conv-1:%s" % (started, started)])
        self.assertTrue(reply.ticket_id)
        self.assertEqual(runtime.llm.requests, [], "the safety branch never calls the model")


class NewPersonNewTicketTests(unittest.TestCase):
    """Review Focus: a second person on the same browser after restart_for,
    with a model that reuses its idempotency key."""

    def test_a_key_reused_after_restart_for_raises_the_second_person_a_new_ticket(self):
        now = [0.0]
        chat = Chat(replies=[
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"), say("I've raised a ticket for you."),
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_2"), say("I've raised a ticket for you."),
        ], clock=lambda: now[0])
        chat.verify()
        chat.state().evidence_seen = True  # a photo earlier: a fault ticket may be raised
        first = chat.say("2")
        first_run = chat.state().started_at

        now[0] += 12 * 60 * 60 + 1  # the first person's session expires
        chat.say("hello again")
        chat.say(ONE_BIKE[3:])
        chat.say(chat.code())
        self.assertNotEqual(chat.state().started_at, first_run, "restart_for began a new run")
        chat.state().evidence_seen = True
        chat.say("yes")
        second = chat.say("battery dead")

        self.assertEqual(first.ticket_id, "EM-00001")
        self.assertEqual(second.ticket_id, "EM-00002")
        tickets = chat.registry.tickets.tickets
        self.assertEqual((tickets["EM-00001"]["phone"], tickets["EM-00002"]["phone"]), (RIDER, ONE_BIKE))
        self.assertNotIn("EM-00001", second.text)

    def test_verify_firsts_calls_carry_no_run_and_keep_todays_key(self):
        chat = Chat()
        calls = spy_on(chat.registry)
        chat.say("my battery isn't charging")
        chat.say(RIDER[3:])
        [ctx] = [c for name, c in calls if name == REQUEST_IDENTITY_VERIFICATION]
        self.assertIsNone(ctx.value_for("started_at"))
        self.assertIsNone(ctx.persona)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_run_scoped_receipts
```

What you should see: `ToolContext.__init__() got an unexpected keyword argument 'started_at'` (TypeError) in the `ToolContextTests` and `ReceiptKeyTests` that pass a run directly. `'ToolContext' object has no attribute 'persona'` (AttributeError) in `test_persona_and_run_default_to_unknown`, the runtime context tests and the verify-first test. The late-run key test fails with `['c1:create_support_ticket:k1'] != ['c1:2026-10-05T09:00:00.000000+00:00:create_support_ticket:k1']`. The agent probes get `None` for the persona. The restart test fails with `'EM-00001' != 'EM-00002'`: the second person is handed the first person's ticket. `test_erasing_a_conversation_still_removes_its_run_scoped_receipts` and `test_a_read_tool_claims_nothing` already pass. They guard behaviour this task must not break.

- [ ] **Step 3: Implement**

`src/emotorad_ai/tools/registry.py`. Replace this block:

```python
2. Write tools require an idempotency key; a retried call returns the first
   result instead of creating a second ticket/booking.
```

with:

```python
2. Write tools require an idempotency key; a retried call in the same run of
   the conversation returns the first result instead of creating a second
   ticket/booking.
```

Replace this block:

```python
    phone: Optional[str] = None
    cluster_id: Optional[str] = None
    customer_id: Optional[str] = None
    dealer_id: Optional[str] = None
    # Facts that can only be known once the turn is under way. Identity is not
```

with:

```python
    phone: Optional[str] = None
    cluster_id: Optional[str] = None
    customer_id: Optional[str] = None
    dealer_id: Optional[str] = None
    # Who the conversation is with ("customer", "dealer", ...), from identity
    # resolution. The ticket seam sends customer tickets to Zoho Desk and
    # every other persona's to the mock, so a dealer's report never becomes a
    # customer ticket. None for a caller that resolved nobody.
    persona: Optional[str] = None
    # When this run of the conversation began (ConversationState.started_at).
    # One conversation id can hold several runs, a new person's after
    # restart_for among them: write receipts and tickets are scoped by it.
    # None for a caller with no run (verify-first, the playground).
    started_at: Optional[str] = None
    # Facts that can only be known once the turn is under way. Identity is not
```

Replace this block:

```python
        scoped_key = None
        if spec.write:
            key = arguments.get("idempotency_key")
            if not key:
                return err("missing_idempotency_key", "%s requires an idempotency_key." % name)
            scoped_key = "%s:%s:%s" % (context.conversation_id, name, key)
```

with:

```python
        scoped_key = None
        if spec.write:
            key = arguments.get("idempotency_key")
            if not key:
                return err("missing_idempotency_key", "%s requires an idempotency_key." % name)
            # Scoped by run when the context knows it. A key the model reuses
            # in a new run, a new person's after restart_for among them, is a
            # new write, never the first run's result handed back. A call with
            # no run (verify-first, the playground) keeps the key as before.
            started_at = context.value_for("started_at")
            if started_at is not None:
                scoped_key = "%s:%s:%s:%s" % (context.conversation_id, started_at, name, key)
            else:
                scoped_key = "%s:%s:%s" % (context.conversation_id, name, key)
```

`src/emotorad_ai/agents/base.py`. Replace this block:

```python
        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
            late=self._late_facts(message.conversation_id, facts),
        )
```

with:

```python
        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
            # Who this is, for the ticket seam. The run arrives in the facts
            # (started_at), read when the tool runs.
            persona=resolved.persona,
            late=self._late_facts(message.conversation_id, facts),
        )
```

`src/emotorad_ai/runtime.py`. In `_node_narrow`, replace this block:

```python
        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
        )
        prefetched: List[Dict[str, Any]] = []
```

with:

```python
        context = ToolContext(
            conversation_id=message.conversation_id,
            phone=resolved.identity.phone,
            cluster_id=resolved.cluster_id,
            persona=resolved.persona,
            started_at=state.started_at,
        )
        prefetched: List[Dict[str, Any]] = []
```

In `_run`, replace this block:

```python
        turn = agent.run(
            message, agent_view, state.history, context,
            # Conversation facts the order tool decides on. Lambdas, because
            # evidence_seen can flip during this very turn when a photo arrives
            # with the message that triggers the order.
            facts={
                "evidence_seen": lambda: state.evidence_seen,
```

with:

```python
        turn = agent.run(
            message, agent_view, state.history, context,
            # Conversation facts the order tool decides on. Lambdas, because
            # evidence_seen can flip during this very turn when a photo arrives
            # with the message that triggers the order.
            facts={
                # This run of the conversation. Write receipts and tickets are
                # scoped by it (tools/registry.py), read when the tool runs.
                "started_at": lambda: state.started_at,
                "evidence_seen": lambda: state.evidence_seen,
```

In `_raise_safety_ticket`, replace this block:

```python
            ToolContext(
                conversation_id=message.conversation_id,
                phone=resolved.identity.phone,
                cluster_id=resolved.cluster_id,
                late=late,
            ),
            run_without_idempotency=True,
```

with:

```python
            ToolContext(
                conversation_id=message.conversation_id,
                phone=resolved.identity.phone,
                cluster_id=resolved.cluster_id,
                persona=resolved.persona,
                started_at=state.started_at,
                late=late,
            ),
            run_without_idempotency=True,
```

`src/emotorad_ai/stores/mongo.py` (docstring only). Replace this block:

```python
def _receipts_of(conversation_id: str) -> Dict[str, Any]:
    """Idempotency receipts are keyed `<conversation id>:<tool>:<key>` and can
    hold what a tool returned about the person (a booking's customer id)."""
```

with:

```python
def _receipts_of(conversation_id: str) -> Dict[str, Any]:
    """Idempotency receipts are keyed `<conversation id>:<run start>:<tool>:<key>`,
    or `<conversation id>:<tool>:<key>` for a call with no run, and can hold
    what a tool returned about the person (a booking's customer id). Both
    begin with the conversation id and a colon, which is what this matches."""
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_run_scoped_receipts
```

What you should see: 18 tests, OK.

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

What you should see: 2,183 tests (the 2,165 baseline plus 18), with only the known environmental failure, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test should change. These tests pin the old key: `tests.test_audit_persistence.KeyScopeTests`, `tests.test_audit_persistence.ErasureTests`, `tests.test_idempotency_claims` and `tests.test_store_review_fixes`. They all build contexts with no run, or write receipts directly, so they still pass unchanged. The safety key `safety:<cid>:<started_at>` stays the same within a run, so the one-safety-ticket-per-run tests are unchanged too.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/tools/registry.py src/emotorad_ai/agents/base.py src/emotorad_ai/runtime.py src/emotorad_ai/stores/mongo.py tests/test_run_scoped_receipts.py
git commit -m "feat: scope write receipts by run and tell tools the persona" -m "ToolContext gains persona and started_at. The receipt key includes the run when the context knows it, so a key the model reuses after restart_for raises the new person their own ticket. Verify-first's calls carry no run and keep the old key. The agent loop, the narrow prefetch and the safety ticket fill both." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Build the ticket store: clock, kinds, record, both stores, indexes and `Stores.tickets`

**Files:**
- Create: `src/emotorad_ai/tickets/__init__.py`
- Create: `src/emotorad_ai/tickets/clock.py`
- Create: `src/emotorad_ai/tickets/kinds.py`
- Create: `src/emotorad_ai/tickets/record.py`
- Create: `src/emotorad_ai/tickets/store.py`
- Modify: `src/emotorad_ai/stores/mongo.py:5-9` (docstring), `:20-44` (imports), `:58-59` (collection names), `:94-96` (`INDEXES`), end of file (`MongoTicketStore`)
- Modify: `src/emotorad_ai/wiring.py:41-65` (`Stores.tickets`, `build_stores`)
- Modify: `scripts/mongo_setup.py:9-12, 22-26` (`PERMANENT` gains `tickets` and `counters`)
- Modify: `tests/test_mongo_store.py:133-134, 148-151`
- Modify: `tests/test_mongo_scripts.py:100-102`
- Test: `tests/test_ticket_kinds.py` (new), `tests/ticket_store_contract.py` (new), `tests/test_ticket_store.py` (new)

**Interfaces:**
- Consumes: `conversation.StoreUnavailable`. The `_guard` pattern of `stores/mongo.py`.
- Produces:
  - `tickets.clock`: `now_iso()`, `iso(dt)`, `parse(text)`, `plus(at, seconds)`.
  - `tickets.kinds`: `KINDS`, the six kind constants `SUPPORT`, `SAFETY`, `HANDOVER`, `LOCKOUT`, `INTAKE`, `WARRANTY_PROOF`, plus `SAFETY_CATEGORY`, `FIRST_DESK_NUMBER`, `URGENT_LATE_SECONDS = 600`, `STUCK_SECONDS = 86400`, `is_urgent`, `is_desk_reference`, `desk_reference(number)` and `subject_label`.
  - `tickets.record`: `new_record(...)` with the exact skeleton signature, the states `WAITING`, `SENT`, `STUCK` and `GONE`, the listing label `HELD`, `OUTSTANDING = (WAITING, STUCK)`, `MODES`, `IDENTITIES` and `FIRST_ATTEMPT_SECONDS = 120`.
  - `tickets.store`: `InMemoryTicketStore`, `listing_row(record, mode)` and `age_seconds(since, now)`.
  - `stores.mongo`: `TICKETS`, `COUNTERS`, `TICKET_COUNTER` and `MongoTicketStore(db)`.
  - Both stores implement every `TicketStore` method in the skeleton. Each `listing` row is `{"reference", "zoho_number", "state", "mode", "last_four"}`. `counts` returns `{"waiting", "stuck", "held", "oldest_due_seconds"}`.
  - `wiring.Stores.tickets` holds an `InMemoryTicketStore` for `memory` and a `MongoTicketStore` for `mongodb`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ticket_kinds.py`:

```python
"""Ticket kinds, urgency, subject labels, the two reference shapes, and the
clock the ticket store keeps time with (plan Task 2)."""

import unittest
from datetime import datetime, timedelta, timezone

from emotorad_ai.tickets import kinds
from emotorad_ai.tickets.clock import iso, now_iso, parse, plus
from emotorad_ai.tickets.kinds import (
    FIRST_DESK_NUMBER, KINDS, desk_reference, is_desk_reference, is_urgent, subject_label,
)

T0 = "2026-10-05T10:00:00.000000+00:00"
IST = timezone(timedelta(hours=5, minutes=30))


class KindTests(unittest.TestCase):
    def test_the_six_kinds(self):
        self.assertEqual(KINDS, ("support", "safety", "handover", "lockout", "intake", "warranty_proof"))

    def test_the_safety_kind_or_a_battery_safety_category_is_urgent(self):
        self.assertTrue(is_urgent("safety", None))
        self.assertTrue(is_urgent("safety", "battery_charging"))
        self.assertTrue(is_urgent("support", "battery_safety"))
        self.assertFalse(is_urgent("support", "battery_charging"))
        for kind in ("handover", "lockout", "intake", "warranty_proof"):
            self.assertFalse(is_urgent(kind, None), kind)

    def test_subject_labels(self):
        self.assertEqual([subject_label(kind, None) for kind in KINDS[1:]],
                         ["SAFETY", "Asked for a person", "Could not verify", "Unverified customer",
                          "Late warranty registration"])
        self.assertEqual(subject_label("support", "battery_charging"), "Battery: charging")
        self.assertEqual(subject_label("support", "battery_range"), "Battery: range")
        self.assertEqual(subject_label("support", "battery_power"), "Battery: power")
        self.assertEqual(subject_label("support", "battery_safety"), "Battery: safety")
        self.assertEqual(subject_label("support", "other"), "Other")
        self.assertEqual(subject_label("support", None), "Support")
        self.assertEqual(subject_label("support", "late_warranty_registration"), "Late warranty registration")
        self.assertEqual(subject_label("safety", "battery_charging"), "SAFETY")  # the kind decides
        with self.assertRaises(ValueError):
            subject_label("complaint", None)

    def test_the_thresholds(self):
        self.assertEqual((kinds.URGENT_LATE_SECONDS, kinds.STUCK_SECONDS), (600, 86400))


class ReferenceTests(unittest.TestCase):
    def test_desk_references_start_at_seven_digits(self):
        self.assertEqual(FIRST_DESK_NUMBER, 1000001)
        self.assertEqual(desk_reference(FIRST_DESK_NUMBER), "EM-1000001")

    def test_only_an_em_reference_of_seven_or_more_digits_is_desks(self):
        for desk in ("EM-1000001", "EM-1000002", "EM-12345678"):
            self.assertTrue(is_desk_reference(desk), desk)
        for other in ("EM-00001", "EM-99999", "EM-123456", "BK-1000001", "RO-1000001", "EM-1000001 ",
                      " EM-1000001", "EM-1000001x", "em-1000001", "", None):
            self.assertFalse(is_desk_reference(other), other)

    def test_devanagari_digits_are_not_a_desk_reference(self):
        # Python's \d matches these; the pattern uses [0-9] so they never pass.
        self.assertFalse(is_desk_reference("EM-१०००००१"))


class ClockTests(unittest.TestCase):
    def test_now_is_utc_with_microseconds(self):
        self.assertRegex(now_iso(), r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$")

    def test_a_whole_second_still_carries_microseconds(self):
        self.assertEqual(iso(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)), T0)

    def test_another_zone_is_written_as_utc_and_a_naive_time_is_taken_as_utc(self):
        self.assertEqual(iso(datetime(2026, 10, 5, 15, 30, tzinfo=IST)), T0)
        self.assertEqual(iso(datetime(2026, 10, 5, 10, 0)), T0)

    def test_plus_moves_either_way_in_the_same_format(self):
        self.assertEqual(plus(T0, 120), "2026-10-05T10:02:00.000000+00:00")
        self.assertEqual(plus(T0, -600), "2026-10-05T09:50:00.000000+00:00")
        self.assertEqual(plus(T0, 0.5), "2026-10-05T10:00:00.500000+00:00")

    def test_parse_reads_the_stored_form_back(self):
        self.assertEqual(parse(T0), datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(parse("2026-10-05T10:00:00"), datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))

    def test_string_order_is_time_order_across_midnight(self):
        before = "2026-10-05T23:59:59.999999+00:00"
        after = plus(before, 0.000001)
        self.assertEqual(after, "2026-10-06T00:00:00.000000+00:00")
        self.assertLess(before, after)


if __name__ == "__main__":
    unittest.main()
```

Create `tests/ticket_store_contract.py`:

```python
"""One set of behaviours every ticket store must have (plan Task 2).

Mixed into a TestCase per implementation (tests/test_ticket_store.py), so the
in-memory store and MongoDB are held to the same contract. MongoDB runs on
mongomock one call after another: its atomic updates are exercised in
sequence, never by threads.
"""

from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.tickets.clock import plus
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.record import new_record

T0 = "2026-10-05T10:00:00.000000+00:00"
RUN = "2026-10-05T09:58:00.000000+00:00"
LATER_RUN = "2026-10-05T11:00:00.000000+00:00"
PHONE = "+919999999999"
OTHER_PHONE = "+919999999998"
FRAME = "EMXP2026001234"  # from the fixtures


def at(seconds):
    return plus(T0, seconds)


def zoho(**fields):
    base = {"contact_id": None, "ticket_id": None, "ticket_number": None, "web_url": None,
            "comment_ids": [], "attachment_ids": []}
    base.update(fields)
    return base


def record_for(store, kind="support", mode="test", identity="verified", phone=PHONE, category="battery_charging",
               conversation_id="c1", started_at=RUN, created_at=T0, source_key=None, **stored):
    """A new record, inserted. `stored` overrides fields as written, for the
    states only the worker reaches (sent, stuck, gone)."""
    reference = store.next_reference()
    record = new_record(
        reference=reference, chat_reference="stage:" + reference,
        source_key=source_key or "%s:%s:create_support_ticket:%s" % (conversation_id, started_at, reference),
        mode=mode, kind=kind, conversation_id=conversation_id, started_at=started_at, cluster_id="cluster-1",
        channel="website_chat", phone=phone, identity=identity, category=category, ai_severity="normal",
        summary="LED stays off.", claims={},
        bike={"model": "EMX Plus", "frame_number": FRAME, "frame_number_source": "record"},
        coverage="computed", customer_name=None, created_at=created_at,
    )
    record.update(stored)
    return store.insert(record)


class TicketStoreContract:
    def make_store(self):
        raise NotImplementedError

    # -- references and inserts -------------------------------------------------

    def test_references_count_up_from_the_first_desk_number(self):
        store = self.make_store()
        first, second = store.next_reference(), store.next_reference()
        self.assertEqual((first, second), ("EM-1000001", "EM-1000002"))
        self.assertTrue(is_desk_reference(first))

    def test_an_inserted_record_reads_back_by_reference_and_source_key(self):
        store = self.make_store()
        record = record_for(store)
        self.assertEqual(record["_id"], "EM-1000001")
        self.assertEqual(store.get("EM-1000001"), record)
        self.assertEqual(store.by_source_key(record["source_key"]), record)

    def test_a_second_insert_with_the_same_source_key_returns_the_first(self):
        store = self.make_store()
        key = "c1:%s:create_support_ticket:k1" % RUN
        first = record_for(store, source_key=key)
        again = record_for(store, source_key=key, kind="safety")
        self.assertEqual(again, first)
        self.assertIsNone(store.get("EM-1000002"))
        self.assertEqual([row["reference"] for row in store.listing("test")], ["EM-1000001"])

    def test_a_reference_used_twice_is_refused(self):
        store = self.make_store()
        first = record_for(store)
        with self.assertRaises(StoreUnavailable):
            store.insert(dict(first, source_key="c1:%s:create_support_ticket:other" % RUN))
        self.assertEqual(store.get(first["_id"]), first)

    def test_an_unknown_reference_or_key_is_none(self):
        store = self.make_store()
        self.assertIsNone(store.get("EM-1000001"))
        self.assertIsNone(store.by_source_key("c1:%s:create_support_ticket:k1" % RUN))

    # -- taking a record ------------------------------------------------------------

    def test_a_new_record_waits_two_minutes_for_its_first_attempt(self):
        store = self.make_store()
        record = record_for(store)
        self.assertEqual((record["state"], record["due_since"], record["next_attempt_at"]), ("waiting", T0, at(120)))
        self.assertIsNone(store.take_due(at(119), "test", 300, "t1"))
        self.assertEqual(store.take_due(at(120), "test", 300, "t1")["_id"], record["_id"])

    def test_taking_a_record_leases_it_and_reads_its_wake_count(self):
        store = self.make_store()
        record_for(store)
        taken = store.take_due(at(120), "test", 300, "t1")
        self.assertEqual((taken["lease_token"], taken["lease_until"], taken["wake"]), ("t1", at(420), 0))
        self.assertEqual(store.get(taken["_id"])["lease_token"], "t1")

    def test_a_leased_record_is_taken_again_only_once_its_lease_has_passed(self):
        store = self.make_store()
        record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertIsNone(store.take_due(at(121), "test", 300, "t2"))
        self.assertIsNone(store.take_due(at(420), "test", 300, "t2"))  # the lease ends at 420, exclusive
        self.assertEqual(store.take_due(at(421), "test", 300, "t2")["lease_token"], "t2")

    def test_urgent_records_come_first_then_the_oldest_due(self):
        store = self.make_store()
        late = record_for(store, created_at=at(0))  # due at 120
        early = record_for(store, created_at=at(-60))  # due at 60
        urgent = record_for(store, kind="safety", category="battery_safety", created_at=at(30))  # due at 150
        order = [store.take_due(at(200), "test", 300, "t%d" % i)["_id"] for i in range(3)]
        self.assertEqual(order, [urgent["_id"], early["_id"], late["_id"]])
        self.assertIsNone(store.take_due(at(200), "test", 300, "t4"))

    def test_only_records_of_the_current_mode_are_taken(self):
        store = self.make_store()
        live = record_for(store, mode="live")
        self.assertIsNone(store.take_due(at(200), "test", 300, "t1"))
        self.assertEqual(store.take_due(at(200), "live", 300, "t1")["_id"], live["_id"])

    def test_sent_and_gone_records_are_never_taken_and_stuck_ones_are(self):
        store = self.make_store()
        record_for(store, state="sent")
        record_for(store, state="gone")
        stuck = record_for(store, state="stuck")
        self.assertEqual(store.take_due(at(200), "test", 300, "t1")["_id"], stuck["_id"])
        self.assertIsNone(store.take_due(at(200), "test", 300, "t2"))

    # -- saving under the lease -------------------------------------------------------

    def test_a_save_under_the_lease_sets_adds_and_pushes(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertTrue(store.save(record["_id"], "t1", {"zoho.ticket_id": "z-1", "intent": None},
                                   add_to_set={"posted_turns": [1, 2], "zoho.comment_ids": ["c-1"]}))
        self.assertTrue(store.save(record["_id"], "t1", {}, add_to_set={"posted_turns": [2, 3]},
                                   push={"zoho.comment_ids": ["c-2"]}))
        saved = store.get(record["_id"])
        self.assertEqual(saved["zoho"]["ticket_id"], "z-1")
        self.assertIsNone(saved["zoho"]["contact_id"])  # a dotted set leaves its neighbours alone
        self.assertEqual(saved["posted_turns"], [1, 2, 3])
        self.assertEqual(saved["zoho"]["comment_ids"], ["c-1", "c-2"])

    def test_a_save_without_the_lease_changes_nothing(self):
        store = self.make_store()
        record = record_for(store)
        self.assertFalse(store.save(record["_id"], None, {"state": "sent"}))  # never leased
        store.take_due(at(120), "test", 300, "t1")
        self.assertFalse(store.save(record["_id"], "t2", {"state": "sent"}))
        self.assertFalse(store.save(record["_id"], None, {"state": "sent"}))
        self.assertEqual(store.get(record["_id"])["state"], "waiting")

    def test_a_save_never_creates_a_record(self):
        store = self.make_store()
        self.assertFalse(store.save("EM-1000009", "t1", {"state": "sent"}, add_to_set={"posted_turns": [1]}))
        self.assertIsNone(store.get("EM-1000009"))

    def test_a_wake_during_the_lease_is_not_lost(self):
        store = self.make_store()
        record = record_for(store)
        taken = store.take_due(at(120), "test", 300, "t1")
        self.assertTrue(store.wake(record["_id"], at(130)))
        self.assertFalse(store.save(record["_id"], "t1", {"state": "sent"}, expect_wake=taken["wake"]))
        self.assertEqual(store.get(record["_id"])["state"], "waiting")
        self.assertTrue(store.save(record["_id"], "t1", {"state": "sent"}, expect_wake=taken["wake"] + 1))

    def test_renewing_a_lease_needs_its_token(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertFalse(store.renew_lease(record["_id"], "t2", at(900)))
        self.assertTrue(store.renew_lease(record["_id"], "t1", at(900)))
        self.assertEqual(store.get(record["_id"])["lease_until"], at(900))
        self.assertIsNone(store.take_due(at(600), "test", 300, "t2"))

    # -- new content ---------------------------------------------------------------------

    def test_a_wake_makes_a_waiting_record_due_now(self):
        store = self.make_store()
        record = record_for(store)
        self.assertTrue(store.wake(record["_id"], at(5)))
        woken = store.get(record["_id"])
        self.assertEqual((woken["wake"], woken["next_attempt_at"], woken["due_since"]), (1, at(5), T0))
        store.wake(record["_id"], at(9))  # a later wake never makes it due later
        self.assertEqual(store.get(record["_id"])["next_attempt_at"], at(5))

    def test_a_wake_reopens_a_sent_record_from_now(self):
        store = self.make_store()
        record = record_for(store, state="sent")
        self.assertTrue(store.wake(record["_id"], at(900)))
        woken = store.get(record["_id"])
        self.assertEqual((woken["state"], woken["due_since"], woken["next_attempt_at"], woken["wake"]),
                         ("waiting", at(900), at(900), 1))

    def test_a_gone_or_unknown_record_takes_no_wake(self):
        store = self.make_store()
        record = record_for(store, state="gone")
        self.assertFalse(store.wake(record["_id"], at(900)))
        self.assertEqual(store.get(record["_id"]), record)
        self.assertFalse(store.wake("EM-1000009", at(900)))

    def test_a_note_is_kept_and_wakes_the_record(self):
        store = self.make_store()
        record = record_for(store, state="sent")
        self.assertTrue(store.add_note(record["_id"], "Customer asked for a person at 14:02", at(900)))
        noted = store.get(record["_id"])
        self.assertEqual(noted["notes"], [{"text": "Customer asked for a person at 14:02", "at": at(900)}])
        self.assertEqual((noted["state"], noted["wake"], noted["due_since"]), ("waiting", 1, at(900)))

    def test_a_gone_or_unknown_record_takes_no_note(self):
        store = self.make_store()
        record = record_for(store, state="gone")
        self.assertFalse(store.add_note(record["_id"], "Customer asked for a person", at(900)))
        self.assertEqual(store.get(record["_id"])["notes"], [])
        self.assertFalse(store.add_note("EM-1000009", "Customer asked for a person", at(900)))

    def test_close_runs_ends_only_the_earlier_open_runs_of_that_conversation(self):
        store = self.make_store()
        earlier = record_for(store, started_at=RUN)
        current = record_for(store, started_at=LATER_RUN)
        other = record_for(store, conversation_id="c2", started_at=RUN)
        self.assertEqual(store.close_runs("c1", LATER_RUN), 1)
        self.assertEqual(store.get(earlier["_id"])["ended_at"], LATER_RUN)
        self.assertIsNone(store.get(current["_id"])["ended_at"])
        self.assertIsNone(store.get(other["_id"])["ended_at"])
        self.assertEqual(store.close_runs("c1", "2026-10-05T12:00:00.000000+00:00"), 1)  # only the open one
        self.assertEqual(store.get(earlier["_id"])["ended_at"], LATER_RUN)  # an end is never moved

    # -- reporting --------------------------------------------------------------------------

    def test_an_urgent_record_is_overdue_ten_minutes_after_it_fell_due(self):
        store = self.make_store()
        urgent = record_for(store, kind="safety", category="battery_safety")
        self.assertEqual(store.overdue(at(599), "test"), [])
        self.assertEqual([r["_id"] for r in store.overdue(at(600), "test")], [urgent["_id"]])

    def test_any_other_record_is_overdue_after_a_day(self):
        store = self.make_store()
        normal = record_for(store)
        self.assertEqual(store.overdue(at(86399), "test"), [])
        self.assertEqual([r["_id"] for r in store.overdue(at(86400), "test")], [normal["_id"]])

    def test_overdue_counts_from_due_since_however_many_turns_follow(self):
        store = self.make_store()
        urgent = record_for(store, kind="safety", category="battery_safety")
        for minute in range(1, 11):
            store.wake(urgent["_id"], at(60 * minute))  # a new turn every minute
        self.assertEqual([r["_id"] for r in store.overdue(at(600), "test")], [urgent["_id"]])

    def test_overdue_ignores_sent_gone_and_other_mode_records(self):
        store = self.make_store()
        for fields in ({"state": "sent"}, {"state": "gone"}, {"mode": "live"}):
            record_for(store, kind="safety", category="battery_safety", **fields)
        self.assertEqual(store.overdue(at(86400), "test"), [])

    def test_counts_waiting_stuck_held_and_the_age_of_the_oldest(self):
        store = self.make_store()
        record_for(store)  # waiting since T0
        record_for(store, state="stuck", due_since=at(-50))
        record_for(store, mode="live")  # the other mode: held
        record_for(store, state="sent")
        record_for(store, state="gone")
        self.assertEqual(store.counts("test", at(100)),
                         {"waiting": 1, "stuck": 1, "held": 1, "oldest_due_seconds": 150})
        self.assertEqual(store.counts("live", at(100)),
                         {"waiting": 1, "stuck": 0, "held": 2, "oldest_due_seconds": 100})

    def test_counts_on_an_empty_store(self):
        self.assertEqual(self.make_store().counts("test", T0),
                         {"waiting": 0, "stuck": 0, "held": 0, "oldest_due_seconds": None})

    def test_unverified_since_counts_only_non_urgent_unverified_records(self):
        store = self.make_store()
        record_for(store, kind="intake", category="intake_unverified", identity="unverified")
        record_for(store, kind="handover", category=None, identity="unverified", phone=OTHER_PHONE)
        record_for(store, kind="safety", category="battery_safety", identity="unverified")  # urgent: never capped
        record_for(store, identity="verified")
        record_for(store, kind="intake", category="intake_unverified", identity="unverified", created_at=at(-1))
        self.assertEqual(store.unverified_since(T0), 2)
        self.assertEqual(store.unverified_since(T0, phone=PHONE), 1)
        self.assertEqual(store.unverified_since(at(-1)), 3)

    def test_contact_for_uses_only_live_verified_records(self):
        store = self.make_store()
        record_for(store, mode="test", zoho=zoho(contact_id="test-contact"))
        record_for(store, mode="live", identity="unverified", zoho=zoho(contact_id="unverified-contact"))
        record_for(store, mode="live", zoho=zoho())  # not sent yet: no contact
        self.assertIsNone(store.contact_for(PHONE))
        record_for(store, mode="live", zoho=zoho(contact_id="z-old"), created_at=at(-100))
        record_for(store, mode="live", zoho=zoho(contact_id="z-new"), created_at=at(-10))
        self.assertEqual(store.contact_for(PHONE), "z-new")
        self.assertIsNone(store.contact_for(OTHER_PHONE))

    def test_the_listing_shows_outstanding_records_with_the_last_four_digits_only(self):
        store = self.make_store()
        waiting = record_for(store, zoho=zoho(ticket_number="1042"))
        stuck = record_for(store, state="stuck", created_at=at(10))
        held = record_for(store, mode="live", created_at=at(20))
        record_for(store, state="sent")
        record_for(store, state="gone")
        rows = store.listing("test")
        self.assertEqual(rows, [
            {"reference": waiting["_id"], "zoho_number": "1042", "state": "waiting", "mode": "test",
             "last_four": "9999"},
            {"reference": stuck["_id"], "zoho_number": None, "state": "stuck", "mode": "test", "last_four": "9999"},
            {"reference": held["_id"], "zoho_number": None, "state": "held", "mode": "live", "last_four": "9999"},
        ])
        self.assertNotIn(PHONE, repr(rows))

    def test_the_store_keeps_source_keys_unique(self):
        self.assertTrue(self.make_store().has_unique_source_key())
```

Create `tests/test_ticket_store.py`:

```python
"""The ticket record and both ticket stores (plan Task 2)."""

import threading
import unittest

import mongomock
from pymongo.errors import ServerSelectionTimeoutError

from emotorad_ai.config import Settings
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.stores.mongo import COUNTERS, INDEXES, TICKETS, MongoTicketStore, ensure_indexes
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.wiring import build_stores
from tests.ticket_store_contract import PHONE, RUN, T0, TicketStoreContract, at, record_for

RECORD_KEYS = {
    "_id", "chat_reference", "source_key", "mode", "kind", "urgent", "conversation_id", "cluster_id", "started_at",
    "ended_at", "channel", "created_at", "phone", "identity", "category", "ai_severity", "summary", "claims", "bike",
    "coverage", "customer_name", "notes", "zoho", "posted_turns", "posted_media", "posted_notes", "state",
    "due_since", "wake", "attempts", "next_attempt_at", "lease_until", "lease_token", "intent", "last_error",
}


def fields(**overrides):
    base = dict(reference="EM-1000001", chat_reference="stage:EM-1000001",
                source_key="c1:%s:create_support_ticket:k1" % RUN, mode="test", kind="support",
                conversation_id="c1", started_at=RUN, cluster_id="cluster-1", channel="whatsapp", phone=PHONE,
                identity="verified", category="battery_charging", ai_severity="normal", summary="LED stays off.",
                claims={}, bike=None, coverage="computed", customer_name=None, created_at=T0)
    base.update(overrides)
    return base


class NewRecordTests(unittest.TestCase):
    def test_the_document_has_exactly_the_spec_fields_and_starts_waiting(self):
        record = new_record(**fields())
        self.assertEqual(set(record), RECORD_KEYS)
        self.assertEqual(record["_id"], "EM-1000001")
        self.assertEqual((record["state"], record["due_since"], record["next_attempt_at"]), ("waiting", T0, at(120)))
        self.assertEqual((record["wake"], record["attempts"], record["ended_at"], record["lease_until"],
                          record["lease_token"], record["intent"], record["last_error"]),
                         (0, 0, None, None, None, None, None))
        self.assertEqual((record["notes"], record["posted_turns"], record["posted_media"], record["posted_notes"]),
                         ([], [], [], []))
        self.assertEqual(record["zoho"], {"contact_id": None, "ticket_id": None, "ticket_number": None,
                                          "web_url": None, "comment_ids": [], "attachment_ids": []})

    def test_urgency_comes_from_the_kind_and_the_category(self):
        self.assertFalse(new_record(**fields())["urgent"])
        self.assertTrue(new_record(**fields(category="battery_safety"))["urgent"])
        self.assertTrue(new_record(**fields(kind="safety", category=None))["urgent"])

    def test_the_callers_claims_and_bike_are_copied(self):
        claims, bike = {"stated_name": "Test"}, {"model": "EMX Plus", "frame_number": "EMXP2026001234"}
        record = new_record(**fields(claims=claims, bike=bike))
        claims["stated_name"], bike["model"] = "changed", "changed"
        self.assertEqual((record["claims"]["stated_name"], record["bike"]["model"]), ("Test", "EMX Plus"))

    def test_a_bad_kind_mode_identity_or_missing_key_is_refused(self):
        for bad in ({"kind": "complaint"}, {"mode": "staging"}, {"identity": "asserted"}, {"source_key": ""},
                    {"conversation_id": ""}):
            with self.subTest(bad), self.assertRaises(ValueError):
                new_record(**fields(**bad))


class InMemoryTicketStoreTests(TicketStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryTicketStore()

    def test_references_from_many_threads_are_all_different(self):
        store, seen, lock = InMemoryTicketStore(), [], threading.Lock()

        def take():
            mine = [store.next_reference() for _ in range(25)]
            with lock:
                seen.extend(mine)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(seen)), 200)

    def test_one_source_key_from_many_threads_is_one_record(self):
        store, results, lock = InMemoryTicketStore(), [], threading.Lock()
        barrier = threading.Barrier(8)
        key = "c1:%s:create_support_ticket:k1" % RUN

        def insert():
            barrier.wait()
            record = record_for(store, source_key=key)
            with lock:
                results.append(record["_id"])

        threads = [threading.Thread(target=insert) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(store.listing("test")), 1)

    def test_a_returned_record_is_a_copy(self):
        store = InMemoryTicketStore()
        record = record_for(store)
        record["notes"].append({"text": "changed outside", "at": T0})
        self.assertEqual(store.get(record["_id"])["notes"], [])


class MongoTicketStoreTests(TicketStoreContract, unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def make_store(self):
        return MongoTicketStore(self.db)

    def test_two_servers_share_one_counter_and_one_record_per_source_key(self):
        one, two = MongoTicketStore(self.db), MongoTicketStore(self.db)
        self.assertEqual([one.next_reference(), two.next_reference(), one.next_reference()],
                         ["EM-1000001", "EM-1000002", "EM-1000003"])
        key = "c1:%s:create_support_ticket:k1" % RUN
        first = record_for(one, source_key=key)
        again = record_for(two, source_key=key)
        self.assertEqual(again["_id"], first["_id"])
        self.assertEqual(self.db[TICKETS].count_documents({"source_key": key}), 1)

    def test_a_second_worker_finds_nothing_while_the_first_holds_the_lease(self):
        one, two = MongoTicketStore(self.db), MongoTicketStore(self.db)
        record_for(one)
        self.assertIsNotNone(one.take_due(at(120), "test", 300, "worker-1"))
        self.assertIsNone(two.take_due(at(121), "test", 300, "worker-2"))

    def test_a_save_after_the_record_was_removed_writes_nothing_back(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.db[TICKETS].delete_one({"_id": record["_id"]})  # removed by a person meanwhile
        self.assertFalse(store.save(record["_id"], "t1", {"state": "sent"}, add_to_set={"posted_turns": [1]}))
        self.assertEqual(self.db[TICKETS].count_documents({}), 0)

    def test_without_setup_the_unique_index_is_reported_missing(self):
        self.assertFalse(MongoTicketStore(mongomock.MongoClient()["emotorad_ai"]).has_unique_source_key())

    def test_a_source_key_index_that_is_not_unique_does_not_count(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        db[TICKETS].create_index([("source_key", 1)], name="source_key")
        self.assertFalse(MongoTicketStore(db).has_unique_source_key())

    def test_an_unreachable_cluster_is_store_unavailable(self):
        class Unreachable:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise ServerSelectionTimeoutError("no servers found")
                return fail

        store = MongoTicketStore({TICKETS: Unreachable(), COUNTERS: Unreachable()})
        for call in (store.next_reference, lambda: store.get("EM-1000001"),
                     lambda: store.take_due(T0, "test", 300, "t1"), store.has_unique_source_key):
            with self.assertRaises(StoreUnavailable):
                call()


class TicketIndexTests(unittest.TestCase):
    def test_tickets_get_their_four_indexes_and_counters_none(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        report = ensure_indexes(db)
        self.assertEqual(report[TICKETS], ["_id_", "conversation", "due", "phone", "source_key"])
        self.assertEqual(report[COUNTERS], ["_id_"])
        info = db[TICKETS].index_information()
        self.assertTrue(info["source_key"]["unique"])
        self.assertEqual(info["due"]["key"], [("state", 1), ("next_attempt_at", 1)])

    def test_neither_collection_ever_expires(self):
        for collection in (TICKETS, COUNTERS):
            for _, options in INDEXES[collection]:
                self.assertNotIn("expireAfterSeconds", options)


class StoresWiringTests(unittest.TestCase):
    def test_the_memory_stores_include_an_in_memory_ticket_store(self):
        self.assertIsInstance(build_stores(Settings(store="memory")).tickets, InMemoryTicketStore)

    def test_mongodb_puts_tickets_on_the_same_database(self):
        client, settings = mongomock.MongoClient(), Settings(store="mongodb")
        stores = build_stores(settings, client=client)
        self.assertIsInstance(stores.tickets, MongoTicketStore)
        stores.tickets.next_reference()
        self.assertEqual(client[settings.mongo_db][COUNTERS].count_documents({}), 1)


if __name__ == "__main__":
    unittest.main()
```

Modify `tests/test_mongo_store.py`. Replace this block:

```python
        self.assertEqual(set(report), {"conversations", "transcript_turns", "conversation_summaries", "idempotency_keys", "media",
                                       "conversation_origins", "erasure_requests"})
```

with:

```python
        self.assertEqual(set(report), {"conversations", "transcript_turns", "conversation_summaries", "idempotency_keys", "media",
                                       "conversation_origins", "erasure_requests", "tickets", "counters"})
```

Replace this block:

```python
    def test_the_index_table_has_no_ttl_on_the_permanent_record(self):
        for collection in ("transcript_turns", "conversation_summaries", "conversation_origins",
                           "erasure_requests"):
```

with:

```python
    def test_the_index_table_has_no_ttl_on_the_permanent_record(self):
        for collection in ("transcript_turns", "conversation_summaries", "conversation_origins",
                           "erasure_requests", "tickets", "counters"):
```

Modify `tests/test_mongo_scripts.py`. Replace this block:

```python
        # Customer media is kept permanently (decision 2026-09-29), so its
        # record is part of the permanent record and must never get a TTL.
        self.assertRegex(text, r"\bmedia\s+permanent")
```

with:

```python
        # Customer media is kept permanently (decision 2026-09-29), so its
        # record is part of the permanent record and must never get a TTL.
        self.assertRegex(text, r"\bmedia\s+permanent")
        # So are the ticket records, and the counter behind their references:
        # one that expired would hand out EM-1000001 again.
        self.assertRegex(text, r"\btickets\s+permanent")
        self.assertRegex(text, r"\bcounters\s+permanent")
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_kinds
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_store
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_mongo_store
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_mongo_scripts
```

What you should see: the first two commands fail to import with `ModuleNotFoundError: No module named 'emotorad_ai.tickets'`. `tests.test_mongo_store` fails in `IndexTests.test_every_collection_gets_exactly_its_indexes_and_only_two_expire`, because the set lacks `tickets` and `counters`, and errors in `test_the_index_table_has_no_ttl_on_the_permanent_record` with `KeyError: 'tickets'`. `tests.test_mongo_scripts.SetupScriptTests` fails on `tickets\s+permanent`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/tickets/__init__.py`:

```python
"""Customer tickets recorded for Zoho Desk (spec 2026-10-05-zoho-desk-tickets-design.md).

The record (record.py) is written in the turn through the seam (seam.py) and
sent to Zoho after the reply by the worker (zoho/worker.py).
"""
```

Create `src/emotorad_ai/tickets/clock.py`:

```python
"""Times in the ticket store.

Every time the store holds is an ISO-8601 UTC string with microseconds,
"2026-10-05T10:00:00.000000+00:00", so the store compares and sorts them as
strings, in MongoDB and in memory alike. One format, from one place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def iso(dt: datetime) -> str:
    """A datetime as the store writes it. A naive one is taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def parse(text: str) -> datetime:
    """A stored time back as an aware datetime. A naive one is taken as UTC."""
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def plus(at: str, seconds: float) -> str:
    """`at` moved by `seconds` (negative goes back), in the store's format."""
    return iso(parse(at) + timedelta(seconds=seconds))
```

Create `src/emotorad_ai/tickets/kinds.py`:

```python
"""Ticket kinds, urgency, subject labels and the two reference shapes.

Kind is set in code by whoever records the ticket, never from the model's
category (spec section 3). Urgent tickets are taken first by the worker, are
late at ten minutes, are high priority, and are exempt from the credits floor
and the caps.
"""

from __future__ import annotations

import re
from typing import Optional

SUPPORT = "support"
SAFETY = "safety"
HANDOVER = "handover"
LOCKOUT = "lockout"
INTAKE = "intake"
WARRANTY_PROOF = "warranty_proof"
KINDS = (SUPPORT, SAFETY, HANDOVER, LOCKOUT, INTAKE, WARRANTY_PROOF)

# The model's category that makes a support ticket urgent.
SAFETY_CATEGORY = "battery_safety"

# Desk references start here. Seven digits, so one never matches an old mock
# number (EM-00001) and is never read as a six-digit one-time code.
FIRST_DESK_NUMBER = 1000001

# How long a record may wait from due_since before it is reported: an urgent
# one is late at ten minutes, any other is stuck at a day.
URGENT_LATE_SECONDS = 600
STUCK_SECONDS = 86400

# [0-9], not \d: in Python \d also matches Devanagari and other digits.
_DESK_REFERENCE = re.compile(r"EM-[0-9]{7,}")

_KIND_LABELS = {
    SAFETY: "SAFETY",
    HANDOVER: "Asked for a person",
    LOCKOUT: "Could not verify",
    INTAKE: "Unverified customer",
    WARRANTY_PROOF: "Late warranty registration",
}

_CATEGORY_LABELS = {
    "battery_charging": "Battery: charging",
    "battery_range": "Battery: range",
    "battery_power": "Battery: power",
    "battery_safety": "Battery: safety",
    "other": "Other",
}


def is_urgent(kind: str, category: Optional[str]) -> bool:
    return kind == SAFETY or category == SAFETY_CATEGORY


def is_desk_reference(ticket_id: Optional[str]) -> bool:
    """Whether the Desk record issued this id (EM-1000001 up), rather than
    the mock (EM-00001)."""
    return isinstance(ticket_id, str) and _DESK_REFERENCE.fullmatch(ticket_id) is not None


def desk_reference(number: int) -> str:
    return "EM-%d" % number


def subject_label(kind: str, category: Optional[str]) -> str:
    """The label in a Zoho subject. A support ticket's comes from its
    category; every other kind's is fixed, whatever the category says."""
    if kind not in KINDS:
        raise ValueError("unknown ticket kind: %r" % (kind,))
    if kind != SUPPORT:
        return _KIND_LABELS[kind]
    if not category:
        return "Support"
    return _CATEGORY_LABELS.get(category, category.replace("_", " ").capitalize())
```

Create `src/emotorad_ai/tickets/record.py`:

```python
"""The ticket record: one document in `tickets` per ticket (spec section 3).

Written in the turn through the seam (seam.py) and sent to Zoho Desk after
the reply by the worker. It holds no copy of the transcript: the worker reads
the conversation store itself, for this run's turns only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .clock import plus
from .kinds import KINDS, is_urgent

# States. "waiting" and "stuck" have work outstanding; "sent" has none; "gone"
# means the Zoho ticket was deleted or merged in Desk and takes no more work.
# "held" is never stored: it is how the listing and /health show an
# outstanding record of the other mode, which is never sent.
WAITING = "waiting"
SENT = "sent"
STUCK = "stuck"
GONE = "gone"
HELD = "held"
OUTSTANDING = (WAITING, STUCK)

MODES = ("test", "live")
IDENTITIES = ("verified", "unverified")

# The fallback for a turn that died before its end: the turn's end wakes the
# record at once, and otherwise it is first sent two minutes after creation.
FIRST_ATTEMPT_SECONDS = 120


def new_record(
    *,
    reference: str,
    chat_reference: str,
    source_key: str,
    mode: str,
    kind: str,
    conversation_id: str,
    started_at: Optional[str],
    cluster_id: Optional[str],
    channel: Optional[str],
    phone: Optional[str],
    identity: str,
    category: Optional[str],
    ai_severity: Optional[str],
    summary: str,
    claims: Dict[str, Any],
    bike: Optional[Dict[str, Any]],
    coverage: Optional[str],
    customer_name: Optional[str],
    created_at: str,
) -> Dict[str, Any]:
    """A new record: waiting, nothing posted, first due two minutes on."""
    if kind not in KINDS:
        raise ValueError("unknown ticket kind: %r" % (kind,))
    if mode not in MODES:
        raise ValueError("unknown mode: %r" % (mode,))
    if identity not in IDENTITIES:
        raise ValueError("unknown identity: %r" % (identity,))
    if not source_key:
        raise ValueError("a ticket record needs a source_key")
    if not conversation_id:
        raise ValueError("a ticket record needs a conversation_id")
    return {
        "_id": reference,
        "chat_reference": chat_reference,
        "source_key": source_key,
        "mode": mode,
        "kind": kind,
        "urgent": is_urgent(kind, category),
        "conversation_id": conversation_id,
        "cluster_id": cluster_id,
        "started_at": started_at,
        "ended_at": None,
        "channel": channel,
        "created_at": created_at,
        "phone": phone,
        "identity": identity,
        "category": category,
        "ai_severity": ai_severity,
        "summary": summary,
        "claims": dict(claims or {}),
        "bike": dict(bike) if bike else None,
        "coverage": coverage,
        "customer_name": customer_name,
        "notes": [],
        "zoho": {"contact_id": None, "ticket_id": None, "ticket_number": None, "web_url": None,
                 "comment_ids": [], "attachment_ids": []},
        "posted_turns": [],
        "posted_media": [],
        "posted_notes": [],
        "state": WAITING,
        "due_since": created_at,
        "wake": 0,
        "attempts": 0,
        "next_attempt_at": plus(created_at, FIRST_ATTEMPT_SECONDS),
        "lease_until": None,
        "lease_token": None,
        "intent": None,
        "last_error": None,
    }
```

Create `src/emotorad_ai/tickets/store.py`:

```python
"""The ticket store in memory: one process, lost on restart.

What tests and a server on the memory store use. MongoTicketStore
(stores/mongo.py) does the same in MongoDB for every server. Both are held to
tests/ticket_store_contract.py. Every method takes the lock, so the worker
thread and a turn never see half an update.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional

from ..conversation import StoreUnavailable
from .clock import parse, plus
from .kinds import FIRST_DESK_NUMBER, STUCK_SECONDS, URGENT_LATE_SECONDS, desk_reference
from .record import GONE, HELD, OUTSTANDING, SENT, STUCK, WAITING


def age_seconds(since: str, now: str) -> int:
    """Whole seconds from `since` to `now`."""
    return int((parse(now) - parse(since)).total_seconds())


def listing_row(record: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """One line of the listing for the support lead: no name, no summary, and
    only the last four digits of the number."""
    digits = "".join(ch for ch in record.get("phone") or "" if ch in "0123456789")
    return {
        "reference": record["_id"],
        "zoho_number": (record.get("zoho") or {}).get("ticket_number"),
        "state": record["state"] if record.get("mode") == mode else HELD,
        "mode": record.get("mode"),
        "last_four": digits[-4:] or None,
    }


def _set_path(doc: Dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for part in parents:
        doc = doc.setdefault(part, {})
    doc[last] = value


def _list_at(doc: Dict[str, Any], path: str) -> List[Any]:
    *parents, last = path.split(".")
    for part in parents:
        doc = doc.setdefault(part, {})
    return doc.setdefault(last, [])


class InMemoryTicketStore:
    """The ticket store protocol (the plan's shared interfaces) in one process."""

    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}
        self._next_number = FIRST_DESK_NUMBER
        self._lock = threading.Lock()

    # -- recording ---------------------------------------------------------------

    def next_reference(self) -> str:
        with self._lock:
            number = self._next_number
            self._next_number += 1
        return desk_reference(number)

    def insert(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The record, or the one already recorded under its source key: a
        retried create never makes a second ticket."""
        with self._lock:
            existing = self._with_source_key(record["source_key"])
            if existing is not None:
                return copy.deepcopy(existing)
            if record["_id"] in self._records:
                # A reference handed out twice: refuse rather than overwrite
                # somebody's ticket.
                raise StoreUnavailable("ticket reference %s is already used" % record["_id"])
            self._records[record["_id"]] = copy.deepcopy(record)
            return copy.deepcopy(record)

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._records.get(reference))

    def by_source_key(self, source_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._with_source_key(source_key))

    def _with_source_key(self, source_key: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._records.values() if r["source_key"] == source_key), None)

    # -- new content -------------------------------------------------------------

    def wake(self, reference: str, now: str) -> bool:
        with self._lock:
            return self._wake(reference, now)

    def add_note(self, reference: str, text: str, now: str) -> bool:
        with self._lock:
            record = self._records.get(reference)
            if record is None or record["state"] == GONE:
                return False
            record.setdefault("notes", []).append({"text": text, "at": now})
            return self._wake(reference, now)

    def _wake(self, reference: str, now: str) -> bool:
        """Under the lock. A sent record has work again from now; an
        outstanding one is due now, never later than it already was; a gone
        one takes no work at all."""
        record = self._records.get(reference)
        if record is None or record["state"] == GONE:
            return False
        record["wake"] = record.get("wake", 0) + 1
        if record["state"] == SENT:
            record.update(state=WAITING, due_since=now, next_attempt_at=now)
        else:
            record["next_attempt_at"] = min(record.get("next_attempt_at") or now, now)
        return True

    def close_runs(self, conversation_id: str, new_started_at: str) -> int:
        with self._lock:
            closed = 0
            for record in self._records.values():
                started = record.get("started_at")
                if (record["conversation_id"] == conversation_id and started is not None
                        and started < new_started_at and record.get("ended_at") is None):
                    record["ended_at"] = new_started_at
                    closed += 1
            return closed

    # -- the worker --------------------------------------------------------------

    def take_due(self, now: str, mode: str, lease_seconds: float, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            due = [r for r in self._records.values()
                   if r["state"] in OUTSTANDING and r["mode"] == mode and r["next_attempt_at"] <= now
                   and (r.get("lease_until") is None or r["lease_until"] < now)]
            if not due:
                return None
            record = min(due, key=lambda r: (not r["urgent"], r["next_attempt_at"], r["created_at"], r["_id"]))
            record["lease_until"] = plus(now, lease_seconds)
            record["lease_token"] = token
            return copy.deepcopy(record)

    def renew_lease(self, reference: str, token: str, until: str) -> bool:
        with self._lock:
            record = self._records.get(reference)
            if not token or record is None or record.get("lease_token") != token:
                return False
            record["lease_until"] = until
            return True

    def save(
        self,
        reference: str,
        token: str,
        changes: Dict[str, Any],
        add_to_set: Optional[Dict[str, List[Any]]] = None,
        push: Optional[Dict[str, List[Any]]] = None,
        expect_wake: Optional[int] = None,
    ) -> bool:
        """Only under this lease, and with the wake count unchanged when asked.
        Never creates a record: False when nothing matched."""
        with self._lock:
            record = self._records.get(reference)
            if not token or record is None or record.get("lease_token") != token:
                return False
            if expect_wake is not None and record.get("wake") != expect_wake:
                return False
            for path, value in changes.items():
                _set_path(record, path, copy.deepcopy(value))
            for path, values in (add_to_set or {}).items():
                items = _list_at(record, path)
                for value in values:
                    if value not in items:
                        items.append(copy.deepcopy(value))
            for path, values in (push or {}).items():
                _list_at(record, path).extend(copy.deepcopy(list(values)))
            return True

    # -- reporting ---------------------------------------------------------------

    def overdue(self, now: str, mode: str) -> List[Dict[str, Any]]:
        late, stuck = plus(now, -URGENT_LATE_SECONDS), plus(now, -STUCK_SECONDS)
        with self._lock:
            found = [copy.deepcopy(r) for r in self._records.values()
                     if r["state"] in OUTSTANDING and r["mode"] == mode
                     and r["due_since"] <= (late if r["urgent"] else stuck)]
        return sorted(found, key=lambda r: (r["due_since"], r["_id"]))

    def counts(self, mode: str, now: str) -> Dict[str, Any]:
        with self._lock:
            mine = [r for r in self._records.values() if r["state"] in OUTSTANDING and r["mode"] == mode]
            held = sum(1 for r in self._records.values() if r["state"] in OUTSTANDING and r["mode"] != mode)
        oldest = min((r["due_since"] for r in mine), default=None)
        return {
            "waiting": sum(1 for r in mine if r["state"] == WAITING),
            "stuck": sum(1 for r in mine if r["state"] == STUCK),
            "held": held,
            "oldest_due_seconds": age_seconds(oldest, now) if oldest else None,
        }

    def unverified_since(self, since: str, phone: Optional[str] = None) -> int:
        with self._lock:
            return sum(1 for r in self._records.values()
                       if r["identity"] == "unverified" and not r["urgent"] and r["created_at"] >= since
                       and (phone is None or r.get("phone") == phone))

    def contact_for(self, phone: str) -> Optional[str]:
        """A Zoho contact only from a live record of a proved number: never
        a test or an unverified one's."""
        with self._lock:
            mine = sorted((r for r in self._records.values()
                           if r.get("phone") == phone and r["mode"] == "live" and r["identity"] == "verified"),
                          key=lambda r: (r["created_at"], r["_id"]), reverse=True)
            return next((r["zoho"]["contact_id"] for r in mine if (r.get("zoho") or {}).get("contact_id")), None)

    def listing(self, mode: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = sorted((r for r in self._records.values() if r["state"] in OUTSTANDING),
                          key=lambda r: (r["created_at"], r["_id"]))
            return [listing_row(r, mode) for r in rows]

    def has_unique_source_key(self) -> bool:
        """Always: insert holds the lock while it looks for the key."""
        return True
```

Modify `src/emotorad_ai/stores/mongo.py`. Replace this block:

```python
Five collections. `transcript_turns`, `conversation_summaries` and `media`
are the conversation record and are kept permanently: no TTL index, and
deletion on request through `delete_person` or `delete_conversation`.
`conversations` (working state) and `idempotency_keys` expire through TTL
indexes.
```

with:

```python
Five collections. `transcript_turns`, `conversation_summaries` and `media`
are the conversation record and are kept permanently: no TTL index, and
deletion on request through `delete_person` or `delete_conversation`.
`conversations` (working state) and `idempotency_keys` expire through TTL
indexes.

`tickets` (the Zoho Desk ticket record, tickets/record.py) and `counters`
(the number behind each reference) are permanent too. Erasure does not reach
`tickets` yet: spec 2026-10-05 section 11 is deferred, so a person erasing
someone removes their ticket records by hand.
```

Replace this block:

```python
import json
import os
import re
```

with:

```python
import copy
import json
import os
import re
```

Replace this block:

```python
from pymongo.errors import DuplicateKeyError, PyMongoError
```

with:

```python
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
```

Replace this block:

```python
from ..tools.registry import CLAIM_LEASE_SECONDS, write_in_progress
```

with:

```python
from ..tickets.clock import plus
from ..tickets.kinds import FIRST_DESK_NUMBER, STUCK_SECONDS, URGENT_LATE_SECONDS, desk_reference
from ..tickets.record import GONE, OUTSTANDING, SENT, STUCK, WAITING
from ..tickets.store import age_seconds, listing_row
from ..tools.registry import CLAIM_LEASE_SECONDS, write_in_progress
```

Replace this block:

```python
ERASURE_REQUESTS = "erasure_requests"
ERASURE_LOG = "erasure_log"
```

with:

```python
ERASURE_REQUESTS = "erasure_requests"
ERASURE_LOG = "erasure_log"
# The Zoho Desk ticket record (tickets/record.py), permanent like the
# transcript, and the counter that numbers its references.
TICKETS = "tickets"
COUNTERS = "counters"
# The counters document behind EM-1000001, EM-1000002, ...
TICKET_COUNTER = "ticket_reference"
```

Replace this block:

```python
        ([("status", 1), ("requested_at", 1)], {"name": "status_requested"}),
    ],
}
```

with:

```python
        ([("status", 1), ("requested_at", 1)], {"name": "status_requested"}),
    ],
    TICKETS: [
        # One record per source key: a retried create finds the first, and two
        # servers racing make one. Zoho stays off without it (spec section 1).
        ([("source_key", 1)], {"name": "source_key", "unique": True}),
        # The worker's due query.
        ([("state", 1), ("next_attempt_at", 1)], {"name": "due"}),
        ([("phone", 1)], {"name": "phone"}),
        ([("conversation_id", 1)], {"name": "conversation"}),
    ],
    # One small document per sequence, found by its _id: no index of its own.
    COUNTERS: [],
}
```

At the end of the file, after `MongoIdempotencyStore.release`, i.e. after this block:

```python
    def release(self, key: str) -> None:
        # Only a pending claim is released; a finished receipt is never undone.
        self._guard("delete_one", lambda: self._receipts.delete_one({"_id": key, "status": "pending"}))
```

add:

```python


class MongoTicketStore:
    """The ticket record (tickets/record.py) for every server.

    The next reference is one atomic `$inc` on a `counters` document. A save
    is conditional on the worker's lease, and on the wake count when asked,
    and never upserts: a record removed, or taken by another worker, is
    dropped rather than written back. Held to the same contract as
    InMemoryTicketStore (tests/ticket_store_contract.py).
    """

    def __init__(self, db: Any) -> None:
        self._tickets = db[TICKETS]
        self._counters = db[COUNTERS]

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            raise StoreUnavailable("MongoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    # -- recording ---------------------------------------------------------------

    def next_reference(self) -> str:
        doc = self._guard("find_one_and_update", lambda: self._counters.find_one_and_update(
            {"_id": TICKET_COUNTER}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER))
        return desk_reference(FIRST_DESK_NUMBER - 1 + int(doc["seq"]))

    def insert(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The record, or the one already recorded under its source key. The
        unique index decides, so two servers racing make one."""
        try:
            self._guard("insert_one", lambda: self._tickets.insert_one(copy.deepcopy(record)))
        except DuplicateKeyError:
            existing = self.by_source_key(record["source_key"])
            if existing is None:
                # The reference itself was taken: refuse rather than overwrite.
                raise StoreUnavailable("MongoDB insert_one failed (DuplicateKeyError)") from None
            return existing
        return copy.deepcopy(record)

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        return self._guard("find_one", lambda: self._tickets.find_one({"_id": reference}))

    def by_source_key(self, source_key: str) -> Optional[Dict[str, Any]]:
        return self._guard("find_one", lambda: self._tickets.find_one({"source_key": source_key}))

    # -- new content -------------------------------------------------------------

    def wake(self, reference: str, now: str) -> bool:
        return self._wake(reference, now, {})

    def add_note(self, reference: str, text: str, now: str) -> bool:
        return self._wake(reference, now, {"$push": {"notes": {"text": text, "at": now}}})

    def _wake(self, reference: str, now: str, extra: Dict[str, Any]) -> bool:
        # The count first, then the state. A worker finishing in between
        # cannot save over this wake: its last save expects the count it read.
        update = dict(extra)
        update["$inc"] = {"wake": 1}
        update["$min"] = {"next_attempt_at": now}
        result = self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "state": {"$ne": GONE}}, update))
        if result.matched_count == 0:
            return False
        self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "state": SENT},
            {"$set": {"state": WAITING, "due_since": now, "next_attempt_at": now}}))
        return True

    def close_runs(self, conversation_id: str, new_started_at: str) -> int:
        result = self._guard("update_many", lambda: self._tickets.update_many(
            {"conversation_id": conversation_id, "started_at": {"$lt": new_started_at}, "ended_at": None},
            {"$set": {"ended_at": new_started_at}}))
        return result.modified_count

    # -- the worker --------------------------------------------------------------

    def take_due(self, now: str, mode: str, lease_seconds: float, token: str) -> Optional[Dict[str, Any]]:
        query = {"state": {"$in": list(OUTSTANDING)}, "mode": mode, "next_attempt_at": {"$lte": now},
                 "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}]}
        return self._guard("find_one_and_update", lambda: self._tickets.find_one_and_update(
            query, {"$set": {"lease_until": plus(now, lease_seconds), "lease_token": token}},
            sort=[("urgent", -1), ("next_attempt_at", 1), ("created_at", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER))

    def renew_lease(self, reference: str, token: str, until: str) -> bool:
        if not token:
            return False
        result = self._guard("update_one", lambda: self._tickets.update_one(
            {"_id": reference, "lease_token": token}, {"$set": {"lease_until": until}}))
        return result.matched_count == 1

    def save(
        self,
        reference: str,
        token: str,
        changes: Dict[str, Any],
        add_to_set: Optional[Dict[str, List[Any]]] = None,
        push: Optional[Dict[str, List[Any]]] = None,
        expect_wake: Optional[int] = None,
    ) -> bool:
        if not token:
            return False
        query: Dict[str, Any] = {"_id": reference, "lease_token": token}
        if expect_wake is not None:
            query["wake"] = expect_wake
        update: Dict[str, Any] = {}
        if changes:
            update["$set"] = dict(changes)
        if add_to_set:
            update["$addToSet"] = {path: {"$each": list(values)} for path, values in add_to_set.items()}
        if push:
            update["$push"] = {path: {"$each": list(values)} for path, values in push.items()}
        if not update:
            return self._guard("find_one", lambda: self._tickets.find_one(query, {"_id": 1})) is not None
        # No upsert: a record erased meanwhile is not written back.
        result = self._guard("update_one", lambda: self._tickets.update_one(query, update))
        return result.matched_count == 1

    # -- reporting ---------------------------------------------------------------

    def overdue(self, now: str, mode: str) -> List[Dict[str, Any]]:
        query = {"state": {"$in": list(OUTSTANDING)}, "mode": mode, "$or": [
            {"urgent": True, "due_since": {"$lte": plus(now, -URGENT_LATE_SECONDS)}},
            {"urgent": False, "due_since": {"$lte": plus(now, -STUCK_SECONDS)}},
        ]}
        return self._guard("find", lambda: list(self._tickets.find(query).sort([("due_since", 1), ("_id", 1)])))

    def counts(self, mode: str, now: str) -> Dict[str, Any]:
        outstanding = {"$in": list(OUTSTANDING)}

        def count(query: Dict[str, Any]) -> int:
            return self._guard("count_documents", lambda: self._tickets.count_documents(query))

        oldest = self._guard("find_one", lambda: self._tickets.find_one(
            {"state": outstanding, "mode": mode}, {"due_since": 1}, sort=[("due_since", 1)]))
        return {
            "waiting": count({"state": WAITING, "mode": mode}),
            "stuck": count({"state": STUCK, "mode": mode}),
            "held": count({"state": outstanding, "mode": {"$ne": mode}}),
            "oldest_due_seconds": age_seconds(oldest["due_since"], now) if oldest else None,
        }

    def unverified_since(self, since: str, phone: Optional[str] = None) -> int:
        query: Dict[str, Any] = {"identity": "unverified", "urgent": False, "created_at": {"$gte": since}}
        if phone is not None:
            query["phone"] = phone
        return self._guard("count_documents", lambda: self._tickets.count_documents(query))

    def contact_for(self, phone: str) -> Optional[str]:
        docs = self._guard("find", lambda: list(self._tickets.find(
            {"phone": phone, "mode": "live", "identity": "verified"}, {"zoho": 1, "created_at": 1})
            .sort([("created_at", -1), ("_id", -1)])))
        return next((d["zoho"]["contact_id"] for d in docs if (d.get("zoho") or {}).get("contact_id")), None)

    def listing(self, mode: str) -> List[Dict[str, Any]]:
        docs = self._guard("find", lambda: list(self._tickets.find({"state": {"$in": list(OUTSTANDING)}})
                                                .sort([("created_at", 1), ("_id", 1)])))
        return [listing_row(d, mode) for d in docs]

    def has_unique_source_key(self) -> bool:
        """Whether mongo_setup.py has made `source_key` unique. Zoho stays
        off without it (spec section 1)."""
        info = self._guard("index_information", lambda: self._tickets.index_information())
        return any([name for name, _ in spec.get("key", [])] == ["source_key"] and bool(spec.get("unique"))
                   for spec in info.values())
```

Modify `src/emotorad_ai/wiring.py`. Replace this block:

```python
@dataclass
class Stores:
    conversations: Any
    idempotency: Any
```

with:

```python
@dataclass
class Stores:
    conversations: Any
    idempotency: Any
    # The ticket record (tickets/store.py, stores/mongo.py). Written only when
    # Zoho Desk is on; the mock ticket system never touches it.
    tickets: Any = None
```

Replace this block:

```python
        return Stores(conversations=InMemoryConversationStore(), idempotency=IdempotencyStore())
    from .stores.mongo import MongoConversationStore, MongoIdempotencyStore, connect

    db = connect(db_name=settings.mongo_db, client=client)
    return Stores(
        conversations=MongoConversationStore(db, state_ttl_hours=settings.state_ttl_hours, log=log),
        idempotency=MongoIdempotencyStore(db, ttl_days=settings.idempotency_ttl_days),
    )
```

with:

```python
        from .tickets.store import InMemoryTicketStore

        return Stores(conversations=InMemoryConversationStore(), idempotency=IdempotencyStore(),
                      tickets=InMemoryTicketStore())
    from .stores.mongo import MongoConversationStore, MongoIdempotencyStore, MongoTicketStore, connect

    db = connect(db_name=settings.mongo_db, client=client)
    return Stores(
        conversations=MongoConversationStore(db, state_ttl_hours=settings.state_ttl_hours, log=log),
        idempotency=MongoIdempotencyStore(db, ttl_days=settings.idempotency_ttl_days),
        tickets=MongoTicketStore(db),
    )
```

Modify `scripts/mongo_setup.py`. Replace this block:

```python
Prints every collection and index so the output can be checked, and fails if
the permanent record (transcripts, summaries, media) has picked up an expiry
index.
```

with:

```python
Prints every collection and index so the output can be checked, and fails if
the permanent record (transcripts, summaries, media, tickets and the counter
behind their references) has picked up an expiry index.
```

Replace this block:

```python
from emotorad_ai.stores.mongo import (  # noqa: E402
    CONVERSATION_SUMMARIES, MEDIA, MONGO_URI_ENV, TRANSCRIPT_TURNS, connect, ensure_indexes,
)

PERMANENT = (TRANSCRIPT_TURNS, CONVERSATION_SUMMARIES, MEDIA)
```

with:

```python
from emotorad_ai.stores.mongo import (  # noqa: E402
    CONVERSATION_SUMMARIES, COUNTERS, MEDIA, MONGO_URI_ENV, TICKETS, TRANSCRIPT_TURNS, connect, ensure_indexes,
)

# Tickets are permanent like the transcript, and so is the counter that
# numbers them: a counter that expired would hand out EM-1000001 again.
PERMANENT = (TRANSCRIPT_TURNS, CONVERSATION_SUMMARIES, MEDIA, TICKETS, COUNTERS)
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_kinds tests.test_ticket_store tests.test_mongo_store tests.test_mongo_scripts
```

What you should see: OK. `tests.test_ticket_kinds` adds 13 tests and `tests.test_ticket_store` adds 81 (the 32-test contract on each store, plus 17 others).

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

What you should see: 2,277 tests (2,183 plus 94), with only the known `test_video` environmental failure. Three existing tests change on purpose, and none is weakened:
- `tests.test_mongo_store.IndexTests.test_every_collection_gets_exactly_its_indexes_and_only_two_expire` now expects `tickets` and `counters`.
- `tests.test_mongo_store.IndexTests.test_the_index_table_has_no_ttl_on_the_permanent_record` now also checks both new collections.
- `tests.test_mongo_scripts.SetupScriptTests.test_setup_reports_every_collection_and_no_expiry_on_the_record` now asserts both are permanent.

`tests.test_mongo_scripts.SmokeScriptTests` still passes, because it runs `ensure_indexes` first and the smoke script's check 0 then finds the new indexes. Nothing writes to `tickets` yet.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/tickets/__init__.py src/emotorad_ai/tickets/clock.py src/emotorad_ai/tickets/kinds.py src/emotorad_ai/tickets/record.py src/emotorad_ai/tickets/store.py src/emotorad_ai/stores/mongo.py src/emotorad_ai/wiring.py scripts/mongo_setup.py tests/test_ticket_kinds.py tests/ticket_store_contract.py tests/test_ticket_store.py tests/test_mongo_store.py tests/test_mongo_scripts.py
git commit -m "feat: the ticket record and its store, in memory and MongoDB" -m "New tickets package: the clock, kinds and urgency, the record shape, and InMemoryTicketStore. MongoTicketStore keeps tickets and a counter for EM-1000001 upwards, with a unique source_key index. Saves are leased and never upsert, and wakes are never lost. Both stores pass one shared contract. Stores.tickets is wired, and mongo_setup keeps both collections permanent. Nothing writes to tickets yet." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: Build the seam: `MockTicketSystem` additions, `DeskTicketSystem`, `TicketRouter`

**Files:**
- Create: `src/emotorad_ai/tickets/seam.py`
- Modify: `src/emotorad_ai/tools/mocks.py:401-419` (`MockTicketSystem`), `:457` (`build_registry`'s `ticket_system` annotation)
- Test: `tests/test_ticket_seam.py` (new)

**Interfaces:**
- Consumes: from Task 2, the `TicketStore` protocol (`next_reference`, `insert`, `get`, `by_source_key`, `wake`, `add_note`, `close_runs`, `take_due`, `save`, `listing`), `new_record`, `MODES`, `KINDS`, `is_desk_reference` and `now_iso`. Also `observability.redact_pii` (existing).
- Produces:
  - `MockTicketSystem`:
    - `create(source_key=None, persona=None, **payload)` returns the same ticket dict for a repeated `source_key`, and keeps `source_key` and `persona` on the ticket when given.
    - `add_note(ticket_id, text)` appends to `ticket["notes"]` and raises `KeyError` for an unknown id.
    - `close_runs(conversation_id, new_started_at)` does nothing.
    - `records_real_tickets = False`.
  - `tickets.seam.DeskTicketSystem(store, mode, environment, clock=now_iso, wake=lambda: None)`:
    - Public attributes `store`, `mode` and `environment`.
    - `create(source_key=None, persona=None, **fields)` returns `{"ticket_id", "status": "open"}`. It raises `ValueError` unless the persona is `"customer"` and there is a `source_key`, a known `kind` and a `conversation_id`. It does not wake the worker.
    - `attach_transcript(ticket_id, transcript)` and `add_note(ticket_id, text)` wake the record and call `wake()`. They raise `KeyError` for an unknown id and do nothing for a `gone` one.
    - `close_runs(conversation_id, new_started_at)`.
  - `CLAIM_FIELDS`.
  - `tickets.seam.TicketRouter(desk, mock)`: public `desk` and `mock`, `records_real_tickets = True`, and the properties `tickets` (the mock's dict) and `store` (`desk.store`). `create` goes by persona; `attach_transcript` and `add_note` go by `is_desk_reference`; `close_runs` reaches both.
  - `build_registry(ticket_system=...)` accepts a `TicketRouter`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ticket_seam.py`:

```python
"""The ticket seam (plan Task 3): the mock's additions, DeskTicketSystem and
TicketRouter. Desk only records: nothing here calls Zoho or the network."""

import copy
import unittest

import mongomock

from emotorad_ai.stores.mongo import MongoTicketStore, ensure_indexes
from emotorad_ai.tickets.clock import plus
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools.mocks import MockTicketSystem, build_registry

T0 = "2026-10-05T10:00:00.000000+00:00"
RUN = "2026-10-05T09:58:00.000000+00:00"
LATER_RUN = "2026-10-05T11:00:00.000000+00:00"
PHONE = "+919999999999"
FRAME = "EMXP2026001234"  # from the fixtures
KEY = "c1:%s:create_support_ticket:k1" % RUN


def support_fields(**overrides):
    """What create_support_ticket passes for a verified customer (Task 4)."""
    fields = dict(kind="support", conversation_id="c1", started_at=RUN, cluster_id="cluster-1", channel="whatsapp",
                  phone=PHONE, identity="verified", category="battery_charging", severity="high",
                  description="LED stays off. Call me on 9876543210.", frame_number=FRAME,
                  frame_number_source="record", bike_model="EMX Plus", coverage="computed",
                  customer_name="Test Rider")
    fields.update(overrides)
    return fields


class MockTicketSystemTests(unittest.TestCase):
    def test_the_same_source_key_returns_the_same_ticket(self):
        mock = MockTicketSystem()
        first = mock.create(source_key=KEY, persona="customer", category="battery_charging")
        again = mock.create(source_key=KEY, persona="customer", category="battery_charging")
        self.assertEqual(again["ticket_id"], first["ticket_id"])
        self.assertEqual(list(mock.tickets), ["EM-00001"])

    def test_without_a_source_key_every_create_is_a_new_ticket(self):
        mock = MockTicketSystem()
        self.assertEqual([mock.create(category="other")["ticket_id"] for _ in range(2)], ["EM-00001", "EM-00002"])

    def test_it_keeps_the_persona_the_key_and_any_field_it_is_given(self):
        ticket = MockTicketSystem().create(source_key=KEY, persona="dealer", category="other", proof_url="x")
        self.assertEqual((ticket["persona"], ticket["source_key"], ticket["proof_url"], ticket["status"]),
                         ("dealer", KEY, "x", "open"))

    def test_a_note_is_kept_on_the_ticket(self):
        mock = MockTicketSystem()
        ticket_id = mock.create(category="other")["ticket_id"]
        mock.add_note(ticket_id, "Customer asked for a person")
        self.assertEqual(mock.tickets[ticket_id]["notes"], ["Customer asked for a person"])
        with self.assertRaises(KeyError):
            mock.add_note("EM-00009", "x")

    def test_close_runs_does_nothing(self):
        mock = MockTicketSystem()
        mock.create(category="other", conversation_id="c1", started_at=RUN)
        before = copy.deepcopy(mock.tickets)
        self.assertIsNone(mock.close_runs("c1", LATER_RUN))
        self.assertEqual(mock.tickets, before)

    def test_it_records_no_real_tickets(self):
        self.assertFalse(MockTicketSystem.records_real_tickets)


class SeamContract:
    """DeskTicketSystem and TicketRouter, on whichever store make_store gives."""

    def make_store(self):
        raise NotImplementedError

    def same_store_again(self, store):
        """The store as a second server would open it."""
        return store

    def desk(self, store=None, mode="test"):
        if not hasattr(self, "now"):
            self.now, self.wakes = [T0], []
        return DeskTicketSystem(store if store is not None else self.make_store(), mode, "stage",
                                clock=lambda: self.now[0], wake=lambda: self.wakes.append(self.now[0]))

    def router(self):
        return TicketRouter(self.desk(), MockTicketSystem())

    # -- DeskTicketSystem -------------------------------------------------------------

    def test_a_customer_ticket_becomes_one_record_with_the_fields_mapped(self):
        desk = self.desk()
        self.assertEqual(desk.create(source_key=KEY, persona="customer", **support_fields()),
                         {"ticket_id": "EM-1000001", "status": "open"})
        record = desk.store.get("EM-1000001")
        self.assertEqual(record["chat_reference"], "stage:EM-1000001")
        self.assertEqual((record["source_key"], record["mode"], record["kind"], record["urgent"]),
                         (KEY, "test", "support", False))
        self.assertEqual((record["conversation_id"], record["started_at"], record["cluster_id"], record["channel"]),
                         ("c1", RUN, "cluster-1", "whatsapp"))
        self.assertEqual((record["phone"], record["identity"], record["category"], record["ai_severity"]),
                         (PHONE, "verified", "battery_charging", "high"))
        self.assertEqual(record["summary"], "LED stays off. Call me on [phone].")
        self.assertEqual(record["bike"], {"model": "EMX Plus", "frame_number": FRAME, "frame_number_source": "record"})
        self.assertEqual((record["coverage"], record["customer_name"], record["claims"]),
                         ("computed", "Test Rider", {}))
        self.assertEqual((record["created_at"], record["next_attempt_at"], record["state"]),
                         (T0, plus(T0, 120), "waiting"))

    def test_the_same_source_key_is_one_record_even_from_a_second_server(self):
        first_desk = self.desk()
        first = first_desk.create(source_key=KEY, persona="customer", **support_fields())
        second_desk = self.desk(store=self.same_store_again(first_desk.store))
        second = second_desk.create(source_key=KEY, persona="customer", **support_fields(severity="low"))
        self.assertEqual(second, first)
        self.assertEqual(len(first_desk.store.listing("test")), 1)
        self.assertEqual(first_desk.store.get("EM-1000001")["ai_severity"], "high")

    def test_urgency_follows_the_kind_and_the_category(self):
        desk = self.desk()
        made = {
            "safety": desk.create(source_key=KEY + ":safety", persona="customer", **support_fields(kind="safety")),
            "model_safety": desk.create(source_key=KEY + ":model", persona="customer",
                                        **support_fields(category="battery_safety")),
            "charging": desk.create(source_key=KEY + ":charging", persona="customer", **support_fields()),
        }
        urgent = {name: desk.store.get(result["ticket_id"])["urgent"] for name, result in made.items()}
        self.assertEqual(urgent, {"safety": True, "model_safety": True, "charging": False})

    def test_an_unverified_ticket_carries_no_bike(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields(identity="unverified"))
        record = desk.store.get("EM-1000001")
        self.assertEqual((record["identity"], record["bike"]), ("unverified", None))

    def test_without_an_identity_a_ticket_is_unverified(self):
        fields = support_fields()
        del fields["identity"]
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **fields)
        self.assertEqual(desk.store.get("EM-1000001")["identity"], "unverified")

    def test_an_intakes_stated_details_are_kept_as_claims(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields(
            kind="intake", category="intake_unverified", identity="unverified", severity="normal",
            stated_name="Test Rider", stated_contact="not given", evidence="none offered",
            frame_number=None, frame_number_source=None, bike_model=None, coverage=None, customer_name=None))
        record = desk.store.get("EM-1000001")
        self.assertEqual(record["claims"],
                         {"stated_name": "Test Rider", "stated_contact": "not given", "evidence": "none offered"})
        self.assertEqual((record["kind"], record["identity"], record["bike"]), ("intake", "unverified", None))

    def test_a_warranty_proofs_date_and_channel_are_claims(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields(
            kind="warranty_proof", category="late_warranty_registration", severity="normal",
            description="Warranty proof submitted.", frame_number=FRAME,
            frame_number_source="given by the customer", bike_model=None,
            claimed_purchase_date="2026-01-15", purchase_channel="dealer", coverage=None, customer_name=None))
        record = desk.store.get("EM-1000001")
        self.assertEqual(record["claims"], {"claimed_purchase_date": "2026-01-15", "purchase_channel": "dealer"})
        self.assertEqual(record["bike"],
                         {"model": None, "frame_number": FRAME, "frame_number_source": "given by the customer"})

    def test_fields_desk_does_not_know_are_never_recorded(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer",
                    **support_fields(proof_url="https://x.test/invoice.jpg", verified=False))
        record = desk.store.get("EM-1000001")
        self.assertNotIn("proof_url", record)
        self.assertNotIn("verified", record)
        self.assertNotIn("invoice.jpg", repr(record))

    def test_only_customer_tickets_with_a_key_kind_and_conversation_are_recorded(self):
        desk = self.desk()
        refused = (
            ("dealer", KEY, support_fields()),
            (None, KEY, support_fields()),
            ("customer", None, support_fields()),
            ("customer", KEY, support_fields(kind="complaint")),
            ("customer", KEY, support_fields(conversation_id=None)),
        )
        for persona, key, fields in refused:
            with self.subTest(persona=persona, key=key, kind=fields["kind"]), self.assertRaises(ValueError):
                desk.create(source_key=key, persona=persona, **fields)
        self.assertEqual(desk.store.listing("test"), [])
        self.assertEqual(desk.store.next_reference(), "EM-1000001")  # no reference was spent on a refusal

    def test_the_mode_is_stamped_on_the_record(self):
        desk = self.desk(mode="live")
        desk.create(source_key=KEY, persona="customer", **support_fields())
        self.assertEqual(desk.store.get("EM-1000001")["mode"], "live")

    def test_an_unknown_mode_or_no_environment_is_refused(self):
        with self.assertRaises(ValueError):
            DeskTicketSystem(self.make_store(), "staging", "stage")
        with self.assertRaises(ValueError):
            DeskTicketSystem(self.make_store(), "test", "")

    def test_recording_does_not_wake_the_worker(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields())
        self.assertEqual(self.wakes, [])
        self.assertEqual(desk.store.get("EM-1000001")["wake"], 0)

    def test_the_turns_end_makes_the_record_due_now_and_wakes_the_worker(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        self.now[0] = plus(T0, 5)
        desk.attach_transcript(ticket_id, "[10:00] Customer: my battery won't charge")
        record = desk.store.get(ticket_id)
        self.assertEqual((record["wake"], record["next_attempt_at"]), (1, plus(T0, 5)))
        self.assertEqual(self.wakes, [plus(T0, 5)])
        self.assertNotIn("my battery", repr(record))  # the worker reads the conversation store itself

    def test_a_later_turn_reopens_a_sent_record(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        desk.store.take_due(plus(T0, 120), "test", 300, "t1")
        desk.store.save(ticket_id, "t1", {"state": "sent", "lease_until": None, "lease_token": None})
        self.now[0] = plus(T0, 600)
        desk.attach_transcript(ticket_id, "ignored")
        record = desk.store.get(ticket_id)
        self.assertEqual((record["state"], record["due_since"], record["next_attempt_at"]),
                         ("waiting", plus(T0, 600), plus(T0, 600)))

    def test_a_gone_ticket_takes_no_more_work_and_no_wake(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        desk.store.take_due(plus(T0, 120), "test", 300, "t1")
        desk.store.save(ticket_id, "t1", {"state": "gone", "lease_until": None, "lease_token": None})
        desk.attach_transcript(ticket_id, "ignored")
        desk.add_note(ticket_id, "Customer asked for a person")
        record = desk.store.get(ticket_id)
        self.assertEqual((record["state"], record["wake"], record["notes"]), ("gone", 0, []))
        self.assertEqual(self.wakes, [])

    def test_an_unknown_ticket_is_a_key_error(self):
        desk = self.desk()
        with self.assertRaises(KeyError):
            desk.attach_transcript("EM-1000009", "x")
        with self.assertRaises(KeyError):
            desk.add_note("EM-1000009", "x")

    def test_a_note_is_recorded_and_wakes_the_worker(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        self.now[0] = plus(T0, 30)
        desk.add_note(ticket_id, "Customer asked for a person at 10:00")
        record = desk.store.get(ticket_id)
        self.assertEqual(record["notes"], [{"text": "Customer asked for a person at 10:00", "at": plus(T0, 30)}])
        self.assertEqual((record["wake"], self.wakes), (1, [plus(T0, 30)]))

    def test_close_runs_marks_where_the_earlier_run_ended(self):
        desk = self.desk()
        earlier = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        later = desk.create(source_key="c1:%s:create_support_ticket:k1" % LATER_RUN, persona="customer",
                            **support_fields(started_at=LATER_RUN))["ticket_id"]
        desk.close_runs("c1", LATER_RUN)
        self.assertEqual(desk.store.get(earlier)["ended_at"], LATER_RUN)
        self.assertIsNone(desk.store.get(later)["ended_at"])

    # -- TicketRouter -------------------------------------------------------------------

    def test_a_customer_ticket_goes_to_desk(self):
        router = self.router()
        self.assertEqual(router.create(source_key=KEY, persona="customer", **support_fields()),
                         {"ticket_id": "EM-1000001", "status": "open"})
        self.assertIsNotNone(router.store.get("EM-1000001"))
        self.assertEqual(router.tickets, {})

    def test_a_dealers_ticket_or_one_with_no_persona_goes_to_the_mock(self):
        router = self.router()
        dealer = router.create(source_key=KEY, persona="dealer", **support_fields())
        nobody = router.create(source_key=KEY + ":2", **support_fields())
        self.assertEqual((dealer["ticket_id"], nobody["ticket_id"]), ("EM-00001", "EM-00002"))
        self.assertEqual(router.store.listing("test"), [])
        self.assertIsNone(router.store.get("EM-1000001"))
        self.assertEqual(router.tickets["EM-00001"]["persona"], "dealer")

    def test_later_calls_go_to_the_system_that_issued_the_id(self):
        router = self.router()
        desk_id = router.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        mock_id = router.create(source_key=KEY + ":dealer", persona="dealer", **support_fields())["ticket_id"]
        router.attach_transcript(desk_id, "thread")
        router.attach_transcript(mock_id, "thread")
        router.add_note(desk_id, "Customer asked for a person")
        router.add_note(mock_id, "Dealer asked for a person")
        self.assertEqual(router.store.get(desk_id)["wake"], 2)
        self.assertEqual([n["text"] for n in router.store.get(desk_id)["notes"]], ["Customer asked for a person"])
        self.assertEqual(router.tickets[mock_id]["transcript"], "thread")
        self.assertEqual(router.tickets[mock_id]["notes"], ["Dealer asked for a person"])
        self.assertNotIn(desk_id, router.tickets)

    def test_close_runs_reaches_the_desk_records(self):
        router = self.router()
        desk_id = router.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        router.close_runs("c1", LATER_RUN)
        self.assertEqual(router.store.get(desk_id)["ended_at"], LATER_RUN)

    def test_it_records_real_tickets_and_exposes_the_mocks_dict_and_the_store(self):
        router = self.router()
        self.assertTrue(router.records_real_tickets)
        self.assertIs(router.tickets, router.mock.tickets)
        self.assertIs(router.store, router.desk.store)

    def test_the_registry_holds_the_router_and_still_reads_the_mocks_dict(self):
        router = self.router()
        registry = build_registry(ticket_system=router)
        self.assertIs(registry.tickets, router)
        self.assertIs(registry.tickets.tickets, router.mock.tickets)


class SeamOnMemoryTests(SeamContract, unittest.TestCase):
    def make_store(self):
        return InMemoryTicketStore()


class SeamOnMongoTests(SeamContract, unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def make_store(self):
        return MongoTicketStore(self.db)

    def same_store_again(self, store):
        return MongoTicketStore(self.db)  # a second server on the same database


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_seam
```

What you should see: the import fails with `ModuleNotFoundError: No module named 'emotorad_ai.tickets.seam'`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/tickets/seam.py`:

```python
"""The ticket seam (spec 2026-10-05, section 2): where a ticket tool or a
runtime gate records a ticket.

`DeskTicketSystem` writes the ticket record (record.py) to the ticket store
and never calls Zoho: the worker (zoho/worker.py) sends it after the reply.
`TicketRouter` is what the registry holds when Zoho is on. A new ticket goes
to Desk for the customer persona, and to the mock for anyone else or a caller
that names nobody, so a dealer's report never becomes a customer ticket.
Every later call goes by the id, to the system that issued it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ..observability import redact_pii
from .clock import now_iso
from .kinds import KINDS, is_desk_reference
from .record import MODES, new_record

if TYPE_CHECKING:
    from ..tools.mocks import MockTicketSystem

# What the customer told us, kept on the record as their claims and never as
# facts. The intake and warranty-proof tools pass these.
CLAIM_FIELDS = ("stated_name", "stated_contact", "evidence", "claimed_purchase_date", "purchase_channel")


class DeskTicketSystem:
    """Records customer tickets for Zoho Desk. Nothing here calls Zoho."""

    def __init__(
        self,
        store: Any,
        mode: str,
        environment: str,
        clock: Callable[[], str] = now_iso,
        wake: Callable[[], None] = lambda: None,
    ) -> None:
        if mode not in MODES:
            raise ValueError("unknown mode: %r" % (mode,))
        if not environment:
            raise ValueError("the environment names the chat reference and is required")
        self.store = store
        # Stamped on every record: the worker sends only its own mode's.
        self.mode = mode
        self.environment = environment
        self._clock = clock
        # Tells the worker there is work now, so it need not wait for its pass.
        self._wake = wake

    def create(self, source_key: Optional[str] = None, persona: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """One record per source key: the same key always returns the same ticket.

        Checked before a reference is taken, so a refusal spends no number.
        Not woken here: the turn's end does that (attach_transcript), so Zoho
        is first called after the reply, or two minutes on if the turn died.
        """
        if persona != "customer":
            raise ValueError("Zoho Desk records customer tickets only, not %r" % (persona,))
        if not source_key:
            raise ValueError("a Desk ticket needs a source_key")
        if fields.get("kind") not in KINDS:
            raise ValueError("unknown ticket kind: %r" % (fields.get("kind"),))
        if not fields.get("conversation_id"):
            raise ValueError("a Desk ticket needs a conversation_id")
        record = self.store.by_source_key(source_key)
        if record is None:
            record = self.store.insert(self._new_record(source_key, fields))
        return {"ticket_id": record["_id"], "status": "open"}

    def _new_record(self, source_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        identity = "verified" if fields.get("identity") == "verified" else "unverified"
        bike = None
        if identity == "verified":
            # A bike only for a proved number. A typed one could name a
            # stranger's bike, because the look-up would find its owner's.
            bike = {"model": fields.get("bike_model"), "frame_number": fields.get("frame_number"),
                    "frame_number_source": fields.get("frame_number_source")}
            if not any(bike.values()):
                bike = None
        reference = self.store.next_reference()
        return new_record(
            reference=reference,
            # Unique across deployments that share the Zoho organisation.
            chat_reference="%s:%s" % (self.environment, reference),
            source_key=source_key,
            mode=self.mode,
            kind=fields["kind"],
            conversation_id=fields["conversation_id"],
            started_at=fields.get("started_at"),
            cluster_id=fields.get("cluster_id"),
            channel=fields.get("channel"),
            phone=fields.get("phone"),
            identity=identity,
            category=fields.get("category"),
            ai_severity=fields.get("severity"),
            # From the customer's words: a number or an email typed into it
            # does not go to a third party.
            summary=redact_pii(fields.get("description") or ""),
            claims={name: fields[name] for name in CLAIM_FIELDS if fields.get(name) not in (None, "")},
            bike=bike,
            coverage=fields.get("coverage"),
            customer_name=fields.get("customer_name"),
            created_at=self._clock(),
        )

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        """Marks the record as having new content. The text is not used: the
        worker reads this run's turns from the conversation store itself, so
        an earlier person's run never reaches this ticket."""
        if self.store.wake(ticket_id, self._clock()):
            self._wake()
            return
        self._known(ticket_id)

    def add_note(self, ticket_id: str, text: str) -> None:
        """A short line for the person working the ticket."""
        if self.store.add_note(ticket_id, text, self._clock()):
            self._wake()
            return
        self._known(ticket_id)

    def close_runs(self, conversation_id: str, new_started_at: str) -> None:
        """Marks where the conversation's earlier runs ended, so the worker
        posts only each run's own turns and media."""
        self.store.close_runs(conversation_id, new_started_at)

    def _known(self, ticket_id: str) -> None:
        # Not woken. A gone record (deleted or merged in Desk) takes no more
        # work, which is not a failure. An id nobody issued is one.
        if self.store.get(ticket_id) is None:
            raise KeyError("no ticket %s" % ticket_id)


class TicketRouter:
    """What the registry holds when Zoho is on."""

    # The registry's ticket system now records tickets a person will work.
    records_real_tickets = True

    def __init__(self, desk: DeskTicketSystem, mock: "MockTicketSystem") -> None:
        self.desk = desk
        self.mock = mock

    @property
    def tickets(self) -> Dict[str, Dict[str, Any]]:
        """The mock's tickets, so code and tests that read
        registry.tickets.tickets keep working."""
        return self.mock.tickets

    @property
    def store(self) -> Any:
        return self.desk.store

    def create(self, source_key: Optional[str] = None, persona: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """Desk for a customer. The mock for anyone else, or for a call that
        names nobody, so a ticket of unknown origin never becomes a customer's."""
        system: Any = self.desk if persona == "customer" else self.mock
        return system.create(source_key=source_key, persona=persona, **fields)

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        self._issuer(ticket_id).attach_transcript(ticket_id, transcript)

    def add_note(self, ticket_id: str, text: str) -> None:
        self._issuer(ticket_id).add_note(ticket_id, text)

    def close_runs(self, conversation_id: str, new_started_at: str) -> None:
        self.desk.close_runs(conversation_id, new_started_at)
        self.mock.close_runs(conversation_id, new_started_at)

    def _issuer(self, ticket_id: str) -> Any:
        # Seven digits (EM-1000001 up) are Desk's; five are the mock's.
        return self.desk if is_desk_reference(ticket_id) else self.mock
```

Modify `src/emotorad_ai/tools/mocks.py`. Replace this block:

```python
class MockTicketSystem:
    """Stands in for Zoho Desk (confirmed for dealer-side W1; customer side is an open item)."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.tickets: Dict[str, Dict[str, Any]] = {}

    def create(self, **payload: Any) -> Dict[str, Any]:
        ticket_id = "EM-%05d" % next(self._counter)
        ticket = dict(payload, ticket_id=ticket_id, status="open")
        self.tickets[ticket_id] = ticket
        return ticket

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        """The conversation thread on the ticket, for whoever picks it up.
        Zoho will implement this as a thread or comment; the mock keeps it."""
        if ticket_id not in self.tickets:
            raise KeyError("no ticket %s" % ticket_id)
        self.tickets[ticket_id]["transcript"] = transcript
```

with:

```python
class MockTicketSystem:
    """Stands in for Zoho Desk: in tests, the playground, the CLI and the live
    evaluation, and for every persona but the customer's when Zoho is on
    (tickets/seam.py). Its EM-00001 numbers exist nowhere else."""

    # Nothing it records reaches a person (TicketRouter says True).
    records_real_tickets = False

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.tickets: Dict[str, Dict[str, Any]] = {}
        # source_key -> ticket id: the same key always returns the same ticket.
        self._by_source_key: Dict[str, str] = {}

    def create(self, source_key: Optional[str] = None, persona: Optional[str] = None, **payload: Any) -> Dict[str, Any]:
        if source_key and source_key in self._by_source_key:
            return self.tickets[self._by_source_key[source_key]]
        ticket_id = "EM-%05d" % next(self._counter)
        ticket = dict(payload, ticket_id=ticket_id, status="open")
        if source_key:
            ticket["source_key"] = source_key
            self._by_source_key[source_key] = ticket_id
        if persona is not None:
            ticket["persona"] = persona
        self.tickets[ticket_id] = ticket
        return ticket

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        """The conversation thread on the ticket, for whoever picks it up.
        Zoho will implement this as a thread or comment; the mock keeps it."""
        if ticket_id not in self.tickets:
            raise KeyError("no ticket %s" % ticket_id)
        self.tickets[ticket_id]["transcript"] = transcript

    def add_note(self, ticket_id: str, text: str) -> None:
        """A short line for the ticket, kept with it."""
        if ticket_id not in self.tickets:
            raise KeyError("no ticket %s" % ticket_id)
        self.tickets[ticket_id].setdefault("notes", []).append(text)

    def close_runs(self, conversation_id: str, new_started_at: str) -> None:
        """Nothing to mark: the mock sends nothing anywhere."""
```

Replace this block:

```python
def build_registry(
    knowledge_base: Optional[BatteryKnowledgeBase] = None,
    ticket_system: Optional[MockTicketSystem] = None,
```

with:

```python
def build_registry(
    knowledge_base: Optional[BatteryKnowledgeBase] = None,
    # A MockTicketSystem, or the TicketRouter (tickets/seam.py) when Zoho is on.
    ticket_system: Optional[Any] = None,
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_seam
```

What you should see: 54 tests, OK (6 for the mock, plus 24 for the seam on each of the two stores).

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

What you should see: 2,331 tests (2,277 plus 54), with only the known `test_video` environmental failure. No existing test should change. Every existing caller of `tickets.create(...)` passes keywords and no `source_key`, so each call still makes a new mock ticket as before. The tests that read `registry.tickets.tickets[...]` still get the mock's dict.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/tickets/seam.py src/emotorad_ai/tools/mocks.py tests/test_ticket_seam.py
git commit -m "feat: the ticket seam: Desk records and a router by persona" -m "MockTicketSystem returns one ticket per source key, accepts a persona, keeps notes and has a do-nothing close_runs. DeskTicketSystem records customer tickets in the ticket store and never calls Zoho. It maps fields to the record, redacts the summary and stamps the mode. It wakes the record at the turn's end, not on create. TicketRouter sends customer tickets to Desk and everything else to the mock, and routes later calls by reference shape." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Interface issues for the plan author

- **`held` is not a stored state.** Spec section 3 lists `held` as a state, but the skeleton only describes it as "other mode". The stores never write it. `counts` and `listing` show a `waiting` or `stuck` record of the other mode as held. `tickets.record.HELD` is the label only.
- **`wake` and `add_note` make a record due no later than now.** They do not always set it to exactly now. For a `waiting` or `stuck` record they take the earlier of `next_attempt_at` and now (Mongo `$min`), so a record already overdue keeps its place in the queue. A `sent` record gets exactly now, with `due_since` set to now. A `gone` record (or an unknown one) returns False, and a note sent to it is not added.
- **The wake uses two updates in MongoDB.** The count and `$min` go first, then the `sent` to `waiting` change. That order keeps the worker's `expect_wake` check correct. Task 9 should rely on `expect_wake`, not on the state alone.
- **`save` and `renew_lease` with a falsy token return False.** Otherwise a token of `None` would match any record that is not leased.
- **`take_due` and the listing break ties.** `take_due` sorts by `created_at`, then `_id`, after the skeleton's urgent and `next_attempt_at`. The listing and `overdue` sort by `created_at` or `due_since`, then `_id`.
- **New names not in the skeleton:**
  - In `tickets/kinds.py`: `URGENT_LATE_SECONDS`, `STUCK_SECONDS`, `SAFETY_CATEGORY`, `desk_reference(number)` and the six kind constants.
  - In `tickets/record.py`: `FIRST_ATTEMPT_SECONDS`, `WAITING`, `SENT`, `STUCK`, `GONE`, `HELD`, `OUTSTANDING`, `MODES` and `IDENTITIES`.
  - In `tickets/store.py`: `listing_row` and `age_seconds`.
  - In `stores/mongo.py`: `TICKETS`, `COUNTERS` and `TICKET_COUNTER`.
  - In `tickets/seam.py`: `CLAIM_FIELDS`.
  - Tasks 9, 10 and 12 should import these rather than redefine them.
- **Row and count shapes.** Each `listing` row is `{"reference", "zoho_number", "state", "mode", "last_four"}`. `counts["oldest_due_seconds"]` is whole seconds from the oldest `due_since` among this mode's waiting and stuck records, or None.
- **`is_desk_reference` uses `[0-9]`, not the skeleton's `\d`.** Python's `\d` also matches Devanagari digits, which the repo rule forbids relying on.
- **`DeskTicketSystem.create` is stricter than the skeleton says.**
  - It raises `ValueError` for a persona other than `"customer"`, a missing `source_key`, an unknown `kind` or a missing `conversation_id`, before it spends a reference.
  - A missing or unknown `identity` is recorded as `unverified`, and an unverified record's `bike` is always None.
  - It returns `status: "open"`, as the mock does.
  - `attach_transcript` and `add_note` raise `KeyError` for an unknown id, as the mock does, and do nothing for a `gone` record.
- **Extra attributes.** `MockTicketSystem` gains `records_real_tickets = False`, so `getattr(registry.tickets, "records_real_tickets", False)` reads the same with either system. `TicketRouter` exposes `desk` and `mock`. `DeskTicketSystem` exposes `store`, `mode` and `environment`.
- **`ConversationState.started_at` is not in the ticket clock's format.** It comes from `conversation.utc_now_iso()`, which drops the microseconds on a whole second. The record keeps `started_at` exactly as given, because it is part of the source key and summary key. `close_runs` compares it as a string, which still orders correctly, because `+` sorts before `.`. Task 5 and Task 9 should parse when comparing turn and media times with `started_at` and `ended_at`, not compare strings, or should normalise both sides.
- **`erasure_admin.PERMANENT` is not changed.** Spec section 3 names it, but erasure is deferred. The `stores/mongo.py` docstring now says erasure does not reach `tickets`.
- **File structure additions.** Task 1 adds `tests/test_run_scoped_receipts.py`, which is not in the plan's file list. Task 3 changes the annotation of `build_registry(ticket_system=...)` to `Optional[Any]`.
- **Test counts are not verified.** The counts in each Step 4 come from the test files as written in this draft (18, 94 and 54 new tests). Nobody has run them in this session, so whoever runs each task should confirm them.

---

<!-- drafted as tasks-4-5 -->

### Task 4: The ticket tools: new injects, source_key, kinds, persona, ASSERTED phones, the intake number rule, warranty proof

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py:15-22` (import `ASSERTED`, `VERIFIED`)
- Modify: `src/emotorad_ai/tools/mocks.py:68-69` (add `_TEN_DIGIT_MOBILE`)
- Modify: `src/emotorad_ai/tools/mocks.py:392-398` (add `CLAIMED_FRAME_SOURCE`, `ticket_source_key`, `_record_name` after `_owned_bike`)
- Modify: `src/emotorad_ai/tools/mocks.py:697-768` (`raise_intake_ticket`: description, the number to call back, new injects, `kind="intake"`, `source_key`)
- Modify: `src/emotorad_ai/tools/mocks.py:952-995` (`create_support_ticket`: `conversation_id` becomes a required inject, new optional injects, `source_key`, kind, identity, ASSERTED skips the bike, coverage, customer name)
- Modify: `src/emotorad_ai/tools/mocks.py:1065-1097` (`submit_warranty_proof`: `conversation_id` becomes a required inject, new optional injects, `kind="warranty_proof"`, claims, `proof_url` ignored, returns `ticket_id`)
- Modify: `src/emotorad_ai/agents/base.py:63-64` (`TICKET_PRODUCING_TOOLS` gains `raise_intake_ticket` and `submit_warranty_proof`)
- Modify: `src/emotorad_ai/runtime.py:42` (import `VERIFIED`), `runtime.py:373-375` (add `Runtime._identity_strength`), `runtime.py:1234-1236` (the `identity_strength` fact), `runtime.py:1496-1498` (the safety branch's `ticket_kind` and `identity_strength`)
- Test: `tests/test_ticket_tools_desk.py` (new)
- Test: `tests/test_verification.py` (`IntakeTicketTests`), `tests/test_late_warranty.py` (`ProofSubmissionTests.test_submitting_proof_never_sets_coverage`)

**Interfaces:**
- Consumes: `ToolContext.persona`, `ToolContext.started_at` and receipts scoped by run (Task 1). `MockTicketSystem.create(source_key=None, persona=None, **fields)` with its `source_key` look-up (Task 3). `DeskTicketSystem(store, mode, environment)`, `TicketRouter(desk, mock)` with `.tickets` and `.store` (Task 3). `InMemoryTicketStore()` with `get`, `by_source_key` (Task 2). `tickets.kinds.is_desk_reference` (Task 2). The record keys from `new_record`: `kind`, `urgent`, `identity`, `phone`, `conversation_id`, `started_at`, `cluster_id`, `channel`, `coverage`, `customer_name`, `source_key`, `bike`, `claims` (Task 2).
- Produces:
  - `tools.mocks.ticket_source_key(conversation_id: str, started_at: Optional[str], tool: str, idempotency_key: str) -> str`, which gives `"%s:%s:%s:%s" % (conversation_id, started_at or "", tool, idempotency_key)`.
  - `tools.mocks.CLAIMED_FRAME_SOURCE`.
  - `create_support_ticket`:
    - Injects `("phone", "conversation_id")`.
    - Optional injects `("evidence_seen", "selected_bike", "unlisted_bike", "persona", "started_at", "cluster_id", "channel", "identity_strength", "coverage", "ticket_kind")`.
    - `kind` is `"safety"` only when the injected `ticket_kind == "safety"`. Otherwise it is `"support"`.
  - `raise_intake_ticket`:
    - Injects `("conversation_id",)`.
    - Optional injects `("phone", "identity_strength", "typed_number", "persona", "started_at", "cluster_id", "channel")`.
    - With no number to call back, it raises `ToolError("contact_number_required", ..., remedy="ask the customer for a mobile number we can call")`.
  - `submit_warranty_proof`:
    - Injects `("phone", "conversation_id")`.
    - Optional injects `("persona", "started_at", "cluster_id", "channel", "identity_strength", "coverage")`.
    - Its result data gains `ticket_id`, equal to `reference`.
  - Each tool's `create()` keyword fields are listed in Step 3.
  - `agents.base.TICKET_PRODUCING_TOOLS = ("create_support_ticket", "raise_intake_ticket", "submit_warranty_proof")`.
  - `Runtime._identity_strength(conversation_id: str, resolved: ResolvedIdentity) -> str`.
  - The fact `"identity_strength"` goes in the agent facts dict. The facts `"ticket_kind"` and `"identity_strength"` go in `_raise_safety_ticket`'s `late`.
  - Test helpers in `tests/test_ticket_tools_desk.py`: `TODAY`, `FAKE`, `FAKE_BIKE`, `FRAME`, `STARTED`, `LATER_RUN`, `fake_bikes`, `desk()`, `context()`, `verified()`, `runtime_with()`, `whatsapp()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ticket_tools_desk.py`:

```python
"""The three ticket tools write what a ticket record needs (spec 2026-10-05,
sections 2, 3 and 6): the run, the persona, the kind, the identity and the
facts code worked out, never anything the model chose."""

import unittest
from datetime import date

from emotorad_ai.adapters import VoiceAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, ASSERTED, VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import (
    CREATE_SUPPORT_TICKET,
    RAISE_INTAKE_TICKET,
    SUBMIT_WARRANTY_PROOF,
    MockTicketSystem,
    build_registry,
)
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.tools.verification import VerificationStore

TODAY = date(2026, 10, 5)
FAKE = "+919999999999"
# Ananya's fixture bike, on the fake number, so no test leans on a number
# that looks like someone's.
FAKE_BIKE = dict(fixtures.WARRANTY_RECORDS["+919876543210"][0], mobile=FAKE)
FRAME = FAKE_BIKE["frame_number"]
STARTED = "2026-10-05T09:00:00.000000+00:00"
LATER_RUN = "2026-10-05T11:00:00.000000+00:00"
TICKET = {"category": "battery_charging", "description": "Charger LED stays off; tried another socket.",
          "severity": "normal", "idempotency_key": "k1"}
INTAKE = {"summary": "Charger light stays off.", "stated_name": "Radhika", "stated_contact": "r@example.com",
          "idempotency_key": "i1"}
PROOF = {"frame_number": FRAME, "claimed_purchase_date": "2025-03-14", "purchase_channel": "dealer",
         "proof_url": "https://example.test/invoice.jpg", "idempotency_key": "p1"}
# Every fact the runtime injects into a ticket tool. None may be in a schema.
FACTS = {"phone", "conversation_id", "persona", "started_at", "cluster_id", "channel", "identity_strength",
         "coverage", "typed_number", "ticket_kind", "evidence_seen", "selected_bike", "unlisted_bike"}


def fake_bikes(phone):
    return [dict(FAKE_BIKE)] if phone == FAKE else None


def desk():
    """The router as api.py wires it with Zoho on, over an in-memory store."""
    store = InMemoryTicketStore()
    return TicketRouter(DeskTicketSystem(store, "test", "stage"), MockTicketSystem()), store


def context(phone=FAKE, persona="customer", started_at=STARTED, **facts):
    """A customer's run as the runtime builds it: identity and run on the
    context, conversation facts as late values (Agent._late_facts)."""
    return ToolContext(conversation_id="c1", phone=phone, cluster_id="cl-1", persona=persona, started_at=started_at,
                       late={name: (lambda value=value: value) for name, value in facts.items()})


def verified(**facts):
    return context(identity_strength=VERIFIED, **facts)


def runtime_with(registry, replies=(), **kw):
    llm = ScriptedClaude(list(replies))
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), **kw)
    return runtime, llm


def whatsapp(text, cid="wa-1", phone=FAKE, **metadata):
    return InboundMessage(conversation_id=cid, persona="customer", channel="whatsapp", message_text=text,
                          identity=Identity(cluster_id="cl-1", strength=VERIFIED, phone=phone),
                          entry_metadata=metadata)


class SupportTicketOnDeskTests(unittest.TestCase):
    def setUp(self):
        self.router, self.store = desk()
        self.registry = build_registry(today=TODAY, ticket_system=self.router, warranty_source=fake_bikes)

    def record(self, arguments, ctx):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, arguments, ctx)
        self.assertFalse(is_error(envelope), envelope)
        reference = envelope["data"]["ticket_id"]
        self.assertTrue(is_desk_reference(reference))
        return self.store.get(reference)

    def test_a_verified_customers_ticket_carries_the_run_and_what_code_worked_out(self):
        record = self.record(dict(TICKET), verified(channel="whatsapp", coverage="computed"))
        self.assertEqual((record["kind"], record["identity"], record["urgent"]), ("support", "verified", False))
        self.assertEqual((record["conversation_id"], record["started_at"], record["cluster_id"], record["channel"]),
                         ("c1", STARTED, "cl-1", "whatsapp"))
        self.assertEqual((record["phone"], record["coverage"], record["customer_name"]),
                         (FAKE, "computed", "Ananya Rao"))
        self.assertEqual(record["source_key"], "c1:%s:create_support_ticket:k1" % STARTED)
        self.assertIn(FRAME, str(record["bike"]))

    def test_a_model_battery_safety_ticket_is_urgent_and_still_a_support_ticket(self):
        record = self.record(dict(TICKET, category="battery_safety", severity="critical"), verified())
        self.assertEqual((record["kind"], record["urgent"]), ("support", True))

    def test_only_the_safety_branchs_fact_makes_a_safety_ticket(self):
        key = "safety:c1:%s" % STARTED
        record = self.record(dict(TICKET, category="battery_safety", severity="critical", idempotency_key=key),
                             verified(ticket_kind="safety"))
        self.assertEqual((record["kind"], record["urgent"]), ("safety", True))
        self.assertEqual(record["source_key"], "c1:%s:create_support_ticket:%s" % (STARTED, key))

    def test_the_model_can_set_none_of_the_facts_nor_the_kind(self):
        forged = dict(TICKET, ticket_kind="safety", identity_strength=VERIFIED, coverage="computed",
                      channel="whatsapp", started_at=LATER_RUN, persona="dealer", cluster_id="cl-x",
                      conversation_id="someone-else")
        record = self.record(forged, context())
        self.assertEqual((record["kind"], record["identity"], record["coverage"], record["channel"]),
                         ("support", "unverified", None, None))
        self.assertEqual((record["conversation_id"], record["started_at"], record["cluster_id"]),
                         ("c1", STARTED, "cl-1"))

    def test_one_ticket_per_key_and_run_across_retries_and_registries(self):
        # A second registry has its own receipts, as a second server would.
        other = build_registry(today=TODAY, ticket_system=self.router, warranty_source=fake_bikes)
        first = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        again = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        elsewhere = other.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        reference = first["data"]["ticket_id"]
        self.assertEqual({again["data"]["ticket_id"], elsewhere["data"]["ticket_id"]}, {reference})
        self.assertEqual(self.store.by_source_key("c1:%s:create_support_ticket:k1" % STARTED)["_id"], reference)

    def test_the_same_key_in_a_new_run_is_a_new_ticket(self):
        first = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        later = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified(started_at=LATER_RUN))
        self.assertNotEqual(first["data"]["ticket_id"], later["data"]["ticket_id"])

    def test_a_dealers_or_an_unknown_personas_ticket_stays_on_the_mock(self):
        for persona, phone in (("dealer", "+919000000001"), (None, FAKE)):
            with self.subTest(persona=persona):
                router, store = desk()
                registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
                envelope = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET),
                                         context(phone=phone, persona=persona, identity_strength=VERIFIED))
                reference = envelope["data"]["ticket_id"]
                self.assertRegex(reference, r"^EM-\d{5}$")
                self.assertIn(reference, router.tickets)
                self.assertIsNone(store.by_source_key("c1:%s:create_support_ticket:k1" % STARTED))

    def test_a_caller_id_gets_no_bike_no_name_no_cover_and_is_unverified(self):
        record = self.record(dict(TICKET, frame_number=FRAME), context(identity_strength=ASSERTED, coverage="computed"))
        self.assertEqual((record["identity"], record["customer_name"], record["coverage"]), ("unverified", None, None))
        self.assertNotIn(FRAME, str(record))


class SupportTicketOnTheMockTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY, warranty_source=fake_bikes)

    def call(self, arguments, ctx):
        return self.registry.call(CREATE_SUPPORT_TICKET, arguments, ctx)

    def test_the_mock_keeps_every_field_it_kept_and_the_new_ones(self):
        envelope = self.call(dict(TICKET), verified(channel="whatsapp", coverage="computed"))
        reference = envelope["data"]["ticket_id"]
        self.assertRegex(reference, r"^EM-\d{5}$")
        ticket = self.registry.tickets.tickets[reference]
        self.assertEqual((ticket["phone"], ticket["category"], ticket["severity"], ticket["description"]),
                         (FAKE, "battery_charging", "normal", TICKET["description"]))
        self.assertEqual((ticket["frame_number"], ticket["frame_number_source"], ticket["bike_model"]),
                         (FRAME, None, "EMX Plus"))
        self.assertEqual((ticket["kind"], ticket["conversation_id"], ticket["started_at"], ticket["channel"]),
                         ("support", "c1", STARTED, "whatsapp"))
        self.assertEqual((ticket["identity"], ticket["coverage"], ticket["customer_name"]),
                         ("verified", "computed", "Ananya Rao"))
        self.assertEqual(envelope["data"]["expected_response"], "within 24 hours on working days")

    def test_a_caller_id_is_never_checked_against_the_bikes_on_that_number(self):
        stranger = "DDL32022119302"  # Rohit's fixture bike, not on the fake number
        refused = self.call(dict(TICKET, frame_number=stranger, idempotency_key="k2"), verified())
        self.assertEqual(refused["error"]["code"], "frame_number_not_owned")
        raised = self.call(dict(TICKET, frame_number=stranger, idempotency_key="k3"),
                           context(identity_strength=ASSERTED))
        ticket = self.registry.tickets.tickets[raised["data"]["ticket_id"]]
        self.assertEqual((ticket["frame_number"], ticket["bike_model"], ticket["identity"]),
                         (None, None, "unverified"))

    def test_with_no_strength_given_the_bike_is_still_checked_and_the_ticket_is_unverified(self):
        # A direct caller with no conversation facts: as before, except that
        # nothing says the phone was proved, so no name and no cover.
        ticket = self.registry.tickets.tickets[self.call(dict(TICKET), context())["data"]["ticket_id"]]
        self.assertEqual((ticket["frame_number"], ticket["identity"], ticket["customer_name"]),
                         (FRAME, "unverified", None))

    def test_the_name_comes_only_from_an_oms_record(self):
        app_bike = {"customer_name": "speedy_rider", "frame_number": FRAME, "bike_ref": FRAME,
                    "frame_on_record": True, "product_name": "EMX Plus", "warranty_on_record": False, "in_app": True}
        registry = build_registry(today=TODAY,
                                  warranty_source=lambda phone: [dict(app_bike)] if phone == FAKE else None)
        envelope = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        self.assertIsNone(registry.tickets.tickets[envelope["data"]["ticket_id"]]["customer_name"])

    def test_the_mock_too_keeps_one_ticket_per_key_and_run(self):
        shared = MockTicketSystem()
        first = build_registry(today=TODAY, ticket_system=shared, warranty_source=fake_bikes)
        second = build_registry(today=TODAY, ticket_system=shared, warranty_source=fake_bikes)
        a = first.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        b = second.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        self.assertEqual(a["data"]["ticket_id"], b["data"]["ticket_id"])
        self.assertEqual(len(shared.tickets), 1)


class ModelFacingTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY, verification=VerificationStore(), warranty_source=fake_bikes)

    def test_none_of_the_facts_is_in_any_ticket_tools_schema(self):
        for name in (CREATE_SUPPORT_TICKET, RAISE_INTAKE_TICKET, SUBMIT_WARRANTY_PROOF):
            with self.subTest(name):
                properties = self.registry.specs[name].schema()["input_schema"]["properties"]
                self.assertEqual(set(properties) & FACTS, set())

    def test_an_argument_a_tool_does_not_take_is_refused_and_records_nothing(self):
        calls = ((CREATE_SUPPORT_TICKET, dict(TICKET, priority="high"), verified()),
                 (RAISE_INTAKE_TICKET, dict(INTAKE, priority="high"), context(typed_number="9999999999")),
                 (SUBMIT_WARRANTY_PROOF, dict(PROOF, priority="high"), verified()))
        for name, arguments, ctx in calls:
            with self.subTest(name):
                self.assertEqual(self.registry.call(name, arguments, ctx)["error"]["code"], "tool_exception")
        self.assertEqual(self.registry.tickets.tickets, {})


class IntakeOnDeskTests(unittest.TestCase):
    def setUp(self):
        self.router, self.store = desk()
        self.registry = build_registry(today=TODAY, ticket_system=self.router, verification=VerificationStore())

    def test_an_intake_ticket_calls_back_the_number_typed_in_the_chat(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE),
                                      context(phone=None, typed_number="9999999999", channel="website_chat",
                                              identity_strength=ANONYMOUS))
        record = self.store.get(envelope["data"]["ticket_id"])
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("intake", "unverified", FAKE))
        self.assertEqual((record["channel"], record["started_at"]), ("website_chat", STARTED))
        self.assertEqual(record["source_key"], "c1:%s:raise_intake_ticket:i1" % STARTED)
        self.assertIn("Radhika", str(record["claims"]))
        self.assertIn("r@example.com", str(record["claims"]))

    def test_a_verified_phone_wins_over_a_typed_one(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE), verified(typed_number="9999999998"))
        record = self.store.get(envelope["data"]["ticket_id"])
        self.assertEqual((record["identity"], record["phone"]), ("verified", FAKE))

    def test_a_typed_number_that_is_not_an_indian_mobile_is_no_number(self):
        # _TEN_DIGIT_MOBILE: ten ASCII digits, the first 6 to 9, matched whole.
        for typed in ("1234567890", "99999", "+919999999999"):
            with self.subTest(typed):
                envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE, idempotency_key=typed),
                                              context(phone=None, typed_number=typed))
                self.assertEqual(envelope["error"]["code"], "contact_number_required")


class WarrantyProofTests(unittest.TestCase):
    def test_a_proof_is_recorded_as_claims_and_its_url_never_kept(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router)
        data = registry.call(SUBMIT_WARRANTY_PROOF, dict(PROOF), verified(coverage="no_warranty_record"))["data"]
        self.assertEqual(data["ticket_id"], data["reference"])
        record = store.get(data["ticket_id"])
        self.assertEqual((record["kind"], record["identity"], record["coverage"]),
                         ("warranty_proof", "verified", "no_warranty_record"))
        self.assertEqual(record["source_key"], "c1:%s:submit_warranty_proof:p1" % STARTED)
        self.assertIn("2025-03-14", str(record["claims"]))
        self.assertIn("dealer", str(record["claims"]))
        self.assertNotIn("example.test", str(record))

    def test_the_mock_never_keeps_the_url_either(self):
        registry = build_registry(today=TODAY)
        data = registry.call(SUBMIT_WARRANTY_PROOF, dict(PROOF), verified())["data"]
        ticket = registry.tickets.tickets[data["ticket_id"]]
        self.assertNotIn("proof_url", ticket)
        self.assertNotIn("example.test", str(ticket))
        self.assertEqual((ticket["kind"], ticket["purchase_channel"], ticket["verified"]),
                         ("warranty_proof", "dealer", False))


class ThroughTheRuntimeTests(unittest.TestCase):
    def test_a_warranty_proof_ticket_gets_the_transcript(self):
        registry = build_registry(today=TODAY, warranty_source=lambda phone: None)
        runtime, _ = runtime_with(registry, [
            call_tool(SUBMIT_WARRANTY_PROOF, dict(PROOF), "t1"),
            say("Thanks. A colleague will check your invoice and come back to you."),
        ])
        reply = runtime.handle(whatsapp("I bought it from a dealer in March and never registered it"))
        self.assertTrue(reply.ticket_id)
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"]), ("warranty_proof", "verified"))
        self.assertIn("Customer: I bought it from a dealer in March and never registered it", ticket["transcript"])
        self.assertNotIn("proof_url", ticket)

    def test_a_whatsapp_safety_ticket_is_verified_and_carries_the_bike(self):
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"], ticket["frame_number"], ticket["customer_name"]),
                         ("safety", "verified", FRAME, "Ananya Rao"))
        self.assertEqual(llm.requests, [])

    def test_a_caller_ids_safety_ticket_carries_no_bike_and_is_unverified(self):
        # Before: the caller ID's registered bike went on the ticket, though
        # anyone can send that caller ID.
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(VoiceAdapter(runtime.resolver).to_message(
            {"caller_id": FAKE, "transcript": "my battery is swollen", "call_id": "CALL-1", "confidence": 0.9}))
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"]), ("safety", "unverified"))
        self.assertEqual((ticket["frame_number"], ticket["bike_model"], ticket["customer_name"]), (None, None, None))
        self.assertEqual(llm.requests, [])


class IdentityStrengthTests(unittest.TestCase):
    def runtime(self, phone_resolver=None):
        runtime, _ = runtime_with(build_registry(today=TODAY), phone_resolver=phone_resolver)
        return runtime

    @staticmethod
    def resolved(identity):
        return ResolvedIdentity(persona="customer", method="unverified", identity=identity)

    def test_a_phone_proved_by_a_code_in_this_chat_is_verified(self):
        anonymous = self.resolved(Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertEqual(self.runtime(lambda cid: FAKE)._identity_strength("c1", anonymous), VERIFIED)

    def test_the_channels_phone_keeps_the_channels_strength(self):
        caller = self.resolved(Identity(strength=ASSERTED, phone=FAKE))
        self.assertEqual(self.runtime(lambda cid: FAKE)._identity_strength("c1", caller), ASSERTED)

    def test_nobody_proved_is_anonymous(self):
        anonymous = self.resolved(Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertEqual(self.runtime()._identity_strength("c1", anonymous), ANONYMOUS)
        self.assertEqual(self.runtime(lambda cid: None)._identity_strength("c1", anonymous), ANONYMOUS)


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_verification.py`, replace:

```python
import unittest

from emotorad_ai.tools.mocks import RAISE_INTAKE_TICKET, build_registry
```

with:

```python
import unittest

from emotorad_ai.contract import ASSERTED, VERIFIED
from emotorad_ai.tools.mocks import RAISE_INTAKE_TICKET, build_registry
```

Replace:

```python
    def setUp(self):
        self.registry = build_registry(verification=VerificationStore())
        self.ctx = ToolContext(conversation_id="c1")

    def _raise(self, **kw):
        args = {"summary": "Motor dead, customer says in warranty", "idempotency_key": "k1"}
        args.update(kw)
        return self.registry.call(RAISE_INTAKE_TICKET, args, self.ctx)
```

with:

```python
    def setUp(self):
        self.registry = build_registry(verification=VerificationStore())
        # The runtime's fact: the last mobile the customer typed in this chat
        # (spec 2026-10-05, the intake number rule). Without one, or a
        # verified phone, the tool refuses (the tests at the end).
        self.ctx = ToolContext(conversation_id="c1", late={"typed_number": lambda: "9999999999"})

    def _raise(self, ctx=None, **kw):
        args = {"summary": "Motor dead, customer says in warranty", "idempotency_key": "k1"}
        args.update(kw)
        return self.registry.call(RAISE_INTAKE_TICKET, args, ctx or self.ctx)
```

Replace:

```python
        self.assertEqual(ticket["stated_name"], "Radhika")
        self.assertEqual(ticket["category"], "intake_unverified")
```

with:

```python
        self.assertEqual(ticket["stated_name"], "Radhika")
        self.assertEqual(ticket["category"], "intake_unverified")
        self.assertEqual(ticket["kind"], "intake")
        # The number typed in the chat is the one to call; what they stated stays a claim.
        self.assertEqual(ticket["phone"], "+919999999999")
        self.assertEqual(ticket["stated_contact"], "r@example.com")
```

Replace:

```python
    def test_it_never_asserts_a_warranty_outcome(self):
        data = self._raise()["data"]
        self.assertNotIn("coverage", str(data).lower())
        self.assertIn("verify", data["expected_response"])
```

with:

```python
    def test_it_never_asserts_a_warranty_outcome(self):
        data = self._raise()["data"]
        self.assertNotIn("coverage", str(data).lower())
        self.assertIn("verify", data["expected_response"])

    def test_without_a_number_to_call_it_refuses_and_says_to_ask_for_one(self):
        envelope = self._raise(ctx=ToolContext(conversation_id="c1"))
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "contact_number_required")
        self.assertEqual(envelope["error"]["remedy"], "ask the customer for a mobile number we can call")
        self.assertEqual(self.registry.tickets.tickets, {})

    def test_a_number_the_model_passes_is_never_the_number_to_call(self):
        # stated_contact is the customer's claim, and typed_number is a fact
        # only the runtime sets: one the model sends is dropped.
        envelope = self._raise(ctx=ToolContext(conversation_id="c1"), stated_contact="99999 99999",
                               typed_number="9999999999")
        self.assertEqual(envelope["error"]["code"], "contact_number_required")

    def test_a_verified_phone_is_the_number_to_call(self):
        ctx = ToolContext(conversation_id="c1", phone="+919999999999",
                          late={"identity_strength": lambda: VERIFIED, "typed_number": lambda: "9999999998"})
        data = self._raise(ctx=ctx)["data"]
        ticket = self.registry.tickets.tickets[data["ticket_id"]]
        self.assertEqual((data["identity"], ticket["identity"], ticket["phone"]),
                         ("verified", "verified", "+919999999999"))

    def test_a_caller_id_is_not_a_number_anyone_proved(self):
        ctx = ToolContext(conversation_id="c1", phone="+919999999999", late={"identity_strength": lambda: ASSERTED})
        self.assertEqual(self._raise(ctx=ctx)["error"]["code"], "contact_number_required")
```

In `tests/test_late_warranty.py`, replace:

```python
        data = envelope["data"]
        self.assertFalse(data["coverage_set"])
        self.assertEqual(data["status"], "awaiting_human_verification")
        self.assertIn("Do not tell the customer their warranty is now active", data["note"])
```

with:

```python
        data = envelope["data"]
        self.assertFalse(data["coverage_set"])
        self.assertEqual(data["status"], "awaiting_human_verification")
        self.assertIn("Do not tell the customer their warranty is now active", data["note"])
        # The runtime reads ticket_id (agents.base.TICKET_PRODUCING_TOOLS), and
        # the URL the model passed never reaches the ticket (spec 2026-10-05).
        self.assertEqual(data["ticket_id"], data["reference"])
        submission = self.registry.tickets.tickets[data["ticket_id"]]
        self.assertNotIn("proof_url", submission)
        self.assertNotIn("example.test", str(submission))
        self.assertEqual(submission["kind"], "warranty_proof")
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_tools_desk tests.test_verification tests.test_late_warranty
```

Expected: `FAILED (failures=..., errors=...)`. These are the expected failures:
- `KeyError: 'kind'` on mock tickets.
- `TypeError: 'NoneType' object is not subscriptable` wherever a Desk record is read. Today the tools pass no persona, so the router sends every call to the mock.
- `AssertionError` in the intake refusal tests, because the tool still raises a ticket without a number.
- `KeyError: 'ticket_id'` in `test_submitting_proof_never_sets_coverage`.
- The caller ID safety test fails on `frame_number`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/tools/mocks.py`, replace:

```python
from ..address import AddressError, PincodeDirectory, assemble, parse_address
from ..conversation import address_tokens
```

with:

```python
from ..address import AddressError, PincodeDirectory, assemble, parse_address
from ..contract import ASSERTED, VERIFIED
from ..conversation import address_tokens
```

Replace:

```python
# An Indian pincode. Six digits, first digit 1 to 9.
_PINCODE = re.compile(r"\b[1-9]\d{5}\b")
```

with:

```python
# An Indian pincode. Six digits, first digit 1 to 9.
_PINCODE = re.compile(r"\b[1-9]\d{5}\b")
# An Indian mobile as the runtime keeps a typed one (ConversationState.typed_number,
# from verify_first.find_phone): ten ASCII digits, the first 6 to 9. Matched
# whole with fullmatch, so no word boundary is needed.
_TEN_DIGIT_MOBILE = re.compile(r"[6-9]\d{9}")
```

Replace:

```python
    if len(records) > 1:
        raise ToolError(
            "frame_number_required",
            "This customer owns %d bikes, so the ticket needs a frame number. Ask which bike "
            "they mean and pass its frame number." % len(records),
        )
    return records[0]
```

with:

```python
    if len(records) > 1:
        raise ToolError(
            "frame_number_required",
            "This customer owns %d bikes, so the ticket needs a frame number. Ask which bike "
            "they mean and pass its frame number." % len(records),
        )
    return records[0]


# Where a warranty proof's frame number came from: the customer read it out
# for a registration nobody has checked yet (spec 2026-10-05, section 3).
CLAIMED_FRAME_SOURCE = "given by the customer for registration; not checked"


def ticket_source_key(conversation_id: str, started_at: Optional[str], tool: str, idempotency_key: str) -> str:
    """A ticket's own key (spec 2026-10-05, section 2): the run, the tool and
    the call's idempotency key. The ticket system answers a key it has seen
    with the first ticket, so a retry whose receipt was lost, or a second
    server, never raises a second one, and a new run (a new person after
    restart_for) never gets the last run's."""
    return "%s:%s:%s:%s" % (conversation_id, started_at or "", tool, idempotency_key)


def _record_name(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """The customer's name from an OMS warranty record, or None. An app record
    carries the rider's username instead (tools/amigo.amigo_records marks it
    warranty_on_record False), and a name typed in the chat is a claim:
    neither is ever a ticket's customer name."""
    if not record or record.get("warranty_on_record") is False:
        return None
    return _clean(record.get("customer_name"))
```

Replace:

```python
            "Raise a ticket for a customer whose identity could NOT be confirmed — they could "
            "not complete the one-time code, or could not give a number or order number at "
            "all. Use it so the conversation still ends with the case in a human's queue "
            "rather than nowhere. Everything you pass is what the customer TOLD you, not "
            "anything the system confirmed: pass their words. Do not state or imply any "
            "warranty outcome to the customer — a person verifies who they are before anyone "
            "acts on this.",
```

with:

```python
            "Raise a ticket for a customer whose identity could NOT be confirmed: they could "
            "not complete the one-time code, or could not give a number or order number at "
            "all. Use it so the conversation still ends with the case in a person's queue "
            "rather than nowhere. Everything you pass is what the customer TOLD you, not "
            "anything the system confirmed: pass their words. Do not state or imply any "
            "warranty outcome to the customer: a person verifies who they are before anyone "
            "acts on this. The team calls back on the customer's verified number, or else on "
            "the last mobile number they typed in this chat, never on anything you pass. If "
            "there is neither, this answers contact_number_required: ask the customer for a "
            "mobile number we can call, and call this again once they have typed it.",
```

Replace:

```python
                    "description": "Any phone or email they offered, unverified, exactly as given.",
```

with:

```python
                    "description": (
                        "Any phone or email they offered, unverified, exactly as given. Kept as "
                        "their claim only: it is never the number we call back."
                    ),
```

Replace:

```python
            required=("summary", "idempotency_key"),
            injects=("conversation_id",),
            write=True,
        )
        def raise_intake_ticket(
            conversation_id: str,
            summary: str,
            idempotency_key: str,
            stated_name: str = "",
            stated_contact: str = "",
            evidence: str = "",
        ) -> Dict[str, Any]:
            """A ticket that records claims, deliberately kept apart from the verified one.

            `create_support_ticket` injects a phone from a resolved identity and
            refuses without one, which is right: a ticket carrying a frame number
            and a coverage claim should only exist for someone we know. But that
            left an unverified customer with no path at all — the agent would
            promise to raise something and then raise nothing, which is worse
            than saying no.

            So this is a second, weaker ticket, and the weakness is the point. It
            asserts nothing. Every field is what the customer said, labelled as
            such, and `identity: unverified` travels with it so triage cannot
            mistake it for a confirmed case.
            """
            ticket = tickets.create(
                category="intake_unverified",
                severity="normal",
                identity="unverified",
                conversation_id=conversation_id,
                stated_name=_clean(stated_name) or "not given",
                stated_contact=_clean(stated_contact) or "not given",
                evidence=_clean(evidence) or "none offered",
                description=summary,
            )
            return ok(
                {
                    "ticket_id": ticket["ticket_id"],
                    "status": ticket["status"],
                    "identity": "unverified",
                    "expected_response": "a person will verify the customer before acting on this",
                }
            )
```

with:

```python
            required=("summary", "idempotency_key"),
            injects=("conversation_id",),
            # The number to call back and what the ticket record needs, from
            # the runtime (spec 2026-10-05, section 2). None is in the model's
            # schema, and anything it sends under these names is dropped.
            optional_injects=("phone", "identity_strength", "typed_number", "persona", "started_at",
                              "cluster_id", "channel"),
            write=True,
        )
        def raise_intake_ticket(
            conversation_id: str,
            summary: str,
            idempotency_key: str,
            stated_name: str = "",
            stated_contact: str = "",
            evidence: str = "",
            phone: Optional[str] = None,
            identity_strength: Optional[str] = None,
            typed_number: Optional[str] = None,
            persona: Optional[str] = None,
            started_at: Optional[str] = None,
            cluster_id: Optional[str] = None,
            channel: Optional[str] = None,
        ) -> Dict[str, Any]:
            """A ticket that records claims, deliberately kept apart from the verified one.

            `create_support_ticket` needs a resolved phone and puts a bike and a
            coverage outcome on the ticket, which is right only for someone we
            know. This is the weaker ticket for everyone else, and the weakness
            is the point: it asserts nothing. Every field is what the customer
            said, labelled as such, and `identity` travels with it so triage
            cannot mistake it for a confirmed case.

            It does need a number to call back (the person's decision,
            2026-10-05): the verified phone when there is one, otherwise the
            last Indian mobile the customer typed in this run, which the
            runtime reads from their messages. Never `stated_contact`, which is
            the model's copy of their words. With neither it refuses, so the
            bot asks for a number rather than promising a call nobody can make.
            """
            if phone and identity_strength == VERIFIED:
                callback, identity = phone, "verified"
            elif typed_number and _TEN_DIGIT_MOBILE.fullmatch(typed_number):
                callback, identity = "+91" + typed_number, "unverified"
            else:
                raise ToolError(
                    "contact_number_required",
                    "There is no number to call this customer back on. Ask the customer for a mobile "
                    "number we can call, then raise the ticket again once they have typed it.",
                    remedy="ask the customer for a mobile number we can call",
                )
            ticket = tickets.create(
                source_key=ticket_source_key(conversation_id, started_at, RAISE_INTAKE_TICKET, idempotency_key),
                persona=persona,
                kind="intake",
                conversation_id=conversation_id,
                started_at=started_at,
                cluster_id=cluster_id,
                channel=channel,
                phone=callback,
                identity=identity,
                category="intake_unverified",
                severity="normal",
                stated_name=_clean(stated_name) or "not given",
                stated_contact=_clean(stated_contact) or "not given",
                evidence=_clean(evidence) or "none offered",
                description=summary,
            )
            return ok(
                {
                    "ticket_id": ticket["ticket_id"],
                    "status": ticket["status"],
                    "identity": identity,
                    "expected_response": "a person will verify the customer before acting on this",
                }
            )
```

Replace:

```python
        required=("category", "description", "severity", "idempotency_key"),
        injects=("phone",),
        # Whether any photo or video has arrived in the conversation, from the
        # runtime's facts; absent for a caller that has none (the safety branch).
        optional_injects=("evidence_seen", "selected_bike", "unlisted_bike"),
        write=True,
    )
    def create_support_ticket(
        phone: str,
        category: str,
        description: str,
        severity: str,
        idempotency_key: str,
        frame_number: Optional[str] = None,
        evidence_seen: Optional[bool] = None,
        selected_bike: Optional[str] = None,
        unlisted_bike: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
```

with:

```python
        required=("category", "description", "severity", "idempotency_key"),
        injects=("phone", "conversation_id"),
        # Whether any photo or video has arrived in the conversation, from the
        # runtime's facts; absent for a caller that has none (the safety branch).
        # Then what the ticket record needs (spec 2026-10-05, section 2): the
        # run, the persona (absent: the mock), the channel, how well the phone
        # is known (absent: unverified), the cover code worked out, and the
        # kind, which only the safety branch sets. None is in the model's
        # schema, and anything it sends under these names is dropped.
        optional_injects=("evidence_seen", "selected_bike", "unlisted_bike", "persona", "started_at",
                          "cluster_id", "channel", "identity_strength", "coverage", "ticket_kind"),
        write=True,
    )
    def create_support_ticket(
        phone: str,
        conversation_id: str,
        category: str,
        description: str,
        severity: str,
        idempotency_key: str,
        frame_number: Optional[str] = None,
        evidence_seen: Optional[bool] = None,
        selected_bike: Optional[str] = None,
        unlisted_bike: Optional[Dict[str, Optional[str]]] = None,
        persona: Optional[str] = None,
        started_at: Optional[str] = None,
        cluster_id: Optional[str] = None,
        channel: Optional[str] = None,
        identity_strength: Optional[str] = None,
        coverage: Optional[str] = None,
        ticket_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
```

Replace:

```python
        bike = _unlisted_ticket_bike(frame_number, unlisted_bike) or _owned_bike(
            phone, frame_number, bikes_on, allow_rider_read=True, selected=selected_bike)
        ticket = tickets.create(
            phone=phone,
            category=category,
            description=description,
            severity=severity,
            frame_number=bike.get("frame_number") if bike else None,
            frame_number_source=bike.get("frame_number_source") if bike else None,
            bike_model=bike.get("product_name") if bike else None,
        )
```

with:

```python
        verified = identity_strength == VERIFIED
        if identity_strength == ASSERTED:
            # Caller ID, which anyone can send (identity.resolve_voice): no
            # bike is looked up for it, so the real owner's bike never lands on
            # a stranger's ticket, and no frame number is checked against it.
            bike = None
        else:
            bike = _unlisted_ticket_bike(frame_number, unlisted_bike) or _owned_bike(
                phone, frame_number, bikes_on, allow_rider_read=True, selected=selected_bike)
        ticket = tickets.create(
            source_key=ticket_source_key(conversation_id, started_at, CREATE_SUPPORT_TICKET, idempotency_key),
            persona=persona,
            # Set by code, never from the model's category: only the safety
            # branch's fact makes a safety ticket (spec 2026-10-05, section 3).
            kind="safety" if ticket_kind == "safety" else "support",
            conversation_id=conversation_id,
            started_at=started_at,
            cluster_id=cluster_id,
            channel=channel,
            phone=phone,
            identity="verified" if verified else "unverified",
            category=category,
            description=description,
            severity=severity,
            frame_number=bike.get("frame_number") if bike else None,
            frame_number_source=bike.get("frame_number_source") if bike else None,
            bike_model=bike.get("product_name") if bike else None,
            # A person's cover and name go on a ticket only for a phone
            # someone proved.
            coverage=coverage if verified else None,
            customer_name=_record_name(bike) if verified else None,
        )
```

Replace:

```python
        required=("frame_number", "idempotency_key"),
        injects=("phone",),
        write=True,
    )
    def submit_warranty_proof(
        phone: str,
        frame_number: str,
        idempotency_key: str,
        proof_url: Optional[str] = None,
        claimed_purchase_date: Optional[str] = None,
        purchase_channel: str = "unknown",
    ) -> Dict[str, Any]:
        if purchase_channel not in ("dealer", "website", "marketplace", "unknown"):
            raise ToolError("invalid_channel", "Unknown purchase channel %r." % purchase_channel)

        submission = tickets.create(
            phone=phone,
            category="late_warranty_registration",
            severity="normal",
            description=(
                "Warranty proof submitted for frame %s via %s. Customer states purchase date %s. "
                "REQUIRES HUMAN VERIFICATION against the document before any coverage is set."
                % (frame_number, purchase_channel, claimed_purchase_date or "not given")
            ),
            frame_number=frame_number,
            proof_url=proof_url,
            claimed_purchase_date=claimed_purchase_date,
            verified=False,
        )
        return ok(
            {
                "reference": submission["ticket_id"],
```

with:

```python
        required=("frame_number", "idempotency_key"),
        injects=("phone", "conversation_id"),
        # What the ticket record needs, from the runtime (spec 2026-10-05,
        # section 2). None is in the model's schema, and anything it sends
        # under these names is dropped.
        optional_injects=("persona", "started_at", "cluster_id", "channel", "identity_strength", "coverage"),
        write=True,
    )
    def submit_warranty_proof(
        phone: str,
        conversation_id: str,
        frame_number: str,
        idempotency_key: str,
        proof_url: Optional[str] = None,
        claimed_purchase_date: Optional[str] = None,
        purchase_channel: str = "unknown",
        persona: Optional[str] = None,
        started_at: Optional[str] = None,
        cluster_id: Optional[str] = None,
        channel: Optional[str] = None,
        identity_strength: Optional[str] = None,
        coverage: Optional[str] = None,
    ) -> Dict[str, Any]:
        if purchase_channel not in ("dealer", "website", "marketplace", "unknown"):
            raise ToolError("invalid_channel", "Unknown purchase channel %r." % purchase_channel)

        # `proof_url` stays in the schema and is ignored (spec 2026-10-05,
        # section 3): the model never sees a URL, so one here is one it wrote.
        # The customer's own photo reaches the ticket from the conversation.
        verified = identity_strength == VERIFIED
        submission = tickets.create(
            source_key=ticket_source_key(conversation_id, started_at, SUBMIT_WARRANTY_PROOF, idempotency_key),
            persona=persona,
            kind="warranty_proof",
            conversation_id=conversation_id,
            started_at=started_at,
            cluster_id=cluster_id,
            channel=channel,
            phone=phone,
            identity="verified" if verified else "unverified",
            category="late_warranty_registration",
            severity="normal",
            description=(
                "Warranty proof submitted for frame %s via %s. Customer states purchase date %s. "
                "REQUIRES HUMAN VERIFICATION against the document before any coverage is set."
                % (frame_number, purchase_channel, claimed_purchase_date or "not given")
            ),
            # The customer's claims, labelled as such: nobody has checked the
            # frame number, the date or where it was bought.
            frame_number=frame_number,
            frame_number_source=CLAIMED_FRAME_SOURCE,
            claimed_purchase_date=claimed_purchase_date,
            purchase_channel=purchase_channel,
            coverage=coverage if verified else None,
            # The proof, not the person: nobody has read the document yet.
            verified=False,
        )
        return ok(
            {
                "reference": submission["ticket_id"],
                # The same id under the name the runtime reads, so this ticket
                # gets the transcript and counts as this turn's ticket.
                "ticket_id": submission["ticket_id"],
```

In `src/emotorad_ai/agents/base.py`, replace:

```python
# Tool names whose successful result carries a ticket the customer must be told about.
TICKET_PRODUCING_TOOLS = ("create_support_ticket",)
```

with:

```python
# Tool names whose successful result carries a ticket the customer must be told
# about, and which the run's transcript is attached to (spec 2026-10-05, section 6).
TICKET_PRODUCING_TOOLS = ("create_support_ticket", "raise_intake_ticket", "submit_warranty_proof")
```

In `src/emotorad_ai/runtime.py`, replace:

```python
from .contract import Attachment, InboundMessage, Reply
```

with:

```python
from .contract import VERIFIED, Attachment, InboundMessage, Reply
```

Replace:

```python
    def _remember_lookup(
        self, state: ConversationState, name: str, arguments: Dict[str, Any], envelope: Dict[str, Any]
    ) -> None:
```

with:

```python
    def _identity_strength(self, conversation_id: str, resolved: ResolvedIdentity) -> str:
        """How well the phone a ticket tool is given is known (spec 2026-10-05,
        section 2). The channel's phone keeps the channel's strength: verified
        for WhatsApp, an app sign-in or a code, asserted for caller ID. With
        no channel phone, the tools get the one this conversation proved by a
        code (Agent._late_facts), which is verified. Nobody proved: the
        identity's own strength."""
        identity = resolved.identity
        if identity.phone or self.phone_resolver is None:
            return identity.strength
        return VERIFIED if self.phone_resolver(conversation_id) else identity.strength

    def _remember_lookup(
        self, state: ConversationState, name: str, arguments: Dict[str, Any], envelope: Dict[str, Any]
    ) -> None:
```

Replace:

```python
                # A bike the customer gave because it is not in their list:
                # the ticket tool puts its frame number on a ticket.
                "unlisted_bike": lambda: state.unlisted_bike,
```

with:

```python
                # A bike the customer gave because it is not in their list:
                # the ticket tool puts its frame number on a ticket.
                "unlisted_bike": lambda: state.unlisted_bike,
                # How well the ticket tools know who this is (spec 2026-10-05,
                # section 2): a ticket is verified only on a proven phone, and
                # a caller ID's phone never has a bike looked up for it.
                "identity_strength": lambda: self._identity_strength(message.conversation_id, resolved),
```

Replace:

```python
        late: Dict[str, Any] = {}
        if state.unlisted_bike:
            late["unlisted_bike"] = lambda: state.unlisted_bike
```

with:

```python
        late: Dict[str, Any] = {
            # Set here and nowhere else: the model never chooses a ticket's
            # kind, and anything it sends under this name is dropped.
            "ticket_kind": lambda: "safety",
            "identity_strength": lambda: self._identity_strength(message.conversation_id, resolved),
        }
        if state.unlisted_bike:
            late["unlisted_bike"] = lambda: state.unlisted_bike
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_tools_desk
```

Then the modules whose behaviour this task touches:

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_verification tests.test_late_warranty tests.test_safety_backstop tests.test_idempotency_claims tests.test_tools tests.test_audit_persistence tests.test_unlisted_bike_runtime tests.test_amigo_bikes_without_frame tests.test_ticket_transcript tests.test_evidence_before_ticket
```

Then the whole suite:

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: the suite is green except the one known environmental failure, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`.

These existing tests change on purpose:
- `tests.test_verification.IntakeTicketTests`:
  - `setUp` gives the context a typed number. This keeps `test_it_works_without_a_resolved_phone`, `test_everything_is_recorded_as_claimed_not_confirmed`, `test_absent_details_are_recorded_as_absent_rather_than_blank` and `test_it_never_asserts_a_warranty_outcome` passing under the new number rule.
  - `test_everything_is_recorded_as_claimed_not_confirmed` gains assertions on `kind`, `phone` and `stated_contact`.
  - There are four new tests.
- `tests.test_late_warranty.ProofSubmissionTests.test_submitting_proof_never_sets_coverage` gains assertions on `ticket_id`, `proof_url` and `kind`.

Three existing tests stay green unchanged. Here is why:
- `tests.test_safety_backstop.HazardClaimTests.test_a_claim_after_an_intake_ticket_this_turn_is_left_alone`: the rider is verified, and the new `identity_strength` fact makes that phone the number to call back.
- `tests.test_idempotency_claims` and `tests.test_audit_persistence.KeyScopeTests`: the same conversation, run and key still give one ticket, and two conversations still give two.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/tools/mocks.py src/emotorad_ai/agents/base.py src/emotorad_ai/runtime.py tests/test_ticket_tools_desk.py tests/test_verification.py tests/test_late_warranty.py
git commit -m "feat: ticket tools record the run, persona, kind and identity, and intake needs a number to call" -m "Each ticket tool builds a source_key from the run, the tool and the call's key, and passes kind, persona, run, channel, identity, cover and the OMS name to the ticket system. Only the safety branch's injected ticket_kind makes a safety ticket. A caller ID's phone has no bike looked up and is unverified. raise_intake_ticket calls back the verified phone or the number the customer typed, and refuses with contact_number_required otherwise. submit_warranty_proof records claims, ignores proof_url and returns ticket_id, so its ticket gets the transcript." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: The runtime: facts, new state fields, run-scoped transcript on every turn, close_runs, dealer isolation

**Files:**
- Modify: `src/emotorad_ai/conversation.py:145-147` (the five new state fields)
- Modify: `src/emotorad_ai/conversation.py:212-227` (`forget_bike` clears `lookup_error`)
- Modify: `src/emotorad_ai/runtime.py:140` (import `ascii_digits`, `find_phone`)
- Modify: `src/emotorad_ai/runtime.py:248-249` (`LOOKUP_ERRORS`, `TURN_FACT_FIELDS`, `coverage_fact`, `_ticket_bike`)
- Modify: `src/emotorad_ai/runtime.py:386-387` (`_remember_lookup` keeps `lookup_error`; new `_remember_coverage`)
- Modify: `src/emotorad_ai/runtime.py:436-438`, `:465-467`, `:492-494` (`_handle`: snapshot, merge, close runs, attach)
- Modify: `src/emotorad_ai/runtime.py:496-508` (`_attach_transcript` run-scoped, on every turn of the run; new `_speaker_owns_run`, `_close_earlier_runs`)
- Modify: `src/emotorad_ai/runtime.py:510-536` (`_merge_onto_fresh` carries the turn's facts)
- Modify: `src/emotorad_ai/runtime.py:587-590` (`_node_prepare` records `typed_number`; new `_note_typed_number`)
- Modify: `src/emotorad_ai/runtime.py:1242-1261` (facts dict; the post-loop lookup)
- Modify: `src/emotorad_ai/runtime.py:1496-1503` (the safety branch's late facts)
- Test: `tests/test_runtime_tickets.py` (new)

**Interfaces:**
- Consumes:
  - From Task 4: `Runtime._identity_strength`, the tools' optional injects `"started_at"`, `"channel"`, `"coverage"`, `"typed_number"`, and `TICKET_PRODUCING_TOOLS`. The test helpers `TODAY`, `FAKE`, `FRAME`, `desk`, `fake_bikes`, `runtime_with`, `whatsapp`.
  - From Task 3: `close_runs(conversation_id, new_started_at)` and `attach_transcript(ticket_id, transcript)` on `MockTicketSystem`, `DeskTicketSystem` and `TicketRouter`. `DeskTicketSystem.attach_transcript` increases the record's `wake`.
  - From Task 2: `InMemoryTicketStore.get`, `by_source_key`, `listing`, and the record keys `ended_at`, `wake`.
  - From Task 1: `ToolContext.persona` filled on the safety branch's context and in `Agent.run`.
  - `verify_first.ascii_digits`, `verify_first.find_phone`.
- Produces:
  - `ConversationState` fields:
    - `typed_number: Optional[str] = None`
    - `lookup_error: Optional[str] = None`
    - `awaiting_callback: Optional[str] = None`
    - `callback_asks: int = 0`
    - `last_code_phone: Optional[str] = None`
  - `runtime.LOOKUP_ERRORS = ("no_warranty_record", "oms_unavailable")`.
  - `runtime.TURN_FACT_FIELDS = ("typed_number", "lookup_error", "awaiting_callback", "callback_asks", "last_code_phone")`.
  - `runtime.coverage_fact(state: ConversationState, resolved: Optional[ResolvedIdentity] = None) -> Optional[str]`.
  - New `Runtime` methods:
    - `_remember_coverage(state, envelope)`
    - `_note_typed_number(message, state)`
    - `_speaker_owns_run(state, resolved) -> bool`
    - `_close_earlier_runs(state)`
  - Changed `Runtime` methods:
    - `_attach_transcript(state, reply, resolved)`
    - `_merge_onto_fresh(ours, this_turn, reply, looked_up=False, loaded=None)`
  - The facts `"started_at"`, `"channel"`, `"coverage"` and `"typed_number"` go in both the agent facts dict and the safety branch's late facts.
  - The event `close_runs_failed` (`error=` class name).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime_tickets.py`:

```python
"""The runtime's side of real tickets (spec 2026-10-05, part 2): the facts
the ticket tools get, the state kept for them, the run's transcript on every
turn of the run, where earlier runs end, and dealers kept off Desk."""

import unittest

from emotorad_ai.adapters import DealerWhatsAppAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import ConversationConflict, ConversationState, InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime, coverage_fact
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import (
    CREATE_SUPPORT_TICKET,
    LOOKUP_WARRANTY_RECORD,
    RAISE_INTAKE_TICKET,
    MockTicketSystem,
    build_registry,
)
from emotorad_ai.tools.registry import ToolError, err, ok
from emotorad_ai.tools.verification import VERIFIED_TTL_SECONDS, VerificationStore
from tests.test_ticket_tools_desk import FAKE, FRAME, TODAY, desk, fake_bikes, runtime_with, whatsapp

# Two people need two numbers with bikes on record: the fixture numbers the
# verify-first tests use (tests/test_origin_runtime.py), invented, not real.
FIRST = fixtures.PHONE_AMIIGO_TEST_RIDER  # two fixture bikes
SECOND = "+919876543210"                 # one fixture bike


class WebChat:
    """A website visitor through verify first (tests.test_verify_first.Chat),
    on a ticket system the test chooses."""

    def __init__(self, tickets=None, replies=(), clock=None):
        self.store = VerificationStore(clock=clock) if clock else VerificationStore()
        self.registry = build_registry(verification=self.store, today=TODAY, ticket_system=tickets)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            self_service_identity=True, phone_resolver=self.store.verified_phone,
            otp_verified_at=self.store.verified_on, verify_first=True,
        )

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

    def state(self):
        return self.conversations.peek("c1")


def two_people(tickets=None):
    """Person one verifies, picks a bike and reports a swollen pack. Their
    proof lapses; person two, on the same browser, writes, verifies (the
    conversation restarts for them) and reports smoke. Returns the chat,
    both safety replies and person one's run start."""
    now = [0.0]
    chat = WebChat(tickets=tickets, replies=[say("Is the charger light on?")] * 4, clock=lambda: now[0])
    chat.say("my battery isn't charging")
    chat.say(FIRST[3:])
    chat.say(chat.store.pending_code("c1"))
    chat.say("2")
    first = chat.say("my battery is swollen")
    first_started = chat.state().started_at
    now[0] += VERIFIED_TTL_SECONDS + 1  # person one's proof lapses
    chat.say("hello, my motor is making a noise")
    chat.say(SECOND[3:])
    chat.say(chat.store.pending_code("c1"))
    chat.say("yes")
    second = chat.say("there is smoke coming from the battery")
    return chat, first, second, first_started


def web(runtime, text, cid="web-1", **metadata):
    """An anonymous website visitor, with no verify-first step."""
    return runtime.handle(InboundMessage(
        conversation_id=cid, persona="customer", channel="website_chat", message_text=text,
        identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), entry_metadata=metadata))


class ClosingTickets(MockTicketSystem):
    """The mock, noting every close_runs call."""

    def __init__(self):
        super().__init__()
        self.closed = []

    def close_runs(self, conversation_id, new_started_at):
        self.closed.append((conversation_id, new_started_at))


class BrokenClose(MockTicketSystem):
    def close_runs(self, conversation_id, new_started_at):
        raise RuntimeError("store down")


class OtherServerFirst(InMemoryConversationStore):
    """Copies on every load, as a database does. The next save finds that
    another server saved first, having made the change `change` makes."""

    def __init__(self, change):
        super().__init__()
        self.change = change

    def get(self, conversation_id):
        return ConversationState.from_json(super().get(conversation_id).to_json())

    def save(self, state):
        if self.change is not None:
            change, self.change = self.change, None
            stored = super().get(state.conversation_id)
            change(stored)
            stored.version += 1
            raise ConversationConflict("another server saved first")
        super().save(state)


class RunTranscriptTests(unittest.TestCase):
    def test_the_second_persons_ticket_carries_none_of_the_first_persons_turns(self):
        chat, first, second, _ = two_people()
        self.assertTrue(first.ticket_id and second.ticket_id)
        self.assertNotEqual(first.ticket_id, second.ticket_id)
        transcript = chat.registry.tickets.tickets[second.ticket_id]["transcript"]
        self.assertIn("Customer: there is smoke coming from the battery", transcript)
        for earlier in ("my battery isn't charging", "my battery is swollen", "hello, my motor is making a noise"):
            self.assertNotIn(earlier, transcript)

    def test_the_first_persons_ticket_gets_none_of_the_second_persons_turns(self):
        chat, first, _, _ = two_people()
        transcript = chat.registry.tickets.tickets[first.ticket_id]["transcript"]
        self.assertIn("Customer: my battery is swollen", transcript)
        # Written after person one's proof lapsed: not theirs to read.
        for later in ("hello, my motor is making a noise", "there is smoke coming from the battery"):
            self.assertNotIn(later, transcript)

    def test_a_ticket_gets_the_runs_later_turns_too(self):
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, [say("Thank you for letting me know.")] * 3)
        first = runtime.handle(whatsapp("my battery is swollen"))
        runtime.handle(whatsapp("I have moved it outside"))
        transcript = registry.tickets.tickets[first.ticket_id]["transcript"]
        self.assertIn("Customer: my battery is swollen", transcript)
        self.assertIn("Customer: I have moved it outside", transcript)


class CloseRunsTests(unittest.TestCase):
    def test_each_run_is_closed_once_on_its_first_turn(self):
        tickets = ClosingTickets()
        chat, _, _, first_started = two_people(tickets)
        second_started = chat.state().started_at
        self.assertNotEqual(first_started, second_started)
        self.assertEqual(tickets.closed, [("c1", first_started), ("c1", second_started)])

    def test_the_first_persons_desk_record_ends_where_the_second_run_starts(self):
        router, store = desk()
        chat, first, second, first_started = two_people(router)
        second_started = chat.state().started_at
        self.assertTrue(is_desk_reference(first.ticket_id) and is_desk_reference(second.ticket_id))
        theirs, ours = store.get(first.ticket_id), store.get(second.ticket_id)
        self.assertEqual((theirs["started_at"], theirs["ended_at"]), (first_started, second_started))
        self.assertEqual((ours["started_at"], ours["ended_at"]), (second_started, None))

    def test_a_failed_close_is_logged_and_the_turn_goes_on(self):
        registry = build_registry(today=TODAY, ticket_system=BrokenClose(), warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        self.assertTrue(reply.ticket_id)
        failed = [e for e in runtime.log.events if e["event"] == "close_runs_failed"]
        self.assertEqual([e["error"] for e in failed], ["RuntimeError"])


class DeskFactsTests(unittest.TestCase):
    def test_a_dealers_safety_report_records_no_desk_ticket_and_calls_no_model(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(DealerWhatsAppAdapter(runtime.resolver).to_message(
            {"from": "919000000001", "text": "a customer's battery is swollen here in the shop",
             "conversation_id": "d1"}))
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertIn(reply.ticket_id, router.tickets)
        self.assertFalse(is_desk_reference(reply.ticket_id))
        started = runtime.conversations.peek("d1").started_at
        self.assertIsNone(store.by_source_key("d1:%s:create_support_ticket:safety:d1:%s" % (started, started)))
        self.assertEqual(store.listing("test"), [])
        self.assertEqual(llm.requests, [])

    def test_a_customers_safety_report_is_a_desk_safety_record_with_the_runs_facts(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        self.assertTrue(is_desk_reference(reply.ticket_id))
        record = store.get(reply.ticket_id)
        started = runtime.conversations.peek("wa-1").started_at
        self.assertEqual((record["kind"], record["urgent"], record["identity"]), ("safety", True, "verified"))
        self.assertEqual((record["conversation_id"], record["started_at"], record["channel"], record["cluster_id"]),
                         ("wa-1", started, "whatsapp", "cl-1"))
        self.assertEqual((record["phone"], record["coverage"], record["customer_name"]),
                         (FAKE, "computed", "Ananya Rao"))
        self.assertEqual(record["source_key"], "wa-1:%s:create_support_ticket:safety:wa-1:%s" % (started, started))
        self.assertEqual(llm.requests, [])

    def test_every_turn_of_the_run_wakes_its_desk_record(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, [say("Thank you. Please keep it outside.")] * 3)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        self.assertEqual(store.get(reply.ticket_id)["wake"], 1)
        runtime.handle(whatsapp("I have moved it outside"))
        self.assertEqual(store.get(reply.ticket_id)["wake"], 2)

    def test_an_agents_ticket_gets_the_runs_facts(self):
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, [
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "t1"),
            say("I have raised a ticket for the team."),
        ])
        # The customer sent a photo earlier: a fault ticket needs one (test_evidence_before_ticket).
        runtime.conversations.get("wa-1").evidence_seen = True
        reply = runtime.handle(whatsapp("the charger light stays off", pinned_agent=BATTERY))
        ticket = registry.tickets.tickets[reply.ticket_id]
        started = runtime.conversations.peek("wa-1").started_at
        self.assertEqual((ticket["started_at"], ticket["channel"], ticket["coverage"], ticket["identity"]),
                         (started, "whatsapp", "computed", "verified"))


class TypedNumberTests(unittest.TestCase):
    def runtime(self, replies=4):
        runtime, _ = runtime_with(build_registry(today=TODAY), [say("Is the charger light on?")] * replies)
        return runtime

    def test_the_latest_indian_mobile_typed_in_the_run_is_kept(self):
        runtime = self.runtime()
        web(runtime, "my battery won't charge, my number is 99999 99999", pinned_agent=BATTERY)
        self.assertEqual(runtime.conversations.peek("web-1").typed_number, "9999999999")
        # Devanagari digits are read as ASCII first (verify_first.ascii_digits).
        web(runtime, "मेरा नंबर ९९९९९ ९९९९८ है", pinned_agent=BATTERY)
        self.assertEqual(runtime.conversations.peek("web-1").typed_number, "9999999998")
        web(runtime, "the light is off", pinned_agent=BATTERY)
        self.assertEqual(runtime.conversations.peek("web-1").typed_number, "9999999998")

    def test_a_number_that_is_not_an_indian_mobile_is_not_kept(self):
        runtime = self.runtime()
        web(runtime, "my Spanish number is +34 612 345 678", pinned_agent=BATTERY)
        web(runtime, "or try 12345 67890", pinned_agent=BATTERY)
        self.assertIsNone(runtime.conversations.peek("web-1").typed_number)

    def test_a_spent_code_is_never_read_as_a_number(self):
        runtime = self.runtime()
        web(runtime, "hello", pinned_agent=BATTERY)
        # Planted as _remember_lookup records a code verify_identity accepted.
        runtime.conversations.peek("web-1").consumed_codes.append("9999999999")
        web(runtime, "9999999999", pinned_agent=BATTERY)
        self.assertIsNone(runtime.conversations.peek("web-1").typed_number)

    def test_a_dealer_typing_a_number_keeps_nothing(self):
        runtime, _ = runtime_with(build_registry(today=TODAY), [say("Noted.")] * 2)
        runtime.handle(DealerWhatsAppAdapter(runtime.resolver).to_message(
            {"from": "919000000001", "text": "my customer's number is 99999 99999", "conversation_id": "d1"}))
        self.assertIsNone(runtime.conversations.peek("d1").typed_number)

    def test_an_unverified_visitors_intake_ticket_calls_back_the_typed_number(self):
        verification = VerificationStore()
        registry = build_registry(today=TODAY, verification=verification)
        runtime, _ = runtime_with(registry, [
            call_tool(RAISE_INTAKE_TICKET, {"summary": "Charger light stays off.", "stated_name": "Radhika",
                                            "idempotency_key": "intake-1"}, "t1"),
            say("I have passed this to our support team."),
        ], self_service_identity=True, phone_resolver=verification.verified_phone)
        reply = web(runtime, "my battery won't charge and the code never came. my number is 99999 99999",
                    pinned_agent=BATTERY)
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"], ticket["phone"]), ("intake", "unverified", FAKE))
        self.assertIn("[phone]", ticket["transcript"])
        self.assertNotIn("99999", ticket["transcript"])


class NewStateFieldTests(unittest.TestCase):
    FIELDS = {"typed_number": "9999999999", "lookup_error": "oms_unavailable", "awaiting_callback": "handover",
              "callback_asks": 1, "last_code_phone": "9999999998"}

    def test_they_round_trip_and_an_older_state_loads_without_them(self):
        state = ConversationState("c1", **self.FIELDS)
        self.assertEqual(ConversationState.from_json(state.to_json()), state)
        old = ConversationState.from_json('{"conversation_id": "c1"}')
        self.assertEqual((old.typed_number, old.lookup_error, old.awaiting_callback, old.callback_asks,
                          old.last_code_phone), (None, None, None, 0, None))

    def test_a_new_person_starts_with_none_of_them(self):
        state = ConversationState("c1", turns=3, user_key="PHONE#" + FIRST, **self.FIELDS)
        state.restart_for("PHONE#" + SECOND, "2026-10-05T10:00:00.000000+00:00")
        self.assertEqual((state.typed_number, state.lookup_error, state.awaiting_callback, state.callback_asks,
                          state.last_code_phone), (None, None, None, 0, None))

    def test_forgetting_the_bike_forgets_its_lookup_and_its_failure_not_the_number(self):
        state = ConversationState("c1", selected_frame=FRAME, coverage_result=ok({"bikes": []}), **self.FIELDS)
        state.forget_bike()
        self.assertIsNone(state.coverage_result)
        self.assertIsNone(state.lookup_error)
        self.assertEqual(state.typed_number, "9999999999")


class LookupErrorTests(unittest.TestCase):
    def test_an_oms_outage_is_kept_until_a_lookup_works(self):
        outage = [True]

        def source(phone):
            if outage[0]:
                raise ToolError("oms_unavailable", "The warranty system is not responding.", retryable=True)
            return fake_bikes(phone)

        runtime, _ = runtime_with(build_registry(today=TODAY, warranty_source=source), [
            call_tool(LOOKUP_WARRANTY_RECORD, {}, "t1"), say("Is the charger light on?"),
            call_tool(LOOKUP_WARRANTY_RECORD, {}, "t2"), say("Thanks. Is it plugged in at the wall?"),
        ])
        runtime.handle(whatsapp("my battery won't charge", pinned_agent=BATTERY))
        state = runtime.conversations.peek("wa-1")
        self.assertEqual(state.lookup_error, "oms_unavailable")
        self.assertEqual(coverage_fact(state), "oms_unavailable")
        outage[0] = False
        runtime.handle(whatsapp("the light is off", pinned_agent=BATTERY))
        state = runtime.conversations.peek("wa-1")
        self.assertIsNone(state.lookup_error)
        self.assertEqual(coverage_fact(state), "computed")

    def test_only_the_two_lookup_failures_are_kept(self):
        runtime, _ = runtime_with(build_registry(today=TODAY))
        state = ConversationState("c1")
        runtime._remember_lookup(state, LOOKUP_WARRANTY_RECORD, {}, err("no_warranty_record", "No bike."))
        self.assertEqual(state.lookup_error, "no_warranty_record")
        runtime._remember_lookup(state, LOOKUP_WARRANTY_RECORD, {},
                                 err("tool_exception", "TypeError: x", retryable=True))
        self.assertEqual(state.lookup_error, "no_warranty_record")
        runtime._remember_lookup(state, LOOKUP_WARRANTY_RECORD, {}, ok({"bike_count": 0, "bikes": []}))
        self.assertIsNone(state.lookup_error)


class CoverageFactTests(unittest.TestCase):
    ONE = {"frame_number": FRAME, "bike_ref": FRAME, "coverage_status": "computed"}
    OTHER = {"frame_number": "DDL32022119302", "bike_ref": "DDL32022119302",
             "coverage_status": "purchase_date_missing"}

    @staticmethod
    def looked_up(*bikes):
        # The envelope lookup_warranty_record answers with (tools/mocks.py).
        return ok({"customer_name": "Ananya Rao", "bike_count": len(bikes), "bikes": list(bikes)},
                  freshness_seconds=300)

    @staticmethod
    def resolved(bikes=(), method="verified", error=None):
        return ResolvedIdentity(persona="customer", method=method, identity=Identity(strength=VERIFIED, phone=FAKE),
                                bikes=list(bikes), error=error)

    def test_the_only_bike_of_the_last_lookup(self):
        self.assertEqual(coverage_fact(ConversationState("c1", coverage_result=self.looked_up(self.ONE))), "computed")

    def test_the_chosen_bike_of_several(self):
        state = ConversationState("c1", coverage_result=self.looked_up(self.ONE, self.OTHER),
                                  selected_frame="DDL32022119302")
        self.assertEqual(coverage_fact(state), "purchase_date_missing")

    def test_several_bikes_and_none_chosen_says_nothing(self):
        self.assertIsNone(coverage_fact(ConversationState("c1", coverage_result=self.looked_up(self.ONE, self.OTHER))))

    def test_this_turns_identity_lookup_when_the_agent_made_none(self):
        self.assertEqual(coverage_fact(ConversationState("c1"), self.resolved([self.ONE])), "computed")

    def test_the_last_lookup_wins_over_this_turns_identity_lookup(self):
        state = ConversationState("c1", coverage_result=self.looked_up(
            dict(self.ONE, coverage_status="computed_from_registration")))
        self.assertEqual(coverage_fact(state, self.resolved([self.ONE])), "computed_from_registration")

    def test_a_failure_only_when_no_bike_is_known(self):
        self.assertEqual(coverage_fact(ConversationState("c1", lookup_error="oms_unavailable")), "oms_unavailable")
        no_record = self.resolved(method="no_warranty_record", error="no_warranty_record")
        self.assertEqual(coverage_fact(ConversationState("c1"), no_record), "no_warranty_record")
        broken = self.resolved(method="oms_error", error="tool_exception")
        self.assertIsNone(coverage_fact(ConversationState("c1"), broken))

    def test_an_unlisted_bike_takes_no_listed_bikes_cover(self):
        state = ConversationState("c1", coverage_result=self.looked_up(self.ONE),
                                  unlisted_bike={"frame_number": "EMXP2026009999", "model": "EMX Plus"})
        self.assertIsNone(coverage_fact(state, self.resolved([self.ONE])))


class MergeTests(unittest.TestCase):
    def test_a_merged_turn_keeps_what_it_changed_and_the_other_servers_rest(self):
        def other_server(state):
            state.last_code_phone, state.callback_asks, state.lookup_error = "9999999997", 1, "oms_unavailable"

        store = OtherServerFirst(other_server)
        runtime, llm = runtime_with(build_registry(today=TODAY, warranty_source=fake_bikes), conversations=store)
        reply = runtime.handle(whatsapp("my battery is swollen, my number is 99999 99998"))
        saved = store.peek("wa-1")
        self.assertIn("merged_after_conflict", saved.transitions)
        self.assertTrue(reply.ticket_id)
        self.assertEqual(saved.typed_number, "9999999998")  # this turn's
        self.assertEqual((saved.last_code_phone, saved.callback_asks, saved.lookup_error),
                         ("9999999997", 1, "oms_unavailable"))  # the other server's
        self.assertEqual(llm.requests, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_runtime_tickets
```

Expected: `ERROR: test_runtime_tickets (unittest.loader._FailedTest)` with `ImportError: cannot import name 'coverage_fact' from 'emotorad_ai.runtime'`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/conversation.py`, replace:

```python
    # The turn that asked for DELETE: only the next one may answer it.
    erasure_turn: Optional[int] = None
    channel: Optional[str] = None
```

with:

```python
    # The turn that asked for DELETE: only the next one may answer it.
    erasure_turn: Optional[int] = None
    # The latest Indian mobile the customer typed in this run, ten digits
    # (Runtime._note_typed_number): the number a ticket is called back on when
    # nobody proved one (spec 2026-10-05, section 6). Never the model's, and it
    # goes with the run.
    typed_number: Optional[str] = None
    # The run's last failed warranty look-up, "no_warranty_record" or
    # "oms_unavailable", for a ticket's coverage: coverage_result keeps only a
    # look-up that worked. Cleared by one that works, and with the bike.
    lookup_error: Optional[str] = None
    # The call-back gate (spec 2026-10-05, section 6): "handover" or "safety"
    # while it waits for a number to call, and the asks it has made.
    awaiting_callback: Optional[str] = None
    callback_asks: int = 0
    # The last number a verification code was sent to, for a lock-out ticket.
    last_code_phone: Optional[str] = None
    channel: Optional[str] = None
```

Replace:

```python
    def forget_bike(self) -> None:
        """Back to before a bike was chosen (navigation, spec 2026-10-02). The
        bike goes, with any unlisted one and its confirmation, the agent, and
        what was learnt about that bike, which does not hold for another: its
        warranty lookup, the evidence seen and the asks for it. Orders placed
        stay: they were placed. The topic is the caller's to keep or clear."""
        if self.selected_frame:
            self.transitions.append("bike_forgotten:%s" % self.selected_frame)
        self.selected_frame = None
        self.selected_bike_label = None
        self.unlisted_bike, self.unlisted_asks, self.bike_confirmation = None, 0, None
        self.agent = None
        self.sub_category = None
        self.coverage_result = None
        self.evidence_seen = False
```

with:

```python
    def forget_bike(self) -> None:
        """Back to before a bike was chosen (navigation, spec 2026-10-02). The
        bike goes, with any unlisted one and its confirmation, the agent, and
        what was learnt about that bike, which does not hold for another: its
        warranty lookup and a failed one, the evidence seen and the asks for
        it. Orders placed stay: they were placed. A number the customer typed
        stays: it is theirs, not the bike's. The topic is the caller's to keep
        or clear."""
        if self.selected_frame:
            self.transitions.append("bike_forgotten:%s" % self.selected_frame)
        self.selected_frame = None
        self.selected_bike_label = None
        self.unlisted_bike, self.unlisted_asks, self.bike_confirmation = None, 0, None
        self.agent = None
        self.sub_category = None
        self.coverage_result = None
        self.lookup_error = None
        self.evidence_seen = False
```

In `src/emotorad_ai/runtime.py`, replace:

```python
from .verify_first import CONFIRMED, NUMBER, VerifyFirst
```

with:

```python
from .verify_first import CONFIRMED, NUMBER, VerifyFirst, ascii_digits, find_phone
```

Replace:

```python
# The agents whose asks to see something are fault evidence (video first).
_FAULT_AGENTS = (BATTERY_SUPPORT, MOTOR_SUPPORT, NARROW_SUPPORT)
```

with:

```python
# The agents whose asks to see something are fault evidence (video first).
_FAULT_AGENTS = (BATTERY_SUPPORT, MOTOR_SUPPORT, NARROW_SUPPORT)

# Look-up failures a ticket's coverage names (spec 2026-10-05, section 3).
# Any other failure says nothing about the customer's cover.
LOOKUP_ERRORS = ("no_warranty_record", "oms_unavailable")

# What a turn reads from the customer or a look-up, carried when the turn
# loses a save race and changed it (Runtime._merge_onto_fresh).
TURN_FACT_FIELDS = ("typed_number", "lookup_error", "awaiting_callback", "callback_asks", "last_code_phone")


def _ticket_bike(bikes: Sequence[Dict[str, Any]], selected: Optional[str]) -> Optional[Dict[str, Any]]:
    """The chosen bike by its reference, or the only bike; None with several
    and none chosen."""
    if selected:
        return next((bike for bike in bikes if bike_ref(bike) == selected), None)
    return bikes[0] if len(bikes) == 1 else None


def coverage_fact(state: ConversationState, resolved: Optional[ResolvedIdentity] = None) -> Optional[str]:
    """What a ticket says about cover, worked out in code (spec 2026-10-05,
    section 3).

    The coverage_status of the bike the ticket is about, the chosen bike or
    the only one: from the conversation's last warranty look-up first, then
    from this turn's identity look-up (the safety branch fires before any
    agent has looked). Failing both, the run's last look-up error, then this
    turn's. None when nothing is known, and always None for a bike the
    customer gave as not in their list: the listed bikes' cover says nothing
    about it.
    """
    if state.unlisted_bike:
        return None
    looked_up = ((state.coverage_result or {}).get("data") or {}).get("bikes") or []
    for bikes in (looked_up, list(resolved.bikes) if resolved is not None else []):
        bike = _ticket_bike(bikes, state.selected_frame)
        if bike is not None and bike.get("coverage_status"):
            return bike["coverage_status"]
    if state.lookup_error:
        return state.lookup_error
    if resolved is not None and resolved.error in LOOKUP_ERRORS:
        return resolved.error
    return None
```

Replace:

```python
        if name == LOOKUP_WARRANTY_RECORD and not is_error(envelope):
            state.coverage_result = envelope
        if name == PLACE_REPLACEMENT_ORDER and not is_error(envelope):
```

with:

```python
        if name == LOOKUP_WARRANTY_RECORD:
            self._remember_coverage(state, envelope)
        if name == PLACE_REPLACEMENT_ORDER and not is_error(envelope):
```

Replace:

```python
    # -- entry point ---------------------------------------------------------

    def handle(self, message: InboundMessage) -> Reply:
```

with:

```python
    @staticmethod
    def _remember_coverage(state: ConversationState, envelope: Dict[str, Any]) -> None:
        """A warranty look-up's answer. One that worked is the conversation's
        cover and clears any failure; a failure that says something about the
        customer (LOOKUP_ERRORS) is kept for a ticket's coverage, because
        coverage_result keeps only answers that worked. Other failures change
        nothing."""
        if not is_error(envelope):
            state.coverage_result = envelope
            state.lookup_error = None
        elif (envelope.get("error") or {}).get("code") in LOOKUP_ERRORS:
            state.lookup_error = envelope["error"]["code"]

    # -- entry point ---------------------------------------------------------

    def handle(self, message: InboundMessage) -> Reply:
```

Replace:

```python
            history_mark, log_mark = len(state.history), len(self.log.events)
            coverage_loaded = state.coverage_result
```

with:

```python
            history_mark, log_mark = len(state.history), len(self.log.events)
            coverage_loaded = state.coverage_result
            # What the turn may change that a merge must not lose or overwrite.
            facts_loaded = {name: getattr(state, name) for name in TURN_FACT_FIELDS}
```

Replace:

```python
                        state = self._merge_onto_fresh(
                            state, this_turn, reply, looked_up=state.coverage_result != coverage_loaded
                        )
```

with:

```python
                        state = self._merge_onto_fresh(
                            state, this_turn, reply, looked_up=state.coverage_result != coverage_loaded,
                            loaded=facts_loaded,
                        )
```

Replace:

```python
            self.log.emit("transcript_write_failed", cid, error=str(exc))
        self._attach_transcript(cid, reply)
        return reply

    def _attach_transcript(self, conversation_id: str, reply: Reply) -> None:
        """Every ticket, from an agent or the safety branch, gets the whole
        thread, this turn included, so a person never starts from nothing."""
        tickets = getattr(self.registry, "tickets", None)
        if not reply.ticket_id or not hasattr(tickets, "attach_transcript"):
            return
        try:
            tickets.attach_transcript(reply.ticket_id, render_transcript(self.conversations.transcript(conversation_id)))
        except Exception as exc:  # the ticket exists either way; say the thread did not reach it
            self.log.emit(
                "transcript_attach_failed", conversation_id,
                ticket_id=reply.ticket_id, error="%s: %s" % (type(exc).__name__, exc),
            )
```

with:

```python
            self.log.emit("transcript_write_failed", cid, error=str(exc))
        self._close_earlier_runs(state)
        self._attach_transcript(state, reply, resolved)
        return reply

    def _close_earlier_runs(self, state: ConversationState) -> None:
        """On a run's first turn (a fresh state, or restart_for), the ticket
        seam marks where this conversation's earlier runs ended, so their
        tickets take none of this run's turns or files (spec 2026-10-05,
        section 3)."""
        tickets = getattr(self.registry, "tickets", None)
        if state.turns != 1 or not state.started_at or not hasattr(tickets, "close_runs"):
            return
        try:
            tickets.close_runs(state.conversation_id, state.started_at)
        except Exception as exc:  # the turn happened; the log says the earlier runs stay open
            self.log.emit("close_runs_failed", state.conversation_id, error=type(exc).__name__)

    def _speaker_owns_run(self, state: ConversationState, resolved: Optional[ResolvedIdentity]) -> bool:
        """Whether this turn comes from the person the run belongs to: the run
        has no proven owner, or this turn proved the same person, through the
        channel or by a code in this chat."""
        if state.user_key is None:
            return True
        if resolved is not None and self._user_key(resolved) == state.user_key:
            return True
        proven = self.phone_resolver(state.conversation_id) if self.phone_resolver is not None else None
        return bool(proven) and "PHONE#" + proven == state.user_key

    def _attach_transcript(
        self, state: ConversationState, reply: Reply, resolved: Optional[ResolvedIdentity]
    ) -> None:
        """The run's ticket gets the run's thread, this turn included, on every
        turn of the run, not only the one that raised it, so a person sees
        what was said after the ticket too (spec 2026-10-05, section 2).

        Only this run's turns, those numbered after `turn_offset`: an earlier
        run of this conversation id, which may be someone else's
        (restart_for), never reaches it. And only while the person writing is
        the run's own: once their proof lapses, the next visitor's words reach
        no ticket of theirs."""
        tickets = getattr(self.registry, "tickets", None)
        ticket_id = reply.ticket_id or state.ticket_id
        if not ticket_id or not hasattr(tickets, "attach_transcript"):
            return
        if not self._speaker_owns_run(state, resolved):
            return
        cid = state.conversation_id
        try:
            turns = [turn for turn in self.conversations.transcript(cid) if turn.n > state.turn_offset]
            tickets.attach_transcript(ticket_id, render_transcript(turns))
        except Exception as exc:  # the ticket exists either way; say the thread did not reach it
            self.log.emit(
                "transcript_attach_failed", cid,
                ticket_id=ticket_id, error="%s: %s" % (type(exc).__name__, exc),
            )
```

Replace:

```python
    def _merge_onto_fresh(
        self, ours: ConversationState, this_turn: List[Dict[str, Any]], reply: Reply, looked_up: bool = False
    ) -> ConversationState:
```

with:

```python
    def _merge_onto_fresh(
        self, ours: ConversationState, this_turn: List[Dict[str, Any]], reply: Reply, looked_up: bool = False,
        loaded: Optional[Dict[str, Any]] = None,
    ) -> ConversationState:
```

Replace:

```python
        fresh.placed_order_ids += [o for o in ours.placed_order_ids if o not in fresh.placed_order_ids]
        fresh.consumed_codes += [c for c in ours.consumed_codes if c not in fresh.consumed_codes]
```

with:

```python
        fresh.placed_order_ids += [o for o in ours.placed_order_ids if o not in fresh.placed_order_ids]
        fresh.consumed_codes += [c for c in ours.consumed_codes if c not in fresh.consumed_codes]
        # What this turn read and kept (spec 2026-10-05, section 6): a number
        # the customer typed, a failed look-up, a wait for a number to call,
        # the number a code went to. Where this turn changed one, its value
        # stands; where it did not, the other server's does. With nothing
        # loaded to compare (a direct caller), this turn's stands.
        for name in TURN_FACT_FIELDS:
            if loaded is None or getattr(ours, name) != loaded.get(name):
                setattr(fresh, name, getattr(ours, name))
```

Replace:

```python
        message = turn["message"]
        state = turn["conversation"]  # loaded by handle(), saved after the graph
        state.turns += 1
        if self.verify_gate is not None and self.phone_resolver is not None:
```

with:

```python
        message = turn["message"]
        state = turn["conversation"]  # loaded by handle(), saved after the graph
        state.turns += 1
        self._note_typed_number(message, state)
        if self.verify_gate is not None and self.phone_resolver is not None:
```

Replace:

```python
    def _note_origin(self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity) -> None:
```

with:

```python
    @staticmethod
    def _note_typed_number(message: InboundMessage, state: ConversationState) -> None:
        """The latest Indian mobile the customer typed in this run, ten digits
        (spec 2026-10-05, section 6): the number a ticket is called back on
        when nobody proved one. Digits of any script are read as ASCII first,
        and a code the conversation already spent is not a number. Customers
        only: a dealer typing a customer's number is not giving their own."""
        if message.persona != "customer":
            return
        text = ascii_digits(message.message_text or "")
        if text.strip() in state.consumed_codes:
            return
        found = find_phone(text)
        if found is not None:
            state.typed_number = found[0]

    def _note_origin(self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity) -> None:
```

Replace (the end of the agent facts dict in `_run`):

```python
                "customer_messages": lambda: [
                    m for m in customer_texts(state.history) if m.strip() not in state.consumed_codes
                ],
            },
```

with the block below. If Task 1 already put `"started_at"` in this dict, keep that line and do not add a second one:

```python
                "customer_messages": lambda: [
                    m for m in customer_texts(state.history) if m.strip() not in state.consumed_codes
                ],
                # What the ticket record needs, worked out in code (spec
                # 2026-10-05, section 2): the run, this turn's channel, the
                # cover of the ticket's bike and the number the customer last
                # typed. A started_at already on the ToolContext wins.
                "started_at": lambda: state.started_at,
                "channel": lambda: message.channel,
                "coverage": lambda: coverage_fact(state, resolved),
                "typed_number": lambda: state.typed_number,
            },
```

Replace:

```python
        for call in turn.tool_calls:
            if call["tool"] == LOOKUP_WARRANTY_RECORD and not is_error(call["result"]):
                state.coverage_result = call["result"]
```

with:

```python
        for call in turn.tool_calls:
            if call["tool"] == LOOKUP_WARRANTY_RECORD:
                self._remember_coverage(state, call["result"])
```

Replace (the safety branch's late facts, as Task 4 left them):

```python
        late: Dict[str, Any] = {
            # Set here and nowhere else: the model never chooses a ticket's
            # kind, and anything it sends under this name is dropped.
            "ticket_kind": lambda: "safety",
            "identity_strength": lambda: self._identity_strength(message.conversation_id, resolved),
        }
```

with:

```python
        late: Dict[str, Any] = {
            # Set here and nowhere else: the model never chooses a ticket's
            # kind, and anything it sends under this name is dropped.
            "ticket_kind": lambda: "safety",
            "identity_strength": lambda: self._identity_strength(message.conversation_id, resolved),
            # The same facts an agent's ticket gets (spec 2026-10-05,
            # section 2). A started_at already on the ToolContext wins.
            "started_at": lambda: state.started_at,
            "channel": lambda: message.channel,
            "coverage": lambda: coverage_fact(state, resolved),
            "typed_number": lambda: state.typed_number,
        }
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_runtime_tickets
```

Then the modules nearest the change:

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_ticket_transcript tests.test_runtime_persistence tests.test_audit_persistence tests.test_edge_cases_after_merge tests.test_merge_integration tests.test_origin_runtime tests.test_verify_first tests.test_navigation_chat tests.test_conversation_json tests.test_coverage_memory tests.test_ticket_tools_desk
```

Then the whole suite:

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: the suite is green except the one known environmental failure, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test changes:
- `tests.test_ticket_transcript.TicketTranscriptTests` still passes. Each conversation there is one run starting at turn 1, so the whole thread is this run's. `test_a_failed_attachment_is_logged_and_the_reply_still_goes_out` still logs `transcript_attach_failed`, because the verified speaker owns the run.
- The direct `_merge_onto_fresh` calls in `tests.test_audit_persistence`, `tests.test_edge_cases_after_merge` and `tests.test_merge_integration` pass no `loaded`. This turn's values for the new fields, all defaults there, then stand.
- `tests.test_conversation_json.StateJsonTests` still round-trips, because the new fields have defaults.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/conversation.py src/emotorad_ai/runtime.py tests/test_runtime_tickets.py
git commit -m "feat: run-scoped ticket transcripts on every turn, run bounds, and the ticket facts" -m "The runtime gives the ticket tools the run, the channel, the cover of the ticket's bike and the number the customer last typed. The conversation keeps typed_number, lookup_error and the call-back fields, carries them through a merge when the turn changed them, and forgets the look-up with the bike. Every turn of a run that holds a ticket attaches that run's turns only, and only while the run's own person is writing. A run's first turn closes the conversation's earlier runs on the seam." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Interface issues for the plan author

1. **The `identity_strength` fact is wired in Task 4, not Task 5.** Task 4 makes `raise_intake_ticket` use the injected phone only when `identity_strength == VERIFIED`. Without the fact, `tests.test_safety_backstop.HazardClaimTests.test_a_claim_after_an_intake_ticket_this_turn_is_left_alone` breaks between the two tasks: a verified rider gets `contact_number_required`. So Task 4 adds three things to `runtime.py`:
   - `Runtime._identity_strength`.
   - The agent fact `identity_strength`.
   - The safety branch's `ticket_kind` and `identity_strength`.

   Task 5 adds `started_at`, `channel`, `coverage` and `typed_number`.
2. **`ticket_kind` is a new optional inject on `create_support_ticket`.** It is set only in `_raise_safety_ticket`'s `late`, as `"safety"`. A key prefix was not used, because the model could choose a key starting with `safety:`. The skeleton's list of new optional injects does not name it.
3. **`cluster_id` is an optional inject on all three tools.** The record needs it, and the worker filters media by cluster. The skeleton's `create()` keyword list has it, but its inject list does not.
4. **`raise_intake_ticket` has more optional injects than the skeleton lists.** The skeleton lists only `typed_number` for it. It also takes `phone`, `identity_strength`, `persona`, `started_at` and `channel`, which "the verified injected phone if any" needs.
5. **The `contact_number_required` remedy is a sentence, as the spec says.** Every other `remedy` in the registry is a snake_case action name (`late_warranty_registration`, `collect_evidence`). Consider `remedy="ask_for_callback_number"` instead, with the sentence kept in the message. The tests pin the spec's text.
6. **Run bounds and the Desk worker (Task 9).** Between person one's proof lapsing and `restart_for`, the next visitor's first messages (the problem, `[phone]`, `[code]` exchange) belong to person one's run, both by turn number and by time.
   - Task 5 stops attaching on turns whose speaker is not the run's proven owner (`_speaker_owns_run`). So the mock never gets them, and those turns never wake the Desk record.
   - The worker, however, posts every turn in `[started_at, ended_at)`. If it runs after those turns for any reason (a retry, or a wake from earlier), it will post them to person one's Zoho ticket.
   - Suggested fix, inside Tasks 3 and 9 only: `DeskTicketSystem.attach_transcript` stores `attached_until = clock()` with the wake, and the worker posts only turns with `at <= attached_until`.
7. **A failed `close_runs` is logged (`close_runs_failed`) and not retried.** It runs only on a run's first turn, so an earlier run's record keeps `ended_at=None` until the next restart. The fix in item 6 also bounds this.
8. **`coverage_fact` reads more than `state.coverage_result` and `lookup_error`.** It also falls back to this turn's identity look-up (`resolved.bikes`, then `resolved.error`). The safety branch fires before any agent look-up, and `hydrate`'s look-up never reaches `_remember_lookup`. Without the fallback, a WhatsApp safety ticket would have no coverage.
9. **Unverified tickets drop more than the bike.** `create_support_ticket` and `submit_warranty_proof` also drop `coverage` and `customer_name` unless the ticket is verified. When `identity_strength` is absent (direct callers with no facts), the bike is still looked up as before, because existing tests rely on it, but the ticket is recorded as unverified. Only ASSERTED skips the look-up.
10. **`typed_number` is recorded for persona `customer` only.** A dealer typing a customer's number is not giving their own.
11. **`build_registry` uses `ticket_system or MockTicketSystem()`.** If `TicketRouter` ever defines `__len__` or `__bool__`, an empty router would quietly be replaced by a mock. Task 3 or Task 10 should change it to `if ticket_system is not None`.
12. **The two `started_at` string formats differ.** `conversation.utc_now_iso()` omits microseconds when they are zero, while `tickets.clock.now_iso()` always writes them, and `close_runs` compares `started_at` strings. This is harmless in practice. A single format would remove the edge.
13. **Two people need two numbers with bikes on record.** The two-person tests use the fixture numbers `PHONE_AMIIGO_TEST_RIDER` and `+919876543210`, as `tests/test_origin_runtime.py` does. Every other test uses `+919999999999`, with the fixture bike served through `warranty_source`.
14. **The runtime tests assume Task 1 fills `ToolContext.persona` in two places:** on the safety branch's context and in `Agent.run`, from `resolved.persona` or `message.persona`. If it does not, the customer Desk tests fail and the dealer tests still pass.

---

<!-- drafted as tasks-6-8-recovered -->

### Task 6: Zoho settings and start-up checks

**Files:**
- Create: `src/emotorad_ai/zoho/__init__.py`
- Create: `src/emotorad_ai/zoho/settings.py`
- Test: `tests/test_zoho_settings.py`

**Interfaces:**
- Consumes: the `TicketStore` protocol's `has_unique_source_key()` (Task 2; the tests use a stub), `conversation.StoreUnavailable` (tests only).
- Produces:
  - `ZohoSettings`, `load_zoho_settings(env) -> Tuple[Optional[ZohoSettings], str]` and `startup_problem(settings, *, region, store_kind, ticket_store, dev_codes, otp_is_mock) -> Optional[str]`, as the skeleton defines them.
  - Name constants: `REFRESH_TOKEN`, `CLIENT_ID`, `CLIENT_SECRET`, `ORG_ID`, `TEST_DEPARTMENT_ID`, `TEST_CONTACT_ID`, `DEPARTMENT_ID`, `UNVERIFIED_CONTACT_ID`, `LIVE`, `CF_CHAT_REFERENCE`, `CF_SOURCE`, `PRIORITY_HIGH`, `PRIORITY_MEDIUM`, `CHANNEL`, `CREDITS_FLOOR`, `ATTACHMENT_LIMIT_MB`, `AI_ENV`.
  - Name groups: `ALWAYS`, `LIVE_ONLY`, `ENV_NAMES` (every `EMOTORAD_ZOHO_*` name, for Task 10's health test and `chat_local` WITHHELD).
  - Status constants: `NOT_CONFIGURED`, `OK`, `NOT_ALLOWED_IN_REGION`, `STORE_NOT_MONGODB`, `INDEX_MISSING`, `INDEX_UNREADABLE`, `LIVE_REFUSED`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zoho_settings.py`:

```python
"""Zoho settings and the start-up checks (spec 2026-10-05, section 1).

Zoho is off unless the refresh token is set. Then either every name the mode
needs is present and every check passes, or the mock is used exactly as when
Zoho is off. No string built here may hold a value, only names: the status
goes to /health and to the log.
"""

import unittest

from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.zoho import settings as zs
from emotorad_ai.zoho.settings import ZohoSettings, load_zoho_settings, startup_problem

REFRESH = "1000.refresh-DO-NOT-LEAK"
CLIENT_ID = "1000.CLIENTID-DO-NOT-LEAK"
SECRET = "client-secret-DO-NOT-LEAK"
SECRETS = (REFRESH, CLIENT_ID, SECRET)

TEST_ENV = {
    "EMOTORAD_ZOHO_REFRESH_TOKEN": REFRESH,
    "EMOTORAD_ZOHO_CLIENT_ID": CLIENT_ID,
    "EMOTORAD_ZOHO_CLIENT_SECRET": SECRET,
    "EMOTORAD_ZOHO_ORG_ID": "60000000001",
    "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID": "4000000000001",
    "EMOTORAD_ZOHO_TEST_CONTACT_ID": "4000000000002",
    "EMOTORAD_ZOHO_CF_CHAT_REFERENCE": "cf_chat_reference",
    "EMOTORAD_ZOHO_CF_SOURCE": "cf_source",
    "EMOTORAD_AI_ENV": "stage",
}
LIVE_ENV = dict(
    TEST_ENV,
    EMOTORAD_ZOHO_DEPARTMENT_ID="4000000000003",
    EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID="4000000000004",
    EMOTORAD_ZOHO_LIVE="yes",
)


def without(env, *names):
    return {name: value for name, value in env.items() if name not in names}


def loaded(env):
    settings, status = load_zoho_settings(env)
    assert settings is not None, status
    return settings


class FakeTicketStore:
    """Only what the start-up check reads."""

    def __init__(self, indexed=True, error=None):
        self.indexed = indexed
        self.error = error
        self.asked = 0

    def has_unique_source_key(self):
        self.asked += 1
        if self.error is not None:
            raise self.error
        return self.indexed


def problem(settings, **changes):
    arguments = dict(region="ap-south-1", store_kind="mongodb", ticket_store=FakeTicketStore(),
                     dev_codes=False, otp_is_mock=False)
    arguments.update(changes)
    return startup_problem(settings, **arguments)


class SwitchTests(unittest.TestCase):
    def test_without_the_refresh_token_zoho_is_not_configured_whatever_else_is_set(self):
        self.assertEqual(load_zoho_settings(without(LIVE_ENV, zs.REFRESH_TOKEN)), (None, "not configured"))

    def test_a_blank_refresh_token_is_the_same_as_none(self):
        for blank in ("", "   ", "\n"):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(load_zoho_settings(dict(TEST_ENV, **{zs.REFRESH_TOKEN: blank})),
                                 (None, "not configured"))

    def test_an_empty_environment_is_not_configured(self):
        self.assertEqual(load_zoho_settings({}), (None, "not configured"))


class TestModeTests(unittest.TestCase):
    def test_every_always_needed_name_gives_test_mode(self):
        settings, status = load_zoho_settings(TEST_ENV)
        self.assertEqual(status, "ok")
        self.assertIsInstance(settings, ZohoSettings)
        self.assertFalse(settings.live)
        self.assertEqual(settings.mode, "test")
        self.assertEqual(settings.active_department_id, "4000000000001")
        self.assertEqual(settings.test_contact_id, "4000000000002")
        self.assertIsNone(settings.department_id)
        self.assertIsNone(settings.unverified_contact_id)
        self.assertEqual(settings.environment, "stage")
        self.assertEqual(settings.org_id, "60000000001")
        self.assertEqual((settings.cf_chat_reference, settings.cf_source), ("cf_chat_reference", "cf_source"))
        self.assertEqual((settings.client_id, settings.client_secret, settings.refresh_token),
                         (CLIENT_ID, SECRET, REFRESH))

    def test_the_probe_only_values_have_their_defaults(self):
        settings = loaded(TEST_ENV)
        self.assertEqual(settings.priority_high, "High")
        self.assertEqual(settings.priority_medium, "Medium")
        self.assertEqual(settings.channel, "Chat")
        self.assertEqual(settings.credits_floor, 1000)
        self.assertEqual(settings.attachment_limit_bytes, 20 * 1024 * 1024)

    def test_the_probe_only_values_can_be_set(self):
        settings = loaded(dict(TEST_ENV, **{
            zs.PRIORITY_HIGH: "P1", zs.PRIORITY_MEDIUM: "P3", zs.CHANNEL: "Web",
            zs.CREDITS_FLOOR: "2500", zs.ATTACHMENT_LIMIT_MB: "15",
        }))
        self.assertEqual((settings.priority_high, settings.priority_medium, settings.channel), ("P1", "P3", "Web"))
        self.assertEqual(settings.credits_floor, 2500)
        self.assertEqual(settings.attachment_limit_bytes, 15 * 1024 * 1024)

    def test_values_are_read_without_surrounding_spaces(self):
        settings = loaded(dict(TEST_ENV, **{zs.ORG_ID: " 60000000001\n", zs.REFRESH_TOKEN: REFRESH + "\n"}))
        self.assertEqual(settings.org_id, "60000000001")
        self.assertEqual(settings.refresh_token, REFRESH)

    def test_a_real_department_without_live_stays_in_test_mode(self):
        settings, status = load_zoho_settings(without(LIVE_ENV, zs.LIVE))
        self.assertEqual(status, "ok")
        self.assertEqual(settings.mode, "test")
        self.assertEqual(settings.active_department_id, "4000000000001")

    def test_live_is_only_the_exact_word_yes(self):
        # Anything else is the test department: the safe way to read a mistake.
        for value in ("Yes", "YES", "true", "1", "y", "yes ", " yes", ""):
            with self.subTest(value=repr(value)):
                settings, status = load_zoho_settings(dict(LIVE_ENV, **{zs.LIVE: value}))
                self.assertEqual(status, "ok")
                self.assertFalse(settings.live)
                self.assertEqual(settings.mode, "test")


class LiveModeTests(unittest.TestCase):
    def test_live_sends_to_the_real_department(self):
        settings = loaded(LIVE_ENV)
        self.assertTrue(settings.live)
        self.assertEqual(settings.mode, "live")
        self.assertEqual(settings.active_department_id, "4000000000003")
        self.assertEqual(settings.unverified_contact_id, "4000000000004")
        self.assertEqual(settings.test_department_id, "4000000000001")

    def test_live_without_the_real_department_or_the_unverified_contact_is_misconfigured(self):
        env = without(LIVE_ENV, zs.DEPARTMENT_ID, zs.UNVERIFIED_CONTACT_ID)
        self.assertEqual(
            load_zoho_settings(env),
            (None, "misconfigured: missing EMOTORAD_ZOHO_DEPARTMENT_ID, EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID"),
        )

    def test_each_live_only_name_is_reported(self):
        for name in zs.LIVE_ONLY:
            with self.subTest(name=name):
                self.assertEqual(load_zoho_settings(without(LIVE_ENV, name)), (None, "misconfigured: missing %s" % name))


class MissingTests(unittest.TestCase):
    def test_each_always_needed_name_is_reported_by_name(self):
        for name in zs.ALWAYS:
            with self.subTest(name=name):
                self.assertEqual(load_zoho_settings(without(TEST_ENV, name)), (None, "misconfigured: missing %s" % name))

    def test_a_blank_value_counts_as_missing(self):
        self.assertEqual(load_zoho_settings(dict(TEST_ENV, **{zs.ORG_ID: "  "})),
                         (None, "misconfigured: missing EMOTORAD_ZOHO_ORG_ID"))

    def test_several_missing_names_come_in_one_fixed_order(self):
        env = without(TEST_ENV, zs.AI_ENV, zs.CLIENT_SECRET)
        self.assertEqual(load_zoho_settings(env),
                         (None, "misconfigured: missing EMOTORAD_ZOHO_CLIENT_SECRET, EMOTORAD_AI_ENV"))

    def test_the_deployment_name_is_required(self):
        # It prefixes every chat reference, so two deployments sharing the
        # Zoho organisation never adopt each other's tickets.
        self.assertEqual(load_zoho_settings(without(TEST_ENV, "EMOTORAD_AI_ENV")),
                         (None, "misconfigured: missing EMOTORAD_AI_ENV"))

    def test_the_probe_only_values_are_never_required(self):
        probe_only = (zs.PRIORITY_HIGH, zs.PRIORITY_MEDIUM, zs.CHANNEL, zs.CREDITS_FLOOR, zs.ATTACHMENT_LIMIT_MB)
        for name in probe_only:
            self.assertNotIn(name, zs.ALWAYS + zs.LIVE_ONLY)
            self.assertNotIn(name, LIVE_ENV)
        self.assertEqual(load_zoho_settings(LIVE_ENV)[1], "ok")


class NumberTests(unittest.TestCase):
    def test_a_number_setting_that_is_not_a_usable_whole_number_is_misconfigured(self):
        cases = [
            (zs.CREDITS_FLOOR, "lots"), (zs.CREDITS_FLOOR, "1.5"), (zs.CREDITS_FLOOR, "-1"),
            (zs.CREDITS_FLOOR, "1_000"), (zs.ATTACHMENT_LIMIT_MB, "0"), (zs.ATTACHMENT_LIMIT_MB, "२०"),
            (zs.ATTACHMENT_LIMIT_MB, "20MB"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.assertEqual(load_zoho_settings(dict(TEST_ENV, **{name: value})),
                                 (None, "misconfigured: bad number: %s" % name))

    def test_a_credits_floor_of_zero_is_allowed(self):
        self.assertEqual(loaded(dict(TEST_ENV, **{zs.CREDITS_FLOOR: "0"})).credits_floor, 0)


class SecretTests(unittest.TestCase):
    def test_no_returned_string_holds_a_value(self):
        envs = [
            TEST_ENV, LIVE_ENV, without(TEST_ENV, zs.ORG_ID, zs.CLIENT_SECRET), without(LIVE_ENV, zs.DEPARTMENT_ID),
            dict(TEST_ENV, **{zs.CREDITS_FLOOR: "lots"}), without(TEST_ENV, zs.REFRESH_TOKEN),
        ]
        for env in envs:
            settings, status = load_zoho_settings(env)
            shown = [status, repr(settings), str(settings)]
            if settings is not None:
                for changes in ({"region": "eu-central-1"}, {"store_kind": "memory"}, {"ticket_store": None},
                                {"ticket_store": FakeTicketStore(error=StoreUnavailable(REFRESH))},
                                {"dev_codes": True}):
                    shown.append(problem(settings, **changes) or "")
            for text in shown:
                for secret in SECRETS:
                    self.assertNotIn(secret, text)

    def test_repr_leaves_out_the_credential_fields(self):
        text = repr(loaded(TEST_ENV))
        for name in ("client_id", "client_secret", "refresh_token"):
            self.assertNotIn(name, text)
        self.assertIn("org_id='60000000001'", text)


class StartupTests(unittest.TestCase):
    def test_a_sound_setup_has_no_problem(self):
        store = FakeTicketStore()
        self.assertIsNone(problem(loaded(TEST_ENV), ticket_store=store))
        self.assertIsNone(problem(loaded(LIVE_ENV)))
        self.assertEqual(store.asked, 1)

    def test_an_eu_region_is_refused_before_anything_else(self):
        for region in ("eu-central-1", "eu-west-1", "EU-NORTH-1", " eu-south-2"):
            with self.subTest(region=region):
                self.assertEqual(problem(loaded(TEST_ENV), region=region, store_kind="memory", ticket_store=None),
                                 "not allowed in this region")

    def test_other_regions_pass(self):
        for region in ("ap-south-1", "us-east-1", ""):
            with self.subTest(region=region):
                self.assertIsNone(problem(loaded(TEST_ENV), region=region))

    def test_a_store_that_is_not_mongodb_is_refused_without_asking_for_its_index(self):
        store = FakeTicketStore()
        self.assertEqual(problem(loaded(TEST_ENV), store_kind="memory", ticket_store=store),
                         "misconfigured: store is not mongodb")
        self.assertEqual(store.asked, 0)

    def test_a_missing_unique_index_is_refused(self):
        self.assertEqual(problem(loaded(TEST_ENV), ticket_store=FakeTicketStore(indexed=False)),
                         "misconfigured: tickets index missing")
        self.assertEqual(problem(loaded(TEST_ENV), ticket_store=None), "misconfigured: tickets index missing")

    def test_an_index_list_that_cannot_be_read_is_refused_by_class_name_only(self):
        store = FakeTicketStore(error=StoreUnavailable("mongodb+srv://user:password@host is down"))
        found = problem(loaded(TEST_ENV), ticket_store=store)
        self.assertEqual(found, "misconfigured: tickets index not readable (StoreUnavailable)")
        self.assertNotIn("password", found)

    def test_live_is_refused_while_verification_is_a_test_one(self):
        settings = loaded(LIVE_ENV)
        refused = "misconfigured: live refused: test verification in use"
        self.assertEqual(problem(settings, dev_codes=True), refused)
        self.assertEqual(problem(settings, otp_is_mock=True), refused)
        self.assertEqual(problem(settings, dev_codes=True, otp_is_mock=True), refused)

    def test_the_test_department_runs_with_test_verification(self):
        # Staging runs dev codes and the mock OTP sender today
        # (deploy-staging.yml), and its tickets go to the test department.
        self.assertIsNone(problem(loaded(TEST_ENV), dev_codes=True, otp_is_mock=True))

    def test_every_problem_reads_as_a_health_value(self):
        settings = loaded(LIVE_ENV)
        found = [
            problem(settings, region="eu-west-1"), problem(settings, store_kind="memory"),
            problem(settings, ticket_store=None), problem(settings, ticket_store=FakeTicketStore(indexed=False)),
            problem(settings, ticket_store=FakeTicketStore(error=StoreUnavailable("down"))),
            problem(settings, dev_codes=True),
        ]
        for text in found:
            self.assertTrue(text == zs.NOT_ALLOWED_IN_REGION or text.startswith("misconfigured: "), text)


class NamesTests(unittest.TestCase):
    def test_env_names_lists_every_zoho_name_once(self):
        names = {value for value in vars(zs).values() if isinstance(value, str) and value.startswith("EMOTORAD_ZOHO_")}
        self.assertEqual(set(zs.ENV_NAMES), names)
        self.assertEqual(len(zs.ENV_NAMES), len(set(zs.ENV_NAMES)))
        self.assertEqual(len(zs.ENV_NAMES), 16)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

From the repo root:

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_settings
```

Expected: `ImportError: Failed to import test module: test_zoho_settings`, caused by `ModuleNotFoundError: No module named 'emotorad_ai.zoho'`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/zoho/__init__.py`:

```python
"""Zoho Desk for the customer chatbot (spec 2026-10-05-zoho-desk-tickets-design).

Nothing in this package calls Zoho or starts a thread at import. The worker
starts only in the API's lifespan, and only when Zoho is configured.
"""
```

Create `src/emotorad_ai/zoho/settings.py`:

```python
"""Zoho Desk settings and the start-up checks (spec 2026-10-05, section 1).

Zoho is off unless EMOTORAD_ZOHO_REFRESH_TOKEN is set. When it is set, every
name the mode needs must be present and every start-up check must pass, or
the mock is used exactly as when Zoho is off: there is no state in which
tickets are recorded but cannot be sent safely. zoho/wiring.py logs
`zoho_misconfigured`, and /health shows the reason.

The client id, the client secret and the refresh token are credentials. They
are kept out of repr, and no string built here ever holds a value, only
names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

REFRESH_TOKEN = "EMOTORAD_ZOHO_REFRESH_TOKEN"
CLIENT_ID = "EMOTORAD_ZOHO_CLIENT_ID"
CLIENT_SECRET = "EMOTORAD_ZOHO_CLIENT_SECRET"
ORG_ID = "EMOTORAD_ZOHO_ORG_ID"
TEST_DEPARTMENT_ID = "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID"
TEST_CONTACT_ID = "EMOTORAD_ZOHO_TEST_CONTACT_ID"
DEPARTMENT_ID = "EMOTORAD_ZOHO_DEPARTMENT_ID"
UNVERIFIED_CONTACT_ID = "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID"
LIVE = "EMOTORAD_ZOHO_LIVE"
CF_CHAT_REFERENCE = "EMOTORAD_ZOHO_CF_CHAT_REFERENCE"
CF_SOURCE = "EMOTORAD_ZOHO_CF_SOURCE"
PRIORITY_HIGH = "EMOTORAD_ZOHO_PRIORITY_HIGH"
PRIORITY_MEDIUM = "EMOTORAD_ZOHO_PRIORITY_MEDIUM"
CHANNEL = "EMOTORAD_ZOHO_CHANNEL"
CREDITS_FLOOR = "EMOTORAD_ZOHO_CREDITS_FLOOR"
ATTACHMENT_LIMIT_MB = "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB"
# Set by the deploy (deploy-staging.yml). It prefixes every chat reference, so
# two deployments sharing one Zoho organisation never adopt each other's
# tickets.
AI_ENV = "EMOTORAD_AI_ENV"

# Needed in both modes, in the order a missing list names them.
ALWAYS = (CLIENT_ID, CLIENT_SECRET, ORG_ID, TEST_DEPARTMENT_ID, TEST_CONTACT_ID,
          CF_CHAT_REFERENCE, CF_SOURCE, AI_ENV)
# Needed only to send to the real department.
LIVE_ONLY = (DEPARTMENT_ID, UNVERIFIED_CONTACT_ID)
# Every Zoho name, for the places that blank or withhold them all (the health
# test, scripts/chat_local.py).
ENV_NAMES = (
    REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET, ORG_ID, TEST_DEPARTMENT_ID, TEST_CONTACT_ID,
    DEPARTMENT_ID, UNVERIFIED_CONTACT_ID, LIVE, CF_CHAT_REFERENCE, CF_SOURCE,
    PRIORITY_HIGH, PRIORITY_MEDIUM, CHANNEL, CREDITS_FLOOR, ATTACHMENT_LIMIT_MB,
)

# The values only part 1's probe can give. These defaults hold until it runs.
DEFAULT_PRIORITY_HIGH = "High"
DEFAULT_PRIORITY_MEDIUM = "Medium"
DEFAULT_CHANNEL = "Chat"
DEFAULT_CREDITS_FLOOR = 1000
DEFAULT_ATTACHMENT_LIMIT_MB = 20

# What load_zoho_settings and startup_problem answer, in /health's words.
NOT_CONFIGURED = "not configured"
OK = "ok"
NOT_ALLOWED_IN_REGION = "not allowed in this region"
STORE_NOT_MONGODB = "misconfigured: store is not mongodb"
INDEX_MISSING = "misconfigured: tickets index missing"
INDEX_UNREADABLE = "misconfigured: tickets index not readable (%s)"
LIVE_REFUSED = "misconfigured: live refused: test verification in use"

# ASCII digits only: int() would also take "२०", "1_000" and "+5".
_WHOLE = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True)
class ZohoSettings:
    """What the client and the worker need. Built by load_zoho_settings."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    org_id: str
    test_department_id: str
    test_contact_id: str
    department_id: Optional[str]
    unverified_contact_id: Optional[str]
    live: bool
    environment: str
    cf_chat_reference: str
    cf_source: str
    priority_high: str
    priority_medium: str
    channel: str
    credits_floor: int
    attachment_limit_bytes: int

    @property
    def mode(self) -> str:
        """The stamp on every record made under these settings."""
        return "live" if self.live else "test"

    @property
    def active_department_id(self) -> str:
        # Live always has the real department: load_zoho_settings refuses it
        # otherwise. Anything short of that is the test department.
        if self.live and self.department_id:
            return self.department_id
        return self.test_department_id


def _value(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _whole(env: Mapping[str, str], name: str, default: int, least: int) -> Optional[int]:
    """A whole-number setting, or its default when unset. None when it is set
    to anything else: a typo must not quietly become the default."""
    text = _value(env, name)
    if not text:
        return default
    if not _WHOLE.fullmatch(text):
        return None
    number = int(text)
    return number if number >= least else None


def load_zoho_settings(env: Mapping[str, str]) -> Tuple[Optional[ZohoSettings], str]:
    """(settings, "ok"), or (None, why not) in the words /health uses."""
    refresh_token = _value(env, REFRESH_TOKEN)
    if not refresh_token:
        return None, NOT_CONFIGURED
    # Exactly "yes", as typed. "Yes", "true" or "1" means the test department:
    # the safe way to read a mistake, and /health shows which one it is.
    live = env.get(LIVE) == "yes"
    needed = ALWAYS + (LIVE_ONLY if live else ())
    missing = [name for name in needed if not _value(env, name)]
    if missing:
        return None, "misconfigured: missing %s" % ", ".join(missing)
    floor = _whole(env, CREDITS_FLOOR, DEFAULT_CREDITS_FLOOR, least=0)
    limit_mb = _whole(env, ATTACHMENT_LIMIT_MB, DEFAULT_ATTACHMENT_LIMIT_MB, least=1)
    bad = [name for name, number in ((CREDITS_FLOOR, floor), (ATTACHMENT_LIMIT_MB, limit_mb)) if number is None]
    if bad:
        return None, "misconfigured: bad number: %s" % ", ".join(bad)
    settings = ZohoSettings(
        client_id=_value(env, CLIENT_ID),
        client_secret=_value(env, CLIENT_SECRET),
        refresh_token=refresh_token,
        org_id=_value(env, ORG_ID),
        test_department_id=_value(env, TEST_DEPARTMENT_ID),
        test_contact_id=_value(env, TEST_CONTACT_ID),
        department_id=_value(env, DEPARTMENT_ID) or None,
        unverified_contact_id=_value(env, UNVERIFIED_CONTACT_ID) or None,
        live=live,
        environment=_value(env, AI_ENV),
        cf_chat_reference=_value(env, CF_CHAT_REFERENCE),
        cf_source=_value(env, CF_SOURCE),
        priority_high=_value(env, PRIORITY_HIGH) or DEFAULT_PRIORITY_HIGH,
        priority_medium=_value(env, PRIORITY_MEDIUM) or DEFAULT_PRIORITY_MEDIUM,
        channel=_value(env, CHANNEL) or DEFAULT_CHANNEL,
        credits_floor=floor,
        attachment_limit_bytes=limit_mb * 1024 * 1024,
    )
    return settings, OK


def startup_problem(settings: ZohoSettings, *, region: str, store_kind: str, ticket_store: Any,
                    dev_codes: bool, otp_is_mock: bool) -> Optional[str]:
    """The first start-up check that fails, in /health's words, or None.

    These run in the spec's order, after load_zoho_settings has checked the
    names: the region, then the store and its unique index, then real
    verification for live mode. Any answer other than None means the mock is
    used and nothing is recorded in `tickets`.
    """
    # EU data stays in eu-central-1 (CLAUDE.md; Risk Register sections 16-20),
    # and there is no EU Zoho organisation.
    if (region or "").strip().lower().startswith("eu-"):
        return NOT_ALLOWED_IN_REGION
    if store_kind != "mongodb":
        # A record must survive a restart to be sent after the reply.
        return STORE_NOT_MONGODB
    if ticket_store is None:
        return INDEX_MISSING
    try:
        indexed = bool(ticket_store.has_unique_source_key())
    except Exception as exc:
        # The index list could not be read, so nothing proves a retried
        # create returns the first record. Named by class only: a driver's
        # message can carry the connection string.
        return INDEX_UNREADABLE % type(exc).__name__
    if not indexed:
        return INDEX_MISSING
    if settings.live and (dev_codes or otp_is_mock):
        # Live tickets carry real customers' numbers. Dev codes or the mock
        # OTP sender would let anyone pass as any number.
        return LIVE_REFUSED
    return None
```

- [ ] **Step 4: Run the module, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_settings -v
```

Expected: `Ran 31 tests`, `OK`.

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: the count is 31 higher than after Task 5. The only failure is the known environmental one, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test changes, because nothing imports the new package yet.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/zoho/__init__.py src/emotorad_ai/zoho/settings.py tests/test_zoho_settings.py
git commit -m "feat: Zoho settings and start-up checks" -m "Zoho stays off without the refresh token. With it, every name the mode needs must be present, the region must not be eu-, the store must be MongoDB with its unique tickets index, and live needs real verification. Otherwise the mock is used. Credentials stay out of repr and out of every status string." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7: Zoho HTTP, errors, token source, Desk client, documented shapes and the fake

**Files:**
- Create: `src/emotorad_ai/zoho/errors.py`
- Create: `src/emotorad_ai/zoho/http.py`
- Create: `src/emotorad_ai/zoho/auth.py`
- Create: `src/emotorad_ai/zoho/desk.py`
- Create: `tests/fake_zoho.py`
- Create: `docs/api-shapes/zoho-ticket.json`, `docs/api-shapes/zoho-contact-search.json`, `docs/api-shapes/zoho-contact-tickets.json`, `docs/api-shapes/zoho-comment.json`, `docs/api-shapes/zoho-attachment.json`, `docs/api-shapes/zoho-token.json`, `docs/api-shapes/zoho-errors.json`
- Test: `tests/test_zoho_http.py`, `tests/test_zoho_auth.py`, `tests/test_zoho_desk.py`

**Interfaces:**
- Consumes: `zoho.settings.ZohoSettings` and `load_zoho_settings` (Task 6).
- Produces:
  - `zoho/errors.py`: `ZohoError(message, error=None)` with `.error`. Its subclasses are `ZohoUnavailable`, `ZohoUnknownOutcome`, `ZohoRejected(message, error=None, fields=())` with `.fields`, `ZohoConfigError`, `ZohoAuthExpired`, `ZohoGone`, `ZohoTooLarge`, `ZohoBusy`, `ZohoCreditsExhausted(message, error=None, retry_after_seconds=None)`, `ZohoTokenRefused` and `ZohoTokenThrottled`. It also provides `safe_code(value, fallback)`.
  - `zoho/http.py`: `DeskHTTP(opener=urllib.request.urlopen, timeout=8.0)`, with `.last_credits_remaining` and `.call(method, url, headers, body=None, *, write, timeout=None, classify=True) -> (status, parsed JSON or None)`. Also `CREDITS_HEADER` and `DEFAULT_TIMEOUT`.
  - `zoho/auth.py`: `TokenSource(settings, http, clock=time.monotonic)`, with `.token()`, `.invalidate()` and `.state`. Also `ACCOUNTS_URL`, `TOKEN_URL`, `REFRESH_EARLY_SECONDS`, `THROTTLE_WAIT_SECONDS` and `REFUSED_WAIT_SECONDS`.
  - `zoho/desk.py`: `DeskClient(settings, tokens, http)` with exactly the skeleton's methods, plus `find_adoptable(tickets, cf_api_name, chat_reference)`, `safe_filename(name)`, `DESK_URL`, `UPLOAD_TIMEOUT`, `COMMENT_LIMIT` and `NAME_LIMIT`.
  - `tests/fake_zoho.py`: `FakeZoho(auto_token=True)` with `.queue(*answers)`, `.requests`, `.desk_requests`, `.token_requests`, `.token_gate` and `.tokens_issued`. Also `Answer(status=200, body=None, headers=None, read_error=None)`, `shape(name)`, `token_answer(n)`, `zoho_settings(live=False, **env_changes)`, `ENV`, `LIVE_ENV`, `SECRETS`, `SHAPES`, `TOKEN_URL`, `ORG_ID`, `TEST_DEPARTMENT`, `TEST_CONTACT`, `LIVE_DEPARTMENT`, `UNVERIFIED_CONTACT`, `CF_CHAT_REFERENCE` and `CF_SOURCE`.

- [ ] **Step 1: Write the failing tests**

Create the seven shapes. Their sources are Zoho's published OAS (https://github.com/zoho/zohodesk-oas, branch `master`, `v1.0/`) and the OMS's production code. The cf names are placeholders that match the test settings.

`docs/api-shapes/zoho-ticket.json`:

```json
{
  "_source": "Not a capture. Built from Zoho's published OpenAPI files (https://github.com/zoho/zohodesk-oas, v1.0/Ticket.json: createTicket answers 200 with ticketResponse) and the fields the OMS reads from this answer in production (em-biz-backend zoho/zoho_proccessor.py, update_or_create_ticket). Every value is fake. The two cf names stand for the settings EMOTORAD_ZOHO_CF_CHAT_REFERENCE and EMOTORAD_ZOHO_CF_SOURCE. scripts/zoho/probe.py and scripts/zoho/test_ticket.py replace this file with a masked capture (spec 2026-10-05, part 1).",
  "id": "4000000528005",
  "ticketNumber": "1024",
  "webUrl": "https://desk.zoho.in/[masked]/4000000528005",
  "subject": "[AI chat] Battery: charging - EMX Plus",
  "description": "Reference: EM-1000001\nSource: AI chatbot",
  "status": "Open",
  "statusType": "Open",
  "priority": "Medium",
  "channel": "Chat",
  "language": "English",
  "email": null,
  "phone": "+919999999999",
  "contactId": "4000000000002",
  "departmentId": "4000000000001",
  "assigneeId": null,
  "dueDate": null,
  "subCategory": null,
  "entitySkills": [],
  "customerResponseTime": "2026-10-05T08:30:00.000Z",
  "createdTime": "2026-10-05T08:30:00.000Z",
  "modifiedTime": "2026-10-05T08:30:00.000Z",
  "closedTime": null,
  "onholdTime": null,
  "isDeleted": false,
  "isTrashed": false,
  "isSpam": false,
  "cf": {
    "cf_chat_reference": "stage:EM-1000001",
    "cf_source": "AI chatbot"
  }
}
```

`docs/api-shapes/zoho-contact-search.json`:

```json
{
  "_source": "Not a capture. Built from Zoho's published OpenAPI files (https://github.com/zoho/zohodesk-oas, v1.0/Search.json: searchContacts takes phone or mobile, with * as a wildcard, and answers 200 with a data list or 204 when nothing matches; the contact's properties come from v1.0/Contact.json). Every value is fake: the contact is the test contact of person step 3. scripts/zoho/probe.py and scripts/zoho/test_ticket.py replace this file with a masked capture (spec 2026-10-05, part 1).",
  "data": [
    {
      "id": "4000000000002",
      "firstName": null,
      "lastName": "AI chatbot test",
      "mobile": "+919999999999",
      "phone": null,
      "email": null,
      "webUrl": "https://desk.zoho.in/[masked]/4000000000002",
      "createdTime": "2026-10-05T08:00:00.000Z"
    }
  ]
}
```

`docs/api-shapes/zoho-contact-tickets.json`:

```json
{
  "_source": "Not a capture. Built from Zoho's published OpenAPI files (https://github.com/zoho/zohodesk-oas, v1.0/Ticket.json: getTicketsByContact takes departmentId, sortBy (-createdTime for newest first) and limit, and answers 200 with a data list or 204), with the ticket properties of zoho-ticket.json. Every value is fake; the second ticket is another deployment's with the same EM- number. Whether this list carries cf is open until part 1 (spec, 'Open until part 1'), and find_adoptable relies on it. scripts/zoho/probe.py and scripts/zoho/test_ticket.py replace this file with a masked capture.",
  "data": [
    {
      "id": "4000000528005",
      "ticketNumber": "1024",
      "subject": "[AI chat] Battery: charging - EMX Plus",
      "status": "Open",
      "departmentId": "4000000000001",
      "contactId": "4000000000002",
      "createdTime": "2026-10-05T08:30:00.000Z",
      "webUrl": "https://desk.zoho.in/[masked]/4000000528005",
      "cf": {
        "cf_chat_reference": "stage:EM-1000001",
        "cf_source": "AI chatbot"
      }
    },
    {
      "id": "4000000527001",
      "ticketNumber": "1023",
      "subject": "[AI chat] SAFETY - bike not given",
      "status": "Open",
      "departmentId": "4000000000001",
      "contactId": "4000000000002",
      "createdTime": "2026-10-05T07:10:00.000Z",
      "webUrl": "https://desk.zoho.in/[masked]/4000000527001",
      "cf": {
        "cf_chat_reference": "prod:EM-1000001",
        "cf_source": "AI chatbot"
      }
    }
  ]
}
```

`docs/api-shapes/zoho-comment.json`:

```json
{
  "_source": "Not a capture. Built from Zoho's published OpenAPI files (https://github.com/zoho/zohodesk-oas, v1.0/TicketComment.json: createTicketComment takes content (at most 32000), isPublic and contentType plainText or html, and answers 200 with commentResponse, whose example this follows; the list answer wraps comments in data). Every value is fake. scripts/zoho/probe.py and scripts/zoho/test_ticket.py replace this file with a masked capture (spec 2026-10-05, part 1).",
  "id": "4000000529001",
  "content": "[stage:EM-1000001 transcript, turns 1-2]",
  "contentType": "plainText",
  "isPublic": false,
  "commentedTime": 1759653060000,
  "modifiedTime": null,
  "commenterId": "4000000008692",
  "attachments": []
}
```

`docs/api-shapes/zoho-attachment.json`:

```json
{
  "_source": "Not a capture. Built from Zoho's published OpenAPI files (https://github.com/zoho/zohodesk-oas, v1.0/TicketAttachment.json: createTicketAttachment takes the multipart field file and the query isPublic, and answers 200 with attachmentResponse; a name is at most 100 characters; the list answer wraps attachments in data, or is 204). Every value is fake. scripts/zoho/probe.py and scripts/zoho/test_ticket.py replace this file with a masked capture (spec 2026-10-05, part 1).",
  "id": "4000000008892",
  "name": "EM-1000001-photo-1.jpg",
  "size": "48213",
  "href": "https://desk.zoho.in/api/v1/tickets/4000000528005/attachments/4000000008892/content",
  "isPublic": false,
  "createdTime": "2026-10-05T08:31:00.000Z"
}
```

`docs/api-shapes/zoho-token.json`:

```json
{
  "_source": "Not a capture, and not in Zoho Desk's OpenAPI files: the token endpoint belongs to Zoho Accounts. The success keys are the ones the OMS reads in production (em-biz-backend zoho/zoho_api_client.py, get_new_token: access_token, token_type, expires_in). The error bodies are the ones the design names (spec 2026-10-05, section 1: 'Access Denied' with HTTP 200 when throttling, invalid_code, invalid_client, invalid_client_secret). Values are fake. scripts/zoho/probe.py replaces this file with a masked capture (spec 2026-10-05, part 1).",
  "refresh": {
    "access_token": "[masked]",
    "token_type": "Bearer",
    "expires_in": 3600
  },
  "access_denied": {"error": "Access Denied"},
  "invalid_code": {"error": "invalid_code"},
  "invalid_client": {"error": "invalid_client"},
  "invalid_client_secret": {"error": "invalid_client_secret"}
}
```

`docs/api-shapes/zoho-errors.json`:

```json
{
  "_source": "Not a capture. The bodies are Zoho's own examples and error codes from its published OpenAPI files (https://github.com/zoho/zohodesk-oas, v1.0/Common.json: errorJson, invalidDataErrorResponse, badRequestErrorResponse and the errorCode list). Codes with no example there carry errorCode only. scripts/zoho/probe.py and scripts/zoho/test_ticket.py replace this file with masked captures of the errors they meet (spec 2026-10-05, part 1).",
  "invalid_data": {
    "errorCode": "INVALID_DATA",
    "message": "The data does not comply to the validation restrictions defined.",
    "errors": [
      {"fieldName": "/contactId", "errorType": "invalid"},
      {"fieldName": "/departmentId", "errorType": "invalid"}
    ]
  },
  "bad_request": {"errorCode": "BAD_REQUEST", "message": "You are not authorized to perform this operation."},
  "invalid_oauth": {"errorCode": "INVALID_OAUTH", "message": "The OauthToken is invalid or has expired."},
  "forbidden": {"errorCode": "FORBIDDEN", "message": "You are not authorized to access this resource."},
  "url_not_found": {"errorCode": "URL_NOT_FOUND", "message": "The URL you requested could not be found."},
  "scope_mismatch": {"errorCode": "SCOPE_MISMATCH"},
  "oauth_org_mismatch": {"errorCode": "OAUTH_ORG_MISMATCH"},
  "license_access_limited": {"errorCode": "LICENSE_ACCESS_LIMITED"},
  "resource_size_exceeded": {"errorCode": "RESOURCE_SIZE_EXCEEDED"},
  "too_many_requests": {"errorCode": "TOO_MANY_REQUESTS"},
  "threshold_exceeded": {"errorCode": "THRESHOLD_EXCEEDED"}
}
```

Create `tests/fake_zoho.py`:

```python
"""A fake Zoho for the suite: an `opener` double, so no test opens a socket.

This follows the pattern of tests/test_oms.py: the client under test takes an
opener, and this one records every request and answers from a script. Bodies
come from the shapes in docs/api-shapes/zoho-*.json, so the fake answers what
the shapes say Zoho answers. tests/test_zoho_desk.py ShapeTests holds the
client to the keys those shapes carry. Part 1's probe replaces the shapes
with captures.

It can answer with any status, a JSON or raw body and headers, including HTTP
200 throttling bodies from the token endpoint and 204 with no body. It can
also raise an exception, either before any answer (socket.timeout, URLError,
ConnectionResetError) or while the body is read.
"""

import http.client
import io
import json
import os
import threading
import urllib.error
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from emotorad_ai.zoho.settings import load_zoho_settings

SHAPES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "api-shapes")
TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
DESK_HOST = "desk.zoho.in"

# Fake values only. The three credentials are marked so that a leak is plain
# in any assertion message.
REFRESH = "1000.refresh-DO-NOT-LEAK"
CLIENT_ID = "1000.CLIENTID-DO-NOT-LEAK"
CLIENT_SECRET = "client-secret-DO-NOT-LEAK"
SECRETS = (REFRESH, CLIENT_ID, CLIENT_SECRET)
ORG_ID = "60000000001"
TEST_DEPARTMENT = "4000000000001"
TEST_CONTACT = "4000000000002"
LIVE_DEPARTMENT = "4000000000003"
UNVERIFIED_CONTACT = "4000000000004"
CF_CHAT_REFERENCE = "cf_chat_reference"
CF_SOURCE = "cf_source"

ENV = {
    "EMOTORAD_ZOHO_REFRESH_TOKEN": REFRESH,
    "EMOTORAD_ZOHO_CLIENT_ID": CLIENT_ID,
    "EMOTORAD_ZOHO_CLIENT_SECRET": CLIENT_SECRET,
    "EMOTORAD_ZOHO_ORG_ID": ORG_ID,
    "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID": TEST_DEPARTMENT,
    "EMOTORAD_ZOHO_TEST_CONTACT_ID": TEST_CONTACT,
    "EMOTORAD_ZOHO_CF_CHAT_REFERENCE": CF_CHAT_REFERENCE,
    "EMOTORAD_ZOHO_CF_SOURCE": CF_SOURCE,
    "EMOTORAD_AI_ENV": "stage",
}
LIVE_ENV = dict(ENV, EMOTORAD_ZOHO_DEPARTMENT_ID=LIVE_DEPARTMENT,
                EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID=UNVERIFIED_CONTACT, EMOTORAD_ZOHO_LIVE="yes")


def zoho_settings(live: bool = False, **env_changes: str):
    """Settings from the fake environment, test mode unless `live`."""
    env = dict(LIVE_ENV if live else ENV)
    env.update(env_changes)
    settings, status = load_zoho_settings(env)
    if settings is None:
        raise AssertionError("the fake settings did not load: %s" % status)
    return settings


def _without_notes(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_notes(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_without_notes(item) for item in value]
    return value


def shape(name: str) -> Any:
    """A recorded shape without its `_source` note, read fresh each time."""
    with open(os.path.join(SHAPES, name), encoding="utf-8") as handle:
        return _without_notes(json.load(handle))


def token_answer(n: int) -> Dict[str, Any]:
    """The recorded token answer, with a token the tests can tell apart."""
    answer = shape("zoho-token.json")["refresh"]
    answer["access_token"] = "tok-%d" % n
    return answer


def headers_of(pairs: Dict[str, str]) -> http.client.HTTPMessage:
    message = http.client.HTTPMessage()
    for name, value in pairs.items():
        message[name] = str(value)
    return message


class Answer:
    """One scripted answer: a status, a JSON or raw body and headers. It can
    also carry an error raised while the body is read."""

    def __init__(self, status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None,
                 read_error: Optional[BaseException] = None) -> None:
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        self.read_error = read_error

    def raw(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            return self.body
        return json.dumps(self.body).encode("utf-8")

    def __repr__(self) -> str:
        return "Answer(%d)" % self.status


class _Response(io.BytesIO):
    def __init__(self, status: int, raw: bytes, headers: http.client.HTTPMessage,
                 read_error: Optional[BaseException] = None) -> None:
        super().__init__(raw)
        self.status = status
        self.headers = headers
        self._read_error = read_error

    def getcode(self) -> int:
        return self.status

    def read(self, *args: Any) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return super().read(*args)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


class FakeZoho:
    """Records every request and answers from a script, first in, first out.

    With `auto_token`, the default, a request to the token endpoint gets a
    fresh token (tok-1, tok-2 and so on) and never takes a scripted answer.
    When `token_gate` is set, it runs inside each such request before the
    answer, so a test can hold one refresh open.
    """

    def __init__(self, auto_token: bool = True) -> None:
        self.auto_token = auto_token
        self.token_gate: Optional[Callable[[], None]] = None
        self.requests: List[Dict[str, Any]] = []
        self.answers: List[Any] = []
        self.tokens_issued = 0
        self._lock = threading.Lock()

    def queue(self, *answers: Any) -> "FakeZoho":
        self.answers.extend(answers)
        return self

    def __call__(self, request: Any, timeout: Optional[float] = None) -> _Response:
        parts = urllib.parse.urlsplit(request.full_url)
        record = {
            "method": request.get_method(),
            "url": request.full_url,
            "host": parts.netloc,
            "path": parts.path,
            "query": urllib.parse.parse_qs(parts.query, keep_blank_values=True),
            "headers": {name.lower(): value for name, value in request.header_items()},
            "body": request.data or b"",
            "timeout": timeout,
        }
        with self._lock:
            self.requests.append(record)
        if self.auto_token and request.full_url == TOKEN_URL:
            if self.token_gate is not None:
                self.token_gate()
            with self._lock:
                self.tokens_issued += 1
                issued = self.tokens_issued
            return _Response(200, json.dumps(token_answer(issued)).encode("utf-8"), headers_of({}))
        with self._lock:
            if not self.answers:
                raise AssertionError("FakeZoho has no answer queued for %s %s" % (record["method"], parts.path))
            answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        raw = answer.raw()
        headers = headers_of(answer.headers)
        if answer.status >= 400:
            raise urllib.error.HTTPError(request.full_url, answer.status, "fake", headers, io.BytesIO(raw))
        return _Response(answer.status, raw, headers, answer.read_error)

    @property
    def desk_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["host"] == DESK_HOST]

    @property
    def token_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["url"] == TOKEN_URL]
```

Create `tests/test_zoho_http.py`:

```python
"""One call to Zoho, classified by Zoho's error code (spec 2026-10-05, section 4).

Every row of the spec's answers table, against tests/fake_zoho.py. No socket
is opened. The privacy tests matter as much as the rest, because these
messages reach the log, and through the registry, the model.
"""

import http.client
import socket
import unittest
import urllib.error

from emotorad_ai.zoho.errors import (
    ZohoAuthExpired,
    ZohoBusy,
    ZohoConfigError,
    ZohoCreditsExhausted,
    ZohoError,
    ZohoGone,
    ZohoRejected,
    ZohoTooLarge,
    ZohoUnavailable,
    ZohoUnknownOutcome,
    safe_code,
)
from emotorad_ai.zoho.http import CREDITS_HEADER, DeskHTTP
from tests.fake_zoho import ORG_ID, Answer, FakeZoho, shape

URL = "https://desk.zoho.in/api/v1/tickets"
TOKEN = "tok-SECRET-DO-NOT-LEAK"
HEADERS = {"Authorization": "Zoho-oauthtoken " + TOKEN, "orgId": ORG_ID, "Accept": "application/json"}
PHONE = "+919999999999"


def zoho_error(name):
    return shape("zoho-errors.json")[name]


def http_with(*answers):
    fake = FakeZoho(auto_token=False).queue(*answers)
    return DeskHTTP(opener=fake), fake


class AnswerTests(unittest.TestCase):
    def test_a_200_comes_back_parsed_and_the_credits_left_are_kept(self):
        ticket = shape("zoho-ticket.json")
        transport, _ = http_with(Answer(200, ticket, headers={CREDITS_HEADER: "48712"}))
        self.assertEqual(transport.call("POST", URL, HEADERS, b"{}", write=True), (200, ticket))
        self.assertEqual(transport.last_credits_remaining, 48712)

    def test_a_204_is_no_body_not_an_error(self):
        for write in (False, True):
            with self.subTest(write=write):
                transport, _ = http_with(Answer(204))
                self.assertEqual(transport.call("GET", URL, HEADERS, write=write), (204, None))

    def test_401_invalid_oauth_is_an_expired_token(self):
        transport, _ = http_with(Answer(401, zoho_error("invalid_oauth")))
        with self.assertRaises(ZohoAuthExpired) as caught:
            transport.call("GET", URL, HEADERS, write=False)
        self.assertEqual(caught.exception.error, "INVALID_OAUTH")

    def test_scope_organisation_access_and_licence_are_configuration(self):
        named = (("scope_mismatch", "SCOPE_MISMATCH"), ("oauth_org_mismatch", "OAUTH_ORG_MISMATCH"),
                 ("forbidden", "FORBIDDEN"), ("license_access_limited", "LICENSE_ACCESS_LIMITED"))
        for status in (401, 403):
            for name, code in named:
                with self.subTest(status=status, code=code):
                    transport, _ = http_with(Answer(status, zoho_error(name)))
                    with self.assertRaises(ZohoConfigError) as caught:
                        transport.call("POST", URL, HEADERS, b"{}", write=True)
                    self.assertEqual(caught.exception.error, code)

    def test_a_401_or_403_with_no_code_is_configuration_named_by_status(self):
        for status in (401, 403):
            with self.subTest(status=status):
                transport, _ = http_with(Answer(status, b""))
                with self.assertRaises(ZohoConfigError) as caught:
                    transport.call("GET", URL, HEADERS, write=False)
                self.assertEqual(caught.exception.error, "http_%d" % status)

    def test_invalid_data_names_the_fields_zoho_named(self):
        for status in (400, 422):
            with self.subTest(status=status):
                transport, _ = http_with(Answer(status, zoho_error("invalid_data")))
                with self.assertRaises(ZohoRejected) as caught:
                    transport.call("POST", URL, HEADERS, b"{}", write=True)
                self.assertEqual(caught.exception.error, "INVALID_DATA")
                self.assertEqual(caught.exception.fields, ("contactId", "departmentId"))
                self.assertIn("contactId", str(caught.exception))

    def test_404_is_gone(self):
        transport, _ = http_with(Answer(404, zoho_error("url_not_found")))
        with self.assertRaises(ZohoGone) as caught:
            transport.call("GET", URL + "/4000000528005", HEADERS, write=False)
        self.assertEqual(caught.exception.error, "URL_NOT_FOUND")

    def test_413_is_too_large(self):
        transport, _ = http_with(Answer(413, zoho_error("resource_size_exceeded")))
        with self.assertRaises(ZohoTooLarge):
            transport.call("POST", URL, HEADERS, b"x", write=True)

    def test_429_too_many_requests_is_busy(self):
        transport, _ = http_with(Answer(429, zoho_error("too_many_requests")))
        with self.assertRaises(ZohoBusy):
            transport.call("GET", URL, HEADERS, write=False)

    def test_429_threshold_exceeded_carries_retry_after_and_the_credits(self):
        transport, _ = http_with(Answer(429, zoho_error("threshold_exceeded"),
                                        headers={"Retry-After": "3600", CREDITS_HEADER: "0"}))
        with self.assertRaises(ZohoCreditsExhausted) as caught:
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        self.assertEqual(caught.exception.retry_after_seconds, 3600.0)
        self.assertEqual(transport.last_credits_remaining, 0)

    def test_threshold_exceeded_without_a_readable_retry_after_has_no_wait(self):
        for headers in ({}, {"Retry-After": "soon"}, {"Retry-After": "-5"}):
            with self.subTest(headers=headers):
                transport, _ = http_with(Answer(429, zoho_error("threshold_exceeded"), headers=headers))
                with self.assertRaises(ZohoCreditsExhausted) as caught:
                    transport.call("GET", URL, HEADERS, write=False)
                self.assertIsNone(caught.exception.retry_after_seconds)

    def test_a_5xx_on_a_read_is_unavailable(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                transport, _ = http_with(Answer(status, b"<html>bad gateway</html>"))
                with self.assertRaises(ZohoUnavailable) as caught:
                    transport.call("GET", URL, HEADERS, write=False)
                self.assertEqual(caught.exception.error, "http_%d" % status)

    def test_a_5xx_on_a_write_is_an_unknown_outcome(self):
        transport, _ = http_with(Answer(503, b""))
        with self.assertRaises(ZohoUnknownOutcome):
            transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_a_2xx_body_that_is_not_json(self):
        transport, _ = http_with(Answer(200, b"<html>ok</html>"))
        with self.assertRaises(ZohoUnavailable):
            transport.call("GET", URL, HEADERS, write=False)
        transport, _ = http_with(Answer(200, b"<html>ok</html>"))
        with self.assertRaises(ZohoUnknownOutcome):
            transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_unclassified_answers_come_back_as_they_came(self):
        # The token endpoint's body says what went wrong, whatever the status.
        refused = shape("zoho-token.json")["invalid_client"]
        transport, _ = http_with(Answer(400, refused), Answer(500, b"<html></html>"))
        self.assertEqual(transport.call("POST", URL, HEADERS, b"", write=False, classify=False), (400, refused))
        self.assertEqual(transport.call("POST", URL, HEADERS, b"", write=False, classify=False), (500, None))


class NetworkTests(unittest.TestCase):
    # urllib wraps a failure while the request is being sent in URLError
    # (urllib.request.AbstractHTTPHandler.do_open) and lets one raised while
    # waiting for, or reading, the answer through as it is. So URLError means
    # Zoho never had the whole request.
    NOT_SENT = (
        urllib.error.URLError(socket.timeout("timed out")),
        urllib.error.URLError(ConnectionRefusedError()),
        urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")),
    )
    AFTER_SENDING = (socket.timeout("timed out"), ConnectionResetError(), http.client.RemoteDisconnected("closed"))

    def test_a_request_that_was_never_sent_is_unavailable_even_for_a_write(self):
        for exc in self.NOT_SENT:
            for write in (False, True):
                with self.subTest(reason=type(exc.reason).__name__, write=write):
                    transport, _ = http_with(exc)
                    with self.assertRaises(ZohoUnavailable):
                        transport.call("POST", URL, HEADERS, b"{}", write=write)

    def test_a_write_with_no_answer_after_sending_has_an_unknown_outcome(self):
        for exc in self.AFTER_SENDING:
            with self.subTest(exc=type(exc).__name__):
                transport, _ = http_with(exc)
                with self.assertRaises(ZohoUnknownOutcome):
                    transport.call("POST", URL, HEADERS, b"{}", write=True)

    def test_a_read_with_no_answer_is_unavailable(self):
        for exc in self.AFTER_SENDING:
            with self.subTest(exc=type(exc).__name__):
                transport, _ = http_with(exc)
                with self.assertRaises(ZohoUnavailable):
                    transport.call("GET", URL, HEADERS, write=False)

    def test_a_body_cut_off_after_a_200(self):
        cut = Answer(200, shape("zoho-ticket.json"), read_error=http.client.IncompleteRead(b""))
        transport, _ = http_with(cut)
        with self.assertRaises(ZohoUnknownOutcome):
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        transport, _ = http_with(cut)
        with self.assertRaises(ZohoUnavailable):
            transport.call("GET", URL, HEADERS, write=False)

    def test_a_timeout_is_named_timeout_and_anything_else_network(self):
        transport, _ = http_with(socket.timeout("timed out"))
        with self.assertRaises(ZohoUnavailable) as caught:
            transport.call("GET", URL, HEADERS, write=False)
        self.assertEqual(caught.exception.error, "timeout")
        transport, _ = http_with(urllib.error.URLError(ConnectionRefusedError()))
        with self.assertRaises(ZohoUnavailable) as caught:
            transport.call("GET", URL, HEADERS, write=False)
        self.assertEqual(caught.exception.error, "network")


class RequestTests(unittest.TestCase):
    def test_the_request_goes_as_given_with_the_default_timeout(self):
        transport, fake = http_with(Answer(200, {"id": "1"}))
        transport.call("POST", URL, HEADERS, b'{"a": 1}', write=True)
        sent = fake.requests[0]
        self.assertEqual((sent["method"], sent["url"], sent["body"], sent["timeout"]), ("POST", URL, b'{"a": 1}', 8.0))
        self.assertEqual(sent["headers"]["orgid"], ORG_ID)
        self.assertEqual(sent["headers"]["authorization"], "Zoho-oauthtoken " + TOKEN)

    def test_a_timeout_can_be_given_per_call(self):
        transport, fake = http_with(Answer(200, {"id": "1"}))
        transport.call("POST", URL, HEADERS, b"x", write=True, timeout=60.0)
        self.assertEqual(fake.requests[0]["timeout"], 60.0)

    def test_a_header_with_a_line_break_is_refused_before_the_network(self):
        transport, fake = http_with()
        with self.assertRaises(ZohoConfigError) as caught:
            transport.call("GET", URL, {"Authorization": "Zoho-oauthtoken tok-SECRET\nX-Injected: 1"}, write=False)
        self.assertEqual(fake.requests, [])
        self.assertEqual(caught.exception.error, "bad_header")
        self.assertNotIn("tok-SECRET", str(caught.exception))


class PrivacyTests(unittest.TestCase):
    LEAKY = {
        "errorCode": "INVALID_DATA",
        "message": "phone %s rejected, token %s" % (PHONE, TOKEN),
        "errors": [{"fieldName": "/phone", "errorType": "invalid", "errorMessage": PHONE}],
    }

    def assert_clean(self, exc):
        shown = " ".join([str(exc), repr(exc), exc.error] + [str(arg) for arg in exc.args]
                         + list(getattr(exc, "fields", ())))
        for secret in (PHONE, PHONE[3:], TOKEN):
            self.assertNotIn(secret, shown)

    def test_no_message_carries_a_body_a_token_or_a_phone(self):
        answers = [
            Answer(400, self.LEAKY), Answer(401, dict(self.LEAKY, errorCode="INVALID_OAUTH")),
            Answer(403, dict(self.LEAKY, errorCode="FORBIDDEN")), Answer(404, self.LEAKY), Answer(413, self.LEAKY),
            Answer(429, dict(self.LEAKY, errorCode="THRESHOLD_EXCEEDED")), Answer(500, self.LEAKY),
            Answer(200, b"not json " + PHONE.encode("ascii")), socket.timeout(PHONE), urllib.error.URLError(PHONE),
        ]
        for answer in answers:
            for write in (False, True):
                with self.subTest(answer=answer, write=write):
                    transport, _ = http_with(answer)
                    with self.assertRaises(ZohoError) as caught:
                        transport.call("POST", URL, HEADERS, b"{}", write=write)
                    self.assert_clean(caught.exception)

    def test_a_code_that_is_not_a_code_is_not_passed_on(self):
        transport, _ = http_with(Answer(400, {"errorCode": "call me on " + PHONE}))
        with self.assertRaises(ZohoRejected) as caught:
            transport.call("POST", URL, HEADERS, b"{}", write=True)
        self.assertEqual(caught.exception.error, "http_400")

    def test_safe_code_knows_a_code_from_text(self):
        self.assertEqual(safe_code("INVALID_OAUTH", "x"), "INVALID_OAUTH")
        self.assertEqual(safe_code("invalid_client_secret", "x"), "invalid_client_secret")
        for text in ("Access Denied", PHONE, "", None, "9999999999", "a" * 61):
            self.assertEqual(safe_code(text, "fallback"), "fallback")

    def test_repr_holds_no_header(self):
        transport, _ = http_with(Answer(200, {}))
        transport.call("GET", URL, HEADERS, write=False)
        self.assertNotIn(TOKEN, repr(transport))


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_zoho_auth.py`:

```python
"""The Desk access token (spec 2026-10-05, section 1).

The token answer is read by its body, whatever the status. Zoho throttles with
HTTP 200 "Access Denied", and a rotated OMS secret shows as
invalid_client_secret.
"""

import socket
import threading
import unittest
import urllib.parse

from emotorad_ai.zoho.auth import (
    REFRESH_EARLY_SECONDS,
    REFUSED_WAIT_SECONDS,
    THROTTLE_WAIT_SECONDS,
    TOKEN_URL,
    TokenSource,
)
from emotorad_ai.zoho.errors import ZohoError, ZohoTokenRefused, ZohoTokenThrottled, ZohoUnavailable
from emotorad_ai.zoho.http import DeskHTTP
from tests.fake_zoho import (
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH,
    SECRETS,
    Answer,
    FakeZoho,
    shape,
    token_answer,
    zoho_settings,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def source(*answers, auto_token=False):
    fake = FakeZoho(auto_token=auto_token).queue(*answers)
    clock = Clock()
    return TokenSource(zoho_settings(), DeskHTTP(opener=fake), clock=clock), fake, clock


def token_body(name):
    return shape("zoho-token.json")[name]


class TokenTests(unittest.TestCase):
    def test_the_refresh_token_goes_in_a_form_body_never_the_address(self):
        tokens, fake, _ = source(Answer(200, token_answer(1)))
        self.assertEqual(tokens.token(), "tok-1")
        sent = fake.requests[0]
        self.assertEqual(TOKEN_URL, "https://accounts.zoho.in/oauth/v2/token")
        self.assertEqual((sent["method"], sent["url"], sent["query"]), ("POST", TOKEN_URL, {}))
        self.assertEqual(sent["headers"]["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(urllib.parse.parse_qs(sent["body"].decode("ascii")), {
            "refresh_token": [REFRESH], "client_id": [CLIENT_ID], "client_secret": [CLIENT_SECRET],
            "grant_type": ["refresh_token"],
        })
        self.assertEqual(tokens.state, "ok")

    def test_the_token_is_kept_until_five_minutes_before_its_hour_ends(self):
        tokens, fake, clock = source(Answer(200, token_answer(1)), Answer(200, token_answer(2)))
        self.assertEqual(REFRESH_EARLY_SECONDS, 300)
        self.assertEqual(tokens.token(), "tok-1")
        clock.now += 3600 - REFRESH_EARLY_SECONDS - 1
        self.assertEqual(tokens.token(), "tok-1")
        self.assertEqual(len(fake.requests), 1)
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-2")
        self.assertEqual(len(fake.requests), 2)

    def test_a_missing_lifetime_is_taken_as_an_hour(self):
        no_lifetime = {key: value for key, value in token_answer(1).items() if key != "expires_in"}
        tokens, _, clock = source(Answer(200, no_lifetime), Answer(200, token_answer(2)))
        tokens.token()
        clock.now += 3600 - 300 - 1
        self.assertEqual(tokens.token(), "tok-1")
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-2")

    def test_invalidate_drops_the_token_so_the_next_call_fetches_one(self):
        tokens, fake, _ = source(Answer(200, token_answer(1)), Answer(200, token_answer(2)))
        tokens.token()
        tokens.invalidate()
        self.assertEqual(tokens.token(), "tok-2")
        self.assertEqual(len(fake.requests), 2)


class ThrottleTests(unittest.TestCase):
    def test_access_denied_with_http_200_stops_token_requests_for_ten_minutes(self):
        tokens, fake, clock = source(Answer(200, token_body("access_denied")), Answer(200, token_answer(1)))
        self.assertEqual(THROTTLE_WAIT_SECONDS, 600)
        with self.assertRaises(ZohoTokenThrottled):
            tokens.token()
        self.assertEqual(tokens.state, "throttled")
        clock.now += THROTTLE_WAIT_SECONDS - 1
        with self.assertRaises(ZohoTokenThrottled):
            tokens.token()
        self.assertEqual(len(fake.requests), 1)
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-1")
        self.assertEqual(tokens.state, "ok")


class RefusalTests(unittest.TestCase):
    def test_a_rotated_secret_sets_the_health_state_and_waits_an_hour(self):
        tokens, fake, clock = source(Answer(400, token_body("invalid_client_secret")), Answer(200, token_answer(1)))
        self.assertEqual(REFUSED_WAIT_SECONDS, 3600)
        with self.assertRaises(ZohoTokenRefused) as caught:
            tokens.token()
        self.assertEqual(caught.exception.error, "invalid_client_secret")
        self.assertEqual(tokens.state, "token refused: invalid_client_secret")
        clock.now += REFUSED_WAIT_SECONDS - 1
        with self.assertRaises(ZohoTokenRefused):
            tokens.token()
        self.assertEqual(len(fake.requests), 1)
        clock.now += 1
        self.assertEqual(tokens.token(), "tok-1")
        self.assertEqual(tokens.state, "ok")

    def test_each_refusal_is_named_whatever_the_status(self):
        for name, status in (("invalid_code", 200), ("invalid_client", 401), ("invalid_client_secret", 200)):
            with self.subTest(name=name):
                tokens, _, _ = source(Answer(status, token_body(name)))
                with self.assertRaises(ZohoTokenRefused):
                    tokens.token()
                self.assertEqual(tokens.state, "token refused: %s" % name)


class FailureTests(unittest.TestCase):
    def test_a_network_failure_is_unavailable_and_leaves_the_state_alone(self):
        tokens, _, _ = source(socket.timeout("timed out"), Answer(200, token_answer(1)))
        with self.assertRaises(ZohoUnavailable):
            tokens.token()
        self.assertEqual(tokens.state, "ok")
        self.assertEqual(tokens.token(), "tok-1")

    def test_an_answer_without_a_token_is_unavailable(self):
        answers = (Answer(200, {}), Answer(200, {"token_type": "Bearer"}), Answer(200, {"access_token": ""}),
                   Answer(400, {"error": "invalid_request"}), Answer(200, {"error": {"nested": True}}),
                   Answer(502, b""), Answer(200, b"<html></html>"))
        for answer in answers:
            with self.subTest(answer=answer):
                tokens, _, _ = source(answer)
                with self.assertRaises(ZohoUnavailable):
                    tokens.token()
                self.assertEqual(tokens.state, "ok")


class ConcurrencyTests(unittest.TestCase):
    def test_callers_at_the_same_moment_share_one_refresh(self):
        fake = FakeZoho(auto_token=True)
        entered, release = threading.Event(), threading.Event()

        def hold():
            entered.set()
            release.wait(5)

        fake.token_gate = hold
        tokens = TokenSource(zoho_settings(), DeskHTTP(opener=fake), clock=Clock())
        got = []
        first = threading.Thread(target=lambda: got.append(tokens.token()))
        first.start()
        self.assertTrue(entered.wait(5))
        second = threading.Thread(target=lambda: got.append(tokens.token()))
        second.start()
        second.join(0.2)  # time to reach the lock (or, without one, Zoho)
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(got, ["tok-1", "tok-1"])
        self.assertEqual(len(fake.token_requests), 1)


class SecretTests(unittest.TestCase):
    def test_no_secret_or_token_reaches_a_message_or_the_state(self):
        shown = []
        for status, body in ((200, token_body("access_denied")), (400, token_body("invalid_client_secret")),
                             (400, {"error": "invalid_request"}), (502, b"")):
            tokens, _, _ = source(Answer(status, body))
            with self.assertRaises(ZohoError) as caught:
                tokens.token()
            shown += [str(caught.exception), repr(caught.exception), caught.exception.error, tokens.state, repr(tokens)]
        tokens, _, _ = source(Answer(200, token_answer(1)))
        tokens.token()
        shown += [tokens.state, repr(tokens)]
        for text in shown:
            for secret in SECRETS + ("tok-1",):
                self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_zoho_desk.py`:

```python
"""The Desk calls the worker makes (spec 2026-10-05, sections 4 and 5), and the
recorded shapes the fake answers from.

All run against tests/fake_zoho.py. The shapes in docs/api-shapes/zoho-*.json
are built from Zoho's published OpenAPI files and the OMS's production code
until part 1's probe replaces them. ShapeTests is the contract between them
and the client.
"""

import json
import os
import socket
import unittest

from emotorad_ai.zoho.auth import TokenSource
from emotorad_ai.zoho.desk import (
    COMMENT_LIMIT,
    NAME_LIMIT,
    UPLOAD_TIMEOUT,
    DeskClient,
    find_adoptable,
    safe_filename,
)
from emotorad_ai.zoho.errors import ZohoAuthExpired, ZohoConfigError, ZohoRejected, ZohoUnavailable, ZohoUnknownOutcome
from emotorad_ai.zoho.http import DeskHTTP
from tests.fake_zoho import (
    CF_CHAT_REFERENCE,
    ORG_ID,
    SHAPES,
    TEST_CONTACT,
    TEST_DEPARTMENT,
    Answer,
    FakeZoho,
    shape,
    zoho_settings,
)

TICKET_ID = "4000000528005"
ZOHO_SHAPES = ("zoho-ticket.json", "zoho-contact-search.json", "zoho-contact-tickets.json", "zoho-comment.json",
               "zoho-attachment.json", "zoho-token.json", "zoho-errors.json")


def desk(*answers):
    fake = FakeZoho().queue(*answers)
    settings = zoho_settings()
    transport = DeskHTTP(opener=fake)
    return DeskClient(settings, TokenSource(settings, transport, clock=lambda: 1000.0), transport), fake


def zoho_error(name):
    return shape("zoho-errors.json")[name]


def listing(*items):
    return {"data": list(items)}


class HeaderTests(unittest.TestCase):
    def test_every_call_carries_the_token_and_the_org_id(self):
        client, fake = desk(Answer(204))
        client.search_contacts("phone", "9999999999")
        sent = fake.desk_requests[0]
        self.assertEqual(sent["headers"]["authorization"], "Zoho-oauthtoken tok-1")
        self.assertEqual(sent["headers"]["orgid"], ORG_ID)
        self.assertTrue(sent["url"].startswith("https://desk.zoho.in/api/v1/"))
        self.assertEqual(sent["timeout"], 8.0)


class SearchTests(unittest.TestCase):
    def test_contacts_are_searched_by_the_last_ten_digits_with_a_wildcard(self):
        found = shape("zoho-contact-search.json")
        for field in ("phone", "mobile"):
            with self.subTest(field=field):
                client, fake = desk(Answer(200, found))
                self.assertEqual(client.search_contacts(field, "9999999999"), found["data"])
                sent = fake.desk_requests[0]
                self.assertEqual((sent["method"], sent["path"]), ("GET", "/api/v1/contacts/search"))
                self.assertIn("%s=*9999999999" % field, sent["url"])
                self.assertEqual(sent["query"], {field: ["*9999999999"]})

    def test_204_is_no_match(self):
        client, _ = desk(Answer(204))
        self.assertEqual(client.search_contacts("mobile", "9999999999"), [])

    def test_only_phone_or_mobile_and_only_ten_digits_reach_zoho(self):
        client, fake = desk()
        for field, digits in (("email", "9999999999"), ("phone", "+919999999999"), ("phone", "999999999"),
                              ("phone", "९९९९९९९९९९"), ("mobile", "")):
            with self.subTest(field=field, digits=digits):
                with self.assertRaises(ValueError) as caught:
                    client.search_contacts(field, digits)
                if digits:
                    self.assertNotIn(digits, str(caught.exception))
        self.assertEqual(fake.requests, [])


class ContactTests(unittest.TestCase):
    def test_a_contact_is_made_with_a_last_name_and_a_mobile_and_nothing_else(self):
        client, fake = desk(Answer(200, shape("zoho-contact-search.json")["data"][0]))
        self.assertEqual(client.create_contact("AI chat customer", "+919999999999"), TEST_CONTACT)
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/contacts"))
        self.assertEqual(json.loads(sent["body"]), {"lastName": "AI chat customer", "mobile": "+919999999999"})
        self.assertEqual(sent["headers"]["content-type"], "application/json")

    def test_a_create_answer_with_no_id_is_an_unknown_outcome(self):
        client, _ = desk(Answer(200, {}))
        with self.assertRaises(ZohoUnknownOutcome):
            client.create_contact("AI chat customer", "+919999999999")


class ContactTicketsTests(unittest.TestCase):
    def test_the_contacts_tickets_newest_first_in_one_department(self):
        recorded = shape("zoho-contact-tickets.json")
        client, fake = desk(Answer(200, recorded))
        self.assertEqual(client.contact_tickets(TEST_CONTACT, TEST_DEPARTMENT), recorded["data"])
        sent = fake.desk_requests[0]
        self.assertEqual(sent["path"], "/api/v1/contacts/%s/tickets" % TEST_CONTACT)
        self.assertEqual(sent["query"], {"departmentId": [TEST_DEPARTMENT], "sortBy": ["-createdTime"], "limit": ["50"]})

    def test_no_tickets_is_204(self):
        client, _ = desk(Answer(204))
        self.assertEqual(client.contact_tickets(TEST_CONTACT, TEST_DEPARTMENT, limit=10), [])

    def test_a_list_answer_without_data_is_unavailable(self):
        client, _ = desk(Answer(200, {"tickets": []}))
        with self.assertRaises(ZohoUnavailable):
            client.contact_tickets(TEST_CONTACT, TEST_DEPARTMENT)


class TicketCreateTests(unittest.TestCase):
    def test_a_ticket_is_created_and_its_id_number_and_link_come_back(self):
        made = shape("zoho-ticket.json")
        client, fake = desk(Answer(200, made))
        payload = {"subject": "[AI chat] Battery: charging - EMX Plus", "departmentId": TEST_DEPARTMENT}
        self.assertEqual(client.create_ticket(payload),
                         {"id": made["id"], "ticketNumber": made["ticketNumber"], "webUrl": made["webUrl"]})
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/tickets"))
        self.assertEqual(json.loads(sent["body"]), payload)

    def test_numeric_ids_come_back_as_text(self):
        client, _ = desk(Answer(200, {"id": 4000000528005, "ticketNumber": 1024}))
        self.assertEqual(client.create_ticket({}), {"id": "4000000528005", "ticketNumber": "1024", "webUrl": None})

    def test_an_answer_without_an_id_is_an_unknown_outcome(self):
        client, _ = desk(Answer(200, {"ticketNumber": "1024"}))
        with self.assertRaises(ZohoUnknownOutcome):
            client.create_ticket({"subject": "x"})

    def test_a_create_that_times_out_after_sending_is_not_sent_again(self):
        client, fake = desk(socket.timeout("timed out"))
        with self.assertRaises(ZohoUnknownOutcome):
            client.create_ticket({"subject": "x"})
        self.assertEqual(len(fake.desk_requests), 1)


class CommentTests(unittest.TestCase):
    def test_a_comment_is_private_plain_text(self):
        made = shape("zoho-comment.json")
        client, fake = desk(Answer(200, made))
        text = "[stage:EM-1000001 transcript, turns 1-2]"
        self.assertEqual(client.add_comment(TICKET_ID, text), made["id"])
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/tickets/%s/comments" % TICKET_ID))
        self.assertEqual(json.loads(sent["body"]), {"isPublic": False, "contentType": "plainText", "content": text})

    def test_a_comment_over_zohos_limit_is_never_sent(self):
        client, fake = desk()
        self.assertEqual(COMMENT_LIMIT, 32000)
        with self.assertRaises(ZohoRejected) as caught:
            client.add_comment(TICKET_ID, "x" * (COMMENT_LIMIT + 1))
        self.assertEqual(caught.exception.fields, ("content",))
        self.assertEqual(fake.requests, [])

    def test_comments_are_read_page_by_page(self):
        one = shape("zoho-comment.json")
        full = listing(*[dict(one, id=str(5000000000000 + i)) for i in range(100)])
        rest = listing(*[dict(one, id=str(6000000000000 + i)) for i in range(3)])
        client, fake = desk(Answer(200, full), Answer(200, rest))
        self.assertEqual(len(client.comments(TICKET_ID)), 103)
        pages = [request["query"] for request in fake.desk_requests]
        self.assertEqual([page["from"] for page in pages], [["0"], ["100"]])
        self.assertEqual([page["limit"] for page in pages], [["100"], ["100"]])

    def test_a_ticket_with_no_comments(self):
        client, _ = desk(Answer(204))
        self.assertEqual(client.comments(TICKET_ID), [])


class AttachmentTests(unittest.TestCase):
    DATA = b"\x89PNG\r\n\x1a\n fake photo bytes"

    def test_a_file_goes_as_the_multipart_field_file_privately_with_a_long_timeout(self):
        made = shape("zoho-attachment.json")
        client, fake = desk(Answer(200, made))
        self.assertEqual(client.upload_attachment(TICKET_ID, "EM-1000001-photo-1.png", self.DATA, "image/png"),
                         made["id"])
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/tickets/%s/attachments" % TICKET_ID))
        self.assertEqual(sent["query"], {"isPublic": ["false"]})
        self.assertEqual(UPLOAD_TIMEOUT, 60.0)
        self.assertEqual(sent["timeout"], UPLOAD_TIMEOUT)
        content_type = sent["headers"]["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        self.assertEqual(sent["body"], b"".join([
            b"--", boundary, b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="EM-1000001-photo-1.png"\r\n',
            b"Content-Type: image/png\r\n\r\n",
            self.DATA, b"\r\n--", boundary, b"--\r\n",
        ]))

    def test_each_upload_has_its_own_boundary(self):
        client, fake = desk(Answer(200, {"id": "1"}), Answer(200, {"id": "2"}))
        client.upload_attachment(TICKET_ID, "a.png", self.DATA, "image/png")
        client.upload_attachment(TICKET_ID, "b.png", self.DATA, "image/png")
        first, second = (request["headers"]["content-type"] for request in fake.desk_requests)
        self.assertNotEqual(first, second)

    def test_a_file_name_is_made_safe_for_the_header_and_for_zohos_length(self):
        self.assertEqual(safe_filename('EM-1000001 "photo"\r\n.jpg'), "EM-1000001_photo_.jpg")
        self.assertEqual(safe_filename("फोटो.jpg"), "_.jpg")
        traversal = safe_filename("../../etc/passwd")
        self.assertNotIn("/", traversal)
        self.assertFalse(traversal.startswith("."))
        long_name = "EM-1000001-" + "x" * 200 + ".jpeg"
        self.assertEqual(NAME_LIMIT, 100)
        self.assertEqual(len(safe_filename(long_name)), NAME_LIMIT)
        self.assertTrue(safe_filename(long_name).endswith(".jpeg"))
        self.assertEqual(safe_filename(""), "attachment")

    def test_the_uploaded_name_is_the_safe_one(self):
        client, fake = desk(Answer(200, {"id": "1"}))
        client.upload_attachment(TICKET_ID, 'a"b.png', self.DATA, "image/png")
        self.assertIn(b'filename="a_b.png"', fake.desk_requests[0]["body"])

    def test_an_odd_mime_type_goes_as_octet_stream(self):
        client, fake = desk(Answer(200, {"id": "1"}))
        client.upload_attachment(TICKET_ID, "a.bin", self.DATA, "image/png\r\nX-Evil: 1")
        self.assertIn(b"Content-Type: application/octet-stream\r\n", fake.desk_requests[0]["body"])
        self.assertNotIn(b"X-Evil", fake.desk_requests[0]["body"])

    def test_attachments_are_listed(self):
        one = shape("zoho-attachment.json")
        client, fake = desk(Answer(200, listing(one)))
        self.assertEqual(client.attachments(TICKET_ID), [one])
        self.assertEqual(fake.desk_requests[0]["path"], "/api/v1/tickets/%s/attachments" % TICKET_ID)


class ExpiredTokenTests(unittest.TestCase):
    def test_an_expired_token_is_refreshed_once_and_the_step_sent_once_more(self):
        made = shape("zoho-ticket.json")
        client, fake = desk(Answer(401, zoho_error("invalid_oauth")), Answer(200, made))
        self.assertEqual(client.create_ticket({"subject": "x"})["id"], made["id"])
        self.assertEqual(len(fake.token_requests), 2)
        self.assertEqual([request["headers"]["authorization"] for request in fake.desk_requests],
                         ["Zoho-oauthtoken tok-1", "Zoho-oauthtoken tok-2"])

    def test_a_second_expiry_is_raised_not_retried_again(self):
        client, fake = desk(Answer(401, zoho_error("invalid_oauth")), Answer(401, zoho_error("invalid_oauth")))
        with self.assertRaises(ZohoAuthExpired):
            client.add_comment(TICKET_ID, "note")
        self.assertEqual(len(fake.desk_requests), 2)
        self.assertEqual(len(fake.token_requests), 2)

    def test_a_read_is_retried_the_same_way(self):
        client, fake = desk(Answer(401, zoho_error("invalid_oauth")), Answer(204))
        self.assertEqual(client.search_contacts("phone", "9999999999"), [])
        self.assertEqual(len(fake.desk_requests), 2)


class IdTests(unittest.TestCase):
    def test_an_id_that_is_not_a_zoho_id_never_reaches_a_path(self):
        client, fake = desk()
        for bad in ("../contacts", TICKET_ID + "/comments", "", None, "12a"):
            with self.subTest(bad=bad):
                with self.assertRaises(ZohoConfigError) as caught:
                    client.add_comment(bad, "note")
                self.assertEqual(caught.exception.error, "bad_id")
        with self.assertRaises(ZohoConfigError):
            client.contact_tickets(TEST_CONTACT, "dept")
        self.assertEqual(fake.requests, [])


class AdoptTests(unittest.TestCase):
    def ticket(self, value, name=CF_CHAT_REFERENCE):
        return {"id": "1", "cf": {name: value}}

    def test_only_the_exact_chat_reference_is_adopted(self):
        wanted = "stage:EM-1000001"
        near = [self.ticket("stage:EM-1000001 "), self.ticket("STAGE:EM-1000001"), self.ticket("stage:EM-10000011"),
                self.ticket("prod:EM-1000001"), self.ticket(wanted, name="cf_other"), {"id": "2"},
                {"id": "3", "cf": None}, "not a ticket"]
        self.assertIsNone(find_adoptable(near, CF_CHAT_REFERENCE, wanted))
        match = dict(self.ticket(wanted), id="4")
        self.assertIs(find_adoptable(near + [match], CF_CHAT_REFERENCE, wanted), match)

    def test_an_empty_reference_adopts_nothing(self):
        self.assertIsNone(find_adoptable([self.ticket("")], CF_CHAT_REFERENCE, ""))

    def test_the_recorded_list_can_be_adopted_from(self):
        recorded = shape("zoho-contact-tickets.json")["data"]
        self.assertEqual(find_adoptable(recorded, CF_CHAT_REFERENCE, "stage:EM-1000001")["id"], TICKET_ID)
        self.assertEqual(find_adoptable(recorded, CF_CHAT_REFERENCE, "prod:EM-1000001")["id"], "4000000527001")


class ShapeTests(unittest.TestCase):
    def test_every_shape_says_where_it_came_from_and_what_replaces_it(self):
        for name in ZOHO_SHAPES:
            with self.subTest(name=name):
                with open(os.path.join(SHAPES, name), encoding="utf-8") as handle:
                    raw = json.load(handle)
                self.assertIn("scripts/zoho/probe.py", raw["_source"])
                self.assertTrue("zohodesk-oas" in raw["_source"] or "em-biz-backend" in raw["_source"])
                self.assertNotIn("_source", shape(name))

    def test_the_client_reads_only_keys_the_shapes_carry(self):
        ticket = shape("zoho-ticket.json")
        for key in ("id", "ticketNumber", "webUrl", "cf", "departmentId", "contactId"):
            self.assertIn(key, ticket)
        self.assertIn(CF_CHAT_REFERENCE, ticket["cf"])
        for name in ("zoho-contact-search.json", "zoho-contact-tickets.json"):
            self.assertIsInstance(shape(name)["data"], list)
            self.assertIn("id", shape(name)["data"][0])
        self.assertIn("cf", shape("zoho-contact-tickets.json")["data"][0])
        self.assertIn("id", shape("zoho-comment.json"))
        self.assertIn("id", shape("zoho-attachment.json"))
        token = shape("zoho-token.json")
        self.assertIn("access_token", token["refresh"])
        self.assertIn("expires_in", token["refresh"])
        self.assertEqual(token["access_denied"]["error"], "Access Denied")
        for name in ("invalid_code", "invalid_client", "invalid_client_secret"):
            self.assertEqual(token[name]["error"], name)

    def test_every_error_code_the_client_classifies_is_recorded(self):
        errors = shape("zoho-errors.json")
        codes = {body["errorCode"] for body in errors.values()}
        for code in ("INVALID_OAUTH", "SCOPE_MISMATCH", "OAUTH_ORG_MISMATCH", "FORBIDDEN", "LICENSE_ACCESS_LIMITED",
                     "INVALID_DATA", "URL_NOT_FOUND", "RESOURCE_SIZE_EXCEEDED", "TOO_MANY_REQUESTS",
                     "THRESHOLD_EXCEEDED"):
            self.assertIn(code, codes)
        self.assertEqual([item["fieldName"] for item in errors["invalid_data"]["errors"]],
                         ["/contactId", "/departmentId"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_http tests.test_zoho_auth tests.test_zoho_desk
```

Expected: all three modules fail to import with `ModuleNotFoundError`. `test_zoho_http` and `test_zoho_auth` fail on `emotorad_ai.zoho.errors` or `emotorad_ai.zoho.auth`. `test_zoho_desk` fails on `emotorad_ai.zoho.auth`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/zoho/errors.py`:

```python
"""Zoho's answers, as the errors the worker acts on (spec 2026-10-05, section 4).

Each class is a row of the spec's answers table. `error` is short and safe to
log as `error=`. It is Zoho's own error code when Zoho sent one that looks
like a code, and otherwise a name of ours ("timeout", "http_503"). A message
never holds a request or response body, a token, a secret or a phone number:
the worker logs it, and the registry copies `str(exc)` to the model and the
log (registry.py:295-296).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

# Zoho's codes are words joined by underscores (INVALID_OAUTH, and
# invalid_client_secret from the token endpoint). Anything else, including
# free text in a body, is not passed on.
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,59}")


def safe_code(value: object, fallback: str) -> str:
    """`value` when it looks like an error code, else `fallback`."""
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return fallback


class ZohoError(Exception):
    """Base for every Zoho failure. `error` is what the log and /health show."""

    error = "zoho_error"

    def __init__(self, message: str, error: Optional[str] = None) -> None:
        super().__init__(message)
        if error is not None:
            self.error = safe_code(error, type(self).error)


class ZohoUnavailable(ZohoError):
    """Zoho or the network did not answer, and nothing was written. The retry schedule."""

    error = "unavailable"


class ZohoUnknownOutcome(ZohoError):
    """A write was sent and no usable answer came back, so it may have been
    made. The worker looks before it writes again."""

    error = "unknown_outcome"


class ZohoRejected(ZohoError):
    """Zoho refused what we sent (400 or 422). `fields` are the names Zoho gave."""

    error = "INVALID_DATA"

    def __init__(self, message: str, error: Optional[str] = None, fields: Iterable[str] = ()) -> None:
        super().__init__(message, error)
        self.fields: Tuple[str, ...] = tuple(fields)


class ZohoConfigError(ZohoError):
    """Ours to fix: scope, organisation, access, licence or a bad id. Retried hourly."""

    error = "config"


class ZohoAuthExpired(ZohoError):
    """401 INVALID_OAUTH: the access token died. Refresh once and retry the step once."""

    error = "INVALID_OAUTH"


class ZohoGone(ZohoError):
    """404: the ticket or contact was deleted or merged in Desk."""

    error = "not_found"


class ZohoTooLarge(ZohoError):
    """413: an attachment over Zoho's limit."""

    error = "RESOURCE_SIZE_EXCEEDED"


class ZohoBusy(ZohoError):
    """429 TOO_MANY_REQUESTS: too many calls at once. Retry in 30 seconds."""

    error = "TOO_MANY_REQUESTS"


class ZohoCreditsExhausted(ZohoError):
    """429 THRESHOLD_EXCEEDED: the organisation's API credits for the day are gone."""

    error = "THRESHOLD_EXCEEDED"

    def __init__(self, message: str, error: Optional[str] = None,
                 retry_after_seconds: Optional[float] = None) -> None:
        super().__init__(message, error)
        self.retry_after_seconds = retry_after_seconds


class ZohoTokenRefused(ZohoError):
    """The token endpoint refused the refresh token or the client."""

    error = "token_refused"


class ZohoTokenThrottled(ZohoError):
    """The token endpoint said "Access Denied": too many token requests."""

    error = "access_denied"
```

Create `src/emotorad_ai/zoho/http.py`:

```python
"""One HTTP call to Zoho, with Zoho's answer classified (spec 2026-10-05, section 4).

This uses the standard library only, like tools/oms.py and openrouter.py. The
opener is the seam: the suite passes tests/fake_zoho.py, so no test opens a
socket.

Answers are classified by Zoho's error code, not the status alone
(https://desk.zoho.com/DeskAPIDocument#Errors). A write is handled more
carefully than a read. urllib wraps a failure while the request is being sent
in URLError (urllib.request.AbstractHTTPHandler.do_open), so Zoho never had
the whole request, and that is unavailable. Anything raised after that point,
while waiting for or reading the answer, may follow a write Zoho has made. So
a timeout, a dropped connection or a 5xx after a write is an unknown outcome,
and the caller looks before it writes again.

Nothing here logs or retries, or puts a body, a header value or a query
string in an exception. The caller logs `exc.error`.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import (
    ZohoAuthExpired,
    ZohoBusy,
    ZohoConfigError,
    ZohoCreditsExhausted,
    ZohoError,
    ZohoGone,
    ZohoRejected,
    ZohoTooLarge,
    ZohoUnavailable,
    ZohoUnknownOutcome,
    safe_code,
)

DEFAULT_TIMEOUT = 8.0
CREDITS_HEADER = "X-Rate-Limit-Remaining-v3"
# Wrong scope, wrong organisation, no access, no licence: these are ours to
# fix, and no reason to try again sooner.
CONFIG_CODES = frozenset({"SCOPE_MISMATCH", "OAUTH_ORG_MISMATCH", "FORBIDDEN", "LICENSE_ACCESS_LIMITED"})
# A field name Zoho gave, such as "/contactId" or "/cf/cf_chat_reference". With
# the slash gone it must start with a letter, so a number can never pass as one.
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{0,79}")
_LINE_BREAK = re.compile(r"[\r\n\x00]")


def _header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    value = headers.get(name)
    if value is None and isinstance(headers, dict):
        wanted = name.lower()
        value = next((item for key, item in headers.items() if str(key).lower() == wanted), None)
    return value


def _parse(raw: bytes) -> Tuple[bool, Any]:
    """(readable, parsed). An empty body is readable, and None."""
    if not raw or not raw.strip():
        return True, None
    try:
        return True, json.loads(raw.decode("utf-8"))
    except ValueError:
        return False, None


def _fields(payload: Any) -> Tuple[str, ...]:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    names = []
    for item in errors if isinstance(errors, list) else ():
        name = item.get("fieldName") if isinstance(item, dict) else None
        if isinstance(name, str):
            name = name.strip().lstrip("/")
            if _FIELD.fullmatch(name) and name not in names:
                names.append(name)
    return tuple(names)


def _retry_after(headers: Any) -> Optional[float]:
    try:
        seconds = float(str(_header(headers, "Retry-After")).strip())
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _not_sent(reason: Any) -> ZohoError:
    error = "timeout" if isinstance(reason, TimeoutError) else "network"
    return ZohoUnavailable("Zoho could not be reached (%s); nothing was sent" % type(reason).__name__, error=error)


def _no_answer(exc: BaseException, write: bool) -> ZohoError:
    # socket.timeout is TimeoutError since Python 3.10.
    error = "timeout" if isinstance(exc, TimeoutError) else "network"
    name = type(exc).__name__
    if write:
        return ZohoUnknownOutcome("a write to Zoho had no answer (%s); it may have been made" % name, error=error)
    return ZohoUnavailable("Zoho did not answer (%s)" % name, error=error)


def _failure(status: int, payload: Any, headers: Any, write: bool) -> ZohoError:
    code = payload.get("errorCode") if isinstance(payload, dict) else None
    named = safe_code(code, "http_%d" % status)
    said = "Zoho answered %d %s" % (status, named)
    if status == 401 and code == "INVALID_OAUTH":
        return ZohoAuthExpired(said, error=named)
    if status in (401, 403):
        # CONFIG_CODES, and any other refusal of access: retried hourly, and
        # shown on /health as "sending failing: <code>".
        return ZohoConfigError(said, error=named)
    if status in (400, 422):
        fields = _fields(payload)
        text = said + (" naming %s" % ", ".join(fields) if fields else "")
        return ZohoRejected(text, error=named, fields=fields)
    if status == 404:
        return ZohoGone(said, error=named)
    if status == 408:
        # The server gave up before it had the whole request.
        return ZohoUnavailable(said, error=named)
    if status == 413:
        return ZohoTooLarge(said, error=named)
    if status == 429:
        if code == "THRESHOLD_EXCEEDED":
            return ZohoCreditsExhausted(said, error=named, retry_after_seconds=_retry_after(headers))
        return ZohoBusy(said, error=named)
    if status >= 500:
        if write:
            return ZohoUnknownOutcome(said + " to a write; it may have been made", error=named)
        return ZohoUnavailable(said, error=named)
    return ZohoRejected(said, error=named)


class DeskHTTP:
    """Sends one request and classifies the answer. It holds no credential:
    the caller passes the headers, and this never repeats them."""

    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._opener = opener
        self.timeout = timeout
        # Zoho's X-Rate-Limit-Remaining-v3 from the last answer that had it:
        # the organisation's API credits left today, shared with the OMS.
        self.last_credits_remaining: Optional[int] = None

    def __repr__(self) -> str:
        return "DeskHTTP(timeout=%r)" % self.timeout

    def call(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None,
             *, write: bool, timeout: Optional[float] = None, classify: bool = True) -> Tuple[int, Any]:
        """(status, parsed JSON or None), or a ZohoError.

        With `classify=False`, any HTTP answer comes back as it came. This is
        for the token endpoint, whose body says what went wrong whatever the
        status. A 2xx body that is not JSON, and network failures, still raise.
        """
        for value in headers.values():
            if _LINE_BREAK.search(str(value)):
                # http.client would raise a ValueError that quotes the whole
                # header, token and all (the OpenRouter key, 2026-09-29). It is
                # refused here, without the value.
                raise ZohoConfigError("a request header holds a line break; nothing was sent", error="bad_header")
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener(request, timeout=timeout or self.timeout) as response:
                status = int(getattr(response, "status", None) or response.getcode())
                answer_headers = getattr(response, "headers", None)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status, answer_headers = exc.code, exc.headers
            try:
                raw = exc.read() or b""
            except (http.client.HTTPException, OSError):
                raw = b""
        except urllib.error.URLError as exc:
            raise _not_sent(exc.reason) from None
        except (http.client.HTTPException, OSError) as exc:
            raise _no_answer(exc, write) from None
        self._note_credits(answer_headers)
        readable, payload = _parse(raw)
        if status == 204:
            return 204, None
        if 200 <= status < 300:
            if readable:
                return status, payload
            if write:
                raise ZohoUnknownOutcome("Zoho answered %d to a write with a body that is not JSON" % status,
                                         error="unreadable")
            raise ZohoUnavailable("Zoho answered %d with a body that is not JSON" % status, error="unreadable")
        if not classify:
            return status, payload if readable else None
        raise _failure(status, payload if readable else None, answer_headers, write)

    def _note_credits(self, headers: Any) -> None:
        value = _header(headers, CREDITS_HEADER)
        if value is None:
            return
        text = str(value).strip()
        if text.isascii() and text.isdigit():
            self.last_credits_remaining = int(text)
```

Create `src/emotorad_ai/zoho/auth.py`:

```python
"""The Desk access token, kept in memory behind a lock (spec 2026-10-05, section 1).

One token serves the API process, refreshed five minutes before its hour
ends. The token endpoint's answer is read by its body, whatever the HTTP
status:

- "Access Denied" is Zoho's throttle (10 requests in 10 minutes per refresh
  token), sent with HTTP 200. No token request is made for 10 minutes.
- invalid_code, invalid_client or invalid_client_secret is a refusal. The
  last is what rotating the OMS's secret without updating ours gives.
  `state` says "token refused: <name>" for /health, and no token request is
  made for an hour.

The refresh token and the client secret go in the form body, never the
address. They are never logged, stored or put in an exception message, and
nor is the access token.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

from .errors import ZohoTokenRefused, ZohoTokenThrottled, ZohoUnavailable, safe_code
from .http import DeskHTTP
from .settings import ZohoSettings

ACCOUNTS_URL = "https://accounts.zoho.in"
TOKEN_URL = ACCOUNTS_URL + "/oauth/v2/token"
REFRESH_EARLY_SECONDS = 300.0
THROTTLE_WAIT_SECONDS = 600.0
REFUSED_WAIT_SECONDS = 3600.0
DEFAULT_LIFETIME_SECONDS = 3600.0
THROTTLED = "Access Denied"
REFUSALS = frozenset({"invalid_code", "invalid_client", "invalid_client_secret"})


def _lifetime(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LIFETIME_SECONDS
    return seconds if math.isfinite(seconds) and seconds > 0 else DEFAULT_LIFETIME_SECONDS


class TokenSource:
    """The one access token, refreshed on demand, early, and by one caller at a time."""

    def __init__(self, settings: ZohoSettings, http: DeskHTTP, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._renew_at = 0.0
        self._quiet_until = 0.0
        self._refused: Optional[str] = None
        self.state = "ok"

    def __repr__(self) -> str:
        return "TokenSource(state=%r)" % self.state

    def token(self) -> str:
        """A live access token, from memory or refreshed. Callers at the same
        moment share one refresh, because the lock is held across it."""
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if now < self._quiet_until:
                if self._refused:
                    raise ZohoTokenRefused(
                        "Zoho refused the token request (%s); not asked again within the hour" % self._refused,
                        error=self._refused,
                    )
                raise ZohoTokenThrottled("Zoho is throttling token requests; not asked again within ten minutes")
            return self._refresh(now)

    def invalidate(self) -> None:
        """Drop the cached token. A Desk 401 INVALID_OAUTH means it died early:
        Zoho keeps at most ten per refresh token, and the person's scripts
        share ours."""
        with self._lock:
            self._token = None
            self._renew_at = 0.0

    def _refresh(self, now: float) -> str:
        form = urllib.parse.urlencode({
            "refresh_token": self._settings.refresh_token,
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            "grant_type": "refresh_token",
        }).encode("ascii")
        status, payload = self._http.call(
            "POST", TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            form, write=False, classify=False,
        )
        body = payload if isinstance(payload, dict) else {}
        error = body.get("error")
        if error == THROTTLED:
            self._token = None
            self._refused = None
            self._quiet_until = now + THROTTLE_WAIT_SECONDS
            self.state = "throttled"
            raise ZohoTokenThrottled("Zoho is throttling token requests (HTTP %d); none for ten minutes" % status)
        if isinstance(error, str) and error in REFUSALS:
            self._token = None
            self._refused = error
            self._quiet_until = now + REFUSED_WAIT_SECONDS
            self.state = "token refused: %s" % error
            raise ZohoTokenRefused("Zoho refused the token request: %s (HTTP %d)" % (error, status), error=error)
        token = body.get("access_token")
        if error is not None or not isinstance(token, str) or not token.strip():
            raise ZohoUnavailable(
                "Zoho's token answer held no token (HTTP %d, %s)" % (status, safe_code(error, "no error named")),
                error="token_unreadable",
            )
        lifetime = _lifetime(body.get("expires_in"))
        self._token = token.strip()
        self._renew_at = now + max(lifetime - REFRESH_EARLY_SECONDS, lifetime / 2)
        self._quiet_until = 0.0
        self._refused = None
        self.state = "ok"
        return self._token
```

Create `src/emotorad_ai/zoho/desk.py`:

```python
"""The Zoho Desk calls the worker makes, and nothing else (spec 2026-10-05,
sections 4 and 5).

These go to the India data centre only. Every call carries the access token
and the organisation id. An expired token (401 INVALID_OAUTH) is refreshed
once and the same call made once more. A 401 means nothing was done, so
that is safe even for a write.

Any id that goes into a path is checked first to be a Zoho id (digits), so
nothing read from a record or a setting can change which URL is called.
"""

from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .auth import TokenSource
from .errors import ZohoAuthExpired, ZohoConfigError, ZohoRejected, ZohoUnavailable, ZohoUnknownOutcome
from .http import DeskHTTP
from .settings import ZohoSettings

DESK_URL = "https://desk.zoho.in"
UPLOAD_TIMEOUT = 60.0
# Zoho's limit on a comment (https://desk.zoho.com/DeskAPIDocument#TicketsComments).
# Transcript chunks stay under 30,000 (zoho/payload.py).
COMMENT_LIMIT = 32000
# Zoho's limit on an attachment's name (v1.0/TicketAttachment.json).
NAME_LIMIT = 100
PAGE = 100
MAX_PAGES = 20
SEARCH_FIELDS = ("phone", "mobile")

_ID = re.compile(r"[0-9]{1,30}")
_TEN_DIGITS = re.compile(r"[0-9]{10}")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MIME = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+")


def _path_id(value: Any) -> str:
    text = "" if value is None else str(value)
    if not _ID.fullmatch(text):
        raise ZohoConfigError("an id for a Zoho call is not a Zoho id; nothing was sent", error="bad_id")
    return text


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _items(status: int, payload: Any) -> List[Dict[str, Any]]:
    """A list answer's `data`. A 204, or no body, is an empty list."""
    if status == 204 or payload is None:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ZohoUnavailable("Zoho's list answer had no data list", error="bad_shape")
    return [item for item in data if isinstance(item, dict)]


def _made_id(payload: Any, what: str) -> str:
    made = payload.get("id") if isinstance(payload, dict) else None
    if made is None or str(made) == "":
        # The write went through, and we cannot tell what it made. A resume
        # looks before it writes again.
        raise ZohoUnknownOutcome("Zoho answered a %s create with no id" % what, error="no_id")
    return str(made)


def safe_filename(name: str) -> str:
    """A file name that cannot break the multipart header and fits Zoho's limit."""
    cleaned = _UNSAFE_NAME.sub("_", name or "").strip(".") or "attachment"
    if len(cleaned) <= NAME_LIMIT:
        return cleaned
    stem, dot, extension = cleaned.rpartition(".")
    if dot and stem and 0 < len(extension) <= 10:
        return stem[: NAME_LIMIT - len(extension) - 1] + "." + extension
    return cleaned[:NAME_LIMIT]


def find_adoptable(tickets: List[Dict[str, Any]], cf_api_name: str, chat_reference: str) -> Optional[Dict[str, Any]]:
    """The ticket an earlier attempt made, if it is in this list.

    This needs exact equality on the chat-reference field, nothing looser.
    Chat references are unique per record and deployment, and a near match
    would put this customer's transcript on someone else's ticket. Zoho
    returns custom fields under "cf".
    """
    if not chat_reference:
        return None
    for ticket in tickets:
        cf = ticket.get("cf") if isinstance(ticket, dict) else None
        if isinstance(cf, dict) and cf.get(cf_api_name) == chat_reference:
            return ticket
    return None


class DeskClient:
    def __init__(self, settings: ZohoSettings, tokens: TokenSource, http: DeskHTTP) -> None:
        self._settings = settings
        self._tokens = tokens
        self._http = http

    def __repr__(self) -> str:
        return "DeskClient(org_id=%r)" % self._settings.org_id

    def _call(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None,
              payload: Optional[Dict[str, Any]] = None, body: Optional[bytes] = None,
              content_type: Optional[str] = None, write: bool, timeout: Optional[float] = None) -> Tuple[int, Any]:
        url = DESK_URL + path
        if query:
            # "*" stays as typed: it is the search wildcard.
            url += "?" + urllib.parse.urlencode(query, safe="*")
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        for attempt in (1, 2):
            headers = {
                "Authorization": "Zoho-oauthtoken " + self._tokens.token(),
                "orgId": self._settings.org_id,
                "Accept": "application/json",
            }
            if content_type:
                headers["Content-Type"] = content_type
            try:
                return self._http.call(method, url, headers, body, write=write, timeout=timeout)
            except ZohoAuthExpired:
                if attempt == 2:
                    raise
                # The token died early: Zoho keeps at most ten per refresh
                # token, and the person's scripts share ours. A 401 means
                # nothing was done, so the same step is sent once more.
                self._tokens.invalidate()
        raise AssertionError("unreachable")

    def _paged(self, path: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for page in range(MAX_PAGES):
            status, payload = self._call("GET", path, query={"from": page * PAGE, "limit": PAGE}, write=False)
            batch = _items(status, payload)
            items.extend(batch)
            if len(batch) < PAGE:
                break
        return items

    def search_contacts(self, field: str, last_ten: str) -> List[Dict[str, Any]]:
        """Contacts whose phone (or mobile) ends with these ten digits."""
        if field not in SEARCH_FIELDS:
            raise ValueError("contacts are searched by phone or mobile only")
        if not isinstance(last_ten, str) or not _TEN_DIGITS.fullmatch(last_ten):
            # Never the number itself in the message.
            raise ValueError("a contact search needs the last ten digits of a number")
        status, payload = self._call("GET", "/api/v1/contacts/search", query={field: "*" + last_ten}, write=False)
        return _items(status, payload)

    def create_contact(self, last_name: str, mobile: str) -> str:
        """A new contact with a last name and a mobile, and no email."""
        _, payload = self._call("POST", "/api/v1/contacts", payload={"lastName": last_name, "mobile": mobile},
                                write=True)
        return _made_id(payload, "contact")

    def contact_tickets(self, contact_id: str, department_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """The contact's tickets in one department, newest first. This is used
        instead of Zoho's search, which can lag by a few minutes."""
        path = "/api/v1/contacts/%s/tickets" % _path_id(contact_id)
        query = {"departmentId": _path_id(department_id), "sortBy": "-createdTime", "limit": int(limit)}
        status, payload = self._call("GET", path, query=query, write=False)
        return _items(status, payload)

    def create_ticket(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """{"id", "ticketNumber", "webUrl"} of the ticket made, as text."""
        _, made = self._call("POST", "/api/v1/tickets", payload=payload, write=True)
        ticket_id = _made_id(made, "ticket")
        return {"id": ticket_id, "ticketNumber": _text(made.get("ticketNumber")), "webUrl": _text(made.get("webUrl"))}

    def add_comment(self, ticket_id: str, content: str) -> str:
        """A private, plain-text comment. Its id comes back."""
        if len(content) > COMMENT_LIMIT:
            raise ZohoRejected("a comment over Zoho's 32,000 characters was not sent", error="too_long",
                               fields=("content",))
        path = "/api/v1/tickets/%s/comments" % _path_id(ticket_id)
        _, payload = self._call("POST", path, payload={"isPublic": False, "contentType": "plainText",
                                                       "content": content}, write=True)
        return _made_id(payload, "comment")

    def comments(self, ticket_id: str) -> List[Dict[str, Any]]:
        """Every comment on a ticket, so a resume can find the markers already posted."""
        return self._paged("/api/v1/tickets/%s/comments" % _path_id(ticket_id))

    def upload_attachment(self, ticket_id: str, filename: str, data: bytes, mime: str) -> str:
        """One file, as the multipart field "file", private. Its id comes back."""
        path = "/api/v1/tickets/%s/attachments" % _path_id(ticket_id)
        boundary = "EMotoradAI" + secrets.token_hex(16)
        while boundary.encode("ascii") in data:
            boundary = "EMotoradAI" + secrets.token_hex(16)
        kind = mime if isinstance(mime, str) and _MIME.fullmatch(mime) else "application/octet-stream"
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                % (boundary, safe_filename(filename), kind)).encode("ascii")
        body = head + data + ("\r\n--%s--\r\n" % boundary).encode("ascii")
        _, payload = self._call("POST", path, query={"isPublic": "false"}, body=body,
                                content_type="multipart/form-data; boundary=" + boundary,
                                write=True, timeout=UPLOAD_TIMEOUT)
        return _made_id(payload, "attachment")

    def attachments(self, ticket_id: str) -> List[Dict[str, Any]]:
        """Every attachment on a ticket, so a resume can see what is already there."""
        return self._paged("/api/v1/tickets/%s/attachments" % _path_id(ticket_id))
```

- [ ] **Step 4: Run the module, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_http tests.test_zoho_auth tests.test_zoho_desk -v
```

Expected: `Ran 71 tests` (27 for HTTP, 11 for the token, 33 for Desk), `OK`.

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: the count is 71 higher than after Task 6, and the only failure is the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test changes. `tests/fake_zoho.py` is not a `test_*` module, so discovery does not run it.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/zoho/errors.py src/emotorad_ai/zoho/http.py src/emotorad_ai/zoho/auth.py src/emotorad_ai/zoho/desk.py tests/fake_zoho.py tests/test_zoho_http.py tests/test_zoho_auth.py tests/test_zoho_desk.py docs/api-shapes/zoho-ticket.json docs/api-shapes/zoho-contact-search.json docs/api-shapes/zoho-contact-tickets.json docs/api-shapes/zoho-comment.json docs/api-shapes/zoho-attachment.json docs/api-shapes/zoho-token.json docs/api-shapes/zoho-errors.json
git commit -m "feat: Zoho Desk client: HTTP, errors, token and calls, with a fake and documented shapes" -m "Answers are classified by Zoho's error code. A write that may have reached Zoho is an unknown outcome, never retried blind. The token is refreshed early under a lock, backs off ten minutes on Access Denied and an hour on a refusal, and an expired token is refreshed once per call. The shapes come from Zoho's published OAS and the OMS's code until the probe replaces them. No body, token or phone reaches a message." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 8: The payload: subject, description, fields, transcript chunks

**Files:**
- Create: `src/emotorad_ai/zoho/payload.py`
- Test: `tests/test_zoho_payload.py`

**Interfaces:**
- Consumes:
  - `tickets.kinds.subject_label(kind, category)` and `tickets.kinds.is_urgent(kind, category)` (Task 2).
  - `tickets.record.new_record(...)` (Task 2, tests only).
  - `conversation.TranscriptTurn` and `conversation.render_transcript`.
  - `zoho.settings.ZohoSettings` (Task 6).
  - `zoho.errors.ZohoConfigError` (Task 7).
  - `tests.fake_zoho.zoho_settings` and its constants (Task 7).
- Produces:
  - `subject(record) -> str`, `description(record) -> str`, `ticket_payload(record, settings, contact_id) -> Dict` and `transcript_chunks(reference, chat_reference, turns, limit=30000) -> List[Tuple[str, List[int]]]`.
  - Constants: `TRANSCRIPT_LIMIT`, `DESCRIPTION_LIMIT`, `SUBJECT_LIMIT`, `CF_LIMIT`, `SOURCE`, `STATUS`, `MODEL_RAISED`, `TAKEOVER_LINE`, `UTC_LINE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zoho_payload.py`:

```python
"""What goes on a Zoho Desk ticket (spec 2026-10-05, section 5).

Code chooses every field Zoho routes on. Model text reaches only the
description, under a heading that says who wrote it. Fake data only: the test
number and a fixture frame number.
"""

import re
import unittest

from emotorad_ai.conversation import TranscriptTurn, render_transcript
from emotorad_ai.tickets.kinds import subject_label
from emotorad_ai.tickets.record import new_record
from emotorad_ai.zoho.errors import ZohoConfigError
from emotorad_ai.zoho.payload import (
    DESCRIPTION_LIMIT,
    TAKEOVER_LINE,
    TRANSCRIPT_LIMIT,
    description,
    subject,
    ticket_payload,
    transcript_chunks,
)
from tests.fake_zoho import CF_CHAT_REFERENCE, CF_SOURCE, LIVE_DEPARTMENT, TEST_CONTACT, TEST_DEPARTMENT, zoho_settings

PHONE = "+919999999999"
FRAME = "EMXP2025004417"
MODEL = "EMX Plus"
ALLOWED = {"subject", "departmentId", "contactId", "phone", "priority", "status", "channel", "cf", "description"}
FORBIDDEN = ("assigneeId", "teamId", "dueDate", "customFields", "productId", "email", "contact", "classification",
             "category", "subCategory", "uploads", "language")
MARKER = re.compile(r"\[stage:EM-1000001 transcript, turns (\d+)-(\d+)(?:, part (\d+))?\]")
UTC = "Chat with the AI chatbot for EM-1000001. Times are in UTC."


def record(**changes):
    fields = dict(
        reference="EM-1000001", chat_reference="stage:EM-1000001",
        source_key="c-1:2026-10-05T08:00:00.000000+00:00:create_support_ticket:k-1", mode="test", kind="support",
        conversation_id="c-1", started_at="2026-10-05T08:00:00.000000+00:00", cluster_id="cl-1", channel="whatsapp",
        phone=PHONE, identity="verified", category="battery_charging", ai_severity="high",
        summary="Battery stops charging at 40 percent. Tried another socket, same result.", claims={},
        bike={"model": MODEL, "frame_number": FRAME, "frame_number_source": None}, coverage="computed",
        customer_name="Test Rider", created_at="2026-10-05T08:05:00.000000+00:00",
    )
    fields.update(changes)
    return new_record(**fields)


def turns(count, start=1, text="My battery stops charging at 40 percent"):
    return [TranscriptTurn(n=n, role="customer" if n % 2 else "bot", text="%s, turn %d" % (text, n),
                           at="2026-10-05T08:%02d:00+00:00" % (n % 60)) for n in range(start, start + count)]


def body_of(text, opening):
    lines = text.split("\n")
    return "\n".join(lines[2:] if opening else lines[1:])


class SubjectTests(unittest.TestCase):
    def test_a_verified_ticket_reads_ai_chat_label_and_bike(self):
        self.assertEqual(subject(record()), "[AI chat] %s - EMX Plus" % subject_label("support", "battery_charging"))

    def test_unverified_comes_first(self):
        self.assertEqual(subject(record(identity="unverified", kind="intake", category=None, bike=None)),
                         "[Unverified] [AI chat] %s - bike not given" % subject_label("intake", None))

    def test_the_label_is_set_by_kind_in_code(self):
        self.assertEqual(subject(record(kind="safety", category="battery_safety")), "[AI chat] SAFETY - EMX Plus")
        for kind, label in (("handover", "Asked for a person"), ("lockout", "Could not verify"),
                            ("intake", "Unverified customer"), ("warranty_proof", "Late warranty registration")):
            with self.subTest(kind=kind):
                self.assertIn(label, subject(record(kind=kind, category=None)))

    def test_no_phone_and_no_name(self):
        text = subject(record(identity="unverified"))
        self.assertNotIn("9999999999", text)
        self.assertNotIn("Test Rider", text)

    def test_at_most_255_characters_on_one_line(self):
        text = subject(record(bike={"model": "EMX\nPlus " + "x" * 400, "frame_number": FRAME,
                                    "frame_number_source": None}))
        self.assertEqual(len(text), 255)
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("[AI chat] "))


class DescriptionTests(unittest.TestCase):
    def test_the_lines_come_in_the_spec_order(self):
        text = description(record(kind="warranty_proof", category=None,
                                  claims={"claimed_purchase_date": "2025-01-10", "purchase_channel": "dealer"}))
        order = ["Reference: EM-1000001", "Source: AI chatbot", "Channel: WhatsApp", "Identity: verified",
                 "Kind: warranty_proof", "Bike: EMX Plus, frame number EMXP2025004417",
                 "Warranty, from our systems, not the AI:", "AI's view of severity: high",
                 "Customer's claims, not checked:",
                 "Summary written by the AI from the customer's words, not checked:"]
        positions = [text.index(line) for line in order]
        self.assertEqual(positions, sorted(positions))

    def test_an_unverified_number_says_so(self):
        self.assertIn("Identity: number given in chat, not verified", description(record(identity="unverified")))
        self.assertNotIn("not verified", description(record()))

    def test_the_bike_line_carries_model_frame_and_source_and_is_left_out_when_unknown(self):
        text = description(record(bike={"model": MODEL, "frame_number": FRAME,
                                        "frame_number_source": "read by the rider"}))
        self.assertIn("Bike: EMX Plus, frame number EMXP2025004417 (read by the rider)", text)
        self.assertNotIn("Bike:", description(record(bike=None)))

    def test_the_coverage_outcome_comes_from_code(self):
        self.assertIn("(computed)", description(record()))
        self.assertIn("no warranty record for this number (no_warranty_record)",
                      description(record(coverage="no_warranty_record")))
        self.assertIn("no result recorded in this chat", description(record(coverage=None)))

    def test_ai_labels_only_for_model_raised_kinds(self):
        for kind in ("support", "intake", "warranty_proof"):
            with self.subTest(kind=kind):
                text = description(record(kind=kind))
                self.assertIn("AI's view of severity: high", text)
                self.assertIn("Summary written by the AI from the customer's words, not checked", text)
        for kind in ("safety", "handover", "lockout"):
            with self.subTest(kind=kind):
                text = description(record(kind=kind, ai_severity="critical"))
                self.assertNotIn("AI's view", text)
                self.assertNotIn("written by the AI", text)
                self.assertIn("by code", text)

    def test_a_safety_ticket_carries_what_the_safety_check_found(self):
        detail = ("Automatic safety escalation. Customer reported: battery is swelling and smoking. "
                  "Matched safety indicators: swelling, smoke. Seen in the customer's photo or video: "
                  "the pack is bulging.")
        text = description(record(kind="safety", category="battery_safety", summary=detail))
        self.assertIn("Safety report", text)
        self.assertIn(detail, text)

    def test_a_handover_with_no_summary_says_the_customer_asked_for_a_person(self):
        self.assertIn("Customer asked for a person.", description(record(kind="handover", category=None, summary="")))

    def test_a_lockout_warns_of_a_takeover_and_nothing_else_does(self):
        self.assertEqual(TAKEOVER_LINE, "Possible takeover attempt: verify only through the number on record, "
                                        "never a number from this chat.")
        locked = description(record(kind="lockout", identity="unverified", bike=None, category=None, summary=""))
        self.assertTrue(locked.endswith(TAKEOVER_LINE))
        for kind in ("support", "safety", "handover", "intake", "warranty_proof"):
            with self.subTest(kind=kind):
                self.assertNotIn("takeover", description(record(kind=kind)))

    def test_claims_are_labelled_as_the_customers(self):
        text = description(record(kind="intake", identity="unverified", category=None, bike=None, claims={
            "stated_name": "Test Rider", "stated_contact": "rider at example dot com", "evidence": "invoice INV-1",
            "claimed_purchase_date": "2025-01-10", "purchase_channel": "website"}))
        self.assertIn("Customer's claims, not checked:", text)
        for line in ("- Name they gave: Test Rider", "- Contact they gave: rider at example dot com",
                     "- Evidence they offered: invoice INV-1", "- Purchase date they gave: 2025-01-10",
                     "- Where they say they bought it: website"):
            self.assertIn(line, text)
        self.assertNotIn("Customer's claims", description(record()))

    def test_a_long_summary_is_cut_to_fit_and_the_takeover_line_kept(self):
        text = description(record(summary="x" * 70000))
        self.assertLessEqual(len(text), DESCRIPTION_LIMIT)
        self.assertIn("[cut to fit Zoho's limit]", text)
        locked = description(record(kind="lockout", summary="y" * 70000))
        self.assertLessEqual(len(locked), DESCRIPTION_LIMIT)
        self.assertTrue(locked.endswith(TAKEOVER_LINE))

    def test_the_customer_name_stays_off_the_description(self):
        self.assertNotIn("Test Rider", description(record()))


class TicketPayloadTests(unittest.TestCase):
    def test_exactly_the_fields_in_the_spec(self):
        payload = ticket_payload(record(), zoho_settings(), TEST_CONTACT)
        self.assertEqual(set(payload), ALLOWED)
        self.assertEqual(payload["subject"], subject(record()))
        self.assertEqual(payload["description"], description(record()))
        self.assertEqual(payload["departmentId"], TEST_DEPARTMENT)
        self.assertEqual(payload["contactId"], TEST_CONTACT)
        self.assertEqual(payload["phone"], PHONE)
        self.assertEqual(payload["status"], "Open")
        self.assertEqual(payload["channel"], "Chat")
        self.assertEqual(payload["priority"], "Medium")

    def test_never_an_assignee_team_due_date_old_custom_fields_or_a_dealer_field(self):
        for kind in ("support", "safety", "handover", "lockout", "intake", "warranty_proof"):
            with self.subTest(kind=kind):
                payload = ticket_payload(record(kind=kind), zoho_settings(live=True), TEST_CONTACT)
                for key in FORBIDDEN:
                    self.assertNotIn(key, payload)
                keys = list(payload) + list(payload["cf"])
                self.assertEqual([key for key in keys if "dealer" in key.lower()], [])
                self.assertNotIn("Dealer Principle", payload["description"])

    def test_the_department_follows_the_records_mode(self):
        live = zoho_settings(live=True)
        self.assertEqual(ticket_payload(record(mode="test"), live, TEST_CONTACT)["departmentId"], TEST_DEPARTMENT)
        self.assertEqual(ticket_payload(record(mode="live"), live, TEST_CONTACT)["departmentId"], LIVE_DEPARTMENT)

    def test_a_live_record_under_test_settings_is_refused(self):
        with self.assertRaises(ZohoConfigError):
            ticket_payload(record(mode="live"), zoho_settings(), TEST_CONTACT)
        # Even with the real department named, test settings never send to it.
        with self.assertRaises(ZohoConfigError):
            ticket_payload(record(mode="live"), zoho_settings(EMOTORAD_ZOHO_DEPARTMENT_ID=LIVE_DEPARTMENT),
                           TEST_CONTACT)

    def test_priority_is_high_only_when_urgent_and_its_values_come_from_the_settings(self):
        settings = zoho_settings(EMOTORAD_ZOHO_PRIORITY_HIGH="P1", EMOTORAD_ZOHO_PRIORITY_MEDIUM="P3")
        self.assertEqual(ticket_payload(record(kind="safety", category="battery_safety"), settings,
                                        TEST_CONTACT)["priority"], "P1")
        self.assertEqual(ticket_payload(record(kind="safety", category=None), settings, TEST_CONTACT)["priority"], "P1")
        self.assertEqual(ticket_payload(record(kind="support", category="battery_safety"), settings,
                                        TEST_CONTACT)["priority"], "P1")
        self.assertEqual(ticket_payload(record(), settings, TEST_CONTACT)["priority"], "P3")
        self.assertEqual(ticket_payload(record(kind="handover", category=None), zoho_settings(),
                                        TEST_CONTACT)["priority"], "Medium")

    def test_model_text_chooses_nothing_zoho_routes_on(self):
        sneaky = 'departmentId 1 priority "High" status Closed contactId 2 assigneeId 3'
        payload = ticket_payload(record(summary=sneaky, ai_severity="critical"), zoho_settings(), TEST_CONTACT)
        self.assertEqual((payload["departmentId"], payload["priority"], payload["status"], payload["contactId"]),
                         (TEST_DEPARTMENT, "Medium", "Open", TEST_CONTACT))
        self.assertNotIn("assigneeId", payload)

    def test_the_channel_is_the_system_channel_from_the_settings(self):
        payload = ticket_payload(record(channel="website_chat"), zoho_settings(EMOTORAD_ZOHO_CHANNEL="Web"),
                                 TEST_CONTACT)
        self.assertEqual(payload["channel"], "Web")
        self.assertIn("Channel: website chat", payload["description"])

    def test_cf_carries_the_chat_reference_and_the_source_each_at_most_255(self):
        payload = ticket_payload(record(), zoho_settings(), TEST_CONTACT)
        self.assertEqual(payload["cf"], {CF_CHAT_REFERENCE: "stage:EM-1000001", CF_SOURCE: "AI chatbot"})
        renamed = zoho_settings(EMOTORAD_ZOHO_CF_CHAT_REFERENCE="cf_ai_chat_ref", EMOTORAD_ZOHO_CF_SOURCE="cf_ai_source")
        self.assertEqual(set(ticket_payload(record(), renamed, TEST_CONTACT)["cf"]), {"cf_ai_chat_ref", "cf_ai_source"})
        long = ticket_payload(record(chat_reference="stage:" + "E" * 400), zoho_settings(), TEST_CONTACT)
        self.assertTrue(all(len(value) <= 255 for value in long["cf"].values()))

    def test_the_phone_goes_in_plus_91_form_and_only_an_indian_mobile(self):
        self.assertEqual(ticket_payload(record(phone=PHONE), zoho_settings(), TEST_CONTACT)["phone"], PHONE)
        for phone in (None, "", "+34600000000", "12345"):
            with self.subTest(phone=phone):
                self.assertNotIn("phone", ticket_payload(record(phone=phone), zoho_settings(), TEST_CONTACT))


class TranscriptTests(unittest.TestCase):
    def test_no_turns_no_comments(self):
        self.assertEqual(transcript_chunks("EM-1000001", "stage:EM-1000001", []), [])

    def test_a_short_run_is_one_comment_with_its_marker_and_the_utc_line(self):
        run = turns(4)
        [(text, numbers)] = transcript_chunks("EM-1000001", "stage:EM-1000001", run)
        lines = text.split("\n")
        self.assertEqual(lines[0], "[stage:EM-1000001 transcript, turns 1-4]")
        self.assertEqual(lines[1], UTC)
        self.assertEqual(body_of(text, True), render_transcript(run))
        self.assertEqual(numbers, [1, 2, 3, 4])

    def test_a_long_run_splits_on_turn_boundaries_under_the_limit(self):
        run = turns(40)
        chunks = transcript_chunks("EM-1000001", "stage:EM-1000001", run, limit=400)
        self.assertGreater(len(chunks), 3)
        seen = []
        for index, (text, numbers) in enumerate(chunks):
            self.assertLessEqual(len(text), 400)
            first, last, part = MARKER.fullmatch(text.split("\n")[0]).groups()
            self.assertIsNone(part)
            self.assertEqual((int(first), int(last)), (numbers[0], numbers[-1]))
            self.assertEqual(numbers, list(range(numbers[0], numbers[-1] + 1)))
            self.assertEqual(body_of(text, index == 0), render_transcript([t for t in run if t.n in numbers]))
            self.assertEqual(UTC in text, index == 0)
            seen += numbers
        self.assertEqual(seen, list(range(1, 41)))

    def test_the_default_limit_is_thirty_thousand(self):
        self.assertEqual(TRANSCRIPT_LIMIT, 30000)
        chunks = transcript_chunks("EM-1000001", "stage:EM-1000001", turns(200, text="x" * 400))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(text) <= 30000 for text, _ in chunks))
        self.assertEqual([n for _, numbers in chunks for n in numbers], list(range(1, 201)))

    def test_a_turn_too_long_for_one_comment_goes_in_parts_and_only_the_last_part_counts_it(self):
        long_turn = TranscriptTurn(n=3, role="customer", text="z" * 1000, at="2026-10-05T08:03:00+00:00")
        run = turns(2) + [long_turn] + turns(1, start=4)
        chunks = transcript_chunks("EM-1000001", "stage:EM-1000001", run, limit=300)
        self.assertTrue(all(len(text) <= 300 for text, _ in chunks))
        self.assertEqual([n for _, numbers in chunks for n in numbers], [1, 2, 3, 4])
        parts = [(text, numbers) for text, numbers in chunks if MARKER.match(text).group(3)]
        self.assertGreater(len(parts), 2)
        self.assertEqual([numbers for _, numbers in parts], [[]] * (len(parts) - 1) + [[3]])
        self.assertEqual("".join(text.split("\n", 1)[1] for text, _ in parts), render_transcript([long_turn]))

    def test_turns_are_put_in_order_and_the_result_is_stable(self):
        run = turns(10)
        shuffled = run[5:] + run[:5]
        self.assertEqual(transcript_chunks("EM-1000001", "stage:EM-1000001", shuffled, limit=300),
                         transcript_chunks("EM-1000001", "stage:EM-1000001", run, limit=300))

    def test_devanagari_is_counted_in_characters_and_kept_whole(self):
        run = turns(30, text="मेरी बैटरी चार्ज नहीं हो रही है")
        chunks = transcript_chunks("EM-1000001", "stage:EM-1000001", run, limit=500)
        self.assertTrue(all(len(text) <= 500 for text, _ in chunks))
        joined = "\n".join(body_of(text, index == 0) for index, (text, _) in enumerate(chunks))
        self.assertEqual(joined, render_transcript(run))

    def test_a_later_posting_is_marked_from_its_first_new_turn(self):
        [(text, numbers)] = transcript_chunks("EM-1000001", "stage:EM-1000001", turns(6, start=7))
        self.assertTrue(text.startswith("[stage:EM-1000001 transcript, turns 7-12]\n"))
        self.assertEqual(numbers, [7, 8, 9, 10, 11, 12])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_payload
```

Expected: `ImportError: Failed to import test module: test_zoho_payload`, caused by `ModuleNotFoundError: No module named 'emotorad_ai.zoho.payload'`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/zoho/payload.py`:

```python
"""What goes on a Zoho Desk ticket (spec 2026-10-05, section 5).

These are pure functions over a ticket record (tickets/record.py) and the
settings: no network, no store. Code chooses every field Zoho routes on: the
department, priority, status, channel and contact. Model text reaches only
the description, under a heading that says the AI wrote it and nobody
checked it.

Never set here: the assignee, the team, a due date (the OMS sends IST time
marked as UTC), the deprecated customFields, or any of the OMS's dealer
fields. "Dealer Principle Name" matters most, because the OMS webhook uses it
to copy Zoho tickets into the OMS (em-biz-backend zoho/views.py).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..conversation import TranscriptTurn, render_transcript
from ..tickets.kinds import is_urgent, subject_label
from .errors import ZohoConfigError
from .settings import ZohoSettings

SUBJECT_LIMIT = 255
CF_LIMIT = 255
# Zoho allows 65,535 (v1.0/Ticket.json). We keep a margin, as for comments.
DESCRIPTION_LIMIT = 60000
# Zoho allows 32,000 a comment (https://desk.zoho.com/DeskAPIDocument#TicketsComments).
TRANSCRIPT_LIMIT = 30000
CLAIM_LIMIT = 500
SOURCE = "AI chatbot"
STATUS = "Open"
NO_BIKE = "bike not given"

# The kinds whose summary the model wrote (spec section 3's table). The others
# are built by code, and their description says what built them.
MODEL_RAISED = ("support", "intake", "warranty_proof")
AI_HEADING = "Summary written by the AI from the customer's words, not checked:"
CODE_HEADINGS = {
    "safety": ("Safety report, recorded by code from the customer's words, the safety terms matched and what "
               "the photo or video check saw:"),
    "handover": "Recorded by code when the customer asked for a person:",
    "lockout": "Recorded by code when the customer could not verify their number:",
}
CODE_HEADING = "Recorded by code:"
EMPTY_SUMMARIES = {
    "handover": "Customer asked for a person.",
    "lockout": "The customer could not verify their number in this chat.",
}
TAKEOVER_LINE = ("Possible takeover attempt: verify only through the number on record, "
                 "never a number from this chat.")
VERIFIED = "Identity: verified"
UNVERIFIED = "Identity: number given in chat, not verified"
UTC_LINE = "Chat with the AI chatbot for %s. Times are in UTC."
CUT = "\n[cut to fit Zoho's limit]"
COVERAGE_PREFIX = "Warranty, from our systems, not the AI:"

CHANNELS = {"whatsapp": "WhatsApp", "amiigo_app": "Amiigo app", "website_chat": "website chat", "voice": "phone call"}
# What each coverage outcome means to a person reading the ticket. The code
# follows in brackets, so the line can be searched.
COVERAGE_TEXT = {
    "computed": "worked out from the purchase date on record",
    "computed_from_registration": "worked out from the registration date, as no purchase date is on record",
    "purchase_date_missing": "registered, with no purchase or registration date on record",
    "not_registered": "in the EMotorad app, not registered for warranty",
    "warranty_unknown": "which warranty record is this b
ike's cannot be told",
    "warranty_unavailable": "the warranty system did not answer",
    "no_warranty_record": "no warranty record for this number",
    "oms_unavailable": "the warranty system did not answer",
}
# The customer's claims, in the order a person reads them. Nothing else in
# `claims` reaches Zoho.
CLAIM_LABELS = (
    ("stated_name", "Name they gave"),
    ("stated_contact", "Contact they gave"),
    ("evidence", "Evidence they offered"),
    ("claimed_purchase_date", "Purchase date they gave"),
    ("purchase_channel", "Where they say they bought it"),
)

_NOT_DIGITS = re.compile(r"[^0-9]")
_INDIAN_MOBILE = re.compile(r"[6-9][0-9]{9}")


def _one_line(value: Any) -> str:
    """A value as one line: a newline in quoted text must not break the
    subject or a labelled line."""
    return " ".join(str(value).split()) if value is not None else ""


def _bike_model(bike: Any) -> str:
    if not isinstance(bike, dict):
        return ""
    # tickets/seam.py maps the tools' bike_model onto the record's bike.
    return _one_line(bike.get("model") or bike.get("bike_model"))


def _channel(channel: Optional[str]) -> str:
    if not channel:
        return "not recorded"
    return CHANNELS.get(channel, _one_line(channel))


def _kind_line(kind: str, category: Optional[str]) -> str:
    line = "Kind: %s" % kind
    if category:
        line += ". Category: %s" % _one_line(category)
    return line


def _bike_line(bike: Any) -> Optional[str]:
    if not isinstance(bike, dict):
        return None
    model = _bike_model(bike)
    frame = _one_line(bike.get("frame_number"))
    if not (model or frame):
        return None
    line = "Bike: %s" % (model or "model not known")
    if frame:
        source = _one_line(bike.get("frame_number_source"))
        line += ", frame number %s" % frame + (" (%s)" % source if source else "")
    return line


def _coverage_line(coverage: Optional[str]) -> str:
    if not coverage:
        return "%s no result recorded in this chat" % COVERAGE_PREFIX
    code = _one_line(coverage)
    if code in COVERAGE_TEXT:
        return "%s %s (%s)" % (COVERAGE_PREFIX, COVERAGE_TEXT[code], code)
    return "%s %s" % (COVERAGE_PREFIX, code)


def _claim_lines(claims: Any) -> List[str]:
    if not isinstance(claims, dict):
        return []
    lines = []
    for key, label in CLAIM_LABELS:
        value = _one_line(claims.get(key))
        if value:
            lines.append("- %s: %s" % (label, value[:CLAIM_LIMIT]))
    return lines


def _plus91(phone: Optional[str]) -> Optional[str]:
    """The number in +91 form, or None for anything but an Indian mobile."""
    digits = _NOT_DIGITS.sub("", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return "+91" + digits if _INDIAN_MOBILE.fullmatch(digits) else None


def subject(record: Dict[str, Any]) -> str:
    """`[AI chat] <label> - <bike model>`, with "[Unverified]" first when the
    number was not verified. No phone, no name, at most 255 characters."""
    label = _one_line(subject_label(record["kind"], record.get("category")))
    text = "[AI chat] %s - %s" % (label, _bike_model(record.get("bike")) or NO_BIKE)
    if record.get("identity") != "verified":
        text = "[Unverified] " + text
    return text[:SUBJECT_LIMIT]


def description(record: Dict[str, Any]) -> str:
    """The ticket's description, as plain text in the spec's order (section 5)."""
    kind = record["kind"]
    by_model = kind in MODEL_RAISED
    lines = [
        "Reference: %s" % record["_id"],
        "Source: %s" % SOURCE,
        "Channel: %s" % _channel(record.get("channel")),
        VERIFIED if record.get("identity") == "verified" else UNVERIFIED,
        _kind_line(kind, record.get("category")),
    ]
    bike = _bike_line(record.get("bike"))
    if bike:
        lines.append(bike)
    lines.append(_coverage_line(record.get("coverage")))
    if by_model and record.get("ai_severity"):
        # Only the model's own tickets: a safety ticket's severity is set by
        # code, and calling it the AI's view would be untrue.
        lines.append("AI's view of severity: %s" % _one_line(record["ai_severity"]))
    claims = _claim_lines(record.get("claims"))
    if claims:
        lines += ["", "Customer's claims, not checked:"] + claims
    heading = AI_HEADING if by_model else CODE_HEADINGS.get(kind, CODE_HEADING)
    before = "\n".join(lines + ["", heading, ""])
    after = "\n\n" + TAKEOVER_LINE if kind == "lockout" else ""
    summary = (record.get("summary") or "").strip() or EMPTY_SUMMARIES.get(kind, "(none given)")
    # Only the summary is ever cut, so the lines above it and the takeover
    # warning below it always arrive.
    room = DESCRIPTION_LIMIT - len(before) - len(after)
    if len(summary) > room:
        summary = summary[: max(room - len(CUT), 0)] + CUT
    return before + summary + after


def ticket_payload(record: Dict[str, Any], settings: ZohoSettings, contact_id: str) -> Dict[str, Any]:
    """The body of POST /api/v1/tickets. Only the fields in section 5."""
    if record.get("mode") == "live":
        # A live record goes to the real department only under live settings.
        # Test settings that happen to name the real department never send there.
        if not (settings.live and settings.department_id):
            raise ZohoConfigError("a live record cannot be sent under test settings", error="mode_mismatch")
        department = settings.department_id
    else:
        department = settings.test_department_id
    urgent = bool(record.get("urgent")) or is_urgent(record["kind"], record.get("category"))
    payload: Dict[str, Any] = {
        "subject": subject(record),
        "departmentId": department,
        "contactId": contact_id,
        "priority": settings.priority_high if urgent else settings.priority_medium,
        "status": STATUS,
        # A system channel from the settings, never an integration channel,
        # which would carry its own reply route. Our channel is in the description.
        "channel": settings.channel,
        "cf": {
            settings.cf_chat_reference: str(record["chat_reference"])[:CF_LIMIT],
            settings.cf_source: SOURCE[:CF_LIMIT],
        },
        "description": description(record),
    }
    phone = _plus91(record.get("phone"))
    if phone:
        payload["phone"] = phone
    return payload


def _marker(chat_reference: str, reference: str, first: int, last: int, opening: bool,
            part: Optional[int] = None) -> str:
    text = "[%s transcript, turns %d-%d%s]" % (chat_reference, first, last, ", part %d" % part if part else "")
    return text + "\n" + UTC_LINE % reference if opening else text


def _chunk(chat_reference: str, reference: str, group: List[TranscriptTurn], opening: bool) -> Tuple[str, List[int]]:
    head = _marker(chat_reference, reference, group[0].n, group[-1].n, opening)
    return head + "\n" + render_transcript(group), [turn.n for turn in group]


def _parts(chat_reference: str, reference: str, turn: TranscriptTurn, opening: bool,
           limit: int) -> List[Tuple[str, List[int]]]:
    """One turn too long for a comment, cut into parts. Only the last part
    lists the turn, so a resume after an early part posts the rest."""
    line = render_transcript([turn])
    parts: List[Tuple[str, List[int]]] = []
    part = 1
    while line:
        head = _marker(chat_reference, reference, turn.n, turn.n, opening and part == 1, part)
        room = limit - len(head) - 1
        if room < 1:
            raise ValueError("a transcript comment limit of %d leaves no room after its marker" % limit)
        piece, line = line[:room], line[room:]
        parts.append((head + "\n" + piece, [] if line else [turn.n]))
        part += 1
    return parts


def transcript_chunks(reference: str, chat_reference: str, turns: Sequence[TranscriptTurn],
                      limit: int = TRANSCRIPT_LIMIT) -> List[Tuple[str, List[int]]]:
    """The turns as private comments, each at most `limit` characters.

    They are split on turn boundaries. Each comment starts with a marker such
    as "[stage:EM-1000001 transcript, turns 7-12]", so a resume that lists the
    ticket's comments can tell what is already there. The first comment says
    the times are in UTC, as render_transcript prints them. Each result is
    (text, turn numbers in it); the worker adds those to posted_turns once the
    comment is posted. The same turns always give the same comments.
    """
    ordered = sorted(turns, key=lambda turn: turn.n)
    chunks: List[Tuple[str, List[int]]] = []
    group: List[TranscriptTurn] = []
    size = 0  # the group's rendered lines, with the newlines between them
    for turn in ordered:
        line = len(render_transcript([turn]))
        if group:
            grown = size + 1 + line
            if len(_marker(chat_reference, reference, group[0].n, turn.n, not chunks)) + 1 + grown <= limit:
                group.append(turn)
                size = grown
                continue
            chunks.append(_chunk(chat_reference, reference, group, not chunks))
            group, size = [], 0
        if len(_marker(chat_reference, reference, turn.n, turn.n, not chunks)) + 1 + line <= limit:
            group, size = [turn], line
            continue
        chunks.extend(_parts(chat_reference, reference, turn, not chunks, limit))
    if group:
        chunks.append(_chunk(chat_reference, reference, group, not chunks))
    return chunks
```

- [ ] **Step 4: Run the module, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_payload -v
```

Expected: `Ran 33 tests`, `OK`.

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: the count is 33 higher than after Task 7, and the only failure is the known `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test changes.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/zoho/payload.py tests/test_zoho_payload.py
git commit -m "feat: what goes on a Zoho ticket: subject, description, fields and transcript chunks" -m "Code sets the department from the record's mode, the priority from urgency, the status, the channel and both custom fields. The model's text appears only in the description, under a heading saying the AI wrote it. No assignee, team, due date, customFields or dealer field is ever sent. Transcripts are split on turn boundaries into private comments of at most 30,000 characters, each with a marker so a resume can see what is already posted." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Interface issues for the plan author

1. **`DeskHTTP.call` has an extra keyword-only argument, `classify: bool = True`.** The token endpoint answers a refusal with HTTP 400 or 401 and a body that names it (`invalid_client_secret`). The spec says to read that body whatever the status. With the skeleton's signature, the 400 would become `ZohoRejected` and the name would be lost. Callers that leave the argument out behave exactly as the skeleton says.
2. **Two outcome strings are added to the skeleton's list.** Both start with `misconfigured: `, so Task 10's fallback rule needs no change.
   - `load_zoho_settings` returns `(None, "misconfigured: bad number: <NAMES>")` when `EMOTORAD_ZOHO_CREDITS_FLOOR` or `EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB` is not a usable whole number. Quietly using the default would hide a typo.
   - `startup_problem` returns `"misconfigured: tickets index not readable (<ErrorClass>)"` when `has_unique_source_key()` raises. This neither swallows the exception nor claims the index is missing.
3. **The keys inside the record's `bike` are not fixed by the skeleton.** The payload reads `model` (falling back to `bike_model`), `frame_number` and `frame_number_source`. Task 3's `DeskTicketSystem` should write `{"model", "frame_number", "frame_number_source"}`.
4. **`TokenSource` waits on its own after a failed token request.** It holds off for an hour after a refusal (`REFUSED_WAIT_SECONDS`) and ten minutes after "Access Denied", raising straight away in between without a request. Task 9 can treat `ZohoTokenRefused` and `ZohoTokenThrottled` as ordinary retryable failures with no hourly gate of its own. `TokenSource.state` is the `token refused: <name>` value for /health.
5. **"Before a write was sent" relies on how urllib behaves.** urllib wraps failures inside `h.request()` (connect, TLS, send) in `URLError`, and lets errors from `getresponse()` or `read()` through bare. This was checked against Python 3.12's `AbstractHTTPHandler.do_open`. So `URLError` means `ZohoUnavailable` even for a write. A bare `TimeoutError`, `ConnectionResetError` or `http.client.HTTPException` on a write means `ZohoUnknownOutcome`. If the transport ever stops being urllib, revisit this.
6. **Zoho's OAS does not say whether the contact's ticket list carries `cf`.** `find_adoptable` depends on it, and the shape's `_source` says it is open. Task 12's `test_ticket.py` should assert it. If it is missing, `contact_tickets` may need an extra query parameter.
7. **Every 404 raises `ZohoGone`.** `DeskClient` cannot tell a ticket from a contact. Task 9 decides by step: for a contact, clear the id and search again; for a ticket, mark the record `gone`.
8. **Two details of `transcript_chunks` matter to Task 9.**
   - `reference` is used only in the first chunk's UTC line.
   - A single turn longer than the limit is cut into chunks whose marker ends `, part k`, an extension of the skeleton's marker. Only the last part lists the turn number.
   - Chunk boundaries depend on which turns are still unposted. So after an unknown outcome, Task 9 should compare the turn ranges in the ticket's markers, not only whole marker strings.
9. **The global test command does not blank every Zoho name.** It leaves out the five probe-only names: `EMOTORAD_ZOHO_PRIORITY_HIGH`, `_PRIORITY_MEDIUM`, `_CHANNEL`, `_CREDITS_FLOOR` and `_ATTACHMENT_LIMIT_MB`, while spec section 9 says every name. This does not affect Tasks 6 to 8, whose tests pass mappings rather than `os.environ`. Task 10's health test and `chat_local` WITHHELD should use `zoho.settings.ENV_NAMES`.
10. **Some of the spec's limits are tighter than Zoho's OAS, and one is new.**
    - The OAS allows a 1,000-character subject. The payload uses the spec's 255.
    - The OAS allows a 65,535-character description. The payload caps it at 60,000 and cuts only the summary.
    - The OAS caps an attachment name at 100 characters, which the spec did not mention. `DeskClient` trims file names to 100.
11. **The `cf` names in the shapes are placeholders.** `cf_chat_reference` and `cf_source` match the test settings; part 1 supplies the real API names. The token shape's error bodies come from the spec's section 1, because the token endpoint is not in Zoho Desk's OAS. Its success keys come from the OMS's `get_new_token`.
12. **The description is sent as plain text with newlines, as the spec asks.** Whether Desk shows the line breaks, or treats the field as HTML, should be checked when `test_ticket.py` reads the ticket back.
13. **`ticket_payload` refuses a mismatched record.** A `live` record under test settings raises `ZohoConfigError(error="mode_mismatch")`. This stays true even when the test settings name the real department. Task 9 already holds mismatched records, so this is a backstop.

---

<!-- drafted as tasks-9-10 -->

### Task 9: The Zoho worker

**Files:**
- Create: `src/emotorad_ai/zoho/worker.py`
- Test: `tests/test_zoho_worker.py` (new)

**Interfaces:**
- Consumes:
  - `tickets.clock.now_iso() -> str`, `tickets.clock.plus(at: str, seconds: float) -> str`
  - `tickets.record.new_record(...)` (tests only), `tickets.store.InMemoryTicketStore` (tests only)
  - The `TicketStore` protocol: `take_due(now, mode, lease_seconds, token)`, `renew_lease(reference, token, until)`, `save(reference, token, changes, add_to_set=None, push=None, expect_wake=None) -> bool`, `overdue(now, mode)`, `contact_for(phone)`, and in tests `next_reference()`, `insert(record)`, `get(reference)`, `wake(reference, now)`, `add_note(reference, text, now)`, `close_runs(conversation_id, new_started_at)`, `counts(mode, now)`
  - `zoho.settings.ZohoSettings` (fields as in the shared interfaces; `.mode`)
  - `zoho.errors`: `ZohoError` and its subclasses, which carry `.error`, plus `ZohoRejected.fields` and `ZohoCreditsExhausted.retry_after_seconds`
  - `DeskClient` methods: `search_contacts`, `create_contact`, `contact_tickets`, `create_ticket`, `add_comment`, `comments`, `upload_attachment`, `attachments`, and the attribute `client.http.last_credits_remaining`
  - `zoho.desk.find_adoptable(tickets, cf_api_name, chat_reference)`
  - `zoho.payload.ticket_payload(record, settings, contact_id)`, `zoho.payload.transcript_chunks(reference, chat_reference, turns, limit=30000)`
  - `conversations.transcript(conversation_id) -> List[TranscriptTurn]`, `conversations.media_of(conversation_id) -> List[Dict]`
  - `media_reader.get_bytes(key) -> bytes` (`S3Store`), raising `storage.s3.StorageError`
- Produces:
  - `zoho.worker.ZohoWorker(store, client, conversations, media_reader, settings, log, clock=now_iso)` with `run_once() -> bool`, `check_overdue() -> None`, `start()`, `stop()`, `wake()`, `status: Dict[str, Any]` = `{"running": bool, "last_pass_at": Optional[str], "failing": Optional[str]}`, `settings` (attribute), `pass_seconds` (attribute, default 30.0)
  - `zoho.worker.THREAD_NAME = "zoho-worker"`, `zoho.worker.retry_wait(attempts: int) -> int`, `LEASE_SECONDS = 300.0`, `PASS_SECONDS = 30.0`
  - Events: `zoho_ticket_sent` (reference, zoho_number, attempts, credits_remaining), `zoho_retry` (reference, error, attempts, wait_seconds), `zoho_rejected` (reference, error, fields), `zoho_token_refused` (level, reference, error), `zoho_worker_error` (level, error), `zoho_ticket_stuck` and `safety_ticket_late` (level, reference, age_seconds), plus `zoho_ticket_gone`, `zoho_ticket_adopted` and `zoho_record_dropped` (reference)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zoho_worker.py`:

```python
"""The Zoho worker (spec 2026-10-05 section 4), offline.

Zoho here is a double with DeskClient's methods over dicts, so a test can make
any call fail before its write lands or after it (a timeout after Zoho made
the ticket). The ticket store and the conversation store are the in-memory
ones, on one clock the test moves by hand. No socket is opened, and only the
two loop tests at the end start a thread.

The 401 INVALID_OAUTH refresh and retry belongs to DeskClient
(tests/test_zoho_desk.py). The worker sees ZohoAuthExpired only when the
retry failed too.
"""

import copy
import itertools
import json
import threading
import time
import unittest
from types import SimpleNamespace

from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.media_records import media_record
from emotorad_ai.observability import EventLog
from emotorad_ai.tickets.clock import plus
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.zoho import errors
from emotorad_ai.zoho.settings import ZohoSettings
from emotorad_ai.zoho.worker import THREAD_NAME, ZohoWorker, retry_wait
from tests.store_contract import inbound, reply

START = "2026-10-05T10:00:00.000000+00:00"
PHONE = "+919999999999"
TEST_DEPARTMENT, REAL_DEPARTMENT = "dept-test", "dept-real"
TEST_CONTACT, UNVERIFIED_CONTACT = "contact-test", "contact-unverified"
CF_REFERENCE, CF_SOURCE = "cf_chat_reference", "cf_source"
MB = 1024 * 1024


def settings(live=False, **changes):
    values = dict(
        client_id="client-test", client_secret="secret-test", refresh_token="refresh-test", org_id="org-test",
        test_department_id=TEST_DEPARTMENT, test_contact_id=TEST_CONTACT, department_id=REAL_DEPARTMENT,
        unverified_contact_id=UNVERIFIED_CONTACT, live=live, environment="stage",
        cf_chat_reference=CF_REFERENCE, cf_source=CF_SOURCE, priority_high="High", priority_medium="Medium",
        channel="Chat", credits_floor=1000, attachment_limit_bytes=20 * MB,
    )
    values.update(changes)
    return ZohoSettings(**values)


def zoho_error(cls, error, **attributes):
    """One of the exceptions in errors.py, with `.error` and any named
    attribute set. Built without calling its constructor, so this file works
    whatever argument names errors.py uses. The worker reads only these
    attributes."""
    exc = cls.__new__(cls)
    Exception.__init__(exc, error)
    exc.error = error
    for name, value in attributes.items():
        setattr(exc, name, value)
    return exc


class Clock:
    """The time, moved by hand. Shared by the worker and the conversation store."""

    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = plus(self.now, seconds)
        return self.now


class FakeDesk:
    """DeskClient's methods over dicts.

    `fail(name, exc)` makes the next call of that method raise before it does
    anything. With `after=True` the write lands first and the error comes back
    afterwards, which is what a timeout after Zoho made the ticket looks like.
    `hooks[name]` runs at the start of the next call of that method. `down`
    makes every call fail as unreachable."""

    def __init__(self):
        self.http = SimpleNamespace(last_credits_remaining=None)
        # The two contacts from person step 3. They have no numbers here, so a
        # search never finds them by accident.
        self.contacts = {
            TEST_CONTACT: {"id": TEST_CONTACT, "lastName": "AI chatbot test"},
            UNVERIFIED_CONTACT: {"id": UNVERIFIED_CONTACT, "lastName": "Unverified AI chat"},
        }
        self.tickets = {}
        self.comment_log = {}
        self.files = {}
        self.calls = []
        self.hooks = {}
        self.down = False
        self._errors = {}
        self._ids = itertools.count(1)

    def fail(self, name, exc, after=False):
        self._errors.setdefault(name, []).append((exc, after))

    def _enter(self, name, *args):
        self.calls.append((name,) + args)
        hook = self.hooks.pop(name, None)
        if hook is not None:
            hook()
        if self.down:
            raise zoho_error(errors.ZohoUnavailable, "network")
        queued = self._errors.get(name)
        if queued and not queued[0][1]:
            raise queued.pop(0)[0]

    def _leave(self, name, result):
        queued = self._errors.get(name)
        if queued and queued[0][1]:
            raise queued.pop(0)[0]
        return result

    def _known_ticket(self, ticket_id):
        if ticket_id not in self.tickets:
            raise zoho_error(errors.ZohoGone, "RESOURCE_NOT_FOUND")

    def search_contacts(self, field, last_ten):
        self._enter("search_contacts", field, last_ten)
        found = [dict(c) for c in self.contacts.values() if (c.get(field) or "").endswith(last_ten)]
        return self._leave("search_contacts", found)

    def create_contact(self, last_name, mobile):
        self._enter("create_contact", last_name, mobile)
        contact_id = "contact-%d" % next(self._ids)
        self.contacts[contact_id] = {"id": contact_id, "lastName": last_name, "mobile": mobile}
        return self._leave("create_contact", contact_id)

    def contact_tickets(self, contact_id, department_id, limit=50):
        self._enter("contact_tickets", contact_id, department_id)
        if contact_id not in self.contacts:
            raise zoho_error(errors.ZohoGone, "RESOURCE_NOT_FOUND")
        newest_first = [copy.deepcopy(t) for t in reversed(list(self.tickets.values()))
                        if t["contactId"] == contact_id and t["departmentId"] == department_id]
        return self._leave("contact_tickets", newest_first[:limit])

    def create_ticket(self, payload):
        self._enter("create_ticket", payload)
        if payload.get("contactId") not in self.contacts:
            raise zoho_error(errors.ZohoGone, "RESOURCE_NOT_FOUND")
        ticket_id = "ticket-%d" % next(self._ids)
        number = str(1000 + len(self.tickets))
        url = "https://desk.zoho.in/agent/tickets/" + ticket_id
        self.tickets[ticket_id] = dict(copy.deepcopy(payload), id=ticket_id, ticketNumber=number, webUrl=url)
        return self._leave("create_ticket", {"id": ticket_id, "ticketNumber": number, "webUrl": url})

    def add_comment(self, ticket_id, content):
        self._enter("add_comment", ticket_id, content)
        self._known_ticket(ticket_id)
        comment_id = "comment-%d" % next(self._ids)
        self.comment_log.setdefault(ticket_id, []).append(
            {"id": comment_id, "content": content, "isPublic": False, "contentType": "plainText"})
        return self._leave("add_comment", comment_id)

    def comments(self, ticket_id):
        self._enter("comments", ticket_id)
        self._known_ticket(ticket_id)
        return self._leave("comments", copy.deepcopy(self.comment_log.get(ticket_id, [])))

    def upload_attachment(self, ticket_id, filename, data, mime):
        self._enter("upload_attachment", ticket_id, filename, len(data), mime)
        self._known_ticket(ticket_id)
        attachment_id = "file-%d" % next(self._ids)
        self.files.setdefault(ticket_id, []).append({"id": attachment_id, "name": filename, "size": len(data)})
        return self._leave("upload_attachment", attachment_id)

    def attachments(self, ticket_id):
        self._enter("attachments", ticket_id)
        self._known_ticket(ticket_id)
        return self._leave("attachments", copy.deepcopy(self.files.get(ticket_id, [])))


class Bucket:
    """S3Store.get_bytes over a dict, recording each read."""

    def __init__(self):
        self.objects, self.reads = {}, []

    def get_bytes(self, key):
        self.reads.append(key)
        return self.objects[key]


class RefusesIntents:
    """The ticket store, except that saving an intent matches nothing. This is
    what happens when the record is erased or another worker takes it in
    between."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def save(self, reference, token, changes, **kwargs):
        if changes.get("intent"):
            return False
        return self.inner.save(reference, token, changes, **kwargs)


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tickets = InMemoryTicketStore()
        self.conversations = InMemoryConversationStore(clock=self.clock)
        self.desk = FakeDesk()
        self.bucket = Bucket()
        self.log = EventLog(path=None)
        self.worker = self.make_worker()

    def make_worker(self, live=False, **changes):
        return ZohoWorker(self.tickets, self.desk, self.conversations, self.bucket,
                          settings(live=live, **changes), self.log, clock=self.clock)

    def chat(self, turns=(("my battery won't charge", "Try another socket."),), cid="conv-1"):
        """Turns said now, in the current run of `cid`. Returns the run's start."""
        state = self.conversations.get(cid)
        for customer, bot in turns:
            state.turns += 1
            self.conversations.record_turn(state, inbound(customer, cid=cid), reply(bot, cid=cid))
        return state.started_at

    def restart(self, customer, bot, cid="conv-1"):
        """A second person proves a number on the same conversation id (verify
        first's restart_for). This is a new run, and its turns are numbered on
        from the last run's."""
        state = self.conversations.get(cid)
        state.turns += 1
        state.restart_for("PHONE#" + PHONE, self.clock())
        self.conversations.record_turn(state, inbound(customer, cid=cid), reply(bot, cid=cid))
        return state.started_at

    def record(self, started_at, kind="support", category="battery_charging", mode="test", identity="verified",
               customer_name=None, cluster_id="cluster-1", cid="conv-1"):
        reference = self.tickets.next_reference()
        return self.tickets.insert(new_record(
            reference=reference, chat_reference="stage:" + reference,
            source_key="%s:%s:create_support_ticket:%s" % (cid, started_at, reference),
            mode=mode, kind=kind, conversation_id=cid, started_at=started_at, cluster_id=cluster_id,
            channel="web", phone=PHONE, identity=identity, category=category, ai_severity="normal",
            summary="Charger LED stays off; tried another socket.", claims={}, bike=None,
            coverage="computed", customer_name=customer_name, created_at=self.clock(),
        ))["_id"]

    def photo(self, upload_id, cluster_id, stored_at, size=200_000, kind="image", cid="conv-1"):
        folder, ext, mime = ("images", "jpg", "image/jpeg") if kind == "image" else ("videos", "mp4", "video/mp4")
        key = "customers/%s/%s/%s/%s.%s" % (cluster_id, cid, folder, upload_id, ext)
        self.bucket.objects[key] = b"fake media bytes"
        self.conversations.record_media(media_record(
            bucket="media-test", key=key, kind=kind, mime_type=mime, size_bytes=size,
            conversation_id=cid, cluster_id=cluster_id, source="inline", stored_at=stored_at))
        return key

    def wake(self, reference):
        self.tickets.wake(reference, self.clock())

    def send(self, reference):
        """The turn ended: wake the record and run the worker once."""
        self.wake(reference)
        return self.worker.run_once()

    def saved(self, reference):
        return self.tickets.get(reference)

    def ticket_of(self, reference):
        return self.saved(reference)["zoho"].get("ticket_id")

    def comments(self, reference):
        return self.desk.comment_log.get(self.ticket_of(reference), [])

    def text_on(self, reference):
        return "\n".join(c["content"] for c in self.comments(reference))

    def files(self, reference):
        return [f["name"] for f in self.desk.files.get(self.ticket_of(reference), [])]

    def events(self, name):
        return [e for e in self.log.events if e["event"] == name]

    def calls(self, name):
        return [c for c in self.desk.calls if c[0] == name]


class WhenTests(WorkerCase):
    def test_nothing_is_sent_before_the_turn_ends(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(10)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.desk.calls, [])
        self.wake(ref)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_turn_that_died_is_sent_two_minutes_after_the_record(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(119)
        self.assertFalse(self.worker.run_once())
        self.clock.advance(2)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_record_of_the_other_mode_is_held_and_never_sent(self):
        ref = self.record(started_at=self.chat(), mode="live")
        self.wake(ref)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.desk.calls, [])
        self.assertEqual(self.tickets.counts("test", self.clock())["held"], 1)


class LeaseTests(WorkerCase):
    def test_a_record_under_another_workers_lease_is_not_taken(self):
        ref = self.record(started_at=self.chat())
        self.wake(ref)
        self.assertIsNotNone(self.tickets.take_due(self.clock(), "test", 300, "another-worker"))
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.desk.calls, [])
        self.clock.advance(301)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_save_that_matches_nothing_drops_the_record_and_sends_nothing(self):
        ref = self.record(started_at=self.chat())
        self.worker.store = RefusesIntents(self.tickets)
        self.assertTrue(self.send(ref))
        self.assertEqual(self.calls("create_ticket"), [])
        self.assertEqual([e["reference"] for e in self.events("zoho_record_dropped")], [ref])
        self.assertNotEqual(self.saved(ref)["state"], "sent")
        # Once the lease runs out, a worker whose saves land sends it.
        self.worker.store = self.tickets
        self.clock.advance(301)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_turn_that_ends_while_the_worker_sends_is_not_lost(self):
        ref = self.record(started_at=self.chat())

        def another_turn():
            self.chat(turns=[("the light is red now", "Thanks, noted.")])
            self.wake(ref)

        self.desk.hooks["add_comment"] = another_turn
        self.assertTrue(self.send(ref))
        self.assertEqual(self.saved(ref)["state"], "waiting")
        self.assertEqual(self.saved(ref)["next_attempt_at"], self.clock())
        self.assertTrue(self.worker.run_once())
        saved = self.saved(ref)
        self.assertEqual(saved["state"], "sent")
        self.assertIn("the light is red now", self.text_on(ref))
        self.assertEqual(sorted(saved["posted_turns"]), [1, 2, 3, 4])

    def test_below_the_credits_floor_only_urgent_records_are_sent(self):
        started = self.chat()
        normal = self.record(started_at=started)
        urgent = self.record(started_at=started, kind="safety", category="battery_safety")
        self.desk.http.last_credits_remaining = 999
        self.wake(normal)
        self.wake(urgent)
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(urgent)["state"], "sent")
        self.assertEqual(self.saved(normal)["state"], "waiting")
        self.assertEqual(self.saved(normal)["next_attempt_at"], plus(self.clock(), 600))
        self.assertEqual(len(self.desk.tickets), 1)
        self.desk.http.last_credits_remaining = 5000
        self.clock.advance(600)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(normal)["state"], "sent")


class FieldTests(WorkerCase):
    def test_a_ticket_carries_what_code_sets_and_nothing_forbidden(self):
        started = self.chat()
        normal = self.record(started_at=started)
        urgent = self.record(started_at=started, kind="safety", category="battery_safety")
        self.wake(normal)
        self.wake(urgent)
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        by_reference = {t["cf"][CF_REFERENCE]: t for t in self.desk.tickets.values()}
        ticket = by_reference["stage:" + normal]
        self.assertEqual(ticket["departmentId"], TEST_DEPARTMENT)
        self.assertEqual(ticket["contactId"], TEST_CONTACT)
        self.assertEqual(ticket["phone"], PHONE)
        self.assertEqual(ticket["priority"], "Medium")
        self.assertEqual(by_reference["stage:" + urgent]["priority"], "High")
        self.assertEqual(ticket["status"], "Open")
        self.assertEqual(ticket["channel"], "Chat")
        self.assertEqual(ticket["cf"][CF_SOURCE], "AI chatbot")
        self.assertTrue(ticket["subject"].startswith("[AI chat]"))
        self.assertLessEqual(len(ticket["subject"]), 255)
        self.assertIn(normal, ticket["description"])
        for forbidden in ("assigneeId", "teamId", "dueDate", "customFields"):
            self.assertNotIn(forbidden, ticket)
        self.assertNotIn("Dealer Principle", json.dumps(ticket))

    def test_a_test_record_always_goes_on_the_test_contact_with_no_search(self):
        started = self.chat()
        for identity in ("verified", "unverified"):
            ref = self.record(started_at=started, identity=identity)
            self.send(ref)
            self.assertEqual(self.saved(ref)["zoho"]["contact_id"], TEST_CONTACT)
        self.assertEqual(self.calls("search_contacts") + self.calls("create_contact"), [])

    def test_a_sent_ticket_is_logged_with_its_number_and_the_credits_left(self):
        self.desk.http.last_credits_remaining = 4321
        ref = self.record(started_at=self.chat())
        self.send(ref)
        saved = self.saved(ref)
        self.assertEqual((saved["state"], saved["attempts"], saved["intent"], saved["lease_token"]),
                         ("sent", 0, None, None))
        self.assertEqual(saved["zoho"]["ticket_number"], "1000")
        self.assertTrue(saved["zoho"]["web_url"])
        [sent] = self.events("zoho_ticket_sent")
        self.assertEqual((sent["reference"], sent["zoho_number"], sent["attempts"], sent["credits_remaining"]),
                         (ref, "1000", 1, 4321))


class ContactTests(WorkerCase):
    """Live mode, step 1: whose contact the ticket goes on."""

    def setUp(self):
        super().setUp()
        self.worker = self.make_worker(live=True)
        self.started = self.chat()

    def live(self, **fields):
        return self.record(started_at=self.started, mode="live", **fields)

    def contact_of(self, ref):
        return self.saved(ref)["zoho"]["contact_id"]

    def test_an_unverified_number_goes_on_the_unverified_contact(self):
        ref = self.live(identity="unverified")
        self.send(ref)
        self.assertEqual(self.contact_of(ref), UNVERIFIED_CONTACT)
        self.assertEqual(self.calls("search_contacts") + self.calls("create_contact"), [])

    def test_one_match_by_phone_is_used(self):
        self.desk.contacts["contact-asha"] = {"id": "contact-asha", "lastName": "Asha", "phone": PHONE}
        ref = self.live()
        self.send(ref)
        self.assertEqual(self.contact_of(ref), "contact-asha")
        self.assertEqual([c[1:] for c in self.calls("search_contacts")], [("phone", "9999999999")])
        self.assertEqual(self.calls("create_contact"), [])

    def test_of_several_matches_the_one_named_as_on_record_is_used(self):
        self.desk.contacts["contact-a"] = {"id": "contact-a", "lastName": "Someone Else", "mobile": PHONE}
        self.desk.contacts["contact-b"] = {"id": "contact-b", "lastName": "Asha Test", "mobile": PHONE}
        ref = self.live(customer_name="Asha Test")
        self.send(ref)
        self.assertEqual(self.contact_of(ref), "contact-b")
        self.assertEqual([c[1] for c in self.calls("search_contacts")], ["phone", "mobile"])
        self.assertEqual(self.calls("create_contact"), [])

    def test_of_several_matches_with_none_named_a_new_contact_is_made(self):
        self.desk.contacts["contact-a"] = {"id": "contact-a", "lastName": "Someone", "mobile": PHONE}
        self.desk.contacts["contact-b"] = {"id": "contact-b", "lastName": "Someone Else", "mobile": PHONE}
        ref = self.live()
        self.send(ref)
        [call] = self.calls("create_contact")
        self.assertEqual(call[1:], ("AI chat customer", PHONE))
        self.assertNotIn(self.contact_of(ref), ("contact-a", "contact-b"))

    def test_with_no_match_a_contact_is_made_with_the_name_on_record(self):
        ref = self.live(customer_name="Asha Test")
        self.send(ref)
        [call] = self.calls("create_contact")
        self.assertEqual(call[1:], ("Asha Test", PHONE))
        made = self.desk.contacts[self.contact_of(ref)]
        self.assertEqual(made["mobile"], PHONE)
        self.assertNotIn("email", made)

    def test_the_contact_found_once_is_reused_for_the_same_number(self):
        first = self.live()
        self.send(first)
        searches = len(self.calls("search_contacts"))
        second = self.live()
        self.send(second)
        self.assertEqual(self.contact_of(second), self.contact_of(first))
        self.assertEqual(len(self.calls("search_contacts")), searches)
        self.assertEqual(len(self.calls("create_contact")), 1)

    def test_a_contact_from_a_test_or_unverified_record_is_never_reused(self):
        tester = self.make_worker(live=False)
        on_test = self.record(started_at=self.started, mode="test")
        self.wake(on_test)
        tester.run_once()
        self.send(self.live(identity="unverified"))
        verified = self.live()
        self.send(verified)
        self.assertNotIn(self.contact_of(verified), (TEST_CONTACT, UNVERIFIED_CONTACT))
        self.assertEqual(len(self.calls("create_contact")), 1)

    def test_a_stored_contact_deleted_in_desk_is_forgotten_and_found_again(self):
        first = self.live()
        self.send(first)
        deleted = self.contact_of(first)
        del self.desk.contacts[deleted]
        second = self.live()
        self.send(second)
        self.assertNotEqual(self.contact_of(second), deleted)
        self.assertEqual(self.saved(second)["state"], "sent")
        self.assertEqual(self.desk.tickets[self.ticket_of(second)]["contactId"], self.contact_of(second))

    def test_a_contact_create_that_was_never_answered_searches_again_first(self):
        ref = self.live()
        self.desk.fail("create_contact", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.send(ref)
        made = [c for c in self.desk.contacts if c not in (TEST_CONTACT, UNVERIFIED_CONTACT)]
        self.desk.contacts[made[0]]["phone"] = PHONE  # Zoho's search now finds it
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(self.contact_of(ref), made[0])
        self.assertEqual(len(self.calls("create_contact")), 1)
        self.assertIsNone(self.saved(ref)["intent"])


class TicketTests(WorkerCase):
    """Step 2. A timeout after the ticket was made must never become two."""

    def test_a_timeout_after_the_ticket_was_made_finds_it_and_makes_no_second(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.assertTrue(self.send(ref))
        saved = self.saved(ref)
        self.assertEqual((saved["state"], saved["intent"]["step"]), ("waiting", "ticket"))
        self.assertNotIn("ticket_id", saved["zoho"])
        self.clock.advance(30)
        self.assertTrue(self.worker.run_once())
        [ticket_id] = self.desk.tickets
        self.assertEqual(self.ticket_of(ref), ticket_id)
        self.assertEqual(self.saved(ref)["state"], "sent")
        self.assertEqual(len(self.calls("create_ticket")), 1)
        self.assertEqual(len(self.calls("contact_tickets")), 1)
        self.assertEqual([e["reference"] for e in self.events("zoho_ticket_adopted")], [ref])

    def test_a_timeout_before_it_was_made_looks_then_makes_it_once(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        self.send(ref)
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.desk.tickets), 1)
        self.assertEqual([c[0] for c in self.desk.calls if c[0] in ("contact_tickets", "create_ticket")],
                         ["create_ticket", "contact_tickets", "create_ticket"])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_crash_after_the_intent_was_saved_looks_before_making_it(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", RuntimeError("process died"))
        with self.assertRaises(RuntimeError):
            self.send(ref)
        self.assertEqual(self.saved(ref)["intent"]["step"], "ticket")
        self.clock.advance(301)  # the dead pass's lease runs out
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.desk.tickets), 1)
        self.assertEqual(len(self.calls("contact_tickets")), 1)

    def test_a_crash_after_the_ticket_was_made_adopts_it(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", RuntimeError("process died"), after=True)
        with self.assertRaises(RuntimeError):
            self.send(ref)
        self.clock.advance(301)
        self.worker.run_once()
        self.assertEqual(len(self.desk.tickets), 1)
        self.assertEqual(self.ticket_of(ref), next(iter(self.desk.tickets)))

    def test_another_records_ticket_on_the_same_contact_is_never_adopted(self):
        started = self.chat()
        first = self.record(started_at=started)
        self.send(first)
        second = self.record(started_at=started)
        self.desk.fail("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        self.send(second)
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.desk.tickets), 2)
        self.assertNotEqual(self.ticket_of(second), self.ticket_of(first))


class TranscriptTests(WorkerCase):
    def test_the_transcript_goes_once_then_only_the_new_turns(self):
        ref = self.record(started_at=self.chat(turns=[("my battery won't charge", "Try another socket."),
                                                      ("still nothing", "Raising a ticket.")]))
        self.send(ref)
        [first] = self.comments(ref)
        self.assertIn("still nothing", first["content"])
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2, 3, 4])
        self.clock.advance(60)
        self.chat(turns=[("thank you", "You're welcome.")])
        self.send(ref)
        later = self.comments(ref)
        self.assertEqual(len(later), 2)
        self.assertIn("thank you", later[1]["content"])
        self.assertNotIn("still nothing", later[1]["content"])
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2, 3, 4, 5, 6])

    def test_a_long_transcript_goes_in_comments_under_the_limit_each_turn_once(self):
        ref = self.record(started_at=self.chat(turns=[("a" * 9000, "b" * 9000)] * 4))
        self.send(ref)
        comments = self.comments(ref)
        self.assertGreater(len(comments), 1)
        self.assertTrue(all(len(c["content"]) <= 30000 for c in comments))
        self.assertEqual(sum(c["content"].count("a" * 9000) for c in comments), 4)
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), list(range(1, 9)))

    def test_a_comment_whose_answer_was_lost_is_not_posted_twice(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("add_comment", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.send(ref)
        self.assertEqual(self.saved(ref)["state"], "waiting")
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.comments(ref)), 1)
        self.assertEqual(len(self.calls("comments")), 1)
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_each_persons_ticket_gets_only_their_own_run(self):
        first_started = self.chat(turns=[("first person's words", "Ok.")])
        self.photo("upl_first", "cluster-1", self.clock())
        first = self.record(started_at=first_started, cluster_id="cluster-1")
        self.clock.advance(60)
        second_started = self.restart("second person's words", "Hello.")
        self.tickets.close_runs("conv-1", second_started)
        self.photo("upl_second", "cluster-2", self.clock())
        second = self.record(started_at=second_started, cluster_id="cluster-2")
        self.clock.advance(10)
        self.chat(turns=[("more from the second person", "Noted.")])
        self.send(first)
        self.send(second)
        self.assertIn("first person's words", self.text_on(first))
        self.assertNotIn("second person", self.text_on(first))
        self.assertIn("second person's words", self.text_on(second))
        self.assertIn("more from the second person", self.text_on(second))
        self.assertNotIn("first person", self.text_on(second))
        self.assertEqual(self.files(first), ["%s-upl_first.jpg" % first])
        self.assertEqual(self.files(second), ["%s-upl_second.jpg" % second])

    def test_a_photo_from_another_cluster_in_the_same_run_is_not_attached(self):
        started = self.chat()
        self.photo("upl_mine", "cluster-1", self.clock())
        self.photo("upl_other", "cluster-9", self.clock())
        ref = self.record(started_at=started, cluster_id="cluster-1")
        self.send(ref)
        self.assertEqual(self.files(ref), ["%s-upl_mine.jpg" % ref])

    def test_a_time_written_without_microseconds_still_counts_inside_the_run(self):
        # conversation.utc_now_iso drops the microseconds when they are zero.
        # Compared as text, "10:00:00+00:00" sorts before the run's start.
        started = self.chat()
        self.photo("upl_whole_second", "cluster-1", "2026-10-05T10:00:00+00:00")
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.files(ref), ["%s-upl_whole_second.jpg" % ref])


class NoteTests(WorkerCase):
    def test_each_note_is_posted_once_with_its_marker(self):
        ref = self.record(started_at=self.chat())
        self.tickets.add_note(ref, "Customer asked for a person at 10:00", self.clock())
        self.assertTrue(self.worker.run_once())
        marker = "[stage:%s note 1]" % ref
        [note] = [c["content"] for c in self.comments(ref) if marker in c["content"]]
        self.assertIn("Customer asked for a person", note)
        self.assertEqual(self.saved(ref)["posted_notes"], [0])
        self.clock.advance(60)
        self.tickets.add_note(ref, "Customer sent another safety report", self.clock())
        self.assertTrue(self.worker.run_once())
        notes = [c["content"] for c in self.comments(ref) if "[stage:%s note " % ref in c["content"]]
        self.assertEqual(len(notes), 2)
        self.assertIn("another safety report", notes[1])
        self.assertEqual(sorted(self.saved(ref)["posted_notes"]), [0, 1])


class AttachmentTests(WorkerCase):
    def test_each_photo_is_attached_once_named_by_the_reference(self):
        started = self.chat()
        self.photo("upl_one", "cluster-1", self.clock())
        self.photo("upl_two", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(sorted(self.files(ref)), ["%s-upl_one.jpg" % ref, "%s-upl_two.jpg" % ref])
        self.clock.advance(60)
        self.chat(turns=[("thanks", "You're welcome.")])
        self.send(ref)
        self.assertEqual(len(self.calls("upload_attachment")), 2)
        self.assertEqual(len(self.saved(ref)["zoho"]["attachment_ids"]), 2)

    def test_a_file_over_the_limit_is_never_read_and_a_comment_says_so(self):
        started = self.chat()
        key = self.photo("upl_big", "cluster-1", self.clock(), size=25 * MB, kind="video")
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.bucket.reads, [])
        self.assertEqual(self.calls("upload_attachment"), [])
        self.assertIn(key, self.saved(ref)["posted_media"])
        [said] = [c["content"] for c in self.comments(ref) if "too large" in c["content"]]
        self.assertIn("A video", said)
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_file_zoho_refuses_as_too_large_gets_the_same_comment(self):
        started = self.chat()
        key = self.photo("upl_refused", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.desk.fail("upload_attachment", zoho_error(errors.ZohoTooLarge, "RESOURCE_SIZE_EXCEEDED"))
        self.send(ref)
        self.assertEqual(self.files(ref), [])
        self.assertIn(key, self.saved(ref)["posted_media"])
        self.assertEqual(len([c for c in self.comments(ref) if "too large" in c["content"]]), 1)
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_file_whose_answer_was_lost_is_not_uploaded_twice(self):
        started = self.chat()
        self.photo("upl_once", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.desk.fail("upload_attachment", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.send(ref)
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.calls("upload_attachment")), 1)
        self.assertEqual(len(self.calls("attachments")), 1)
        self.assertEqual(self.files(ref), ["%s-upl_once.jpg" % ref])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_without_a_media_bucket_the_ticket_goes_without_files(self):
        started = self.chat()
        self.photo("upl_unreadable", "cluster-1", self.clock())
        self.worker = ZohoWorker(self.tickets, self.desk, self.conversations, None, settings(), self.log,
                                 clock=self.clock)
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.files(ref), [])
        self.assertEqual(self.saved(ref)["state"], "sent")


class AnswerTests(WorkerCase):
    """Zoho's answers, sorted by error code (the table in spec section 4)."""

    def fail_once(self, method, exc):
        ref = self.record(started_at=self.chat())
        self.desk.fail(method, exc)
        self.assertTrue(self.send(ref))
        return ref

    def test_too_many_requests_waits_thirty_seconds_and_counts_no_attempt(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoBusy, "TOO_MANY_REQUESTS"))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["attempts"]), (plus(self.clock(), 30), 0))

    def test_an_access_token_refused_twice_goes_on_the_schedule(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoAuthExpired, "INVALID_OAUTH"))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["attempts"], saved["last_error"]),
                         (plus(self.clock(), 30), 1, "ZohoAuthExpired:INVALID_OAUTH"))

    def test_our_payload_refused_retries_hourly_and_names_the_fields(self):
        ref = self.fail_once("create_ticket",
                             zoho_error(errors.ZohoRejected, "INVALID_DATA", fields=["contactId"]))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["last_error"]),
                         (plus(self.clock(), 3600), "ZohoRejected:INVALID_DATA"))
        [rejected] = self.events("zoho_rejected")
        self.assertEqual((rejected["reference"], rejected["fields"]), (ref, ["contactId"]))

    def test_a_configuration_fault_retries_hourly_and_shows_on_health(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoConfigError, "SCOPE_MISMATCH"))
        self.assertEqual(self.saved(ref)["next_attempt_at"], plus(self.clock(), 3600))
        self.assertEqual(self.worker.status["failing"], "sending failing: SCOPE_MISMATCH")
        self.assertEqual([e["error"] for e in self.events("zoho_rejected")], ["ZohoConfigError:SCOPE_MISMATCH"])
        self.clock.advance(3600)
        self.worker.run_once()
        self.assertEqual(self.saved(ref)["state"], "sent")
        self.assertIsNone(self.worker.status["failing"])

    def test_a_refused_refresh_token_retries_hourly_and_is_alarmed(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoTokenRefused, "invalid_client_secret"))
        self.assertEqual(self.saved(ref)["next_attempt_at"], plus(self.clock(), 3600))
        self.assertEqual(self.worker.status["failing"], "token refused: invalid_client_secret")
        [refused] = self.events("zoho_token_refused")
        self.assertEqual((refused["level"], refused["error"]), ("error", "ZohoTokenRefused:invalid_client_secret"))

    def test_a_throttled_token_endpoint_waits_ten_minutes(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoTokenThrottled, "Access Denied"))
        self.assertEqual(self.saved(ref)["next_attempt_at"], plus(self.clock(), 600))

    def test_an_unknown_outcome_goes_on_the_schedule_with_its_intent_kept(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["intent"]["step"]), (plus(self.clock(), 30), "ticket"))

    def test_the_days_credits_gone_pause_every_record_until_zoho_says(self):
        started = self.chat()
        first, second = self.record(started_at=started), self.record(started_at=started)
        self.desk.fail("create_ticket", zoho_error(errors.ZohoCreditsExhausted, "THRESHOLD_EXCEEDED",
                                                   retry_after_seconds=1200))
        self.wake(first)
        self.wake(second)
        self.assertTrue(self.worker.run_once())
        self.assertFalse(self.worker.run_once())
        self.clock.advance(1199)
        self.assertFalse(self.worker.run_once())
        self.clock.advance(1)
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        self.assertEqual({self.saved(first)["state"], self.saved(second)["state"]}, {"sent"})
        self.assertEqual(len(self.desk.tickets), 2)

    def test_a_ticket_deleted_in_desk_is_gone_and_never_retried(self):
        ref = self.record(started_at=self.chat())
        self.send(ref)
        del self.desk.tickets[self.ticket_of(ref)]
        self.clock.advance(60)
        self.chat(turns=[("hello?", "I'm here.")])
        self.send(ref)
        self.assertEqual(self.saved(ref)["state"], "gone")
        self.assertEqual([e["reference"] for e in self.events("zoho_ticket_gone")], [ref])
        self.clock.advance(3600)
        self.wake(ref)
        self.assertFalse(self.worker.run_once())


class RetryTests(WorkerCase):
    def test_the_schedule(self):
        self.assertEqual([retry_wait(n) for n in range(1, 9)], [30, 60, 120, 300, 600, 1800, 3600, 3600])

    def test_the_waits_grow_then_settle_hourly_and_nothing_is_dropped(self):
        ref = self.record(started_at=self.chat())
        self.desk.down = True
        self.clock.advance(120)
        for _ in range(8):
            self.assertTrue(self.worker.run_once())
            self.clock.now = self.saved(ref)["next_attempt_at"]
        self.assertEqual([e["wait_seconds"] for e in self.events("zoho_retry")],
                         [30, 60, 120, 300, 600, 1800, 3600, 3600])
        self.assertEqual(self.saved(ref)["attempts"], 8)
        self.desk.down = False
        self.assertTrue(self.worker.run_once())
        self.assertEqual((self.saved(ref)["state"], self.saved(ref)["attempts"]), ("sent", 0))
        self.assertEqual(len(self.desk.tickets), 1)


class OverdueTests(WorkerCase):
    def test_a_safety_ticket_is_late_at_ten_minutes_even_with_a_turn_every_minute(self):
        ref = self.record(started_at=self.chat(), kind="safety", category="battery_safety")
        self.desk.down = True
        for _ in range(9):
            self.clock.advance(60)
            self.chat(turns=[("it is still hot", "Please keep away from it.")])
            self.wake(ref)
            self.worker.run_once()
            self.worker.check_overdue()
        self.assertEqual(self.events("safety_ticket_late"), [])
        self.clock.advance(61)
        self.worker.check_overdue()
        [late] = self.events("safety_ticket_late")
        self.assertEqual((late["reference"], late["level"]), (ref, "error"))
        self.assertGreaterEqual(late["age_seconds"], 600)
        self.assertEqual(self.saved(ref)["state"], "stuck")
        self.clock.advance(60)
        self.worker.check_overdue()
        self.assertEqual(len(self.events("safety_ticket_late")), 1)
        self.clock.advance(3600)
        self.worker.check_overdue()
        self.assertEqual(len(self.events("safety_ticket_late")), 2)

    def test_any_other_ticket_is_stuck_after_a_day_and_is_still_retried(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(86_399)
        self.worker.check_overdue()
        self.assertEqual(self.events("zoho_ticket_stuck"), [])
        self.clock.advance(2)
        self.worker.check_overdue()
        [stuck] = self.events("zoho_ticket_stuck")
        self.assertEqual((stuck["reference"], stuck["level"]), (ref, "error"))
        self.assertEqual(self.saved(ref)["state"], "stuck")
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")


class SecretTests(WorkerCase):
    def test_no_secret_or_phone_reaches_the_log_and_no_secret_the_record(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", zoho_error(errors.ZohoTokenRefused, "invalid_client_secret"))
        self.send(ref)
        self.clock.advance(3600)
        self.worker.run_once()
        logged = json.dumps(self.log.events)
        stored = json.dumps(self.saved(ref), default=str)
        for secret in ("secret-test", "refresh-test"):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, stored)
        self.assertNotIn("9999999999", logged)


class LoopTests(WorkerCase):
    def test_the_thread_sends_what_is_due_and_stops(self):
        ref = self.record(started_at=self.chat())
        self.wake(ref)
        self.worker.pass_seconds = 0.01
        self.worker.start()
        try:
            self.assertTrue(wait_for(lambda: self.saved(ref)["state"] == "sent"))
            self.assertTrue(self.worker.status["running"])
        finally:
            self.worker.stop()
        self.assertFalse(self.worker.status["running"])
        self.assertIsNotNone(self.worker.status["last_pass_at"])
        self.assertFalse([t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()])

    def test_a_pass_that_fails_is_logged_by_class_and_the_loop_carries_on(self):
        class Broken(Exception):
            pass

        def take_due(*args, **kwargs):
            raise Broken("boom with details")

        self.worker.store = SimpleNamespace(take_due=take_due, overdue=lambda now, mode: [])
        self.worker.pass_seconds = 0.01
        self.worker.start()
        try:
            self.assertTrue(wait_for(lambda: len(self.events("zoho_worker_error")) >= 2))
        finally:
            self.worker.stop()
        failures = self.events("zoho_worker_error")
        self.assertEqual({(e["error"], e["level"]) for e in failures}, {("Broken", "error")})
        self.assertNotIn("boom", json.dumps(failures))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_worker
```

Expected: one import error, `ModuleNotFoundError: No module named 'emotorad_ai.zoho.worker'`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/zoho/worker.py`:

```python
"""The Zoho worker (spec 2026-10-05 section 4). It sends recorded tickets to
Zoho Desk after the customer has their reply.

A daemon thread in the API process. api.py's lifespan starts it, and nothing
starts it at import. It takes one due record at a time under a five-minute
lease and runs the steps in order: the contact, the ticket, this run's
transcript, the notes, then this run's photos and videos. Each step is saved
before the next, so a retry starts where the last one stopped.

Before every write to Zoho it saves what it is about to do (`intent`), and
sends nothing if that save fails. A later pass that finds an intent with no
result checks Zoho before it writes again: the contact's tickets for a
ticket, the ticket's comments for a comment, its attachments for a file. This
is how a timeout after Zoho made the ticket still ends in one ticket, not two.

Failures are sorted by Zoho's error code, not by the HTTP status alone (the
answers table in section 4). Nothing is ever dropped. A record with work
outstanding is retried on the schedule below for as long as it takes. Once a
record is late (10 minutes for an urgent one, a day for any other), it is
logged at error level once an hour.

The log carries references, Zoho's ticket number, error classes and Zoho's
error codes. It never carries a phone, a token, a request or a response body.
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ..storage.s3 import StorageError
from ..tickets.clock import now_iso, plus
from .desk import find_adoptable
from .errors import (
    ZohoBusy,
    ZohoConfigError,
    ZohoCreditsExhausted,
    ZohoError,
    ZohoGone,
    ZohoRejected,
    ZohoTokenRefused,
    ZohoTokenThrottled,
    ZohoTooLarge,
)
from .payload import ticket_payload, transcript_chunks

THREAD_NAME = "zoho-worker"
# How often the loop looks for due records when nothing wakes it.
PASS_SECONDS = 30.0
# How long a taken record belongs to this worker. The lease is renewed before
# every Zoho call, so it runs out only when a worker has died.
LEASE_SECONDS = 300.0
# The wait after the first, second, ... failure in a row. Hourly after the last.
RETRY_WAITS = (30, 60, 120, 300, 600, 1800)
HOURLY = 3600
# Zoho's 429 TOO_MANY_REQUESTS is about calls in flight, not the day's credits.
BUSY_WAIT = 30
# The token endpoint throttles for ten minutes, and auth.py sends no request
# until then.
TOKEN_THROTTLED_WAIT = 600
# How long a record that is not urgent waits while credits are below the floor.
CREDITS_FLOOR_WAIT = 600
# A late or stuck record is logged at most this often.
OVERDUE_LOG_SECONDS = 3600
# How long stop() waits for a pass in progress. The thread is a daemon either way.
STOP_JOIN_SECONDS = 10.0
# The contact's last name when the OMS record gave no name (step 1).
UNNAMED_CONTACT = "AI chat customer"
_WHAT = {"image": "A photo", "video": "A video"}


def retry_wait(attempts: int) -> int:
    """Seconds before the next try after `attempts` failures in a row."""
    if 1 <= attempts <= len(RETRY_WAITS):
        return RETRY_WAITS[attempts - 1]
    return HOURLY


def _moment(text: Any) -> Optional[datetime]:
    """An ISO time as an aware datetime, or None. Times are parsed, never
    compared as text: conversation.utc_now_iso drops the microseconds when they
    are zero, and "10:00:00+00:00" sorts before "10:00:00.000000+00:00"."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _seconds(since: Any, until: Any) -> float:
    start, end = _moment(since), _moment(until)
    if start is None or end is None:
        return 0.0
    return (end - start).total_seconds()


def _folded(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def _file_name(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _error_name(exc: BaseException) -> str:
    """The error class and Zoho's code. This is all that is logged or kept."""
    code = getattr(exc, "error", None)
    return "%s:%s" % (type(exc).__name__, code) if code else type(exc).__name__


class _Dropped(Exception):
    """A save matched nothing: the record was erased, or another worker holds it."""


class _Job:
    """One taken record, as this pass sees it."""

    def __init__(self, record: Dict[str, Any], token: str) -> None:
        # A copy: the in-memory store hands back its own dict.
        self.record = copy.deepcopy(record)
        self.token = token
        self.ref: str = record["_id"]
        self.wake = record.get("wake", 0)
        self.attempts = int(record.get("attempts") or 0)
        self.zoho: Dict[str, Any] = copy.deepcopy(record.get("zoho") or {})

    @property
    def intent(self) -> Dict[str, Any]:
        return self.record.get("intent") or {}

    @property
    def conversation_id(self) -> str:
        return self.record.get("conversation_id") or "zoho"


class ZohoWorker:
    """Sends due ticket records to Zoho Desk, one at a time, under a lease."""

    def __init__(
        self,
        store: Any,
        client: Any,
        conversations: Any,
        media_reader: Any,
        settings: Any,
        log: Any,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self.store = store
        self.client = client
        self.conversations = conversations
        # S3Store, or None on a deployment with no media bucket. With none, no
        # photo or video was kept, so there is nothing to attach.
        self.media_reader = media_reader
        self.settings = settings
        self.log = log
        self._clock = clock
        # Read by the loop on every wait. The tests shorten it.
        self.pass_seconds = PASS_SECONDS
        # For /health. `failing` is set while Zoho refuses our token or our
        # configuration, and cleared when the next record is sent.
        self.status: Dict[str, Any] = {"running": False, "last_pass_at": None, "failing": None}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Set by Zoho's THRESHOLD_EXCEEDED. No record is taken before this time.
        self._paused_until: Optional[str] = None
        # Reference -> when it was last logged as late or stuck.
        self._overdue_logged: Dict[str, str] = {}

    # -- life -------------------------------------------------------------

    def start(self) -> None:
        """Start the loop. Called by api.py's lifespan and nowhere else. Safe to call twice."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self.status["running"] = True
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to end, and wait briefly for a pass in progress."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(STOP_JOIN_SECONDS)
        self.status["running"] = False

    def wake(self) -> None:
        """A turn ended or a note came in: look now, not at the next pass."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Cleared before the pass, so a wake during the pass brings the
            # next pass forward.
            self._wake.clear()
            try:
                self.check_overdue()
                while not self._stop.is_set() and self.run_once():
                    pass
                self.status["last_pass_at"] = self._clock()
            except Exception as exc:
                # The class only, never the message: a driver's text can
                # include what it failed on.
                self.log.emit("zoho_worker_error", "zoho", level="error", error=type(exc).__name__)
            self._wake.wait(self.pass_seconds)

    # -- one record ---------------------------------------------------------

    def run_once(self) -> bool:
        """Take one due record and send what it has outstanding. True if one was taken."""
        now = self._clock()
        if self._paused_until is not None:
            if now < self._paused_until:
                return False
            self._paused_until = None
        token = uuid.uuid4().hex
        record = self.store.take_due(now, self.settings.mode, LEASE_SECONDS, token)
        if record is None:
            return False
        job = _Job(record, token)
        try:
            if not job.record.get("urgent") and self._credits_low():
                self._defer_for_credits(job)
                return True
            self._send(job)
        except _Dropped:
            self.log.emit("zoho_record_dropped", job.conversation_id, reference=job.ref)
        except (ZohoError, StorageError) as exc:
            self._failed(job, exc)
        return True

    def _send(self, job: _Job) -> None:
        """The steps in order, each saved before the next."""
        if not job.zoho.get("contact_id"):
            self._contact(job)
        if not job.zoho.get("ticket_id"):
            self._ticket(job)
        self._settle_intent(job)
        self._transcript(job)
        self._notes(job)
        self._attachments(job)
        self._finish(job)

    # -- step 1: the contact --------------------------------------------------

    def _contact(self, job: _Job, use_stored: bool = True) -> None:
        record = job.record
        if record.get("mode") != "live":
            # Contacts belong to the whole organisation, not to a department,
            # so a test never searches for a contact or makes one.
            contact_id = self.settings.test_contact_id
        elif not self._searches_contacts(job):
            # A number nobody proved never goes on a real customer's contact.
            contact_id = self.settings.unverified_contact_id
        else:
            # Our own first: the store returns a contact only from a live,
            # verified record of this number, never a test or unverified one.
            stored = self.store.contact_for(record["phone"]) if use_stored else None
            contact_id = stored or self._find_or_create_contact(job)
        job.zoho["contact_id"] = contact_id
        changes: Dict[str, Any] = {"zoho": copy.deepcopy(job.zoho)}
        if job.intent.get("step") == "contact":
            changes["intent"] = None
        self._save(job, changes)

    def _find_or_create_contact(self, job: _Job) -> str:
        """Search by phone, then by mobile, in separate calls. One match is
        used. Of several, the one with the name on the OMS record is used.
        Otherwise a new contact is made. A create that never got an answer
        (a crash or a timeout) comes back here, so the search runs again
        before any second create."""
        record = job.record
        last_ten = re.sub(r"[^0-9]", "", record["phone"])[-10:]
        matches = list(self._call(job, self.client.search_contacts, "phone", last_ten) or [])
        if not matches:
            matches = list(self._call(job, self.client.search_contacts, "mobile", last_ten) or [])
        if len(matches) == 1:
            return str(matches[0]["id"])
        name = _folded(record.get("customer_name"))
        if name:
            for match in matches:
                if name in (_folded(match.get("lastName")), _folded(match.get("name"))):
                    return str(match["id"])
        self._intent(job, {"step": "contact"})
        return str(self._call(job, self.client.create_contact,
                              record.get("customer_name") or UNNAMED_CONTACT, record["phone"]))

    def _searches_contacts(self, job: _Job) -> bool:
        record = job.record
        return record.get("mode") == "live" and record.get("identity") == "verified" and bool(record.get("phone"))

    # -- step 2: the ticket ---------------------------------------------------

    def _ticket(self, job: _Job) -> None:
        try:
            self._create_or_adopt(job)
        except ZohoGone:
            if not self._searches_contacts(job):
                raise
            # The stored contact was deleted or merged in Desk. Forget it and
            # find the person again, once.
            job.zoho.pop("contact_id", None)
            self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None})
            self._contact(job, use_stored=False)
            self._create_or_adopt(job)

    def _create_or_adopt(self, job: _Job) -> None:
        contact_id = job.zoho["contact_id"]
        if job.intent.get("step") == "ticket":
            # A create was started and its answer never came back. Look
            # before making a second one. The contact's own ticket list is
            # used, not Zoho's search, which can lag by minutes.
            found = self._adoptable(job, contact_id)
            if found is not None:
                self._ticket_done(job, found, adopted=True)
                return
        payload = ticket_payload(job.record, self.settings, contact_id)
        self._intent(job, {"step": "ticket"})
        self._ticket_done(job, self._call(job, self.client.create_ticket, payload))

    def _adoptable(self, job: _Job, contact_id: str) -> Optional[Dict[str, Any]]:
        tickets = self._call(job, self.client.contact_tickets, contact_id, self._department(job)) or []
        return find_adoptable(tickets, self.settings.cf_chat_reference, job.record["chat_reference"])

    def _ticket_done(self, job: _Job, ticket: Dict[str, Any], adopted: bool = False) -> None:
        job.zoho.update(ticket_id=str(ticket["id"]), ticket_number=ticket.get("ticketNumber"),
                        web_url=ticket.get("webUrl"))
        self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None})
        if adopted:
            self.log.emit("zoho_ticket_adopted", job.conversation_id, reference=job.ref,
                          zoho_number=job.zoho.get("ticket_number"))

    def _department(self, job: _Job) -> Optional[str]:
        if job.record.get("mode") == "live":
            return self.settings.department_id
        return self.settings.test_department_id

    # -- an intent left by an earlier pass --------------------------------------

    def _settle_intent(self, job: _Job) -> None:
        """Deal with an intent left by a pass that crashed or never heard
        back: check the ticket before writing again. If the write is found,
        it is recorded as posted. If not, it never landed, and its step runs
        again below."""
        intent = job.intent
        step = intent.get("step")
        if not step:
            return
        ticket_id = job.zoho["ticket_id"]
        if step in ("transcript", "note", "media_note"):
            marker = intent.get("marker") or ""
            for comment in self._call(job, self.client.comments, ticket_id) or []:
                if marker and marker in (comment.get("content") or ""):
                    self._comment_done(job, str(comment["id"]), intent)
                    return
        elif step == "attachment":
            for attachment in self._call(job, self.client.attachments, ticket_id) or []:
                if attachment.get("name") == intent.get("filename"):
                    self._attachment_done(job, str(attachment["id"]), intent["key"])
                    return
        self._save(job, {"intent": None})

    # -- step 3: the transcript -------------------------------------------------

    def _transcript(self, job: _Job) -> None:
        """This run's turns that are not on Zoho yet, as private comments of
        at most 30,000 characters. Each comment starts with its marker."""
        posted = set(job.record.get("posted_turns") or [])
        turns = [turn for turn in self.conversations.transcript(job.record["conversation_id"])
                 if turn.n not in posted and self._in_run(job, turn.at)]
        if not turns:
            return
        for text, numbers in transcript_chunks(job.ref, job.record["chat_reference"], turns):
            marker = text.split("\n", 1)[0]
            self._post_comment(job, {"step": "transcript", "marker": marker, "turns": list(numbers)}, text)

    # -- step 4: the notes --------------------------------------------------

    def _notes(self, job: _Job) -> None:
        posted = set(job.record.get("posted_notes") or [])
        for index, note in enumerate(job.record.get("notes") or []):
            if index in posted:
                continue
            marker = "[%s note %d]" % (job.record["chat_reference"], index + 1)
            at = _moment(note.get("at"))
            when = at.strftime("%d %b %H:%M UTC") if at is not None else "at a time not recorded"
            text = "%s\n%s\nAdded %s." % (marker, note.get("text") or "", when)
            self._post_comment(job, {"step": "note", "marker": marker, "index": index}, text)

    # -- step 5: the photos and videos ---------------------------------------------

    def _attachments(self, job: _Job) -> None:
        """This run's media from the same cluster, each attached once, with a
        file name that starts with our reference."""
        if self.media_reader is None:
            return
        record = job.record
        posted = set(record.get("posted_media") or [])
        cluster = record.get("cluster_id")
        for item in self.conversations.media_of(record["conversation_id"]):
            key = item["_id"]
            if key in posted or not self._in_run(job, item.get("stored_at")):
                continue
            if cluster and item.get("cluster_id") != cluster:
                continue
            if int(item.get("size_bytes") or 0) > self.settings.attachment_limit_bytes:
                # Never read: a comment says so, and the file stays with us.
                self._too_large(job, item)
                continue
            data = self.media_reader.get_bytes(key)
            filename = "%s-%s" % (job.ref, _file_name(key))
            self._intent(job, {"step": "attachment", "key": key, "filename": filename})
            try:
                attachment_id = self._call(job, self.client.upload_attachment, job.zoho["ticket_id"], filename,
                                           data, item.get("mime_type") or "application/octet-stream")
            except ZohoTooLarge:
                self._too_large(job, item)
                continue
            self._attachment_done(job, str(attachment_id), key)

    def _too_large(self, job: _Job, item: Dict[str, Any]) -> None:
        key = item["_id"]
        marker = "[%s file %s]" % (job.record["chat_reference"], _file_name(key))
        size_mb = int(item.get("size_bytes") or 0) / (1024 * 1024)
        text = "%s\n%s sent in this chat (%.0f MB) was too large to attach here. The AI team keeps it." % (
            marker, _WHAT.get(item.get("kind"), "A file"), size_mb)
        self._post_comment(job, {"step": "media_note", "marker": marker, "key": key}, text)

    # -- writes and their records ---------------------------------------------

    def _post_comment(self, job: _Job, intent: Dict[str, Any], text: str) -> None:
        self._intent(job, intent)
        comment_id = self._call(job, self.client.add_comment, job.zoho["ticket_id"], text)
        self._comment_done(job, str(comment_id), intent)

    def _comment_done(self, job: _Job, comment_id: str, intent: Dict[str, Any]) -> None:
        job.zoho.setdefault("comment_ids", []).append(comment_id)
        field, values = {
            "transcript": ("posted_turns", list(intent.get("turns") or ())),
            "note": ("posted_notes", [intent.get("index")]),
            "media_note": ("posted_media", [intent.get("key")]),
        }[intent["step"]]
        self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None}, add_to_set={field: values})

    def _attachment_done(self, job: _Job, attachment_id: str, key: str) -> None:
        job.zoho.setdefault("attachment_ids", []).append(attachment_id)
        self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None}, add_to_set={"posted_media": [key]})

    def _finish(self, job: _Job) -> None:
        """Nothing is outstanding: the record is sent. This save also checks
        that `wake` has not changed. If a turn ended or a note came in while
        this pass was working, what it added is not on Zoho yet, so the record
        goes back to waiting, due now."""
        self.status["failing"] = None
        sent = {"state": "sent", "attempts": 0, "last_error": None, "intent": None,
                "lease_until": None, "lease_token": None}
        if self.store.save(job.ref, job.token, sent, expect_wake=job.wake):
            self.log.emit("zoho_ticket_sent", job.conversation_id, reference=job.ref,
                          zoho_number=job.zoho.get("ticket_number"), attempts=job.attempts + 1,
                          credits_remaining=self._credits())
            return
        again = {"state": "waiting", "attempts": 0, "last_error": None, "next_attempt_at": self._clock(),
                 "lease_until": None, "lease_token": None}
        if not self.store.save(job.ref, job.token, again):
            raise _Dropped()

    # -- failures ---------------------------------------------------------------

    def _failed(self, job: _Job, exc: BaseException) -> None:
        """Sort a failure by Zoho's answer (the table in spec section 4) and
        put the record back with its next try. The intent stays, so the next
        pass checks Zoho before it writes again."""
        now = self._clock()
        error = _error_name(exc)
        code = getattr(exc, "error", None) or type(exc).__name__
        cid = job.conversation_id
        if isinstance(exc, ZohoGone) and job.zoho.get("ticket_id"):
            # Deleted or merged in Desk. Nothing more can be added to it, and a
            # new ticket would reopen what someone closed. Logged, not retried.
            gone = {"state": "gone", "last_error": error, "intent": None, "lease_until": None, "lease_token": None}
            if self.store.save(job.ref, job.token, gone):
                self.log.emit("zoho_ticket_gone", cid, reference=job.ref, zoho_number=job.zoho.get("ticket_number"))
            else:
                self.log.emit("zoho_record_dropped", cid, reference=job.ref)
            return
        attempts = job.attempts + 1
        if isinstance(exc, ZohoBusy):
            wait, attempts = BUSY_WAIT, job.attempts
        elif isinstance(exc, ZohoCreditsExhausted):
            # The organisation's credits for the day are used up. No record
            # is sent until Zoho's Retry-After time. This is not the record's
            # fault, so it counts no attempt.
            wait, attempts = int(getattr(exc, "retry_after_seconds", None) or HOURLY), job.attempts
            self._paused_until = plus(now, wait)
        elif isinstance(exc, ZohoTokenRefused):
            wait = HOURLY
            self.status["failing"] = "token refused: %s" % code
            self.log.emit("zoho_token_refused", cid, level="error", reference=job.ref, error=error)
        elif isinstance(exc, (ZohoConfigError, ZohoGone)):
            # Scope, organisation or licence, or the fixed test or unverified
            # contact deleted in Desk. A person has to fix it.
            wait = HOURLY
            self.status["failing"] = "sending failing: %s" % code
            self.log.emit("zoho_rejected", cid, reference=job.ref, error=error)
        elif isinstance(exc, (ZohoRejected, ZohoTooLarge)):
            wait = HOURLY
            self.log.emit("zoho_rejected", cid, reference=job.ref, error=error,
                          fields=[str(name) for name in getattr(exc, "fields", None) or ()])
        elif isinstance(exc, ZohoTokenThrottled):
            wait = TOKEN_THROTTLED_WAIT
        else:
            # Zoho unreachable, an unknown outcome, an access token refused
            # twice, or a photo S3 could not give us: the schedule.
            wait = retry_wait(attempts)
        retry = {"attempts": attempts, "next_attempt_at": plus(now, wait), "last_error": error,
                 "lease_until": None, "lease_token": None}
        if not self.store.save(job.ref, job.token, retry):
            self.log.emit("zoho_record_dropped", cid, reference=job.ref)
            return
        self.log.emit("zoho_retry", cid, reference=job.ref, error=error, attempts=attempts, wait_seconds=wait)

    def _credits(self) -> Optional[int]:
        return getattr(getattr(self.client, "http", None), "last_credits_remaining", None)

    def _credits_low(self) -> bool:
        remaining = self._credits()
        return remaining is not None and remaining < self.settings.credits_floor

    def _defer_for_credits(self, job: _Job) -> None:
        """Credits are shared with the OMS, whose calls do not retry. Below
        the floor only urgent records are sent, and the others wait."""
        later = {"next_attempt_at": plus(self._clock(), CREDITS_FLOOR_WAIT), "lease_until": None, "lease_token": None}
        if not self.store.save(job.ref, job.token, later):
            raise _Dropped()
        self.log.emit("zoho_retry", job.conversation_id, reference=job.ref, error="credits_floor",
                      attempts=job.attempts, wait_seconds=CREDITS_FLOOR_WAIT, credits_remaining=self._credits())

    # -- late and stuck -------------------------------------------------------------

    def check_overdue(self) -> None:
        """Log records still waiting past their limit, by reference and age
        only, at most once an hour each, and mark them stuck. Stuck records
        are still retried."""
        now = self._clock()
        seen = set()
        for record in self.store.overdue(now, self.settings.mode):
            reference = record["_id"]
            seen.add(reference)
            last = self._overdue_logged.get(reference)
            if last is not None and _seconds(last, now) < OVERDUE_LOG_SECONDS:
                continue
            self._overdue_logged[reference] = now
            event = "safety_ticket_late" if record.get("urgent") else "zoho_ticket_stuck"
            self.log.emit(event, "zoho", level="error", reference=reference,
                          age_seconds=int(_seconds(record.get("due_since") or now, now)))
            if record.get("state") != "stuck":
                # Saved against whatever lease the record holds now. If a
                # worker took it in between, that worker's own save wins.
                self.store.save(reference, record.get("lease_token"), {"state": "stuck"})
        for reference in [r for r in self._overdue_logged if r not in seen]:
            del self._overdue_logged[reference]

    # -- the store, under the lease ---------------------------------------------------

    def _in_run(self, job: _Job, at: Any) -> bool:
        """At or after the run's start, and before its end. close_runs sets the
        end when a new run begins on the conversation. A record with no start
        takes the conversation from its beginning."""
        moment = _moment(at)
        if moment is None:
            return False
        start, end = _moment(job.record.get("started_at")), _moment(job.record.get("ended_at"))
        return (start is None or moment >= start) and (end is None or moment < end)

    def _save(self, job: _Job, changes: Dict[str, Any], add_to_set: Optional[Dict[str, Any]] = None) -> None:
        if not self.store.save(job.ref, job.token, changes, add_to_set=add_to_set):
            raise _Dropped()
        for name, value in changes.items():
            job.record[name] = copy.deepcopy(value)
        for name, values in (add_to_set or {}).items():
            have = job.record.setdefault(name, [])
            have.extend(v for v in values if v not in have)

    def _intent(self, job: _Job, intent: Dict[str, Any]) -> None:
        """Saved before the write it names. If this save fails, nothing is sent."""
        self._save(job, {"intent": dict(intent, at=self._clock())})

    def _call(self, job: _Job, method: Callable[..., Any], *args: Any) -> Any:
        """One Zoho call, after the lease is renewed for it."""
        if not self.store.renew_lease(job.ref, job.token, plus(self._clock(), LEASE_SECONDS)):
            raise _Dropped()
        return method(*args)
```

- [ ] **Step 4: Run the module, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_worker
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: the module passes. The suite's count is the count after Task 8 plus this file's tests, and the only failure is the known environmental one (`tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`). No existing test changes: nothing imports the worker yet.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/zoho/worker.py tests/test_zoho_worker.py
git commit -m "feat: the Zoho worker sends recorded tickets after the reply" -m "Resumable steps under a lease: contact, ticket, this run's transcript, notes, then this run's photos and videos. An intent is saved before every write and looked up on Zoho before any second one, so a timeout after Zoho made the ticket still gives one ticket. Answers are sorted by Zoho's error code; nothing is dropped; late safety tickets and stuck ones are logged hourly at error level." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Wiring: api.py only, health, the lifespan, the local chat page, and old mock references

**Files:**
- Create: `src/emotorad_ai/zoho/wiring.py`
- Modify: `src/emotorad_ai/api.py:80-87` (imports), `:152` (`ZOHO` after `AMIGO`), `:185-231` (`_build_registry` passes the router), `:290-296` (`_lifespan` starts and stops the worker), `:446-462` (`health` gains `zoho` and the ticket fields)
- Modify: `src/emotorad_ai/enrichment.py:106-129` (`summarise_past` gains `drop_mock_tickets`)
- Modify: `src/emotorad_ai/runtime.py:633-637` (passes the flag), `:1128-1137` (new `_records_real_tickets` after `_user_key`)
- Modify: `scripts/chat_local.py:56-60` (`WITHHELD`), `:86-88` (a status line), `:93` (`server_env`)
- Modify: `tests/test_api_health.py:12-32`, `:148-150` (pinned dict, Zoho names blanked)
- Modify: `tests/test_memory.py` (imports, two new tests)
- Test: `tests/test_zoho_wiring.py` (new)

Line numbers are as of `9cc10af`. Tasks 1 to 5 may have moved them, so find each block by the quoted text.

**Interfaces:**
- Consumes:
  - `zoho.settings.load_zoho_settings(env) -> Tuple[Optional[ZohoSettings], str]`, `zoho.settings.startup_problem(settings, *, region, store_kind, ticket_store, dev_codes, otp_is_mock) -> Optional[str]`
  - `zoho.http.DeskHTTP(opener=urllib.request.urlopen, timeout=8.0)`, `zoho.auth.TokenSource(settings, http)`, `zoho.desk.DeskClient(settings, tokens, http)`
  - `zoho.worker.ZohoWorker`, `THREAD_NAME` (Task 9)
  - `tickets.seam.DeskTicketSystem(store, mode, environment, clock=now_iso, wake=...)`, `tickets.seam.TicketRouter(desk, mock)` with `records_real_tickets = True` and `.store`
  - `tools.mocks.MockTicketSystem`, `tools.mocks.build_registry(ticket_system=...)`
  - `wiring.build_stores(...)`, which returns `Stores` with `.tickets` (Task 2); `stores.mongo.ensure_indexes`, whose `INDEXES` covers `tickets` (Task 2)
  - `TicketStore.counts(mode, now)`, `TicketStore.listing(mode)`, `TicketStore.has_unique_source_key()`
  - `tickets.clock.now_iso`, `tickets.kinds.is_desk_reference`, `tickets.record.new_record`
  - `Runtime`'s safety branch passing `persona` on its `ToolContext` (Task 5)
- Produces:
  - `zoho.wiring.ZohoWiring(status: str, router: Optional[TicketRouter] = None, worker: Optional[ZohoWorker] = None, store: Optional[TicketStore] = None)`
  - `zoho.wiring.build_zoho(env, *, ticket_store, store_kind, conversations, media_reader, log, otp_is_mock, opener=None) -> ZohoWiring`
  - `zoho.wiring.zoho_status(wiring) -> str`, `zoho.wiring.ticket_health(wiring, now) -> Dict[str, Any]`, `zoho.wiring.NOT_CONFIGURED`, `zoho.wiring.STORE_UNREACHABLE = "misconfigured: store unreachable"`
  - `api.ZOHO: ZohoWiring`. `/health` gains `"zoho"`, plus `"tickets_waiting"`, `"tickets_stuck"`, `"tickets_held"`, `"zoho_worker"` and `"oldest_due_seconds"` while Zoho is on or records are outstanding
  - `enrichment.summarise_past(summaries, drop_mock_tickets: bool = False)`
  - `Runtime._records_real_tickets() -> bool` (Tasks 13 to 15 can use it for "only when Zoho is on")
  - `scripts/chat_local.ZOHO_SETTINGS`, `WITHHELD` (now includes them), `WITHHELD_PREFIX = "EMOTORAD_ZOHO_"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zoho_wiring.py`:

```python
"""Zoho on or off, decided once, and only in api.py (spec 2026-10-05,
sections 1 and 9).

Every misconfiguration leaves the mock in place, exactly as when Zoho is off,
and records nothing in `tickets`. The worker is built at import and started
only by the lifespan. The CLI, the live evaluation and the playground keep the
mock even with every Zoho setting present. Nothing here reaches Zoho: the
opener given to the wiring records each call and refuses it.
"""

import asyncio
import importlib
import io
import os
import pathlib
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import mongomock

from emotorad_ai import cli, wiring
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.live_eval.runner import scenario_registry
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import ensure_indexes
from emotorad_ai.tickets.clock import now_iso
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.seam import TicketRouter
from emotorad_ai.tools.mocks import MockTicketSystem, build_registry
from emotorad_ai.zoho.wiring import ZohoWiring, build_zoho, ticket_health, zoho_status
from emotorad_ai.zoho.worker import THREAD_NAME, ZohoWorker
from tests.live_eval_helpers import TODAY as EVAL_TODAY
from tests.live_eval_helpers import scenario
from tests.test_chat_page_new_bot import load_launcher
from tests.test_runtime_persistence import TODAY, runtime_on, send

ROOT = pathlib.Path(__file__).resolve().parents[1]
ZOHO_NAMES = (
    "EMOTORAD_ZOHO_REFRESH_TOKEN", "EMOTORAD_ZOHO_CLIENT_ID", "EMOTORAD_ZOHO_CLIENT_SECRET",
    "EMOTORAD_ZOHO_ORG_ID", "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID", "EMOTORAD_ZOHO_TEST_CONTACT_ID",
    "EMOTORAD_ZOHO_DEPARTMENT_ID", "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID", "EMOTORAD_ZOHO_LIVE",
    "EMOTORAD_ZOHO_CF_CHAT_REFERENCE", "EMOTORAD_ZOHO_CF_SOURCE", "EMOTORAD_ZOHO_PRIORITY_HIGH",
    "EMOTORAD_ZOHO_PRIORITY_MEDIUM", "EMOTORAD_ZOHO_CHANNEL", "EMOTORAD_ZOHO_CREDITS_FLOOR",
    "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB",
)
# Everything else api.py reads at import, blanked so this machine's own
# environment cannot change what is tested.
BASE = {
    "EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": "", "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "",
    "EMOTORAD_AMIGO_PG_DSN": "", "EMOTORAD_AI_MEDIA_BUCKET": "", "EMOTORAD_OMS_API_KEY": "",
    "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb",
}


def zoho_env(live=False, **changes):
    """Every Zoho setting set to a fake value, in the test department unless `live`."""
    env = {
        "EMOTORAD_ZOHO_REFRESH_TOKEN": "refresh-test", "EMOTORAD_ZOHO_CLIENT_ID": "client-test",
        "EMOTORAD_ZOHO_CLIENT_SECRET": "secret-test", "EMOTORAD_ZOHO_ORG_ID": "org-test",
        "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID": "dept-test", "EMOTORAD_ZOHO_TEST_CONTACT_ID": "contact-test",
        "EMOTORAD_ZOHO_DEPARTMENT_ID": "dept-real", "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID": "contact-unverified",
        "EMOTORAD_ZOHO_LIVE": "yes" if live else "no",
        "EMOTORAD_ZOHO_CF_CHAT_REFERENCE": "cf_chat_reference", "EMOTORAD_ZOHO_CF_SOURCE": "cf_source",
        "EMOTORAD_ZOHO_PRIORITY_HIGH": "High", "EMOTORAD_ZOHO_PRIORITY_MEDIUM": "Medium",
        "EMOTORAD_ZOHO_CHANNEL": "Chat", "EMOTORAD_ZOHO_CREDITS_FLOOR": "1000",
        "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB": "20",
        "EMOTORAD_AI_ENV": "stage", "AWS_REGION": "ap-south-1", "EMOTORAD_AI_DEV_CODES": "",
    }
    env.update(changes)
    return env


def blank_zoho():
    """Every Zoho name blank, including any on this machine that this file does not know."""
    names = set(ZOHO_NAMES) | {name for name in os.environ if name.startswith("EMOTORAD_ZOHO_")}
    return {name: "" for name in names}


def mongo_stores(indexed=True):
    client = mongomock.MongoClient()
    if indexed:
        ensure_indexes(client[Settings().mongo_db])
    return wiring.build_stores(Settings(store="mongodb"), client=client)


def memory_stores():
    return wiring.build_stores(Settings(store="memory"))


def refusing_opener(calls):
    def opener(request, timeout=None):
        calls.append(getattr(request, "full_url", request))
        raise urllib.error.URLError("no network in tests")

    return opener


def wire(env, stores, store_kind="mongodb", otp_is_mock=False, log=None, calls=None):
    return build_zoho(env, ticket_store=stores.tickets, store_kind=store_kind, conversations=stores.conversations,
                      media_reader=None, log=log if log is not None else EventLog(path=None),
                      otp_is_mock=otp_is_mock, opener=refusing_opener(calls if calls is not None else []))


def nothing_recorded(store):
    return store.listing("test") == [] and store.listing("live") == []


def zoho_threads():
    return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]


def a_record(store, mode="test"):
    reference, now = store.next_reference(), now_iso()
    return store.insert(new_record(
        reference=reference, chat_reference="stage:" + reference, source_key="conv-1:%s:handover:%s" % (now, reference),
        mode=mode, kind="support", conversation_id="conv-1", started_at=now, cluster_id=None, channel="web",
        phone="+919999999999", identity="verified", category="battery_charging", ai_severity="normal",
        summary="Charger LED stays off.", claims={}, bike=None, coverage=None, customer_name=None, created_at=now,
    ))["_id"]


def reload_api(env, client=None):
    """emotorad_ai.api imported again with `env`, with its stores on mongomock when a client is given."""
    real = wiring.build_stores

    def on_mongomock(settings, log=None):
        return real(settings, log=log, client=client)

    with mock.patch.dict(os.environ, env, clear=False):
        import emotorad_ai.api as api

        if client is None:
            return importlib.reload(api)
        with mock.patch.object(wiring, "build_stores", on_mongomock):
            return importlib.reload(api)


class OffTests(unittest.TestCase):
    def test_without_the_refresh_token_zoho_is_off_and_nothing_is_alarmed(self):
        log = EventLog(path=None)
        result = wire(dict(zoho_env(), EMOTORAD_ZOHO_REFRESH_TOKEN=""), mongo_stores(), log=log)
        self.assertEqual((result.status, result.router, result.worker), ("not configured", None, None))
        self.assertEqual([e for e in log.events if e["event"] == "zoho_misconfigured"], [])


class MisconfiguredTests(unittest.TestCase):
    CASES = (
        ("misconfigured: missing EMOTORAD_ZOHO_ORG_ID", {"EMOTORAD_ZOHO_ORG_ID": ""}, {}),
        ("misconfigured: missing EMOTORAD_AI_ENV", {"EMOTORAD_AI_ENV": ""}, {}),
        ("not allowed in this region", {"AWS_REGION": "eu-central-1"}, {}),
        ("misconfigured: store is not mongodb", {}, {"store": "memory"}),
        ("misconfigured: tickets index missing", {}, {"indexed": False}),
        ("misconfigured: live refused: test verification in use",
         {"EMOTORAD_ZOHO_LIVE": "yes", "EMOTORAD_AI_DEV_CODES": "1"}, {}),
        ("misconfigured: live refused: test verification in use", {"EMOTORAD_ZOHO_LIVE": "yes"},
         {"otp_is_mock": True}),
    )

    def test_each_reason_falls_back_to_the_mock_and_records_nothing(self):
        for reason, env_changes, how in self.CASES:
            with self.subTest(reason=reason, how=how):
                memory = how.get("store") == "memory"
                stores = memory_stores() if memory else mongo_stores(indexed=how.get("indexed", True))
                log, calls = EventLog(path=None), []
                result = wire(zoho_env(**env_changes), stores, store_kind="memory" if memory else "mongodb",
                              otp_is_mock=how.get("otp_is_mock", False), log=log, calls=calls)
                self.assertEqual((result.status, result.router, result.worker), (reason, None, None))
                self.assertEqual([e["reason"] for e in log.events if e["event"] == "zoho_misconfigured"], [reason])
                self.assertEqual([e["level"] for e in log.events if e["event"] == "zoho_misconfigured"], ["error"])
                # A signed-in customer's safety report still gets its ticket,
                # from the mock, and nothing reaches the tickets collection.
                registry = build_registry(today=TODAY, ticket_system=result.router)
                runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
                answer = send(runtime, "my battery is swollen")
                self.assertIs(type(registry.tickets), MockTicketSystem)
                self.assertTrue(answer.ticket_id)
                self.assertFalse(is_desk_reference(answer.ticket_id))
                self.assertEqual(runtime.llm.requests, [])
                self.assertTrue(nothing_recorded(stores.tickets))
                self.assertEqual(calls, [])

    def test_a_store_that_cannot_be_read_at_start_keeps_the_mock(self):
        def unreadable():
            raise StoreUnavailable("MongoDB index_information failed (ServerSelectionTimeoutError)")

        log = EventLog(path=None)
        result = build_zoho(zoho_env(), ticket_store=SimpleNamespace(has_unique_source_key=unreadable),
                            store_kind="mongodb", conversations=InMemoryConversationStore(), media_reader=None,
                            log=log, otp_is_mock=False, opener=refusing_opener([]))
        self.assertEqual((result.status, result.router, result.worker),
                         ("misconfigured: store unreachable", None, None))
        self.assertEqual(len([e for e in log.events if e["event"] == "zoho_misconfigured"]), 1)


class OnTests(unittest.TestCase):
    def test_with_everything_in_place_customers_get_desk_and_the_worker_waits(self):
        calls, stores = [], mongo_stores()
        result = wire(zoho_env(), stores, calls=calls)
        self.assertEqual(result.status, "test department")
        self.assertIsInstance(result.router, TicketRouter)
        self.assertIsInstance(result.worker, ZohoWorker)
        self.assertIs(result.store, stores.tickets)
        self.assertIs(result.router.store, stores.tickets)
        self.assertFalse(result.worker.status["running"])
        self.assertEqual(zoho_threads(), [])
        self.assertEqual(calls, [])

    def test_live_needs_real_verification_and_then_says_live(self):
        result = wire(zoho_env(live=True), mongo_stores(), otp_is_mock=False)
        self.assertEqual(result.status, "live")
        self.assertEqual(result.worker.settings.mode, "live")

    def test_zoho_is_never_called_inside_a_turn(self):
        calls, stores = [], mongo_stores()
        result = wire(zoho_env(), stores, calls=calls)
        registry = build_registry(today=TODAY, ticket_system=result.router)
        runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
        answer = send(runtime, "my battery is swollen")
        self.assertTrue(is_desk_reference(answer.ticket_id))
        self.assertIsNotNone(stores.tickets.get(answer.ticket_id))
        self.assertEqual(runtime.llm.requests, [])
        self.assertEqual(calls, [])
        self.assertEqual(zoho_threads(), [])


class HealthFieldTests(unittest.TestCase):
    def test_a_failure_the_worker_met_replaces_the_start_up_status(self):
        result = wire(zoho_env(), mongo_stores())
        self.assertEqual(zoho_status(result), "test department")
        result.worker.status["failing"] = "token refused: invalid_client_secret"
        self.assertEqual(zoho_status(result), "token refused: invalid_client_secret")

    def test_with_zoho_off_the_counts_appear_only_when_records_are_outstanding(self):
        stores = memory_stores()
        off = ZohoWiring(status="not configured", store=stores.tickets)
        self.assertEqual(ticket_health(off, now_iso()), {})
        a_record(stores.tickets)
        report = ticket_health(off, now_iso())
        self.assertEqual((report["tickets_waiting"], report["tickets_stuck"], report["tickets_held"]), (1, 0, 0))
        self.assertEqual(report["zoho_worker"], "off")
        self.assertIn("oldest_due_seconds", report)

    def test_with_zoho_on_the_counts_and_the_worker_are_shown(self):
        result = wire(zoho_env(), mongo_stores())
        report = ticket_health(result, now_iso())
        self.assertEqual(report["tickets_waiting"], 0)
        self.assertEqual(report["zoho_worker"]["running"], False)


class ApiTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        reload_api(dict(BASE, EMOTORAD_STORE="memory", **blank_zoho()))

    def zoho_on_api(self, **changes):
        client = mongomock.MongoClient()
        ensure_indexes(client[Settings().mongo_db])
        return reload_api(dict(BASE, EMOTORAD_STORE="mongodb", **zoho_env(**changes)), client=client)

    def test_importing_and_reloading_the_api_starts_no_worker(self):
        self.zoho_on_api()
        api = self.zoho_on_api()
        self.assertIsInstance(api.ZOHO.worker, ZohoWorker)
        self.assertFalse(api.ZOHO.worker.status["running"])
        self.assertEqual(zoho_threads(), [])
        self.assertIsInstance(api.registry.tickets, TicketRouter)
        report = api.health()
        self.assertEqual(report["zoho"], "test department")
        self.assertEqual(report["tickets_waiting"], 0)

    def test_health_counts_a_waiting_ticket(self):
        api = self.zoho_on_api()
        a_record(api.stores.tickets)
        self.assertEqual(api.health()["tickets_waiting"], 1)

    def test_only_the_lifespan_starts_and_stops_the_worker(self):
        api = self.zoho_on_api()
        seen = []

        async def serve():
            async with api._lifespan(api.app):
                seen.append((api.ZOHO.worker.status["running"], len(zoho_threads())))

        asyncio.run(serve())
        self.assertEqual(seen, [(True, 1)])
        self.assertFalse(api.ZOHO.worker.status["running"])
        self.assertEqual(zoho_threads(), [])

    def test_an_eu_region_keeps_the_mock_and_says_so(self):
        api = self.zoho_on_api(AWS_REGION="eu-central-1")
        self.assertIs(type(api.registry.tickets), MockTicketSystem)
        self.assertIsNone(api.ZOHO.worker)
        self.assertEqual(api.health()["zoho"], "not allowed in this region")
        self.assertTrue(nothing_recorded(api.stores.tickets))


class EntryPointTests(unittest.TestCase):
    """api.py is the only place that chooses Zoho (spec section 9).
    docker/start.py passes its whole environment to Streamlit, so the
    playground sees every Zoho setting too."""

    def test_the_cli_keeps_the_mock_with_every_zoho_setting_present(self):
        built, real = [], cli.build_registry

        def capture(**kwargs):
            registry = real(**kwargs)
            built.append(registry)
            return registry

        with mock.patch.dict(os.environ, zoho_env(), clear=False), \
                mock.patch.object(cli, "build_registry", capture), redirect_stdout(io.StringIO()):
            code = cli.main(["--offline", "--store", "memory", "--channel", "amiigo", "--session", "sess-amiigo-test",
                             "hi"])
        self.assertEqual(code, 0)
        [registry] = built
        self.assertIs(type(registry.tickets), MockTicketSystem)
        self.assertEqual(zoho_threads(), [])

    def test_the_live_evaluation_keeps_the_mock(self):
        with mock.patch.dict(os.environ, zoho_env(), clear=False):
            registry = scenario_registry(scenario(), EVAL_TODAY)
        self.assertIs(type(registry.tickets), MockTicketSystem)

    def test_a_default_registry_is_the_mock_whatever_the_environment(self):
        with mock.patch.dict(os.environ, zoho_env(), clear=False):
            self.assertIs(type(build_registry().tickets), MockTicketSystem)

    def test_no_entry_point_but_the_api_reaches_for_zoho(self):
        reaching = ("build_zoho", "TicketRouter", "DeskTicketSystem", "zoho.wiring", "from .zoho", "tickets.seam")
        package = ROOT / "src" / "emotorad_ai"
        entry_points = [package / "cli.py", package / "playground.py", *sorted((package / "live_eval").glob("*.py")),
                        ROOT / "scripts" / "live_eval.py", ROOT / "docker" / "start.py"]
        for path in entry_points:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual([name for name in reaching if name in text], [])
        self.assertIn("build_zoho", (package / "api.py").read_text(encoding="utf-8"))

    def test_the_local_chat_page_never_passes_a_zoho_setting(self):
        launcher = load_launcher()
        given = dict(zoho_env(), EMOTORAD_ZOHO_SOMETHING_NEW="x")
        env = launcher.server_env(given, mode="offline", store="memory")
        self.assertEqual([name for name in env if name.startswith("EMOTORAD_ZOHO_")], [])
        for name in ZOHO_NAMES:
            self.assertIn(name, launcher.WITHHELD)
        lines = "\n".join(launcher.status_lines(given, "offline", "memory"))
        self.assertIn("never Zoho Desk", lines)
        self.assertNotIn("refresh-test", lines)
        self.assertNotIn("secret-test", lines)


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_api_health.py`, replace this block:

```python
def fresh_api(env):
    with mock.patch.dict(os.environ, env, clear=False):
        import emotorad_ai.api as api

        return importlib.reload(api)


class HealthTests(unittest.TestCase):
    def test_offline_reports_no_secret(self):
        # Both video keys blanked, so the frames fallback is what is reported on
        # any machine, including one with a real key in its environment.
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": "",
                         "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "", "EMOTORAD_AMIGO_PG_DSN": "",
                         "EMOTORAD_AI_BUILD": "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.assertEqual(
            api.health(),
            {"status": "ok", "mode": "offline", "store": "memory", "secrets": "not configured", "media": "not configured",
             "guide_media": "0 of %d sendable" % len(api.GUIDE_MEDIA), "video_summary": "frames", "tracing": "off",
             "amigo": "not configured", "build": "unknown", "ip_location": "not configured",
             "photo_check": "off"},
        )
```

with this block:

```python
def fresh_api(env):
    with mock.patch.dict(os.environ, env, clear=False):
        import emotorad_ai.api as api

        return importlib.reload(api)


# Every Zoho setting (spec 2026-10-05 section 9). Blanked where /health is
# pinned, together with any other EMOTORAD_ZOHO_* name on this machine, so a
# laptop with the staging secret loaded still sees Zoho off here.
ZOHO_NAMES = (
    "EMOTORAD_ZOHO_REFRESH_TOKEN", "EMOTORAD_ZOHO_CLIENT_ID", "EMOTORAD_ZOHO_CLIENT_SECRET",
    "EMOTORAD_ZOHO_ORG_ID", "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID", "EMOTORAD_ZOHO_TEST_CONTACT_ID",
    "EMOTORAD_ZOHO_DEPARTMENT_ID", "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID", "EMOTORAD_ZOHO_LIVE",
    "EMOTORAD_ZOHO_CF_CHAT_REFERENCE", "EMOTORAD_ZOHO_CF_SOURCE", "EMOTORAD_ZOHO_PRIORITY_HIGH",
    "EMOTORAD_ZOHO_PRIORITY_MEDIUM", "EMOTORAD_ZOHO_CHANNEL", "EMOTORAD_ZOHO_CREDITS_FLOOR",
    "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB",
)


def zoho_blank():
    names = set(ZOHO_NAMES) | {name for name in os.environ if name.startswith("EMOTORAD_ZOHO_")}
    return {name: "" for name in names}


class HealthTests(unittest.TestCase):
    def test_offline_reports_no_secret(self):
        # Both video keys blanked, so the frames fallback is what is reported on
        # any machine, including one with a real key in its environment. Zoho
        # blanked for the same reason: off, it is the mock, and no ticket
        # counts appear while nothing is waiting.
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": "",
                              "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "", "EMOTORAD_AMIGO_PG_DSN": "",
                              "EMOTORAD_AI_BUILD": "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"}, **zoho_blank()))
        self.assertEqual(
            api.health(),
            {"status": "ok", "mode": "offline", "store": "memory", "secrets": "not configured", "media": "not configured",
             "guide_media": "0 of %d sendable" % len(api.GUIDE_MEDIA), "video_summary": "frames", "tracing": "off",
             "amigo": "not configured", "build": "unknown", "ip_location": "not configured",
             "photo_check": "off", "zoho": "not configured"},
        )
```

And replace:

```python
    @classmethod
    def tearDownClass(cls):
        fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""})
```

with:

```python
    @classmethod
    def tearDownClass(cls):
        fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""}, **zoho_blank()))
```

In `tests/test_memory.py`, replace the imports:

```python
from emotorad_ai.conversation import ConversationSummaryItem, InMemoryConversationStore
from emotorad_ai.enrichment import summarise_past
from emotorad_ai.llm import say
from emotorad_ai.runtime import Runtime
from tests.store_contract import inbound, reply
from tests.test_runtime_persistence import runtime_on, send
```

with:

```python
from emotorad_ai.conversation import ConversationSummaryItem, InMemoryConversationStore
from emotorad_ai.enrichment import summarise_past
from emotorad_ai.llm import say
from emotorad_ai.runtime import Runtime
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools.mocks import MockTicketSystem, build_registry
from tests.store_contract import inbound, reply
from tests.test_runtime_persistence import TODAY, runtime_on, send
```

Then replace:

```python
    def test_nothing_to_say_is_none(self):
        self.assertIsNone(summarise_past([]))
```

with:

```python
    def test_nothing_to_say_is_none(self):
        self.assertIsNone(summarise_past([]))

    def test_with_real_tickets_an_old_mock_number_is_left_out(self):
        # Spec 2026-10-05 section 3: once Zoho is on, an EM-00012 exists
        # nowhere, so the bot must never quote it. A Desk reference stays.
        text = summarise_past([
            item("b", 20, title="Battery will not charge", outcome="escalated", ticket_id="EM-00012"),
            item("a", 2, title="Motor is making a noise", outcome="escalated", ticket_id="EM-1000001"),
        ], drop_mock_tickets=True)
        self.assertEqual(text, "20 Sep: Battery will not charge, escalated\n"
                               "02 Sep: Motor is making a noise, escalated, ticket EM-1000001")
```

And replace:

```python
    def test_what_the_customer_typed_never_reaches_memory(self):
```

with:

```python
    def test_with_zoho_on_an_old_mock_number_never_reaches_the_model(self):
        store = InMemoryConversationStore()
        earlier = store.get("earlier")
        earlier.user_key = "PHONE#+919876543210"
        store.record_turn(earlier, inbound("hi", cid="earlier"), reply("Ok.", cid="earlier"),
                          item("earlier", 20, title="Battery issue", outcome="escalated", ticket_id="EM-00012"))
        router = TicketRouter(DeskTicketSystem(InMemoryTicketStore(), "test", "stage"), MockTicketSystem())
        runtime = runtime_on(store, [say("Welcome back.")], registry=build_registry(today=TODAY, ticket_system=router))
        send(runtime, "my battery won't charge", cid="second")
        system = runtime.llm.requests[0]["system"]
        self.assertIn("Last contact:", system)
        self.assertIn("escalated", system)
        self.assertNotIn("EM-00012", system)

    def test_what_the_customer_typed_never_reaches_memory(self):
```

- [ ] **Step 2: Run them to see them fail**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_wiring tests.test_api_health tests.test_memory
```

Expected:
- `tests.test_zoho_wiring`: one import error, `ModuleNotFoundError: No module named 'emotorad_ai.zoho.wiring'`.
- `HealthTests.test_offline_reports_no_secret`: fails, because the dict has no `"zoho"`.
- The two new `tests.test_memory` tests: `TypeError: summarise_past() got an unexpected keyword argument 'drop_mock_tickets'`, then an `AssertionError` that `EM-00012` is in the system prompt.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/zoho/wiring.py`:

```python
"""Zoho Desk on or off, decided once at start-up (spec 2026-10-05, sections 1
and 9).

Only api.py calls `build_zoho`. The CLI, the playground and the live
evaluation build their registries with the mock and never come here. So a
laptop with every Zoho setting in its environment still sends nothing.

Zoho is off while EMOTORAD_ZOHO_REFRESH_TOKEN is unset. With it set, the
start-up checks run in order (settings.startup_problem). If any check fails,
the mock stays, exactly as when Zoho is off: nothing is recorded in `tickets`,
/health says why, and `zoho_misconfigured` is logged at error level for the
alarm. There is no state in which tickets are recorded but cannot be sent
safely.

The worker is built here. It is started only by api.py's lifespan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from ..conversation import StoreUnavailable
from ..tickets.seam import DeskTicketSystem, TicketRouter
from ..tools.mocks import MockTicketSystem
from .auth import TokenSource
from .desk import DeskClient
from .http import DeskHTTP
from .settings import load_zoho_settings, startup_problem
from .worker import ZohoWorker

NOT_CONFIGURED = "not configured"
STORE_UNREACHABLE = "misconfigured: store unreachable"


@dataclass
class ZohoWiring:
    # The /health "zoho" value as start-up left it. zoho_status adds what the
    # worker has met since.
    status: str
    # What the registry holds when Zoho is on. None means the mock.
    router: Optional[TicketRouter] = None
    worker: Optional[ZohoWorker] = None
    # The tickets store, whether Zoho is on or off, so /health still counts
    # what a rollback left waiting.
    store: Optional[Any] = None


def build_zoho(
    env: Mapping[str, str],
    *,
    ticket_store: Any,
    store_kind: str,
    conversations: Any,
    media_reader: Any,
    log: Any,
    otp_is_mock: bool,
    opener: Optional[Callable[..., Any]] = None,
) -> ZohoWiring:
    settings, status = load_zoho_settings(env)
    if settings is None:
        if status != NOT_CONFIGURED:
            _misconfigured(log, status)
        return ZohoWiring(status=status, store=ticket_store)
    try:
        problem = startup_problem(
            settings,
            region=env.get("AWS_REGION") or "",
            store_kind=store_kind,
            ticket_store=ticket_store,
            dev_codes=env.get("EMOTORAD_AI_DEV_CODES") == "1",
            otp_is_mock=otp_is_mock,
        )
    except StoreUnavailable:
        # The index list could not be read. Starting on the mock shows on
        # /health and raises the alarm. Failing the import would take the
        # chat down.
        problem = STORE_UNREACHABLE
    if problem is not None:
        _misconfigured(log, problem)
        return ZohoWiring(status=problem, store=ticket_store)
    http = DeskHTTP(opener) if opener is not None else DeskHTTP()
    client = DeskClient(settings, TokenSource(settings, http), http)
    worker = ZohoWorker(ticket_store, client, conversations, media_reader, settings, log)
    # The end of the turn wakes the worker, so Zoho is first called after the reply.
    desk = DeskTicketSystem(ticket_store, settings.mode, settings.environment, wake=worker.wake)
    return ZohoWiring(
        status="live" if settings.live else "test department",
        router=TicketRouter(desk, MockTicketSystem()),
        worker=worker,
        store=ticket_store,
    )


def _misconfigured(log: Any, reason: str) -> None:
    # A reason holds setting names, never their values.
    if log is not None:
        log.emit("zoho_misconfigured", "zoho", level="error", reason=reason)


def zoho_status(wiring: ZohoWiring) -> str:
    """The /health "zoho" value: the start-up status, or what the worker has
    met since ("token refused: ...", "sending failing: ...")."""
    failing = wiring.worker.status.get("failing") if wiring.worker is not None else None
    return failing or wiring.status


def ticket_health(wiring: ZohoWiring, now: str) -> Dict[str, Any]:
    """The /health ticket fields. Shown while Zoho is on, and whenever
    records are waiting, stuck or held, so that after a rollback the
    customers who were told someone would be in touch can still be counted."""
    if wiring.store is None:
        return {}
    # With Zoho off there is no mode. Records are counted as for test, so a
    # live one shows as held: counted either way.
    mode = wiring.worker.settings.mode if wiring.worker is not None else "test"
    try:
        counts = wiring.store.counts(mode, now)
    except StoreUnavailable:
        return {"tickets": "store unavailable"}
    outstanding = sum(int(counts.get(name) or 0) for name in ("waiting", "stuck", "held"))
    if wiring.worker is None and not outstanding:
        return {}
    return {
        "tickets_waiting": counts.get("waiting", 0),
        "tickets_stuck": counts.get("stuck", 0),
        "tickets_held": counts.get("held", 0),
        "zoho_worker": dict(wiring.worker.status) if wiring.worker is not None else "off",
        "oldest_due_seconds": counts.get("oldest_due_seconds"),
    }
```

In `src/emotorad_ai/api.py`, replace:

```python
from .storage.uploads import UploadError, UploadRegistry
from .tools import amigo as amigo_tools
```

with:

```python
from .storage.uploads import UploadError, UploadRegistry
from .tickets.clock import now_iso
from .tools import amigo as amigo_tools
```

Replace:

```python
from .wiring import build_models, build_stores
```

with:

```python
from .wiring import build_models, build_stores
from .zoho.wiring import build_zoho, ticket_health, zoho_status
```

Replace:

```python
# Amigo, read-only (tools/amigo.py), when EMOTORAD_AMIGO_PG_DSN is set; the
# config store exports it from the staging secret. None: behaviour as before.
AMIGO = amigo_tools.from_env()
```

with:

```python
# Amigo, read-only (tools/amigo.py), when EMOTORAD_AMIGO_PG_DSN is set; the
# config store exports it from the staging secret. None: behaviour as before.
AMIGO = amigo_tools.from_env()

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
```

Replace:

```python
    Ticketing stays mocked either way. That combination is worth knowing about:
    real bike details followed by a ticket number that exists nowhere is more
    convincing, and therefore worse, than fixtures all the way through.
    """
```

with:

```python
    Tickets do not follow this switch. With Zoho on (ZOHO, above), a
    customer's ticket is recorded for Zoho Desk and a dealer's stays on the
    mock. With Zoho off, every ticket is the mock's, and its number exists
    nowhere. That combination is worth knowing about: real bike details
    followed by a ticket number that exists nowhere is more convincing, and
    therefore worse, than fixtures all the way through.
    """
```

Replace (the first `build_registry` call, inside the `if`):

```python
            location_sharing=True,
            idempotency=stores.idempotency,
        )
    client = OMSClient()
```

with:

```python
            location_sharing=True,
            idempotency=stores.idempotency,
            # None while Zoho is off: build_registry then makes the mock.
            ticket_system=ZOHO.router,
        )
    client = OMSClient()
```

Replace (the second call):

```python
        location_sharing=True,
        idempotency=stores.idempotency,
    )


registry = _build_registry()
```

with:

```python
        location_sharing=True,
        idempotency=stores.idempotency,
        ticket_system=ZOHO.router,
    )


registry = _build_registry()
```

Replace:

```python
@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # The SDK batches in a background thread; a container stopped mid-batch
    # would otherwise lose the last turns of every open conversation.
    if TRACING is not None:
        TRACING.flush()
```

with:

```python
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
```

Replace:

```python
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
```

with:

```python
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
        "build": BUILD,
        "ip_location": IP_LOCATOR.db if IP_LOCATOR is not None else "not configured",
        # Zoho Desk: on, off, or why not (zoho/wiring.py).
        "zoho": zoho_status(ZOHO),
    }
    # Tickets waiting, stuck and held, and the worker's state. Shown while
    # Zoho is on, or while any record is outstanding.
    report.update(ticket_health(ZOHO, now_iso()))
    return report
```

In `src/emotorad_ai/enrichment.py`, replace:

```python
def summarise_past(summaries: Sequence[Any]) -> Optional[str]:
    """Earlier conversations, one line each, newest first, at most three.

    Built only from fields code wrote (a record title or a fixed label, the
    bike, the outcome, the ticket), never from what the customer typed, so a
    past message cannot steer a future prompt.
    """
    lines: List[str] = []
```

with:

```python
# An old mock ticket number (tools/mocks.py, "EM-%05d"). Desk references have
# seven digits from EM-1000001, so they never match this.
_MOCK_TICKET = re.compile(r"EM-\d{5}")


def summarise_past(summaries: Sequence[Any], drop_mock_tickets: bool = False) -> Optional[str]:
    """Earlier conversations, one line each, newest first, at most three.

    Built only from fields code wrote (a record title or a fixed label, the
    bike, the outcome, the ticket), never from what the customer typed, so a
    past message cannot steer a future prompt.

    With `drop_mock_tickets` (Zoho on, spec 2026-10-05 section 3), an old mock
    number is left out: it exists nowhere, and the bot must never quote it.
    """
    lines: List[str] = []
```

And replace:

```python
        if item.ticket_id:
            line += ", ticket %s" % item.ticket_id
```

with:

```python
        if item.ticket_id and not (drop_mock_tickets and _MOCK_TICKET.fullmatch(item.ticket_id)):
            line += ", ticket %s" % item.ticket_id
```

In `src/emotorad_ai/runtime.py`, replace:

```python
            if state.user_key:
                try:
                    last_contact = summarise_past(self.conversations.recent_summaries(
                        state.user_key, limit=3, exclude=summary_key(state.conversation_id, state.started_at)))
```

with:

```python
            if state.user_key:
                try:
                    last_contact = summarise_past(
                        self.conversations.recent_summaries(
                            state.user_key, limit=3, exclude=summary_key(state.conversation_id, state.started_at)),
                        # With Zoho on, an old mock number exists nowhere,
                        # so it never reaches the model (spec 2026-10-05).
                        drop_mock_tickets=self._records_real_tickets(),
                    )
```

And replace:

```python
        if resolved.persona == "customer" and identity.may_disclose and identity.phone:
            return "PHONE#" + identity.phone
        return None
```

with:

```python
        if resolved.persona == "customer" and identity.may_disclose and identity.phone:
            return "PHONE#" + identity.phone
        return None

    def _records_real_tickets(self) -> bool:
        """Whether customer tickets go to Zoho Desk (api.py wired a
        TicketRouter) rather than the mock. `is True`, so a test double whose
        attributes are all truthy never counts."""
        tickets = getattr(self.registry, "tickets", None)
        return getattr(tickets, "records_real_tickets", False) is True
```

In `scripts/chat_local.py`, replace:

```python
# Never passed to the server. With it, a phone number typed on the page reaches
# the live purchase table (api._build_registry), and those details would go to
# OpenRouter with the rest of the conversation.
WITHHELD = ("EMOTORAD_OMS_API_KEY",)
```

with:

```python
# Never passed to the server. With the OMS key, a phone number typed on the
# page reaches the live purchase table (api._build_registry), and those
# details would go to OpenRouter with the rest of the conversation. With the
# Zoho settings, a test chat on this machine would become a Zoho Desk ticket
# (spec 2026-10-05 section 9). This page always keeps the mock tickets.
ZOHO_SETTINGS = (
    "EMOTORAD_ZOHO_REFRESH_TOKEN", "EMOTORAD_ZOHO_CLIENT_ID", "EMOTORAD_ZOHO_CLIENT_SECRET",
    "EMOTORAD_ZOHO_ORG_ID", "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID", "EMOTORAD_ZOHO_TEST_CONTACT_ID",
    "EMOTORAD_ZOHO_DEPARTMENT_ID", "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID", "EMOTORAD_ZOHO_LIVE",
    "EMOTORAD_ZOHO_CF_CHAT_REFERENCE", "EMOTORAD_ZOHO_CF_SOURCE", "EMOTORAD_ZOHO_PRIORITY_HIGH",
    "EMOTORAD_ZOHO_PRIORITY_MEDIUM", "EMOTORAD_ZOHO_CHANNEL", "EMOTORAD_ZOHO_CREDITS_FLOOR",
    "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB",
)
WITHHELD = ("EMOTORAD_OMS_API_KEY",) + ZOHO_SETTINGS
# Any other Zoho name added later is withheld too.
WITHHELD_PREFIX = "EMOTORAD_ZOHO_"
```

Replace:

```python
    ignored = " (EMOTORAD_OMS_API_KEY is set and is ignored here)" if _is_set(environ, "EMOTORAD_OMS_API_KEY") else ""
    lines.append("business tools: fixtures, never the live OMS" + ignored)
    return lines
```

with:

```python
    ignored = " (EMOTORAD_OMS_API_KEY is set and is ignored here)" if _is_set(environ, "EMOTORAD_OMS_API_KEY") else ""
    lines.append("business tools: fixtures, never the live OMS" + ignored)
    zoho_set = any(name.startswith(WITHHELD_PREFIX) and _is_set(environ, name) for name in environ)
    lines.append("tickets: the mock, never Zoho Desk"
                 + (" (EMOTORAD_ZOHO_* settings are set and are ignored here)" if zoho_set else ""))
    return lines
```

Replace:

```python
    env = {name: value for name, value in environ.items() if name not in WITHHELD}
```

with:

```python
    env = {name: value for name, value in environ.items()
           if name not in WITHHELD and not name.startswith(WITHHELD_PREFIX)}
```

- [ ] **Step 4: Run the modules, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_wiring tests.test_api_health tests.test_memory tests.test_chat_page_new_bot
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: all four modules pass. In the suite, the only failure is the known environmental one (`tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`), and the count is the count after Task 9 plus the new tests. Existing tests that change on purpose:
- `tests.test_api_health.HealthTests.test_offline_reports_no_secret`: the pinned dict gains `"zoho": "not configured"`, and its environment blanks every `EMOTORAD_ZOHO_*` name.
- `tests.test_api_health.HealthTests.tearDownClass`: it also blanks the Zoho names.

No other existing test changes. With Zoho off, the registry is the mock exactly as before, and with no records `/health` gains only `"zoho"`.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/zoho/wiring.py src/emotorad_ai/api.py src/emotorad_ai/enrichment.py src/emotorad_ai/runtime.py scripts/chat_local.py tests/test_zoho_wiring.py tests/test_api_health.py tests/test_memory.py
git commit -m "feat: wire Zoho Desk into the API only, with health and the lifespan" -m "build_zoho decides once at import: off without the refresh token, and the mock with a logged reason on any failed start-up check. With Zoho on, the registry holds the TicketRouter and the worker starts and stops with the lifespan, never at import. /health gains zoho and the ticket counts. The CLI, live evaluation, playground and local chat page keep the mock. Old five-digit mock numbers no longer reach the model while Zoho is on." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Interface issues for the plan author

1. **`tests/fake_zoho.py` has no API in the shared interfaces.** It is an opener-level double, and its methods and fixtures are Task 7's to choose. Task 9's tests therefore use a double defined in `tests/test_zoho_worker.py` (`FakeDesk`), which has exactly the `DeskClient` methods from the shared interfaces. This tests the worker's logic without guessing the internals of `fake_zoho`. Once Task 7 settles `fake_zoho`, the plan author may want one more end-to-end test that runs the real `DeskClient` over it.
2. **The constructors in `zoho/errors.py` are not fixed.** The tests build errors with `zoho_error(cls, error, **attributes)`, which skips the constructor. The worker reads only `.error`, `.fields` (with a `getattr` default) and `.retry_after_seconds` (with a `getattr` default).
3. **`ZohoWorker.status` has a third key, `"failing"`.** It holds `"token refused: <error>"` or `"sending failing: <code>"`, and is cleared on the next send. Spec section 9 needs these on `/health`, and `ZohoWiring` has nowhere else to carry them. `zoho_status(wiring)` reads it.
4. **The worker writes the whole `zoho` sub-document on every save.** It does this as `changes={"zoho": {...}}`. It uses no dotted paths (`"zoho.contact_id"`) and no `push`, so `InMemoryTicketStore.save` only needs top-level `$set` and `add_to_set` on top-level lists. This is safe because only the lease holder writes `zoho`. `save` must also accept `lease_token: None` among the `changes`.
5. **`check_overdue` marks a record stuck through the existing `save`.** It calls `save(reference, record["lease_token"], {"state": "stuck"})`, because the protocol has no other write that works without a lease. The store must match a record whose `lease_token` is `None` when the token passed is `None`. Mongo does this already, and the memory store will if it compares `doc.get("lease_token") == token`.
6. **`counts()` has no total.** So "whenever the tickets store holds records" is approximated: the ticket fields are shown while Zoho is on, or while `waiting + stuck + held > 0`. With Zoho off, a store holding only sent or gone records shows nothing. With Zoho off, `counts` is called with mode `"test"`, so any live records show as held. They are still counted.
7. **What Task 10's tests assume of `load_zoho_settings`:**
   - A blank value counts as unset. The tests blank names with `""`, as the repo's tests do.
   - `EMOTORAD_ZOHO_LIVE` values other than `yes` (here `"no"`) mean test mode, not an error.
   - A missing `EMOTORAD_AI_ENV` gives exactly `"misconfigured: missing EMOTORAD_AI_ENV"`.
   - With one name missing, the text is `"misconfigured: missing <NAME>"`.
8. **`startup_problem` can raise `StoreUnavailable`.** `has_unique_source_key()` raises it when Atlas cannot be reached at start-up. `build_zoho` catches it and uses the new reason `"misconfigured: store unreachable"` (falling back to the mock and raising the alarm), so the import never fails. If Task 6 catches this itself, `test_a_store_that_cannot_be_read_at_start_keeps_the_mock` needs Task 6's reason text.
9. **An unknown outcome is resolved on the next pass, not the same one.** The intent stays. The retry schedule runs again 30 seconds later, and that pass checks Zoho (contact tickets, comments or attachments) before writing again. A crash is handled the same way. If part 1 shows that the contact's ticket list lags, the 120, 300 and 600 second windows still need their own rule: today a pass that finds nothing creates the ticket.
10. **Events not in spec section 8:** `zoho_ticket_gone`, `zoho_ticket_adopted` and `zoho_record_dropped` (each with a reference only). `EventLog` has no level, so "error level" is sent as a `level="error"` field on `zoho_misconfigured`, `zoho_token_refused`, `zoho_worker_error`, `zoho_ticket_stuck` and `safety_ticket_late`. Task 12's metric filter should match on event names. Overdue events use the conversation id `"zoho"`, so they carry only the reference and the age.
11. **`DeskClient` is assumed to expose `.http`.** This follows the brief's `client.http.last_credits_remaining`. The worker reads it with nested `getattr`, so a missing attribute means the credits floor is never applied, rather than a crash.
12. **`find_adoptable` is assumed to read `ticket["cf"][cf_api_name]`.** That is the shape `ticket_payload` sends. `FakeDesk.contact_tickets` returns the created payloads. If Task 7 reads another shape (Zoho's list answer may nest custom fields differently), the fake must return that shape.
13. **Run-window times are parsed, not compared as text.** Turn `at` values and media `stored_at` come from `conversation.utc_now_iso`, which drops the microseconds when they are zero. Compared as strings against `now_iso()` output, a whole-second time sorts as earlier. The global rule that store times are "compared as strings" holds only for the ticket store's own fields. A test covers this.
14. **The worker has no `media_reader` without a bucket.** In that case it skips attachments, because no media record can exist then. An S3 read failure (`StorageError`) goes on the retry schedule. A key hidden by erasure would keep retrying until erasure covers `tickets`, which is deferred.
15. **`ZohoWorker.stop()` waits at most 10 seconds.** An upload in flight, with its 60 second timeout, can outlive the wait. The thread is a daemon, so it does not hold up the process.
16. **Task 10's "never called inside a turn" and safety tests depend on earlier tasks.** They need Task 5's persona on the safety branch's `ToolContext` and Task 3's router sending `persona="customer"` to Desk. The tests also rely on `Stores.tickets` existing for both stores (Task 2) and on `ensure_indexes` creating the unique `source_key` index on mongomock.
17. **The Global Constraints suite command misses five names.** It does not unset `EMOTORAD_ZOHO_PRIORITY_HIGH`, `_PRIORITY_MEDIUM`, `_CHANNEL`, `_CREDITS_FLOOR` or `_ATTACHMENT_LIMIT_MB`. This is harmless while the refresh token is unset, but spec section 9 says every name. The tests here blank every `EMOTORAD_ZOHO_*` name themselves wherever it matters.

---

<!-- drafted as tasks-11-12 -->

### Task 11: Redaction of glued, Devanagari-digit and foreign numbers, and the OAuth field names

**Files:**
- Create: `src/emotorad_ai/digits.py`
- Modify: `src/emotorad_ai/verify_first.py:20-31` (remove `import unicodedata`, import `ascii_digits` from `digits`) and `:156-161` (remove the function body; the imported name is re-exported)
- Modify: `src/emotorad_ai/observability.py:16-18` (import `ascii_digits`), `:20-29` (comment), `:36-37` (three new patterns after `_LONG_DIGITS`), `:47-52` (`_SENSITIVE_KEYS`), `:64-84` (`redact_pii`)
- Test: `tests/test_digits.py` (new), `tests/test_log_redaction.py` (new methods and classes)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `emotorad_ai.digits.ascii_digits(text: str) -> str`. `emotorad_ai.verify_first.ascii_digits` is the same object. Tasks 13 to 15 import it from `emotorad_ai.digits`.
  - `emotorad_ai.observability.redact_pii(text: str) -> str` now also hides Devanagari-digit numbers, a mobile glued to Latin or Devanagari letters, and `+<country code>` numbers.
  - `_SENSITIVE_KEYS` now includes `access_token`, `refresh_token`, `client_secret` and `authorization` (any case), so `redact_fields` and `EventLog.emit` replace them with `[redacted]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_digits.py`:

```python
"""Digits of any script read as ASCII (digits.py, spec 2026-10-05, section 8).

Moved out of verify_first.py so observability.py can use it: verify_first
imports observability, so the other way round would be an import cycle.
"""

import ast
import unittest
from pathlib import Path

from emotorad_ai import digits, verify_first
from emotorad_ai.digits import ascii_digits


class AsciiDigitsTests(unittest.TestCase):
    def test_devanagari_digits_become_ascii(self):
        self.assertEqual(ascii_digits("९८७६५४३२१०"), "9876543210")

    def test_digits_of_other_scripts_too(self):
        # Bengali and Tamil, as a keyboard in either script types them.
        self.assertEqual(ascii_digits("৯৮৭"), "987")
        self.assertEqual(ascii_digits("௯௮௭"), "987")

    def test_everything_else_is_left_as_typed(self):
        text = "मेरा नंबर 98765 है, battery ½ charged²"
        self.assertEqual(ascii_digits(text), text)

    def test_verify_first_still_offers_the_same_function(self):
        self.assertIs(verify_first.ascii_digits, ascii_digits)

    def test_the_module_imports_nothing_from_the_package(self):
        tree = ast.parse(Path(digits.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "digits.py must not import from the package")
                self.assertFalse((node.module or "").startswith("emotorad_ai"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("emotorad_ai"))


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_log_redaction.py`, replace this block:

```python
import unittest

from emotorad_ai.observability import EventLog, redact_pii
```

with this block:

```python
import json
import unittest

from emotorad_ai.observability import EventLog, redact_fields, redact_pii
```

Replace this block:

```python
    def test_a_number_after_a_label_still_goes(self):
        self.assertEqual(redact_pii("mobile: 9876543210, call me"), "mobile: [phone], call me")
        self.assertEqual(redact_pii("reach me on +91 98765-43210."), "reach me on [phone].")
```

with this block:

```python
    def test_a_number_after_a_label_still_goes(self):
        self.assertEqual(redact_pii("mobile: 9876543210, call me"), "mobile: [phone], call me")
        self.assertEqual(redact_pii("reach me on +91 98765-43210."), "reach me on [phone].")

    def test_an_id_that_starts_with_ten_digits_and_letters_is_left_alone(self):
        """Spec 2026-10-05, section 8. The glued-number rule must not reach
        into an identifier. In a key the digits follow a slash or an
        underscore. In a hex id the letters are a to f only, or they run on
        into more digits, a dash or a file extension."""
        for text in (
            "customers/c1/images/9876543210abcd.jpg",
            "upl_9876543210qx",
            "9876543210abcdef",
            "9876543210ab12cd",
            "trace 9876543210abc-4cd2-8a28 saved",
        ):
            self.assertEqual(redact_pii(text), text)

    def test_a_hex_id_in_a_logged_field_is_left_alone(self):
        event = EventLog(path=None).emit("tool_call", "c1", result={"data": {"id": "9876543210abcdef"}})
        self.assertEqual(event["result"]["data"]["id"], "9876543210abcdef")
```

Replace this block, which ends the file:

```python
if __name__ == "__main__":
    unittest.main()
```

with this block:

```python
class GluedNumbersTests(unittest.TestCase):
    """A number typed straight against a word (spec 2026-10-05, section 8).
    Transcripts now go to Zoho, so whatever the log misses, a third party keeps."""

    def test_english_letters_after_the_number(self):
        self.assertEqual(redact_pii("9876543210pls"), "[phone]pls")
        self.assertEqual(redact_pii("call me on 9876543210asap."), "call me on [phone]asap.")
        self.assertEqual(redact_pii("ring 98765 43210please"), "ring [phone]please")
        self.assertEqual(redact_pii("+919876543210pls"), "[phone]pls")

    def test_hinglish_letters_after_the_number(self):
        self.assertEqual(redact_pii("mera number 9876543210hai"), "mera number [phone]hai")
        self.assertEqual(redact_pii("isi 9876543210pe call karo"), "isi [phone]pe call karo")

    def test_devanagari_after_the_number(self):
        self.assertEqual(redact_pii("नंबर 9876543210पर कॉल करें"), "नंबर [phone]पर कॉल करें")

    def test_devanagari_before_the_number(self):
        self.assertEqual(redact_pii("नंबर9876543210 है"), "नंबर[phone] है")


class DevanagariDigitsTests(unittest.TestCase):
    """Digits typed on a Hindi keyboard are the same digits (digits.ascii_digits)."""

    def test_a_number_in_devanagari_digits_goes(self):
        self.assertEqual(redact_pii("मेरा नंबर ९८७६५४३२१० है"), "मेरा नंबर [phone] है")

    def test_hinglish_with_devanagari_digits_in_groups(self):
        self.assertEqual(redact_pii("mera number ९८७६५ ४३२१० hai"), "mera number [phone] hai")

    def test_devanagari_digits_glued_to_devanagari(self):
        self.assertEqual(redact_pii("मेरा नंबर ९८७६५४३२१०पर"), "मेरा नंबर [phone]पर")

    def test_a_code_in_devanagari_digits_is_still_hidden(self):
        self.assertEqual(redact_pii("०३९७६०"), "[6 digits]")


class OtherCountriesNumbersTests(unittest.TestCase):
    """A +<country code> number (spec 2026-10-05, section 8): customers in Spain
    write to the same chat."""

    def test_a_spanish_number_goes(self):
        self.assertEqual(redact_pii("llámame al +34 612 345 678"), "llámame al [phone]")
        self.assertEqual(redact_pii("+34612345678"), "[phone]")

    def test_other_groupings_go(self):
        self.assertEqual(redact_pii("UK office +44 20 7946 0958."), "UK office [phone].")
        self.assertEqual(redact_pii("call +1 (415) 555-0100 now"), "call [phone] now")

    def test_an_indian_number_is_still_one_phone(self):
        self.assertEqual(redact_pii("reach me on +91 98765-43210."), "reach me on [phone].")

    def test_a_plus_sign_on_a_short_figure_is_left_alone(self):
        self.assertEqual(redact_pii("range +15 km after the update"), "range +15 km after the update")


class SecretFieldsTests(unittest.TestCase):
    """Zoho's OAuth values are removed by name wherever they appear in an event
    (spec 2026-10-05, section 8). They are secret because of what they are,
    not what they look like."""

    def test_each_oauth_field_is_redacted(self):
        event = EventLog(path=None).emit(
            "zoho_token_refused", "-",
            access_token="1000.aaaa.bbbb", refresh_token="1000.cccc.dddd",
            client_secret="test-client-secret", error="invalid_client_secret",
        )
        for name in ("access_token", "refresh_token", "client_secret"):
            self.assertEqual(event[name], "[redacted]")
        self.assertEqual(event["error"], "invalid_client_secret")
        self.assertNotIn("1000.", json.dumps(event))
        self.assertNotIn("test-client-secret", json.dumps(event))

    def test_an_authorization_header_is_redacted_whatever_its_case(self):
        self.assertEqual(
            redact_fields({"headers": {"Authorization": "Zoho-oauthtoken 1000.aaaa.bbbb", "orgId": "60001234567"}}),
            {"headers": {"Authorization": "[redacted]", "orgId": "60001234567"}},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_digits tests.test_log_redaction
```

Expected results:
- `tests.test_digits` fails to load with `ImportError: cannot import name 'digits' from 'emotorad_ai'`.
- In `tests.test_log_redaction`, the `GluedNumbersTests`, `DevanagariDigitsTests` (except the code case), `OtherCountriesNumbersTests` (except the `+91` and `+15 km` cases) and `SecretFieldsTests` fail with `AssertionError`, for example `'9876543210pls' != '[phone]pls'` and `'1000.aaaa.bbbb' != '[redacted]'`.
- These already pass and guard what must not change: the new identifier tests, `test_a_code_in_devanagari_digits_is_still_hidden`, `test_an_indian_number_is_still_one_phone` and `test_a_plus_sign_on_a_short_figure_is_left_alone`.

- [ ] **Step 3: Implement**

Create `src/emotorad_ai/digits.py`:

```python
"""Digits of any script, read as ASCII.

A customer on a Hindi keyboard types ९८७६५४३२१० for 9876543210. Whatever reads
a number (the verify-first step, the log's redaction) runs this first, so a
number is the same number whatever script it was typed in.

Nothing here imports from the package. observability.py uses it, and
verify_first.py imports observability, so it could live in neither without an
import cycle.
"""

from __future__ import annotations

import unicodedata


def ascii_digits(text: str) -> str:
    """Digits of any script (Devanagari ९७००…) as ASCII, everything else as typed."""
    return "".join(
        str(unicodedata.decimal(ch)) if not ch.isascii() and unicodedata.decimal(ch, None) is not None else ch
        for ch in text
    )
```

In `src/emotorad_ai/verify_first.py`, replace this block:

```python
import re
import unicodedata
from dataclasses import dataclass, replace
```

with this block:

```python
import re
from dataclasses import dataclass, replace
```

Replace this block:

```python
from .contract import InboundMessage
from .conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ConversationState, utc_now_iso
from .identity import IdentityResolver, ResolvedIdentity
```

with this block:

```python
from .contract import InboundMessage
from .conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ConversationState, utc_now_iso
# ascii_digits lives in digits.py so observability.py can use it without an
# import cycle. It is imported here by name, so `verify_first.ascii_digits`
# still works for anything that reads it from this module.
from .digits import ascii_digits
from .identity import IdentityResolver, ResolvedIdentity
```

Replace this block:

```python
def ascii_digits(text: str) -> str:
    """Digits of any script (Devanagari ९७००…) as ASCII, everything else as typed."""
    return "".join(
        str(unicodedata.decimal(ch)) if not ch.isascii() and unicodedata.decimal(ch, None) is not None else ch
        for ch in text
    )


# -- the step ----------------------------------------------------------------
```

with this block:

```python
# -- the step ----------------------------------------------------------------
```

In `src/emotorad_ai/observability.py`, replace this block:

```python
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# `\b` cannot match before a "+", so the leading sign was left behind and the
```

with this block:

```python
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .digits import ascii_digits

# `\b` cannot match before a "+", so the leading sign was left behind and the
```

Replace this block:

```python
# the transcript must hide them too. A letter on either side means the digits
# sit inside an identifier (a UUID in an S3 key, a hex id), not a phone: the
# deploy gate once failed on a customer id logged as "b0c[phone]cd2-...".
_PHONE = re.compile(r"(?<![\w+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\w)")
```

with this block:

```python
# the transcript must hide them too. A letter on either side means the digits
# sit inside an identifier (a UUID in an S3 key, a hex id), not a phone: the
# deploy gate once failed on a customer id logged as "b0c[phone]cd2-...".
# Two narrower patterns below take back the cases that are plainly a phone:
# a word typed straight after the number, and Devanagari on either side.
_PHONE = re.compile(r"(?<![\w+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\w)")
```

Replace this block:

```python
# 16-digit-ish sequences: card numbers pasted into a support chat.
_LONG_DIGITS = re.compile(r"\b\d{12,19}\b")
```

with this block:

```python
# 16-digit-ish sequences: card numbers pasted into a support chat.
_LONG_DIGITS = re.compile(r"\b\d{12,19}\b")

# Devanagari on either side of a mobile (spec 2026-10-05, section 8). Hindi
# puts a postposition straight after a number ("9876543210पर"), and a
# Devanagari letter is a word character to `\w`, so _PHONE sees no boundary
# there. Devanagari never appears in an S3 key or a hex id, so here the glue
# cannot mean an identifier.
_PHONE_BY_DEVANAGARI = re.compile(
    r"(?<=[ऀ-ॿ])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"
    r"|(?<![\d+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?=[ऀ-ॿ])"
)
# A mobile with Latin letters typed straight after it ("9876543210pls",
# "9876543210hai"). Hidden only when the digits start a word and the letters
# end it: a space, a bracket, a quote or a comma (or nothing) before the
# digits, and a space, sentence punctuation or the end after the letters. An
# identifier fails one test or the other. In an S3 key or a hex id the digits
# follow a slash, a dash, an underscore or a letter ("run-a9876543210f"), or
# the letters run on into more digits, a dash, or a dot and an extension
# ("9876543210ab12cd", "9876543210abcd.jpg"). The letters must also include
# one past "f", so a hex id of ten digits then a to f ("9876543210abcdef")
# stays. A rare word made only of a to f ("9876543210bad") stays with it.
_PHONE_BEFORE_LETTERS = re.compile(
    r"""(?<![^\s(\["',;])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}"""
    r"""(?=[A-Za-z]*[g-zG-Z][A-Za-z]*(?:[\s,;:!?)\]"']|\.(?!\w)|$))"""
)
# A number with another country's code ("+34 612 345 678", "+44 20 7946
# 0958", "+1 (415) 555-0100"): a plus sign and eight to fifteen digits, the
# lengths E.164 allows, with spaces, dashes or brackets between them.
# Customers in Spain write to the same chat, and the transcript now goes to Zoho.
_INTL_PHONE = re.compile(r"(?<![\w+])\+\d(?:[\s\-()]{0,2}\d){7,14}(?!\d)")
```

Replace this block:

```python
_SENSITIVE_KEYS = frozenset({"code", "otp", "phone", "mobile", "stated_contact"})
```

with this block:

```python
# Zoho's OAuth values join them (spec 2026-10-05, section 8). A token or a
# secret logged once stays a credential in CloudWatch for as long as the log
# is kept. `authorization` is the header that carries the access token.
_SENSITIVE_KEYS = frozenset({
    "code", "otp", "phone", "mobile", "stated_contact",
    "access_token", "refresh_token", "client_secret", "authorization",
})
```

Replace this block:

```python
    Ownership data already reaches us through identity resolution, so nothing
    downstream needs these to be readable in the log.
    """
    if _OTP_ALONE.match(text):
```

with this block:

```python
    Ownership data already reaches us through identity resolution, so nothing
    downstream needs these to be readable in the log.

    Digits of any script are read as ASCII first (digits.ascii_digits), so a
    number typed on a Hindi keyboard is hidden like any other. The text that
    comes back has ASCII digits where the customer typed Devanagari ones.
    """
    text = ascii_digits(text)
    if _OTP_ALONE.match(text):
```

Replace this block:

```python
    text = _PHONE.sub("[phone]", text)
    text = LOOSE_PHONE.sub(_phone_or_as_typed, text)
    text = _LONG_DIGITS.sub("[number]", text)
    return text
```

with this block:

```python
    text = _PHONE.sub("[phone]", text)
    text = _PHONE_BY_DEVANAGARI.sub("[phone]", text)
    text = _PHONE_BEFORE_LETTERS.sub("[phone]", text)
    text = LOOSE_PHONE.sub(_phone_or_as_typed, text)
    # Last of the phone patterns: by now an Indian number is already [phone].
    text = _INTL_PHONE.sub("[phone]", text)
    text = _LONG_DIGITS.sub("[number]", text)
    return text
```

- [ ] **Step 4: Run the module, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_digits tests.test_log_redaction tests.test_verify_first_parts tests.test_verify_first tests.test_verify_first_parsing tests.test_audit_edges
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected results:
- All listed modules pass.
- No existing test changes. These guard behaviour that must not move, and stay green unchanged: `IdentifiersAreNotPhonesTests`, `RedactionTests.test_other_numbers_are_left_alone` (`tests/test_verify_first_parts.py`), `test_every_grouping_the_step_accepts_is_hidden`, `FreeTextRedactionTests` (`tests/test_audit_edges.py`) and `DevanagariDigitsTests` (`tests/test_verify_first.py`).
- The whole suite shows the previous count plus the new tests, with only the known environmental failure, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/digits.py src/emotorad_ai/verify_first.py src/emotorad_ai/observability.py tests/test_digits.py tests/test_log_redaction.py
git commit -m "fix: hide glued, Devanagari-digit and foreign numbers in the log" -m "redact_pii reads digits of any script as ASCII first (ascii_digits, moved to digits.py so observability can use it without an import cycle; verify_first still offers it). It hides an Indian mobile glued to Latin or Devanagari letters and +<country code> numbers, and leaves identifiers alone: a key, a hex id, digits that run on into a dash or an extension. Zoho's OAuth field names join the names redacted whatever their value." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 12: Scripts the person runs, the alarm stack, and the runbook and contract updates

**Files:**
- Create: `scripts/zoho/_common.py`, `scripts/zoho/consent_url.py`, `scripts/zoho/exchange_code.py`, `scripts/zoho/probe.py`, `scripts/zoho/test_ticket.py`, `scripts/zoho/revoke.py`, `scripts/zoho/tickets_report.py`
- Create: `infra/zoho-alarms.yaml`
- Modify: `docs/runbooks/config-store.md:16-17` (rows for every `EMOTORAD_ZOHO_*` name), and the end of the file (new section 7)
- Modify: `docs/contracts/amiigo-support-chat.md:7`, `:16`, `:209`, `:213-214` (part 3 changes)
- Test: `tests/test_zoho_scripts.py`

**Interfaces:**
- Consumes:
  - `ZohoSettings(client_id, client_secret, refresh_token, org_id, test_department_id, test_contact_id, department_id, unverified_contact_id, live, environment, cf_chat_reference, cf_source, priority_high, priority_medium, channel, credits_floor, attachment_limit_bytes)` (Task 6).
  - From Task 7:
    - `DeskHTTP(opener=..., timeout=8.0)`, with `.call(method, url, headers, body=None, *, write, timeout=None) -> (status, parsed)` and `.last_credits_remaining`.
    - `TokenSource(settings, http)`, with `.token()` and `.invalidate()`.
    - `DeskClient(settings, tokens, http)`, with `.search_contacts(field, last_ten)`, `.create_ticket(payload) -> {"id","ticketNumber","webUrl"}`, `.contact_tickets(contact_id, department_id, limit=50)`, `.add_comment(ticket_id, content) -> str` and `.upload_attachment(ticket_id, filename, data, mime) -> str`.
    - `find_adoptable(tickets, cf_api_name, chat_reference)`.
    - `ZohoError` (`.error`), `ZohoAuthExpired` and `ZohoTooLarge`.
  - `ticket_payload(record, settings, contact_id)` (Task 8).
  - From Tasks 2 and 3:
    - `DeskTicketSystem(store, mode, environment)`, with `.create(source_key=..., persona=..., **fields) -> {"ticket_id","status"}`.
    - `InMemoryTicketStore()`, with `.next_reference()`, `.insert(record)`, `.get(reference)` and `.listing(mode)`.
    - `new_record(...)` and `now_iso()`.
    - `wiring.build_stores(settings).tickets`.
  - `observability.redact_pii` (Task 11).
- Produces:
  - `scripts/zoho/_common.py`:
    - Constants: `SCOPES`, `AUTH_URL`, `TOKEN_URL`, `REVOKE_URL`, `TEST_DEPARTMENT_NAME = "AI chatbot test"`, `TEST_PHONE = "+919999999999"` and `SHAPES`.
    - Functions: `scope_string()`, `consent_url(client_id, redirect_uri)`, `exchange_form(...)`, `refresh_form(...)`, `post_form(http, url, form)`, `says_india(answer)`, `granted_scopes(answer)`, `revoke(http, token)`, `ask_secret(prompt, ask)`, `script_settings(...)`, `step(out, label, read)`, `rows(answer)`, `mask(value, person=False)`, `save_shape(name, answer, source, shapes_dir, person=False)`, `source_note(script)`, `department_names(shapes_dir)` and `department_refusal(names, department_id, real, typed_name)`.
    - Class: `ScriptClient(settings, http)`, with `.get(path, params, org=True)` and `.desk`.
  - `test_ticket.look_up(...)`, `needs_second_create(found)`, `fake_record(n, environment, mode)`, `tiny_png()` and `filler(megabytes)`.
  - `tickets_report.lines(rows)`.
  - `infra/zoho-alarms.yaml`, which alarms on the seven events. Tasks 13 to 15 emit `safety_ticket_not_recorded` and `unverified_ticket_capped` as `log.emit("<name>", ...)`, so `AlarmStackTests` sees them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zoho_scripts.py`:

```python
"""The Zoho scripts a person runs, and the alarm stack (spec 2026-10-05, sections 8 and 10).

The scripts reach the real Zoho, so none runs end to end here. What is tested
is every part that decides something: the consent address and its scopes, the
code exchange's India rule, the masking of what is printed and saved, the
department guard, the look-up the worker shares, the listing for the support
lead, and the alarm stack against the events the code emits.

Zoho's accounts server and the Desk client are small doubles with the shared
interfaces' signatures (DeskHTTP.call, DeskClient.contact_tickets). Test data
is fake: +919999999999 and the fixtures' frame numbers.
"""

import ast
import importlib.util
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import urllib.parse
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ZOHO_SCRIPTS = ROOT / "scripts" / "zoho"
if str(ZOHO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ZOHO_SCRIPTS))

import _common  # noqa: E402
from emotorad_ai.tickets.clock import now_iso  # noqa: E402
from emotorad_ai.tickets.record import new_record  # noqa: E402
from emotorad_ai.tickets.store import InMemoryTicketStore  # noqa: E402
from emotorad_ai.zoho.payload import ticket_payload  # noqa: E402

SCRIPTS = ("_common", "consent_url", "exchange_code", "probe", "test_ticket", "revoke", "tickets_report")
REDIRECT = "https://example.test/zoho/callback"
INDIA = {"access_token": "1000.access.value", "refresh_token": "1000.refresh.value",
         "scope": "Desk.tickets.READ Desk.basic.READ", "api_domain": "https://www.zohoapis.in",
         "token_type": "Bearer", "expires_in": 3600}
DEPARTMENTS = {"data": [{"id": "111", "name": "AI chatbot test", "isEnabled": True},
                        {"id": "222", "name": "Service", "isEnabled": True}]}
TICKET_ARGS = ["--org-id", "60001234567", "--contact-id", "C1",
               "--cf-chat-reference", "cf_chat_reference", "--cf-source", "cf_source"]


def load(name):
    spec = importlib.util.spec_from_file_location("zoho_script_" + name, ZOHO_SCRIPTS / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Screen:
    """What a script printed."""

    def __init__(self):
        self.lines = []

    def out(self, line):
        self.lines.append(str(line))

    @property
    def text(self):
        return "\n".join(self.lines)


def answers(*values):
    """Hidden input, answered in order."""
    queue = list(values)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return queue.pop(0)

    ask.asked = asked
    return ask


def never(prompt):
    raise AssertionError("asked for %r" % prompt)


class FakeAccounts:
    """Zoho's accounts server as DeskHTTP.call reaches it: answers in order,
    and keeps each form that was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.last_credits_remaining = None

    def call(self, method, url, headers, body=None, *, write, timeout=None):
        self.calls.append({"method": method, "url": url, "write": write,
                           "form": dict(urllib.parse.parse_qsl((body or b"").decode("utf-8")))})
        return self.replies.pop(0)


class FakeDesk:
    """DeskClient.contact_tickets, one listing per call."""

    def __init__(self, *listings):
        self.listings = list(listings)
        self.calls = []

    def contact_tickets(self, contact_id, department_id, limit=50):
        self.calls.append((contact_id, department_id))
        return self.listings.pop(0)


def zoho_ticket(chat_reference, number="1201"):
    return {"id": "1892000000123001", "ticketNumber": number, "departmentId": "111",
            "createdTime": "2026-10-05T10:00:00.000Z",
            "cf": {"cf_chat_reference": chat_reference, "cf_source": "AI chatbot"}}


class ScopesAndConsentTests(unittest.TestCase):
    def test_the_eight_scopes_comma_separated(self):
        self.assertEqual(
            _common.scope_string(),
            "Desk.tickets.CREATE,Desk.tickets.UPDATE,Desk.tickets.READ,Desk.search.READ,"
            "Desk.contacts.READ,Desk.contacts.CREATE,Desk.basic.READ,Desk.settings.READ",
        )

    def test_no_scope_for_uploads_is_asked_for(self):
        self.assertNotIn("Desk.basic.CREATE", _common.SCOPES)

    def test_the_consent_address_is_indias_with_offline_access_and_a_fresh_consent(self):
        url = _common.consent_url("1000.TESTCLIENT", REDIRECT)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "accounts.zoho.in", "/oauth/v2/auth"))
        self.assertEqual(dict(urllib.parse.parse_qsl(parts.query)), {
            "response_type": "code", "client_id": "1000.TESTCLIENT", "scope": _common.scope_string(),
            "redirect_uri": REDIRECT, "access_type": "offline", "prompt": "consent",
        })
        # Commas as Zoho documents them, not %2C.
        self.assertIn("scope=Desk.tickets.CREATE,Desk.tickets.UPDATE,", url)

    def test_the_script_asks_for_the_client_id_hidden_and_prints_the_address(self):
        screen = Screen()
        ask = answers("1000.TESTCLIENT")
        rc = load("consent_url").main(["--redirect-uri", REDIRECT], ask=ask, out=screen.out)
        self.assertEqual(rc, 0)
        self.assertEqual(ask.asked, ["Zoho client id: "])
        [address] = [line for line in screen.lines if line.startswith("https://")]
        self.assertEqual(address, _common.consent_url("1000.TESTCLIENT", REDIRECT))


class ExchangeTests(unittest.TestCase):
    def exchange(self, *replies, argv=("--redirect-uri", REDIRECT)):
        accounts = FakeAccounts(*replies)
        screen = Screen()
        ask = answers("1000.TESTCLIENT", "test-client-secret", "1000.grantcode")
        rc = load("exchange_code").main(list(argv), ask=ask, http=accounts, out=screen.out)
        return rc, accounts, screen

    def test_server_based_sends_the_redirect_address(self):
        rc, accounts, _ = self.exchange((200, INDIA))
        self.assertEqual(rc, 0)
        [call] = accounts.calls
        self.assertEqual((call["method"], call["url"], call["write"]), ("POST", _common.TOKEN_URL, True))
        self.assertEqual(call["form"], {
            "grant_type": "authorization_code", "client_id": "1000.TESTCLIENT",
            "client_secret": "test-client-secret", "code": "1000.grantcode", "redirect_uri": REDIRECT,
        })

    def test_a_self_client_sends_no_redirect_address(self):
        rc, accounts, _ = self.exchange((200, INDIA), argv=("--self-client",))
        self.assertEqual(rc, 0)
        self.assertNotIn("redirect_uri", accounts.calls[0]["form"])

    def test_an_india_answer_shows_the_refresh_token_once_and_nothing_else_secret(self):
        rc, _, screen = self.exchange((200, INDIA))
        self.assertEqual(rc, 0)
        self.assertEqual(screen.text.count("1000.refresh.value"), 1)
        self.assertNotIn("1000.access.value", screen.text)
        self.assertNotIn("test-client-secret", screen.text)

    def test_another_data_centre_is_refused_and_its_token_revoked_unseen(self):
        elsewhere = dict(INDIA, api_domain="https://www.zohoapis.com")
        rc, accounts, screen = self.exchange((200, elsewhere), (200, {"status": "success"}))
        self.assertEqual(rc, 1)
        self.assertNotIn("1000.refresh.value", screen.text)
        self.assertEqual(accounts.calls[1]["url"], _common.REVOKE_URL)
        self.assertEqual(accounts.calls[1]["form"], {"token": "1000.refresh.value"})

    def test_a_refused_code_prints_the_error_name_only(self):
        rc, accounts, screen = self.exchange((200, {"error": "invalid_code"}))
        self.assertEqual(rc, 1)
        self.assertIn("invalid_code", screen.text)
        self.assertEqual(len(accounts.calls), 1)

    def test_no_refresh_token_explains_offline_access(self):
        rc, _, screen = self.exchange((200, {k: v for k, v in INDIA.items() if k != "refresh_token"}))
        self.assertEqual(rc, 1)
        self.assertIn("access_type=offline", screen.text)


class IndiaAndScopesTests(unittest.TestCase):
    def test_only_the_india_data_centre_passes(self):
        self.assertTrue(_common.says_india({"api_domain": "https://www.zohoapis.in"}))
        self.assertTrue(_common.says_india({"api_domain": "https://www.zohoapis.in", "location": "in"}))
        for answer in ({"api_domain": "https://www.zohoapis.com"}, {"api_domain": "https://www.zohoapis.eu"},
                       {"api_domain": "https://www.zohoapis.in.example.com"}, {"location": "us"},
                       {"api_domain": "https://www.zohoapis.in", "location": "eu"}, {}):
            self.assertFalse(_common.says_india(answer), answer)

    def test_granted_scopes_with_spaces_or_commas(self):
        self.assertEqual(_common.granted_scopes({"scope": "Desk.tickets.READ Desk.basic.READ"}),
                         ["Desk.tickets.READ", "Desk.basic.READ"])
        self.assertEqual(_common.granted_scopes({"scope": "Desk.tickets.READ,Desk.basic.READ"}),
                         ["Desk.tickets.READ", "Desk.basic.READ"])
        self.assertEqual(_common.granted_scopes({}), [])


class AskSecretTests(unittest.TestCase):
    def test_an_empty_answer_stops_the_script(self):
        with self.assertRaises(SystemExit):
            _common.ask_secret("Zoho client secret: ", ask=lambda prompt: "  ")

    def test_the_answer_is_trimmed(self):
        self.assertEqual(_common.ask_secret("Zoho client id: ", ask=lambda prompt: " 1000.X \n"), "1000.X")


class RevokeTests(unittest.TestCase):
    def test_the_token_goes_as_a_form_field_to_indias_revoke_address(self):
        accounts = FakeAccounts((200, {"status": "success"}))
        screen = Screen()
        rc = load("revoke").main([], ask=answers("1000.refresh.value"), typed=lambda prompt: "REVOKE",
                                 http=accounts, out=screen.out)
        self.assertEqual(rc, 0)
        [call] = accounts.calls
        self.assertEqual((call["method"], call["url"]), ("POST", "https://accounts.zoho.in/oauth/v2/revoke/token"))
        self.assertEqual(call["form"], {"token": "1000.refresh.value"})
        self.assertNotIn("1000.refresh.value", call["url"])
        self.assertNotIn("1000.refresh.value", screen.text)

    def test_nothing_is_revoked_without_typing_revoke(self):
        accounts = FakeAccounts()
        rc = load("revoke").main([], ask=answers("1000.refresh.value"), typed=lambda prompt: "yes",
                                 http=accounts, out=Screen().out)
        self.assertEqual(rc, 1)
        self.assertEqual(accounts.calls, [])

    def test_a_refusal_names_the_error(self):
        screen = Screen()
        rc = load("revoke").main([], ask=answers("1000.refresh.value"), typed=lambda prompt: "REVOKE",
                                 http=FakeAccounts((200, {"error": "invalid_token"})), out=screen.out)
        self.assertEqual(rc, 1)
        self.assertIn("invalid_token", screen.text)


TICKET_ANSWER = {
    "id": "1892000000123001", "ticketNumber": "1201", "departmentId": "1892000000006907",
    "subject": "[AI chat] Battery: charging - EMX Plus",
    "description": "Call Ananya on 9876543210 or ananya.rao@example.com",
    "email": "ananya.rao@example.com", "phone": "+919999999999", "priority": "Medium",
    "channel": "Chat", "status": "Open",
    "cf": {"cf_chat_reference": "stage:EM-TEST-7", "cf_source": "AI chatbot"},
    "contact": {"id": "1892000000099001", "firstName": "Ananya", "lastName": "Rao",
                "email": "ananya.rao@example.com", "phone": "+919999999999", "mobile": "9876543210", "type": None},
    "assignee": {"id": "1892000000011111", "firstName": "Ravi", "lastName": "Kumar",
                 "email": "ravi.k@example.com", "name": "Ravi Kumar"},
    "createdTime": "2026-10-05T10:00:00.000Z",
}
LAYOUT_DETAILS = [{"sections": [{"name": "Ticket Information", "fields": [
    {"apiName": "priority", "displayLabel": "Priority", "isMandatory": False, "allowedValues": ["High", "Medium", "Low"]},
    {"apiName": "cf_dealer_principle_name", "displayLabel": "Dealer Principle Name", "isCustomField": True,
     "allowedValues": ["Ravi Motors - D001"], "defaultValue": "Ravi Motors - D001"},
    {"apiName": "cf_chat_reference", "displayLabel": "Chat reference", "isCustomField": True, "isMandatory": True},
    {"apiName": "status", "displayLabel": "Status", "allowedValues": [{"value": "Open", "id": "1"}]},
]}]}]


class MaskingTests(unittest.TestCase):
    PERSONAL = ("Ananya", "Rao", "ananya.rao@example.com", "9999999999", "9876543210",
                "Ravi", "Kumar", "ravi.k@example.com")

    def test_no_phone_email_or_name_survives(self):
        written = json.dumps(_common.mask(TICKET_ANSWER))
        for personal in self.PERSONAL:
            self.assertNotIn(personal, written)

    def test_ids_numbers_and_enums_survive_and_every_key_stays(self):
        masked = _common.mask(TICKET_ANSWER)
        self.assertEqual(set(masked), set(TICKET_ANSWER))
        self.assertEqual(masked["id"], "1892000000123001")
        self.assertEqual(masked["ticketNumber"], "1201")
        self.assertEqual((masked["priority"], masked["channel"], masked["status"]), ("Medium", "Chat", "Open"))
        self.assertEqual(set(masked["cf"]), {"cf_chat_reference", "cf_source"})
        self.assertEqual(masked["contact"]["id"], "1892000000099001")
        self.assertEqual(masked["contact"]["firstName"], "<str>")
        self.assertIsNone(masked["contact"]["type"])
        self.assertEqual(masked["phone"], "<str>")

    def test_a_contact_answer_keeps_only_its_id_and_type(self):
        masked = _common.mask({"id": "5", "type": "END_USER", "name": "Ananya Rao", "firstName": "Ananya"}, person=True)
        self.assertEqual(masked, {"id": "5", "type": "END_USER", "name": "<str>", "firstName": "<str>"})

    def test_a_pick_list_of_people_is_counted_not_written(self):
        masked = _common.mask(LAYOUT_DETAILS)
        fields = masked[0]["sections"][0]["fields"]
        self.assertEqual(fields[0]["allowedValues"], ["High", "Medium", "Low"])
        self.assertEqual(fields[1]["allowedValues"]["count"], 1)
        self.assertNotIn("Ravi", json.dumps(masked))
        self.assertEqual(fields[3]["allowedValues"], [{"value": "Open", "id": "1"}])
        self.assertEqual(masked[0]["sections"][0]["name"], "Ticket Information")

    def test_tokens_are_never_written(self):
        masked = _common.mask(INDIA)
        self.assertEqual((masked["access_token"], masked["refresh_token"]), ("[secret]", "[secret]"))
        self.assertEqual(masked["api_domain"], "https://www.zohoapis.in")
        self.assertEqual(masked["expires_in"], 3600)

    def test_a_saved_shape_carries_its_source_and_round_trips_department_names(self):
        with tempfile.TemporaryDirectory() as shapes:
            path = _common.save_shape("departments", DEPARTMENTS, "captured in a test", Path(shapes))
            self.assertEqual(path.name, "zoho-departments.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["_source"], "captured in a test")
            self.assertEqual(_common.department_names(Path(shapes)), {"111": "AI chatbot test", "222": "Service"})
            empty = _common.save_shape("contact-search", None, "a 204", Path(shapes))
            self.assertEqual(json.loads(empty.read_text(encoding="utf-8")), {"_source": "a 204", "body": None})


class ProbeOutputTests(unittest.TestCase):
    def setUp(self):
        self.probe = load("probe")

    def test_field_lines_show_api_names_flags_and_kept_values_only(self):
        text = "\n".join(self.probe.field_lines(_common.mask(LAYOUT_DETAILS)))
        self.assertRegex(text, r"priority\s+Priority\s+values: High, Medium, Low")
        self.assertRegex(text, r"cf_chat_reference\s+Chat reference\s+mandatory custom")
        self.assertIn("1 values withheld", text)
        self.assertNotIn("Ravi", text)

    def test_a_contact_is_described_without_its_number(self):
        self.assertEqual(self.probe.describe_contact({"id": "5", "phone": "+919999999999"}), "found, has a number")
        self.assertEqual(self.probe.describe_contact({"id": "6"}), "found, no number")
        self.assertEqual(self.probe.describe_contact(None), "not found")


class DepartmentGuardTests(unittest.TestCase):
    NAMES = {"111": "AI chatbot test", "222": "Service"}

    def test_the_test_department_is_allowed(self):
        self.assertIsNone(_common.department_refusal(self.NAMES, "111", False, None))

    def test_any_other_department_is_refused_without_the_flag(self):
        self.assertIn("AI chatbot test", _common.department_refusal(self.NAMES, "222", False, None))

    def test_a_department_the_probe_never_saw_is_refused(self):
        self.assertIn("probe.py", _common.department_refusal(self.NAMES, "333", False, None))

    def test_the_real_department_needs_its_exact_name_typed(self):
        self.assertIsNone(_common.department_refusal(self.NAMES, "222", True, "Service"))
        self.assertIsNotNone(_common.department_refusal(self.NAMES, "222", True, "service"))
        self.assertIsNotNone(_common.department_refusal(self.NAMES, "222", True, ""))

    def test_the_real_department_flag_never_names_the_test_one(self):
        self.assertIsNotNone(_common.department_refusal(self.NAMES, "111", True, "AI chatbot test"))

    def test_the_script_refuses_before_asking_for_any_secret(self):
        for department, real, typed in (("222", False, never), ("222", True, lambda prompt: "service")):
            with tempfile.TemporaryDirectory() as shapes:
                _common.save_shape("departments", DEPARTMENTS, "test", Path(shapes))
                screen = Screen()
                argv = TICKET_ARGS + ["--department-id", department] + (["--real-department"] if real else [])
                rc = load("test_ticket").main(argv, ask=never, typed=typed, http=FakeAccounts(),
                                              out=screen.out, shapes_dir=Path(shapes))
            self.assertEqual(rc, 1, department)
            self.assertTrue(screen.lines)


class LookUpTests(unittest.TestCase):
    REFERENCE = "stage:EM-TEST-7"

    def setUp(self):
        self.script = load("test_ticket")
        self.sleeps = []

    def look(self, *listings):
        desk = FakeDesk(*listings)
        found = self.script.look_up(desk, "C1", "111", "cf_chat_reference", self.REFERENCE,
                                    sleep=self.sleeps.append, out=Screen().out)
        return desk, found

    def test_found_at_once_makes_no_second_ticket(self):
        desk, found = self.look([zoho_ticket(self.REFERENCE)], [zoho_ticket(self.REFERENCE)])
        self.assertEqual([t["ticketNumber"] for t in found], ["1201", "1201"])
        self.assertFalse(self.script.needs_second_create(found))
        self.assertEqual(self.sleeps, [120])
        self.assertEqual(desk.calls, [("C1", "111"), ("C1", "111")])

    def test_found_only_after_two_minutes_makes_no_second_ticket(self):
        _, found = self.look([], [zoho_ticket(self.REFERENCE)])
        self.assertIsNone(found[0])
        self.assertFalse(self.script.needs_second_create(found))

    def test_never_found_makes_a_second_ticket(self):
        _, found = self.look([], [])
        self.assertTrue(self.script.needs_second_create(found))

    def test_a_near_miss_reference_is_not_adopted(self):
        _, found = self.look([zoho_ticket("stage:EM-TEST-70")], [zoho_ticket("stage:EM-TEST-70")])
        self.assertEqual(found, [None, None])
        self.assertTrue(self.script.needs_second_create(found))


class TestTicketShapeTests(unittest.TestCase):
    def setUp(self):
        self.script = load("test_ticket")

    def settings(self, live=False, department="111"):
        return _common.script_settings(
            client_id="test-id", client_secret="test-secret", refresh_token="test-token", org_id="60001234567",
            department_id=department, contact_id="C1", live=live,
            cf_chat_reference="cf_chat_reference", cf_source="cf_source")

    def test_the_test_ticket_is_built_as_the_worker_builds_one(self):
        record = self.script.fake_record(7, "stage", "test")
        self.assertEqual((record["_id"], record["chat_reference"], record["mode"]),
                         ("EM-TEST-7", "stage:EM-TEST-7", "test"))
        payload = ticket_payload(record, self.settings(), "C1")
        self.assertEqual(str(payload["departmentId"]), "111")
        self.assertEqual(payload["contactId"], "C1")
        self.assertEqual(payload["cf"]["cf_chat_reference"], "stage:EM-TEST-7")
        self.assertEqual(self.script.FRAME_NUMBER, "EMXP2025004417")
        self.assertIn(self.script.FRAME_NUMBER, payload["description"])

    def test_a_real_department_ticket_goes_to_the_department_named(self):
        record = self.script.fake_record(8, "stage", "live")
        payload = ticket_payload(record, self.settings(live=True, department="222"), "C1")
        self.assertEqual(str(payload["departmentId"]), "222")

    def test_the_small_image_is_a_real_png(self):
        data = self.script.tiny_png()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", data[16:24]), (8, 8))
        idat_length = struct.unpack(">I", data[33:37])[0]
        self.assertEqual(data[37:41], b"IDAT")
        self.assertEqual(len(zlib.decompress(data[41:41 + idat_length])), 8 * (8 + 1))

    def test_a_filler_file_has_the_size_asked_for(self):
        self.assertEqual(len(self.script.filler(1)), 1024 * 1024)


class TicketsReportTests(unittest.TestCase):
    def setUp(self):
        self.report = load("tickets_report")

    def test_a_waiting_record_is_listed_with_the_last_four_digits_only(self):
        store = InMemoryTicketStore()
        reference = store.next_reference()
        now = now_iso()
        store.insert(new_record(
            reference=reference, chat_reference="stage:" + reference,
            source_key="c1:%s:create_support_ticket:k1" % now, mode="test", kind="support",
            conversation_id="c1", started_at=now, cluster_id=None, channel="website_chat",
            phone="+919999999999", identity="verified", category="battery_charging", ai_severity="normal",
            summary="The battery will not charge.", claims={}, bike=None, coverage=None, customer_name=None,
            created_at=now,
        ))
        text = "\n".join(self.report.lines(store.listing("test")))
        self.assertIn(reference, text)
        self.assertIn("waiting", text)
        self.assertIn("...9999", text)
        self.assertNotIn("9999999999", text)
        self.assertIn("1 record(s)", text)

    def test_it_refuses_without_the_deployments_store(self):
        screen = Screen()
        with mock.patch.dict(os.environ, {"EMOTORAD_STORE": "memory"}):
            rc = self.report.main([], out=screen.out)
        self.assertEqual(rc, 1)
        self.assertIn("not mongodb", screen.text)

    def test_the_mode_is_the_deployments(self):
        with mock.patch.dict(os.environ, {"EMOTORAD_ZOHO_LIVE": "yes"}):
            self.assertEqual(self.report.parser().parse_args([]).mode, "live")
        with mock.patch.dict(os.environ, {"EMOTORAD_ZOHO_LIVE": "no"}):
            self.assertEqual(self.report.parser().parse_args([]).mode, "test")


class ScriptHygieneTests(unittest.TestCase):
    def test_every_script_says_who_runs_it_and_where(self):
        for name in SCRIPTS:
            tree = ast.parse((ZOHO_SCRIPTS / ("%s.py" % name)).read_text(encoding="utf-8"))
            doc = " ".join((ast.get_docstring(tree) or "").split())
            self.assertIn("Run by a person, never by a Claude session", doc, name)
            self.assertIn("outside the Claude app", doc, name)

    def test_no_script_holds_a_token(self):
        for name in SCRIPTS:
            text = (ZOHO_SCRIPTS / ("%s.py" % name)).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"1000\.[0-9a-f]{16,}", name)


# The events the spec alarms on (section 8), and those it logs without an alarm.
ALARMED = ("zoho_misconfigured", "zoho_token_refused", "zoho_worker_error", "zoho_ticket_stuck",
           "safety_ticket_late", "safety_ticket_not_recorded", "unverified_ticket_capped")
NOT_ALARMED = ("zoho_ticket_sent", "zoho_retry", "zoho_rejected")
_EMITTED = re.compile(r"""\bemit\(\s*["']((?:zoho|safety_ticket|unverified_ticket)_[a-z_]+)["']""")


class AlarmStackTests(unittest.TestCase):
    """Read as text: the template uses CloudFormation tags (!Ref, !Sub), which a
    plain YAML parser refuses."""

    def setUp(self):
        self.text = (ROOT / "infra" / "zoho-alarms.yaml").read_text(encoding="utf-8")

    def test_every_alarmed_event_has_a_filter_and_an_alarm(self):
        for event in ALARMED:
            with self.subTest(event=event):
                self.assertIn("FilterPattern: '{ $.event = \"%s\" }'" % event, self.text)
                self.assertGreaterEqual(self.text.count("MetricName: %s\n" % event), 2)
                self.assertIn('AlarmName: !Sub "${LogGroupName}-%s"' % event, self.text)

    def test_the_stack_takes_the_log_group_and_emails_one_person(self):
        self.assertRegex(self.text, r"(?m)^  LogGroupName:\n    Type: String")
        self.assertRegex(self.text, r"(?m)^  AlarmEmail:\n    Type: String")
        self.assertIn("Protocol: email", self.text)
        self.assertIn("Endpoint: !Ref AlarmEmail", self.text)
        self.assertEqual(self.text.count("Type: AWS::Logs::MetricFilter"), len(ALARMED))
        self.assertEqual(self.text.count("Type: AWS::CloudWatch::Alarm"), len(ALARMED))
        self.assertEqual(self.text.count("- !Ref AlarmTopic"), len(ALARMED))

    def test_every_zoho_event_the_code_emits_has_a_decision(self):
        emitted = set()
        for path in (ROOT / "src" / "emotorad_ai").rglob("*.py"):
            emitted.update(_EMITTED.findall(path.read_text(encoding="utf-8")))
        undecided = sorted(emitted - set(ALARMED) - set(NOT_ALARMED))
        self.assertEqual(undecided, [], "add each to infra/zoho-alarms.yaml, or to NOT_ALARMED if the spec does not alarm it")
        for event in emitted & set(ALARMED):
            self.assertIn('"%s"' % event, self.text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_scripts
```

Expected: the module fails to load (`unittest.loader._FailedTest`) with `ModuleNotFoundError: No module named '_common'`.

- [ ] **Step 3: Implement**

Create `scripts/zoho/_common.py`:

```python
"""Shared pieces of the Zoho scripts (spec 2026-10-05, section 10).

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

Every script asks for secrets by hidden input and keeps them in memory. None
prints a token or a secret, except exchange_code.py, whose job is to show the
refresh token once. Zoho's answers are masked (`mask`) before they are printed
or saved, so no phone, email or person's name reaches the screen or the repo.

The scripts reach Desk through the service's own client (emotorad_ai.zoho):
the same HTTP layer, token source and Desk calls, and the same look-up
(`find_adoptable`) the worker runs after an unknown outcome. What a script
proves against the real Zoho is what the worker will do.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.observability import redact_pii  # noqa: E402
from emotorad_ai.zoho.auth import TokenSource  # noqa: E402
from emotorad_ai.zoho.desk import DeskClient  # noqa: E402
from emotorad_ai.zoho.errors import ZohoAuthExpired, ZohoError  # noqa: E402
from emotorad_ai.zoho.http import DeskHTTP  # noqa: E402
from emotorad_ai.zoho.settings import ZohoSettings  # noqa: E402

# The India data centre only (spec section 1). The service calls the same two
# hosts. These are for the accounts calls only a person makes (consent, code
# exchange, revoke) and for the reads only the probe makes.
ACCOUNTS = "https://accounts.zoho.in"
DESK = "https://desk.zoho.in"
AUTH_URL = ACCOUNTS + "/oauth/v2/auth"
TOKEN_URL = ACCOUNTS + "/oauth/v2/token"
# Zoho's revoke page: a POST with the token as a form field, never in the address.
REVOKE_URL = ACCOUNTS + "/oauth/v2/revoke/token"

# Every scope the chatbot needs, asked for in one grant (spec section 10). A
# second grant would mean another refresh token on the OMS's client.
# Desk.settings.READ is here in case the layout calls need it. Desk.basic.CREATE
# is not: it is only for /uploads, which this design does not use.
SCOPES = (
    "Desk.tickets.CREATE",
    "Desk.tickets.UPDATE",
    "Desk.tickets.READ",
    "Desk.search.READ",
    "Desk.contacts.READ",
    "Desk.contacts.CREATE",
    "Desk.basic.READ",
    "Desk.settings.READ",
)

# The only department test_ticket.py writes to without --real-department.
TEST_DEPARTMENT_NAME = "AI chatbot test"
# The test contact's fake number (person step 3). Never a real customer's.
TEST_PHONE = "+919999999999"

SHAPES = ROOT / "docs" / "api-shapes"


def scope_string() -> str:
    """The scopes as Zoho's consent address takes them: comma-separated."""
    return ",".join(SCOPES)


def consent_url(client_id: str, redirect_uri: str) -> str:
    """The India consent address for a server-based client (person step 4).
    access_type=offline gives a refresh token; prompt=consent makes Zoho issue
    a new one even if this user approved the client before."""
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": scope_string(),
            "redirect_uri": redirect_uri,
            "access_type": "offline",
            "prompt": "consent",
        },
        safe=",",
    )
    return AUTH_URL + "?" + query


def exchange_form(client_id: str, client_secret: str, code: str, redirect_uri: Optional[str] = None) -> Dict[str, str]:
    """The code exchange. A server-based client sends the redirect address it
    was granted with. A Self Client sends none."""
    form = {"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret, "code": code}
    if redirect_uri:
        form["redirect_uri"] = redirect_uri
    return form


def refresh_form(client_id: str, client_secret: str, refresh_token: str) -> Dict[str, str]:
    return {"grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token}


def post_form(http: Any, url: str, form: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """POST a form to the accounts server. Zoho answers an error with HTTP 200
    and an "error" field, so callers read the body, not the status."""
    body = urllib.parse.urlencode(form).encode("utf-8")
    status, answer = http.call("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, body, write=True)
    return status, answer if isinstance(answer, dict) else {}


def says_india(answer: Dict[str, Any]) -> bool:
    """True only when Zoho's answer names the India data centre, by its
    `location` or its `api_domain`, and nothing in it names another one."""
    seen = []
    location = answer.get("location")
    if location is not None:
        seen.append(str(location).strip().lower() == "in")
    domain = answer.get("api_domain")
    if domain is not None:
        host = (urllib.parse.urlsplit(str(domain)).hostname or "").lower()
        seen.append(host == "zohoapis.in" or host.endswith(".zohoapis.in"))
    return bool(seen) and all(seen)


def granted_scopes(answer: Dict[str, Any]) -> List[str]:
    """The scopes Zoho says it granted, when its answer says. Some answers
    separate them with spaces, others with commas."""
    return [scope for scope in re.split(r"[\s,]+", str(answer.get("scope") or "")) if scope]


def revoke(http: Any, token: str) -> Tuple[bool, str]:
    """Revoke one refresh token. Returns (done, what Zoho said): "revoked", or
    the error name. Never the token."""
    try:
        status, answer = post_form(http, REVOKE_URL, {"token": token})
    except ZohoError as exc:
        return False, exc.error
    if status >= 400 or answer.get("error"):
        return False, str(answer.get("error") or "http_%d" % status)
    return True, "revoked"


def ask_secret(prompt: str, ask: Callable[[str], str] = getpass.getpass) -> str:
    """A secret by hidden input, kept in memory. An empty answer stops the
    script: Zoho would refuse it anyway, after spending a request."""
    value = (ask(prompt) or "").strip()
    if not value:
        raise SystemExit("Nothing entered for %r. Stopping." % prompt.strip().rstrip(":"))
    return value


def script_settings(*, client_id: str, client_secret: str, refresh_token: str, org_id: str,
                    department_id: str = "", contact_id: str = "", live: bool = False,
                    environment: str = "stage", cf_chat_reference: str = "", cf_source: str = "",
                    priority_high: str = "High", priority_medium: str = "Medium",
                    channel: str = "Chat") -> ZohoSettings:
    """Settings for one script run. The department and contact the person
    named stand for both the test and the live ones, so whichever the payload
    picks by the record's mode, it is the one the person chose. test_ticket.py
    checks the payload against it before sending."""
    return ZohoSettings(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token, org_id=org_id,
        test_department_id=department_id, test_contact_id=contact_id,
        department_id=department_id or None, unverified_contact_id=contact_id or None, live=live,
        environment=environment, cf_chat_reference=cf_chat_reference, cf_source=cf_source,
        priority_high=priority_high, priority_medium=priority_medium, channel=channel,
        credits_floor=0, attachment_limit_bytes=20 * 1024 * 1024,
    )


class ScriptClient:
    """The worker's Desk client, plus the reads only the scripts make
    (organisations, departments, layouts, channels, one contact, one ticket)."""

    def __init__(self, settings: ZohoSettings, http: Optional[Any] = None) -> None:
        self.settings = settings
        self.http = http if http is not None else DeskHTTP()
        self.tokens = TokenSource(settings, self.http)
        self.desk = DeskClient(settings, self.tokens, self.http)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, *, org: bool = True) -> Any:
        """A read. An expired access token is refreshed once and the read tried
        once more, as the worker does (spec section 1)."""
        url = DESK + path + ("?" + urllib.parse.urlencode(params) if params else "")
        retried = False
        while True:
            headers = {"Authorization": "Zoho-oauthtoken " + self.tokens.token()}
            if org:
                headers["orgId"] = self.settings.org_id
            try:
                return self.http.call("GET", url, headers, write=False)[1]
            except ZohoAuthExpired:
                if retried:
                    raise
                retried = True
                self.tokens.invalidate()


def step(out: Callable[[str], None], label: str, read: Callable[[], Any]) -> Any:
    """Run one read. On a Zoho refusal, print its error name and carry on."""
    try:
        return read()
    except ZohoError as exc:
        out("%s: refused (error=%s)" % (label, exc.error))
        return None


def rows(answer: Any) -> List[Dict[str, Any]]:
    """The "data" list of a Desk answer. Empty for a 204 or a refusal."""
    data = answer.get("data") if isinstance(answer, dict) else None
    return [row for row in data or [] if isinstance(row, dict)]


# -- masking -----------------------------------------------------------------

# Values worth reading in a shape that name nobody: ids, the names of
# departments, layouts, fields and channels, types, flags, counts and times,
# and Zoho's own error names. Every other value becomes its type.
KEEP_VALUES = frozenset({
    "id", "ticketNumber", "departmentId", "contactId", "layoutId", "productId", "accountId",
    "status", "statusType", "priority", "channel", "classification", "category", "subCategory",
    "module", "type", "apiName", "displayLabel", "name", "layoutName", "layoutDisplayName",
    "isMandatory", "isSystemMandatory", "isCustomField", "isNested", "isEnabled", "isDefault",
    "isDefaultLayout", "isCustomSection", "isPublic", "isSandboxPortal", "maxLength", "defaultValue",
    "contentType", "size", "createdTime", "modifiedTime", "commentedTime", "companyName", "portalName",
    "edition", "count", "errorCode", "fieldName", "errorType", "api_domain", "location", "token_type",
    "expires_in", "scope", "webUrl",
})
# Free text among those, which still goes through the log's phone and email
# filter. Ids are not: a Zoho id is a long digit run the filter would hide.
_TEXT_VALUES = frozenset({"name", "displayLabel", "layoutName", "layoutDisplayName", "companyName",
                          "portalName", "defaultValue"})
# Never written, whatever else is kept.
SECRET_KEYS = frozenset({"access_token", "refresh_token", "client_secret", "authorization"})
# Objects that describe a person. Inside one, only its id and type are kept.
PERSON_KEYS = frozenset({"contact", "assignee", "commenter", "author", "creator", "owner", "createdBy",
                         "modifiedBy", "account", "agent", "sharedBy", "approver"})
# A field whose label or API name says its values hold people or dealers. Its
# pick list is counted, never written (the 1 October probe's rule: the OMS's
# "Dealer Principle Name" list names real dealers).
_PERSONAL_FIELD = re.compile(
    r"name|dealer|principle|customer|contact|phone|mobile|email|address|owner|agent|franchise", re.IGNORECASE)


def mask(value: Any, person: bool = False) -> Any:
    """A Zoho answer with nothing personal in it. Every key stays, so the shape
    is whole, but only the values in KEEP_VALUES survive. Every other value
    becomes its type ("<str>", "<int>"). `person` treats the whole answer as
    a person (a contact read or a contact search)."""
    return _mask(value, None, person)


def _mask(value: Any, key: Optional[str], in_person: bool) -> Any:
    if isinstance(value, dict):
        people = _lists_people(value)
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() in SECRET_KEYS:
                out[k] = "[secret]"
            elif people and k == "allowedValues":
                out[k] = {"withheld": "the list may name people or dealers",
                          "count": len(v) if isinstance(v, list) else 0}
            elif people and k == "defaultValue":
                out[k] = None if v is None else "<withheld>"
            elif k == "allowedValues" and not in_person and isinstance(v, list):
                out[k] = [_allowed_value(item) for item in v]
            else:
                out[k] = _mask(v, k, in_person or k in PERSON_KEYS)
        return out
    if isinstance(value, list):
        return [_mask(item, key, in_person) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if key not in KEEP_VALUES or (in_person and key not in ("id", "type")):
        return "<%s>" % type(value).__name__
    if isinstance(value, str) and key in _TEXT_VALUES:
        return redact_pii(value)
    return value


def _lists_people(field: Dict[str, Any]) -> bool:
    if "apiName" not in field and "displayLabel" not in field:
        return False
    label = "%s %s" % (field.get("apiName") or "", field.get("displayLabel") or "")
    return bool(_PERSONAL_FIELD.search(label))


def _allowed_value(item: Any) -> Any:
    if isinstance(item, str):
        return redact_pii(item)
    if isinstance(item, dict):
        return {k: redact_pii(v) if k == "value" and isinstance(v, str) else _mask(v, k, False)
                for k, v in item.items()}
    return _mask(item, None, False)


def source_note(script: str) -> str:
    return "Captured by scripts/zoho/%s on %s, masked by scripts/zoho/_common.py. Replaces the published-shape draft." % (
        script, date.today().isoformat())


def save_shape(name: str, answer: Any, source: str, shapes_dir: Path = SHAPES, person: bool = False) -> Path:
    """Write a masked answer to docs/api-shapes/zoho-<name>.json, with a
    "_source" note saying where and when it was captured."""
    masked = mask(answer, person=person)
    doc: Dict[str, Any] = {"_source": source}
    if isinstance(masked, dict):
        doc.update(masked)
    else:
        doc["body"] = masked
    path = Path(shapes_dir) / ("zoho-%s.json" % name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def department_names(shapes_dir: Path = SHAPES) -> Dict[str, str]:
    """Department id to name, from the probe's masked departments shape."""
    path = Path(shapes_dir) / "zoho-departments.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {str(row.get("id")): str(row.get("name")) for row in rows(doc)}


def department_refusal(names: Dict[str, str], department_id: str, real: bool,
                       typed_name: Optional[str]) -> Optional[str]:
    """Why a test ticket may not go to this department, or None when it may."""
    name = names.get(str(department_id))
    if name is None:
        return "Department %s is not in docs/api-shapes/zoho-departments.json. Run probe.py first." % department_id
    if not real:
        if name != TEST_DEPARTMENT_NAME:
            return "Department %s is %r, not %r. Test tickets go to the test department only." % (
                department_id, name, TEST_DEPARTMENT_NAME)
        return None
    if name == TEST_DEPARTMENT_NAME:
        return "--real-department names the test department. Leave the flag off for a test ticket."
    if (typed_name or "").strip() != name:
        return "The name typed does not match department %s. Nothing was sent." % department_id
    return None
```

Create `scripts/zoho/consent_url.py`:

```python
"""Print Zoho's consent address for the chatbot's grant (spec 2026-10-05, section 10, person step 4).

    python scripts/zoho/consent_url.py --redirect-uri <the redirect address registered on the client>

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards: the address holds the client id.

For a server-based client only (person step 2 decides). It asks for the client
id by hidden input and prints the India consent address: response_type=code,
the client id, every scope the chatbot needs (comma-separated, SCOPES in
_common.py), the redirect address, access_type=offline and prompt=consent.
Open it signed in to Zoho as the granting user, approve, and copy the code
from the address bar (the page itself may show an error). Then run
exchange_code.py within two minutes.
"""

from __future__ import annotations

import argparse
import getpass
from typing import Callable, List, Optional

from _common import ask_secret, consent_url


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--redirect-uri", required=True, help="the redirect address registered on the OMS's client")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    client_id = ask_secret("Zoho client id: ", ask)
    out("Open this address while signed in to Zoho as the granting user, then approve:")
    out(consent_url(client_id, args.redirect_uri))
    out("Copy the code= value from the address bar (the page may show an error), "
        "then run exchange_code.py within two minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `scripts/zoho/exchange_code.py`:

```python
"""Swap a Zoho grant code for the chatbot's refresh token (spec 2026-10-05, section 10, person step 4).

    python scripts/zoho/exchange_code.py --redirect-uri <the same address as consent_url.py>
    python scripts/zoho/exchange_code.py --self-client

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session), within two minutes
of approving. Clear the scrollback afterwards.

It asks for the client id, the client secret and the code by hidden input, and
posts them to https://accounts.zoho.in/oauth/v2/token: with the redirect
address for a server-based client, without it for a Self Client (spec section
1: a Self Client stops part 1 until Sachin decides). It refuses unless Zoho's
answer names the India data centre. A token issued anywhere else is revoked at
once and never shown. Otherwise it prints the refresh token once, with a
warning. Keep it somewhere safe until it goes into the config store (person
step 8). Replaces reports/zoho-probe/exchange_code.py.
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any, Callable, List, Optional

from _common import TOKEN_URL, ask_secret, exchange_form, granted_scopes, post_form, revoke, says_india
from emotorad_ai.zoho.errors import ZohoError
from emotorad_ai.zoho.http import DeskHTTP

WARNING = (
    "The refresh token is on the next line, shown once. Put it straight into the config store "
    "(docs/runbooks/config-store.md, section 7) or a password manager. Never paste it into a chat, "
    "a file in a repo or a ticket. Then clear this terminal's scrollback."
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--redirect-uri", help="a server-based client: the address used in consent_url.py")
    kind.add_argument("--self-client", action="store_true", help="a Self Client: no redirect address")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass, http: Any = None,
         out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    client_id = ask_secret("Zoho client id: ", ask)
    client_secret = ask_secret("Zoho client secret: ", ask)
    code = ask_secret("Grant code from the address bar: ", ask)
    http = http if http is not None else DeskHTTP()
    form = exchange_form(client_id, client_secret, code, None if args.self_client else args.redirect_uri)
    try:
        _, answer = post_form(http, TOKEN_URL, form)
    except ZohoError as exc:
        out("Zoho refused the code (error=%s). Codes last two minutes: approve again and rerun." % exc.error)
        return 1
    if answer.get("error"):
        out("Zoho refused the code (error=%s). Codes last two minutes: approve again and rerun." % answer["error"])
        return 1
    refresh_token = answer.get("refresh_token")
    if not says_india(answer):
        out("Zoho's answer does not name the India data centre, so the token is not shown.")
        if refresh_token:
            done, said = revoke(http, refresh_token)
            out("The token Zoho issued was revoked." if done else
                "The token Zoho issued was NOT revoked (error=%s). Ask the Zoho admin to remove it." % said)
        return 1
    if not refresh_token:
        out("Zoho gave no refresh token. The consent address needs access_type=offline and prompt=consent "
            "(consent_url.py adds both). Approve again and rerun.")
        return 1
    granted = granted_scopes(answer)
    out("Scopes granted: %s" % (", ".join(granted) if granted else "Zoho did not say"))
    out(WARNING)
    out(refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `scripts/zoho/probe.py`:

```python
"""Read-only probe of EMotorad's Zoho Desk for the chatbot (spec 2026-10-05, section 10, person step 5).

    python scripts/zoho/probe.py --org-id <org id> --test-department-id <id> \
        --test-contact-id <id> [--department-id <id>] [--unverified-contact-id <id>]

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards. It asks for the client id, the client secret and the
chatbot's refresh token by hidden input, and keeps them in memory.

It only reads. It checks that the token sees the organisation given. Then it
lists the departments, the ticket layouts and fields of the test and real
departments, the contact layout, the channels, and the test and unverified
contacts. It prints the scopes Zoho says it granted and the API credits left
today. Every answer is masked (scripts/zoho/_common.py) and written to
docs/api-shapes/zoho-*.json, replacing the drafts taken from Zoho's published
specification. Claude reviews those files and fills in the settings.

Two token requests per run (one to read the scopes, one by the client). Zoho
allows ten in ten minutes per refresh token. Replaces the 1 October spike in
reports/zoho-probe/.
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from _common import (
    SCOPES, SHAPES, TEST_DEPARTMENT_NAME, TOKEN_URL, ScriptClient, ask_secret, granted_scopes, mask, post_form,
    refresh_form, rows, save_shape, says_india, script_settings, source_note, step,
)
from emotorad_ai.zoho.errors import ZohoError
from emotorad_ai.zoho.http import DeskHTTP


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--org-id", required=True)
    p.add_argument("--test-department-id", required=True)
    p.add_argument("--test-contact-id", required=True)
    p.add_argument("--department-id", help="the real department, once the support lead has named it")
    p.add_argument("--unverified-contact-id", help='the "Unverified AI chat" contact')
    return p


def describe_contact(answer: Any) -> str:
    """Whether a contact exists and has a number. Never the number."""
    if not isinstance(answer, dict) or not answer.get("id"):
        return "not found"
    return "found, %s" % ("has a number" if answer.get("phone") or answer.get("mobile") else "no number")


def _fields(value: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(value, dict):
        if "apiName" in value:
            yield value
            return
        for item in value.values():
            yield from _fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from _fields(item)


def field_lines(masked: Any) -> List[str]:
    """One line per field in masked layout details: API name, label, whether it
    is mandatory or custom, and its pick-list values when they were kept."""
    lines = []
    for field in _fields(masked):
        values = field.get("allowedValues")
        if isinstance(values, dict):
            shown = "%s values withheld" % values.get("count", 0)
        elif isinstance(values, list):
            shown = ", ".join(str(v.get("value") if isinstance(v, dict) else v) for v in values)
        else:
            shown = ""
        flags = ("mandatory " if field.get("isMandatory") or field.get("isSystemMandatory") else "") + (
            "custom " if field.get("isCustomField") else "")
        lines.append("  %-30s %-30s %s%s" % (field.get("apiName"), field.get("displayLabel"), flags,
                                             ("values: " + shown) if shown else ""))
    return lines


def layout_details(client: ScriptClient, out: Callable[[str], None], label: str,
                   params: Dict[str, Any]) -> Dict[str, Any]:
    """A module's layouts, and each one's sections and fields."""
    listed = step(out, "%s layouts" % label, lambda: client.get("/api/v1/layouts", params))
    details = [step(out, "%s layout %s" % (label, row.get("id")),
                    lambda row=row: client.get("/api/v1/layouts/%s" % row.get("id")))
               for row in rows(listed)]
    return {"layouts": listed, "details": details}


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass, http: Any = None,
         out: Callable[[str], None] = print, shapes_dir: Path = SHAPES) -> int:
    args = parser().parse_args(argv)
    client_id = ask_secret("Zoho client id: ", ask)
    client_secret = ask_secret("Zoho client secret: ", ask)
    refresh_token = ask_secret("Zoho refresh token: ", ask)
    http = http if http is not None else DeskHTTP()
    source = source_note("probe.py")

    # One token request of our own, only to read what Zoho says it granted.
    try:
        _, answer = post_form(http, TOKEN_URL, refresh_form(client_id, client_secret, refresh_token))
    except ZohoError as exc:
        out("token: refused (error=%s)" % exc.error)
        return 1
    if answer.get("error"):
        out("token: refused (error=%s)" % answer["error"])
        return 1
    if not says_india(answer):
        out("token: Zoho's answer does not name the India data centre. Stopping.")
        return 1
    save_shape("token", answer, source, shapes_dir)
    granted = granted_scopes(answer)
    out("scopes granted: %s" % (", ".join(granted) if granted else "not stated in Zoho's answer"))
    missing = [scope for scope in SCOPES if scope not in granted]
    if granted and missing:
        out("scopes MISSING: %s" % ", ".join(missing))

    client = ScriptClient(script_settings(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token, org_id=args.org_id,
        department_id=args.test_department_id, contact_id=args.test_contact_id), http)

    orgs = step(out, "organisations", lambda: client.get("/api/v1/organizations", org=False))
    mine = [row for row in rows(orgs) if str(row.get("id")) == str(args.org_id)]
    if not mine:
        out("The token cannot see organisation %s. Stopping." % args.org_id)
        return 1
    out("organisation %s: found, edition %s" % (args.org_id, mine[0].get("edition")))
    save_shape("organizations", orgs, source, shapes_dir)

    departments = step(out, "departments", lambda: client.get("/api/v1/departments", {"limit": 100}))
    if departments is not None:
        save_shape("departments", departments, source, shapes_dir)
    names = {str(row.get("id")): str(row.get("name")) for row in rows(departments)}
    out("departments: %d" % len(names))
    for label, department_id in (("test", args.test_department_id), ("real", args.department_id)):
        if not department_id:
            continue
        name = names.get(str(department_id))
        out("%s department %s: %s" % (label, department_id, name or "NOT FOUND"))
        if label == "test" and name != TEST_DEPARTMENT_NAME:
            out("  test_ticket.py writes only to a department named %r." % TEST_DEPARTMENT_NAME)
        found = layout_details(client, out, "%s ticket" % label, {"module": "tickets", "departmentId": department_id})
        save_shape("ticket-layouts-%s" % label, found, source, shapes_dir)
        for line in field_lines(mask(found["details"])):
            out(line)

    found = layout_details(client, out, "contact", {"module": "contacts"})
    save_shape("contact-layout", found, source, shapes_dir)
    out("contact fields:")
    for line in field_lines(mask(found["details"])):
        out(line)

    channels = step(out, "channels", lambda: client.get("/api/v1/channels"))
    if channels is not None:
        save_shape("channels", channels, source, shapes_dir)
    out("channels: %s" % (", ".join(str(row.get("name")) for row in rows(channels)) or "none listed"))

    contacts: Dict[str, Any] = {}
    for label, contact_id in (("test", args.test_contact_id), ("unverified", args.unverified_contact_id)):
        if contact_id:
            contacts[label] = step(out, "%s contact" % label, lambda: client.get("/api/v1/contacts/%s" % contact_id))
            out("%s contact %s: %s" % (label, contact_id, describe_contact(contacts[label])))
    save_shape("contacts", contacts, source, shapes_dir, person=True)

    credits = getattr(http, "last_credits_remaining", None)
    out("API credits left today: %s" % (credits if credits is not None else "not reported"))
    out("Masked shapes written to %s. Ask Claude to review them." % shapes_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `scripts/zoho/test_ticket.py`:

```python
"""Write one test ticket in Zoho Desk and read it back (spec 2026-10-05, section 10, person step 6).

    python scripts/zoho/test_ticket.py --org-id <org id> --department-id <test department id> \
        --contact-id <test contact id> --cf-chat-reference <API name> --cf-source <API name>

    # Person step 11 only, with Sachin watching: one ticket in the real department.
    python scripts/zoho/test_ticket.py ... --department-id <real department id> --real-department

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards. Run probe.py first. This script refuses any department
that is not the one named "AI chatbot test" in docs/api-shapes/zoho-departments.json,
unless --real-department is given and the person types that department's name.

On the test contact, with fake data only (the test number +919999999999 and a
fixture frame number), it:
  1. searches contacts by the test number (read only, to record the shape);
  2. creates a ticket built exactly as the worker builds one, with the chat
     reference stage:EM-TEST-<n>;
  3. reads it back;
  4. runs the worker's look-up (the contact's tickets, matched on the chat
     reference by find_adoptable) at once and after two minutes, and makes a
     second ticket only if both find nothing;
  5. adds a private comment, then uploads a small image and files of 19 MB and
     26 MB, to find Zoho's attachment limit;
  6. saves the masked shapes to docs/api-shapes/zoho-*.json.
The real-department run stops after step 4 and never makes a second ticket.
Close the ticket (or tickets) in Desk afterwards.
"""

from __future__ import annotations

import argparse
import getpass
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from _common import (
    SHAPES, TEST_PHONE, ScriptClient, ask_secret, department_names, department_refusal, save_shape,
    script_settings, source_note,
)
from emotorad_ai.tickets.clock import now_iso
from emotorad_ai.tickets.seam import DeskTicketSystem
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools.fixtures import WARRANTY_RECORDS
from emotorad_ai.zoho.desk import find_adoptable
from emotorad_ai.zoho.errors import ZohoError, ZohoTooLarge
from emotorad_ai.zoho.payload import ticket_payload

# A fixture bike: invented data, so the test ticket names no real frame.
_FIXTURE_BIKE = WARRANTY_RECORDS["+919876543210"][0]
FRAME_NUMBER = _FIXTURE_BIKE["frame_number"]
BIKE_MODEL = _FIXTURE_BIKE["product_name"]
MB = 1024 * 1024


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--org-id", required=True)
    p.add_argument("--department-id", required=True)
    p.add_argument("--contact-id", required=True, help="the test contact, also with --real-department")
    p.add_argument("--cf-chat-reference", required=True, help="API name of the Chat reference field, from the probe")
    p.add_argument("--cf-source", required=True, help="API name of the Source field, from the probe")
    p.add_argument("--priority-high", default="High")
    p.add_argument("--priority-medium", default="Medium")
    p.add_argument("--channel", default="Chat")
    p.add_argument("--env", default="stage", help="the chat reference's prefix")
    p.add_argument("--n", type=int, help="the EM-TEST number. Defaults to the clock's seconds")
    p.add_argument("--real-department", action="store_true")
    return p


def fake_record(n: int, environment: str, mode: str) -> Dict[str, Any]:
    """A ticket record made by the service's own seam, so the payload built
    from it is the one the worker would send. Fake data only. The reference
    becomes EM-TEST-<n>, which no real record can have."""
    store = InMemoryTicketStore()
    desk = DeskTicketSystem(store, mode, environment)
    now = now_iso()
    made = desk.create(
        source_key="test_ticket:%d" % n, persona="customer", kind="support",
        conversation_id="test-ticket-%d" % n, started_at=now, cluster_id=None, channel="website_chat",
        phone=TEST_PHONE, identity="verified", category="battery_charging", severity="normal",
        description="Test ticket from scripts/zoho/test_ticket.py, fake data only. Close it in Desk.",
        frame_number=FRAME_NUMBER, frame_number_source=None, bike_model=BIKE_MODEL, coverage="computed",
        customer_name=None,
    )
    record = dict(store.get(made["ticket_id"]))
    record["_id"] = "EM-TEST-%d" % n
    record["chat_reference"] = "%s:EM-TEST-%d" % (environment, n)
    return record


def look_up(desk: Any, contact_id: str, department_id: str, cf_api_name: str, chat_reference: str,
            waits: Sequence[float] = (0, 120), sleep: Callable[[float], None] = time.sleep,
            out: Callable[[str], None] = print) -> List[Optional[Dict[str, Any]]]:
    """The worker's look-up after an unknown outcome (spec section 4, step 2):
    the contact's tickets in this department, matched exactly on the chat
    reference by the shared find_adoptable. Run at once and again after two
    minutes, to learn whether Zoho's list lags."""
    found: List[Optional[Dict[str, Any]]] = []
    elapsed = 0.0
    for wait in waits:
        if wait:
            out("Looking again in %d seconds..." % wait)
            sleep(wait)
            elapsed += wait
        match = find_adoptable(desk.contact_tickets(contact_id, department_id), cf_api_name, chat_reference)
        out("look-up at %d s: %s" % (elapsed, "found #%s" % match.get("ticketNumber") if match else "not found"))
        found.append(match)
    return found


def needs_second_create(found: Sequence[Optional[Dict[str, Any]]]) -> bool:
    return not any(found)


def tiny_png(size: int = 8) -> bytes:
    """A small grey square, made here so no file is read from disk."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    pixels = b"".join(b"\x00" + b"\x80" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def filler(megabytes: int) -> bytes:
    return bytes(megabytes * MB)


def uploads(reference: str) -> List[Tuple[str, bytes, str]]:
    """A small image, then files either side of 20 MB, to find Zoho's limit
    (spec "Open until part 1"). Names start with the reference, as the worker's do."""
    return [
        ("%s-small.png" % reference, tiny_png(), "image/png"),
        ("%s-19mb.bin" % reference, filler(19), "application/octet-stream"),
        ("%s-26mb.bin" % reference, filler(26), "application/octet-stream"),
    ]


def upload(desk: Any, ticket_id: str, name: str, data: bytes, mime: str) -> str:
    """What happened to one upload, in words. A refusal is the finding, not a failure."""
    try:
        return "attached (id %s)" % desk.upload_attachment(ticket_id, name, data, mime)
    except ZohoTooLarge as exc:
        return "refused as too large (error=%s)" % exc.error
    except ZohoError as exc:
        return "failed (error=%s)" % exc.error


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         typed: Callable[[str], str] = input, http: Any = None, out: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, shapes_dir: Path = SHAPES) -> int:
    args = parser().parse_args(argv)
    typed_name = typed("Type the real department's name exactly as Desk shows it: ") if args.real_department else None
    refusal = department_refusal(department_names(shapes_dir), args.department_id, args.real_department, typed_name)
    if refusal:
        out(refusal)
        return 1
    settings = script_settings(
        client_id=ask_secret("Zoho client id: ", ask), client_secret=ask_secret("Zoho client secret: ", ask),
        refresh_token=ask_secret("Zoho refresh token: ", ask), org_id=args.org_id,
        department_id=args.department_id, contact_id=args.contact_id, live=args.real_department,
        environment=args.env, cf_chat_reference=args.cf_chat_reference, cf_source=args.cf_source,
        priority_high=args.priority_high, priority_medium=args.priority_medium, channel=args.channel)
    client = ScriptClient(settings, http)
    n = args.n if args.n is not None else int(time.time())
    record = fake_record(n, args.env, "live" if args.real_department else "test")
    payload = ticket_payload(record, settings, args.contact_id)
    if str(payload.get("departmentId")) != str(args.department_id):
        out("The payload names another department. Nothing was sent.")
        return 1
    created: Dict[str, Any] = {}
    try:
        return _run(client, args, record, payload, created, source_note("test_ticket.py"), shapes_dir, sleep, out)
    except ZohoError as exc:
        where = " Close ticket #%s in Desk." % created.get("ticketNumber") if created else ""
        out("Stopped: error=%s.%s" % (exc.error, where))
        return 1


def _run(client: ScriptClient, args: argparse.Namespace, record: Dict[str, Any], payload: Dict[str, Any],
         created: Dict[str, Any], source: str, shapes_dir: Path, sleep: Callable[[float], None],
         out: Callable[[str], None]) -> int:
    last_ten = TEST_PHONE[-10:]
    save_shape("contact-search", client.get("/api/v1/contacts/search", {"phone": "*" + last_ten, "limit": 10}),
               source, shapes_dir, person=True)
    for field in ("phone", "mobile"):
        out("contacts whose %s ends %s: %d" % (field, last_ten[-4:], len(client.desk.search_contacts(field, last_ten))))

    created.update(client.desk.create_ticket(payload))
    ticket_id = created["id"]
    out("Created ticket #%s with chat reference %s." % (created.get("ticketNumber"), record["chat_reference"]))

    ticket = client.get("/api/v1/tickets/%s" % ticket_id)
    save_shape("ticket", ticket, source, shapes_dir)
    on_ticket = ((ticket or {}).get("cf") or {}).get(args.cf_chat_reference)
    out("chat reference on the ticket: %s" % ("matches" if on_ticket == record["chat_reference"] else "MISSING OR DIFFERENT"))
    out("frame number in the description: %s" % ("yes" if FRAME_NUMBER in str((ticket or {}).get("description")) else "NO"))

    save_shape("contact-tickets", client.get("/api/v1/contacts/%s/tickets" % args.contact_id,
                                             {"departmentId": args.department_id, "limit": 50}), source, shapes_dir)
    found = look_up(client.desk, args.contact_id, args.department_id, args.cf_chat_reference,
                    record["chat_reference"], sleep=sleep, out=out)
    if not needs_second_create(found):
        out("The look-up found the first ticket, so no second ticket was made.")
    elif args.real_department:
        out("The look-up found nothing. No second ticket in the real department. Tell Claude the list lags.")
    else:
        second = client.desk.create_ticket(payload)
        out("The look-up found nothing, so a second ticket was made (#%s): the list lags. Close both."
            % second.get("ticketNumber"))

    if args.real_department:
        out("Real department: one ticket only. Close #%s in Desk once it has been checked." % created.get("ticketNumber"))
        return 0

    comment_id = client.desk.add_comment(
        ticket_id, "[%s test comment] Private, plain text. Nothing to do." % record["chat_reference"])
    save_shape("comment", client.get("/api/v1/tickets/%s/comments/%s" % (ticket_id, comment_id)), source, shapes_dir)
    for name, data, mime in uploads(record["_id"]):
        out("upload %-26s %s" % (name, upload(client.desk, ticket_id, name, data, mime)))
    save_shape("attachment", client.get("/api/v1/tickets/%s/attachments" % ticket_id), source, shapes_dir)
    out("Done. Close ticket #%s in Desk now." % created.get("ticketNumber"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `scripts/zoho/revoke.py`:

```python
"""Revoke the chatbot's Zoho refresh token (spec 2026-10-05, section 10, rollback).

    python scripts/zoho/revoke.py

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

It asks for the refresh token by hidden input, and posts it to
https://accounts.zoho.in/oauth/v2/revoke/token as a form field, never in the
address, as Zoho's revoke page documents. Only that token stops working.
Revoking through Zoho's Connected Apps page works per app, and could revoke the
OMS's token too, so do not use it. Never give this script the OMS's token.

After revoking, remove EMOTORAD_ZOHO_REFRESH_TOKEN from the config store and
redeploy (docs/runbooks/config-store.md, section 7). Until then /health says
"token refused".
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any, Callable, List, Optional

from _common import ask_secret, revoke
from emotorad_ai.zoho.http import DeskHTTP


def parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__.splitlines()[0])


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         typed: Callable[[str], str] = input, http: Any = None, out: Callable[[str], None] = print) -> int:
    parser().parse_args(argv)
    token = ask_secret("The chatbot's refresh token to revoke: ", ask)
    if typed("Type REVOKE to revoke it. This cannot be undone: ").strip() != "REVOKE":
        out("Nothing revoked.")
        return 1
    done, said = revoke(http if http is not None else DeskHTTP(), token)
    out("Revoked. The chatbot can no longer reach Zoho with that token." if done else "Not revoked (error=%s)." % said)
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `scripts/zoho/tickets_report.py`:

```python
"""Waiting, stuck and held chatbot tickets, for the support lead (spec 2026-10-05, section 10).

    python scripts/zoho/tickets_report.py
    python scripts/zoho/tickets_report.py --mode live

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

Read only, against the deployment's store: EMOTORAD_STORE=mongodb and
EMOTORAD_MONGO_URI (never printed), read as the service reads them. For each
record still waiting, stuck or held, it prints our reference, the Zoho ticket
number, the state, the mode and the last four digits of the number to call
back. Those customers were told someone would be in touch, so run it before
stopping or rolling back the Zoho integration, and give the list to the
support lead.

The mode is the deployment's: live when EMOTORAD_ZOHO_LIVE is exactly "yes",
test otherwise. Records of the other mode are listed as held.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.conversation import StoreUnavailable  # noqa: E402
from emotorad_ai.wiring import build_stores  # noqa: E402

_ROW = "%-12s %-10s %-8s %-5s %s"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=("test", "live"),
                   default="live" if os.environ.get("EMOTORAD_ZOHO_LIVE") == "yes" else "test")
    return p


def lines(rows: List[Dict[str, Any]]) -> List[str]:
    """The listing as text. Only ever the last four digits of a number."""
    out = [_ROW % ("reference", "zoho", "state", "mode", "number")]
    for row in rows:
        last_four = row.get("last_four")
        out.append(_ROW % (row.get("reference"), row.get("zoho_number") or "-", row.get("state"), row.get("mode"),
                           "..." + str(last_four)[-4:] if last_four else "-"))
    out.append("%d record(s)" % len(rows))
    return out


def main(argv: Optional[List[str]] = None, out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    settings = load_settings()
    if settings.store != "mongodb":
        out("EMOTORAD_STORE is %r, not mongodb: there is no deployment store to read." % settings.store)
        return 1
    try:
        rows = build_stores(settings).tickets.listing(args.mode)
    except StoreUnavailable as exc:
        out("The store could not be read (%s). Check EMOTORAD_MONGO_URI and the Atlas access list."
            % type(exc).__name__)
        return 1
    for line in lines(rows):
        out(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `infra/zoho-alarms.yaml`:

```yaml
# infra/zoho-alarms.yaml
# Alarms on the Zoho Desk ticket events (spec 2026-10-05, section 8): one
# CloudWatch Logs metric filter and one alarm per event, on the deployment's
# log group, emailing one named person through an SNS topic. Deployed by a
# person (person step 9), never by a workflow or a chat session.
#
#   aws cloudformation deploy --profile emotorad-staging --region ap-south-1 \
#     --stack-name emotorad-ai-stage-zoho-alarms --template-file infra/zoho-alarms.yaml \
#     --parameter-overrides LogGroupName=emotorad-ai-stage AlarmEmail=<their @emotorad.com address>
#
# AWS emails that address once to confirm the subscription. No alarm reaches
# it until its owner confirms.
#
# Every event is one JSON line on the container's stdout (observability.EventLog,
# with EMOTORAD_AI_LOG_STDOUT=1 in the Dockerfile), which the awslogs driver
# writes to the log group. So each filter matches the line's "event" field.
# An alarm fires on one event in its period. The worker logs a stuck or late
# record once an hour, so a stuck record means one email an hour until it is sent.
AWSTemplateFormatVersion: "2010-09-09"
Description: EMotorad AI platform - alarms on the Zoho Desk ticket events

Parameters:
  LogGroupName:
    Type: String
    Default: emotorad-ai-stage
    Description: The deployment's log group, the awslogs-group on the workflow's docker run line
  AlarmEmail:
    Type: String
    AllowedPattern: '^[^@\s]+@emotorad\.com$'
    ConstraintDescription: an @emotorad.com address
    Description: Who receives the alarms (person step 9)

Resources:
  AlarmTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: !Sub "${LogGroupName}-zoho-alarms"
      Subscription:
        - Protocol: email
          Endpoint: !Ref AlarmEmail
      Tags:
        - Key: service
          Value: emotorad-ai

  ZohoMisconfiguredFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "zoho_misconfigured" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: zoho_misconfigured
          MetricValue: "1"

  ZohoMisconfiguredAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-zoho_misconfigured"
      AlarmDescription: "Zoho is switched on but a start-up check failed, so tickets go to the mock and nothing reaches Desk. /health says why."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: zoho_misconfigured
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

  ZohoTokenRefusedFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "zoho_token_refused" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: zoho_token_refused
          MetricValue: "1"

  ZohoTokenRefusedAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-zoho_token_refused"
      AlarmDescription: "Zoho refused the chatbot's refresh token, for example after the OMS client secret was rotated without updating ours. Nothing reaches Desk until it is fixed."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: zoho_token_refused
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

  ZohoWorkerErrorFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "zoho_worker_error" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: zoho_worker_error
          MetricValue: "1"

  ZohoWorkerErrorAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-zoho_worker_error"
      AlarmDescription: "The Zoho worker hit an unexpected error in a pass. It carries on; the log line names the error class."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: zoho_worker_error
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

  ZohoTicketStuckFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "zoho_ticket_stuck" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: zoho_ticket_stuck
          MetricValue: "1"

  ZohoTicketStuckAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-zoho_ticket_stuck"
      AlarmDescription: "A ticket has waited 24 hours to reach Zoho. Run scripts/zoho/tickets_report.py and tell the support lead."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: zoho_ticket_stuck
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

  SafetyTicketLateFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "safety_ticket_late" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: safety_ticket_late
          MetricValue: "1"

  SafetyTicketLateAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-safety_ticket_late"
      AlarmDescription: "A safety ticket has waited 10 minutes to reach Zoho. Ask the support lead to call the customer now."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: safety_ticket_late
      Statistic: Sum
      Period: 60
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

  SafetyTicketNotRecordedFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "safety_ticket_not_recorded" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: safety_ticket_not_recorded
          MetricValue: "1"

  SafetyTicketNotRecordedAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-safety_ticket_not_recorded"
      AlarmDescription: "A customer reported a safety issue and no ticket could be recorded. Read the conversation and reach the customer."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: safety_ticket_not_recorded
      Statistic: Sum
      Period: 60
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

  UnverifiedTicketCappedFilter:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref LogGroupName
      FilterPattern: '{ $.event = "unverified_ticket_capped" }'
      MetricTransformations:
        - MetricNamespace: !Sub "EMotorad/AI/${LogGroupName}"
          MetricName: unverified_ticket_capped
          MetricValue: "1"

  UnverifiedTicketCappedAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${LogGroupName}-unverified_ticket_capped"
      AlarmDescription: "The daily cap on unverified tickets refused a request. Check for abuse, or whether the cap is too low."
      Namespace: !Sub "EMotorad/AI/${LogGroupName}"
      MetricName: unverified_ticket_capped
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching
      AlarmActions:
        - !Ref AlarmTopic

Outputs:
  TopicArn:
    Value: !Ref AlarmTopic
```

In `docs/runbooks/config-store.md`, replace this block:

```markdown
| `LANGFUSE_PUBLIC_KEY` | `tracing.py`; optional, tracing is off without both Langfuse keys |
| `LANGFUSE_SECRET_KEY` | `tracing.py`; see `docs/runbooks/tracing.md` |
```

with this block:

```markdown
| `LANGFUSE_PUBLIC_KEY` | `tracing.py`; optional, tracing is off without both Langfuse keys |
| `LANGFUSE_SECRET_KEY` | `tracing.py`; see `docs/runbooks/tracing.md` |
| `EMOTORAD_ZOHO_REFRESH_TOKEN` | `zoho/settings.py`. **Secret.** The switch for Zoho Desk tickets: absent means the mock, as before. The chatbot's own refresh token from `scripts/zoho/exchange_code.py`, never the OMS's. See section 7 |
| `EMOTORAD_ZOHO_CLIENT_ID` | `zoho/settings.py`. **Secret.** The OMS's Zoho OAuth client id |
| `EMOTORAD_ZOHO_CLIENT_SECRET` | `zoho/settings.py`. **Secret.** The OMS's client secret. When it is rotated in the OMS, update it here the same day, or `/health` says `token refused: invalid_client_secret` |
| `EMOTORAD_ZOHO_ORG_ID` | `zoho/settings.py`. Not secret; needed with the token. The Desk organisation id |
| `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID` | `zoho/settings.py`. Not secret; needed with the token. The "AI chatbot test" department, where every test-mode ticket goes |
| `EMOTORAD_ZOHO_TEST_CONTACT_ID` | `zoho/settings.py`. Not secret; needed with the token. The "AI chatbot test" contact, on every test-mode ticket |
| `EMOTORAD_ZOHO_CF_CHAT_REFERENCE` | `zoho/settings.py`. Not secret; needed with the token. API name of the "Chat reference" ticket field, from `scripts/zoho/probe.py` |
| `EMOTORAD_ZOHO_CF_SOURCE` | `zoho/settings.py`. Not secret; needed with the token. API name of the "Source" ticket field, from the probe |
| `EMOTORAD_ZOHO_DEPARTMENT_ID` | `zoho/settings.py`. Not secret; live only. The real department |
| `EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID` | `zoho/settings.py`. Not secret; live only. The "Unverified AI chat" contact, on every ticket for a number nobody proved |
| `EMOTORAD_ZOHO_LIVE` | `zoho/settings.py`. Not secret; live only. Exactly `yes` sends to the real department; anything else is test mode. Set only after Sachin's sign-off. Refused while `EMOTORAD_AI_DEV_CODES` is on or the OTP sender is the mock |
| `EMOTORAD_ZOHO_PRIORITY_HIGH` | `zoho/settings.py`. Optional, default `High`: Zoho's priority value for urgent tickets, from the probe |
| `EMOTORAD_ZOHO_PRIORITY_MEDIUM` | `zoho/settings.py`. Optional, default `Medium`: the priority for every other ticket |
| `EMOTORAD_ZOHO_CHANNEL` | `zoho/settings.py`. Optional, default `Chat`: a system channel from the probe, never an integration channel |
| `EMOTORAD_ZOHO_CREDITS_FLOOR` | `zoho/settings.py`. Optional, default `1000`: below this many API credits left today, only urgent tickets are sent |
| `EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB` | `zoho/settings.py`. Optional, default `20`: a photo or video over this is noted on the ticket, not attached |
```

Replace this block, which ends the file:

```markdown
format: the unprefixed id (`claude-opus-5`, `claude-sonnet-5`, …) for `anthropic`, the
`anthropic.`-prefixed id for `bedrock`.
```

with this block:

````markdown
format: the unprefixed id (`claude-opus-5`, `claude-sonnet-5`, …) for `anthropic`, the
`anthropic.`-prefixed id for `bedrock`.

## 7. Zoho Desk tickets

Spec: `docs/superpowers/specs/2026-10-05-zoho-desk-tickets-design.md`. Zoho is off while
`EMOTORAD_ZOHO_REFRESH_TOKEN` is absent: tickets go to the mock and `/health` says
`"zoho":"not configured"`. `EMOTORAD_AI_ENV` (already on the workflow's `docker run` line) is
needed too, because it starts every chat reference, for example `stage:EM-1000001`. The
playground, the CLI and the local chat page never use these settings, even when they are present.

Run every command here in a terminal window outside the Claude app, and clear the scrollback
afterwards. Never paste a value into a chat session.

1. **The collection first.** Run `python scripts/mongo_setup.py` against the environment's
   database, and check that `tickets` lists the `source_key` index. Without it the service
   refuses Zoho and `/health` says `misconfigured: tickets index missing`.
2. **The settings.** Add the eight fields needed with the token: the three secrets,
   `EMOTORAD_ZOHO_ORG_ID`, `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID`, `EMOTORAD_ZOHO_TEST_CONTACT_ID`,
   `EMOTORAD_ZOHO_CF_CHAT_REFERENCE` and `EMOTORAD_ZOHO_CF_SOURCE`. The secret is replaced
   whole, so start from its current value. Write it to a file, never to the screen:

   ```bash
   umask 077
   aws secretsmanager get-secret-value --secret-id /emotorad/stage/ai/app \
     --query SecretString --output text > ~/app-config.json
   # Add the Zoho fields to ~/app-config.json in an editor. Keep every existing field.
   aws secretsmanager put-secret-value --secret-id /emotorad/stage/ai/app \
     --secret-string file://$HOME/app-config.json
   rm -P ~/app-config.json
   ```

   Then check the names only, with the command in section 2.
3. **Deploy** (section 3). `curl -s https://ai-release-stage.emotorad.com/health` should show
   `"zoho":"test department"`. Anything else says what is wrong (see the table below).
4. **The alarms,** once per environment:

   ```bash
   aws cloudformation deploy --stack-name emotorad-ai-stage-zoho-alarms \
     --template-file infra/zoho-alarms.yaml \
     --parameter-overrides LogGroupName=emotorad-ai-stage AlarmEmail=<the person who receives them>
   ```

   AWS emails that address to confirm the subscription. No alarm reaches it until they confirm.
5. **Live** (person step 11 only, after Sachin's sign-off, in an environment with real phone
   verification): add `EMOTORAD_ZOHO_DEPARTMENT_ID`, `EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID` and
   `EMOTORAD_ZOHO_LIVE=yes` by step 2, with that environment's own refresh token.

**To stop sending.** First run `python scripts/zoho/tickets_report.py` and give the support lead
the list: those customers were told someone would be in touch. Then remove
`EMOTORAD_ZOHO_REFRESH_TOKEN` by step 2 and redeploy. To revoke the token itself, run
`python scripts/zoho/revoke.py`.

| `/health` `zoho` | Meaning |
| --- | --- |
| `not configured` | No refresh token: the mock, as before |
| `test department` | Sending to the test department |
| `live` | Sending to the real department |
| `misconfigured: <reason>` | A start-up check failed: the mock is used and nothing is recorded |
| `not allowed in this region` | `AWS_REGION` begins with `eu-`: the mock is used |
| `token refused: <error>` | Zoho refused the refresh token, for example after a secret rotation |
| `sending failing: <code>` | Zoho refused the calls themselves, for example a missing scope |
````

In `docs/contracts/amiigo-support-chat.md`, replace this line:

```markdown
The support chat is built and tested for the website; the Amiigo endpoint, the Amiigo token check and Zoho ticketing are not built yet. Build the app against this contract; anything marked **Proposed** can still change before the endpoint ships, and we will tell you before it does.
```

with this line:

```markdown
The support chat is built and tested for the website; the Amiigo endpoint and the Amiigo token check are not built yet. Zoho ticketing is built and sends to a test department on staging only, until engineering signs off real tickets. Build the app against this contract; anything marked **Proposed** can still change before the endpoint ships, and we will tell you before it does.
```

Replace this line:

```markdown
| Support tickets | Test tickets (`EM-00001`) | Zoho Desk (see "What changes with Zoho") |
```

with this line:

```markdown
| Support tickets | Recorded with our own `EM-` reference and sent to a Zoho Desk test department on staging | Zoho Desk's real department, after sign-off (see "What changes with the Zoho integration") |
```

Replace this line:

```markdown
Tickets move from a test system to Zoho Desk, where the support team works them. The request side of this contract does not change; the response keeps every field and may gain new optional ones. No date is set for the Zoho work yet.
```

with this line:

```markdown
Tickets move from a test system to Zoho Desk, where the support team works them. The request side of this contract does not change; the response keeps every field and may gain new optional ones. The ticket part is built (5 October 2026): each ticket is recorded with our own reference during the reply, and a background worker sends it to Zoho straight after. It goes to a test department on staging until engineering signs off real tickets. The handover wording comes with the next part.
```

Replace these two lines:

```markdown
| `ticket_id` value | Test ids like `EM-00001`, from a counter that restarts with the server | The Zoho Desk ticket number | Treat it as an opaque string: do not parse it or assume a prefix or length |
| Where a ticket goes | A test store nobody works | Zoho Desk, with the chat transcript and links to the rider's photos and videos attached | Nothing |
```

with these three lines:

```markdown
| `ticket_id` value | Test ids like `EM-00001`, from a counter that restarts with the server | Our own reference: `EM-` and seven digits, from `EM-1000001`. Never the Zoho Desk ticket number; the Zoho ticket carries our reference, so support finds it either way | Treat it as an opaque string: do not parse it or assume a prefix or length |
| Where a ticket goes | A test store nobody works | Zoho Desk, with this chat's transcript, and the rider's photos and videos uploaded to the ticket as files. A file over Zoho's size limit (taken as 20 MB until it is tested) is not attached: the ticket says a photo or video was too large, and the AI team keeps it | Nothing |
| When the ticket reaches Zoho | Not applicable | Shortly after the reply. The reply never waits for Zoho, so `ticket_id` is in the reply at once. Later messages in the chat are added to the same ticket | Nothing: show the reference as soon as it arrives |
```

- [ ] **Step 4: Run the module, then the whole suite**

```bash
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_zoho_scripts
.venv/bin/python -m py_compile scripts/zoho/_common.py scripts/zoho/consent_url.py scripts/zoho/exchange_code.py scripts/zoho/probe.py scripts/zoho/test_ticket.py scripts/zoho/revoke.py scripts/zoho/tickets_report.py
for s in consent_url exchange_code probe test_ticket revoke tickets_report; do .venv/bin/python scripts/zoho/$s.py --help >/dev/null || echo "$s failed"; done
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

`--help` exits from argparse before any input or network call.

Expected results:
- `tests.test_zoho_scripts` passes.
- No existing test changes.
- The whole suite shows the previous count plus this module's tests, with only the known environmental failure.

If `AlarmStackTests.test_every_zoho_event_the_code_emits_has_a_decision` fails, it names an event Task 9 or 10 emits in the `zoho_` family that is in neither list. Add it to `NOT_ALARMED`. If the spec alarms it, add it to the stack and to `ALARMED` instead. Never weaken the test.

- [ ] **Step 5: Commit**

```bash
git add scripts/zoho/_common.py scripts/zoho/consent_url.py scripts/zoho/exchange_code.py scripts/zoho/probe.py scripts/zoho/test_ticket.py scripts/zoho/revoke.py scripts/zoho/tickets_report.py infra/zoho-alarms.yaml docs/runbooks/config-store.md docs/contracts/amiigo-support-chat.md tests/test_zoho_scripts.py
git commit -m "feat: Zoho scripts for the person, the alarm stack, runbook and contract" -m "The scripts are consent address, code exchange (India only), read-only probe, test ticket in the test department, revoke and the waiting-tickets report. They run the service's own Desk client and look-up, ask for secrets by hidden input, and save only masked shapes. The alarm stack covers the seven alarmed events on the deployment's log group. The config-store runbook gains every EMOTORAD_ZOHO_* name, and the app contract says what part 3 changes." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Interface issues for the plan author

1. **`tests/fake_zoho.py` is not used by `tests/test_zoho_scripts.py`.** The skeleton names the file but not its API. The tests use two local doubles with the shared signatures instead: `FakeAccounts.call(method, url, headers, body=None, *, write, timeout=None)` and `FakeDesk.contact_tickets(contact_id, department_id, limit=50)`. If Task 7 gives `fake_zoho` a public double, these can switch to it.
2. **`DeskHTTP.call` on the accounts host.** The scripts send the code exchange, the probe's refresh and the revoke through it. They need two things from it:
   - Zoho's HTTP 200 `{"error": ...}` answers come back as `(200, body)`.
   - A non-2xx answer either comes back the same way or raises a `ZohoError` with `.error` set.

   Task 7 must not raise anything without `.error` for these URLs.
3. **`ScriptClient.get` builds the Desk headers itself** (`Authorization: Zoho-oauthtoken <token>` and `orgId`). `DeskClient` has no generic read in the skeleton. Task 7's `DeskClient` must send the same two headers. Better: Task 7 exposes a `headers()` method, or `ACCOUNTS`/`DESK` constants, and `_common.py` imports them instead of repeating `https://accounts.zoho.in` and `https://desk.zoho.in`.
4. **`TicketStore.listing(mode)` row keys** are assumed to be `reference`, `zoho_number`, `state`, `mode` and `last_four` (four digits only). Task 2 must use exactly these. `TicketsReportTests` uses the real `InMemoryTicketStore`, so a mismatch fails there.
5. **`DeskTicketSystem.create(identity="verified", frame_number_source=None, ...)`** is assumed to set the record's `identity` to `verified` and keep the bike. The skeleton lists `identity` as a `create()` keyword but not its values. `InMemoryTicketStore()` is assumed to take no arguments.
6. **`ZohoSettings` is built directly** with every field as a keyword in `script_settings`. The probe passes empty `cf_chat_reference` and `cf_source`. Task 6 must keep its validation in `load_zoho_settings`, not in a `__post_init__`.
7. **`find_adoptable` is assumed to read `ticket["cf"][cf_api_name]`** with an exact match. The look-up tests build tickets that way.
8. **The revoke address.** The spec and this task use `POST https://accounts.zoho.in/oauth/v2/revoke/token` with the form field `token`. Older Zoho pages document `/oauth/v2/token/revoke?token=...`. The person should check Zoho's current revoke page before relying on `revoke.py`. `REVOKE_URL` is the one place to change.
9. **Zoho ids in the event log.** Desk ids are 16 or more digits, so `redact_pii` logs them as `[number]` (the existing `_LONG_DIGITS` rule). Task 9's `zoho_ticket_sent` should log our reference and `ticketNumber`, not Zoho's `id`.
10. **Alarm decisions.** The spec gives no event names for a `gone` ticket or the credits pause. If Task 9 emits `zoho_*` events for them, `NOT_ALARMED` in `tests/test_zoho_scripts.py` grows. Tasks 13 to 15 must emit `safety_ticket_not_recorded` and `unverified_ticket_capped` as `log.emit("<name>", ...)` literals.
11. **Task 11's side effect.** `redact_pii` now turns Devanagari digits into ASCII in all logged and transcript text, not only in numbers it hides. The spec asks for this. No existing test depends on the old behaviour.

---

<!-- drafted as tasks-13-15 -->

### Task 13: Safety without a known phone, a failed safety record, the store-down safety reply, one safety ticket per run, the texts

**Files:**
- Modify: `src/emotorad_ai/guardrails.py:113-127` (split `SAFETY_MESSAGE` into its parts without changing its text; add the section 7 texts)
- Modify: `src/emotorad_ai/runtime.py:27` (typing import), `:75-94` (guardrails import), `:140` (verify_first import), the import lines before `from .disclosure` and `from .tools.mocks`, `:214-222` (new module-level helpers), `:573-583` (`_store_down`), `:684-704` (`_node_safety`), `:1561-1609` (`_handle_safety`, plus new methods after it)
- Create: `tests/test_safety_without_phone.py`
- Test: `tests/test_safety_without_phone.py`

**Interfaces:**
- Consumes: `TicketRouter.records_real_tickets`, `TicketRouter.store`, `TicketRouter.create(source_key=..., persona=..., **fields) -> {"ticket_id", "status"}`, `TicketRouter.add_note(ticket_id, text)`, `MockTicketSystem.add_note` (Task 3); `DeskTicketSystem(store, mode, environment, clock=...)`, `InMemoryTicketStore()`, `TicketStore.by_source_key`, `TicketStore.get` (Tasks 2 and 3); `tickets.kinds.is_urgent`, `is_desk_reference`, `FIRST_DESK_NUMBER`; `tickets.clock.now_iso` (Task 2); record keys `_id`, `kind`, `urgent`, `identity`, `phone`, `source_key`, `bike`, `summary`, `notes` (Task 2); `ConversationState.typed_number`, `.awaiting_callback`, `.callback_asks` (Task 5); `ToolContext.persona` and `.started_at` filled in `_raise_safety_ticket` (Task 1); `digits.ascii_digits` (Task 11); `verify_first.find_phone`, `looks_like_a_number`, `redact` (existing).
- Produces: in `guardrails.py`: `SAFETY_STEPS`, `SAFETY_EMERGENCY`, `SAFETY_MESSAGE` (text unchanged), `SAFETY_NO_CONTACT_MESSAGE`, `SAFETY_NOT_RECORDED_MESSAGE`, `SAFETY_ADDED_MESSAGE`, `NUMBER_RECEIVED_MESSAGE`, `HANDOVER_RECORDED_MESSAGE`, `HANDOVER_ASK_NUMBER_MESSAGE`, `REFERENCE_SUFFIX`, `CAP_PER_NUMBER_MESSAGE`, `CAP_OVERALL_MESSAGE` (the `{reference}` texts are `str.format` templates). In `runtime.py`: `read_number(text) -> TypedNumber(number, shown, attempted)`, `Recorded(reference, refusal=None)`, `PURPOSE_SAFETY = "safety_callback"`, `PURPOSE_HANDOVER = "handover"`, `PURPOSE_LOCKOUT = "lockout"`, `_safety_scan(message) -> (matched, evidence)`, `_as_shown(message, typed)`, `_again_note(matched)`. On `Runtime`: `_desk_store()`, `_gate_key(state, purpose)`, `_record_ticket(message, state, *, kind, purpose, phone, verified, description, cluster_id=None, category=None, severity=None, bike=None) -> Recorded`, `_add_note(conversation_id, ticket_id, text) -> bool`, `_safety_reply(...)`, `_safety_not_recorded(...)`. Events: `ticket_recorded` (`ticket_id`, `kind`, `urgent`), `ticket_note_added`, `ticket_note_failed`, `ticket_record_failed`, `safety_ticket_not_recorded` (`why`, `level="error"`), `safety_without_contact`, `callback_asked`. Test helpers in `tests/test_safety_without_phone.py`: `DeskChat`, `CALL_BACK`, `FAKE`, `ONE_BIKE`, `RIDER`, `PROMISES`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_safety_without_phone.py`:

```python
"""Safety without a known phone, and a safety record that cannot be written
(spec 2026-10-05-zoho-desk-tickets-design.md, sections 6 and 7, part 4).

Every test goes through runtime.handle() and checks that the model was never
called. ScriptedClaude gets no replies, so any model call would raise. The rule
under test: a reply promises a call only when a ticket is behind it.

DeskChat is the helper the part 4 tests share (test_callback_gate.py,
test_handover_tickets.py, test_lockout_tickets.py). It is a web chat with Zoho
on, wired as api.py wires it, over an in-memory ticket store.
"""

import unittest
from datetime import date
from unittest import mock

from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.guardrails import (
    CAP_OVERALL_MESSAGE,
    CAP_PER_NUMBER_MESSAGE,
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
    REFERENCE_SUFFIX,
    SAFETY_ADDED_MESSAGE,
    SAFETY_EMERGENCY,
    SAFETY_MESSAGE,
    SAFETY_NO_CONTACT_MESSAGE,
    SAFETY_NOT_RECORDED_MESSAGE,
    SAFETY_STEPS,
)
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import HANDOVER_TEXT, Runtime, read_number
from emotorad_ai.tickets.clock import now_iso
from emotorad_ai.tickets.kinds import FIRST_DESK_NUMBER, is_desk_reference
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import MockTicketSystem, build_registry
from emotorad_ai.tools.verification import VerificationStore
from tests.test_runtime_persistence import runtime_on, send
from tests.test_verify_first import ConflictedStore

TODAY = date(2026, 10, 5)
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus (fixtures)
TWO_BIKES = fixtures.PHONE_AMIIGO_TEST_RIDER  # two bikes (fixtures)
CALL_BACK = "9999999999"  # the fake number, as a customer types it
FAKE = "+91" + CALL_BACK
RIDER = Identity(strength=VERIFIED, phone=ONE_BIKE, em_aid="aid-1")
# The words a promise of a call is made of. A reply that recorded nothing
# must contain none of them.
PROMISES = ("call you", "in touch", "linked to your account", "reference", "passed this on", "pass you")
# A safety report with a number, as customers write it.
HAZARD_WITH_NUMBER = {
    "english": "my battery is smoking, call me on 99999 99999",
    "hinglish": "battery se dhuan aa raha hai, mera number 9999999999 hai",
    "hindi": "battery smoking, मेरा नंबर ९९९९९९९९९९ है",
}
ORIGINAL_SAFETY_MESSAGE = (
    "Please stop using and stop charging the battery right now, and move it away from "
    "anything flammable and away from people. Do not try to open, repair or charge it "
    "again, and do not put it in water.\n\n"
    "What you have described is a safety issue rather than a normal support question, so "
    "I am handing this to our safety team immediately rather than troubleshooting it here. "
    "They will call you on the number linked to your account.\n\n"
    "If you can see smoke or flames right now, move away from the bike and call emergency "
    "services on 112."
)


class DeskChat:
    """One website visitor with Zoho on. The registry holds a TicketRouter, as
    api.py wires it, over an in-memory ticket store the test can read. With
    zoho=False it holds only the mock, as today."""

    def __init__(self, replies=(), zoho=True, verify_first=True, conversations=None, ticket_clock=None):
        self.store = VerificationStore()
        self.tickets = InMemoryTicketStore()
        self.mock = MockTicketSystem()
        system = (TicketRouter(DeskTicketSystem(self.tickets, "test", "stage", clock=ticket_clock or now_iso),
                               self.mock)
                  if zoho else self.mock)
        self.registry = build_registry(verification=self.store, today=TODAY, ticket_system=system,
                                       account_finder=fixtures.find_account_by_order_code)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = conversations if conversations is not None else InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            self_service_identity=True, phone_resolver=self.store.verified_phone,
            otp_verified_at=self.store.verified_on, verify_first=verify_first,
        )

    def say(self, text, identity=None, cid="c1", attachments=()):
        return self.runtime.handle(InboundMessage(
            conversation_id=cid, persona="customer", channel="website_chat", message_text=text,
            identity=identity or Identity(strength=ANONYMOUS, em_aid="aid-1"), attachments=list(attachments),
        ))

    def state(self, cid="c1"):
        return self.conversations.peek(cid)

    def records(self):
        """Every Desk record, in reference order."""
        found = (self.tickets.get("EM-%d" % n) for n in range(FIRST_DESK_NUMBER, FIRST_DESK_NUMBER + 100))
        return [record for record in found if record is not None]

    def events(self, name):
        return [e for e in self.log.events if e["event"] == name]


class ReadNumberTests(unittest.TestCase):
    def test_a_number_in_each_language_and_shape(self):
        for text, shown in (
            ("call me on 99999 99999", "call me on [phone]"),
            ("mera number 9999999999 hai", "mera number [phone] hai"),
            ("मेरा नंबर ९९९९९९९९९९ है", "मेरा नंबर [phone] है"),
            ("नंबर 9999999999पर", "नंबर [phone]पर"),
            ("+91 99999 99999", "[phone]"),
        ):
            with self.subTest(text=text):
                typed = read_number(text)
                self.assertEqual((typed.number, typed.shown, typed.attempted), (CALL_BACK, shown, False))

    def test_a_quoted_reference_is_neither_a_number_nor_a_try_at_one(self):
        for text in ("my ticket is EM-1000001", "booking BK-0001234 and order ro-1234567"):
            with self.subTest(text=text):
                self.assertEqual(tuple(read_number(text)), (None, text, False))
        # The same digits without a prefix are a try at a number.
        self.assertTrue(read_number("my ticket is 1000001").attempted)

    def test_a_number_that_is_not_an_indian_mobile_is_a_try(self):
        for text, shown in (("12345678", "[number]"), ("call +34 612 345 678", "call [number]")):
            with self.subTest(text=text):
                typed = read_number(text)
                self.assertEqual((typed.number, typed.shown, typed.attempted), (None, shown, True))

    def test_a_reference_beside_a_number_leaves_the_number_to_be_read(self):
        typed = read_number("EM-1000001, call 9999999999")
        self.assertEqual((typed.number, typed.shown), (CALL_BACK, "EM-1000001, call [phone]"))


class AskForANumberTests(unittest.TestCase):
    def test_a_hazard_with_no_number_asks_for_one_and_promises_no_call(self):
        chat = DeskChat()
        reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, reply.text)
        for promise in ("call you", "in touch", "linked to your account", "reference"):
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), ("safety", 0))
        self.assertEqual(len(chat.events("callback_asked")), 1)


class NumberInTheSameMessageTests(unittest.TestCase):
    def test_the_hazard_and_a_number_record_an_urgent_unverified_ticket(self):
        for language, text in HAZARD_WITH_NUMBER.items():
            with self.subTest(language=language):
                chat = DeskChat()
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["urgent"], record["identity"]), ("safety", True, "unverified"))
                self.assertEqual(record["phone"], FAKE)
                self.assertEqual(record["source_key"], "c1:%s:safety_callback" % chat.state().started_at)
                self.assertFalse(record["bike"])
                self.assertNotIn(CALL_BACK, record["summary"])
                self.assertEqual(reply.ticket_id, record["_id"])
                self.assertTrue(is_desk_reference(reply.ticket_id))
                self.assertTrue(reply.escalated)
                self.assertEqual(reply.handled_by, "guardrail:battery_safety")
                self.assertIn(SAFETY_STEPS, reply.text)
                self.assertIn(NUMBER_RECEIVED_MESSAGE.format(reference=reply.ticket_id), reply.text)
                self.assertIn(SAFETY_EMERGENCY, reply.text)
                self.assertIsNone(chat.state().awaiting_callback)
                self.assertEqual(chat.state().typed_number, CALL_BACK)
                (event,) = chat.events("ticket_recorded")
                self.assertEqual((event["ticket_id"], event["kind"], event["urgent"]), (record["_id"], "safety", True))

    def test_the_number_is_phone_in_history_and_transcript(self):
        for language, text in HAZARD_WITH_NUMBER.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say(text)
                history = repr(chat.state().history)
                self.assertIn("[phone]", history)
                for form in (CALL_BACK, "99999 99999", "९९९९९९९९९९"):
                    self.assertNotIn(form, history)
                said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
                self.assertIn("[phone]", said)
                self.assertNotIn("९९९९९९९९९९", said)
                self.assertNotIn(CALL_BACK, said)

    def test_a_typed_number_that_owns_two_bikes_looks_up_no_bike(self):
        chat = DeskChat()
        reply = chat.say("there is smoke from the battery, my number is 9700000010")
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual(record["phone"], TWO_BIKES)
        self.assertFalse(record["bike"])
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertFalse([e for e in chat.events("tool_call") if e["tool"] == "create_support_ticket"])

    def test_a_save_conflict_records_one_ticket(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        store.conflicts = 1
        reply = chat.say("my battery is smoking, my number is 9999999999")
        (record,) = chat.records()
        self.assertEqual(reply.ticket_id, record["_id"])


class OneSafetyTicketPerRunTests(unittest.TestCase):
    def test_a_second_report_adds_a_note_to_the_typed_numbers_ticket(self):
        chat = DeskChat()
        first = chat.say("smoke from my battery, my number is 9999999999")
        second = chat.say("it is still smoking")
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual(second.ticket_id, first.ticket_id)
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn("again", record["notes"][0]["text"])
        self.assertIn(SAFETY_ADDED_MESSAGE.format(reference=first.ticket_id), second.text)
        self.assertNotIn("mobile number", second.text)
        self.assertTrue(second.escalated)

    def test_a_second_report_with_a_known_phone_adds_a_note(self):
        chat = DeskChat()
        first = chat.say("my battery is swollen", identity=RIDER)
        second = chat.say("it is still swollen and hot", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(is_desk_reference(first.ticket_id))
        self.assertIn("I have raised this as a priority safety case, reference %s." % first.ticket_id, first.text)
        self.assertEqual(second.ticket_id, first.ticket_id)
        (record,) = chat.records()
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn(SAFETY_MESSAGE, second.text)


class NotRecordedTests(unittest.TestCase):
    def assert_no_promise(self, reply):
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)

    def test_a_write_that_fails_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("smoke from my battery, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual(event["level"], "error")
        self.assertEqual(chat.records(), [])
        self.assertNotIn(CALL_BACK, repr(chat.state().history))

    def test_a_seam_that_returns_nothing_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", return_value=None):
            reply = chat.say("smoke from my battery, my number is 9999999999")
        self.assert_no_promise(reply)
        self.assertEqual(len(chat.events("safety_ticket_not_recorded")), 1)

    def test_a_ticket_store_that_cannot_be_read_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.tickets, "by_source_key", side_effect=StoreUnavailable("down")):
            reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)

    def test_a_known_phones_ticket_that_cannot_be_raised_promises_no_call(self):
        chat = DeskChat()
        with mock.patch.object(chat.runtime, "_raise_safety_ticket", return_value=None):
            reply = chat.say("my battery is swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assert_no_promise(reply)
        self.assertEqual(len(chat.events("safety_ticket_not_recorded")), 1)


class StoreDownTests(unittest.TestCase):
    class DownStore(InMemoryConversationStore):
        def get(self, conversation_id):
            raise StoreUnavailable("MongoDB find_one failed")

    def test_a_safety_report_while_the_store_is_down_gets_the_steps_and_no_promise(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery is swollen")
        self.assertEqual(runtime.llm.requests, [])
        self.assertEqual(reply.handled_by, "store_unavailable")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn(HANDOVER_TEXT, reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        (event,) = [e for e in runtime.log.events if e["event"] == "safety_ticket_not_recorded"]
        self.assertEqual((event["why"], event["level"]), ("store_unavailable", "error"))

    def test_smoke_seen_in_a_clip_while_the_store_is_down_is_a_safety_report(self):
        runtime = runtime_on(self.DownStore(), [])
        clip = Attachment("video", "s3://customers/clu_1/c1/videos/upl_1.mp4", "video/mp4",
                          summary="White smoke rises from the battery pack.")
        reply = runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text="",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), attachments=[clip],
        ))
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)

    def test_any_other_message_while_the_store_is_down_still_hands_over(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery won't charge")
        self.assertIn(HANDOVER_TEXT, reply.text)
        self.assertTrue(reply.escalated)


class ZohoOffTests(unittest.TestCase):
    def test_no_phone_gets_the_steps_without_a_question_or_a_promise(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("my battery is smoking")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn("mobile number", reply.text)
        for promise in PROMISES:
            self.assertNotIn(promise, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.mock.tickets, {})
        self.assertEqual(len(chat.events("safety_without_contact")), 1)

    def test_a_number_in_the_message_records_nothing(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("smoke from the battery, call me on 9999999999")
        self.assertEqual(chat.mock.tickets, {})
        self.assertIsNone(reply.ticket_id)
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)

    def test_a_known_phone_still_raises_its_ticket_as_before(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("my battery is swollen", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(SAFETY_MESSAGE, reply.text)
        self.assertIn("reference %s." % reply.ticket_id, reply.text)
        self.assertIn(reply.ticket_id, chat.mock.tickets)
        self.assertTrue(reply.escalated)


class TextsTests(unittest.TestCase):
    def test_the_known_phone_text_is_unchanged(self):
        self.assertEqual(SAFETY_MESSAGE, ORIGINAL_SAFETY_MESSAGE)

    def test_the_texts_without_a_record_promise_no_call(self):
        for text in (SAFETY_NOT_RECORDED_MESSAGE, SAFETY_NO_CONTACT_MESSAGE):
            for promise in ("call you", "in touch", "linked to your account", "reference"):
                self.assertNotIn(promise, text)
            self.assertTrue(text.startswith(SAFETY_STEPS))
            self.assertTrue(text.endswith(SAFETY_EMERGENCY))
        self.assertIn("112", SAFETY_NOT_RECORDED_MESSAGE)
        self.assertIn("mobile number", SAFETY_NO_CONTACT_MESSAGE)

    def test_the_new_texts_are_plain(self):
        texts = (SAFETY_NO_CONTACT_MESSAGE, SAFETY_NOT_RECORDED_MESSAGE, SAFETY_ADDED_MESSAGE,
                 NUMBER_RECEIVED_MESSAGE, HANDOVER_RECORDED_MESSAGE, HANDOVER_ASK_NUMBER_MESSAGE,
                 REFERENCE_SUFFIX, CAP_PER_NUMBER_MESSAGE, CAP_OVERALL_MESSAGE)
        for text in texts:
            self.assertNotIn("\u2014", text)  # no em dash
        for text in (SAFETY_ADDED_MESSAGE, NUMBER_RECEIVED_MESSAGE, HANDOVER_RECORDED_MESSAGE, REFERENCE_SUFFIX):
            self.assertIn("EM-1000001", text.format(reference="EM-1000001"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_safety_without_phone
```

Expected: `ImportError: cannot import name 'CAP_OVERALL_MESSAGE' from 'emotorad_ai.guardrails'`. The module fails to load, so no test runs.

- [ ] **Step 3: Implement**

**3a. `src/emotorad_ai/guardrails.py`.** Replace this block:

```python
SAFETY_MESSAGE = (
    "Please stop using and stop charging the battery right now, and move it away from "
    "anything flammable and away from people. Do not try to open, repair or charge it "
    "again, and do not put it in water.\n\n"
    "What you have described is a safety issue rather than a normal support question, so "
    "I am handing this to our safety team immediately rather than troubleshooting it here. "
    "They will call you on the number linked to your account.\n\n"
    "If you can see smoke or flames right now, move away from the bike and call emergency "
    "services on 112."
)
```

with this block (the composed `SAFETY_MESSAGE` is the same string, character for character):

```python
# The safety reply's parts (spec 2026-10-05, section 7). The replies that
# cannot promise a call reuse the same steps and the same 112 line, word for word.
SAFETY_STEPS = (
    "Please stop using and stop charging the battery right now, and move it away from "
    "anything flammable and away from people. Do not try to open, repair or charge it "
    "again, and do not put it in water."
)
SAFETY_EMERGENCY = (
    "If you can see smoke or flames right now, move away from the bike and call emergency "
    "services on 112."
)

# A number we know, and the safety ticket raised (runtime._safety_with_phone).
SAFETY_MESSAGE = (
    SAFETY_STEPS + "\n\n"
    "What you have described is a safety issue rather than a normal support question, so "
    "I am handing this to our safety team immediately rather than troubleshooting it here. "
    "They will call you on the number linked to your account.\n\n" + SAFETY_EMERGENCY
)
```

Then, straight after the existing `HANDOFF_MESSAGE = (...)` block (leave that block as it is), add:

```python
# --- the Zoho Desk handovers (spec 2026-10-05, section 7) ---------------------
# Drafts. The support lead confirms or rewrites them in person step 10, before
# part 4 merges. None of them promises a channel or a time
# (docs/contracts/amiigo-support-chat.md). The ones with {reference} are
# filled with str.format.

# Safety, with no number we know and Zoho on: the steps, then a request for a
# number in place of a promise, then 112. The callback gate waits for the number.
SAFETY_NO_CONTACT_MESSAGE = (
    SAFETY_STEPS + "\n\n"
    "This is a safety issue, so I want our safety team to reach you. Please send me your "
    "mobile number and I'll pass this on straight away.\n\n" + SAFETY_EMERGENCY
)
# Safety when nothing could be recorded (Zoho off with no number, a failed
# write, the store down): the steps and 112, and no promise of a call.
SAFETY_NOT_RECORDED_MESSAGE = SAFETY_STEPS + "\n\n" + SAFETY_EMERGENCY
# A later report in a run whose safety ticket exists: added to it as a note.
SAFETY_ADDED_MESSAGE = "I've added this to your safety case. Your reference is {reference}."
# A call-back number received and its ticket recorded.
NUMBER_RECEIVED_MESSAGE = "Thank you. I've passed this on. Your reference is {reference}."
# Talk to a person: the ticket recorded, or the question when no number is known.
HANDOVER_RECORDED_MESSAGE = (
    "I've passed this conversation to our support team, so you won't need to repeat yourself. "
    "They will be in touch. Your reference is {reference}."
)
HANDOVER_ASK_NUMBER_MESSAGE = "I can pass you to our support team. What mobile number can they reach you on?"
# After the verify step's lock-out text, once its ticket is recorded.
REFERENCE_SUFFIX = " Your reference is {reference}."
# The caps on unverified tickets that are not urgent.
CAP_PER_NUMBER_MESSAGE = "I can't take another request for that number today."
CAP_OVERALL_MESSAGE = "I can't pass this on right now. Please try again tomorrow."
```

**3b. `src/emotorad_ai/runtime.py`, imports.** Replace:

```python
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
```

with:

```python
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple
```

Replace:

```python
    ORDER_BLOCKED_MESSAGE,
    SAFETY_MESSAGE,
    CoverageCheck,
```

with:

```python
    NUMBER_RECEIVED_MESSAGE,
    ORDER_BLOCKED_MESSAGE,
    SAFETY_ADDED_MESSAGE,
    SAFETY_EMERGENCY,
    SAFETY_MESSAGE,
    SAFETY_NO_CONTACT_MESSAGE,
    SAFETY_NOT_RECORDED_MESSAGE,
    SAFETY_STEPS,
    CoverageCheck,
```

Replace:

```python
from .disclosure import apply_disclosure
```

with:

```python
from .digits import ascii_digits
from .disclosure import apply_disclosure
```

Replace:

```python
from .tools.mocks import (
    CREATE_SUPPORT_TICKET,
```

with the lines below. If Task 5 already imports from `.tickets.kinds`, add `is_urgent` to that import and do not add a second line.

```python
from .tickets.kinds import is_urgent
from .tools.mocks import (
    CREATE_SUPPORT_TICKET,
```

Replace:

```python
from .verify_first import CONFIRMED, NUMBER, VerifyFirst
```

with:

```python
from .verify_first import CONFIRMED, NUMBER, VerifyFirst, find_phone, looks_like_a_number, redact
```

**3c. `runtime.py`, module-level helpers.** Replace:

```python
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", summary) if p.strip()]
    return [p for p in pieces if check_safety_in_description(p).triggered]


_REFERENCE = re.compile(r"\b[A-Z]{2,4}-\d{3,}\b")
```

with:

```python
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", summary) if p.strip()]
    return [p for p in pieces if check_safety_in_description(p).triggered]


def _safety_scan(message: InboundMessage) -> Tuple[List[str], List[str]]:
    """The safety terms a message trips, and the hazard sentences of any clip
    description that tripped them. The typed text gets the plain scan. A
    video's description (Attachment.summary, written by the video analyser at
    ingest) gets the negation-aware one, because an analyser that writes "no
    smoke visible" is describing a safe clip. One function, so the safety gate
    and the store-down reply judge a message the same way."""
    matched = list(check_safety(message.message_text).matched)
    evidence: List[str] = []
    for attachment in message.attachments:
        if attachment.summary:
            verdict = check_safety_in_description(attachment.summary)
            matched += [m for m in verdict.matched if m not in matched]
            if verdict.triggered:
                evidence += _hazard_sentences(attachment.summary)
    return matched, evidence


def _again_note(matched: Sequence[str]) -> str:
    """The note a later safety report in the same run adds to its ticket. Built
    by code, with no customer text: the transcript carries their words."""
    return "Customer reported a safety issue again. Matched safety indicators: %s." % ", ".join(matched)


# The purposes of the tickets the runtime's gates record (spec 2026-10-05,
# section 6). Each is the last part of the source key
# "<conversation_id>:<started_at>:<purpose>", so a run gets one ticket per need
# and a retry gets the same one.
PURPOSE_SAFETY = "safety_callback"
PURPOSE_HANDOVER = "handover"
PURPOSE_LOCKOUT = "lockout"


class Recorded(NamedTuple):
    """What a gate's recording came to: the reference, or None when nothing
    was recorded. When a cap on unverified tickets refused it, `refusal` is
    the text to send instead, so the caller promises nothing."""

    reference: Optional[str]
    refusal: Optional[str] = None


class TypedNumber(NamedTuple):
    """A call-back number read from a customer's message (read_number)."""

    number: Optional[str]  # ten digits: the first valid Indian mobile, or None
    shown: str  # the text as the model's history and the transcript keep it
    attempted: bool  # no valid mobile, but something that looked like a number


# Our ticket, booking and order references, which a customer may quote. They
# are blanked at the same length before a number is read, so a quoted
# EM-1000001 is neither a mobile nor a failed try at one. The left side checks
# for ASCII letters and digits on purpose: no \b next to text that may be
# Devanagari.
_QUOTED_REFERENCE = re.compile(r"(?<![A-Za-z0-9])(?:EM|BK|RO)-\d+", re.IGNORECASE)
# A try at a number: seven or more digits, however spaced. This is the shape
# verify_first.looks_like_a_number counts.
_NUMBER_TRY = re.compile(r"\+?\d[\d \-]{5,}\d")


def read_number(text: str) -> TypedNumber:
    """The call-back number in a customer's message (spec 2026-10-05, section 6).

    Digits in any script are read as ASCII first (Devanagari ९८७६…), quoted
    references are set aside, then the first valid Indian mobile is taken
    (verify_first.find_phone). In the text that is kept, the number becomes
    [phone] and a try that is not a valid Indian mobile becomes [number]. A
    message with neither is kept as typed."""
    plain = ascii_digits(text or "")
    probe = _QUOTED_REFERENCE.sub(lambda m: " " * len(m.group()), plain)
    found = find_phone(probe)
    if found is not None:
        number, span = found
        return TypedNumber(number, redact(plain, span, "[phone]"), False)
    if not looks_like_a_number(probe):
        return TypedNumber(None, text or "", False)
    shown = plain
    tries = [m.span() for m in _NUMBER_TRY.finditer(probe) if sum(ch.isdigit() for ch in m.group()) >= 7]
    for start, end in reversed(tries):
        shown = shown[:start] + "[number]" + shown[end:]
    return TypedNumber(None, shown, True)


def _as_shown(message: InboundMessage, typed: TypedNumber) -> InboundMessage:
    """The message as the model's history and the transcript keep it."""
    return message if typed.shown == (message.message_text or "") else replace(message, message_text=typed.shown)


_REFERENCE = re.compile(r"\b[A-Z]{2,4}-\d{3,}\b")
```

**3d. `runtime.py`, `_store_down`.** Replace:

```python
    def _store_down(self, message: InboundMessage, exc: Exception, ticket_id: Optional[str] = None) -> Reply:
        """The store cannot be reached: hand over, never start from blank. A
        ticket the turn already raised is named, so the customer can quote it."""
        self.log.emit("store_unavailable", message.conversation_id, error=str(exc))
        self.log.escalation(message.conversation_id, "store_unavailable", ticket_id)
        # A throwaway state, so the AI disclosure is always added: we cannot
        # know whether this person has already seen it.
        text = HANDOVER_TEXT + ("\n\nYour reference is %s." % ticket_id if ticket_id else "")
        text = apply_disclosure(text, ConversationState(conversation_id=message.conversation_id), message.channel)
        return Reply(conversation_id=message.conversation_id, text=text, handled_by="store_unavailable",
                     escalated=True, ticket_id=ticket_id)
```

with:

```python
    def _store_down(self, message: InboundMessage, exc: Exception, ticket_id: Optional[str] = None) -> Reply:
        """The store cannot be reached: hand over, never start from blank. A
        ticket the turn already raised is named, so the customer can quote it.

        A safety report is the exception (spec 2026-10-05, section 6). With
        the store down no safety ticket could be recorded, so it gets the
        safety steps and 112 and no promise of a call. Unless the turn already
        raised a ticket, this is logged as safety_ticket_not_recorded, which
        is alarmed."""
        cid = message.conversation_id
        self.log.emit("store_unavailable", cid, error=str(exc))
        reference = "\n\nYour reference is %s." % ticket_id if ticket_id else ""
        matched, _ = _safety_scan(message)
        metadata: Dict[str, Any] = {}
        if matched:
            if ticket_id:
                self.log.escalation(cid, "store_unavailable", ticket_id)
            else:
                self.log.emit("safety_ticket_not_recorded", cid, why="store_unavailable", level="error")
            text, escalated, metadata = SAFETY_NOT_RECORDED_MESSAGE + reference, bool(ticket_id), {"matched": matched}
        else:
            self.log.escalation(cid, "store_unavailable", ticket_id)
            text, escalated = HANDOVER_TEXT + reference, True
        # A throwaway state, so the AI disclosure is always added: we cannot
        # know whether this person has already seen it.
        text = apply_disclosure(text, ConversationState(conversation_id=cid), message.channel)
        return Reply(conversation_id=cid, text=text, handled_by="store_unavailable",
                     escalated=escalated, ticket_id=ticket_id, metadata=metadata)
```

**3e. `runtime.py`, `_node_safety`.** Replace:

```python
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        safety = check_safety(message.message_text)
        matched = list(safety.matched)
        evidence: List[str] = []
        for attachment in message.attachments:
            if attachment.summary:
                verdict = check_safety_in_description(attachment.summary)
                matched += [m for m in verdict.matched if m not in matched]
                if verdict.triggered:
                    evidence += _hazard_sentences(attachment.summary)
        if matched:
            return {"reply": self._handle_safety(message, resolved, state, matched, evidence)}
        return {}
```

with:

```python
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        matched, evidence = _safety_scan(message)
        if matched:
            return {"reply": self._handle_safety(message, resolved, state, matched, evidence)}
        return {}
```

**3f. `runtime.py`, `_handle_safety`.** Replace the whole current method:

```python
    def _handle_safety(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        matched: List[str],
        evidence: Optional[List[str]] = None,
    ) -> Reply:
        self.log.guardrail(message.conversation_id, "battery_safety", matched)

        ticket_id: Optional[str] = None
        if resolved.identity.phone:
            # Deterministic: code decides this ticket exists, not the model.
            description = (
                "Automatic safety escalation. Customer reported: %s. Matched safety "
                "indicators: %s. No troubleshooting was offered."
                % (message.message_text, ", ".join(matched))
            )
            if evidence:
                # The trigger came from the clip, and the typed text may say
                # nothing alarming: the safety team needs what the analyser
                # saw, not just "video attached".
                description += " Seen in the customer's photo or video: %s" % " ".join(evidence)
            ticket_id = self._raise_safety_ticket(message, state, resolved, description)

        text = SAFETY_MESSAGE
        if ticket_id:
            text += "\n\nI have raised this as a priority safety case, reference %s." % ticket_id

        self.log.escalation(message.conversation_id, "battery_safety", ticket_id)
        if evidence:
            # The whole description goes into the transcript, not just the
            # matched lines, so a human reading it later sees what the
            # analyser saw. Same labelled shape the agent path writes, and the
            # same rule for the typed text: a clip sent with no caption must
            # not leave an empty text block behind, because the API rejects it
            # on every later turn of the conversation (staging, 2026-09-22).
            # Every photo, fetched, so it stays in history as a photo (the
            # final review: unfetched, a stored photo read "could not be
            # retrieved" on every later turn); a video only by its text.
            kept = [a for a in message.attachments if a.summary or _is_image(a)]
            content = user_content(replace(message, attachments=kept), self.fetch)
            state.history.append({"role": "user", "content": content})
            self._note_customer_turn(message, state, content)
        return self._finish(
            message, state, text, "guardrail:battery_safety",
            escalated=True, ticket_id=ticket_id, metadata={"matched": matched},
            already_in_history=bool(evidence),
        )
```

with:

```python
    def _handle_safety(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        state: ConversationState,
        matched: List[str],
        evidence: Optional[List[str]] = None,
    ) -> Reply:
        """The safety branch, never a model call (spec 2026-10-05, section 6).
        The reply promises a call only when a ticket is behind it."""
        self.log.guardrail(message.conversation_id, "battery_safety", matched)
        if resolved.identity.phone:
            return self._safety_with_phone(message, resolved, state, matched, evidence)
        if resolved.persona == "customer" and self._desk_store() is not None:
            return self._safety_without_phone(message, resolved, state, matched, evidence)
        # Zoho off, or not a customer, and no number we know: nothing can be
        # recorded. So the steps and 112, with no question and no promise.
        self.log.emit("safety_without_contact", message.conversation_id)
        return self._safety_reply(message, message, state, SAFETY_NOT_RECORDED_MESSAGE, matched, evidence,
                                  outcome="no_contact")

    def _safety_with_phone(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState,
        matched: List[str], evidence: Optional[List[str]],
    ) -> Reply:
        """A number we know (WhatsApp, the app, a code proved, caller ID). The
        ticket goes through create_support_ticket with the run's safety key,
        as before. A later report in the same run gets the same ticket back
        and adds a note to it. If no ticket can be raised, the reply promises
        nothing."""
        cid = message.conversation_id
        # Deterministic: code decides this ticket exists, not the model.
        description = (
            "Automatic safety escalation. Customer reported: %s. Matched safety "
            "indicators: %s. No troubleshooting was offered."
            % (message.message_text, ", ".join(matched))
        )
        if evidence:
            # The trigger came from the clip, and the typed text may say
            # nothing alarming: the safety team needs what the analyser
            # saw, not just "video attached".
            description += " Seen in the customer's photo or video: %s" % " ".join(evidence)
        # The run's ticket before this report. Getting the same id back means
        # the receipt returned the run's safety ticket: this is a repeat.
        held = state.ticket_id
        try:
            ticket_id = self._raise_safety_ticket(message, state, resolved, description)
        except Exception as exc:  # the class only; the customer still gets the steps
            self.log.emit("safety_ticket_failed", cid, error=type(exc).__name__)
            ticket_id = None
        if not ticket_id:
            return self._safety_not_recorded(message, message, state, matched, evidence, why="tool_error")
        if ticket_id == held:
            self._add_note(cid, ticket_id, _again_note(matched))
        self.log.escalation(cid, "battery_safety", ticket_id)
        text = SAFETY_MESSAGE + "\n\nI have raised this as a priority safety case, reference %s." % ticket_id
        return self._safety_reply(message, message, state, text, matched, evidence, outcome="recorded",
                                  escalated=True, ticket_id=ticket_id)

    def _safety_without_phone(
        self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState,
        matched: List[str], evidence: Optional[List[str]],
    ) -> Reply:
        """A customer with no number we know (an anonymous web visitor), with
        Zoho on (spec 2026-10-05, section 6). A number in this message records
        the urgent safety ticket at once, unverified and with no bike looked
        up. Without one, the safety steps ask for a number instead of
        promising a call, and the callback gate waits for it. A report after
        the run's safety ticket was recorded adds a note to that ticket."""
        cid = message.conversation_id
        typed = read_number(message.message_text or "")
        shown = _as_shown(message, typed)
        try:
            held = self._desk_store().by_source_key(self._gate_key(state, PURPOSE_SAFETY))
        except StoreUnavailable:
            return self._safety_not_recorded(message, shown, state, matched, evidence, why="store_unavailable")
        if held is not None:
            if held.get("state") == "gone":
                # Deleted or merged in Desk: quoting it would promise nothing real.
                return self._safety_not_recorded(message, shown, state, matched, evidence, why="ticket_gone")
            reference = held["_id"]
            self._add_note(cid, reference, _again_note(matched))
            state.awaiting_callback, state.callback_asks = None, 0
            self.log.escalation(cid, "battery_safety", reference)
            text = "\n\n".join((SAFETY_STEPS, SAFETY_ADDED_MESSAGE.format(reference=reference), SAFETY_EMERGENCY))
            return self._safety_reply(message, shown, state, text, matched, evidence, outcome="note_added",
                                      escalated=True, ticket_id=reference)
        if typed.number is None:
            state.awaiting_callback, state.callback_asks = "safety", 0
            self.log.emit("callback_asked", cid, purpose="safety")
            return self._safety_reply(message, shown, state, SAFETY_NO_CONTACT_MESSAGE, matched, evidence,
                                      outcome="asked_for_number")
        state.typed_number = typed.number
        description = (
            "Automatic safety escalation. Customer reported: %s. Matched safety indicators: %s. "
            "No troubleshooting was offered. The number to call was typed in the chat and is not verified."
            % (typed.shown, ", ".join(matched))
        )
        if evidence:
            description += " Seen in the customer's photo or video: %s" % " ".join(evidence)
        recorded = self._record_ticket(
            message, state, kind="safety", purpose=PURPOSE_SAFETY, phone="+91" + typed.number,
            verified=False, description=description, cluster_id=resolved.cluster_id,
            category="battery_safety", severity="critical",
        )
        if recorded.reference is None:
            return self._safety_not_recorded(message, shown, state, matched, evidence, why="not_recorded")
        state.awaiting_callback, state.callback_asks = None, 0
        self.log.escalation(cid, "battery_safety", recorded.reference)
        text = "\n\n".join((SAFETY_STEPS, NUMBER_RECEIVED_MESSAGE.format(reference=recorded.reference),
                            SAFETY_EMERGENCY))
        return self._safety_reply(message, shown, state, text, matched, evidence, outcome="recorded",
                                  escalated=True, ticket_id=recorded.reference)

    def _safety_reply(
        self, message: InboundMessage, shown: InboundMessage, state: ConversationState, text: str,
        matched: List[str], evidence: Optional[List[str]], *, outcome: str, escalated: bool = False,
        ticket_id: Optional[str] = None,
    ) -> Reply:
        """Every safety reply. `shown` is the message as the model's history
        and the transcript keep it, with a typed number replaced by [phone]."""
        if evidence:
            # The whole description goes into the transcript, not just the
            # matched lines, so a human reading it later sees what the
            # analyser saw. Same labelled shape the agent path writes, and the
            # same rule for the typed text: a clip sent with no caption must
            # not leave an empty text block behind, because the API rejects it
            # on every later turn of the conversation (staging, 2026-09-22).
            # Every photo, fetched, so it stays in history as a photo (the
            # final review: unfetched, a stored photo read "could not be
            # retrieved" on every later turn); a video only by its text.
            kept = [a for a in shown.attachments if a.summary or _is_image(a)]
            content = user_content(replace(shown, attachments=kept), self.fetch)
            state.history.append({"role": "user", "content": content})
            self._note_customer_turn(shown, state, content)
        metadata: Dict[str, Any] = {"matched": matched, "safety": outcome}
        if shown is not message:
            metadata["transcript_text"] = shown.message_text
        return self._finish(
            shown, state, text, "guardrail:battery_safety",
            escalated=escalated, ticket_id=ticket_id, metadata=metadata,
            already_in_history=bool(evidence),
        )

    def _safety_not_recorded(
        self, message: InboundMessage, shown: InboundMessage, state: ConversationState,
        matched: List[str], evidence: Optional[List[str]], why: str,
    ) -> Reply:
        """No safety ticket could be recorded: the steps and 112, and no
        promise of a call. Logged at error level and alarmed (spec section 8)."""
        self.log.emit("safety_ticket_not_recorded", message.conversation_id, why=why, level="error")
        return self._safety_reply(message, shown, state, SAFETY_NOT_RECORDED_MESSAGE, matched, evidence,
                                  outcome="not_recorded")

    def _desk_store(self) -> Any:
        """The ticket store when Zoho is on, else None. Zoho is on exactly
        when api.py wired a TicketRouter (records_real_tickets). The
        playground, the CLI, the live evaluation and Zoho off keep the mock,
        and the gates record nothing (spec 2026-10-05, section 6)."""
        tickets = getattr(self.registry, "tickets", None)
        if not getattr(tickets, "records_real_tickets", False):
            return None
        return tickets.store

    @staticmethod
    def _gate_key(state: ConversationState, purpose: str) -> str:
        """A gate ticket's source key: one per run and purpose (spec section 2)."""
        return "%s:%s:%s" % (state.conversation_id, state.started_at or "", purpose)

    def _record_ticket(
        self,
        message: InboundMessage,
        state: ConversationState,
        *,
        kind: str,
        purpose: str,
        phone: Optional[str],
        verified: bool,
        description: str,
        cluster_id: Optional[str] = None,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        bike: Optional[Dict[str, Any]] = None,
    ) -> Recorded:
        """A ticket a gate writes straight to the seam (spec sections 2 and 6):
        safety with no number we know, the call-back number, the handover and
        the lock-out. It never goes through create_support_ticket, so no bike
        is looked up for a number nobody proved. There is one per run and
        purpose: the same source key always returns the same ticket, so a
        retry or a rerun records nothing new.

        Called only for a customer, with Zoho on (_desk_store). It logs
        ticket_recorded, which a save conflict counts as a side effect. A
        failure is logged and comes back as no reference, and the caller then
        promises nothing."""
        cid = message.conversation_id
        source_key = self._gate_key(state, purpose)
        fields: Dict[str, Any] = dict(
            kind=kind, conversation_id=cid, started_at=state.started_at,
            cluster_id=cluster_id or state.cluster_id, channel=message.channel, phone=phone,
            identity="verified" if verified else "unverified", category=category, severity=severity,
            description=description,
        )
        fields.update(bike or {})
        try:
            created = self.registry.tickets.create(source_key=source_key, persona="customer", **fields)
        except Exception as exc:  # StoreUnavailable included; the class only, never str(exc)
            self.log.emit("ticket_record_failed", cid, kind=kind, error=type(exc).__name__)
            return Recorded(None)
        reference = (created or {}).get("ticket_id")
        if not reference:
            self.log.emit("ticket_record_failed", cid, kind=kind, error="no_ticket")
            return Recorded(None)
        self.log.emit("ticket_recorded", cid, ticket_id=reference, kind=kind, urgent=is_urgent(kind, category))
        return Recorded(reference)

    def _add_note(self, conversation_id: str, ticket_id: str, text: str) -> bool:
        """A line on a ticket the run already holds (spec section 2). A failed
        note is logged. The ticket stands either way."""
        tickets = getattr(self.registry, "tickets", None)
        if not hasattr(tickets, "add_note"):
            return False
        try:
            tickets.add_note(ticket_id, text)
        except Exception as exc:  # the class only
            self.log.emit("ticket_note_failed", conversation_id, ticket_id=ticket_id, error=type(exc).__name__)
            return False
        self.log.emit("ticket_note_added", conversation_id, ticket_id=ticket_id)
        return True
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_safety_without_phone
```

Expected: every test passes.

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: Task 12's count plus the new tests. The only failure is the known environmental one, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test should need editing.

Behaviour changes these tests rely on still holding:
- With Zoho off and no phone, a safety reply no longer says "They will call you on the number linked to your account", and `escalated` is now `False`. `tests.test_api_photo_check.PhotoCheckTests.test_an_unverified_visitor_gets_the_safety_reply_without_a_ticket` and `tests.test_verify_first.StillFirstTests.test_a_safety_report_comes_before_verification` check only `handled_by` and `ticket_id`, so they still pass.
- `tests.test_safety_ticket_per_run.SafetyTicketPerRunTests.test_a_repeat_inside_one_run_shares_its_ticket` still sees one mock ticket. The repeat now adds a note to it through `MockTicketSystem.add_note` (Task 3).
- `tests.test_store_review_fixes...test_the_safety_branch_still_raises_its_ticket_and_warns_the_customer` still finds `SAFETY_MESSAGE[:60]`.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/guardrails.py src/emotorad_ai/runtime.py tests/test_safety_without_phone.py
git commit -m "feat: safety without a known phone asks for a number and never promises a call

A typed number records an urgent safety ticket straight through the seam,
unverified and with no bike looked up. A failed record, Zoho off with no
number, and the store being down all get the steps and 112 with no promise.
A second report in the run adds a note to the run's safety ticket.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: The callback-number gate and handover tickets

**Files:**
- Modify: `src/emotorad_ai/graph.py:1-11` (docstring), `:38-56` (`TurnNodes`, `NODE_NAMES`), `:79-80` (edges)
- Modify: `src/emotorad_ai/guardrails.py` (three texts after `CAP_OVERALL_MESSAGE`)
- Modify: `src/emotorad_ai/runtime.py`: imports; `CALLBACK_FIELDS` after `VERIFY_FIRST_FIELDS` (`:195-203`); `TurnNodes(...)` in `__init__` (`:339-351`); `_handle` (`:437-438`, `:465-467`); `_merge_onto_fresh` (`:510-550`); new `_node_callback` after `_node_safety`; `_node_handoff` (`:780-791`) plus new methods after it; `_side_effects_since` (`:1103-1120`)
- Modify: `tests/test_graph.py:20-21`
- Create: `tests/test_callback_gate.py`, `tests/test_handover_tickets.py`
- Test: `tests/test_callback_gate.py`, `tests/test_handover_tickets.py`, `tests/test_graph.py`

**Interfaces:**
- Consumes: from Task 13: `read_number`, `TypedNumber`, `_as_shown`, `Recorded`, `PURPOSE_SAFETY`, `PURPOSE_HANDOVER`, `Runtime._desk_store`, `_record_ticket`, `_add_note`, the `ticket_recorded` and `ticket_note_added` events, `NUMBER_RECEIVED_MESSAGE`, `HANDOVER_RECORDED_MESSAGE`, `HANDOVER_ASK_NUMBER_MESSAGE`, `SAFETY_NOT_RECORDED_MESSAGE`, and the test helpers `DeskChat`, `CALL_BACK`, `FAKE`, `ONE_BIKE`, `RIDER`, `PROMISES`. `tickets.kinds.is_desk_reference` (Task 2). `ConversationState.typed_number`, `.lookup_error`, `.awaiting_callback`, `.callback_asks`, `.last_code_phone` (Task 5). `navigation.wants_start_over` and `verify_first.INVALID_NUMBER` (existing). `tests.test_verify_first.ConflictedStore` (existing).
- Produces: graph node `"callback_gate"` (`TurnNodes.callback_gate`, `NODE_NAMES[2]`); `Runtime._node_callback`, `_callback_number`, `_end_wait`, `_handover_ticket`, `_handover_bike`, `_live_run_ticket`; `runtime.CALLBACK_FIELDS = ("typed_number", "lookup_error", "awaiting_callback", "callback_asks", "last_code_phone")`; `_merge_onto_fresh(..., changed: Sequence[str] = ())`; `_side_effects_since` counts `ticket_recorded` and `ticket_note_added`. Texts: `SAFETY_ASK_AGAIN_MESSAGE`, `HANDOVER_NO_NUMBER_MESSAGE`, `HANDOVER_NOT_RECORDED_MESSAGE`. `handled_by` values: `guardrail:callback:recorded`, `guardrail:callback:invalid_number`, `guardrail:callback:ask_again`, `guardrail:callback:no_number`, `guardrail:callback:not_recorded`, `guardrail:callback:capped`. Events: `callback_number_invalid`, `callback_wait_ended` (`purpose`, `why`), `handover_ticket_not_recorded`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_graph.py`, replace:

```python
    def test_going_back_comes_after_safety_and_before_the_handoff(self):
        self.assertEqual(NODE_NAMES[1:4], ("safety_gate", "navigation_gate", "handoff_gate"))
```

with:

```python
    def test_the_callback_number_then_going_back_come_after_safety_and_before_the_handoff(self):
        self.assertEqual(NODE_NAMES[1:5], ("safety_gate", "callback_gate", "navigation_gate", "handoff_gate"))
```

Create `tests/test_callback_gate.py`:

```python
"""The callback-number gate (spec 2026-10-05, section 6, part 4).

While a handover or a safety report waits for a number, code reads the next
message for one. The gate runs after safety and before going back, the
handover, erasure and the verify step. Every test goes through
runtime.handle() with a model that raises if called.
"""

import unittest

from emotorad_ai.graph import NODE_NAMES
from emotorad_ai.guardrails import (
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_NO_NUMBER_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
    SAFETY_ASK_AGAIN_MESSAGE,
    SAFETY_NO_CONTACT_MESSAGE,
    SAFETY_NOT_RECORDED_MESSAGE,
)
from emotorad_ai.verify_first import INVALID_NUMBER
from tests.test_safety_without_phone import CALL_BACK, FAKE, PROMISES, DeskChat
from tests.test_verify_first import ConflictedStore

# The number, as customers write it.
GOLDEN = {
    "english": "my number is 9999999999",
    "hinglish": "mera number 9999999999 hai",
    "hindi": "मेरा नंबर ९९९९९९९९९९ है",
}


def codes_sent(chat):
    return [e for e in chat.events("tool_call") if e["tool"] == "request_identity_verification"]


class OrderTests(unittest.TestCase):
    def test_the_gate_comes_straight_after_safety_and_before_the_verify_step(self):
        self.assertEqual(NODE_NAMES.index("callback_gate"), NODE_NAMES.index("safety_gate") + 1)
        self.assertLess(NODE_NAMES.index("callback_gate"), NODE_NAMES.index("navigation_gate"))
        self.assertLess(NODE_NAMES.index("callback_gate"), NODE_NAMES.index("verify_gate"))

    def test_a_number_typed_while_a_handover_waits_sends_no_code(self):
        chat = DeskChat()
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, chat.say("I want to talk to a person").text)
        reply = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(codes_sent(chat), [])
        self.assertIsNone(chat.store.pending_code("c1"))
        self.assertEqual(reply.handled_by, "guardrail:callback:recorded")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("handover", "unverified", FAKE))
        self.assertEqual(record["source_key"], "c1:%s:handover" % chat.state().started_at)
        self.assertFalse(record["bike"])
        self.assertEqual(reply.text, NUMBER_RECEIVED_MESSAGE.format(reference=record["_id"]))
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertTrue(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)

    def test_a_number_typed_while_a_safety_report_waits_is_a_ticket_not_a_code(self):
        chat = DeskChat()
        self.assertIn(SAFETY_NO_CONTACT_MESSAGE, chat.say("my battery is smoking").text)
        reply = chat.say("9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(codes_sent(chat), [])
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["urgent"], record["phone"]), ("safety", True, FAKE))
        self.assertEqual(record["source_key"], "c1:%s:safety_callback" % chat.state().started_at)
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertTrue(reply.escalated)


class GoldenPhraseTests(unittest.TestCase):
    def test_a_number_in_each_language_is_recorded_for_a_handover(self):
        for language, text in GOLDEN.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say("talk to a person")
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["phone"]), ("handover", FAKE))
                self.assertEqual(reply.ticket_id, record["_id"])
                self.assertEqual(chat.state().typed_number, CALL_BACK)

    def test_a_number_in_each_language_is_recorded_for_a_safety_report(self):
        for language, text in GOLDEN.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say("my battery is swollen")
                reply = chat.say(text)
                (record,) = chat.records()
                self.assertEqual((record["kind"], record["urgent"], record["phone"]), ("safety", True, FAKE))
                self.assertIn(NUMBER_RECEIVED_MESSAGE.format(reference=record["_id"]), reply.text)

    def test_the_number_is_phone_in_history_and_transcript(self):
        for language, text in GOLDEN.items():
            with self.subTest(language=language):
                chat = DeskChat()
                chat.say("talk to a person")
                chat.say(text)
                history = repr(chat.state().history)
                self.assertIn("[phone]", history)
                for form in (CALL_BACK, "९९९९९९९९९९"):
                    self.assertNotIn(form, history)
                said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
                self.assertIn("[phone]", said)
                self.assertNotIn("९९९९९९९९९९", said)


class InvalidNumberTests(unittest.TestCase):
    def test_a_number_that_is_not_an_indian_mobile_is_refused_and_the_wait_goes_on(self):
        for text, raw in (("12345678", "12345678"), ("+34 612 345 678", "612 345 678")):
            with self.subTest(text=text):
                chat = DeskChat()
                chat.say("talk to a person")
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual(reply.handled_by, "guardrail:callback:invalid_number")
                self.assertEqual(reply.text, INVALID_NUMBER)
                self.assertFalse(reply.escalated)
                self.assertEqual(chat.records(), [])
                self.assertEqual(chat.state().awaiting_callback, "handover")
                self.assertIn("[number]", repr(chat.state().history))
                self.assertNotIn(raw, repr(chat.state().history))
                self.assertEqual(len(chat.events("callback_number_invalid")), 1)
                self.assertEqual(codes_sent(chat), [])
                self.assertEqual(chat.say("9999999999").handled_by, "guardrail:callback:recorded")


class QuotedReferenceTests(unittest.TestCase):
    def test_a_quoted_reference_is_not_read_as_a_bad_number(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("my old ticket is EM-1000001")
        self.assertEqual(reply.handled_by, "guardrail:callback:no_number")
        self.assertEqual(chat.events("callback_number_invalid"), [])
        self.assertEqual(chat.records(), [])

    def test_a_reference_and_a_number_records_the_number(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("ticket EM-1000001, call 9999999999")
        (record,) = chat.records()
        self.assertEqual(record["phone"], FAKE)
        self.assertEqual(reply.ticket_id, record["_id"])


class NoNumberTests(unittest.TestCase):
    def test_a_handover_wait_ends_with_the_other_ways_line(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("I'd rather not")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:callback:no_number")
        self.assertEqual(reply.text, HANDOVER_NO_NUMBER_MESSAGE)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertTrue(chat.say("my battery isn't charging").handled_by.startswith("verify_first:"))

    def test_a_safety_wait_asks_once_more_then_logs_not_recorded(self):
        chat = DeskChat()
        chat.say("my battery is smoking")
        again = chat.say("what?")
        self.assertEqual(again.handled_by, "guardrail:callback:ask_again")
        self.assertEqual(again.text, SAFETY_ASK_AGAIN_MESSAGE)
        self.assertFalse(again.escalated)
        self.assertEqual((chat.state().awaiting_callback, chat.state().callback_asks), ("safety", 1))
        last = chat.say("no")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(last.handled_by, "guardrail:callback:no_number")
        self.assertEqual(last.text, SAFETY_NOT_RECORDED_MESSAGE)
        for promise in PROMISES:
            self.assertNotIn(promise, last.text)
        self.assertFalse(last.escalated)
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("safety_ticket_not_recorded")
        self.assertEqual((event["why"], event["level"]), ("no_number", "error"))
        self.assertEqual(chat.records(), [])


class StartOverTests(unittest.TestCase):
    def test_start_over_ends_the_wait(self):
        chat = DeskChat()
        chat.say("talk to a person")
        reply = chat.say("start over")
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.records(), [])
        self.assertTrue(reply.handled_by.startswith("verify_first:"), reply.handled_by)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual((event["purpose"], event["why"]), ("handover", "start_over"))


class ConflictTests(unittest.TestCase):
    def test_a_conflict_after_a_recording_merges_and_does_not_run_again(self):
        store = ConflictedStore()
        chat = DeskChat(conversations=store)
        chat.say("talk to a person")
        store.conflicts = 1
        reply = chat.say("9999999999")
        self.assertEqual(reply.handled_by, "guardrail:callback:recorded")
        (record,) = chat.records()
        saved = chat.state()
        self.assertIn("merged_after_conflict", saved.transitions)
        self.assertIsNone(saved.awaiting_callback)
        self.assertEqual(saved.typed_number, CALL_BACK)
        self.assertEqual(saved.ticket_id, record["_id"])
        self.assertEqual(len(chat.events("ticket_recorded")), 1)


class ZohoOffTests(unittest.TestCase):
    def test_a_wait_left_when_zoho_went_off_is_dropped(self):
        chat = DeskChat()
        chat.say("talk to a person")
        chat.registry.tickets = chat.mock  # Zoho switched off between turns
        reply = chat.say("9999999999")
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        self.assertIsNone(chat.state().awaiting_callback)
        (event,) = chat.events("callback_wait_ended")
        self.assertEqual(event["why"], "not_recordable")


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_handover_tickets.py`:

```python
"""Talk to a person with Zoho on (spec 2026-10-05, section 6, part 4).

A customer's request records a handover ticket, or asks for the number to
record it with. A dealer's request records nothing. Zoho off is today's
handover. Every test goes through runtime.handle() and checks that the model
was never called.
"""

import unittest
from unittest import mock

from emotorad_ai.adapters import DealerWhatsAppAdapter
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.guardrails import (
    HANDOFF_MESSAGE,
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_NOT_RECORDED_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
)
from emotorad_ai.conversation import StoreUnavailable
from tests.test_safety_without_phone import CALL_BACK, FAKE, ONE_BIKE, RIDER, DeskChat

HEALTHY_DEALER = "919000000001"  # Royal Cycle Stores (fixtures)


class WithAPhoneTests(unittest.TestCase):
    def test_a_verified_customer_gets_a_handover_ticket_and_its_reference(self):
        chat = DeskChat()
        reply = chat.say("I want to talk to a person", identity=RIDER)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("handover", "verified", ONE_BIKE))
        self.assertEqual(record["source_key"], "c1:%s:handover" % chat.state().started_at)
        self.assertIn("EMXP2025004417", repr(record["bike"]))
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=record["_id"]), reply.text)
        self.assertEqual(reply.ticket_id, record["_id"])
        self.assertTrue(reply.escalated)
        self.assertIsNone(chat.state().awaiting_callback)

    def test_a_number_in_the_first_message_is_recorded(self):
        chat = DeskChat()
        reply = chat.say("call me on 99999 99999")
        self.assertEqual(chat.llm.requests, [])
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("handover", "unverified", FAKE))
        self.assertFalse(record["bike"])
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=record["_id"]), reply.text)
        history = repr(chat.state().history)
        self.assertIn("[phone]", history)
        self.assertNotIn("99999 99999", history)
        self.assertFalse([e for e in chat.events("tool_call") if e["tool"] == "request_identity_verification"])

    def test_a_verified_customer_who_types_another_number_is_called_on_it_unverified(self):
        chat = DeskChat()
        chat.say("please call me on 9999999999", identity=RIDER)
        (record,) = chat.records()
        self.assertEqual((record["phone"], record["identity"]), (FAKE, "unverified"))
        self.assertFalse(record["bike"])


class WithoutANumberTests(unittest.TestCase):
    def test_the_number_is_asked_for_and_nothing_is_escalated_yet(self):
        chat = DeskChat()
        reply = chat.say("I want to talk to a person")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertTrue(reply.text.startswith(DISCLOSURE_TEXT))
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertEqual(chat.state().awaiting_callback, "handover")
        self.assertEqual(chat.events("escalation"), [])

    def test_a_record_that_fails_promises_nothing_and_keeps_reading_the_number(self):
        chat = DeskChat()
        with mock.patch.object(chat.registry.tickets, "create", side_effect=StoreUnavailable("down")):
            reply = chat.say("call me on 9999999999")
        self.assertIn(HANDOVER_NOT_RECORDED_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertEqual(chat.state().awaiting_callback, "handover")
        self.assertEqual(chat.say("9999999999").handled_by, "guardrail:callback:recorded")


class OneTicketPerRunTests(unittest.TestCase):
    def test_a_second_request_adds_a_note_to_the_runs_ticket(self):
        chat = DeskChat()
        first = chat.say("call me on 9999999999")
        second = chat.say("I want to talk to a human")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(second.ticket_id, first.ticket_id)
        (record,) = chat.records()
        self.assertEqual([note["text"] for note in record["notes"]], ["Customer asked for a person."])
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=first.ticket_id), second.text)
        self.assertTrue(second.escalated)

    def test_a_safety_ticket_in_the_run_gets_the_note(self):
        chat = DeskChat()
        safety = chat.say("my battery is swollen", identity=RIDER)
        handover = chat.say("talk to a person", identity=RIDER)
        self.assertEqual(handover.ticket_id, safety.ticket_id)
        (record,) = chat.records()
        self.assertEqual(len(record["notes"]), 1)

    def test_a_ticket_gone_from_desk_is_not_quoted(self):
        chat = DeskChat()
        first = chat.say("call me on 9999999999")
        (record,) = chat.records()
        with mock.patch.object(chat.tickets, "get", return_value=dict(record, state="gone")):
            reply = chat.say("I want to talk to a human")
        self.assertNotIn(first.ticket_id, reply.text)
        self.assertIn(HANDOVER_ASK_NUMBER_MESSAGE, reply.text)


class DealerTests(unittest.TestCase):
    def test_a_dealers_request_records_nothing(self):
        chat = DeskChat()
        message = DealerWhatsAppAdapter(chat.runtime.resolver).to_message(
            {"from": HEALTHY_DEALER, "text": "please connect me to my account manager", "conversation_id": "d1"})
        reply = chat.runtime.handle(message)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertIn(HANDOFF_MESSAGE, reply.text)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.records(), [])
        self.assertIsNone(chat.conversations.peek("d1").awaiting_callback)


class ZohoOffTests(unittest.TestCase):
    def test_zoho_off_is_todays_handover(self):
        chat = DeskChat(zoho=False)
        reply = chat.say("call me on 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(HANDOFF_MESSAGE, reply.text)
        self.assertTrue(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(chat.mock.tickets, {})
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(chat.say(CALL_BACK).handled_by, "verify_first:code_sent")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_callback_gate tests.test_handover_tickets tests.test_graph
```

Expected: `tests.test_callback_gate` and `tests.test_handover_tickets` fail to import (`ImportError: cannot import name 'HANDOVER_NO_NUMBER_MESSAGE'` / `'HANDOVER_NOT_RECORDED_MESSAGE'`). `tests.test_graph...test_the_callback_number_then_going_back_come_after_safety_and_before_the_handoff` fails with `AssertionError` (no `callback_gate` in `NODE_NAMES`).

- [ ] **Step 3: Implement**

**3a. `src/emotorad_ai/guardrails.py`.** Straight after the `CAP_OVERALL_MESSAGE = ...` line added in Task 13, add:

```python
# The callback gate (runtime._node_callback).
# A safety wait with no number in the reply: asked once more.
SAFETY_ASK_AGAIN_MESSAGE = (
    "Please send me your mobile number so our safety team can reach you.\n\n" + SAFETY_EMERGENCY
)
# A handover wait with no number in the reply. Draft: the line on how else to
# reach support comes from the support lead (person step 10). Until then it
# promises nothing.
HANDOVER_NO_NUMBER_MESSAGE = (
    "Without a number I can't pass this on from here, so nothing has been sent. "
    "You can send me a mobile number at any time."
)
# The handover ticket could not be written: nothing is promised.
HANDOVER_NOT_RECORDED_MESSAGE = "Sorry, I couldn't pass this on just now. Please try again in a few minutes."
```

**3b. `src/emotorad_ai/graph.py`.** Replace:

```python
The graph is the order, and the order is the design (runtime.py's docstring):
identity and context, then the safety gate, going back and the handoff gate,
then verification for an anonymous customer, then persona routing and triage,
```

with:

```python
The graph is the order, and the order is the design (runtime.py's docstring):
identity and context, then the safety gate, the call-back number, going back
and the handoff gate, then verification for an anonymous customer, then
persona routing and triage,
```

Replace:

```python
    prepare: Node
    safety_gate: Node
    navigation_gate: Node
```

with:

```python
    prepare: Node
    safety_gate: Node
    callback_gate: Node
    navigation_gate: Node
```

Replace:

```python
NODE_NAMES = (
    "prepare", "safety_gate", "navigation_gate", "handoff_gate", "erasure_gate", "verify_gate", "persona_route",
    "jev_classify", "standard_reply", "narrow_agent", "full_agent",
)
```

with:

```python
NODE_NAMES = (
    "prepare", "safety_gate", "callback_gate", "navigation_gate", "handoff_gate", "erasure_gate", "verify_gate",
    "persona_route", "jev_classify", "standard_reply", "narrow_agent", "full_agent",
)
```

Replace:

```python
    graph.add_conditional_edges("safety_gate", _replied_or("navigation_gate"), ["navigation_gate", END])
```

with:

```python
    graph.add_conditional_edges("safety_gate", _replied_or("callback_gate"), ["callback_gate", END])
    # The call-back number (runtime._node_callback). While a handover or a
    # safety report waits for a number, the next message is read for one
    # here, straight after safety and before going back and the verify step,
    # so a number typed for a call is never taken as a number to verify.
    graph.add_conditional_edges("callback_gate", _replied_or("navigation_gate"), ["navigation_gate", END])
```

**3c. `runtime.py`, imports.** Replace:

```python
    EVIDENCE_BLOCKED_MESSAGE,
    HANDOFF_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
```

with:

```python
    EVIDENCE_BLOCKED_MESSAGE,
    HANDOFF_MESSAGE,
    HANDOVER_ASK_NUMBER_MESSAGE,
    HANDOVER_NO_NUMBER_MESSAGE,
    HANDOVER_NOT_RECORDED_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    NUMBER_RECEIVED_MESSAGE,
```

Replace:

```python
    SAFETY_ADDED_MESSAGE,
    SAFETY_EMERGENCY,
```

with:

```python
    SAFETY_ADDED_MESSAGE,
    SAFETY_ASK_AGAIN_MESSAGE,
    SAFETY_EMERGENCY,
```

Replace:

```python
from .tickets.kinds import is_urgent
```

with:

```python
from .tickets.kinds import is_desk_reference, is_urgent
```

Replace:

```python
from .verify_first import CONFIRMED, NUMBER, VerifyFirst, find_phone, looks_like_a_number, redact
```

with:

```python
from .verify_first import CONFIRMED, INVALID_NUMBER, NUMBER, VerifyFirst, find_phone, looks_like_a_number, redact
```

**3d. `runtime.py`, `CALLBACK_FIELDS`.** Replace:

```python
    # origin goes with started_at: it belongs to one run, never another.
    "origin",
)
```

with:

```python
    # origin goes with started_at: it belongs to one run, never another.
    "origin",
)

# The call-back wait and the numbers read for it (spec 2026-10-05, section 6).
# When a turn that changed them loses a save race, they are carried
# (Runtime._merge_onto_fresh), so a number already recorded is never waited
# for again.
CALLBACK_FIELDS = ("typed_number", "lookup_error", "awaiting_callback", "callback_asks", "last_code_phone")
```

**3e. `runtime.py`, the graph nodes in `__init__`.** Replace:

```python
                prepare=self._node_prepare,
                safety_gate=self._node_safety,
                navigation_gate=self._node_navigation,
```

with:

```python
                prepare=self._node_prepare,
                safety_gate=self._node_safety,
                callback_gate=self._node_callback,
                navigation_gate=self._node_navigation,
```

**3f. `runtime.py`, `_handle`.** These lines are unchanged by Tasks 1 to 13. Replace:

```python
            history_mark, log_mark = len(state.history), len(self.log.events)
            coverage_loaded = state.coverage_result
```

with:

```python
            history_mark, log_mark = len(state.history), len(self.log.events)
            coverage_loaded = state.coverage_result
            callback_loaded = {name: getattr(state, name) for name in CALLBACK_FIELDS}
```

Replace:

```python
                        state = self._merge_onto_fresh(
                            state, this_turn, reply, looked_up=state.coverage_result != coverage_loaded
                        )
```

with:

```python
                        state = self._merge_onto_fresh(
                            state, this_turn, reply, looked_up=state.coverage_result != coverage_loaded,
                            changed=[name for name in CALLBACK_FIELDS if getattr(state, name) != callback_loaded[name]],
                        )
```

**3g. `runtime.py`, `_merge_onto_fresh`.** Replace:

```python
    def _merge_onto_fresh(
        self, ours: ConversationState, this_turn: List[Dict[str, Any]], reply: Reply, looked_up: bool = False
    ) -> ConversationState:
```

with:

```python
    def _merge_onto_fresh(
        self, ours: ConversationState, this_turn: List[Dict[str, Any]], reply: Reply, looked_up: bool = False,
        changed: Sequence[str] = (),
    ) -> ConversationState:
```

Replace:

```python
        for name in ("user_key", "channel", "context_block", "cluster_id"):
            if getattr(fresh, name) is None:
                setattr(fresh, name, getattr(ours, name))
```

with:

```python
        for name in ("user_key", "channel", "context_block", "cluster_id"):
            if getattr(fresh, name) is None:
                setattr(fresh, name, getattr(ours, name))
        # The call-back fields this turn changed are this turn's. A number it
        # recorded is not waited for again, and the next message is read
        # against where this turn left the wait (spec 2026-10-05, section 6).
        for name in changed:
            setattr(fresh, name, getattr(ours, name))
```

**3h. `runtime.py`, the callback gate.** Straight after the end of `_node_safety`, which ends:

```python
        matched, evidence = _safety_scan(message)
        if matched:
            return {"reply": self._handle_safety(message, resolved, state, matched, evidence)}
        return {}
```

add:

```python

    def _node_callback(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 1a. The call-back number (spec 2026-10-05, section 6). While a
        #     handover or a safety report waits for a number, code reads the
        #     next message for one here. This runs after safety and before
        #     going back, the handover, erasure and the verify step, so a
        #     number typed for a call is never taken as a number to verify.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        waiting = state.awaiting_callback
        if waiting is None:
            return {}
        cid = message.conversation_id
        if resolved.persona != "customer" or self._desk_store() is None:
            # Zoho switched off since the question: nothing can be recorded,
            # so the wait goes and the turn carries on as any other.
            self._end_wait(cid, state, "not_recordable")
            return {}
        if wants_start_over(message.message_text or ""):
            self._end_wait(cid, state, "start_over")
            return {}
        typed = read_number(message.message_text or "")
        if typed.number is not None:
            return {"reply": self._callback_number(message, state, resolved, waiting, typed)}
        shown = _as_shown(message, typed)
        metadata: Dict[str, Any] = {"purpose": waiting}
        if shown is not message:
            metadata["transcript_text"] = shown.message_text
        if typed.attempted:
            # Not a valid Indian mobile (too short, or +34 and the like). The
            # gate says so and keeps waiting; the try is kept as [number].
            self.log.emit("callback_number_invalid", cid, purpose=waiting)
            return {"reply": self._finish(shown, state, INVALID_NUMBER, "guardrail:callback:invalid_number",
                                          metadata=metadata)}
        if waiting == "handover":
            self._end_wait(cid, state, "no_number")
            self.log.escalation(cid, "customer_requested_human", None)
            return {"reply": self._finish(shown, state, HANDOVER_NO_NUMBER_MESSAGE, "guardrail:callback:no_number",
                                          escalated=True, metadata=metadata)}
        if state.callback_asks < 1:
            # A safety report: asked once more.
            state.callback_asks += 1
            return {"reply": self._finish(shown, state, SAFETY_ASK_AGAIN_MESSAGE, "guardrail:callback:ask_again",
                                          metadata=metadata)}
        self._end_wait(cid, state, "no_number")
        self.log.emit("safety_ticket_not_recorded", cid, why="no_number", level="error")
        return {"reply": self._finish(shown, state, SAFETY_NOT_RECORDED_MESSAGE, "guardrail:callback:no_number",
                                      metadata=metadata)}

    def _end_wait(self, conversation_id: str, state: ConversationState, why: str) -> None:
        self.log.emit("callback_wait_ended", conversation_id, purpose=state.awaiting_callback, why=why)
        state.awaiting_callback, state.callback_asks = None, 0

    def _callback_number(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity, waiting: str,
        typed: TypedNumber,
    ) -> Reply:
        """The number a handover or a safety report waited for. It is recorded
        unverified, straight through the seam, so no bike is looked up for it
        (spec 2026-10-05, section 6)."""
        cid = message.conversation_id
        shown = replace(message, message_text=typed.shown)
        metadata: Dict[str, Any] = {"purpose": waiting, "transcript_text": typed.shown}
        state.typed_number = typed.number
        phone = "+91" + typed.number
        if waiting == "safety":
            recorded = self._record_ticket(
                message, state, kind="safety", purpose=PURPOSE_SAFETY, phone=phone, verified=False,
                description=("Automatic safety escalation. The customer reported a safety issue in the AI chat "
                             "and gave this number when asked. The number is not verified. Their words are "
                             "in the transcript."),
                cluster_id=resolved.cluster_id, category="battery_safety", severity="critical",
            )
        else:
            recorded = self._record_ticket(
                message, state, kind="handover", purpose=PURPOSE_HANDOVER, phone=phone, verified=False,
                description="Customer asked for a person and gave this number when asked.",
                cluster_id=resolved.cluster_id,
            )
        if recorded.refusal is not None:
            # A cap on unverified tickets refused it: say so, promise nothing.
            state.awaiting_callback, state.callback_asks = None, 0
            return self._finish(shown, state, recorded.refusal, "guardrail:callback:capped", metadata=metadata)
        if recorded.reference is None:
            # The wait stays, so the number sent again is still read here,
            # never by the verify step.
            if waiting == "safety":
                self.log.emit("safety_ticket_not_recorded", cid, why="not_recorded", level="error")
                return self._finish(shown, state, SAFETY_NOT_RECORDED_MESSAGE, "guardrail:callback:not_recorded",
                                    metadata=metadata)
            self.log.emit("handover_ticket_not_recorded", cid)
            return self._finish(shown, state, HANDOVER_NOT_RECORDED_MESSAGE, "guardrail:callback:not_recorded",
                                metadata=metadata)
        state.awaiting_callback, state.callback_asks = None, 0
        self.log.escalation(cid, "battery_safety" if waiting == "safety" else "customer_requested_human",
                            recorded.reference)
        return self._finish(
            shown, state, NUMBER_RECEIVED_MESSAGE.format(reference=recorded.reference),
            "guardrail:callback:recorded", escalated=True, ticket_id=recorded.reference, metadata=metadata,
        )
```

**3i. `runtime.py`, the handover gate.** Replace:

```python
    def _node_handoff(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 2. Human handoff, reachable at any point, no friction.
        message, state = turn["message"], turn["conversation"]
        handoff = check_human_handoff(message.message_text)
        if handoff.triggered:
            self.log.guardrail(message.conversation_id, "human_handoff", handoff.matched)
            self.log.escalation(message.conversation_id, "customer_requested_human", None)
            return {"reply": self._finish(
                message, state, HANDOFF_MESSAGE, "guardrail:human_handoff",
                escalated=True, metadata={"matched": handoff.matched},
            )}
        return {}
```

with:

```python
    def _node_handoff(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 2. Human handoff, reachable at any point, no friction. With Zoho
        #    on, a customer's request records a handover ticket, or asks for
        #    the number to record it with (spec 2026-10-05, section 6). With
        #    Zoho off, or a dealer asking for their account manager, it is
        #    exactly as before and nothing is recorded (dealer tickets wait
        #    for W1).
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        handoff = check_human_handoff(message.message_text)
        if handoff.triggered:
            self.log.guardrail(message.conversation_id, "human_handoff", handoff.matched)
            if resolved.persona == "customer" and self._desk_store() is not None:
                return {"reply": self._handover_ticket(message, state, resolved, handoff.matched)}
            self.log.escalation(message.conversation_id, "customer_requested_human", None)
            return {"reply": self._finish(
                message, state, HANDOFF_MESSAGE, "guardrail:human_handoff",
                escalated=True, metadata={"matched": handoff.matched},
            )}
        return {}

    def _handover_ticket(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity, matched: List[str],
    ) -> Reply:
        """Talk to a person, for a customer, with Zoho on (spec 2026-10-05,
        section 6). One ticket per need in a run: a Desk ticket the run
        already holds gets the note and is quoted. Otherwise a number typed in
        this message, or failing that the number we know, records a handover
        ticket. With neither, the number is asked for and the callback gate
        waits for it. "Call me" is itself a trigger, so the number is read
        from this message first."""
        cid = message.conversation_id
        typed = read_number(message.message_text or "")
        shown = _as_shown(message, typed)
        metadata: Dict[str, Any] = {"matched": matched}
        if shown is not message:
            metadata["transcript_text"] = shown.message_text
        if typed.number:
            state.typed_number = typed.number
        held = self._live_run_ticket(state)
        if held is not None:
            self._add_note(cid, held, "Customer asked for a person.")
            self.log.escalation(cid, "customer_requested_human", held)
            return self._finish(shown, state, HANDOVER_RECORDED_MESSAGE.format(reference=held),
                                "guardrail:human_handoff", escalated=True, ticket_id=held,
                                metadata=dict(metadata, handover="note_added"))
        known = resolved.identity.phone
        phone = "+91" + typed.number if typed.number else known
        if phone is None:
            state.awaiting_callback, state.callback_asks = "handover", 0
            self.log.emit("callback_asked", cid, purpose="handover")
            return self._finish(shown, state, HANDOVER_ASK_NUMBER_MESSAGE, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="asked_for_number"))
        # Verified only when it is the number the channel or a code proved.
        verified = resolved.identity.may_disclose and phone == known
        recorded = self._record_ticket(
            message, state, kind="handover", purpose=PURPOSE_HANDOVER, phone=phone, verified=verified,
            description="Customer asked for a person.", cluster_id=resolved.cluster_id,
            bike=self._handover_bike(resolved, state) if verified else None,
        )
        if recorded.refusal is not None:
            # A cap on unverified tickets refused it: say so, promise nothing.
            return self._finish(shown, state, recorded.refusal, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="capped"))
        if recorded.reference is None:
            self.log.emit("handover_ticket_not_recorded", cid)
            if typed.number:
                # The number sent again is read by the callback gate, not the verify step.
                state.awaiting_callback, state.callback_asks = "handover", 0
            return self._finish(shown, state, HANDOVER_NOT_RECORDED_MESSAGE, "guardrail:human_handoff",
                                metadata=dict(metadata, handover="not_recorded"))
        self.log.escalation(cid, "customer_requested_human", recorded.reference)
        return self._finish(
            shown, state, HANDOVER_RECORDED_MESSAGE.format(reference=recorded.reference),
            "guardrail:human_handoff", escalated=True, ticket_id=recorded.reference,
            metadata=dict(metadata, handover="recorded"),
        )

    def _live_run_ticket(self, state: ConversationState) -> Optional[str]:
        """The run's Desk ticket, unless it was deleted or merged in Desk
        (gone). state.ticket_id belongs to the run: a new run starts with a
        fresh state, and restart_for clears it."""
        store = self._desk_store()
        if store is None or not is_desk_reference(state.ticket_id):
            return None
        try:
            record = store.get(state.ticket_id)
        except StoreUnavailable:
            return None
        if record is None or record.get("state") == "gone":
            return None
        return state.ticket_id

    def _handover_bike(self, resolved: ResolvedIdentity, state: ConversationState) -> Optional[Dict[str, Any]]:
        """The bike on a verified handover ticket: the chosen one, or the only
        one. Never an unlisted bike, which is a claim and not a record."""
        if state.unlisted_bike:
            return None
        bike = self._selected_bike(resolved, state)
        if not bike or not bike.get("frame_number"):
            return None
        return {"frame_number": bike.get("frame_number"), "frame_number_source": bike.get("frame_number_source"),
                "bike_model": bike.get("product_name")}
```

**3j. `runtime.py`, `_side_effects_since`.** Replace:

```python
    def _side_effects_since(self, log_mark: int, conversation_id: str) -> List[Dict[str, Any]]:
        """Tool calls since `log_mark` that changed something for the customer:
        any write tool, plus SIDE_EFFECT_TOOLS (a picture sent, a code sent or
        spent), which a rerun would repeat or break."""
        done: List[Dict[str, Any]] = []
        for event in self.log.events[log_mark:]:
            if event.get("event") != "tool_call" or event.get("conversation_id") != conversation_id:
                continue
```

with:

```python
    def _side_effects_since(self, log_mark: int, conversation_id: str) -> List[Dict[str, Any]]:
        """Tool calls since `log_mark` that changed something for the customer:
        any write tool, plus SIDE_EFFECT_TOOLS (a picture sent, a code sent or
        spent), which a rerun would repeat or break. Also a ticket a gate
        recorded or noted straight through the seam (ticket_recorded,
        ticket_note_added). A turn that recorded one is merged, never run
        again, and a rerun would add the note twice (spec 2026-10-05,
        section 6)."""
        done: List[Dict[str, Any]] = []
        for event in self.log.events[log_mark:]:
            if event.get("conversation_id") != conversation_id:
                continue
            if event.get("event") in ("ticket_recorded", "ticket_note_added"):
                done.append({"tool": event["event"], "ticket_id": event.get("ticket_id")})
                continue
            if event.get("event") != "tool_call":
                continue
```

The rest of the method (from `# A failed call changed nothing, except a code tried:` onwards) stays as it is.

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_callback_gate tests.test_handover_tickets tests.test_graph tests.test_safety_without_phone
```

Expected: every test passes.

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: Task 13's count plus the new tests. The only failure is the known environmental one, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. The one existing test changed on purpose is `tests.test_graph.GraphShapeTests.test_going_back_comes_after_safety_and_before_the_handoff`, renamed and updated in Step 1 for the new node.

With Zoho off nothing changes: `tests.test_verify_first.StillFirstTests.test_asking_for_a_person_comes_before_verification` and `tests.test_erasure_chat...test_safety_and_handoff_come_first` still see `guardrail:human_handoff`. `tests.test_audit_persistence...test_a_second_clash_while_merging_a_ticket_hands_over_with_its_reference` and the `_side_effects_since` probe in that file are unaffected, because no gate event is logged there.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/graph.py src/emotorad_ai/guardrails.py src/emotorad_ai/runtime.py tests/test_graph.py tests/test_callback_gate.py tests/test_handover_tickets.py
git commit -m "feat: the callback-number gate and handover tickets

A new graph node straight after safety reads the number a handover or a
safety report waits for, before the verify step, so a typed number becomes a
ticket and never a code. Talk to a person records a handover ticket for a
customer when Zoho is on, notes the run's ticket if it has one, and asks for
a number when none is known. Dealers and Zoho off are unchanged. A recording
is a side effect for the save-conflict merge.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Lock-out tickets, caps on unverified tickets, the edge case register rows

**Files:**
- Create: `src/emotorad_ai/tickets/caps.py`
- Modify: `src/emotorad_ai/verify_first.py:18-38` (imports), `:133-145` (texts), `:170-189` (`GateReply`, `VerifyFirst.__init__`), `:206-209` (locked path), `:246-299` (send, resend, order, check), `:336-337` (`_too_many`), plus new `_lockout` and `_sent_to`
- Modify: `src/emotorad_ai/runtime.py`: imports; `__init__` (`:287`); `_remember_lookup` (`:395-398`); `_navigate_number` (`:748-755`); `_node_verify` (`:878-897`); Task 13's `_record_ticket`; new `_cap_refusal` and `_record_lockout`
- Modify: `src/emotorad_ai/tools/mocks.py` (`raise_intake_ticket` as Task 4 left it, and imports)
- Modify: `docs/Emotorad_Edge_Case_Register.md` (new section 7 after the `6.4` row)
- Modify: `docs/contracts/amiigo-support-chat.md` (handover wording row in "What changes with the Zoho integration")
- Create: `tests/test_lockout_tickets.py`
- Test: `tests/test_lockout_tickets.py`

**Interfaces:**
- Consumes: from Task 13: `Recorded`, `PURPOSE_LOCKOUT`, `Runtime._desk_store`, `_record_ticket` (and its `source_key` local), `REFERENCE_SUFFIX`, `CAP_PER_NUMBER_MESSAGE`, `CAP_OVERALL_MESSAGE`, `DeskChat`, `CALL_BACK`, `FAKE`. From Task 14: `handled_by` `guardrail:callback:capped`, and the `refusal` branches in `_handover_ticket` and `_callback_number`. `TicketStore.unverified_since(since, phone=None)`, `by_source_key`, `next_reference`, `insert`; `tickets.record.new_record`; `tickets.clock.now_iso`; `tickets.kinds.is_urgent` (Task 2). `ConversationState.last_code_phone` (Task 5). `VerificationStore.pending_phone` (existing). `raise_intake_ticket` parameters `conversation_id`, `started_at`, `persona`, `identity_strength`, `typed_number` (ten digits), `idempotency_key` (Task 4). `ToolContext(persona=..., started_at=...)` (Task 1).
- Produces: `tickets/caps.py`: `PER_NUMBER = 2`, `OVERALL = 50`, `PER_NUMBER_CAP = "per_number"`, `OVERALL_CAP = "overall"`, `CAP_TEXTS`, `ist_day_start(now: str) -> str`, `cap_reached(store, *, phone, source_key, now) -> Optional[str]`. In `verify_first.py`: `LOCKED_WHY`, `TOO_MANY_WHY`, `PASSING_ON` (`LOCKED` and `TOO_MANY_CODES` keep their text), `GateReply.ticket_id`, `VerifyFirst(registry, resolver, log, record_lockout=None)` where `record_lockout(message, state, phone, outcome) -> (reference, refusal)`, and `state.last_code_phone` set on every code sent. `Runtime._record_lockout`, `Runtime._cap_refusal`. Events: `unverified_ticket_capped` (`kind`, `cap`, `level="error"`), `lockout_ticket_not_recorded`, `ticket_cap_unchecked`. Tool error code `unverified_ticket_capped` from `raise_intake_ticket`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lockout_tickets.py`:

```python
"""Lock-out tickets and the caps on unverified tickets (spec 2026-10-05,
section 6, part 4).

The lock-out (five wrong codes, or a fourth code asked for) records one
`lockout` ticket per run. It uses the first number found in this message, the
pending number, or the last number a code went to. Unverified tickets that
are not urgent are capped at two per number and fifty in all per calendar day
in IST. Urgent tickets are never capped.
"""

import unittest
from unittest import mock

from emotorad_ai.contract import ANONYMOUS
from emotorad_ai.guardrails import (
    CAP_OVERALL_MESSAGE,
    CAP_PER_NUMBER_MESSAGE,
    HANDOVER_RECORDED_MESSAGE,
    REFERENCE_SUFFIX,
)
from emotorad_ai.tickets.caps import OVERALL, PER_NUMBER, cap_reached, ist_day_start
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tools.mocks import RAISE_INTAKE_TICKET
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.verify_first import LOCKED, LOCKED_WHY, PASSING_ON, TOO_MANY_CODES
from tests.test_safety_without_phone import CALL_BACK, FAKE, DeskChat

NOW = "2026-10-05T07:00:00.000000+00:00"  # 12:30 IST, 5 October
EARLIER = "2026-10-05T01:00:00.000000+00:00"  # 06:30 IST, the same day
NEXT_DAY = "2026-10-05T19:00:00.000000+00:00"  # 00:30 IST, 6 October
PENDING = "+919700000010"  # the fixture rider's number, where the codes go


def at(now):
    """The runtime's clock for the caps, held at `now`."""
    return mock.patch("emotorad_ai.runtime.now_iso", return_value=now)


def seed(chat, phone, count=1, created_at=EARLIER, kind="handover", identity="unverified"):
    """Records from earlier chats, written straight to the store."""
    for _ in range(count):
        reference = chat.tickets.next_reference()
        chat.tickets.insert(new_record(
            reference=reference, chat_reference="stage:" + reference, source_key="seed:" + reference,
            mode="test", kind=kind, conversation_id="older-chat", started_at=created_at, cluster_id=None,
            channel="website_chat", phone=phone, identity=identity, category=None, ai_severity=None,
            summary="", claims={}, bike=None, coverage=None, customer_name=None, created_at=created_at,
        ))


def wrong(chat):
    return "000000" if chat.store.pending_code("c1") != "000000" else "111111"


def lock_out(chat):
    chat.say("hi")
    chat.say("9700000010")
    for _ in range(4):
        chat.say(wrong(chat))
    return chat.say(wrong(chat))


class LockoutTests(unittest.TestCase):
    def test_five_wrong_codes_record_one_lockout_ticket_on_the_pending_number(self):
        chat = DeskChat()
        locked = lock_out(chat)
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(locked.handled_by, "verify_first:locked")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("lockout", "unverified", PENDING))
        self.assertEqual(record["source_key"], "c1:%s:lockout" % chat.state().started_at)
        self.assertFalse(record["bike"])
        self.assertEqual(locked.text, LOCKED + REFERENCE_SUFFIX.format(reference=record["_id"]))
        self.assertEqual(locked.ticket_id, record["_id"])
        self.assertTrue(locked.escalated)
        # One per run: every later locked message gets the same reference.
        again = chat.say("hello?")
        self.assertEqual(again.ticket_id, record["_id"])
        self.assertEqual(len(chat.records()), 1)

    def test_a_number_typed_while_locked_is_kept_as_phone(self):
        chat = DeskChat()
        lock_out(chat)
        chat.say("9876543210")
        self.assertNotIn("9876543210", repr(chat.state().history))

    def test_a_fourth_code_asked_for_records_the_number_in_the_message(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("9876543210")
        chat.say("9700000009")
        reply = chat.say("9999999999")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        (record,) = chat.records()
        self.assertEqual((record["kind"], record["phone"]), ("lockout", FAKE))
        self.assertEqual(reply.text, TOO_MANY_CODES + REFERENCE_SUFFIX.format(reference=record["_id"]))
        self.assertEqual(reply.ticket_id, record["_id"])

    def test_a_fourth_code_asked_for_with_no_number_uses_the_pending_one(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("resend")
        chat.say("resend")
        reply = chat.say("resend")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        (record,) = chat.records()
        self.assertEqual(record["phone"], PENDING)

    def test_with_the_code_cancelled_the_last_number_a_code_went_to_is_used(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        chat.say("resend")
        chat.say("resend")
        chat.say("wrong number")  # cancels the pending code
        self.assertEqual(chat.state().last_code_phone, PENDING)
        reply = chat.say("my order number is EMO-100234")
        self.assertEqual(reply.handled_by, "verify_first:too_many_codes")
        (record,) = chat.records()
        self.assertEqual(record["phone"], PENDING)

    def test_the_last_number_a_code_went_to_follows_each_code(self):
        chat = DeskChat()
        chat.say("hi")
        chat.say("9700000010")
        self.assertEqual(chat.state().last_code_phone, PENDING)
        chat.say("sorry, wrong number, it's 9876543210")
        self.assertEqual(chat.state().last_code_phone, "+919876543210")

    def test_zoho_off_locks_out_as_today(self):
        chat = DeskChat(zoho=False)
        locked = lock_out(chat)
        self.assertEqual(locked.text, LOCKED)
        self.assertIsNone(locked.ticket_id)
        self.assertTrue(locked.escalated)
        self.assertEqual(chat.mock.tickets, {})


class CapHelperTests(unittest.TestCase):
    def test_the_day_starts_at_midnight_in_ist(self):
        self.assertEqual(ist_day_start(NOW), "2026-10-04T18:30:00.000000+00:00")
        self.assertEqual(ist_day_start("2026-10-05T18:29:59.999999+00:00"), "2026-10-04T18:30:00.000000+00:00")
        self.assertEqual(ist_day_start("2026-10-05T18:30:00.000000+00:00"), "2026-10-05T18:30:00.000000+00:00")

    def test_no_store_never_caps(self):
        self.assertIsNone(cap_reached(None, phone=FAKE, source_key="c1:s:handover", now=NOW))

    def test_a_recorded_source_key_is_never_capped(self):
        chat = DeskChat()
        seed(chat, FAKE, count=PER_NUMBER)
        self.assertIsNone(cap_reached(chat.tickets, phone=FAKE, source_key="seed:EM-1000001", now=NOW))
        self.assertEqual(cap_reached(chat.tickets, phone=FAKE, source_key="c1:s:handover", now=NOW), "per_number")


class CapTests(unittest.TestCase):
    def test_a_third_unverified_ticket_for_one_number_in_a_day_is_refused(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        with at(NOW):
            reply = chat.say("call me on 9999999999")
            other = chat.say("call me on 9876543210", cid="c2")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(CAP_PER_NUMBER_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertIsNone(reply.ticket_id)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual((event["cap"], event["kind"], event["level"]), ("per_number", "handover", "error"))
        # Another number the same day is recorded.
        self.assertTrue(is_desk_reference(other.ticket_id))
        self.assertEqual(len(chat.records()), PER_NUMBER + 1)

    def test_the_callback_number_is_capped_too(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        with at(NOW):
            chat.say("talk to a person")
            reply = chat.say(CALL_BACK)
        self.assertEqual(reply.handled_by, "guardrail:callback:capped")
        self.assertEqual(reply.text, CAP_PER_NUMBER_MESSAGE)
        self.assertIsNone(chat.state().awaiting_callback)
        self.assertEqual(len(chat.records()), PER_NUMBER)

    def test_the_day_is_the_calendar_day_in_ist(self):
        chat = DeskChat(ticket_clock=lambda: NEXT_DAY)
        seed(chat, FAKE, count=PER_NUMBER, created_at=EARLIER)
        with at(NEXT_DAY):
            reply = chat.say("call me on 9999999999")
        self.assertIn(HANDOVER_RECORDED_MESSAGE.format(reference=reply.ticket_id), reply.text)

    def test_fifty_unverified_tickets_in_a_day_refuse_any_more(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, None, count=OVERALL)
        with at(NOW):
            reply = chat.say("call me on 9999999999")
        self.assertIn(CAP_OVERALL_MESSAGE, reply.text)
        self.assertIsNone(reply.ticket_id)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual(event["cap"], "overall")

    def test_urgent_tickets_are_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        seed(chat, None, count=OVERALL)
        with at(NOW):
            reply = chat.say("my battery is smoking, my number is 9999999999")
        self.assertEqual(chat.llm.requests, [])
        self.assertTrue(is_desk_reference(reply.ticket_id))
        self.assertEqual(chat.tickets.get(reply.ticket_id)["kind"], "safety")
        self.assertEqual(chat.events("unverified_ticket_capped"), [])

    def test_a_locked_out_number_over_its_cap_is_told_so_and_nothing_is_promised(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, PENDING, count=PER_NUMBER)
        with at(NOW):
            locked = lock_out(chat)
        self.assertEqual(locked.text, LOCKED_WHY + " " + CAP_PER_NUMBER_MESSAGE)
        self.assertNotIn(PASSING_ON, locked.text)
        self.assertFalse(locked.escalated)
        self.assertIsNone(locked.ticket_id)
        self.assertEqual(len(chat.records()), PER_NUMBER)

    def test_a_retry_of_a_recorded_ticket_is_never_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        with at(NOW):
            locked = lock_out(chat)
            seed(chat, PENDING, count=PER_NUMBER)  # the number is now over its cap
            again = chat.say("hello?")
        self.assertEqual(again.ticket_id, locked.ticket_id)
        self.assertEqual(chat.events("unverified_ticket_capped"), [])


class IntakeCapTests(unittest.TestCase):
    def call(self, chat, typed):
        context = ToolContext(
            conversation_id="c1", persona="customer", started_at=NOW,
            late={"typed_number": lambda: typed, "identity_strength": lambda: ANONYMOUS,
                  "channel": lambda: "website_chat"},
        )
        with mock.patch("emotorad_ai.tools.mocks.now_iso", return_value=NOW):
            return chat.registry.call(
                RAISE_INTAKE_TICKET, {"summary": "Charger not working.", "idempotency_key": "intake-1"}, context)

    def test_the_intake_tool_uses_the_same_caps(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        envelope = self.call(chat, CALL_BACK)
        self.assertEqual(envelope["error"]["code"], "unverified_ticket_capped")
        self.assertIn(CAP_PER_NUMBER_MESSAGE, envelope["error"]["message"])
        self.assertEqual(len(chat.records()), PER_NUMBER)

    def test_another_number_is_not_capped(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        envelope = self.call(chat, "9876543210")
        self.assertTrue(is_desk_reference(envelope["data"]["ticket_id"]))

    def test_a_capped_intake_is_logged(self):
        chat = DeskChat(ticket_clock=lambda: NOW)
        seed(chat, FAKE, count=PER_NUMBER)
        envelope = self.call(chat, CALL_BACK)
        chat.runtime._remember_lookup(chat.conversations.get("c1"), RAISE_INTAKE_TICKET, {}, envelope)
        (event,) = chat.events("unverified_ticket_capped")
        self.assertEqual((event["kind"], event["level"]), ("intake", "error"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_lockout_tickets
```

Expected: `ModuleNotFoundError: No module named 'emotorad_ai.tickets.caps'`. No test runs.

- [ ] **Step 3: Implement**

**3a. Create `src/emotorad_ai/tickets/caps.py`:**

```python
"""Caps on unverified tickets that are not urgent (spec 2026-10-05, section 6).

A number nobody proved can be typed by anyone, so the tickets it raises are
capped: at most two per number and fifty in all, per calendar day in IST.
Urgent tickets (a safety report) are never capped. This is the one helper
every path that records an unverified ticket uses: the handover, the call-back
number and the lock-out (runtime.Runtime._record_ticket), and the intake tool
(tools/mocks.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..guardrails import CAP_OVERALL_MESSAGE, CAP_PER_NUMBER_MESSAGE

PER_NUMBER = 2
OVERALL = 50
PER_NUMBER_CAP = "per_number"
OVERALL_CAP = "overall"
# What the customer is told for each cap.
CAP_TEXTS = {PER_NUMBER_CAP: CAP_PER_NUMBER_MESSAGE, OVERALL_CAP: CAP_OVERALL_MESSAGE}

IST = timezone(timedelta(hours=5, minutes=30))


def ist_day_start(now: str) -> str:
    """The start of `now`'s calendar day in IST, as a UTC time in the ticket
    store's format (tickets.clock.now_iso), so it compares as a string."""
    moment = datetime.fromisoformat(now)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    start = moment.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="microseconds")


def cap_reached(store: Any, *, phone: Optional[str], source_key: str, now: str) -> Optional[str]:
    """Which cap a new unverified, non-urgent ticket would break, or None.

    No store (Zoho off) means no cap. A source key already recorded is a
    retry, not a new ticket, so it is never capped and gets the record it
    already has. The caller decides what is unverified and not urgent. Raises
    StoreUnavailable when the store cannot answer."""
    if store is None or store.by_source_key(source_key) is not None:
        return None
    since = ist_day_start(now)
    if phone and store.unverified_since(since, phone=phone) >= PER_NUMBER:
        return PER_NUMBER_CAP
    if store.unverified_since(since) >= OVERALL:
        return OVERALL_CAP
    return None
```

**3b. `src/emotorad_ai/verify_first.py`.** Replace:

```python
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

from .contract import InboundMessage
from .conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ConversationState, utc_now_iso
```

with:

```python
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional, Tuple

from .contract import InboundMessage
from .conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, ConversationState, utc_now_iso
from .guardrails import REFERENCE_SUFFIX
```

Replace:

```python
LOCKED = (
    "That's too many wrong codes, so I can't confirm it's you here. I'm passing you to our support "
    "team, who can verify you another way."
)
```

with:

```python
# The lock-out's two halves (spec 2026-10-05, section 6). If a cap on
# unverified tickets refuses the lock-out ticket, the cap's text replaces
# PASSING_ON, so nothing is promised.
LOCKED_WHY = "That's too many wrong codes, so I can't confirm it's you here."
TOO_MANY_WHY = "I can't send any more codes in this chat."
PASSING_ON = "I'm passing you to our support team, who can verify you another way."
LOCKED = LOCKED_WHY + " " + PASSING_ON
```

Replace:

```python
TOO_MANY_CODES = (
    "I can't send any more codes in this chat. I'm passing you to our support team, who can verify you "
    "another way."
)
```

with:

```python
TOO_MANY_CODES = TOO_MANY_WHY + " " + PASSING_ON
```

Replace:

```python
    text: str
    outcome: str
    model_text: str
    escalated: bool = False
    resolved: Optional[ResolvedIdentity] = None


class VerifyFirst:
    def __init__(self, registry: ToolRegistry, resolver: IdentityResolver, log: Any) -> None:
        self.registry = registry
        self.resolver = resolver
        self.log = log
        self.store = getattr(registry, "verification", None)
```

with:

```python
    text: str
    outcome: str
    model_text: str
    escalated: bool = False
    resolved: Optional[ResolvedIdentity] = None
    # The lock-out ticket's reference, once it is recorded.
    ticket_id: Optional[str] = None


# Records the lock-out ticket: (message, state, the number to call, the
# outcome) -> (reference, refusal). Runtime._record_lockout. It answers
# (None, None) when nothing can be recorded (Zoho off, no number).
LockoutRecorder = Callable[[InboundMessage, ConversationState, Optional[str], str],
                           Tuple[Optional[str], Optional[str]]]


class VerifyFirst:
    def __init__(self, registry: ToolRegistry, resolver: IdentityResolver, log: Any,
                 record_lockout: Optional[LockoutRecorder] = None) -> None:
        self.registry = registry
        self.resolver = resolver
        self.log = log
        self.store = getattr(registry, "verification", None)
        self.record_lockout = record_lockout
```

Replace:

```python
        if state.verify_step == CODE and self.store.attempts_left(message.conversation_id) <= 0:
            # Locked: the handover stands. No code is sent or tried again,
            # whatever the message, until the store lets the entry go.
            return self._reply(message, state, LOCKED, "locked", text, escalated=True)
        phone = find_phone(text)
```

with:

```python
        if state.verify_step == CODE and self.store.attempts_left(message.conversation_id) <= 0:
            # Locked: the handover stands. No code is sent or tried again,
            # whatever the message, until the store lets the entry go. A
            # number in the message is the one the lock-out ticket calls.
            found = find_phone(text)
            model_text = redact(text, found[1], "[phone]") if found else text
            return self._lockout(message, state, LOCKED_WHY, "locked", model_text,
                                 typed=found[0] if found else None)
        phone = find_phone(text)
```

Replace:

```python
        if state.verify_sends >= MAX_CODES:
            return self._too_many(message, state, model_text)
        envelope = self._call(message, REQUEST_IDENTITY_VERIFICATION, {"phone": number})
        if is_error(envelope):
            return self._reply(message, state, INVALID_NUMBER, "invalid_number", model_text)
        state.verify_sends += 1
        state.verify_step = CODE
        state.verify_masked = envelope["data"]["phone_masked"]
```

with:

```python
        if state.verify_sends >= MAX_CODES:
            return self._too_many(message, state, model_text, typed=number)
        envelope = self._call(message, REQUEST_IDENTITY_VERIFICATION, {"phone": number})
        if is_error(envelope):
            return self._reply(message, state, INVALID_NUMBER, "invalid_number", model_text)
        state.verify_sends += 1
        self._sent_to(message, state)
        state.verify_step = CODE
        state.verify_masked = envelope["data"]["phone_masked"]
```

Replace:

```python
        state.verify_sends += 1
        state.verify_masked = envelope["data"]["phone_masked"]
        return self._reply(message, state, CODE_RESENT.format(masked=state.verify_masked), "code_resent", text)
```

with:

```python
        state.verify_sends += 1
        self._sent_to(message, state)
        state.verify_masked = envelope["data"]["phone_masked"]
        return self._reply(message, state, CODE_RESENT.format(masked=state.verify_masked), "code_resent", text)
```

Replace:

```python
        state.verify_sends += 1
        state.verify_step = CODE
        state.verify_masked = sent["data"]["phone_masked"]
```

with:

```python
        state.verify_sends += 1
        self._sent_to(message, state)
        state.verify_step = CODE
        state.verify_masked = sent["data"]["phone_masked"]
```

Replace:

```python
            if envelope["error"]["code"] == "verification_locked":
                return self._reply(message, state, LOCKED, "locked", model_text, escalated=True)
```

with:

```python
            if envelope["error"]["code"] == "verification_locked":
                return self._lockout(message, state, LOCKED_WHY, "locked", model_text)
```

Replace:

```python
    def _too_many(self, message: InboundMessage, state: ConversationState, model_text: str) -> GateReply:
        return self._reply(message, state, TOO_MANY_CODES, "too_many_codes", model_text, escalated=True)
```

with:

```python
    def _too_many(self, message: InboundMessage, state: ConversationState, model_text: str,
                  typed: Optional[str] = None) -> GateReply:
        return self._lockout(message, state, TOO_MANY_WHY, "too_many_codes", model_text, typed=typed)

    def _lockout(self, message: InboundMessage, state: ConversationState, why: str, outcome: str,
                 model_text: str, typed: Optional[str] = None) -> GateReply:
        """The lock-out: five wrong codes, or a fourth code asked for (spec
        2026-10-05, section 6). When the runtime records tickets (Zoho on), a
        `lockout` ticket is recorded, unverified. Its number is the first one
        found in this message, the pending number, or the last number a code
        went to. Its reference is added to the text. There is one per run:
        every later locked message gets the same ticket back. A cap that
        refuses it is said instead of the hand-over, and nothing is promised."""
        text = why + " " + PASSING_ON
        if self.record_lockout is None:
            return self._reply(message, state, text, outcome, model_text, escalated=True)
        number = ("+91" + typed) if typed else (
            self.store.pending_phone(message.conversation_id) or state.last_code_phone)
        reference, refusal = self.record_lockout(message, state, number, outcome)
        if refusal is not None:
            return self._reply(message, state, why + " " + refusal, outcome, model_text)
        if reference:
            text += REFERENCE_SUFFIX.format(reference=reference)
        reply = self._reply(message, state, text, outcome, model_text, escalated=True)
        reply.ticket_id = reference
        return reply

    def _sent_to(self, message: InboundMessage, state: ConversationState) -> None:
        """Keep the number a code just went to. The lock-out ticket falls back
        on it when the pending code is gone (spec 2026-10-05, section 6)."""
        state.last_code_phone = self.store.pending_phone(message.conversation_id) or state.last_code_phone
```

**3c. `src/emotorad_ai/runtime.py`.** Add the imports. If Task 5 already imports `now_iso`, do not add it twice. Replace:

```python
from .tickets.kinds import is_desk_reference, is_urgent
```

with:

```python
from .tickets.caps import CAP_TEXTS, cap_reached
from .tickets.clock import now_iso
from .tickets.kinds import is_desk_reference, is_urgent
```

Replace:

```python
        self.verify_gate = VerifyFirst(self.registry, self.resolver, self.log) if verify_first else None
```

with:

```python
        self.verify_gate = (VerifyFirst(self.registry, self.resolver, self.log, record_lockout=self._record_lockout)
                            if verify_first else None)
```

In `_remember_lookup`, replace:

```python
        if name == VERIFY_IDENTITY and not is_error(envelope):
            code = str(arguments.get("code", "")).strip()
            if code and code not in state.consumed_codes:
                state.consumed_codes.append(code)
```

with:

```python
        if name == VERIFY_IDENTITY and not is_error(envelope):
            code = str(arguments.get("code", "")).strip()
            if code and code not in state.consumed_codes:
                state.consumed_codes.append(code)
        # The intake tool refuses past the caps itself (tickets/caps.py). The
        # alarmed event is logged here, where there is a log.
        if name == RAISE_INTAKE_TICKET and is_error(envelope) and envelope["error"].get("code") == "unverified_ticket_capped":
            self.log.emit("unverified_ticket_capped", state.conversation_id, kind="intake", level="error")
```

In `_navigate_number`, replace:

```python
            gate = self.verify_gate.handle(message, state)
            if gate.escalated:
                self.log.escalation(cid, "verification_locked", None)
            shown = replace(message, message_text=gate.model_text)
            return {"reply": self._finish(
                shown, state, gate.text, "verify_first:" + gate.outcome, escalated=gate.escalated,
                metadata={"transcript_text": gate.model_text},
            )}
```

with:

```python
            gate = self.verify_gate.handle(message, state)
            if gate.escalated:
                self.log.escalation(cid, "verification_locked", gate.ticket_id)
            shown = replace(message, message_text=gate.model_text)
            return {"reply": self._finish(
                shown, state, gate.text, "verify_first:" + gate.outcome, escalated=gate.escalated,
                ticket_id=gate.ticket_id, metadata={"transcript_text": gate.model_text},
            )}
```

In `_node_verify`, replace:

```python
        gate = self.verify_gate.handle(message, state)
        if gate.escalated:
            self.log.escalation(message.conversation_id, "verification_locked", None)
        text = gate.text
```

with:

```python
        gate = self.verify_gate.handle(message, state)
        if gate.escalated:
            self.log.escalation(message.conversation_id, "verification_locked", gate.ticket_id)
        text = gate.text
```

and replace:

```python
        update: Dict[str, Any] = {"reply": self._finish(
            shown, state, text, "verify_first:" + gate.outcome, escalated=gate.escalated,
            metadata={"transcript_text": gate.model_text},
        )}
```

with:

```python
        update: Dict[str, Any] = {"reply": self._finish(
            shown, state, text, "verify_first:" + gate.outcome, escalated=gate.escalated,
            ticket_id=gate.ticket_id, metadata={"transcript_text": gate.model_text},
        )}
```

In Task 13's `_record_ticket`, replace:

```python
        cid = message.conversation_id
        source_key = self._gate_key(state, purpose)
        fields: Dict[str, Any] = dict(
```

with:

```python
        cid = message.conversation_id
        source_key = self._gate_key(state, purpose)
        if not verified and not is_urgent(kind, category):
            refusal = self._cap_refusal(cid, kind, phone, source_key)
            if refusal is not None:
                return Recorded(None, refusal)
        fields: Dict[str, Any] = dict(
```

Straight after the end of `_record_ticket` (its last line is `return Recorded(reference)`), add:

```python

    def _cap_refusal(self, conversation_id: str, kind: str, phone: Optional[str], source_key: str) -> Optional[str]:
        """The caps on unverified tickets that are not urgent (spec
        2026-10-05, section 6), through the one helper the intake tool also
        uses. Returns the text to send instead, or None. A store that cannot
        answer does not cap: the record then fails or succeeds on its own."""
        try:
            cap = cap_reached(self._desk_store(), phone=phone, source_key=source_key, now=now_iso())
        except StoreUnavailable as exc:
            self.log.emit("ticket_cap_unchecked", conversation_id, kind=kind, error=type(exc).__name__)
            return None
        if cap is None:
            return None
        self.log.emit("unverified_ticket_capped", conversation_id, kind=kind, cap=cap, level="error")
        return CAP_TEXTS[cap]

    def _record_lockout(
        self, message: InboundMessage, state: ConversationState, phone: Optional[str], outcome: str
    ) -> Recorded:
        """verify_first's lock-out ticket (spec 2026-10-05, section 6).
        VerifyFirst runs only for an anonymous customer, so the ticket is
        always unverified and never has a bike. Nothing is recorded with Zoho
        off, or with no number to call."""
        if self._desk_store() is None:
            return Recorded(None)
        if phone is None:
            self.log.emit("lockout_ticket_not_recorded", message.conversation_id, why="no_number")
            return Recorded(None)
        why = "five wrong codes" if outcome == "locked" else "a fourth code was asked for"
        return self._record_ticket(
            message, state, kind="lockout", purpose=PURPOSE_LOCKOUT, phone=phone, verified=False,
            description="The customer could not verify their number in the AI chat: %s." % why,
            cluster_id=state.cluster_id,
        )
```

**3d. `src/emotorad_ai/tools/mocks.py`.** Add these imports after `from ..conversation import address_tokens`. If Task 4 already imports `VERIFIED` or `now_iso`, do not add them twice.

```python
from ..contract import VERIFIED
from ..tickets.caps import CAP_TEXTS, cap_reached
from ..tickets.clock import now_iso
```

In `raise_intake_ticket` as Task 4 left it, insert this block straight before the statement that calls `tickets.create(`. That is after Task 4's `contact_number_required` refusal, so an unverified customer always has `typed_number` here.

```python
            # The caps on unverified tickets (spec 2026-10-05, section 6), the
            # same helper the runtime's gates use. A verified customer's intake
            # is never capped, and nor is a retry of a recorded one.
            if (persona == "customer" and identity_strength != VERIFIED
                    and getattr(tickets, "records_real_tickets", False)):
                capped = cap_reached(
                    tickets.store, phone="+91" + typed_number if typed_number else None,
                    source_key="%s:%s:%s:%s" % (conversation_id, started_at or "", RAISE_INTAKE_TICKET, idempotency_key),
                    now=now_iso(),
                )
                if capped is not None:
                    raise ToolError(
                        "unverified_ticket_capped",
                        "No ticket was raised. Tell the customer exactly this, and promise no call: %s"
                        % CAP_TEXTS[capped],
                    )
```

**3e. `docs/Emotorad_Edge_Case_Register.md`.** Replace:

```
| 6.4 | No EU–India adequacy decision | **CAPTURE** (legal) | Deployment topology decision. Risk 18 |
```

with:

```
| 6.4 | No EU–India adequacy decision | **CAPTURE** (legal) | Deployment topology decision. Risk 18 |

## 7. Handovers that record no Zoho ticket (2026-10-05)

From the Zoho Desk spec (`docs/superpowers/specs/2026-10-05-zoho-desk-tickets-design.md`, "Not changed in this version"). Each one tells the customer a person takes over, or could, and records nothing for a person to act on. Each is counted by the event named, so shadow mode can promote it.

| # | Case | Disposition | Notes |
|---|---|---|---|
| 7.1 | The evidence-not-forthcoming handover records no ticket | **CAPTURE** | Counted by `escalation` with `reason: evidence_not_forthcoming`, and `guardrail_triggered` with `guardrail: evidence_not_forthcoming` |
| 7.2 | The coverage and order post-check blocks hand over and record no ticket | **CAPTURE** | `escalation` with `reason: coverage_claim_blocked` or `order_claim_blocked`; `guardrail_triggered` with `coverage_post_check` or `order_post_check` |
| 7.3 | A ticket promised with none behind it, not about a live hazard, hands over and records nothing | **CAPTURE** | `guardrail_triggered` and `escalation`, both `ticket_promise_unbacked`. The live hazard case is backed by the safety backstop |
| 7.4 | Agent loop failures hand over and record nothing: model unavailable, an empty reply, a stuck tool loop, the iteration budget | **CAPTURE** | `llm_error`, `empty_reply`, `stuck_agent`. The budget has no event of its own, only `escalation` with `reason: agent_requested_handover` |
| 7.5 | Model and store outages, other than safety, hand over and record nothing | **CAPTURE** | `escalation` with `reason: llm_error` or `llm_error_after_write`; `store_unavailable`. A safety report while the store is down is handled (`safety_ticket_not_recorded`) |
| 7.6 | The unsupported persona hands over and records nothing | **CAPTURE** | No event of its own: the `outcome` event with `handled_by: router` |
| 7.7 | Triage's "unsupported topic" says it will put the customer through, and records nothing | **CAPTURE** | `routed` and `classification` with `reason: unsupported_topic:<topic>` |
| 7.8 | Prose handovers in the late-warranty, motor, dealer and photo-safety prompts ("I'll pass you to our team") record nothing | **CAPTURE** | No event yet: the model writes the line without escalating. A handover-phrase scan of bot replies would count it |
| 7.9 | A request for a person in Hindi or Hinglish misses the handover gate | **CAPTURE** | No event yet: the triggers are English only (`guardrails._HANDOFF_TERMS`). The `classification` event keeps the raw text, so misses can be found by search |
| 7.10 | A non-Indian number typed for a call-back is refused | **CAPTURE** | `callback_number_invalid`. EU volume through the web chat would promote it |
```

**3f. `docs/contracts/amiigo-support-chat.md`.** In the table under "What changes with the Zoho integration", replace the "Safety reports" row (Task 12 leaves it as it is today):

```
| Safety reports | Raised at once as critical | The same, as a top-priority Zoho ticket | Nothing |
```

with:

```
| Safety reports | Raised at once as critical | The same, as a top-priority Zoho ticket | Nothing |
| Handover wording | "I am connecting you to a member of our support team now", with nothing recorded | A signed-in rider always has a number, so the app gets: "I've passed this conversation to our support team, so you won't need to repeat yourself. They will be in touch. Your reference is EM-…", with `escalated: true` and the reference in `ticket_id`. On the website without a number, the bot first asks "What mobile number can they reach you on?" with `escalated: false`. Drafts until the support lead confirms them | Show the text as sent. Treat `escalated: false` as a chat still open |
```

- [ ] **Step 4: Run the module, then the whole suite**

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest tests.test_lockout_tickets tests.test_verify_first tests.test_callback_gate tests.test_handover_tickets
```

Expected: every test passes.

```
env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_CF_CHAT_REFERENCE -u EMOTORAD_ZOHO_CF_SOURCE .venv/bin/python -m unittest discover -s tests -t .
```

Expected: Task 14's count plus the new tests. The only failure is the known environmental one, `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`. No existing test changes:
- `tests.test_verify_first.CodeTests.test_five_wrong_codes_hand_over` and `LockoutAndCapTests` run with Zoho off, so `LOCKED` and `TOO_MANY_CODES` come out unchanged and nothing is recorded.
- The one difference with Zoho off is that a number typed while locked is now kept as `[phone]` in the history.
- Task 4's `IntakeTicketTests` run on the mock, which has no `records_real_tickets`, so the cap block is skipped.

- [ ] **Step 5: Commit**

```
git add src/emotorad_ai/tickets/caps.py src/emotorad_ai/verify_first.py src/emotorad_ai/runtime.py src/emotorad_ai/tools/mocks.py docs/Emotorad_Edge_Case_Register.md docs/contracts/amiigo-support-chat.md tests/test_lockout_tickets.py
git commit -m "feat: lock-out tickets and caps on unverified tickets

Five wrong codes or a fourth code asked for record one lockout ticket per run,
with the number from the message, the pending one or the last one a code went
to. Unverified tickets that are not urgent are capped at two per number and
fifty in all per IST day, through one helper the gates and the intake tool
share. Safety is never capped. The edge case register gains the handovers
that still record nothing, each with the event that counts it, and the app
contract gains the handover wording.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Interface issues for the plan author

1. **`InMemoryTicketStore()` with no arguments.** The shared interfaces do not give its constructor. The tests assume it needs none. If Task 2 gives it required arguments, change `DeskChat` in `tests/test_safety_without_phone.py`.
2. **All five new `ConversationState` fields come from Task 5.** This includes `awaiting_callback`, `callback_asks` and `last_code_phone`, which the skeleton marks "(Task 14)" and "(Task 15)". If Task 5 adds only `typed_number` and `lookup_error`, Task 13 must add the other three, defaulting to None, 0 and None.
3. **Which task does what for save conflicts.** Task 13 logs `ticket_recorded` and `ticket_note_added`. Task 14 makes `_side_effects_since` count both, not only `ticket_recorded`, because a rerun would otherwise add a note twice.
   - Before Task 14 a conflict reruns the turn. That is harmless because the same source key returns the same record. `test_a_save_conflict_records_one_ticket` passes either way.
   - Task 14 carries only the `CALLBACK_FIELDS` that the turn actually changed, using the same pattern as `looked_up`, so another server's newer wait is not overwritten. This adds `changed: Sequence[str] = ()` to `_merge_onto_fresh`.
4. **New file `src/emotorad_ai/tickets/caps.py` (Task 15).** It is not in the skeleton's file list. It is needed because "one helper every unverified recording path uses" has to be reachable both from the runtime and from `raise_intake_ticket` in `tools/mocks.py`.
5. **The intake edit depends on Task 4's names.** It assumes the parameters `persona`, `identity_strength`, `typed_number` (ten digits), `conversation_id`, `started_at` and `idempotency_key`, and inserts before Task 4's `tickets.create(` call. If Task 4 names any differently, the inserted block needs the same names.
6. **`escalated` is `False` on every safety reply that recorded nothing.** This covers Zoho off with no phone, a failed record, the store being down, and the end of a safety wait. Today the Zoho-off no-phone reply is `True`, and the app contract shows "our team will contact you" whenever `escalated` is true. That banner would be the promise the Review Focus forbids. Please confirm this change.
7. **A repeated safety report with a known phone is spotted by comparing the returned ticket with `state.ticket_id`.** The receipt replay gives no "already existed" flag. If an agent raised another ticket between the two reports, no note is added, though the reply still quotes the safety reference.
8. **Texts beyond spec section 7, all drafts for person step 10:**
   - `SAFETY_ADDED_MESSAGE`
   - `SAFETY_ASK_AGAIN_MESSAGE`
   - `HANDOVER_NO_NUMBER_MESSAGE`: the "how else to reach support" line. It names no channel until the support lead gives one.
   - `HANDOVER_NOT_RECORDED_MESSAGE`
   - `LOCKED` and `TOO_MANY_CODES` are now composed from `LOCKED_WHY` / `TOO_MANY_WHY` + `PASSING_ON`, with the same text, so a cap can replace the promise.
9. **New `handled_by` values start with `guardrail:callback:`.** That keeps them out of the "resolved" (deflection) count in `metrics.Report`. Safety and handover replies keep `guardrail:battery_safety` and `guardrail:human_handoff`, and the store-down safety reply keeps `store_unavailable`.
10. **The `level="error"` field.** `EventLog` has no levels. "Logged at error level" is written as `level="error"` on `safety_ticket_not_recorded` and `unverified_ticket_capped`. Task 12's metric filter should match on the event name.
11. **The repo `CLAUDE.md` turn order goes stale.** Its description ("identity, enrichment, safety ..., handoff, ...") does not include the new `callback_gate` node. This draft does not change it; that is for the person to decide.
12. **Gate tickets carry no `coverage` or `customer_name`.** The handover ticket for a verified customer carries the bike only. The rest is left to Task 5's facts, if it exposes a helper for them.
