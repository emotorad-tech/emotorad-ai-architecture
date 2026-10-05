# Edge Case Register

**Status:** living document · started 2026-08-01

## The rule this document exists to enforce

**Capture, don't solve.** Every edge case below is real, and most of them are genuinely broken.
That is exactly why they are dangerous: each one is individually defensible to fix, and fixing them
in sequence is how this project spends six months improving EMotorad's existing data quality
instead of shipping a working bot.

So: when an edge case is discovered, it lands here with a disposition, and **work stops there**
unless it blocks the happy path. Building the happy path first is not a shortcut — it is the only
way to find out which of these actually matter, because production frequency decides priority and
we do not have production frequency yet.

**The default disposition is Capture.** Promoting anything out of Capture needs one of:
- it makes the happy path impossible (not merely uglier), or
- it is a safety, legal or money-losing failure, or
- production data shows it is frequent.

"A customer might hit this" is not a promotion criterion. Almost anything might happen.

### How to add a case

One row, honest disposition, and a note on what you'd need to know to promote it. If you cannot
state the harm in one sentence, it is probably not an edge case — it is an inefficiency.

---

## Dispositions

| Code | Meaning |
|---|---|
| **BLOCKS** | The happy path cannot ship without this. Work on it now |
| **SAFE-FAIL** | Already handled by a generic guardrail — escalate to a human. No specific work; verify the fallback actually catches it |
| **CAPTURE** | Log it, measure frequency in shadow mode, decide later. The default |
| **NOT OURS** | A real defect in an upstream EMotorad system. Report it to that system's owner; **do not fix it inside the agent.** Working around these is the trap this register exists to prevent |

---

## 1. Warranty and ownership data

| # | Case | Disposition | Notes |
|---|---|---|---|
| 1.1 | `purchase_date` is null on a real warranty row | **Resolved** | Returns `purchase_date_missing` + `remedy: collect_purchase_proof`; routes to §4.1 with a distinct opening line. The case that prompted this register |
| 1.2 | No warranty term exists in any system | **BLOCKS** (R1 gate) | Provisional 24 months in `fixtures.warranty_term_months()`. Risk 21. A wrong term is a commitment Emotorad didn't make |
| 1.3 | Warranty API response for a phone with **no record** — shape unknown | **BLOCKS** | Decides "route to Late Warranty Registration" vs "we're down". Cannot build the branch without the shape |
| 1.4 | One phone, multiple bikes (3 seen in real data) | **BLOCKS** | Disambiguation is on the happy path for any multi-bike owner. Already designed (`awaiting_bike_selection`) |
| 1.5 | Dealer registers warranties under their own phone → many customers collapse into one cluster | **BLOCKS** identity backfill | Gate with the collision query before any migration. Not a bot problem — a data problem that would corrupt the identity graph at its root |
| 1.6 | Customer-supplied purchase date (from invoice) is unverifiable and self-interested | **SAFE-FAIL** | Human verification step; never quoted as coverage in the same conversation. Risk 21 |
| 1.7 | Test/junk rows in production (`customer_name: "jsjsjs"`) | **CAPTURE** | Harmless until one is a real lookup. Measure how many |
| 1.8 | `product_color`, `product_value` are empty strings, not null | **NOT OURS** | Normalise on read; don't chase the source |
| 1.9 | Frame number the customer states doesn't match their owned set | **SAFE-FAIL** | Validation against owned set already specified. Could be a typo or someone else's bike |
| 1.10 | Bike sold/transferred to a new owner | **CAPTURE** | Warranty follows the bike, identity follows the person. Unknown frequency; no data model for it today |
| 1.11 | Warranty claim on a bike bought from a marketplace (not dealer/D2C) | **CAPTURE** | Different proof and possibly different terms |

## 2. Orders data

| # | Case | Disposition | Notes |
|---|---|---|---|
| 2.1 | Orders endpoint returns **dealer** orders, not customer orders | **NOT OURS** | Confirmed: 47 records, all one dealer. Correct tool for R4, wrong for customers. Do not build a customer order-status flow on it |
| 2.2 | `tracking_link` is the same static bit.ly across all orders | **NOT OURS** | Cannot answer "where is *mine*". Report to OMS owners; don't synthesise a tracking URL |
| 2.3 | `awb_number` / `invoice_code` are the **string** `"None"`, while `tracking_link` is real `null` | **NOT OURS** | Normalise `"None"` → null at the adapter boundary, in one place. Verified in the real response |
| 2.4 | `oms_status` and `order_status` are two vocabularies (`DELIVERED` vs `Delivered`) | **NOT OURS** | Pick one as authoritative and write down which. Never show either raw to a customer |
| 2.5 | Delivered/dispatch dates present but unvalidated against status | **CAPTURE** | A `DELIVERED` order with a null `delivered_date` will happen |

## 3. Identity

| # | Case | Disposition | Notes |
|---|---|---|---|
| 3.1 | Unverified email used as a merge key → two customers fused | **Resolved by design** | Merge only on verified co-occurrence. Already the rule |
| 3.2 | Caller ID is spoofable | **Resolved by design** | `asserted` strength, gates disclosure |
| 3.3 | `cluster_id` retired by a merge, stale references | **Resolved by design** | Events keyed on `em_aid`; resolve to cluster at read time |
| 3.4 | Same person, two devices | **Resolved by design** | Merge on verified phone |
| 3.5 | Safari ITP caps JS cookies at ~7 days | **Resolved by design** | Server-set cookie. Re-verify at implementation time |
| 3.6 | Ad blockers block GA entirely | **Resolved by design** | GA is not the spine |
| 3.7 | Customer edits the prefilled WhatsApp message, stripping `ref:` | **SAFE-FAIL** | Falls back to phone-only resolution — degraded, not broken |
| 3.8 | WhatsApp ref code expires (TTL) before the customer sends | **CAPTURE** | Pick a generous TTL; measure the miss rate |
| 3.9 | Shared phone (family, one number, two riders) | **CAPTURE** | Real in India. Breaks the person↔phone assumption. No data on frequency |
| 3.10 | Customer changes phone number | **CAPTURE** | Cluster fragments. No merge signal exists |
| 3.11 | A **foreign national-format** phone (a German `0151…`) cannot be normalised | **CAPTURE** | `normalise()` applies `+91` to any bare 10-digit number. Genuinely undecidable without knowing the country, so it is not a code bug. Safe today because every channel supplies E.164 — WhatsApp guarantees it — leaving hand-entered data as the only exposure. Promote if EU volume through manual entry becomes real |
| 3.13 | Enrichment does not **decay by recency** — build plan §3.2.1 asks for it | **CAPTURE** | A bike viewed eight months ago is noise. Harmless at current volumes because the events query is already windowed to 90 days; promote when a cluster's history is long enough for old browsing to crowd out recent |
| 3.12 | Trunk prefix (`09876543210`) and international access code (`00919876…`) | **Resolved 2026-08-02** | Both produced corrupt E.164 that could never match, creating a duplicate person silently. Found by QC of the reference implementation, not by a test failure. Regression test now covers both |

## 4. Channels

| # | Case | Disposition | Notes |
|---|---|---|---|
| 4.1 | Voice/IVR cannot accept a file upload — blocks the invoice flow | **BLOCKS** (once IVR ships) | Check channel capability before promising an upload; hand off to WhatsApp or a human |
| 4.2 | WhatsApp 24-hour session window closes mid-conversation | **CAPTURE** | Template message needed to reopen. Risk 15 |
| 4.3 | WhatsApp template approval / tier limits throttle volume | **CAPTURE** | Submission started. Risk 15 |
| 4.4 | Duplicate inbound webhook delivery | **SAFE-FAIL** | Idempotency on writes already enforced |
| 4.5 | Out-of-order message delivery | **CAPTURE** | Rare; conversation state is per-turn |
| 4.6 | Customer sends a photo/voice note when text is expected | **CAPTURE** | Common on WhatsApp. Needs an "I can't read that yet" reply, not a crash |

## 5. Conversation

| # | Case | Disposition | Notes |
|---|---|---|---|
| 5.1 | Safety keywords (swelling, smoke, heat) mid-troubleshooting | **Resolved by design** | Deterministic branch ahead of the model; asserts the model was never called |
| 5.2 | "Talk to a human" at any point | **Resolved by design** | Immediate exit |
| 5.3 | Hindi / Marathi / Hinglish code-switching | **BLOCKS** eval validity | Golden set must report per-language, never aggregate. Risk 6 |
| 5.4 | Customer asks about a bike they don't own | **SAFE-FAIL** | Disclosure rule; no data leaves the owned set |
| 5.5 | Prompt injection via customer message | **Resolved by design** | Risk 11. Customer is the attacker |
| 5.6 | Model loops on the same tool call | **Resolved by design** | Duplicate-call detection breaks early |
| 5.7 | Customer changes intent mid-flow (battery → order status) | **Resolved by design** | Bidirectional handoff in triage |
| 5.8 | Customer abandons and returns days later | **CAPTURE** | Resume vs restart is a product decision, not a technical one |
| 5.9 | Angry/abusive customer | **CAPTURE** | Escalate; don't build sentiment handling in R1 |
| 5.11 | Triage classifies with **keywords, not the LLM** — build plan §3.5 says classify with Claude | **CAPTURE** | Found 2026-08-06 by plan/code QC. Keywords are exact, free and currently 100% on the golden set, so this is not yet a problem. It becomes one on phrasings nobody anticipated — which is most of them. The `classification` log event now captures every decision with its raw text, so the LLM fallback can be added *and evaluated* against real misses rather than guesses |
| 5.12 | A sub-agent cannot hand **back** to triage — `ConversationState.hand_back()` exists but nothing calls it | **CAPTURE** | Build plan §3.5 requires it. Matters when a customer changes topic mid-flow ("battery's fine now, but the motor is noisy"); today that stays with the first agent. Not blocking because the agent can still escalate, and the bike selection is retained either way |
| 5.13 | The model concludes "terminal intact" from a photo too far away to read, and nothing checks the photo was usable | **CAPTURE** | Found 2026-09-20/21 on the owner's melted X1 C terminal: the close-up reads "melted" every time, the wide shot reads "intact" three times out of four. An over-close shot of bare pins has the opposite failure (which part is it?). Video cannot enter the production loop at all. Full write-up and problem statement for the intern in `docs/problems/2026-09-21-photo-evidence-and-video.md`. Not solved on purpose: log the adequacy classification first, count `too_far` and `too_close`, then decide between a code gate with retake requests and a camera-led stencil |
| 5.10 | A bare warranty question — "am I still covered?", "is this under guarantee?" | **CAPTURE** | Found 2026-08-04 during integration. Matches no battery/motor keyword, so triage replies "what is happening with the bike?" — confusing, because nothing is wrong with it. Very likely high-frequency. The fix is a `warranty` topic, but it is not obviously battery support's job, so decide the owning agent from shadow-mode volume rather than guessing now |

## 6. EU

| # | Case | Disposition | Notes |
|---|---|---|---|
| 6.1 | Consent refusal blocks the cookie → no identity graph | **SAFE-FAIL** | Anonymous path must work standalone. Risk 17 |
| 6.2 | EU statutory 2-year runs from **delivery**, ours from purchase | **CAPTURE** | Terms coincide at 24 months today, which masks the divergence. Risk 19 |
| 6.3 | AI disclosure required on first turn | **Resolved by design** | Risk 16, all channels |
| 6.4 | No EU–India adequacy decision | **CAPTURE** (legal) | Deployment topology decision. Risk 18 |

## 7. Handovers that record no Zoho ticket (2026-10-05)

From the Zoho Desk spec (`docs/superpowers/specs/2026-10-05-zoho-desk-tickets-design.md`, "Not changed in this version"). Each one tells the customer a person takes over, or could, and records nothing for a person to act on. Each is counted by the event named, so shadow mode can promote it.

| # | Case | Disposition | Notes |
|---|---|---|---|
| 7.1 | The evidence-not-forthcoming handover records no ticket | **CAPTURE** | Counted by `escalation` with `reason: evidence_not_forthcoming`. The `guardrail_triggered` event beside it says which path: `guardrail: evidence_not_forthcoming` (asked to show the fault, nothing came back) or `guardrail: evidence_post_check` (a conclusion reached having seen nothing) |
| 7.2 | The coverage and order post-check blocks hand over and record no ticket | **CAPTURE** | `escalation` with `reason: coverage_claim_blocked` or `order_claim_blocked`; `guardrail_triggered` with `guardrail: coverage_post_check` or `order_post_check` |
| 7.3 | A ticket promised with none behind it, in a reply that is not about a live hazard, hands over and records nothing | **CAPTURE** | `guardrail_triggered` and `escalation`, both `ticket_promise_unbacked`. A reply about a live hazard is backed instead (`safety_backstop`): the safety ticket is raised, or for a customer with no number known the reply asks for one. A backstop ticket that fails (`safety_backstop_failed`), and anyone who is not a customer, fall back to this handover |
| 7.4 | Agent loop failures hand over and record nothing: model unavailable, an empty reply, a stuck tool loop, the iteration budget | **CAPTURE** | `llm_error` (with `escalation` and `reason: model_unavailable`), `empty_reply` and `stuck_agent` (each with `escalation` and `reason: agent_requested_handover`). The budget has no event of its own, only `escalation` with `reason: agent_requested_handover` |
| 7.5 | Model and store outages, other than safety, hand over and record nothing | **CAPTURE** | `escalation` with `reason: llm_error` or `llm_error_after_write`; `store_unavailable` (the event, and the `reason` of its `escalation`). A safety report while the store is down is handled: it logs `safety_ticket_not_recorded`, which is alarmed |
| 7.6 | The unsupported persona hands over and records nothing | **CAPTURE** | No event of its own: the `outcome` event with `handled_by: router` and `escalated: true` |
| 7.7 | Triage's "unsupported topic" says it will put the customer through, and records nothing | **CAPTURE** | `routed` and `classification` with `reason: unsupported_topic:<topic>`. The reply is not marked `escalated` |
| 7.8 | Prose handovers in the late-warranty, motor, dealer and photo-safety prompts ("I'll pass you to our team") record nothing | **CAPTURE** | No event yet: the model writes the line without escalating. A scan of bot replies for handover phrases would count it |
| 7.9 | A request for a person in Hindi or Hinglish misses the handover gate | **CAPTURE** | No event counts a miss, because nothing fires: the triggers are English only (`guardrails._HANDOFF_TERMS`). The `inbound` event keeps every message's text, with numbers hidden, so misses can be found by searching it |
| 7.10 | A non-Indian number typed for a call-back is refused | **CAPTURE** | `callback_number_invalid`, from the call-back gate, on a try made while it waits. The first try, in the message that asked for a person or reported the hazard, shows as `callback_asked`. EU volume through the web chat would promote it |

## 8. Ticket references and log redaction gaps (2026-10-05)

Found in the reviews of the Zoho Desk work (Tasks 10 and 11) and left alone on purpose. None is a regression. No event counts them, so each is found by searching the log text.

| # | Case | Disposition | Notes |
|---|---|---|---|
| 8.1 | A conversation whose context block was built while Zoho was off keeps its old mock reference (`EM-0xxxx`, five digits) after a deploy that turns Zoho on | **CAPTURE** | The block is kept in the conversation's working state, which lives 48 h. The reference exists nowhere in Desk, but `_ticket_known` still accepts a reply that names it. It ends with the working state. Found by searching `outcome` text for an `EM-0` reference after the deploy |
| 8.2 | A mobile number glued to letters stays visible in the log and in the Zoho transcript after a label or a change of script: `mobile:9876543210pls`, `phone=9876543210pls`, `9876543210plsधन्यवाद`, a loosely grouped number run into a word (`970 000 0010पर`), and a word made only of the letters a to f (`9876543210bad`) | **CAPTURE** | `redact_pii` hides a number glued to letters only after a space, a bracket, a quote, a comma or a semicolon. The verify step does not read the loosely grouped ones either, so the reader and the log agree. Found by searching `inbound` and `outcome` text for ten digits |
| 8.3 | A Spanish number written with the `00` international prefix (`0034 612 345 678`) stays visible in the log and in the Zoho transcript | **CAPTURE** | The redaction hides `+<country code>` forms, which is what the spec names. Spain is live, so this is the form to watch for |

---

## What to do with CAPTURE items

Nothing, until shadow mode runs. Then the register becomes useful in the way it is meant to be:
**shadow mode produces frequency counts, and frequency promotes cases out of CAPTURE.** A case that
never fires in 10,000 real conversations was correctly deferred. A case that fires in 8% of them
was correctly captured and is now the top of the backlog.

That is the whole argument for building the happy path first: it is the instrument that tells us
which of these thirty-odd cases are real.
