# Emotorad unified AI architecture

This supersedes the architecture section of the earlier HLD. It consolidates the journey map (57 use cases) with everything absorbed from Cars24's real reference material — a single-agent dealer bot, a multi-agent LangGraph marketing pipeline, a master system design, a real Gupshup+ElevenLabs voice handoff, a 13-company competitive benchmark, and a unified router-to-MCP architecture staged across four build-maturity versions.

## 1. The flow, end to end

```
WhatsApp · Website · Voice/IVR · Social DM
        │
Channel adapter          normalizes every channel to one internal format
        │
Middleware / decision layer
   session check → parallel fetch (history, CRM profile, funnel stage) → decision node
        │                                              │
   AI router agent                              nudge trigger / analytics trigger
        │
   ┌────┴─────┬──────────┬─────────┬──────────┐
Pre-sales   Purchase   Onboarding  Service   Retention
& discovery & financing & delivery & support & loyalty      ← domain agents
        │
   each owns sub-agents, calling MCP tool servers
        │
Human escalation ← reachable from any point above, not just after AI fails
```

Supporting services sit beside this flow, not inside it: a session/CDP store, a decision engine, an analytics & nudge rules engine, and a lead/campaign manager. Cars24's diagram named these Session Management, AutoLMS, Clio, and AIRO Campaign Manager respectively — different names, same job.

## 2. Layer by layer

### 2.1 Connector layer
WhatsApp Business API (primary), website chat widget, voice/IVR (inbound customer-care line), Instagram/Facebook DM. Marketplaces (Amazon, Flipkart) stay listing-only — no bot integration there.

### 2.2 Transformation layer
Converts whatever comes in — a WhatsApp text, a spoken sentence, a form submit — into one internal message shape: text, channel, known customer ID if any, language, attachments. For voice this is also where speech-to-text (inbound) and text-to-speech (outbound) live.

### 2.3 Middleware / decision layer
1. **Session check** — existing session (by phone number/customer ID) or start a new one.
2. **Parallel fetch** — three things at once, not sequentially: prior session context, CRM/CDP profile (purchase history, dealer assigned, funnel stage), and the decision engine's read on this customer.
3. **Decision node** — human vs AI, which domain agent, which campaign/priority tag. This is the one step in Cars24's diagram that stayed a dedicated, separate service (AutoLMS) rather than folding into the conversational agent — worth keeping separate for Emotorad too, since it's business logic that changes independently of any single conversation flow.
4. **Dispatch** — to a nudge-sending trigger, the request going forward to the router, and an analytics event, in parallel.

This upfront human-vs-AI decision matters for cost, not just safety: a message that's clearly going to a human (a legal complaint, a flagged VIP account) shouldn't spend any LLM tokens at all before being routed there.

### 2.4 AI router → domain agents → sub-agents → MCP tools

The router's only job is picking a domain agent. When the entry point is a chip tap (Find my e-bike, What is an e-cycle, Show me options), the tap *is* the routing decision — no classification needed. Routing is a real problem only for free text: a DM, an inbound call, or a message that drifts off a chip's original topic.

Five domain agents, each owning a cluster of sub-agents pulled from the journey map's 57 use cases:

| Domain agent | Journey stage | Sub-agents (conversational) | Fed by (predictive/generative utilities, not standalone agents) |
|---|---|---|---|
| **Pre-sales & discovery** | Awareness, Consideration | WhatsApp/IG/website first-response + lead capture, voice bot for inbound queries, social comment auto-response, model configurator, RAG-based FAQ/policy assistant, review summarizer, outbound voice bot for lead follow-up | Lookalike audience modeling, dynamic creative optimization, lead scoring, personalized merchandising |
| **Purchase & financing** | Purchase | Abandoned-cart recovery, checkout upsell, EMI/BNPL eligibility assistant, voice bot for EMI verbal verification, KYC/document verification, dealer sales copilot (staff-facing) | Fraud/credit risk scoring, next-best-offer, order-to-dealer inventory allocation |
| **Onboarding & delivery** | Onboarding | Delivery/setup WhatsApp agent, outbound delivery-confirmation voice bot, automated warranty/serial registration | Delivery ETA optimization, auto-generated welcome content, vision-based PDI |
| **Service & support** | Usage, Service | Tier-1 support bot/voice bot, outbound voice bot for service scheduling, voice bot for complaint/warranty intake, technician copilot (RAG, staff-facing), vision-based warranty triage | Predictive maintenance outreach, spare-parts forecasting, technician capacity scheduling, sentiment/escalation flagging |
| **Retention & loyalty** | Retention | Outbound win-back voice bot, upsell/cross-sell agent, outbound review-collection voice bot, community/event-matching agent | Churn prediction, CLV modeling, loyalty-tier personalization, VoC synthesis, referral fraud detection |

The pattern in that right column matters: predictive-ML and pure-generative use cases aren't separate sub-agents — they're triggers that feed the decision engine, or capabilities a sub-agent calls mid-conversation. Only genuinely multi-turn, tool-using conversations become their own sub-agent, consistent with what we worked out earlier (rule-based lookups like dealer-match stay tool calls, not agents).

**MCP tool servers** (standardizing tool access the way Cars24's diagram showed, rather than bespoke integration per agent):
- `MCP-CRM` — customer profile, funnel stage, session read/write
- `MCP-Inventory` — dealer stock by model and pincode
- `MCP-Calendar` — test-ride and service slot booking
- `MCP-Payments` — EMI eligibility, KYC/document checks, payment status
- `MCP-Knowledge` — RAG over manuals, warranty policy, spec sheets, past tickets
- `MCP-Telephony` — outbound/inbound call initiation, speech-to-text/text-to-speech, live call tracking
- `MCP-Logistics` — delivery ETA, route/dispatch status
- `MCP-Ticketing` — human escalation, warranty claims, CRM case creation

### 2.5 Voice handoff pattern
Adapted from the real Gupshup+ElevenLabs workflow: a chat-to-call handoff needs a correlation ID (their `CallUID`) threading together the trigger, the voice session, and the eventual completion webhook — that ID is what lets three separate systems (WhatsApp provider, Emotorad backend, voice AI provider) stay in sync across an async handoff. Before connecting the call, decide whether the voice agent gets the full chat history or a summarized version — a real cost/completeness trade-off, not a detail to skip. If the call fails to connect, that's not a dead end — it creates a callback lead. After the call, fetch the transcript via the correlation ID, store it, update the CRM. Live call activity (who's speaking) gets tracked in real time, not just reviewed after the fact.

This same pattern serves both directions: inbound (a customer on WhatsApp asks to talk to someone) and outbound (the service-scheduling, win-back, and review-collection voice bots from the journey map, which are our system initiating the call).

### 2.6 Human escalation
Reachable from three points, not one: the middleware's upfront decision node (before any AI spend), a hard-coded guardrail inside any sub-agent (refund, warranty, safety), or a mid-conversation handoff the AI itself triggers. Destination is a dealer/agent console, fed by the same session and context data the AI had, so a human isn't starting cold.

## 3. Eval targets, benchmarked against real competitors

From the 13-company research (Spinny, District, Zomato, Policybazaar, and others):

| Metric | Realistic Emotorad target | Benchmark source |
|---|---|---|
| Tool-call accuracy | 90%+ | Policybazaar — closest comparable (considered purchase + financing + service) |
| Tool-call latency | Under 2–3s | Policybazaar (max 2s), Zomato (~1s simple), District (~2–3s) |
| Query-resolution rate | 70–85% at launch, targeting 80%+ | Between Policybazaar (60–85%) and Zomato at scale (80–90%) |
| Context window | 15–30 min sticky session | Policybazaar |
| Cross-channel continuity | Deliberate differentiator | Almost nobody in the research has this working — Zomato explicitly breaks on platform change |
| Voice | Real opportunity, not catch-up | Every competitor's voice capability is IVR-only or "present but basic" — nobody has genuine conversational voice yet |
| Multilingual | Hindi + 1–2 regional languages, set expectations honestly | Every competitor shows the same weakness: good intent recognition, but templated/English-only responses |

Track these on the same two axes Cars24 used: **model health** (tool-call accuracy, latency, cost/query, hallucination rate) separately from **business outcomes** (resolution rate, contact-to-human ratio, CSAT, cost saved) — they don't move together, and conflating them hides which one actually needs fixing.

## 4. Phased build plan (maturity-staged, not a fixed date plan)

**V0 — single agent, one channel.** WhatsApp only. No router, no domain-agent split yet — one bounded agent covering the Phase 1 quick wins (lead-capture/FAQ, review summarizer, abandoned-cart recovery, basic order-status bot), the same way Cars24's dealer bot started as a single agent before the router+sub-agent redesign. No MCP layer yet — direct tool integration is fine at this scale.

**V1 — website added, decision layer begins.** Add the website channel. Build the parallel context fetch (CRM profile) and a basic human-vs-AI decision node. Split into an AI router plus the two highest-volume domain agents: Pre-sales & Discovery, Purchase & Financing. Start `MCP-CRM`, `MCP-Inventory`, `MCP-Calendar`.

**V2 — voice and knowledge layer.** Add inbound voice/IVR. Add the Onboarding & Delivery and Service & Support domain agents. Bring up `MCP-Payments` and `MCP-Knowledge` (this is where RAG genuinely starts — technician copilot, policy/FAQ assistant). Build the full decision engine (funnel stage + campaign routing, not just human-vs-AI). Add the inbound half of the voice handoff pattern.

**V3 — outbound voice, retention agent, full decision engine.** Add social DM and outbound voice triggers. Add the last domain agent, Retention & Loyalty. Bring up `MCP-Telephony` for outbound calls (win-back, review collection, delivery confirmation). Stand up the analytics/nudge rules engine and the lead/campaign manager. Predictive models (churn, CLV, predictive maintenance) go live and start feeding the decision engine.

**V4 — ideal state.** All channels live, all 57 use cases have a home (agent, tool, or predictive trigger). Cross-channel context continuity works. Full eval loop running continuously: golden-set regression before every prompt change, shadow-mode plus canary rollout for every new sub-agent, every production failure permanently added back to the golden set.

## 5. Open decisions — need your input, not mine

- **WhatsApp/telephony vendor** — Gupshup, Exotel, Ozonetel, Yellow.ai, Verloop, or other. This is commodity infrastructure per our earlier discussion, but still a real procurement decision.
- **Voice AI vendor** — third-party (ElevenLabs-style) vs in-house voice service vs both in parallel. Cars24 ran both — worth finding out from them *why*, since that reasoning (cost split, quality split, redundancy) should inform whether Emotorad needs both from day one or can start with one.
- **MCP as the tool standard** — recommended, given it's what Cars24 converged on and it's becoming a real industry standard, but it's a real build decision, not a given.
- **Who owns the decision engine** — new engineering work, or does it piggyback on logic that already exists in Emotorad's CRM?
- **Data readiness** — the same open question from the original HLD, now more load-bearing: does dealer and service-center data already reach one central system, or is that the actual first project?

---

*Builds on `Emotorad_AI_Journey_Map.md` and `Emotorad_Agentic_AI_HLD.md`. Reference material absorbed: Cars24 `agent-service` codebase (LangGraph), 9 SVGs on agent design and evaluation, a 13-company competitive benchmark, two Gupshup+ElevenLabs voice-handoff workflows, and a 4-stage unified router-to-MCP architecture (V0–V4 ideal state).*
