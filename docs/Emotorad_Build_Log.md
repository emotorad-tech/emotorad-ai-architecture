# Build log

Moved out of `CLAUDE.md` on 13 September 2026 so the rulebook stays under 200 lines. This is the dated history of what landed and which bugs each build surfaced. It is history, not instructions; the rules distilled from it live in `CLAUDE.md` and `.claude/rules/`.

## The code so far (as of R4, 6 August 2026)

`src/emotorad_ai/` is the skeleton built end-to-end on use case #1 (customer / website chat /
battery support), with every tool mocked. `tests/` runs it offline with no dependencies and no
AWS: `python3 -m unittest discover -s tests -t .`. See `README.md` for the module map, the
guardrails that are enforced in code, and the open items the code encodes.

Built: message contract, deterministic identity resolution, website-chat adapter, tool registry
(identity injection + idempotency + error envelopes), seven mocked tools, stub router, battery
knowledge retrieval, safety and handoff guardrails, the agent loop, JSONL observability, and the
Bedrock client. Not built: real integrations, other channels, other personas, real routing.

**R0 unit 1 landed 2026-08-02: the contract and identity now match the design.** 68 tests.

- `contract.py` — `Identity` carries `cluster_id` (the person), `em_aid` (the browser), `phone`,
  and **`strength`** (`verified` / `asserted` / `anonymous`). Disclosure is gated by
  `Identity.may_disclose` — **in code, so no prompt wording can widen it.** `InboundMessage.subject`
  carries the actor/subject split and is rejected for any persona but `internal`; `message.about`
  is what every downstream read must scope to.
- `identity.py` — `IdentityGraph` is a working reference implementation of `link_identity`: the
  three branches, verified-only merges, older-cluster-survives, and a `cluster_merges` audit trail.
  **Production owns this in Nest**, but merge semantics are the easiest thing here to get subtly
  wrong and a wrong merge is unpickable, so the rules live in `tests/test_identity_graph.py` as
  executable spec the Nest implementation must reproduce.
- Resolvers per channel: website (cookie ± session), WhatsApp (verified natively), voice (asserted
  — resolves a person, authorises nothing), internal (Google SSO).

**Bug this exercise caught, now fixed in code *and* in the spec:** `wa_id` and `phone` were separate
identity types, so the same number arriving from WhatsApp and from a web form produced **two
clusters** — the unique key is `(type, value)`, so they never collided, and nothing raised an error.
A WhatsApp ID *is* a phone number; `canonical_type()` now stores it as one.

**R0 unit 2 landed 2026-08-02: one warranty tool, keyed on phone.** 80 tests.

- `lookup_warranty_record` replaces `get_customer_profile` + `get_warranty_status`. Injected on
  **phone**, returns *every* bike on that number with coverage computed per bike — a list always,
  so the single-bike case is never a special shape. Fixtures now mirror the real 60-key response
  (`docs/api-shapes/warranty.json`), including `""` for absent strings and a `created_at` that must
  never be used for coverage.
- **Four outcomes that must never collapse into each other**, each with its own prompt: coverage
  computed · `purchase_date_missing` (→ ask for the invoice) · `no_warranty_record` (→ offer
  registration, *not* "we can't help you") · `oms_unavailable` (retryable, our fault, say so).
  Conflating the last two either tells a registered customer to re-register or tells an
  unregistered one to come back later forever.
- **Frame-number guardrail in code**: a ticket naming a bike the customer does not own is refused
  (`frame_number_not_owned`), and on a multi-bike number an unspecified frame is refused rather
  than guessed (`frame_number_required`).
- `_clean()` normalises upstream's `""` / `"None"` to null at the adapter boundary — one place,
  per the NOT OURS rule in the edge case register.

**R0 complete 2026-08-04: the whole skeleton is assembled.** 187 tests, no network, no AWS.

New modules, each built and tested before the next: `conversation.py` (phase state + the
`pending_topic` that survives a bike-selection turn) · `triage.py` (deterministic bike matching and
issue classification; the model is reached for only when free text needs it) · `disclosure.py` ·
`enrichment.py` (token-budgeted, disclosure-gated) · `agents/late_warranty.py` ·
`adapters/{whatsapp,voice,amiigo}.py`. `router.py` is deleted — triage replaced it.

**`runtime.handle()` is the order, and the order is the design:** identity → enrichment → safety →
handoff → registration-if-unregistered → triage → sub-agent → **coverage post-check** → disclosure.
Every outbound string passes through `_outbound()`, so the AI disclosure cannot be missed on a
branch someone adds later — including guardrail short-circuits, which are the replies a customer is
most likely to hit first.

**Three bugs this build surfaced, all of the silent kind:**
- `\b` word boundaries **do not work on Devanagari** — the vowel sign ending "तीसरी" is a combining
  mark, excluded from `\w`, so the ordinal never matched. Hindi selection was silently broken while
  English worked. `_contains_token()` now falls back to substring for non-ASCII.
- "the second one" selected **bike 1**, because "one" was in the ordinal table as a cardinal.
  Strong ordinals are now checked before weak ones.
- Channel pill vocabularies (`battery_issue`, `battery_health`, DTMF `1`) never matched the topic
  names, so tapped entry points fell through to "what is happening with the bike?".
  `topic_from_pill()` normalises them.

**R1 knowledge + R2 Bot 2 landed 2026-08-06.** 239 tests.

- **`knowledge/` — authored records, one file per sub-issue** (9 records, battery + motor). Structured
  authoring is what makes chunking free: each file is already a retrieval unit, so no step is ever
  cut in half. `applies_to` is a **hard filter** (a throttle record is unretrievable for a bike
  without one), a `superseded_by` record is deleted from the index rather than down-ranked, and
  malformed records raise at load rather than vanishing silently. Files in the repo, not a CMS —
  Git is the audit trail and PRs are the approval workflow.
- **`tests/test_retrieval_evals.py` — retrieval scored on its own**, with a 27-query golden set and
  accuracy floors. This is separate from the conversation tests on purpose: a wrong passage produces
  a fluent, confident answer that passes conversational review. Currently 100% top-1, **reported per
  language and never averaged**.
- **`agents/motor_support.py` — Bot 2, and the architecture's own test.** It is a prompt, a tool
  slice and a topic; identity, enrichment, triage, safety, the coverage post-check, disclosure and
  idempotency are all inherited unmodified. It deliberately *shares* the battery agent's context
  blocks rather than copying them — the copy that drifts is the one that starts stating coverage it
  should not.
- **`metrics.py` — quality metrics, not volume metrics.** Deflection is reported and explicitly not
  a target; **zero escalation is flagged `suspiciously_low`**, because that is the Klarna shape.
  Repeat contact within 48h is the counter-metric, cost is per *resolved* conversation, and
  everything is broken out per language.
- Safety is now **one gate for the whole conversation** (`check_safety`), covering drive-system loss
  of control and any report of injury. It runs before triage, so it cannot depend on having been
  routed to the right agent first.

**Three bugs this build surfaced, all silent:**
- **The tokeniser was ASCII-only** (`[a-z0-9]+`), so Hindi retrieval returned *nothing*. Worse, the
  obvious fix (`\w+`) splits "बैटरी" into ["ब","टर"] because Devanagari combining marks are not word
  characters. Same root cause as the earlier `\b` bug in triage. Fixed with an explicit
  `[\w\u0900-\u097f]+` class.
- **A single incidental body-word match counted as retrieval evidence** — "where is my order"
  returned a battery-storage passage because that passage contains the word "where". Body text alone
  is now never sufficient; a symptom or title must match.
- **`\b` written through a shell heredoc became literal backspace bytes** (`\x08`), producing a
  safety regex that compiled, read correctly in review, and could never fire. Also exposed that
  `dent` had no word boundary and was matching inside "accident" and "incident".

**R4 dealer persona landed 2026-08-06.** 265 tests.

- **A second persona, not a second sub-agent** — the real test of the message contract. Identity,
  registry, guardrails, disclosure and observability all carried over unchanged; routing is now
  **scoped per persona** (`TOPIC_AGENTS` for customers, `DEALER_AGENTS` for dealers), never one
  router over everything.
- **Persona isolation is enforced at the registry, not the prompt.** `lookup_warranty_record` is
  simply absent from the dealer tool slice, and `hydrate()` gives dealers their own path that never
  touches the customer warranty table. This is a live risk, not a theoretical one: dealers register
  most warranties under their own number, so the customer path would hand a dealer dozens of
  unrelated customers' bikes. Tests assert the tool is not even offered to the model.
- **Money guardrails in code**: `quote_order` (repeatable read) and `place_order` (write) are split,
  so a model cannot commit an order while it thinks it is showing a price. `place_order` **re-prices
  and re-checks credit** rather than trusting the total the model carried across turns — a stale or
  mistyped number fails as `quote_mismatch`. Credit limit, overdue balance, account status and stock
  are all decided in `_price_order()`; the model may propose items and nothing else.
- **A new adapter on a separate WhatsApp line** (`DealerWhatsAppAdapter`). An unknown sender there
  resolves to `unknown` rather than being downgraded to a customer.

**Bug this build surfaced:** the human-handoff guardrail only knew *customer* vocabulary. Dealers
never say "agent" — they say "account manager", "ASM", "area manager", and every one of those was
missed. Added as its own pattern, requiring an explicit request verb so that "my account manager
said I get 5% off" reads as a discount argument (which the money guardrails already refuse) rather
than a transfer request.

**Website chat on a real phone, and the first replacement order, 2026-09-20.** 715 tests.
Read `docs/handoff-2026-09-21-fulfilment.md` before touching anything; it carries the run
command, the never-do list, the open bugs and the owner's open decisions. In brief:

- `/chat` establishes identity itself (`Runtime(self_service_identity=True)`, one-time code
  read off a dev endpoint because no SMS exists), carries the proved phone onto the identity,
  and resolves facts a tool needs at call time via `ToolContext.late`, because the model
  verifies and looks up in the same turn. The chat page fills a phone, opens photos full
  screen, keeps its composer mounted, renders markdown (no links, on purpose), and sends
  the customer's photos to Claude as vision content, stored nowhere.
- Three conversational bugs found only by running it on a handset, all fixed in code: the
  agent sent only the last block the model wrote; the coverage post-check saw one turn's
  tools when the fact was three turns back; and it read "chargeable even within warranty"
  as a denial of cover. Coverage is two questions, and the check knows that now.
- **Replacement fulfilment, first build**: spec `docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md`.
  `place_replacement_order` is the one write. Code decides technician-or-not
  (`knowledge/_replacement/parts.yaml`), item code, in flight (48 h), "sure" (four runtime
  facts, never the model's confidence) and the approval mode (`EMOTORAD_AI_APPROVAL_MODE`:
  `bot` / `reasonable` / `human`). Battery and charger only. Every write is a mock. The spec's
  fourth "sure" fact is not implemented; `is_sure` says so; do not launch on `bot`.
- Owner policy in the records: a melted terminal is a defect, never impact damage, and in
  warranty means a replacement placed from the conversation, free.
- The address backstop is per word plus a required pincode, and a spent one-time code does
  not count, because the live model dropped the pincode to get past the old check and the
  customer types their code into the same conversation.

**Two rules that earned their place on the 20th.** Guardrails in code, not prompted: the model
ignored a "wait a turn" instruction within the hour. And a front-end change is reviewed on the
device it ships to: four days of reading the code missed three defects that two minutes on a
phone found.

**Jev routing, the MongoDB store and the live evaluation merged into feat/integration, 2026-09-29.**
Built on `main` from 2026-09-27 and merged onto `feat/integration`
(`docs/superpowers/plans/2026-09-29-merge-jev-mongo-into-integration.md`). One mode list for
everything (`offline|anthropic|bedrock|openrouter`); the graph runtime carries the web chat's
identity tools, the video-summary safety scan, remembered coverage and orders and the order
post-check; the web chat's cluster and pinned agent are applied inside the turn so a durable
store keeps them; the history window is applied before the turn's positions are taken; the
permanent transcript never keeps an inline photo or a signed link.

Still not built: real integrations behind the mocks, and a vector index — retrieval is still keyword
scoring over the authored records (`_score` is the single seam).

**Zoho Desk tickets, parts 1 to 4, 2026-10-05.** 3,022 tests (one known `test_video` failure). Spec
`docs/superpowers/specs/2026-10-05-zoho-desk-tickets-design.md`, plan
`docs/superpowers/plans/2026-10-05-zoho-desk-tickets.md`, runbook `docs/runbooks/config-store.md` §7.
Zoho is off by default: without `EMOTORAD_ZOHO_REFRESH_TOKEN` every ticket goes to the mock, as
before.

- Part 1, the scripts a person runs (`scripts/zoho/`): the consent address, the code exchange
  (India only), the read-only probe, one test ticket, the tickets report and the revoke. Their
  masked captures replace the drafts in `docs/api-shapes/`.
- Part 2, the ticket record: our own reference (`EM-` and seven digits, from `counters`), one
  `tickets` record per ticket with a unique `source_key`, receipts scoped to the conversation's
  run, and run bounds, so a second person on the same browser takes none of the first person's
  turns or photos.
- Part 3, the worker, in the API's lifespan only: after the reply it sends each record to Desk
  (contact, ticket, transcript comments, files), adopts the ticket an unanswered create made by
  the chat reference at the end of its subject, and logs stuck, late and refused records for the
  alarms in `infra/zoho-alarms.yaml`.
- Part 4, the conversation: the handover and lock-out tickets, the call-back number gate, safety
  with no known number, the safety reply while the store is down, and the caps on unverified
  tickets. Every text is a draft for person step 10. Erasure (spec section 11) is deferred, so
  ticket records are removed by hand.

The bugs the build surfaced, each fixed with a test:

- **The capture format collided with the suite.** The probe and `test_ticket.py` wrote over the
  shapes the fake Zoho answers from: `zoho-token.json` lost its `refresh` key, the subjects were
  masked, and `zoho-attachment.json` became a list. Committing part 1's captures would have
  turned CI red. The token answer now goes under `refresh`, the chatbot's own subject is kept,
  and the first upload's own answer is the attachment shape.
- **"Invalid Redirect Uri" in use.** The person's consent step failed on a redirect address that
  did not match the client's, and no script showed the address it used. Both now trim it, print
  it, and say it must match character for character.
- **The owner's run start after a stranger.** When the run's own person came back after a
  stranger used the same chat, a ticket recorded for them began at the run's start and took the
  stranger's turns. The owner now gets a stretch of their own (`owner_started_at`).
- **A photo on the first message of a new run.** A file is stored before its turn runs, so by
  when it was stored it fell inside the previous run, whose record could still be outstanding,
  and it could go on the previous person's Zoho ticket. A file a turn carried is now judged by
  that turn alone.
- **A lock-out that promised a hand-over with nothing recorded.** With Zoho on, a lock-out with
  no number, or whose ticket failed, still said "I'm passing you to our support team". It now
  says it could not pass this on, with `escalated: false`, and logs `lockout_ticket_not_recorded`.

## How to work in this repo

1. Read this file and `docs/Emotorad_Platform_Build_Plan.md` before writing code.
2. For the open items above: explore the existing website/Amiigo/OMS/ERP code read-only first to answer them, rather than guessing.
3. Build in the order given in the build plan's §5 (Build sequence) — message contract and tool interfaces first, on paper; mock every tool before touching real systems; one contained, reviewable unit at a time (don't scaffold the entire skeleton across all personas/channels in one pass).
4. Do not wire real write-access to OMS/ERP/ticketing until the mocked conversational flow has been reviewed and tested.
