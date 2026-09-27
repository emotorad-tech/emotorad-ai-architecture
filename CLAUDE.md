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

## The code today (13 September 2026)

`src/emotorad_ai/` is the skeleton built end to end on the battery use case with every tool mocked, then extended with motor support, late warranty registration, the dealer persona and a bot catalogue. 29 test files, 372 tests, no network, no AWS: `python3 -m unittest discover -s tests -t .`. Retrieval is keyword scoring over the authored records in `knowledge/` (`_score` is the single seam; there is no vector index and no pgvector dependency yet). The dated history of each build and the bugs it surfaced is in `docs/Emotorad_Build_Log.md`.

`runtime.handle()` is the order, and the order is the design: identity, enrichment, safety, handoff, registration if unregistered, triage, sub-agent, coverage post-check, disclosure. Every outbound string passes through `_outbound()`.

## Rules distilled from the build log

- **Never use `\b` or `\w` alone on text that may be Indic.** Devanagari vowel signs are combining marks outside `\w`, so word boundaries and `\w+` tokenisers silently miss Hindi. Use the explicit class `[\wऀ-ॿ]+` and substring fallback for non-ASCII. This bug recurred three times.
- **Never write a regex through a shell heredoc.** `\b` became a literal backspace once and produced a safety regex that compiled and never fired. Edit the file directly and add a test that the pattern matches a known phrase.
- **Four warranty outcomes never collapse:** coverage computed, `purchase_date_missing`, `no_warranty_record`, `oms_unavailable`. Each has its own reply.
- **Persona isolation lives in the registry.** A tool absent from a persona's slice cannot be called; tests assert it is not even offered to the model.
- **Money guardrails are code:** `quote_order` and `place_order` are separate, and `place_order` re-prices and re-checks credit rather than trusting a total carried across turns.
- **Retrieval is evaluated on its own** (`tests/test_retrieval_evals.py`), reported per language, never averaged.

## Commands

- Tests: `python3 -m unittest discover -s tests -t .` (this is also the deploy gate in `.github/workflows/deploy-staging.yml`; it runs only on manual deploy, not on push).
- CLI offline: `PYTHONPATH=src python3 -m emotorad_ai.cli --offline "my battery won't charge"`. Against Bedrock: drop `--offline`, set `--session`.
- Playground: `docker/start.sh` (Streamlit on 8501 behind `/playground`, API on 8000). `EMOTORAD_AI_MODE=offline` is the default and what staging runs; nothing reaches Bedrock unless the mode is flipped.
- There is no linter or formatter configured. `python3 -m py_compile` runs on every edit through the hook in `.claude/settings.json`; adopting ruff is a small separate PR.

## Git

- Working branch `main`; feature branches from `main`, PRs to `main`. Deploy to staging is the manual workflow. Never push or open a PR without the person's yes.

## Danger zones (Tier-1: human review before merge, tests that assert the model was never called)

- `guardrails.py`: `_SAFETY_TERMS`, `_MOTOR_SAFETY_TERMS`, `check_safety`, `check_coverage_claim`. The safety branch runs before triage and before any model call; the coverage post-check is the highest-value control in the system and its claim regexes are English-only today (a Hindi coverage assertion likely passes unchecked).
- `tools/registry.py`: injects identity, enforces idempotency, checks required args, but does not validate model-supplied argument types or reject extra keys; extra keys reach the tool as a `TypeError` swallowed into `tool_exception`. Do not rely on the schema the model sees as enforcement.
- `observability.py`: `redact_pii` runs on conversation logs, not on tool arguments or results, where frame numbers and addresses live.
- `agents/blocks.py`: retrieved knowledge and enrichment go into the system prompt unsanitised; the coverage post-check is the only downstream backstop against injected text.
- `llm.py`: the only Bedrock and Anthropic clients. Keys come from the environment; the `.gitignore` already blocks `*secret*`, `*credential*`, `*.csv`, `.env*` on purpose.

## Definition of done

- New guardrail or safety change ships with a test that asserts the model was never called on the blocked path, and a golden phrase per supported language.
- New tool ships with a registry test for identity injection, idempotency and a rejected extra argument.
- `python3 -m unittest discover -s tests -t .` green with the count in the PR. Retrieval changes show the per-language eval numbers.
- Tier-1 change carries the rollback section from `.github/PULL_REQUEST_TEMPLATE.md`.

