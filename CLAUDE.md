# Emotorad AI architecture — project memory

This repo is the AI/agentic layer for Emotorad (Indian e-cycle/e-bike D2C + dealer network company). Read this file first in any new session before writing code.

## What this project is

An agentic AI platform serving three distinct personas — **customers**, **dealers**, and **internal users** — across multiple channels each, built on a shared skeleton (message contract → persona resolution → channel adapter → tool registry → router → sub-agents), with a deferred no-code front end (internally referred to as "AIRO," after the equivalent internal tool at Cars24) to be extracted later once several sub-agents exist, not built up front.

Full detail lives in `docs/`:
- `docs/Emotorad_AI_Journey_Map.md` — 57 AI use cases across the full customer journey (awareness → aftersales), tagged by type (Agentic / Predictive / Generative / Rule-based) and priority.
- `docs/Emotorad_Unified_AI_Architecture.md` — the target-state architecture: layers, MCP tool servers, voice handoff pattern, phased V0→V4 build maturity.
- `docs/Emotorad_Platform_Build_Plan.md` — **the actionable build plan**, covering the general skeleton plus the first use case end-to-end (customer chatbot, after-sales battery support). Start here for what to build next.
- `docs/Emotorad_Testing_Strategy.md` — the ten test layers, per-release milestones, and the rollout gates. Two properties are load-bearing: the safety branch asserts **the model was never called**, and retrieval is evaluated **separately from conversations** (a plausible wrong answer passes conversational review).
- `docs/Emotorad_Edge_Case_Register.md` — **where edge cases go to wait.** Every negative case, broken upstream field and data-quality defect found so far, each with a disposition: BLOCKS / SAFE-FAIL / CAPTURE / NOT OURS. **The default is CAPTURE, and the working rule is capture-don't-solve.** These cases are individually all defensible to fix, and fixing them in sequence is how this project spends six months improving EMotorad's existing data quality instead of shipping a bot. Build the happy path first — shadow-mode frequency counts are what promote a case out of CAPTURE. Anything marked NOT OURS is an upstream defect to report, never to work around inside the agent.
- `docs/dealer-whatsapp-flows/` — real, detailed spec for the dealer persona: 12 use cases (W1–W12: service tickets, order placement, ledger/collections, stock, dispatch tracking, warranty, EMI, lead routing, pricing, schemes, reverse pickup, marketing support) plus flow diagrams for 5 of them. This is the dealer persona's equivalent of the battery use case — build it once the skeleton is proven on customer/battery.

## Architecture decisions already made (don't re-litigate without reason)

- **Three personas, not one flow**: customer (webchat, WhatsApp, Amiigo app, call), dealer (dealer app / Em Biz, WhatsApp, call), internal (web portal). Each persona gets its own channel adapters and sub-agents, sharing one message contract and tool registry.
- **Persona/identity resolution is deterministic code, never an LLM guess**, and it runs in **two layers**: an identity graph answers *who is this person* (the `identities` table keyed on phone / cookie / WhatsApp ID — see `docs/Website_Anonymous_Identity_Approach.md`), and the OMS warranty API answers *what do they own* (phone → frame number + `purchase_date`; coverage dates are derived, see below). Browsing history may personalise; only the warranty record may authorise a claim about a bike or its coverage. **Customer identity is the phone number** — Amiigo authenticates on it and WhatsApp supplies it natively, so both channels share one resolver. A missing warranty record does not mean "not a customer" (registration is often skipped) — it routes to Late Warranty Registration. Dealers resolve on phone + dealer ID — and must **not** share the customer lookup, since dealers register most warranties under their own number. Internal resolves on Google Workspace SSO, and forces a contract decision: the employee asking is the *actor*, the customer discussed is the *subject*, so `identity` and `subject` are separate fields (identical for customer and dealer). Collapsing them loses either the audit trail or the data scoping.
- **Metadata over inference**: if the entry point already tells you intent (a pill tapped, a template replied to), skip LLM classification entirely. Only free text needs a routing decision.
- **Identity sets the option set; intent picks from it.** A frame number does not mean the customer wants to discuss their bike — they may want a new one, or an order update. So the router is a **real conversational triage agent from day one**, not a stub: it greets with enriched context, disambiguates which bike when several are owned, captures the issue in free text, classifies it (in Hindi/Marathi/Tamil/Hinglish — use Claude, not embeddings, until there's labelled data), and hands off. Routing happens mid-conversation, and sub-agents can hand back. This needs conversation state the current code lacks.
- **A context enrichment engine sits between identity and the agent** — assembling the profile row, pre-authentication events via `em_aid`, past conversations across channels, and owned bikes where a verified phone exists. Summarised, not dumped.
- **Cookie identity personalises; verified identity discloses.** A cookie identifies a browser, not a person (shared laptops), so it may reference product interest but never name, purchases, frame numbers or warranty status. Those need a verified phone.
- **Guardrails are enforced in code, not prompted.** Deterministic tool calls for anything financial or safety-critical (order creation, warranty/credit status, the battery-safety branch) — the model requests, code enforces. Idempotency keys on every write tool. Full table in `docs/Emotorad_HLD_Current.md` §6.
- **The coverage post-check is the highest-value control in the system.** Calling the warranty tool guarantees the tool ran; it does *not* guarantee the reply matches what it returned. A deterministic check must block any reply asserting coverage that contradicts the turn's tool result. This one control closes the *Air Canada* liability precedent, the "tell me my battery is covered" injection attack, and the EU statutory-warranty trap.
- **The model may choose from a set, never invent a member of it.** `customer_id` is injected by the registry and absent from the model's schema. `frame_number` is model-supplied (a customer may own several bikes) and therefore must be validated against the set the warranty API returned for that cluster.
- **The bot always says it is a bot.** Required in the EU from 2 Aug 2026 (AI Act Art. 50, up to €15M / 3% turnover). Asserted in the golden set so a prompt edit can't silently remove it.
- **Serving the EU changes three things** (see `docs/Emotorad_Risk_Register.md` §16–20): the deployment splits by region (no EU–India adequacy decision, so EU data stays in `eu-central-1` and a person active in both regions is two clusters); the `em_aid` cookie is consent-gated so EU visitors are anonymous until they opt in; and the warranty tool is region-aware because EU consumers hold a statutory 2-year guarantee independent of our commercial warranty. Treat GDPR as the baseline and DPDP as the subset.
- **Volume metrics are not quality metrics.** Klarna hit their deflection targets and were rehiring humans eighteen months later. Track CSAT on bot conversations separately, repeat-contact within 48h, escalation rate as a *health* signal, and cost per *resolved* conversation — from the first day of canary, not after launch.
- **AIRO (the business-user agent-builder front end) is explicitly deferred.** Build the tool registry, router, and 2–4 sub-agents by hand first; extract a UI over them only once hand-building a new sub-agent feels repetitive.
- **The knowledge base is a product with an SME front end, and is NOT deferred with AIRO.** AIRO builds agents; this maintains content — validated photos and videos per issue/sub-issue/angle, diagnostic PDFs, SOPs, standards — and that content decays from the week R1 ships. **The content model must be right from day one; the editing tool can wait.** R1 authors it as structured files in the repo (Git = audit trail, PRs = approval) since there's only one author; revisit an editor when the second and third non-technical SME arrive. No free option exists at Emotorad's size — Directus is BSL and free only under $5M revenue, Strapi gates RBAC/SSO/audit behind paid tiers, and a custom UI is ~4–8 weeks the AI engineer isn't doing AI. Build the publish → chunk → embed → upsert worker either way; it's identical regardless of editor. Two properties are load-bearing: **authoring at sub-issue granularity means each record is already a retrieval chunk** (which removes the biggest cause of RAG failure), and **superseded records are deleted from the index, not down-ranked** (semantic similarity has no relationship to recency). See build plan §3.5.1.
- **Mock tools first, real integrations second**, for every sub-agent — validate the full conversational flow against fake data before wiring real OMS/ERP/ticketing systems.
- **First use case: customer chatbot for after-sales battery support** (not dealer ops, despite the dealer spec being more detailed) — see the build plan for why and the full design (conversation flow, required tools, hard-coded safety branch for swelling/smoke/damage). **All four channels are in scope** — WhatsApp, Amiigo, website chat and IVR — sharing one resolver and one message contract. They differ only in the identifier each supplies: `wa_id` and Amiigo login are verified phone numbers, caller ID is asserted by the telco (identify and personalise on it, but require a second factor before anything financial or warranty-related), and website chat supplies the `em_aid` cookie resolved to a cluster. IVR additionally needs a bought-in speech layer (STT/TTS plus a call correlation ID) in front of its adapter.
- **Late Warranty Registration is a first-class sub-agent**, needed early because warranty registration is frequently never completed. A null frame number makes it *available* (and makes bike-specific routes unavailable), but the customer still has to express that intent — they might equally want to browse new cycles. Realistic near-term set: Product Support (all components, not one agent per component), Late Warranty Registration, Order Status, Pre-sales, General Help.
- **Dealer order placement (W2) is a confirmed release, not a maybe** — it lands after the customer channels, and it is the first time a second persona is served, which is what the three-persona architecture exists for. W5 (dispatch tracking) follows as a small read-only companion. Its guardrails are stricter than anything before it because it creates a financial obligation: order creation is deterministic and idempotent, the model never sets price/discount/credit terms, the dealer explicitly confirms before commit, and credit checks are code.
- **Release order** (see build plan §5): R0 core skeleton → R1 Product Support/battery on WhatsApp → R2 motor content → R3 website chat → R4 dealer order placement → R5 IVR → R6 EU deployment. Channels come before the dealer persona because they reuse a proven agent.

## Tech stack

- **Language**: Python.
- **Model access**: Claude via AWS Bedrock (keeps LLM traffic inside Emotorad's existing AWS boundary).
- **Deployment**: own containerized service (ECS Fargate default), same AWS account/region as the rest of Emotorad's platform for low-latency data access, but its own VPC subnet/security group — isolated blast radius from the main site/checkout.
- **RAG**: pgvector (or similar) for the battery-support knowledge base; this is the one genuinely RAG piece in use case #1 — everything else is tool calls to structured data.

## Confirmed (was open; answered 2026-07-28 — build plan §2 has the detail)

- **Identity**: customer = phone number (Amiigo auth + WhatsApp native). Dealer = phone + dealer ID. Internal = Google Workspace SSO. Email is unreliable — never depend on it.
- **Ownership + warranty**: the OMS warranty table (Postgres), via an API — phone in, frame number + bike model + `purchase_date` out. Frame number, not a customer ID, identifies the bike. **The API returns no coverage dates** (verified 2026-08-01 against the real 60-field response, `docs/api-shapes/warranty.json`), so *we* compute them: `warranty_start = purchase_date`, `warranty_end = purchase_date + 24 months`. That term is **provisional** — no system owns real per-product terms yet — and lives in exactly one place, `fixtures.warranty_term_months()`, so the real source replaces it without a code hunt. Never compute from `created_at` (that is when the customer *registered*, not bought). A null `purchase_date` means coverage is undeterminable — but **not a dead end**: the tool returns `remedy: "collect_purchase_proof"` and the agent asks the customer for their invoice or proof of purchase showing the date, routing into Late Warranty Registration (build plan §4.1) with a *different* opening line, since the bike *is* registered and only the date is blank. A date read off an uploaded invoice is a claim, not a fact — a human verifies it before it is written or quoted back as coverage.
- **Ticketing**: Zoho, for customer *and* dealer tickets.
- **Battery diagnostics**: no telematics exists. What exists is documented troubleshooting workflows as PNGs and video (on a junior PM's Mac) — content, not a data feed. So `get_battery_diagnostics` does not exist; do not stub it.

## Open items — confirm before/while building (see build plan §8 for full context)

- The warranty API's exact contract, auth and rate limits — and critically, it must distinguish "no record" (→ Late Warranty Registration) from "call failed" (→ outage).
- Whether one phone can return multiple frame numbers (household, repeat buyer). If so, the agent needs a disambiguation step.
- What share of customers are unregistered — this sizes Late Warranty Registration and may reorder priorities.
- Where the battery workflow media is hosted, and how it gets off the PM's Mac (S3 + CloudFront proposed; WhatsApp size limits may need the videos re-encoded).
- Website chat's identity path, whenever that surface returns to scope.

## The code so far

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

Still not built: real integrations behind the mocks, and a vector index — retrieval is still keyword
scoring over the authored records (`_score` is the single seam).

**Bot catalogue (2026-09):** sub-agents are entries in `bots.py`'s catalogue —
the four Python agents plus `bots/*.yaml`. Runtime, triage keywords, the
`search_knowledge` topic enum and the "I can help with …" reply derive from it.
The playground's Chat mode runs the real `Runtime` (offline planner or the
Anthropic API via `AnthropicClaude`; production stays on Bedrock) and its New
bot mode writes drafts under `EMOTORAD_AI_BOT_DRAFTS`. Design:
`docs/superpowers/specs/2026-09-05-custom-bot-builder-design.md`.

## How to work in this repo

1. Read this file and `docs/Emotorad_Platform_Build_Plan.md` before writing code.
2. For the open items above: explore the existing website/Amiigo/OMS/ERP code read-only first to answer them, rather than guessing.
3. Build in the order given in the build plan's §5 (Build sequence) — message contract and tool interfaces first, on paper; mock every tool before touching real systems; one contained, reviewable unit at a time (don't scaffold the entire skeleton across all personas/channels in one pass).
4. Do not wire real write-access to OMS/ERP/ticketing until the mocked conversational flow has been reviewed and tested.
