# Amiigo Support Chat v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Amiigo app's support chat exactly as `docs/contracts/amiigo-support-chat.md` (v1) specifies: the Amiigo token check, the chat WebSocket, history restored by `GET`, uploads and deletion under `/amiigo/v1/`, and ticket closure from a Zoho Desk webhook.

**Architecture:** A new `amiigo` package beside the existing API: `amiigo/auth.py` checks PASETO v4.public tokens with Amiigo's public key; `amiigo/history.py` reads the stores the runtime already writes; `amiigo/socket.py` runs the chat protocol and calls the same `Runtime.handle()` that `POST /message` calls, through a turn-preparation function extracted from `post_message` without changing its behaviour; `amiigo/tickets.py` records ticket closure and pushes it to open sockets. Routes are registered from `api.py`. The website chat is untouched.

**Tech Stack:** Python 3.12, FastAPI (HTTP and WebSocket), the `cryptography` package (Ed25519), pymongo and mongomock, stdlib unittest, FastAPI `TestClient` (its `websocket_connect` for socket tests).

**Spec:** `docs/contracts/amiigo-support-chat.md` (the contract is the spec: every field, code and number in it is binding). Approved by the person on 6 Oct 2026 ("now start building it"). How this plan's tasks are written: each task gives exact behaviour, interfaces and the tests that pin them; the implementer writes the tests first, then the code. Code is not pre-written here because each task's code depends on the shape the previous task leaves; the Interfaces blocks fix the names every later task uses.

## Global Constraints

- Work only in the worktree `/private/tmp/claude-501/-Users-macbookpro-emotorad-emotorad-ai-architecture/34c78fec-4143-4f23-8509-5a545bb648b8/scratchpad/wt-history`, branch `feat/amiigo-history`. Never push, never deploy.
- Interpreter `/Users/macbookpro/emotorad/emotorad-ai-architecture/.venv/bin/python`. Whole suite before every commit, from the worktree root:
  `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB -u EMOTORAD_ZOHO_REFRESH_TOKEN -u EMOTORAD_ZOHO_CLIENT_ID -u EMOTORAD_ZOHO_CLIENT_SECRET -u EMOTORAD_ZOHO_ORG_ID -u EMOTORAD_ZOHO_TEST_DEPARTMENT_ID -u EMOTORAD_ZOHO_TEST_CONTACT_ID -u EMOTORAD_ZOHO_DEPARTMENT_ID -u EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID -u EMOTORAD_ZOHO_LIVE -u EMOTORAD_ZOHO_LAYOUT_ID -u EMOTORAD_ZOHO_PRIORITY_HIGH -u EMOTORAD_ZOHO_PRIORITY_MEDIUM -u EMOTORAD_ZOHO_CHANNEL -u EMOTORAD_ZOHO_CREDITS_FLOOR -u EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB -u EMOTORAD_EVIDENCE_CHECK -u EMOTORAD_EVIDENCE_MODEL -u EMOTORAD_CUSTOMER_CARE_CONTACT -u EMOTORAD_AMIIGO_PUBLIC_KEY -u EMOTORAD_ZOHO_WEBHOOK_SECRET /Users/macbookpro/emotorad/emotorad-ai-architecture/.venv/bin/python -m unittest discover -s tests -t . 2>&1 | grep -E "^Ran|^OK|^FAILED|^FAIL:|^ERROR:"`
  Baseline at 023819f: 3398 tests, only the known macOS failure `tests.test_video.SpeechToTextTests.test_speech_in_a_clip_comes_back_as_text`.
- Commit messages end with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Amiigo's SECRET signing key (in `~/emotorad/backend/server/common/util/token.go`) is never read, copied, used or printed. Tests make their own Ed25519 keypair. Only the public key is ever configured, through `EMOTORAD_AMIIGO_PUBLIC_KEY` (64 hex characters), which a person sets; its value never goes into the repo.
- The website chat (`POST /message`, `/uploads`, `/media`, the existing erasure endpoints, `/chat`) behaves exactly as before; its tests stay green unchanged.
- Never read a `.env` file or anything under `~/.config/emotorad/` or `~/.claude.json`; never print environment variables; never write to MongoDB, S3 or Zoho; tests use mongomock and fakes, no network.
- No log line carries a token, phone, frame number, message text, URL or webhook secret. Riders are told apart in logs by `rider_hash` = the first 12 hex characters of SHA-256 of the user key.
- No `\b` or `\w` on text that may be Indic; regexes are written in the file, never through a shell heredoc, each with a test against a known phrase.
- `knowledge/` does not change (only `knowledge/_media/catalogue.yaml` may, and this plan does not need it).
- Every HTTP response under `/amiigo/v1/` (success and error) carries `Cache-Control: no-store`; errors are `{"detail": "<code>"}` with the contract's codes; `422` keeps FastAPI's field list.
- Safety, the guardrails and the post-checks are not touched: a socket turn goes through the same `Runtime.handle()` as a web turn, so the safety branch, disclosure, evidence check and post-checks apply unchanged.

## Review Focus

1. A rider's token expires while a reply is being prepared: the reply is still saved and sent if the socket is open; the socket then closes with `4401 token_expired` (owner: Task 5).
2. The same `client_message_id` arrives on two sockets of the same rider at once: it is handled once; both get the same `ack` and `reply` (owner: Task 5).
3. A message frame names a `conversation_id` that another rider already used: refused with `conversation_not_found`, and nothing of the other chat leaks (owner: Task 5; history side in Task 2).
4. Zoho calls the webhook twice for the same closure, or for a ticket that is not the chatbot's: one notice, one push, or nothing (owner: Task 6).
5. A history page is requested while a turn is being written: no message is skipped or repeated across `before`/`after` pages (owner: Task 2).

---

### Task 1: The Amiigo token check and the rider dependency

**Files:**
- Create: `src/emotorad_ai/amiigo/__init__.py`, `src/emotorad_ai/amiigo/auth.py`
- Modify: `requirements.txt` (add `cryptography>=42` with a one-line comment), `src/emotorad_ai/config_store.py` (export `EMOTORAD_AMIIGO_PUBLIC_KEY` if it exports a fixed list), `src/emotorad_ai/api.py` (`/health` gains `"amiigo_tokens": "on" | "not configured"`)
- Test: `tests/test_amiigo_auth.py`; reuse the untracked `tests/amiigo_tokens.py` helper and `tests/data/paseto_v4_public_vectors.json` left in the worktree if they are correct (check them; fix or replace them; commit them with this task)

**Interfaces (produces):**
- `amiigo.auth.TokenCheck(public_key_hex: Optional[str], clock: Callable[[], datetime] = utc_now)`; `.enabled -> bool`; `.check(token: Optional[str]) -> CheckResult`.
- `CheckResult` (frozen dataclass): `ok: bool`, `error: Optional[str]` (one of `token_missing`, `token_invalid`, `token_expired`, `token_type_not_allowed`), `phone: Optional[str]` (normalised E.164, the same normaliser the runtime uses for `"PHONE#" + phone`), `emuser_id: Optional[str]`, `expires_at: Optional[datetime]`.
- `amiigo.auth.Rider` (frozen): `phone`, `user_key` (`"PHONE#" + phone`), `emuser_id`, `expires_at`, `rider_hash`.
- `amiigo.auth.rider_from_header(authorization: Optional[str], check: TokenCheck) -> Union[Rider, str]` (a `Rider`, or the error code). Accepts only `Bearer <token>`.
- `amiigo.auth.token_check_from_env(environ=os.environ) -> TokenCheck`.

**Behaviour:** PASETO v4.public verification implemented on the `cryptography` package's Ed25519 per the PASETO spec (header `v4.public.`, base64url body = message || 64-byte signature, optional footer, signature over PAE([header, message, footer, implicit=b""])). Claims: `exp` must be after now minus 60 s; `nbf`, if present, before now plus 60 s; the string claim `payload` parses as JSON; its `token_type` must be `access`; its `phone` must normalise to a valid Indian mobile. Malformed input of any kind is `token_invalid`, never an exception. No public key (missing or not 64 hex characters): `enabled` is false and `check` returns `token_invalid`; log `amiigo_tokens_not_configured` once.

- [ ] Step 1: Write the failing tests in `tests/test_amiigo_auth.py`: every expected-success v4.public vector verifies and every expected-fail vector is refused (vectors file carries its source URL `https://github.com/paseto-standard/test-vectors` and licence); a token minted in the test with Amiigo's claim layout (`payload` JSON string with `id`, `emuserId`, `level`, `role`, `phone`, `token_type`, `issued_at`, `expired_at`; `iat`, `nbf`, `exp`) verifies to the normalised phone and emuser id; expired; not yet valid; wrong key; tampered body; tampered footer; truncated; not `v4.public.`; empty; `token_type` `refresh` and `otp` give `token_type_not_allowed`; no phone or a malformed phone gives `token_invalid`; header parsing (`Bearer x`, `bearer x`, missing, `Basic x`); no key configured; `/health` shows `amiigo_tokens`.
- [ ] Step 2: Run them; expected FAIL (module missing).
- [ ] Step 3: Implement `amiigo/auth.py`, the requirements line, the config export and `/health`.
- [ ] Step 4: Run the tests; expected PASS. Run the whole suite; expected baseline plus the new tests.
- [ ] Step 5: Commit: `feat(amiigo): check Amiigo access tokens (PASETO v4.public) with Amiigo's public key`.

### Task 2: History: store reads, ticket status and the two GET endpoints

**Files:**
- Create: `src/emotorad_ai/amiigo/history.py`, `src/emotorad_ai/amiigo/routes.py` (an `APIRouter` included by `api.py`), `src/emotorad_ai/amiigo/common.py` (the rider dependency, `no-store`, error responses, per-rider rate limiters)
- Modify: `src/emotorad_ai/stores/mongo.py` and `src/emotorad_ai/conversation.py` (`InMemoryConversationStore`): read methods below, a new collection `conversation_notices` (read side only here) included in `delete_conversation`/`delete_person`; `INDEXES` gain `conversation_summaries (user_key, last_at desc)`, `conversation_summaries (conversation_id)`, `conversation_notices (conversation_id, at)`. `src/emotorad_ai/tickets/record.py` and the ticket stores: records gain `support_status` (`"open"` by default) and `closed_at` (`None`); a read method `ticket_status(reference) -> Optional[dict]` on the ticket store (Mongo and in-memory); records written before this change read as open.
- Test: `tests/test_amiigo_history.py`

**Interfaces (produces):**
- Store: `conversations_of(user_key, channel=None) -> List[ConversationSummaryItem]` (all runs; grouping is in `history.py`), `owner_of(conversation_id) -> Optional[str]` (a summary's `user_key`), `turns_of(conversation_id) -> List[TranscriptTurn]` (existing `transcript`), `notices_of(conversation_id) -> List[dict]`, `count_turns(conversation_id) -> int`.
- `amiigo.history.list_conversations(stores, rider, *, limit, cursor, channel, now) -> dict` (the contract's response), `list_messages(stores, rider, conversation_id, *, limit, before, after, now, signer) -> dict`, `message_view(turn_or_notice, signer, now) -> dict` (the contract's message object; used by Task 5 for `ack`, `reply` and by Task 6 for the notice).
- `amiigo.common.require_rider` (FastAPI dependency giving a `Rider` or the 401 response with the code), `amiigo.common.RiderLimiter(per_minute)`, `amiigo.common.amiigo_error(status, code)`.
- `signer`: a callable `(stored_url) -> (url_or_None, expires_at_or_None)`: `s3://bucket/key` and S3 `https://` asset URLs presigned for 900 s with `S3Store.presign_get` (add `expires_in`); `data:(inline, not kept)` and anything else `None`; no media bucket: always `None`.

**Behaviour:** exactly the contract's "History" section: grouping of runs (`started_at` earliest, `last_message_at` latest, `title`/`bike`/`status`/`ticket` from the latest run); `ticket` from the ticket store by the summary's `ticket_id` (`{"reference", "status", "closed_at"}`; status `open` when the record is missing or has no `support_status`); `status` `handed_to_support` when `outcome == "escalated"`; `can_continue` within 48 h of `now`; `channel` filter; ordering `last_message_at` desc then `conversation_id`; opaque base64url keyset cursors, scoped by `user_key` always, `400 cursor_invalid` when undecodable. Messages: turns and notices merged in order of (`at`, then turns before notices at the same instant, then `n`/sequence); ids are the turn `_id` (`<conversation_id>#<n:05d>`) or the notice `_id` (`<conversation_id>#N<seq:05d>`); newest page by default (oldest first within it) with `older_cursor`; `before=<older_cursor>`; `after=<message id>` (`400 cursor_invalid` when the id is not in this conversation) with `more_after`. `sender`: `customer` to `rider`, `bot` to `bot`, notices `system`. Ownership: `404 conversation_not_found` unless `owner_of` is the rider's `user_key`. Limits 1-50 and 1-100 (`422` outside). Rate limit 60 a minute per rider (`429 rate_limited`). Store unavailable: `503 history_unavailable`. Token checker off: `503 history_unavailable`. Logs `amiigo_history_list` / `amiigo_history_messages` with `outcome`, `count`, `rider_hash`.

- [ ] Step 1: Failing tests (TestClient, mongomock and the in-memory store, a fake signer, a pinned clock): empty history; ordering; two runs grouped; channel filter; limit bounds 0 and 51 give 422; paging with no item lost or repeated; forged cursor 400; another rider's chats never listed; a deleted conversation never listed; ticket status open, closed (record with `support_status: "closed"`), missing record; `can_continue` either side of 48 h; messages newest page, `before` to the start, `after` the last id gives nothing and `more_after: false`, `after` in the middle; a notice merged in time order with `sender: "system"`; masked text as stored; attachment `s3://` signed, asset `https://` signed, `data:` null, no bucket null; 404 for another rider's id and a made-up id; every 401 code; 429 at the 61st request; 503 with the store down; `Cache-Control: no-store` on success and error; no log line holds the phone, token or text. Review Focus 5: a turn recorded between two page requests does not cause a skip or a repeat when the second page uses `before`.
- [ ] Step 2: Run; expected FAIL.
- [ ] Step 3: Implement.
- [ ] Step 4: Run; PASS. Whole suite.
- [ ] Step 5: Commit: `feat(amiigo): a rider's chats and messages, restored with GET`.

### Task 3: Uploads and deletion under `/amiigo/v1/`

**Files:**
- Modify: `src/emotorad_ai/amiigo/routes.py`; `src/emotorad_ai/api.py` only to extract the shared bodies of `post_upload` and the three erasure handlers into functions both paths call, with no behaviour change on the old paths.
- Test: `tests/test_amiigo_uploads_erasure.py`

**Behaviour:** `POST /amiigo/v1/uploads` takes `{"conversation_id", "mime_type", "size_bytes"}` with the rider from the header; the tree is `customers`; the conversation must be new or the rider's (`404 conversation_not_found`); answers `{"upload_id", "url", "headers", "expires_in": 300}`; `413 file_too_large`, `415 file_type_not_accepted`, `429 rate_limited` (20 a minute per rider), `503 storage_unavailable`. The cluster used for the key is the rider's, as `_cluster_for_session` gives today for a verified phone. `POST /amiigo/v1/erasure-requests`, `/status`, `/cancel`: the contract's bodies and answers, the rider from the header, `proof` = app sign-in, channel `amiigo_app`; cancel with nothing pending is `404 nothing_pending`; without `"confirm": true` is `400`.

- [ ] Step 1: Failing tests for each answer above and that the old `/uploads` and `/erasure-requests*` answers are unchanged (their existing tests stay green).
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS and whole suite. Step 5: commit `feat(amiigo): uploads and deletion requests with the rider's token`.

### Task 4: One turn-preparation function for HTTP and the socket

**Files:**
- Modify: `src/emotorad_ai/api.py`: extract from `post_message` (`api.py:1094`) everything between the parsed body and `runtime.handle(message)` into `prepare_turn(...) -> InboundMessage` (attachments, photo check, evidence check at ingest, video summary, origin, verified identity, cluster, pinned agent) and the `MessageOut` building into a helper; `post_message` becomes a thin caller. No behaviour change.
- Test: the existing `POST /message` tests must pass unchanged; add `tests/test_prepare_turn.py` proving the function gives the same `InboundMessage` as before for a text turn, a photo turn and an upload turn, and that it accepts a `channel` argument (`website_chat` default, `amiigo_app` for the socket) and an identity override (a verified phone from the token, which skips the session lookup).

**Interfaces (produces):** `prepare_turn(*, conversation_id, text, attachments, pill, screen, location, channel, session_token=None, em_aid=None, rider_phone=None, agent=None, client_ip=None) -> InboundMessage` (exact signature as the extraction needs; record it in the report for Task 5).

- [ ] Step 1: Write `tests/test_prepare_turn.py`; FAIL. Step 2: extract. Step 3: all `POST /message` tests and the new ones PASS; whole suite. Step 4: commit `refactor(api): one turn-preparation function for every way a message arrives`.

### Task 5: The chat socket `/amiigo/v1/chat`

**Files:**
- Create: `src/emotorad_ai/amiigo/socket.py` (protocol), `src/emotorad_ai/amiigo/sockets.py` (registry of open sockets by `user_key`, used by Task 6), store support for sent-message receipts (a new collection `amiigo_receipts`, TTL 24 h, Mongo and in-memory: `_id` `<user_key>#<client_message_id>`, `conversation_id`, `ack`, `reply`, `state` `processing|done`, `at`).
- Modify: `src/emotorad_ai/amiigo/routes.py` (the websocket route), `src/emotorad_ai/stores/mongo.py` `INDEXES` (receipts TTL).
- Test: `tests/test_amiigo_socket.py` (TestClient `websocket_connect` with headers; a fake runtime where useful, and one end-to-end turn through the real offline runtime).

**Behaviour (the contract's "The chat socket" section, all of it):** handshake reads `Authorization`; a bad token closes with `4401` and the code as reason before `ready`; `ready` `{"type":"ready","protocol":1,"server_time"}` within 2 s; frames are JSON text up to 64 KB, else `error bad_frame` (three bad frames in a row close with `1008`); `ping` gives `pong`; 10 minutes with no frame closes `4408 idle`; the socket closes `4401 token_expired` when the token's `exp` passes (a reply being prepared is still saved and, if the socket is still open, sent first); `message` validation (`client_message_id` UUID, `conversation_id` UUID, `text` ≤ 4,000 characters, ≤ 3 attachments, each `{"upload_id"}` only; codes `text_too_long`, `too_many_attachments`, `bad_frame`, `upload_not_found`, `upload_not_finished`); a `conversation_id` whose owner is another rider gives `conversation_not_found`; one message at a time per conversation (`conversation_busy`, with the message not handled); the receipt store makes a repeated `client_message_id` answer with the stored `ack` and, once it exists, the stored `reply`, never a second turn, across sockets and reconnects (Review Focus 2); `ack` carries the rider's message as `message_view` of the stored turn; `bot_typing` `thinking`, or `looking_at_video` when an attachment is a video; the turn runs `runtime.handle(prepare_turn(..., channel="amiigo_app", rider_phone=rider.phone))` off the event loop (thread executor), so the socket keeps answering `ping`; `reply` carries `message_view` of the stored bot turn plus `actions`, `escalated`, `ticket` (`{"reference","status":"open","closed_at":null}` when the reply raised one, else `null`) and `handled_by`; 20 messages a minute per rider (`rate_limited`); the registry adds and removes the socket; the conversation's `channel` is `amiigo_app` and its first reply carries the disclosure line. Logs: `amiigo_socket_open`, `amiigo_socket_close` (code, reason), `amiigo_message` (outcome, `rider_hash`); never text or ids that identify the rider.

- [ ] Step 1: Failing tests for every point above, including Review Focus 1, 2 and 3, and an end-to-end: open, send "my battery is not charging", get `ack`, `bot_typing`, `reply` with the disclosure line; then `GET /amiigo/v1/conversations` lists it with `channel: "amiigo_app"` and `GET .../messages` returns both messages with the same ids the frames carried.
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS, whole suite. Step 5: commit `feat(amiigo): the chat socket`.

### Task 6: Ticket closure from Zoho Desk

**Files:**
- Create: `src/emotorad_ai/amiigo/tickets.py` (`close_ticket(stores, registry, *, zoho_ticket_id, closed_at, now) -> str` outcome: `closed`, `already_closed`, `not_ours`, `unknown`), the webhook route in `src/emotorad_ai/amiigo/routes.py` or a `webhooks.py` beside it, `docs/api-shapes/zoho-webhook-ticket-update.json`.
- Modify: ticket stores (find a record by `zoho.ticket_id`; set `support_status: "closed"` and `closed_at` once, atomically), conversation stores (write a notice: `_id` `<conversation_id>#N<seq:05d>`, `conversation_id`, `user_key`, `kind: "ticket_closed"`, `text`, `at`), `src/emotorad_ai/amiigo/sockets.py` (push to every open socket of the rider).
- Test: `tests/test_amiigo_ticket_close.py`

**Behaviour:** The notice text (draft, as the contract shows): `Your support request {reference} was closed by our support team.` The push is `ticket_update` exactly as the contract shows, to every open socket of the ticket's rider; a rider with no open socket gets it through history. A repeat of the same closure changes nothing and sends nothing (Review Focus 4); a Zoho ticket id that is not in our records is `not_ours` and ignored with a log line. The webhook: `POST /webhooks/zoho/tickets`, authenticated by a shared secret from `EMOTORAD_ZOHO_WEBHOOK_SECRET` compared in constant time (no secret configured: the route answers 503 and does nothing). Before writing the parser, read Zoho Desk's own webhook documentation (Zoho Desk API docs, "Webhooks"), choose the strongest authentication Zoho Desk supports for webhooks (a custom header if it supports one; otherwise a secret path segment, never a query parameter), and capture Zoho's documented sample payload for a ticket update into `docs/api-shapes/zoho-webhook-ticket-update.json` with its source URL and a line saying it is from the documentation, not yet a live capture. Parse only the fields that sample carries (the ticket id and its status, and the time if present); a status other than Zoho's closed status is ignored in v1. Answers: 200 for any authenticated request (Zoho retries on errors), 401 for a wrong or missing secret. Logs `zoho_webhook` with `outcome` and the Zoho ticket id's hash, never the payload.

- [ ] Step 1: Failing tests: close once (record closed, notice stored, push to two open sockets of the rider and none to another rider's), repeat does nothing, not ours, unknown status ignored, webhook auth (missing, wrong, right), no secret configured 503, the documented sample parses, history afterwards shows `ticket.status: "closed"` and the `system` notice in order; `delete_conversation` removes notices.
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS, whole suite. Step 5: commit `feat(amiigo): ticket closure from Zoho Desk reaches the rider`.

### Task 7: Documents and set-up

**Files:**
- Modify: `docs/contracts/amiigo-support-chat.md` ("Status and scope": built on branch `feat/amiigo-history`, on staging once deployed; any change the build forced, listed), `CLAUDE.md` (one dated bullet under "Rules distilled from the build log"), `docs/runbooks/config-store.md` (`EMOTORAD_AMIIGO_PUBLIC_KEY`, `EMOTORAD_ZOHO_WEBHOOK_SECRET`: what each is and who sets it; never the values), `docs/Emotorad_Build_Log.md` (a 6 October entry), `scripts/mongo_setup.py` only if the new collections need anything beyond `INDEXES`.
- [ ] Step 1: Make the edits; the whole suite; commit `docs: the Amiigo support chat v1 is built`.
