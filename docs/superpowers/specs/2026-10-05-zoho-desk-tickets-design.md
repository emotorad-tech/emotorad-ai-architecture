# Real Zoho Desk tickets for the customer chatbot

Date: 5 October 2026. Branch: `feat/zoho-desk-tickets`, fast-forwarded to
`feat/integrate-jev-mongo` at `9cc10af`. Pull requests target
`feat/integrate-jev-mongo`, which holds all current work. (The repo
`CLAUDE.md` still names `feat/integration`; reconciling the two is a separate,
parked item.)

Reviewed on 5 October 2026 by five independent readers (code claims,
durability, safety and privacy, consistency, Zoho's API), then by a coverage
check and a cold read. Their findings are folded in.

## Purpose

Every ticket the bot raises today is a mock: an `EM-00001` number in a Python
dict that exists nowhere else and is lost at restart (`tools/mocks.py:401-419`).
The customer is told a person will call, and nobody does. The person (Sagnik)
wants real tickets in EMotorad's Zoho Desk, using the production Zoho keys
Sachin approved.

The person's decisions (5 October 2026):

- **Client:** use the OMS's existing Zoho OAuth client, not a new one.
- **Handovers that create a ticket:** the agents' `create_support_ticket`, the
  safety branch, the unverified intake ticket, "talk to a human" and the
  verify-first lock-out. Also the late-warranty proof submission, which uses
  the same ticket code.
- **Test isolation:** a test department in Zoho, and a guard in code that sends
  nothing to the real department until Sachin signs off.
- **Dealers:** later, with W1. Dealer tickets stay on the mock.
- **Zoho is called after the reply,** by a background worker. A turn never waits
  for Zoho.
- **An intake ticket needs a number to call back.** Without one, the tool
  refuses and the bot asks for a number.

## What exists today

- **The OMS already creates Desk tickets** (`em-biz-backend/zoho/zoho_api_client.py`)
  on the India data centre, into one department (`:215`), with the contact sent
  inline (`:247-252`) and dealer-shaped custom fields (`:216-233`).
- **The OMS's keys hold no refresh token.** The OMS keeps it only in Postgres,
  in `em_zoho_token` (`zoho/models.py:7-18`). Its grant-code login is commented
  out (`:163`), so it cannot replace a lost token.
- **The OMS's client secret is committed** in
  `em-biz-backend/embiz-backend-server.yaml` (lines 83 to 86, with
  `MSG91_SECRET` at 90). Reported to the person on 5 October 2026.
- **The ticket seam** is `MockTicketSystem`, with `create(**payload)` and
  `attach_transcript(ticket_id, text)` (`mocks.py:401-419`). It is passed in
  through `build_registry(ticket_system=...)` (`:457`, `:521`, `:538`). Its
  callers are `create_support_ticket` (`:925-1002`), `raise_intake_ticket`
  (`:697-768`) and `submit_warranty_proof` (`:1035-1108`, which returns
  `reference`, not `ticket_id`). The safety branch and its backstop call
  `create_support_ticket` through the registry (`runtime.py:1473-1609`;
  backstop `:1528-1559`).
- **One registry serves every persona** (`api.py:185-234`). The runtime's tool
  contexts carry no persona and no dealer id (`agents/base.py:200-205`,
  `runtime.py:1006-1010`, `:1514-1519`). So a tool cannot tell a dealer from a
  customer. (Dealer tools are affected too; that is a separate task.)
- **Most handovers create nothing.**
  - "Talk to a human" logs an escalation with ticket `None`
    (`runtime.py:780-791`), yet says "I have passed on this conversation"
    (`guardrails.py:124-127`).
  - The lock-out says "I'm passing you to our support team"
    (`verify_first.py:133-145`, `:201-209`) and records nothing.
  - The safety branch raises a ticket only when a phone is resolved
    (`runtime.py:1572`), yet always says "They will call you on the number
    linked to your account" (`guardrails.py:113-122`).
- **A transcript is attached only on the turn that raised the ticket**
  (`runtime.py:493-508`). It covers every run of the conversation id
  (`conversation.py:504-505`), including an earlier person's run that
  verify-first separated (`conversation.py:166-190`).
- **Idempotency** is a receipt keyed `<conversation_id>:<tool>:<key>`
  (`registry.py:269`), kept 7 days and released on any error (`:300-306`). It is
  not scoped by run, so a key the model reuses after a restart for a new person
  returns the first person's result. The key never reaches `tickets.create`.
- **There is no background worker, outbox or OAuth code** in the service.

## How the work is split

Five parts. Each has its own plan and pull request, in this order, and ends at
an exit check before the next begins.

| Part | What | Zoho calls | Exit check |
|---|---|---|---|
| 1. Access and real shapes | The scripts in section 10; person steps 1 to 6 | Person-run only | Client type known and safe; the open questions answered (see "Open until part 1"); masked shapes committed in `docs/api-shapes/zoho-*.json` |
| 2. The ticket record | The seam (`source_key`, `add_note`, the router), persona and run on `ToolContext`, receipts scoped by run, the injected facts, the `tickets` and `counters` collections, run bounds, the intake and warranty-proof changes, erasure of records | None. Zoho stays off | Whole suite green; only the behaviour changes listed under part 2 in "Rollout" |
| 3. Zoho client and worker | The switch and start-up checks, `api.py` wiring, the token, the worker, field mapping, Zoho's answers, health, redaction, alarms, the listing script, entry points kept on the mock, the app contract (except wording) | From staging, test department only | Person steps 7 to 9 done; the four existing handovers reach the test department with this run's transcript and photos |
| 4. Conversation changes | The handover and lock-out tickets, the callback-number gate, safety without a phone, the store-down safety reply, caps, final texts, the contract wording | Same | Person step 10 done before merge; the new paths reach the test department |
| 5. Go live | Person step 11, the live environment, the erasure wording | Real department | Sachin's sign-off recorded |

## Design

### 1. Access and keys

**The client and who grants.** The chatbot uses the OMS's client id and secret
with **its own refresh token**. Zoho keeps at most "20 active refresh tokens …
by a client per user" and invalidates the oldest past that
(https://www.zoho.com/accounts/protocol/oauth/token-limits.html). What is safe
depends on the client's type. The Zoho admin confirms it in person step 2,
before anything else.

- **Server-based client.** Any user in the organisation can approve it in a
  browser. The grant is made by a **different Zoho user** from the one whose
  token the OMS holds: ideally an integration user with a Desk licence and access
  to the test department. The OMS's token cannot be dropped.
- **Self Client.** Only the console owner can generate a code, so the grant would
  be the OMS token owner's. A new grant could drop the OMS's token, which the OMS
  cannot get back by itself. **Part 1 stops here** and goes back to the person
  and Sachin. The options are a separate client for the chatbot, or an explicit
  acceptance of the risk with a restore plan: a reviewed script that writes
  `em_zoho_token`, run by a person, with an audit trail.

The code points both ways. The OMS sends a redirect address when it exchanges a
code (`zoho_api_client.py:112`), which suggests server-based. It also keeps a
grant code as configuration (`ZOHO_AUTH_CODE`, `:111`), the Self Client
pattern. Hence the check.

**Settings,** in the chatbot's own secret `/emotorad/<env>/ai/app`, never the
OMS's:

| Setting | Secret | Needed | What it is |
|---|---|---|---|
| `EMOTORAD_ZOHO_REFRESH_TOKEN` | yes | always | The chatbot's own refresh token. **The switch** |
| `EMOTORAD_ZOHO_CLIENT_ID` | yes | always | The OMS's client id |
| `EMOTORAD_ZOHO_CLIENT_SECRET` | yes | always | The OMS's client secret |
| `EMOTORAD_ZOHO_ORG_ID` | no | always | Desk organisation id |
| `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID` | no | always | The test department |
| `EMOTORAD_ZOHO_TEST_CONTACT_ID` | no | always | The test contact |
| `EMOTORAD_ZOHO_DEPARTMENT_ID` | no | live | The real department |
| `EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID` | no | live | The "Unverified AI chat" contact |
| `EMOTORAD_ZOHO_LIVE` | no | live | Exactly `yes` sends to the real department |

`EMOTORAD_AI_ENV` (already set by the deploy, `deploy-staging.yml:102`) is also
required. The accounts and Desk addresses are constants
(`https://accounts.zoho.in`, `https://desk.zoho.in`). The custom field API
names, priority values, channel value and the credits floor are constants, taken
from part 1.

**The switch.** Zoho is off when `EMOTORAD_ZOHO_REFRESH_TOKEN` is unset. The
mock is used, as today, and `/health` says `not configured`.

When it is set, the service runs these checks at start-up, in order:

1. Every setting the mode needs is present, and `EMOTORAD_AI_ENV` is set.
2. `AWS_REGION` does not begin with `eu-`. EU data stays in `eu-central-1`
   (`CLAUDE.md`; Risk Register §16-20).
3. The store is MongoDB, and the `tickets` collection has its unique
   `source_key` index (a read of the index list).
4. Live mode only: phone verification is real. Live is refused while
   `EMOTORAD_AI_DEV_CODES` is on or the OTP sender is the mock (`api.py:148`).
   Staging runs both today (`deploy-staging.yml:102`), so live tickets wait for
   an environment with real verification.

If any check fails, **the mock is used, exactly as when Zoho is off**. Nothing is
recorded in `tickets`. `/health` says `misconfigured: <reason>` (or
`not allowed in this region`), and `zoho_misconfigured` is logged at error level
and alarmed (section 8). There is no state in which tickets are recorded but
cannot be sent safely.

**Mode stamp.** Each record is stamped `test` or `live` when it is recorded. The
worker sends only records whose stamp matches the current mode. Others are
`held`: they appear on `/health` and in the listing script (section 10), and
are never sent. So switching modes never sends test chats to the real
department, or real customers' tickets to the test one.

**The access token.** One token is kept in memory in the API process, behind a
lock, and refreshed five minutes before its hour ends. The token endpoint's
answer is read by its body, whatever the HTTP status:

- `"error": "Access Denied"`. Zoho returns this with HTTP 200 when throttling
  (10 requests in 10 minutes per refresh token). No token request is made for 10
  minutes.
- `invalid_code`, `invalid_client` or `invalid_client_secret`. The last is what
  rotating the OMS secret without updating ours gives. `/health` says
  `token refused: <error name>`, `zoho_token_refused` is logged and alarmed, and
  the worker tries again hourly.

An access token can also die early, because Zoho keeps at most 10 per refresh
token, and the person's scripts use the same refresh token. So a Desk answer of
401 `INVALID_OAUTH` drops the cached token, refreshes once and retries the same
step once.

The token is never written to the store or a log.

### 2. The ticket seam

The interface becomes:

- `create(source_key, persona, **fields) -> {ticket_id, status}`. The same
  `source_key` always returns the same ticket.
- `attach_transcript(ticket_id, text)`. Called at the end of every customer turn
  whose run holds a ticket, not only the turn that raised it.
- `add_note(ticket_id, text)`. New: a short line for the ticket, for example
  "Customer asked for a person at 14:02".
- `close_runs(conversation_id, new_started_at)`. New: marks where earlier runs of
  a conversation ended (section 3).

Three implementations:

- **`MockTicketSystem`** keeps its `EM-%05d` numbers. It gains the `source_key`
  look-up, `add_note` (kept in its dict) and a do-nothing `close_runs`. Tests,
  the playground, the CLI and the live evaluation keep using it.
- **`DeskTicketSystem`** (new) records tickets in the store. It never calls Zoho.
  Its `attach_transcript` ignores the text, because the worker reads the
  conversation store itself (section 4). It marks the record as having new
  content.
- **`TicketRouter`** (new) is what the registry holds when Zoho is on. `create`
  sends customer tickets to Desk, and every other persona's, or a call with no
  persona, to the mock. The other calls go by the id: seven digits
  (`EM-1000001` up) to Desk, five digits to the mock.

**`ToolContext`** (`tools/registry.py`, Tier-1) gains `persona` and
`started_at`. Both are filled in by the three places the runtime builds a context
for ticket-raising calls (`agents/base.py:200`, `runtime.py:1006`,
`runtime.py:1514`). The model never sees or sets them.

**Receipts are scoped by run.** The registry's key becomes
`<conversation_id>:<started_at>:<tool>:<key>` when the context has a
`started_at` (`registry.py:269`), and stays as today when it does not
(verify-first's calls). So a key the model reuses in a new run, including a new
person's run after `restart_for`, raises a new ticket. Receipts written before
the change no longer match, so one retry that straddles the deploy could
duplicate a mock ticket. That is accepted.

**Injected facts** for the three ticket tools. None is in the model's schema.

| Fact | Where the runtime fills it | `create_support_ticket` | `raise_intake_ticket` | `submit_warranty_proof` |
|---|---|---|---|---|
| `conversation_id` | `ToolContext` | required | required (today) | required |
| `started_at` | `ToolContext` | required | required | required |
| `persona` | `ToolContext` | optional; absent means the mock | same | same |
| `channel` | the facts dict (`runtime.py:1228`) and the safety branch's late facts (`:1497`) | optional | optional | optional |
| `identity_strength` | same | optional; absent means unverified | same | same |
| `coverage` | same (section 3) | optional | no | optional |
| `typed_number` | same: the latest number the customer typed in this run | no | optional | no |

Each tool builds `source_key` as
`<conversation_id>:<started_at>:<tool>:<idempotency_key>`. The safety branch
keeps its key `safety:<cid>:<started_at>`. The part 4 gates use the same
`<conversation_id>:<started_at>:` prefix with `safety_callback`, `handover` or
`lockout`.

**Unverified tickets** are written straight to the seam, never through
`create_support_ticket` with a typed phone. These are safety without a known
phone, a handover with a typed number, the lock-out, and intake by a typed
number. So no bike is looked up for a number nobody proved (`mocks.py:985-986`
would put its real owner's bike on the ticket). An ASSERTED phone (caller ID,
`identity.py:378-385`) going through `create_support_ticket` skips the bike
look-up too.

### 3. The ticket record

A new collection, `tickets`, in both stores. Its indexes go in
`stores/mongo.py` `INDEXES`: unique `source_key`; `state` with
`next_attempt_at`; `phone`; `conversation_id`. `counters` joins `INDEXES` with no
index. Both join `PERMANENT` in `scripts/mongo_setup.py` and
`erasure_admin.py:37`.

**Kinds.** Kind is set in code, by the caller, never from the model's category:

| Kind | Raised by | Identity | Bike | Summary | Subject label |
|---|---|---|---|---|---|
| `support` | the model, `create_support_ticket` | verified, or unverified for an ASSERTED phone | when verified | written by the AI | from the category, for example "Battery: charging" |
| `safety` | the safety branch and backstop, or the callback gate | verified or unverified | when verified | built by code: the customer's words, the matched terms, what the photo or video analyser saw | "SAFETY" |
| `handover` | the handover gate | verified or unverified | when verified and chosen | none: "Customer asked for a person" | "Asked for a person" |
| `lockout` | verify-first | unverified | none | none: built by code | "Could not verify" |
| `intake` | the model, `raise_intake_ticket` | verified or unverified | none | written by the AI, with the customer's claims | "Unverified customer" |
| `warranty_proof` | the model, `submit_warranty_proof` | verified | the frame number the customer gave, as a claim | written by the AI, with the claims | "Late warranty registration" |

**Urgent** means kind `safety`, or a `support` ticket whose category is
`battery_safety`. Urgent tickets are taken first, alarm at 10 minutes, are high
priority, and are exempt from the credits floor and the caps.

**Fields:**

| Field | Meaning |
|---|---|
| `_id` | Our reference, `EM-1000001` upwards, from a `counters` document (`$inc`). Seven digits, so it never matches an old mock number, and is never read as a six-digit one-time code (`verify_first.py:50`). Both reference patterns accept it (`runtime.py:222`, `one_step.py:48`). |
| `chat_reference` | `<EMOTORAD_AI_ENV>:<_id>`, for example `stage:EM-1000001`. Written to Zoho's chat-reference field, so it is unique across deployments that share the Zoho organisation. |
| `source_key` | Unique. A second `create` with the same key returns the first document. |
| `mode` | `test` or `live`. |
| `kind`, `urgent` | As above. |
| `conversation_id`, `cluster_id` | From the context. |
| `started_at`, `ended_at` | The run's bounds. `ended_at` is set by `close_runs` when a new run begins on the conversation: a fresh state, or `restart_for`. The worker posts only turns and media whose time is at or after `started_at` and before `ended_at`, and media of the same cluster. |
| `channel`, `created_at` | From the facts. |
| `phone`, `identity` | The number to call back, in `+91` form. `verified` means identity strength VERIFIED: WhatsApp, Amiigo sign-in, or a passed code. Anything else is `unverified`. |
| `category`, `ai_severity`, `summary` | From the caller. An AI-written `summary` passes through `redact_pii` before it is stored. |
| `claims` | For intake and warranty proof: the stated name, contact and evidence, the claimed purchase date and purchase channel. Labelled as the customer's claims. `submit_warranty_proof`'s `proof_url` stays in the schema, is ignored and is never recorded (the model never sees a URL). |
| `bike` | Model, frame number and its source, when the identity is verified. |
| `coverage` | The ticket bike's `coverage_status` (`computed`, `computed_from_registration`, `purchase_date_missing`, `not_registered`, `warranty_unknown`, `warranty_unavailable`). Or the run's last look-up error (`no_warranty_record`, `oms_unavailable`), which a new state field keeps, because `coverage_result` drops errors (`runtime.py:386-387`). `forget_bike` clears both. |
| `customer_name` | Only from an OMS warranty record (`warranty_on_record` not False). Never an app username (`tools/amigo.py:224-232`) or a typed name. |
| `notes` | Lines from `add_note`. |
| `zoho` | `contact_id`, `ticket_id`, `ticket_number`, `web_url`, and the ids of every comment and attachment posted. The ids are kept so that erasing Zoho copies stays possible later. |
| `posted_turns`, `posted_media`, `posted_notes` | What is already on Zoho. |
| `state` | `waiting`, `sent`, `stuck`, `held` or `gone` (the Zoho ticket was deleted or merged in Desk). |
| `due_since` | When the record last went from fully sent to having work outstanding, or its creation. It does not move while work is outstanding. |
| `wake` | A counter `attach_transcript` and `add_note` increase. |
| `attempts`, `next_attempt_at`, `lease_until`, `lease_token`, `intent`, `last_error` | Worker bookkeeping. `intent` is set before each Zoho write (section 4). `last_error` holds an error class and Zoho's error code only. |

The record holds no transcript copy; the worker reads the conversation store.
Records are permanent, like transcripts, and erased with the person (section 11).

When Zoho is on, `summarise_past` drops five-digit `EM-` references (old mock
numbers) from the model's context, so the bot never quotes a ticket that exists
nowhere (`enrichment.py:126-127`, `runtime.py:235-241`).

### 4. The worker

**Life.**
- A daemon thread, started in the API's lifespan hook (`api.py:290`) when Zoho is
  on. Never at import. A stop flag is set on shutdown.
- Never started by the playground, the CLI, the live evaluation, the local chat
  page (which withholds the keys) or the tests.
- Each loop pass catches every exception, logs `zoho_worker_error` with its class
  (alarmed), waits 30 seconds and carries on.
- `/health` shows whether it is running, its last pass, and the age of the oldest
  due record.

**When a record is due.**
- `create` sets `next_attempt_at` two minutes ahead. A turn that dies before its
  end still gets its ticket sent.
- At the end of the turn, `attach_transcript` sets it to now and increases
  `wake`. So Zoho is first called after the reply.
- On every later turn of the run, `attach_transcript` does the same for a `sent`
  record. The worker, not the turn, decides whether anything is outstanding.
- `gone` records take no wakes.
- The worker also passes every 30 seconds.

**Taking a record.**
- The due query takes `waiting` and `stuck` records of the current mode whose
  `next_attempt_at` has passed and whose lease has expired. Urgent ones come
  first, then the oldest.
- Taking one sets `lease_until` five minutes ahead and a fresh `lease_token`, in
  one atomic update. It also reads `wake`.
- The lease is renewed before each Zoho call.
- Every save is conditional on `_id` and `lease_token`, and is never an upsert.
  When nothing matches, the worker drops the record: erasure removed it, or
  another worker took it.
- The final save is also conditional on `wake` being unchanged. If it changed,
  the record goes back to `waiting`, due now.
- `posted_*` lists grow with `$addToSet`.
- One Zoho call is in flight at a time.

**Steps,** each saved before the next, so a retry resumes where it stopped.
Before each Zoho write the worker saves `intent` (what it is about to do), and
does not send if that save fails. It clears `intent` in the same update that
saves the result. A resume that finds an `intent` with no result looks before
writing again.

1. **Contact.**
   - Test mode: always `EMOTORAD_ZOHO_TEST_CONTACT_ID`. No contact is searched
     or created, because contacts belong to the whole organisation, not to a
     department.
   - Live, unverified: always `EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID`. A number
     nobody proved never lands on a real customer's contact.
   - Live, verified:
     - First, a `contact_id` from our own live, verified records for this
       phone. Never one from a test or unverified record.
     - Otherwise, search Zoho's contacts for `phone=*<last 10 digits>`, then
       for `mobile=*<last 10 digits>`, in separate calls.
     - One match is used. With several, the one whose name equals
       `customer_name` is used; failing that, a new contact is created.
     - With no match, a new contact is created: last name from
       `customer_name`, or "AI chat customer"; the number in `mobile`; no
       email.
     - A contact create is an `intent` like any write: on resume, search again
       before creating.
     - A 404 on a stored contact clears the id and searches again.
2. **Ticket.** Create it (section 5).
   - After an unknown outcome, list the contact's tickets: newest first, this
     department, created after the record. Adopt the one whose chat-reference
     field equals `chat_reference` exactly.
   - The contact's ticket list is used rather than Zoho's search, which can lag
     by "a few minutes" (https://desk.zoho.com/DeskAPIDocument#Search).
   - If part 1 shows the list lags too, the look-up retries at 2, 5 and 10
     minutes before creating.
3. **Transcript.**
   - The run's turns not in `posted_turns`, rendered by `render_transcript`,
     posted as private plain-text comments (`isPublic=false`,
     `contentType=plainText`).
   - Each comment is at most 30,000 characters, split on line boundaries. Zoho's
     limit is 32,000 (https://desk.zoho.com/DeskAPIDocument#TicketsComments).
   - Each comment starts with a marker, for example
     `[stage:EM-1000001 transcript, turns 7-12]`. The first says times are in
     UTC.
   - After an unknown outcome, the ticket's comments are listed, and a marker
     already there is not posted again.
4. **Notes.** Each note not yet posted, as a private comment with a marker.
5. **Attachments.**
   - The run's media not in `posted_media`.
   - A file whose recorded `size_bytes` is over the limit (20 MB until part 1
     proves otherwise) is not read. A private comment says a photo or video was
     too large to attach and is kept by the AI team, and the key is marked
     posted.
   - Others are read from S3 (`S3Store.get_bytes`) and uploaded to
     `/api/v1/tickets/{id}/attachments?isPublic=false` (multipart field
     `file`). The file name starts with the reference.
   - After an unknown outcome, the ticket's attachments are listed first.

When nothing is outstanding, the record is `sent`.

**Retries.** Waits of 30 s, 1 min, 2 min, 5 min, 10 min and 30 min, then
hourly. Nothing is ever dropped. A record with outstanding work is marked
`stuck`, and logged at error level once an hour by reference and age only:
- `safety_ticket_late` when an urgent record has waited 10 minutes from
  `due_since`;
- `zoho_ticket_stuck` when any other has waited 24 hours.

Both are alarmed. Stuck records are still retried.

**Zoho's answers,** classified by error code, not status alone
(https://desk.zoho.com/DeskAPIDocument#Errors):

| Answer | Treated as | Then |
|---|---|---|
| 401 `INVALID_OAUTH` | Expired access token | Refresh once, retry the step once |
| 401 or 403 `SCOPE_MISMATCH`, `OAUTH_ORG_MISMATCH`, `FORBIDDEN`, `LICENSE_ACCESS_LIMITED` | Configuration | `/health` says `sending failing: <code>`; retry hourly |
| 400 or 422 `INVALID_DATA` | Our payload | `zoho_rejected` with the field names Zoho named; retry hourly |
| 404 on a ticket we created | Deleted or merged in Desk | `gone`, logged, not retried |
| 404 on a contact | Deleted or merged in Desk | Clear the id, search again |
| 413 `RESOURCE_SIZE_EXCEEDED` | Attachment too large | The note, as in step 5 |
| 429 `TOO_MANY_REQUESTS` | Concurrency | Retry in 30 s |
| 429 `THRESHOLD_EXCEEDED` | Daily credits gone | Pause the worker until `Retry-After` |
| 204 on a search or list | No match | Carry on |
| 5xx or a network error on a read, or before a write was sent | Unavailable | The retry schedule |
| A timeout, a dropped connection or a 5xx after a write was sent | Unknown outcome | The look-up in steps 1 to 5 |

**Shared credits.** Daily API credits belong to the whole organisation, and
are shared with the OMS, whose own calls do not retry
(`zoho_api_client.py:263-267`). The worker logs `X-Rate-Limit-Remaining-v3`
with each sent ticket. It pauses non-urgent records while the remaining credits
are below a fixed floor, set in part 1 from the daily allowance found in person
step 2.

**Timeouts:** 8 seconds a call, 60 seconds an upload.

### 5. What goes on a Zoho ticket

| Zoho field | Value |
|---|---|
| `subject` | `[AI chat] <label> - <bike model, or "bike not given">`. `[Unverified]` comes first when unverified. No phone, no name. At most 255 characters. |
| `departmentId` | The test or real department, from the record's `mode`. |
| `contactId` | From step 1. |
| `phone` | The `+91` number. |
| `priority` | Set in code, never by the model: high when urgent, medium otherwise. Values from part 1. |
| `status` | Open. |
| `channel` | A system channel from part 1 (for example Chat). Never an integration or instant-message channel, which carry their own reply route. Our real channel goes in the description. |
| `cf` | `{"<chat reference API name>": chat_reference, "<source API name>": "AI chatbot"}`. Custom fields go only as `cf`, never as the deprecated `customFields` the OMS also sends. Each value at most 255 characters. |
| `description` | Plain text, in order: see below. |

The description, in this order:
- our reference; Source: AI chatbot; our channel;
- verified, or "number given in chat, not verified";
- kind and category;
- the bike (model, frame number and its source), when known;
- the coverage outcome, from code;
- for model-raised kinds: "AI's view of severity: …";
- the customer's claims, labelled as claims;
- the summary. For model-raised kinds it is headed "Summary written by the AI
  from the customer's words, not checked". For code-built kinds it is headed by
  what built it.
- For a lock-out: "Possible takeover attempt: verify only through the number on
  record, never a number from this chat."

Never set: the assignee, the team, a due date (the OMS sends IST time marked as
UTC, `zoho_api_client.py:203`), or the OMS's dealer fields. In particular, never
set "Dealer Principle Name", which the OMS webhook uses to copy Zoho tickets into
the OMS (`em-biz-backend/zoho/views.py:241-288`). Model text never chooses the
department, priority, status, contact or assignee.

### 6. The handovers that create a ticket

Customer persona only. WhatsApp and Amiigo always carry a verified phone. The
web chat may not.

**Parts 2 and 3: the three that raise one today, plus warranty proof.**

- **`create_support_ticket`.** Unchanged for the model: its schema, evidence
  rule and frame-number checks stay. Its reply keeps `ticket_id` (now `EM-1…`),
  `status` and `expected_response`.
- **Safety with a known phone** (the branch and its backstop). As today, through
  `create_support_ticket` with the run's safety key. One safety ticket per run: a
  later safety report in the same run adds a note to it.
- **`raise_intake_ticket`.**
  - The number to call back is the verified phone when there is one.
    Otherwise it is `typed_number`: the latest number the customer typed in
    this run, read by the runtime (`ascii_digits`, then `find_phone`) and kept
    in a new state field. Never the model's `stated_contact` text.
  - With neither, the tool refuses with `contact_number_required` and the
    remedy "ask the customer for a mobile number we can call".
  - Its description and the four `IntakeTicketTests` change to match. A new test
    covers the refusal.
  - It joins the ticket-producing tools (`agents/base.py:64`).
- **`submit_warranty_proof`.** Recorded as kind `warranty_proof`. The claimed
  date and purchase channel are recorded as claims, and the customer's invoice
  photo is attached in step 5. It returns `ticket_id` as well as `reference`, and
  joins the ticket-producing tools, so its ticket gets the transcript.

**Part 4: the conversation changes.** All are code, before any model, in the
runtime's gates. Each is tested with the model never called. **They record
tickets only when Zoho is on.** With Zoho off, the handover and the lock-out
behave as today, and safety without a phone uses the no-promise text below
without asking for a number.

- **One ticket per need in a run.** Before recording, a gate checks the run.
  - If the run already holds a Desk ticket that is not `gone`, a handover adds
    the note "Customer asked for a person" to it and quotes its reference.
  - A safety report adds a note to the run's safety ticket, if there is one.
- **Talk to a human.** The handover gate (`runtime.py:780-791`) reads a number
  from the same message first (`ascii_digits`, then `find_phone`), since "call
  me" is itself a trigger (`guardrails.py:83`).
  - With a known or just-typed number, it records a `handover` ticket and
    replies with the reference.
  - Without one, it asks for a number, and the conversation waits for one.
- **Safety without a known phone.** The safety gate reads a number from the same
  message first.
  - With one, it records an urgent `safety` ticket at once.
  - Without one, it gives the safety steps and the 112 line, asks for a number
    instead of promising a call, and the conversation waits for one.
- **The callback-number gate.** A new graph node, straight after `safety_gate`
  and before navigation, handover, erasure and verify-first, so a typed number is
  never taken as a number to verify. While waiting:
  - `EM-`, `BK-` and `RO-` references are removed before any number test, so a
    quoted reference is not read as a bad number.
  - A valid Indian mobile (`ascii_digits`, then `find_phone`): the gate records
    the ticket with that number, unverified, and replies with the confirmation
    and the reference. The number becomes `[phone]` in the model's history and
    in the transcript, as verify-first does.
  - Something that looks like a number but is not a valid Indian mobile: the
    gate says so, keeps waiting and replaces it with a placeholder in the
    history.
  - Anything else:
    - A handover wait ends with a line on how else to reach support (from
      person step 10).
    - A safety wait asks once more. If the next message has no number either,
      `safety_ticket_not_recorded` is logged at error level, alarmed and counted
      on `/health`.
  - "Start over" ends the wait.
  - `escalated: true` is set when a ticket is recorded or the handover wait
    ends, not on the question itself. This matches the app contract's meaning:
    the bot has stopped and a person will be in touch
    (`docs/contracts/amiigo-support-chat.md:77`).
- **Verify-first lock-out** (five wrong codes, or a fourth code asked for). It
  records a `lockout` ticket, unverified, with the first number found in:
  - this message;
  - the pending number (`VerificationStore.pending_phone`);
  - the last number a code was sent to (a new state field).
- **When the safety record cannot be written.** This covers the store being
  down (`_store_down`, `runtime.py:573-583`, which now runs `check_safety` on
  the message), a write error, or a tool error in the safety branch. The reply is
  the safety steps and the 112 line, with no promise of a call.
  `safety_ticket_not_recorded` is logged and alarmed.
- **Caps** on unverified tickets that are not urgent: at most 2 per number and 50
  in all, per calendar day in IST.
  - Past the per-number cap, the reply is "I can't take another request for that
    number today."
  - Past the overall cap, it is "I can't pass this on right now. Please try
    again tomorrow."
  - Both log `unverified_ticket_capped`, which is alarmed.
  - Urgent tickets are never capped.
- **Recording is a side effect for the save-conflict check.**
  - Each recording logs `ticket_recorded`, which `_side_effects_since`
    (`runtime.py:1103-1120`) counts. So a conflict merges the turn instead of
    re-running it.
  - Every one of these replies passes `ticket_id` to `_finish`, so the
    transcript attaches.
  - The new state fields (the wait, `typed_number`, the look-up error, the last
    number a code was sent to) are carried when a turn is merged onto fresh
    state.

**Not changed in this version.** Each goes to
`docs/Emotorad_Edge_Case_Register.md` as a CAPTURE row, named with the event
that counts it; the exact event names go in the part 4 plan. The rows:

- the evidence-not-forthcoming handover (`guardrail:evidence_not_forthcoming`);
- the coverage and order post-check blocks;
- a ticket promised with none behind it (`ticket_promise_unbacked`);
- agent loop failures;
- model and store outages, other than safety;
- the unsupported persona;
- triage's "unsupported topic";
- the prose handovers in the late-warranty, motor, dealer and photo-safety
  prompts;
- a request for a person in Hindi or Hinglish (the triggers are English only,
  `guardrails.py:75-97`);
- a non-Indian number typed for a call-back.

**Not done on purpose:**
- dealer tickets (W1);
- ticket status coming back from Zoho;
- an EU Zoho organisation;
- motor ticket categories;
- erasing the Zoho copies (section 11).

### 7. What the customer is told

Drafts. The support lead confirms or rewrites them in person step 10, before
part 4 merges. Until then no new text promises a channel or a time, as the app
contract asks (`docs/contracts/amiigo-support-chat.md:215`).

| When | Draft |
|---|---|
| Handover, ticket recorded | "I've passed this conversation to our support team, so you won't need to repeat yourself. They will be in touch. Your reference is EM-…" |
| Handover, no number known | "I can pass you to our support team. What mobile number can they reach you on?" |
| Safety, no number known | The safety steps; then "This is a safety issue, so I want our safety team to reach you. Please send me your mobile number and I'll pass this on straight away."; then the 112 line |
| Safety, record not written | The safety steps and the 112 line, with no promise of a call |
| Number received | "Thank you. I've passed this on. Your reference is EM-…" |
| Lock-out, ticket recorded | The lock-out text, then "Your reference is EM-…" |
| Caps | As in section 6 |

The disclosure line still reaches every new reply through `_outbound`.

### 8. Logging, redaction and alerting

- `access_token`, `refresh_token`, `client_secret` and `authorization` join the
  redacted key names (`observability.py:52`, Tier-1).
- `redact_pii` (Tier-1) learns three things:
  - Devanagari digits;
  - an Indian mobile number glued to Latin or Devanagari letters ("9876543210pls",
    "नंबर 9876543210पर");
  - `+<country code>` numbers.

  `IdentifiersAreNotPhonesTests` (`tests/test_log_redaction.py`) stays green,
  with a new case for an id that starts with ten digits followed by letters.
  `ascii_digits` moves to a module with no dependencies, so `observability` can
  use it without an import cycle. This matters more now, because transcripts go
  to a third party.
- Zoho failures are logged by error class and Zoho's error code, as `error=`,
  never `code=`. No request or response body is logged, unlike the OMS
  (`zoho_api_client.py:254`). Exception messages carry no token, secret, phone or
  body, because `registry.py:295-296` copies `str(exc)` to the model and the log.
- Events: `ticket_recorded`, `zoho_ticket_sent` (reference, Zoho number,
  attempts, credits remaining), `zoho_retry`, `zoho_rejected`,
  `zoho_token_refused`, `zoho_misconfigured`, `zoho_worker_error`,
  `zoho_ticket_stuck`, `safety_ticket_late`, `safety_ticket_not_recorded`,
  `unverified_ticket_capped`.
- **Alarms.** A CloudWatch metric filter on the deployment's log group (today
  `emotorad-ai-stage`). It watches `zoho_misconfigured`, `zoho_token_refused`,
  `zoho_worker_error`, `zoho_ticket_stuck`, `safety_ticket_late`,
  `safety_ticket_not_recorded` and `unverified_ticket_capped`, with an alarm
  that emails a named person (person step 9). It is defined in `infra/` with the
  log group as a parameter, and deployed by the person.

### 9. Health, local chat, playground and CLI

- `/health` gains `zoho`, with one of these values:
  - `not configured`
  - `test department`
  - `live`
  - `misconfigured: <reason>`
  - `not allowed in this region`
  - `token refused: <error>`
  - `sending failing: <code>`

  Whenever the `tickets` collection holds any record, it also gains
  `tickets_waiting`, `tickets_stuck`, `tickets_held`, the worker's state and the
  age of the oldest due record.

  The pinned dictionary in `tests/test_api_health.py` changes, and that test
  blanks every `EMOTORAD_ZOHO_*` name.
- `TicketRouter` and `DeskTicketSystem` are chosen in `api.py` only. The CLI,
  the live evaluation and the playground keep `MockTicketSystem` even with every
  Zoho setting present (`docker/start.py` passes the whole environment to
  Streamlit). Tests assert this.
- `scripts/chat_local.py` adds every `EMOTORAD_ZOHO_*` name to `WITHHELD`.
- The whole-suite command blanks every `EMOTORAD_ZOHO_*` name, as well as the
  names it already blanks (see the `env -u` list in
  `docs/superpowers/plans/2026-10-02-greeting-going-back-confirm.md`).

### 10. Scripts the person runs (`scripts/zoho/`)

All are committed, with no secrets in them. Each takes secrets by hidden input,
keeps them in memory, and prints names, ids and counts. The one exception is
`exchange_code.py`, whose job is to show the refresh token once.

A Claude session never runs them. The person runs them in a terminal window
outside the Claude app (the app's Terminal panel can be read by the session),
and clears the scrollback afterwards.

| Script | Part | What it does |
|---|---|---|
| `consent_url.py` | 1 | Prints `https://accounts.zoho.in/oauth/v2/auth?response_type=code&client_id=…&scope=<comma-separated>&redirect_uri=…&access_type=offline&prompt=consent`. For a server-based client only. |
| `exchange_code.py` | 1 | Swaps the code for the refresh token within two minutes: with the redirect address for a server-based client, without it in Self Client mode. Refuses unless Zoho's answer says the India data centre. Replaces `reports/zoho-probe/exchange_code.py`. |
| `probe.py` | 1 | Read only. Checks that the token's organisation is the org id given. Lists the departments; the ticket layouts and fields of the test and real departments; the contact layout and fields; the channels; and the test and unverified contacts. Writes masked shapes to `docs/api-shapes/zoho-*.json` (`.claude/rules/testing.md`) and prints the scopes granted. Replaces `reports/zoho-probe/zoho_desk_probe.py`. |
| `test_ticket.py` | 1 | Writes in the test department only (see below). |
| `revoke.py` | 1 | Revokes the chatbot's refresh token, as Zoho's revoke page documents: `POST https://accounts.zoho.in/oauth/v2/revoke/token`, form field `token`. Revoking through Zoho's Connected Apps page works per app, and could revoke the OMS's token too. |
| `tickets_report.py` | 3 | Read only, against the deployment's store. Lists waiting, stuck and held records: reference, Zoho number, state, mode and the last four digits of the number. For the support lead before a rollback. |

`test_ticket.py`, in detail:
- It refuses unless the department id given is the one named "AI chatbot test"
  in the probe's shapes. With `--real-department` it writes once to the real
  department instead, after the person types that department's name. That run is
  person step 11's supervised ticket.
- On the test contact, it searches contacts by the test number (read only, to
  record the search shape).
- It creates a ticket with a fixed `stage:EM-TEST-<n>` chat reference and the
  fake frame number from the fixtures.
- It reads the ticket back.
- It runs its own look-up: it lists the contact's tickets and matches the chat
  reference exactly, straight away and after 2 minutes. It then makes a second
  create attempt only if the look-up finds nothing, so it should make none.
- It adds a private comment, then uploads a small image and files of 19 MB and
  26 MB.
- It saves the masked shapes. The person closes the ticket in Desk afterwards.

Scopes requested, comma-separated: `Desk.tickets.CREATE`, `Desk.tickets.UPDATE`,
`Desk.tickets.READ`, `Desk.search.READ`, `Desk.contacts.READ`,
`Desk.contacts.CREATE`, `Desk.basic.READ`, `Desk.settings.READ`.
`Desk.settings.READ` is included in case the layout calls need it, because a
second grant would mean another refresh token on the OMS's client.
`Desk.basic.CREATE` is not: it is only needed for `/uploads`, which this design
does not use.

### 11. Erasure

- `scripts/delete_person.py` and `erasure_admin delete` also delete `tickets`
  records. They find them through the person's conversations **and** by `phone`,
  because anonymous chats have no `user_key`.
- `erasure_admin`'s totals include those records, so a ticket recorded after
  `show` makes `delete` refuse, as other changes do (`erasure_admin.py:37`,
  `:200-203`).
- `show` lists the person's references, Zoho ticket numbers and any record still
  waiting, and flags them.
- `delete` refuses while one of them is leased by the worker. A waiting record is
  deleted with the rest, and the deletion log says a promised call was
  cancelled.
- **The customer-facing wording becomes untrue once copies reach Zoho.** The
  erasure dialogue promises to delete "your past chats with me, the photos and
  videos you sent". It keeps only service tickets (`erasure.py:56-61`). Under
  this design both are copied onto Zoho tickets. Before live, Sachin chooses
  (person step 11):
  - **(a)** Change the dialogue and the app contract to say a support ticket
    keeps its copy of the chat and photos. This is a text change in part 5's
    pull request.
  - **(b)** Make erasure delete the Zoho comments and attachments. That needs a
    delete scope, a new grant and its own spec. It is not in this design. The
    comment and attachment ids kept on each record make it possible later.

### 12. The app contract

`docs/contracts/amiigo-support-chat.md`, section "What changes with the Zoho
integration", is updated, and the app team is told:
- In part 3: `ticket_id` stays our `EM-` reference, not the Zoho number. Photos
  and videos are uploaded to the ticket; one over the size limit is noted, not
  attached.
- In part 4: the handover wording.

## The person's steps

Nobody pastes a key, token or code into a Claude session at any step.

**Part 1**

1. **Sachin.** The OMS's client secret is committed
   (`embiz-backend-server.yaml`, lines 83 to 86) and should be rotated. Rotating
   also means updating `EMOTORAD_ZOHO_CLIENT_SECRET` at the same time.
   - If step 2 finds that rotation revokes refresh tokens, rotate before step 4.
   - Otherwise rotation can wait, but it is a precondition of part 5.
2. **Zoho admin.** Answer these:
   - Is the OMS's client server-based or a Self Client, and whose console holds
     it?
   - Who holds the OMS's token?
   - Which Zoho user will grant the chatbot's token?
   - Does regenerating the client secret revoke its refresh tokens?
   - Which Desk edition, and how many API credits a day?

   If it is a Self Client, stop (section 1).
3. **Support lead and Zoho admin.**
   - Create the test department "AI chatbot test": no assignment rules, no SLA,
     no automatic replies or customer notifications, visible to Sagnik and the
     lead only.
   - Check that no ticket webhook to the OMS, contact webhook or CRM contact
     sync fires for it.
   - Add the ticket fields "Chat reference" (text) and "Source" (pick list with
     "AI chatbot") to the test department's layout.
   - Name the real department that chatbot tickets will go to, and add the same
     two fields to its layout.
   - Create two contacts: "AI chatbot test" with the fake number
     +919999999999, and "Unverified AI chat" with no number.
   - Send Claude both department ids, both contact ids and the two field API
     names. None of these is secret.
4. **The granting user, with Sagnik.**
   - Run `consent_url.py` and open the address while signed in to Zoho as that
     user.
   - Choose the EMotorad Desk organisation if Zoho asks, and approve.
   - Copy the code from the address bar. The page itself may show an error.
   - Run `exchange_code.py` within two minutes. The refresh token is shown once.
   - Keep the token somewhere safe until step 8. The scripts in steps 5 and 6
     ask for it.
5. **Sagnik.** Run `probe.py`. Claude reviews the masked shapes and fills in the
   constants.
6. **Sagnik.** Run `test_ticket.py`, then close the test ticket in Desk.

**Part 3**

7. **Sagnik.** Run `python scripts/mongo_setup.py` against staging, and check the
   `tickets` unique index is listed.
8. **Sagnik.** Then, just before the part 3 deploy, add the six always-needed
   settings to `/emotorad/stage/ai/app` by the config-store runbook
   (`docs/runbooks/config-store.md`):
   - write the whole JSON, every existing field included, to a file outside any
     repo;
   - run `put-secret-value`;
   - delete the file;
   - check the names only.
9. **Sagnik.** Deploy the alarm stack and name who receives it. Tell the app team
   about the contract update.

**Part 4**

10. **Support lead.** Confirm in writing:
    - who works chatbot tickets;
    - the priority for safety and for normal faults;
    - the texts in section 7;
    - whether "within 24 hours on working days" (`mocks.py:1000`) is true;
    - the line on how else to reach support.

**Part 5**

11. **Sachin.**
    - Sign off before any real customer conversation reaches Zoho: customer data
      in a third-party system, as with OpenRouter.
    - Choose the erasure option (section 11).
    - Confirm the rotation in step 1 is done.
    - Confirm the OMS webhook rule excludes Source "AI chatbot". The OMS prints
      every webhook payload (`em-biz-backend/zoho/views.py:183`).
    - Confirm the real department's layout has no mandatory field the chatbot
      does not send, and check its notification and automatic-reply rules.
    - Name the environment with real verification that goes live. For it:
      - its own secret with all nine settings;
      - its own refresh token, from a second grant (steps 4 and 8 again): the
        token throttle is per refresh token, and revoking one never stops the
        other;
      - `mongo_setup.py` against its database;
      - the alarm stack on its log group.
    - Watch `test_ticket.py --real-department` make one supervised ticket.
    - Then set `EMOTORAD_ZOHO_LIVE=yes` there and redeploy.

## Testing

Offline, with no network, against a fake Zoho that returns part 1's recorded
shapes. Those include HTTP 200 throttling bodies and 204 empty searches. The
whole suite runs with every `EMOTORAD_ZOHO_*` name blanked (section 9).

**Part 2**
- **Registry.** Each ticket tool gets the facts in section 2's table, none of
  them in the model's schema. A retry with the same key returns the same
  reference. An extra argument is rejected.
- **Receipts.** A key reused in a new run, after `restart_for`, raises a new
  ticket, tested through `registry.call` and `runtime.handle()`.
- **Seam.**
  - One record per `source_key` on both stores (memory, and mongomock tested
    sequentially), also across two registries on one store.
  - A dealer's call goes to the mock.
  - The router sends each id to the system that issued it.
  - `add_note` and `close_runs` work on both systems.
- **Dealers through `runtime.handle()`.** With Desk wired in, a dealer's safety
  report records no Desk ticket, and the model is never called.
- **Run bounds.** Two people on one conversation id (`restart_for`), both ways:
  the second person's ticket carries none of the first person's turns or files,
  and the first person's ticket gets none of the second's.
- **Kinds and urgency.** Each set from the caller. A model `battery_safety`
  ticket is urgent.
- **Intake.** The verified phone; a typed number; `contact_number_required` with
  neither.
- **Warranty proof.** The transcript attaches, the claims are recorded, and
  `proof_url` is ignored.
- **ASSERTED phone.** No bike is looked up, and the ticket is unverified.
- **Erasure.** Records are found and deleted by conversation and by phone.
  `show` lists them. A ticket recorded after `show` makes `delete` refuse.

**Part 3**
- **Token.** Cached and refreshed early. One refresh under concurrent callers.
  HTTP 200 "Access Denied" backs off for 10 minutes. `invalid_client_secret`
  sets the health state. 401 `INVALID_OAUTH` refreshes once, then succeeds.
- **Worker.**
  - Each contact rule: test; unverified; verified with one, several or no
    matches; a stored id from a test or unverified record is never reused; a 404
    on a stored contact.
  - Every field in section 5, and none of the forbidden ones.
  - An unknown create outcome adopts the ticket found in the contact's list, and
    creates only when none is found. An `intent` left by a crash is handled the
    same way.
  - The transcript is posted once, then only new turns, in chunks under 30,000
    characters.
  - Notes.
  - Attachments once each. An oversized one gets a note and is never read.
  - Every row of the answers table.
  - The retry schedule.
  - `safety_ticket_late` at 10 minutes, even with new turns every minute.
    `zoho_ticket_stuck` at 24 hours.
  - The lease, tested sequentially: a second worker finds nothing due.
  - A save that matches nothing drops the record.
  - A wake during a lease is not lost.
  - The credits floor.
  - Held records in the wrong mode.
  - The first call comes only after the turn ends, or two minutes after a turn
    that died.
- **Guards.** Each misconfigured reason falls back to the mock and writes
  nothing to `tickets`. Live is refused with dev codes or the mock OTP sender. An
  `eu-` region is refused.
- **Zoho is never called inside `runtime.handle()`.** This is the counterpart of
  the model-never-called tests.
- **The worker never starts at import.** With every `EMOTORAD_ZOHO_*` set,
  importing and reloading `emotorad_ai.api` starts no thread. Only the lifespan
  starts it.
- **Secrets.** No token or secret appears in any log line, exception message or
  store document.
- **Redaction per language.** English, Hinglish, Devanagari text and Devanagari
  digits, glued numbers, `+34` numbers. Identifiers stay unredacted.
- **Entry points.** Health, `chat_local`, the playground, the CLI and the live
  evaluation, as in section 9.
- **Contract.** A CI test that the fake's answers match the recorded shapes.

**Part 4.** Each test goes through `runtime.handle()` and asserts the model was
never called. The number replies have golden phrases in English, Hinglish and
Hindi (Devanagari).
- **Safety:**
  - with a phone;
  - without one;
  - the hazard and the number in one message;
  - a typed number that owns two bikes (no bike look-up, no failure);
  - a second report in the same run (a note, not a second ticket);
  - with the record not written (no promise of a call).
- **Talk to a human:**
  - with a phone;
  - "call me on 98765 43210" as the first message;
  - without a number, then a number ("mera number 9876543210 hai", Devanagari
    digits);
  - without a number, then no number;
  - with a Desk ticket already in the run;
  - a dealer's request records nothing.
- **The callback gate:**
  - it runs before verify-first, so a number typed while waiting sends no code;
  - an invalid number;
  - a quoted `EM-1000001`;
  - "start over".
- **Lock-out:** with a number in the message; a pending number; the last number.
- **History:** `[phone]` appears in the model's history and the transcript on
  every path.
- **Save conflicts:** a conflict after a recording merges, and does not re-run.
- **Store down:** a safety report gets the safety steps, with no promise.
- **Caps:** both caps, and urgent tickets never capped.
- **Zoho off:** each gate behaves as stated in section 6.
- **Disclosure:** the line is on every new reply.

**Person-run, part 1.** This is Layer 5 of
`docs/Emotorad_Testing_Strategy.md` (the Zoho rows at `:221` and `:340`):
`test_ticket.py`'s look-up finds the first ticket and makes no second, and the
ticket carries the frame number.

## Danger zones touched

Tier-1 review, with a rollback section in each PR:

- Part 2: `tools/registry.py` (`ToolContext`, receipts scoped by run, new
  injected facts).
- Part 3: `observability.py` (redacted keys, `redact_pii`).
- Part 4: `guardrails.py` (texts), `graph.py` (the new gate's position) and the
  safety branch in `runtime.py`.

## Rollout

1. **Part 1.** Scripts merged; person steps 1 to 6; shapes committed.
2. **Part 2.** Merged with Zoho off. Behaviour changes:
   - the intake number rule;
   - `submit_warranty_proof` returns `ticket_id`, so its ticket sets
     `state.ticket_id`, gets the transcript and counts as a ticket this turn;
   - receipts are scoped by run;
   - the mock's transcript covers the run only, and is attached on every turn
     of the run.
3. **Part 3.**
   - Person steps 7 and 8.
   - Deploy to staging.
   - Test on a phone, on the fake number: a battery fault with a photo, a safety
     report while signed in, a warranty proof, and an intake ticket. Each must
     appear in the test department, on the test contact, with this run's
     transcript and photos.
   - Person step 9.
4. **Part 4.** Person step 10, then merge, deploy and test the new paths the same
   way, including five wrong codes.
5. **Part 5.** Person step 11, then live in the named environment.

## Rollback

- **Stop sending.**
  - Run `tickets_report.py` and give the support lead the waiting, stuck and
    held records, because those customers were told someone would be in touch.
  - Remove `EMOTORAD_ZOHO_REFRESH_TOKEN` from the secret and redeploy. New
    tickets go to the mock, and `/health` shows `zoho: not configured`, with the
    records still waiting counted.
- **Revoke access.** The person runs `revoke.py`. This revokes only the
  chatbot's refresh token.
- **Code.** Revert the part's merge, after the report above. If part 2 is
  reverted, also delete the `tickets` collection after that report, because the
  reverted erasure code would no longer erase it.
- **Check afterwards.** `/health`; no new chatbot tickets in Desk. Tell Sachin
  and the support lead.

## Open until part 1

Part 1's exit check needs each of these answered from the person's answers and
the recorded shapes:

- The client's type and owner, and whether rotating the secret revokes tokens
  (section 1, person step 2).
- Whether the contact layout accepts a last name, a mobile number and no email,
  and which ticket fields are mandatory in each department.
- Whether the contact's ticket list shows a new ticket at once. If not, how long
  it lags.
- Zoho's attachment size limit (20 MB is assumed).
- The priority values and the system channel to use.
- The custom field API names.
- The daily credit allowance, for the floor.
- The format of Zoho's ticket number. It appears only in logs and the
  description; the customer sees our reference.
