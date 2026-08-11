# Emotorad AI platform — build plan

**Goal of this document:** the build plan for the Emotorad agentic AI platform — the shared
architecture skeleton, and the releases that ship on top of it.

**Scope, as of this revision:**

| | |
|---|---|
| **Personas** | Customer (R1–R3), dealer (R4). Internal deferred |
| **Sub-agents** | Product Support (battery, then motor), Late Warranty Registration, dealer order placement, plus triage |
| **Channels** | WhatsApp, Amiigo, website chat, IVR |
| **Regions** | India first; EU as a separate deployment (R6) |

Battery support is still the *first* thing shipped, and §1 explains why — but it is no longer the
whole plan. Release sequencing is in §5.

AIRO's business-facing front end remains deliberately deferred; this plan builds the backend it
would eventually sit on.

**Read alongside:**
- `Emotorad_HLD_Current.md` — the architecture this plan builds
- `Emotorad_Risk_Register.md` — **20 documented failure modes** from Klarna, Air Canada, OWASP,
  RAG and EU compliance research. Several controls in R0 and R1 exist *because* of it; read it
  before estimating anything
- `Emotorad_Testing_Strategy.md` — how each release is verified, layer by layer, and the gates
  that block a rollout
- `Website_Anonymous_Identity_Approach.md` — Track W, the identity work owned by the web team

This assumes Kush executing the AI track with Claude Code as the accelerant, and a separate web
developer on Track W. Read/write access to ERP, OMS, Em Biz, Amiigo and the website is assumed to
arrive incrementally as each release needs it, not on day one.

---

## 1. Why battery after-sales support, as use case #1

From the original journey map: "Tier-1 support bot" was already tagged **High priority**, and battery is very likely the single highest-volume, highest-cost driver of that support load — it's the most complex, most expensive, most failure-prone component on the bike, and the one customers are least equipped to self-diagnose. It's also a clean first case for a different reason: it's customer-facing (proves the "customer" persona branch, distinct from all the dealer work already spec'd in W1–W12), and it's bounded — a battery complaint decomposes into a fairly enumerable set of branches (won't charge, degraded range, won't power on, physical damage/swelling, warranty claim, replacement request), not an open-ended domain. Bounded scope is what made the dealer W2 (order placement) spec tractable too; the same discipline applies here.

One branch — **swelling, smoke, unusual heat, or any physical battery damage** — is a safety issue, not a support issue. That branch gets called out separately below because it changes how the whole agent must be guarded.

---

## 2. What we know now (confirmed)

The five things this plan needed answered are answered. They are stated here as fact, because
the design below depends on them:

- **Customer identity is a phone number.** Amiigo authenticates the customer on phone number, and
  WhatsApp carries the phone number natively — so the same check serves both, and there is no
  separate customer ID to resolve. Email may or may not be present, so nothing may depend on it.
  Dealers are identified by phone number plus dealer ID; internal employees by SSO login.
- **Purchase/ownership lives in the OMS warranty table** (Postgres), reachable over an API — pass
  a phone number, get the frame number back. The exact cURL is pending from Kush. The frame
  number, not a customer ID, is the ownership key that identifies the specific bike.
- **Warranty status is NOT returned by the API** — corrected 2026-08-01 against the real response
  (`docs/api-shapes/warranty.json`). There is no `warranty_start`, `warranty_end`, `expiry` or any
  equivalent field in the 60-key payload. What exists is `purchase_date` (populated in real records,
  null in the dummy test record), plus `created_at` as the registration timestamp.

  **So coverage is computed by us.** The rule, decided 2026-08-01 and explicitly provisional:

  ```
  warranty_start = purchase_date
  warranty_end   = purchase_date + 24 months
  ```

  Three things this requires:

  - **One replacement seam.** The term lives in a single named constant with all components mapped
    to 24 months, not scattered through the tool. When real per-product terms arrive — from an API
    or a table — it becomes a data change, not a code hunt. Mark it clearly as provisional so nobody
    six months from now mistakes it for a validated business rule.
  - **Never compute from `created_at`.** That is when the customer *registered*, not when they
    bought. A bike purchased in January and registered in June would receive five months of free
    coverage; a late registrant would be short-changed. `purchase_date` is the only defensible basis.
  - **A null `purchase_date` means coverage is undeterminable.** The agent says so and escalates —
    it does not fall back to the registration date, and it does not guess.

  One useful coincidence: 24 months matches the EU statutory guarantee period, which narrows (but
  does not remove) the divergence risk in §6.1 of the HLD — EU statutory runs from *delivery* and
  exists independently of the commercial warranty.
- **Zoho handles both customer and dealer tickets.** No fork — customer-side support tickets go
  to the same system as the dealer flows in W1.
- **There is no battery telematics.** What exists is a documented set of battery troubleshooting
  workflows — PNG diagrams plus videos, currently on the junior product manager's Mac. That is
  content the agent can retrieve and show a customer; it is not a live health feed from the bike.

**The consequence that shapes everything below:** warranty registration is not completed in every
case, so a phone number that returns no warranty record does **not** mean the person is not a
customer. They may well own a bike whose registration was never done — marketplace buyers on
Amazon and Flipkart are the likely bulk of that population. That case gets its own path (§3.2,
§4.1) rather than being treated as an unknown visitor.

---

## 3. The architecture skeleton — built once, reused by every release

The skeleton below is shared by every release in §5. It is built in R0 against mocked tools, then
each subsequent release plugs into it without reworking it — that is the whole point of building it
first.

Runtime order: **channel adapter → identity resolution → context enrichment → guardrails → triage →
sub-agent → tools**, with observability across all of it.

One caveat on numbering: §3.2.1 (context enrichment) was added after the rest and is numbered to
avoid renumbering sections that code comments and other documents already reference.

### 3.1 Internal message contract
Define this first, before writing any adapter code:

```
{
  "conversation_id": "uuid",
  "persona": "customer" | "dealer" | "internal" | "unknown",

  "identity": {                     // WHO IS TALKING — resolved by §3.2, never by the model
    "cluster_id": "uuid",           // the person; what everything downstream keys on
    "strength": "verified" | "asserted" | "anonymous",
    "phone": "+91..." | null,       // WhatsApp wa_id, Amiigo login, or caller ID
    "em_aid": "uuid" | null,        // website cookie
    "dealer_id": "..." | null,      // dealer persona only
    "employee_email": "..." | null, // internal persona only — the SSO subject.
                                    // The email, not an opaque id: it is what Google
                                    // Workspace returns and what an audit log needs
                                    // to be readable by a human
    "channel_user_id": "..."        // channel-native id, whatever the channel calls it
  },

  "subject": {                      // WHOSE RECORD the conversation is about
    "cluster_id": "uuid" | null     // == identity.cluster_id for customer and dealer;
  },                                // differs for internal, where staff discuss someone else

  "channel": "website_chat" | "amiigo_app" | "whatsapp" | "voice"
             | "dealer_app" | "internal_portal",
  "entry_metadata": { "pill_clicked": "battery_issue" | null, "referrer": "..." },
  "message": { "text": "...", "attachments": [...] },
  "timestamp": "..."
}
```

Every adapter must emit exactly this shape. Everything downstream — enrichment, guardrails, triage,
sub-agents, logging — depends on it being stable.

**Four things this contract deliberately does, all of them corrections to earlier drafts:**

**No `frame_number`.** An earlier version carried it here, which was wrong: ownership lives in the
OMS warranty table and is fetched by tool *when a conversation turns on it*. An adapter cannot
populate it without an OMS call at adapter time, which is exactly what §3.2 moved out of identity
resolution. Identity says who someone is; it does not say what they own.

**`cluster_id` is the primary key of the whole system.** It is what identity resolution returns,
what enrichment queries on, what `profile_cache` is keyed by, and what an erasure request deletes by.
Anything downstream that needs "this person" uses this and nothing else.

**`strength` is load-bearing, not decoration.** It gates disclosure: `verified` (WhatsApp, Amiigo
login, OTP) permits stating name, purchases, frame numbers and warranty; `asserted` (caller ID —
network-supplied, spoofable) permits identification but requires a second factor before anything
financial; `anonymous` (cookie only) permits referencing product interest and nothing personal. It
is explicit rather than derived from `channel` because that inference would be wrong the moment a
channel changes behaviour.

**`identity` and `subject` are separate.** For customers and dealers they're the same person and
`subject` mirrors `identity`. For internal staff they diverge — an employee asking about a
customer's warranty is the *actor*, the customer is the *subject*. Collapsing them means either
losing who asked (no audit trail) or losing whose data it is (no scoping). Internal isn't built yet,
but the field exists now so adding it later isn't a contract change.

**The reply shape needs its own attachments field**, because the agent sends workflow diagrams and
video clips (§4, §6) — not just the inbound attachments shown above. **Built 2026-08-06**: media
authored on a knowledge record now travels from retrieval to `Reply.attachments` in code, rather
than relying on the model to copy a URL it cannot see.

**One field exists in code that is not listed above: `customer_id`.** It is an artefact of the
mocked fixtures, not of the real system — the OMS has no customer ID, and `frame_number` identifies
the bike (§2). It is carried so the mocked tools have a key to look up, and **it must not become
load-bearing**: nothing may branch on it, and it disappears when the real OMS integration lands.

### 3.2 Persona/identity resolution — deterministic, not LLM

This is code, never a model guess. It runs in **two layers** that answer two different questions,
and conflating them is the mistake to avoid:

| Layer | Answers | Source | What it authorises |
|---|---|---|---|
| **Identity graph** | *Who is this person?* | `identities` table, keyed on phone / cookie / WhatsApp ID — see `Website_Anonymous_Identity_Approach.md` | Continuity and personalisation: past chats, browsing history, which conversations belong together across devices and channels |
| **Warranty record** | *What do they own?* | The OMS warranty API, keyed on phone → frame number | Any statement about a bike, its model, or its coverage |

The order matters. Resolve the person first, then their hardware. A person can exist in the graph
with no warranty record at all — that is not a failure, it is the Late Warranty Registration case
(§4.1). And the reverse guardrail is absolute: **browsing history never authorises a claim about
ownership or coverage.** Only the warranty record does.

There is one customer identity key: the **phone number**. Amiigo has already authenticated the
customer on it, and WhatsApp supplies it with the message, so both channels hand the same key to
the same resolver.

Four states result. The middle two are the ones the earlier draft of this plan missed:

| State | What we know | Goes to |
|---|---|---|
| **Verified owner** | phone verified, warranty record found, frame number returned | Product Support, with bike and coverage attached before the LLM sees anything |
| **Verified, no bike record** | phone verified, warranty API returns no record | Late Warranty Registration (§4.1) — they are probably still a customer |
| **Known but anonymous** | no phone, but a cookie cluster carrying past chats and browsing behaviour | General help, personalised from history — but **no ownership or warranty claims** |
| **Cold** | nothing at all | General help, or straight to a human |

A null frame number is the unregistered signal, and branching on it is a code branch, not a
routing decision the model gets to make.

Dealers resolve on phone number plus dealer ID; internal users on SSO — and internal is
structurally different, because the employee asking is not the person the conversation is about.
That persona needs the actor and the subject held separately, plus authorisation on who may look at
whose record. Neither dealer nor internal is built for use case #1.

Open, and it affects the tool contract: one phone number may map to **more than one frame
number** — a household with two bikes, or a repeat buyer. The agent must ask which bike the
conversation is about rather than silently taking the first row.

### 3.2.1 Context enrichment — assembling what the model sees

Identity resolution answers *who is this*. Enrichment answers *what do we know about them*, and
renders it into a compact block for the prompt. It runs after §3.2 and before the guardrails.

**Four sources, one of them optional:**

| Source | Gives | Cost |
|---|---|---|
| `profile_cache` | Top models viewed, price band, intent signals | One indexed query |
| Conversation summaries | Last few conversations, across all channels | One indexed query |
| `events` | Same-session activity if the cache is stale | One bounded query |
| OMS warranty API | Owned bikes — **only if the cluster has a verified phone** | Network call, cached |

**It is deterministic code, not a model call.** It queries and formats with a template. Adding an
LLM here would roughly double per-turn latency, double cost, and risk a frame number being
hallucinated in transit — for work that is an f-string. The one place a model legitimately appears
is writing a conversation summary *after* a conversation ends, asynchronously, off the hot path.

**Compute lazily, cache the result.** Do **not** build a scheduled job that precomputes profiles for
every visitor: most never chat, so it is largely wasted, and a nightly rollup misses the browsing
someone did five minutes before opening the chat — which is the most relevant browsing there is.
Aggregating one person's 90 days of events is a single indexed query over a few hundred rows.

**Three disciplines that keep it cheap:**

- **Summarise, don't dump.** Raw clickstream and full transcripts degrade answer quality as well as
  cost — models get worse when the relevant fact is buried among irrelevant ones.
- **Hard token budget** (~800), dropping lowest-value and oldest items first. Without a ceiling this
  grows unbounded and nobody notices.
- **Decay by recency.** A cycle viewed eight months ago is noise.

**It enforces the disclosure rule.** A cookie identifies a *browser*, not a person — shared family
laptops are common. So enrichment filters by identity strength before the model ever sees anything:

| Cookie-resolved (soft) | Requires a verified phone |
|---|---|
| Product interest on this device — "looking at the Doodle again?" | Name, purchase history, frame numbers, warranty status, order details |

**Runs once per conversation, not per turn.** The customer's bikes and history don't change
mid-chat, so build the block at conversation start and reuse it — which also keeps it in a stable
prompt position for caching (§6).

Empty context is normal, not an error: a genuinely new visitor produces almost nothing, and the
triage prompt has to read naturally when that happens.

### 3.3 Channel adapters — all four channels
WhatsApp, Amiigo, website chat and IVR are all in scope. They share one resolver and one message
contract; each adapter is thin, and differs only in which identifier it hands over:

| Adapter | Identifier it supplies | Strength |
|---|---|---|
| WhatsApp | `wa_id`, which is the phone number | verified |
| Amiigo | phone, authenticated at login | verified |
| Website chat | the `em_aid` cookie, resolved to a cluster | anonymous unless that cluster holds a verified phone |
| IVR / voice | caller ID | asserted by the telco |

**Website chat is no longer the odd one out.** It was deferred while identity meant "a phone
number," because the site has no reliable login. The identity graph changes that: the cookie *is*
its identity, and a returning visitor who verified an OTP at any point arrives already known. It
needs no session path of its own.

**IVR needs one thing the others don't**: a speech layer in front of the adapter — speech-to-text
inbound, text-to-speech outbound, and a correlation ID so a chat-to-call handoff stays threaded
across the WhatsApp provider, our backend and the voice vendor (see
`Emotorad_Unified_AI_Architecture.md` §2.5). Buy that plumbing; don't build it. Downstream of the
speech layer, a call is just another message on the same contract.

**Caller ID is weaker than the other identifiers.** It arrives free, with no interaction, but the
network asserts it rather than the customer proving it, and it can be withheld or spoofed. Good
enough to identify and personalise; **require a second factor before any financial or warranty
consequence**.

Dealer-app and internal-portal adapters are the same pattern, applied later.

### 3.4 Tool registry — the first real tools
Each wrapped once, engineer-maintained, consistent contract (`{"data": ..., "freshness_seconds": ...}` on success, `{"error": {...}}` on failure — same envelope pattern from the reference Cars24 codebase):

- `lookup_warranty_record(phone)` — the OMS warranty API. Returns frame number, bike model and
  `purchase_date`, or an explicit "no record" that triggers the unregistered path. This is one
  call, not two — **built 2026-08-02**; the earlier plan's separate `get_customer_profile` and `get_warranty_status`
  collapse into it. **Coverage dates are not in the response** and are derived in code from
  `purchase_date + term` (§2) — so the tool returns `warranty_start`/`warranty_end` that we
  computed, tagged `term_source: "provisional"` so a consumer can tell a derived date from an
  authoritative one. A null `purchase_date` returns `purchase_date_missing`, never a guess.
- ~~`get_battery_diagnostics`~~ — **not buildable, and not a "later" item.** There is no
  telematics feed from the bike, so this tool does not exist and diagnosis stays conversational.
  Do not stub it: an absent tool is a fact the agent can reason about, an empty one invites it to
  guess.
- `search_battery_knowledge(query)` — retrieval over the digitised battery troubleshooting
  workflows (§6), returning both the step text and references to the diagram or video that goes
  with it.
- `create_support_ticket(frame_number, category, description, severity)` — writes to Zoho Desk,
  confirmed for customer tickets as well as dealer. Returns the ticket number, and carries the
  frame number so support can identify the exact bike. Idempotency key required — a retried call
  must not create a duplicate ticket.

  **`frame_number` is the one identifier the model supplies rather than the registry injecting**,
  because a customer may own several bikes and only the conversation establishes which one. So the
  registry must **validate it against the set the warranty API returned for this cluster** and
  reject anything else. Without that check, a hallucinated or injected frame number writes a ticket
  against a bike — possibly someone else's — that this customer does not own. Same principle as
  identity injection: the model may choose from a set, never invent a member of it.
- `start_warranty_registration(...)` — for the Late Warranty Registration sub-agent (§4.1).
- `check_service_center_slots(pincode)` / `book_service_slot(...)` — if the resolution path is "bring it in," reuses the same kind of calendar lookup as the dealer test-ride booking.

### 3.5 Triage agent — a real one, not a stub
An earlier draft had the router branching on identity state, on the assumption that a customer
with a frame number wants to talk about their bike. That is wrong: they may want a new cycle, or an
order update, or a human. **Identity determines which routes are available; intent determines which
is taken.**

| Identity state | Routes available |
|---|---|
| Verified, ≥1 frame | Product Support · Order Status · Pre-sales · General |
| Verified, no frame | **Late Warranty Registration** · Order Status · Pre-sales · General |
| Anonymous or cold | Pre-sales · General |

So routing is conversational and happens **mid-conversation**, not on the first message — you
cannot classify an intent that hasn't been expressed. The triage agent greets with whatever
context enrichment supplied, disambiguates which bike when several are owned, captures the issue in
free text, classifies it, and hands off. A sub-agent must be able to hand *back* when the topic
changes.

This needs conversation state (`awaiting_bike_selection`, `awaiting_issue`, `routed`) that the
current code does not have.

**Classify with the LLM before reaching for a semantic router.** Customers write in Hindi, Marathi,
Tamil and transliterated Hinglish; Claude handles that natively, multilingual embedding models are
weaker, and a semantic router needs curated examples per intent *per language* — plus you have no
labelled data yet. Classification runs once per conversation, not per turn, so its latency is
affordable. Log every classification with the raw text: that becomes the training set that makes a
semantic router worth building later.

### 3.5.1 The knowledge base is a product, not a data load

The current plan says "ingest the workflows into pgvector," which treats the knowledge base as a
one-time load. It isn't. It holds validated troubleshooting videos and photos at issue and
sub-issue level, diagnostic PDFs, SOPs and standards — and **all of it changes**: new spares get
added, troubleshooting methods are revised, new failure modes get identified, new photo angles get
captured. Content that can only be updated by an engineer running a script goes stale within weeks,
and stale retrieval is invisible (risk register #12: retrieval fails silently).

**So the knowledge base needs an editorial front end for SMEs** — the service and product people who
actually know how a battery fails — not a repo they can't use.

**This is not AIRO, and must not be deferred with it.** AIRO is an agent-*builder* for business
users, deliberately postponed until several sub-agents exist. This is content maintenance for a
system that ships in R1, and the content starts decaying immediately.

#### Content model — structured authoring, which is also the fix for chunking

Authoring at this granularity means **each record is already a retrievable chunk**, which removes
the single largest cause of retrieval failure. Do not author free-form documents and chunk them
afterwards.

```
Component (battery, motor, display, gear)
└── Issue            "will not charge"
    └── Sub-issue    "charger LED does not light"
        ├── Symptom text        ← what the customer describes, in their words
        ├── Diagnostic steps    ← ordered, each safe for a customer to perform
        ├── Media               ← photos/videos, tagged by angle and purpose
        ├── Resolution          ← fixed, or escalate with this ticket category
        ├── Applies to          ← models, battery variants, date ranges
        └── Status              draft | in review | published | superseded
```

`Applies to` matters more than it looks: a troubleshooting step valid for one battery variant may be
wrong for another, and retrieval must filter on it rather than relying on similarity.

#### Requirements

- **Only `published` records are indexed.** When a record is superseded, it is **removed from the
  index**, not merely down-ranked — semantic similarity has no relationship to recency, so a stale
  step will otherwise outrank a current one indefinitely.
- **Publish triggers re-embedding.** Editing content and re-indexing are one action, not two.
- **Draft → review → publish, with a named approver.** This is customer-facing safety-adjacent
  guidance; it should not go live because one person typed it.
- **Full audit trail.** Who changed which step, when, and what it said before.
- **Media library** with angle and purpose tagging, feeding the S3/CloudFront pipeline (§6).

#### What to actually use — and when to decide

**The content model above is the thing that must be right from day one. The tool is not.** Get the
granularity wrong and retrieval suffers and you re-migrate; pick the wrong editor and you swap it.

**For R1: structured files in the repo, plus the sync worker.** R1's content comes from *one*
author. That is not a multi-SME editorial workflow, and standing up a CMS for it is the same
over-building this plan avoids elsewhere:

- One YAML or Markdown file per sub-issue, in the shape modelled above
- **Git is the audit trail** — who changed which step, when, and what it said before
- **Pull requests are the approval workflow**, with a named reviewer
- Media in S3, referenced by key
- Zero new infrastructure, and the content model is exercised for real before it's locked in

**Build the sync worker either way**: on publish → chunk by record → embed → upsert into pgvector;
on supersede → delete from the index. That's the only genuinely custom piece, it's small, and it is
identical regardless of what the editor turns out to be.

**Revisit when the second and third SME arrive** — service and product people who won't touch Git.
That is the moment an editor earns its cost, and by then you'll know what the approval flow actually
needs. Two things to check at that point rather than assume:

| | |
|---|---|
| **Directus** | BSL 1.1 — all features, free **only under $5M annual revenue**. Emotorad is likely above that, so price the commercial licence |
| **Strapi** | MIT community edition with no revenue condition, but **RBAC, SSO and audit logs are paid tiers** — exactly the features listed as requirements above |
| **Custom UI** | Roughly 1–2 weeks for CRUD, media and status; 4–8 weeks with versioning, roles, approval and audit. Not months — but it is 4–8 weeks the AI engineer is not doing AI, plus maintenance forever |

There is no free option at your size. Decide it against a real quote and a real SME count, not an
assumption.

#### The connection to photo validation

The reference photos SMEs capture per sub-issue and angle are what make customer-submitted photo
validation tractable. The question stops being the open-ended *"what is wrong with this bike?"* and
becomes *"does this match the reference for 'swollen pack, side view'?"* — cheaper, far more
accurate, and it improves every time an SME adds an angle. That reframing is what turns vision from
a research project into a feature.

### 3.6 Observability
Log every conversation, every tool call and its result, every escalation — from the first test conversation, not after launch. This is what lets you build the golden regression set in §6 and is the same plumbing every future sub-agent (any persona, any channel) will log through.

---

## 4. The Product Support sub-agent — detailed design

**System prompt scope:** a customer-support agent for Emotorad e-cycle batteries. Job is to understand the symptom, check warranty/ownership context automatically (already attached via 3.2 — never ask the customer for their model/purchase date if it's already known), walk through basic troubleshooting for common issues, and create a support ticket or escalate when it can't resolve the issue conversationally.

**Conversation flow (typical path):**
1. Customer describes the issue (or arrived via a "Battery issue" pill, in which case intent is already known — skip straight to symptom detail).
2. Agent asks 1–2 clarifying questions only if needed (e.g., "won't charge" vs "loses charge quickly" vs "won't turn on") — model/purchase/warranty context is already loaded, not asked.
3. Agent walks through 2–3 basic, safe troubleshooting steps for that symptom (e.g., check charger connection, try a different outlet, confirm the battery is seated correctly) — these come from a fixed knowledge source: RAG over the battery troubleshooting workflows the product team has already documented, not the model's general knowledge. This is the clearest RAG use case in the whole journey map, same reasoning as the dealer EMI-options RAG case. Because those workflows exist as diagrams and videos, the agent should be able to **send the matching image or clip** rather than only describing the step in text — which needs an outbound media field on the reply and somewhere to host the assets (§6).
4. If resolved — close out, log outcome.
5. If not resolved — call `create_support_ticket` with a clean summary (category, symptom, troubleshooting already attempted, warranty status), confirm the ticket number and expected turnaround to the customer, done.

**Hard guardrails — enforced in code, not just prompted:**
- **Safety branch is a hard stop, not a model judgment call.** Any mention of swelling, smoke, burning smell, unusual heat, or visible damage triggers an immediate, deterministic branch — stop troubleshooting, do not suggest continued use or self-repair, surface an emergency-safety message, and escalate to a human/priority ticket instantly. This check should run as a keyword/classifier gate ahead of the main agent turn, not rely on the LLM choosing to notice it.
- **Warranty status is always a tool call.** The agent never estimates or infers whether something is covered — coverage is computed in code from the start and end dates `lookup_warranty_record` returned, and the agent states that answer.
- **No bike record means no claims about the bike.** On the unregistered path the agent has no frame number, model or coverage, so it must not state any of them — it hands over to §4.1 rather than guessing from what the customer says they own.
- **Ticket creation is idempotent and confirmed.** Same discipline as the dealer order-placement flow — no silent double-submission on retry.
- **Escalation is reachable at any point**, not just after the agent gives up — a customer typing "talk to a human" mid-flow exits immediately, no friction.

### 4.1 Late Warranty Registration — the second sub-agent

**Why it exists:** warranty registration is frequently never completed, so a real customer with a
real bike can be absent from the warranty table. Without this path, that customer hits a dead end
on the one thing the platform needs from them, and every downstream flow — support, service
booking, claims — stays blocked. It is also a data-quality win: every conversation that ends in a
completed registration improves the table the rest of the platform depends on.

**Trigger:** deterministic, from §3.2 — never a model decision. **Two triggers reach this
sub-agent, and they must not sound the same to the customer:**

| Trigger | State | Opening line |
|---|---|---|
| Warranty API returned **no record** | We have no bike for this phone | Register the bike — collect frame number, proof, date, channel |
| `lookup_warranty_record` returned `purchase_date_missing` | We have the bike; the date is blank | *"I can see your \<model\>. I just need your invoice or proof of purchase to confirm the date you bought it."* |

The second case is new (added 2026-08-01, after real OMS rows showed null purchase dates). It
routes here because the ending is identical — a human reads an invoice and writes a verified date —
but **telling a customer whose bike is registered that they need to register it reads as though we
lost their record.** The agent already knows the model and frame number and should say so; only the
date is missing. The tool signals this with `remedy: "collect_purchase_proof"` on the error
envelope, so the routing is a code branch rather than the model inferring it from prose.

**Channel constraint:** this path needs a file upload. On voice/IVR there is nowhere to put an
invoice, so the flow must hand off to WhatsApp or a human rather than dead-ending — check channel
capability before promising the customer an upload.

**Job:** collect what a registration needs — the frame number (read off the bike; the agent should
be able to send a diagram showing where to find it), proof of purchase, purchase date, and whether
it came from a dealer or a marketplace — then submit it and tell the customer what happens next.
Once registration succeeds the conversation can hand back to Product Support with a real bike
record attached.

**Hard guardrail:** the agent must never fabricate or infer a warranty start date. That date
determines what Emotorad owes the customer, so it comes from the submitted proof, and anything
ambiguous goes to a human rather than being resolved in chat.

**A customer-supplied date is not a verified date.** The warranty start is exactly the field a
customer has an incentive to shift, and an uploaded image is not self-proving — so a date read off
an invoice (by OCR or by the model) is a *claim* until a human confirms it against the document.
Never write it straight into the warranty record, and never quote coverage back to the customer
based on it in the same conversation. The honest reply is "we've received your invoice and someone
will confirm your coverage", not a computed expiry date. Two consequences worth building for:
extraction and verification are separate states, and the record must retain who verified it.

---

## 5. Release plan

### Two tracks, running in parallel

**Track W — website identity.** Owned by the web team, specified end-to-end in
`Website_Anonymous_Identity_Approach.md`. It has no dependency on the AI work and should start
immediately, because it must land *during* the website revamp — a cookie you didn't set on launch
day cannot be backfilled.

**Track A — the bot.** Everything below. Only R3 depends on Track W.

### Where your three bots land

The scope was given as *core architecture, Bot 1 (battery), Bot 2 (motor), Bot 3 (place order)*.
Nothing has been dropped — but it is translated into a different vocabulary, so here is the mapping:

| Your framing | Where it is | Why it looks different |
|---|---|---|
| **Core architecture** | R0 | Same thing, renamed "skeleton rework" because code already exists and needs changing rather than writing |
| **Bot 1 — battery** | R1 | Ships as the *Product Support* agent, loaded with battery content |
| **Bot 2 — motor** | R2 | **Not a second bot** — the same agent, plus motor content in the same index. Days, not weeks |
| **Bot 3 — place order** | R4 | **Confirmed as dealer order placement** (W2). A different *persona*, not just a sub-agent — which makes it the second-largest release after R1 |

Two things to be explicit about, because they're judgement calls rather than facts:

**Battery and motor are split across two releases even though both content sets are ready.** The
reason is not content availability — it's that R1 is proving an entire pipeline for the first time
(retrieval, chunking, reranking, media delivery, the eval loop). Adding a second content domain to
that first proof doubles the surface you're debugging while you still don't know whether the
pipeline works. Once R1 is green, R2 is genuinely small. **If you'd rather launch with both, the
cost is a larger R1 eval scope, not extra engineering** — that's your call to make.

**R3 and R5 are channels, not bots.** Website chat and IVR add reach for agents that already exist,
which is why they sit ahead of order placement despite your numbering — both reuse the proven
Product Support agent, whereas R4 opens an entirely new persona. Confirmed: channels first, order
placement after.

### Battery and motor are not two bots

Worth settling before planning around it. Battery and motor faults share the same tools, the same
ticket flow, the same service booking and the same conversation shape. They differ only in
*content* — which workflows and diagrams get retrieved. Building them as two agents means two
prompts to keep in sync forever, for no gain.

**One Product Support agent; battery and motor are two content sets in the same index**, tagged by
component. That makes motor a **content release measured in days**, not a second bot measured in
weeks. It is the single biggest saving available in this plan.

### Start these now — they have lead times you cannot compress

Nothing below blocks on code, and all of it blocks *something* later. Kick these off before R0.

| Item | Why now | Owner |
|---|---|---|
| **WhatsApp template submission and tier check** | Approval takes 1–24h for utility, 1–3 days for marketing, and wrong-category rejection is common. Confirm your current tier actually fits the canary volume (tiers run 250 → 2,000 → 10,000 business-initiated/day) | Ops |
| **Legal: EU data transfer** | India has **no EU adequacy decision**; transfers need SCCs and a Transfer Impact Assessment, and an EU authority has refused one before. This gates the deployment topology | Legal |
| **Legal: EU warranty wording** | EU consumers hold a statutory 2-year guarantee independent of our commercial warranty. We need fixed, approved phrasing | Legal |
| **Legal: dealer data sharing** | Sending browsing history to dealers is third-party disclosure under DPDP and GDPR; needs a clause in dealer agreements | Legal |
| **Get the battery/motor workflows off the PM's Mac** | Single point of failure, and R1 can't start the retrieval work without them | Product |
| **The warranty API cURL** | R1's critical path | Kush |
| **Business case number** | Cost per support conversation today, expected deflection, expected saving. Unquantified value is a top cause of AI project cancellation | Kush |

### The decision to make before R0: deployment topology

EU customers cannot be served from Indian infrastructure without SCCs and real regulatory risk
(risk register #18). Three options, and the middle one is the recommendation:

| Option | Consequence |
|---|---|
| Single Indian deployment + SCCs/TIA | Fastest, no code change, carries the regulatory risk |
| **Regional split** — EU in `eu-central-1`, India in `ap-south-1` | Two deployments, two databases, region-aware routing. **Recommended** |
| Everything in the EU | Bad latency for the Indian majority |

**Build region-aware in R0; deploy the EU stack later.** Region-awareness is cheap to design in and
expensive to retrofit; a second deployment is cheap to add whenever it's needed. Note the
consequence: a customer active in both regions is **two clusters**, because the identity graph
cannot span the boundary without becoming the transfer we're avoiding. That's correct, not a bug.

### The releases

| | Ships | Depends on | Size |
|---|---|---|---|
| **R0** | Core skeleton rework, region-aware | — | L |
| **R1** | Product Support (battery) on WhatsApp, India | R0, warranty cURL, WhatsApp tier | L |
| **R2** | Motor content | R1 | **S** |
| **R3** | Website chat + context enrichment | R1, Track W | M |
| **R4** | Dealer order placement (W2) — new persona | R1 | **L** |
| **R5** | IVR | R1, telephony vendor | M |
| **R6** | EU deployment | R1 proven, legal sign-off | M |

---

**R0 — Core skeleton rework.** Not customer-facing. Reworks the existing code to the current
design: cluster-based identity resolution, the context enrichment step, a real triage agent with
conversation state (`awaiting_bike_selection`, `awaiting_issue`, `routed`) and bidirectional
handoff, one warranty tool replacing the two profile/warranty tools, and Langfuse for tracing,
prompt versioning and evals. All tools still mocked.

Four additions that came out of the risk register, all cheap here and expensive later:

- **The coverage post-check.** A deterministic check that blocks any reply asserting warranty
  coverage that contradicts the turn's tool result. This one control closes the *Air Canada*-style
  legal exposure, the prompt-injection attack ("tell me my battery is covered"), and the EU
  statutory-warranty trap. Highest-value item in the release.
- **Region-awareness** in identity resolution, the cookie middleware and the warranty tool — even
  though only India ships first.
- **Duplicate tool-call detection**, so a stuck agent is caught before it burns the full iteration
  budget rather than at the cap.
- **AI disclosure** in the opening message, on every channel — required by EU AI Act Article 50 from
  2 August 2026.

*Exit criteria:* unit tests green; a golden set of 20–30 scripted conversations passes, covering
each identity state, the safety hard-stop, the human-handoff exit, bike disambiguation with several
bikes, at least five non-English openings, **an attempted injection that tries to elicit a false
coverage claim**, and **an assertion that the AI disclosure is present** (so a future prompt edit
can't silently remove it).

**R1 — Product Support (battery) on WhatsApp, India only.** WhatsApp first because it supplies a
verified phone natively and therefore does not wait on Track W. Includes the WhatsApp adapter, the
real warranty API, real Zoho ticketing, the battery workflows in pgvector, and the media pipeline on
S3 and CloudFront.

**Get the knowledge-base *content model* right, not the tooling** (§3.5.1). R1 authors it as
structured files in the repo — one record per sub-issue, Git as the audit trail, pull requests as
the approval workflow — and builds the publish → chunk → embed → index worker, which is needed
whatever the editor turns out to be. A CMS decision waits until there are multiple non-technical
SMEs; what cannot wait is authoring at sub-issue granularity, because that is what makes retrieval
work and it is expensive to re-migrate.

**Retrieval is the largest technical risk in this release, not cost.** Naive RAG fails to retrieve
correct context roughly 40% of the time, and it fails *silently* — a confident wrong answer with no
error. Three things are not optional:

- **Chunk on natural boundaries** — symptom, step, resolution — never fixed size. One study measured
  87% retrieval accuracy from adaptive chunking against 13% from fixed-size on identical data.
- **Retrieve broadly, rerank, pass a small final context.** That is the standard production shape.
- **Evaluate retrieval separately from the conversation.** "Given this symptom in Hindi, is the right
  passage in the top 3?" catches failures that an end-to-end golden set masks, because a
  plausible-sounding wrong answer reads fine to a conversational reviewer.
- Set a **confidence floor**: below it the agent says it isn't sure and raises a ticket, rather than
  answering from a weak match.

**Quality metrics, from the first day of canary — not after launch.** Klarna replaced 700 agents,
hit their volume targets, and were rehiring humans within eighteen months because satisfaction fell
while nobody was watching for it. Resolution rate is a volume metric and can rise while quality
falls:

- **CSAT on bot-handled conversations**, tracked separately from human-handled
- **Repeat-contact rate within 48 hours** — the best proxy for a false resolution
- **Escalation rate as a health signal, not a failure.** Falling escalation *alongside* falling CSAT
  is the failure mode, not a win
- **WhatsApp quality rating** — it gates your tier, and a degraded rating throttles the channel for
  the whole business, not just the bot
- **Tokens per conversation (p95)** and cost per *resolved* conversation

Roll out through three gates: **shadow** (runs alongside whoever handles battery complaints today,
outputs compared, nothing sent to customers) → **canary** (small share of real traffic) → **full**.
Targets: 90%+ tool-call accuracy, sub-3s latency, 70%+ resolution — but set the *production* target
from measurement, since published data suggests a 90% benchmark becomes 70–80% in the wild.

**Rollback is automatic, not a meeting.** If CSAT on bot conversations drops below human-handled by
an agreed margin, or WhatsApp quality rating degrades, the canary percentage reverts to zero.

**R2 — Motor content.** Ingest motor workflows into the same index, tagged by component. Extend the
golden set with motor conversations. No new agent, no new adapter, no new tools.

**R3 — Website chat and enrichment.** The first release where the identity graph pays off:
anonymous visitors, browsing history in context, the WhatsApp `ref:` stitch. Needs the disclosure
rule enforced — cookie-only clusters must never be told their own name, purchases or warranty
status.

**R4 — Dealer order placement (W2).** Confirmed as the *dealer* persona, not customers ordering.
This is the second-largest release after R1, because it is the first time the platform serves a
second persona — and that is precisely what the three-persona architecture was designed for, so it
is also the release that proves the skeleton generalises.

What it adds:

- **A dealer adapter** (Em Biz app and/or dealer WhatsApp), and **dealer identity resolution** on
  phone + dealer ID. Note the dealer number problem from the migration check: dealers frequently
  appear in the warranty table under their own phone, so dealer and customer identity must not be
  resolved by the same lookup.
- **Its own router scope.** A dealer asking about stock and a customer asking about stock are
  different questions with different authorisation. Routes never cross personas.
- **OMS write access** — and this is the first time the platform creates a financial obligation
  rather than a support ticket.

**The guardrails are stricter here than anywhere else so far**, because a mistake costs money
directly rather than costing a support interaction:

- **Order creation is a deterministic tool call with an idempotency key.** A duplicated order is
  real stock and real money, not a duplicate ticket. This is already the registry's behaviour;
  R4 is where it stops being theoretical.
- **The model never sets price, discount or credit terms.** Those come from the OMS and are stated,
  never computed or negotiated by the agent.
- **Confirm before commit.** The dealer sees the full order — SKUs, quantities, price, total — and
  explicitly confirms before anything is written. No implicit ordering from a conversational turn.
- **Credit and authorisation checks are code, not prompt.** Whether this dealer may order this SKU
  at this quantity against their outstanding ledger is a deterministic check before the write is
  attempted.

W5 (dispatch and order tracking) is the natural companion — it reuses the same adapter, identity
and tools, and it is largely read-only, which makes it a small follow-on rather than a separate
release.

**R5 — IVR.** Adds the speech layer (STT, TTS, call correlation ID) in front of the existing
adapter. Everything downstream is unchanged. Requires a telephony vendor decision first, and the
caller-ID caveat: identify and personalise on it, but require a second factor before anything with
financial or warranty consequence.

**R6 — EU deployment.** The same releases, run in `eu-central-1` with a separate database, once R1
is proven in India and legal has signed off. Three things differ from the Indian stack and none are
optional:

- **The cookie is consent-gated.** Under ePrivacy, `em_aid` requires prior opt-in — it cannot be
  minted on first request. So EU visitors are anonymous until they consent, and a meaningful share
  never will. Model the EU funnel assuming a large anonymous share; don't build EU dealer briefs
  that assume browsing history exists.
- **The warranty tool is region-aware.** EU consumers hold a statutory 2-year guarantee independent
  of our commercial warranty. The bot must never say a flat "not covered" to an EU customer — it
  states that the commercial warranty has expired, that statutory rights may still apply, and that a
  human will confirm. Fixed wording from legal, pinned in the prompt, not paraphrased by the model.
- **GDPR rights beyond erasure.** Right of access (export everything we hold about a person — the
  same traversal as erasure, in reverse), and no autonomous final refusal of a warranty claim, since
  Article 22 gives a right to human review.

Treat GDPR as the design baseline and DPDP as the subset. It is very close to the same work, done
once, rather than building for India and patching Europe on.

### What gates every release

The golden set is not an R0 deliverable that then sits still. **Every release extends it, and no
prompt change ships without a passing run.** Every production failure gets added to it permanently.
That habit — evals in CI — is the single biggest gap between this plan and how AI-first teams
actually operate.

Two failure modes to design against, because a golden set can quietly stop being honest:

- **Overfitting** — tuning prompts until the eval passes. Hold out a slice used only for final
  verification, never for tuning. The tell is eval scores rising while CSAT and escalation rate stay
  flat.
- **Drift** — production inputs change and static coverage decays. Refresh continuously from real
  traffic: every escalation, wrong answer and complaint becomes a case. Version it alongside the
  code so scores are comparable over time.

And report eval scores **per language**, never as a single aggregate — an average hides a weak
language, and Hinglish is where this bot will actually be judged.

---

## 5.1 Detailed build sequence for R0 and R1

1. **Write the message contract and tool interfaces on paper first** (§3.1, §3.4) — no code yet, just the exact function signatures and JSON shapes. This is cheap to change now, expensive to change after code depends on it.
2. **Mock every tool** with fake data matching real shapes (fake customer profiles, fake warranty statuses, fake ticket creation) — build and test the entire conversational flow against mocks before touching any real system, same approach validated for the dealer bot plan.
3. **Write the agent loop** — Python, Claude's tool-calling, the system prompt from §4, the mocked tools from step 2.
4. **Build the RAG layer** for battery troubleshooting content — get the documented battery workflows off the junior PM's Mac first, into version control and object storage with a manifest mapping symptom to diagram/video, then into a retrieval index. This is the one piece of this use case that's genuinely RAG rather than a tool call, and the assets existing on one laptop is a single point of failure worth removing early regardless.
5. **Test end-to-end against mocks** — script realistic battery complaints (won't charge, degraded range, swelling/safety case, warranty edge cases) and confirm correct tool calls, correct RAG grounding, correct hard-stop behavior on the safety branch.
6. **Wire the real channel adapters** (§3.3) — Amiigo and WhatsApp, both against the one phone-based resolver.
7. **Swap mocked tools for real integrations** — `lookup_warranty_record` against the OMS warranty API, `create_support_ticket` against Zoho — one tool at a time, re-testing after each swap. Then add the Late Warranty Registration sub-agent (§4.1) on the proven skeleton.
8. **Add the logging/observability layer** (§3.6) if not already present from step 2 onward — ideally this exists from the very first mocked test, not bolted on later.
9. **Build a golden set** — 20–30 real or realistic battery conversations, labelled with the correct expected tool calls/outcomes, used as a regression suite before every future prompt change.
10. **Shadow mode** — run the agent in parallel with however battery complaints are handled today (even just a person reading transcripts), comparing outcomes, before it's customer-facing.
11. **Canary rollout** — a small percentage of real customer traffic first, then full rollout once tool-call accuracy and resolution rate look right (targets from the earlier benchmarking: 90%+ tool-call accuracy, sub-3s latency, 70%+ resolution at launch).
12. **Full launch**, with monitoring live from day one, not added after something goes wrong.

---

## 6. Tech stack and deployment (recap, applied to this use case)

- **Language:** Python, matching the reference Cars24 codebase and the best-supported ecosystem for Claude's tool-calling.
- **Model access:** Claude via AWS Bedrock, since Emotorad's infra is already on AWS — keeps this traffic inside your existing AWS boundary rather than a separate vendor relationship.
- **Deployment:** its own small containerized service (ECS Fargate is the low-effort default), same AWS account/region as the rest of Emotorad's platform for low-latency access to OMS/ERP, but its own VPC subnet and security group — not sharing compute with the website or checkout, so a bad deploy here can't take down anything else.
- **RAG index:** a simple vector store is enough at this scale (pgvector if you already run Postgres, or a managed option) — no need for heavier infrastructure for one knowledge domain.
- **Workflow media hosting:** the battery workflow diagrams and videos need to live somewhere the chat surfaces can fetch them. S3 plus CloudFront in the same AWS account is the low-effort answer, with signed URLs — WhatsApp in particular needs a publicly reachable URL to render media, and enforces its own per-format size limits, which the videos may exceed. Storing the source assets in the repo and serving derived, size-compliant copies from S3 keeps the two concerns separate.
- **Evals and tracing:** Langfuse, self-hosted (free, open source) inside the same AWS account. It
  covers tracing, prompt versioning, datasets and evals in one tool, which closes three gaps at
  once — and self-hosting keeps conversation traces inside the boundary, for the same reason
  everything else is there. Build it in R0, not later; without it, model and prompt choices are made
  on vibes.

### 6.1 Cost model and the levers, in order of impact

Output tokens cost roughly **5× input**, and thinking tokens bill as output — so a support bot's
bill is driven less by the large system prompt than by how much the model *writes*, including
reasoning the customer never sees. The levers, most valuable first:

**1. Prompt caching.** The single biggest lever, and it costs nothing but a marker. Cached tokens
re-read at ~10% of price. Two breakpoints:

```
tools + system instructions   ─┐ breakpoint 1 — identical for EVERY customer
                              ─┘ permanently warm at any real volume
context block                 ─── breakpoint 2 — stable within one conversation
conversation turns                never cached
```

Breakpoint 1 is the one that matters: your tools and instructions are the same for every
conversation, so it is written once and read cheaply by everyone thereafter. On a ten-turn
conversation the two together take roughly 50–70% off the repeated portion.

Two Bedrock specifics: **automatic prompt caching is not available on Bedrock**, so place explicit
`cache_control` breakpoints (which is what the design assumes) — don't let anyone "simplify" it to
the automatic form. And **the Batch API is not on Bedrock either**, which removes a 50%-off option
for async work like conversation summarisation and eval runs. If that matters at volume, *Claude
Platform on AWS* offers the same AWS billing and IAM with full feature parity.

**What silently breaks caching** — no error, the savings just never appear: a timestamp or date
interpolated into the system prompt, non-deterministic tool serialisation order, varying the tool
set per conversation, or anything user-specific placed *before* the stable instructions. Verify with
`usage.cache_read_input_tokens`; if it is zero on repeat requests, one of those is happening.

**2. Effort level.** The biggest quality/cost dial. Sweep `low`, `medium`, `high` against the golden
set rather than guessing — for bounded work like battery triage, the lower settings are unusually
strong.

**3. Output discipline.** A short "be concise" instruction measurably shortens responses, and a chat
widget wants short answers anyway.

**4. Model tier per task.** Roughly a 5× spread between the cheapest and most capable tiers. Triage
classification and conversation summarisation do not need the same model as the support conversation
itself. Note caches are model-scoped, so switching mid-conversation drops the cache — but the
triage→sub-agent handoff changes the system prompt anyway, so that boundary is free.

**5. Context discipline.** Every token of stale history is paid for on every turn. This is why §3.2.1
summarises rather than dumps.

### 6.2 Choosing models — and when to add a second one

**Add models after you can measure, not before.** Until the golden set runs, a model choice is made
on price and vibes with no way to know what was traded away. Once evals exist, they answer the
question as data.

**The first swap worth testing is triage classification.** It is the highest-frequency call, the
output is tiny, and language understanding is the whole job — which is exactly where an
Indic-specialist model may beat a frontier model on quality *and* cost. Sarvam's open-weight models
cover 22 Indian languages including code-mixed Hinglish, and being open-weight they can be
self-hosted inside the AWS account, which would also remove the data-residency question. Test
against the golden set before switching anything.

**Two things to weigh before adding any vendor:** prompts do not transfer cleanly between models, so
every swap costs a re-tune and a full eval run; and each provider adds keys, quotas, failure modes
and an outage surface. Worth it where the gain is measured, not everywhere.

**And one carve-out:** the battery-safety branch is regex plus (eventually) embeddings, running
*before* any model call. Model choice cannot weaken it, which is what makes cost tuning safe here.

**Cost controls to build, not just measure:** alert on **tokens per conversation (p95)** rather than
monthly spend — a doubling week-over-week is the signal, the monthly bill is the autopsy. Set a hard
per-conversation token ceiling that escalates to a human rather than looping. And track **cost per
*resolved* conversation**, so cost and quality are traded explicitly rather than discovered.

---

## 7. What this proves, and what comes after

Shipping R1 end-to-end validates the entire skeleton — message contract, identity resolution, context enrichment, channel adapters, tool registry, triage and routing, observability — on real traffic. Everything built there is reused, not redone, by R2–R6:

- **Third customer sub-agent** (order status, or another journey-map item) — Late Warranty Registration is already the second, but it is selected by a code branch rather than by classification. This is the point where the router in §3.5 becomes real classification logic, because there is finally more than one sub-agent reachable from the same identity state.
- **Dealer persona** — the W1–W12 spec is already far more detailed than this use case; once the skeleton is proven, dealer WhatsApp (starting with W2/W5, per the earlier plan) plugs into the same message contract and tool-registry pattern, with its own adapter and its own sub-agents.
- **Internal persona** — web-portal-based, identity resolution is trivial (SSO), same skeleton again.
- **AIRO front end** — only once you're hand-building a third or fourth sub-agent and feel the repetition, extract a UI over the tool registry and router that already exist by then.

---

## 8. Open items to confirm before/while building

> **Scope discipline.** This section holds only what blocks the build. Negative cases, broken
> upstream fields and data-quality defects go to `docs/Emotorad_Edge_Case_Register.md` and **stay
> there** — the default disposition is CAPTURE, and shadow-mode frequency is what promotes one into
> real work. Every case in that register is individually defensible to fix; fixing them in sequence
> is how this project becomes a data-cleanup exercise instead of a working bot. **Build the happy
> path first.**

**Blocking R1, found 2026-08-01 when the real API responses arrived:**

- ~~**Where do warranty terms live?**~~ **Unblocked 2026-08-01 with a provisional rule**: 24 months
  from `purchase_date`, every component (§2). This lets R1 proceed. It is still not a *validated*
  business rule, so it carries a standing obligation: the real per-product terms must replace
  `fixtures.warranty_term_months()` before the bot answers a coverage question for a real customer
  in production. A wrong term is worse than no answer — it commits Emotorad to a repair it does not
  owe, or refuses one it does. Track this as a release gate on R1, not as a backlog item.
- **How does the warranty endpoint respond when a phone has no record?** Every sample so far is a
  phone *with* records. This is the response that routes a genuine customer to Late Warranty
  Registration rather than telling them we're down, so its exact shape — empty array, 404,
  `{"purchases": []}` — is load-bearing.
- **Is `purchase_date` reliably populated in production?** Non-null across the 100-row sample, but
  null in the API's own dummy record. If a meaningful share is null, coverage is undeterminable for
  those customers and the agent must escalate rather than guess.

The four items previously listed here are answered — see §2. What is genuinely still open:

- **The warranty API contract:** the cURL itself, plus auth, rate limits, and what it returns when
  a phone number has no record versus when the call simply fails. Those two must be
  distinguishable, because one routes to Late Warranty Registration and the other is an outage.
- **One phone, many bikes:** can the warranty table return multiple frame numbers for a single
  phone number? If so, the agent needs a disambiguation step before anything else (§3.2).
- **How big the unregistered population is.** This sizes Late Warranty Registration, and if the
  share is large it may deserve to ship alongside Product Support rather than after it.
- **Where the battery workflow media is hosted**, and how the assets get off the junior PM's Mac
  into version control and object storage (§6). Also whether the videos are within WhatsApp's
  size limits or need re-encoding.
- **Website chat's identity path**, whenever that surface comes back into scope — whether the site
  has customer login at all, and if not, how a visitor there is identified.

---

*This plan builds on `Emotorad_AI_Journey_Map.md`, `Emotorad_Unified_AI_Architecture.md`, the W1–W12 dealer WhatsApp flow spec, and the architecture discussion establishing persona-based routing (customer/dealer/internal), deferred AIRO front-end, and code-enforced guardrails over prompt-based ones.*
