# Emotorad — high-level design: the agentic AI layer

This describes the proposed technical architecture behind the agentic use cases in `Emotorad_AI_Journey_Map.md`. The goal is one reusable architecture — not a bespoke build per use case — so the WhatsApp lead-capture bot, the dealer sales copilot, and the technician copilot are all the same core system wearing different channel adapters.

## 1. Architecture at a glance

```
Customer channels        WhatsApp · Website · Voice/IVR · Social DM
        │
Channel adapters         normalize every channel to one internal format
        │
   Agent core            LLM orchestrator + guardrails  (+ observability/evals)
        │           ╲
    Tools           Knowledge base (RAG)     ─── Human escalation
 (live lookups)      (documents)                 (dealer staff / call center)
        │                  │
                Data layer
   CRM/CDP · dealer POS · service records · document store
```

## 2. Component responsibilities

### 2.1 Channel layer
WhatsApp Business API, website chat widget, voice/IVR telephony, Instagram/Facebook DMs. Treat this as commodity infrastructure — Gupshup, Exotel, Ozonetel, Yellow.ai, Verloop and similar vendors already handle WhatsApp and voice plumbing for Indian D2C brands. Buying here is almost always the right call.

### 2.2 Channel adapter layer
Converts whatever comes in — a WhatsApp text, a spoken sentence, a website form — into one internal message format (text + metadata: known customer ID, channel, language). For voice, this is also where speech-to-text (inbound) and text-to-speech (outbound) live. This is the layer where voice bots pick up most of their extra cost and latency versus text bots.

### 2.3 Agent core — the orchestrator
A single reasoning loop that: holds conversation state, decides which tool(s) to call based on what the customer is asking for, applies guardrails before it acts or answers, and hands off to a human when a rule requires it. This is the layer to build and own directly, even if everything around it is bought — the prompts, tool definitions, and business rules are what make it behave like Emotorad rather than a generic vendor bot. Roughly 24 of the 57 use cases in the journey map are "agentic" specifically because they run through this core.

### 2.4 Capability layer: tools vs. knowledge (RAG)
Two different ways the agent core gets information, and they should not be built the same way:

- **Tools** — typed function calls to live, structured systems: CRM/CDP lookup, dealer inventory check, calendar/booking, payment/EMI status, order tracking. Fast, deterministic, no model involved in the lookup itself.
- **Knowledge base (RAG)** — retrieval over a document store for anything that lives in unstructured, changing text: service manuals, warranty policy, spec sheets across models, past support tickets, review corpora. This is what the technician copilot and the RAG-based FAQ/policy assistant actually need.

Rule of thumb: if the answer is "look it up in a table," it's a tool call. If it's "look it up in a paragraph," it's RAG. Most over-engineering in agent builds comes from reaching for RAG when a tool call would do.

### 2.5 Data layer
CRM/CDP (single customer view), dealer POS/inventory feed, service management system, and a document/vector store for RAG. This is the most likely real bottleneck in the whole build — the AI layer is usually the easy part; getting dealer and service data into one place, reliably, is the hard part.

### 2.6 Human escalation (cross-cutting, not a stage)
A safety valve off every layer, governed by explicit rules rather than model judgment: refunds above a threshold, warranty approvals, anything safety-related, anything the model is unsure about. This should never be "the model decides to escalate" — it should be a hard-coded rule the model can't override.

### 2.7 Observability & evals (cross-cutting)
Conversation logging, a sampled human review process, an eval suite run before every prompt or tool change ships, and dashboards tracking escalation rate and resolution rate — not just "conversations handled."

## 3. Example end-to-end flow

A customer messages "hi, thinking about an e-cycle" on WhatsApp:

1. WhatsApp → channel adapter normalizes the message.
2. Agent core classifies intent (early-stage consideration), checks the **CRM tool** — no existing record, creates a new lead.
3. Agent core asks a couple of clarifying questions (commute distance, budget) — a few conversational turns.
4. Agent core calls the **inventory tool** to find matching models with stock near the customer's pincode.
5. Customer asks a spec question ("what's the range on a full charge?") — agent core calls the **knowledge base (RAG)** to answer from the actual spec sheet.
6. Customer asks to book a test ride — agent core calls the **calendar/booking tool**, confirms a slot at the nearest dealer.
7. The full conversation and lead summary are written back to the CRM.
8. If at any point the customer raises a refund, warranty, or safety issue, the agent core routes to **human escalation** instead of resolving it itself.

## 4. Build vs. buy summary

| Layer | Build vs. buy | Notes |
|---|---|---|
| Channels (WhatsApp/voice/IVR infra) | Buy | Commodity — Gupshup/Exotel/Ozonetel/Yellow.ai/Verloop-type vendors |
| Channel adapters | Mostly buy | Usually bundled with the channel vendor; light custom glue code |
| Agent core (prompts, tools, rules) | Build | This is the actual IP — don't outsource it to a vendor's generic bot |
| Tools (CRM/inventory/calendar/payment integrations) | Build | Custom per system, but standard API integration work |
| Knowledge base / RAG pipeline | Build | Needs a real document corpus first — don't build this before the documents exist in usable form |
| Data layer (CRM/CDP, POS, service records) | Existing + extend | Emotorad already has "advanced CRM" per public sources — the open question is whether dealer and service data actually feed into it |
| Observability/evals | Build (lightweight) | Logging + a growing eval set; doesn't need to be sophisticated on day one |

## 5. How this maps to the phased rollout

- **Phase 1** needs only: channel layer + agent core + a thin tool layer (CRM lookup, basic FAQ). No RAG required yet.
- **Phase 2** needs deeper tool integrations: EMI/KYC systems, dealer stock visibility, calendar systems across hundreds of dealers.
- **Phase 3** needs the knowledge/RAG layer at real scale (manuals, tickets) plus the data layer clean enough to feed predictive models (churn, maintenance, CLV).

## 6. Open questions to resolve before building

- Does dealer and service-center data already reach the central CRM, or does that pipeline need to be built first?
- Has a buy decision been made for WhatsApp/telephony infrastructure, or is that still open?
- Who owns and signs off on the escalation rules — compliance, ops, or both — before any customer-facing agent goes live?
- What's the realistic monthly conversation/call volume, to size the cost estimate in the journey map?
