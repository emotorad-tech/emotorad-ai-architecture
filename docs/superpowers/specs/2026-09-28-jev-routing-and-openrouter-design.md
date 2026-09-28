# Jev decision routing, standard responses and OpenRouter models

**Status:** Approved design, awaiting spec review

**Date:** 2026-09-28

**Scope:** `emotorad-ai-architecture`, customer persona only.

**Objective:** Cut model cost per turn without lowering reply quality:

- Jev (TypeSafe's System One decision model) scores each customer message against typed questions.
- Code uses those scores to pick one of three paths: a standard response with no LLM, a narrow cheap agent given only one knowledge record, or today's full agent.

## 1. Decisions

1. **Jev is reached through OpenRouter's Decisions API**, `POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`, with the OpenRouter key. It returns typed answers with probabilities, not text. The price is $0.042 per million input tokens, and output is free.
2. **The reply models are called through OpenRouter chat completions** with the same key:
   - narrow path: `deepseek/deepseek-v4-flash-0731`
   - full path: `anthropic/claude-haiku-4.5`

   All three model ids are settings.
3. **LangGraph orchestrates the turn.** `Runtime.handle(message) -> Reply` keeps its signature, so adapters, the API, the CLI and all existing tests are unchanged.
4. **Code decides the path from Jev's scores.** Thresholds are per question, stored in a reviewed file and set by calibration against a labelled set. They are never picked by feel.
5. **Anything Jev cannot answer cleanly falls back to today's full agent.** That covers a timeout, 429, 529, an unreadable answer, or scores below threshold. The worst case is today's behaviour at today's cost.
6. **The order of the fixed controls does not move.** Identity, the safety check and the human-handoff check run before Jev. The coverage post-check, the evidence post-check and the AI disclosure run after every path, including standard responses.
7. **Off by default.** `EMOTORAD_AI_MODE=openrouter` turns this on. `offline` and `bedrock` behave exactly as today, with no Jev call.
8. **Sachin signs off before real customer traffic.** This sends customer text to OpenRouter, TypeSafe and the model providers, outside AWS, which reverses the recorded Bedrock decision. EMotorad sells in Spain, so GDPR transfer rules apply. Until sign-off it runs in tests, the CLI and the playground only.

## 2. The turn, as a LangGraph graph

```
START
 → resolve_identity → build_context                      (runtime.py today)
 → safety_gate ──triggered──→ safety_reply ─────────────────────────────┐
 → handoff_gate ──triggered──→ handoff_reply ───────────────────────────┤
 → persona_route ──dealer──→ dealer_agent ──────────────────────────────┤
                 ──other persona──→ unsupported_reply ──────────────────┤
                 ──no warranty record──→ late_warranty_agent ───────────┤
 → triage ──triage replies itself (greeting, which bike, what issue)──→ ─┤
 → jev_classify                                                          │
 → choose_path ──standard──→ standard_reply ──→ post_checks ────────────┤
               ──narrow────→ prefetch_tools → narrow_agent → post_checks ┤
               ──full──────→ full_agent ───────────────────────→ post_checks ┤
                                                                         ↓
                                                               finish (disclosure, history, log) → END
```

- Each node is a thin wrapper over code that exists today: `_handle_safety`, `check_human_handoff`, `TriageAgent.handle`, `Agent.run`, `check_coverage_claim`, `check_evidence`, `_finish` and `_outbound`. Only `jev_classify`, `choose_path`, `standard_reply`, `prefetch_tools` and `narrow_agent` are new.
- The graph state is one `TurnState` dataclass per turn: `message`, `resolved`, `conversation`, `decision`, `route`, `prefetched`, `agent_turn`, `reply`. Conversation memory stays in the existing `ConversationStore`. There is no LangGraph checkpointer.
- With no Jev client configured, `jev_classify` returns immediately and `choose_path` returns `full`. That keeps the graph identical in behaviour to today's `handle()`, and the existing suite proves it.

## 3. Jev

### 3.1 Client: `src/emotorad_ai/jev.py`

```
JevClient(transport: OpenRouterTransport, model: str, timeout: float)
  .decide(state: dict, questions: dict[str, Question]) -> JevDecision     # raises JevError
choice(instructions: str, criteria: dict[str, str]) -> Question
noul(instructions: str, true: str = "", false: str = "") -> Question
JevDecision(answers: dict[str, ChoiceAnswer | NoulAnswer], model: str, cost: float | None, latency_ms: int)
ChoiceAnswer(choice: str, probabilities: dict[str, float], confidence: float)
NoulAnswer(p_true: float)
ScriptedJev(decisions)        # tests; records every call so tests can assert "never called"
```

**Request body:**

```json
{"model": "typesafe/jev-1.13", "state": {...}, "questions": {"<id>": {"type": "choice|noul", "instructions": "...", "criteria": {...}}}}
```

**Response parsed:**

```json
{"id", "model", "provider", "answers": {"<id>": {"type": "choice", "choice", "probabilities", "confidence"} | {"type": "noul", "noul"}}, "usage": {"input_tokens", "output_tokens", "cost"}}
```

**Validation.** Any failure here raises `JevBadResponse`, and the turn falls back:

- every question asked has an answer of the right type
- the `choice` value is a key of that question's criteria
- every probability and `noul` value is within [0, 1]

**Error mapping:**

| Condition | Error |
|---|---|
| 401 | `JevAuthError` |
| 429 | `JevRateLimited` |
| 529, 5xx, timeout, connection error | `JevUnavailable` |
| Anything unreadable | `JevBadResponse` |

The key never appears in an exception, a log line or a `repr`.

**Wire-format guard.** The Decisions API is alpha. `scripts/jev_probe.py` makes one real call and saves the response, with the state text replaced by a placeholder, to `docs/api-shapes/jev-decisions.json`. A test parses that saved file, so a change in the live format fails the suite rather than silently sending every turn down the full path. The saved shape is created when a human first runs the probe with their key. Until then, the test uses the shape documented by OpenRouter (section 11).

### 3.2 The questions: `src/emotorad_ai/decisions.py`

One call per message. The question set is built from files, never hand-written in code:

| Id | Type | Criteria built from |
|---|---|---|
| `standard_response` | choice | every **approved** standard response's `criteria`, plus `none` ("anything that needs more than one of the standard replies, or any question about this customer's bike, order, warranty or a fault") |
| `category` | choice | `battery`, `motor`, `none_of_these`. Descriptions state the boundaries: charging, range and power-on are battery; noise, assist, throttle and cutting out while riding are motor |
| `sub_category` | choice | every live knowledge record (`title` plus its `symptoms`), plus `none` |
| `error_code` | choice | every code in `knowledge/_errors/codes.yaml`, plus `none` |
| `language` | choice | `english`, `hindi`, `hinglish`, `marathi`, `tamil`, `other` |
| `needs_warranty_lookup` | noul | "The customer asks whether something is covered, or about warranty, replacement or cost of repair" |
| `needs_service_slots` | noul | "The customer wants to visit, book or find a service centre" |

**The state sent to Jev.** This is the minimum, following Jev's own guidance that unrelated content costs accuracy:

```json
{"message": "<redact_pii(current text)>", "recent_turns": ["<redact_pii>", ...up to 3], "channel": "whatsapp",
 "bike_model": "EMX Plus", "current_sub_category": "battery-wont-charge" | null}
```

It carries no name, phone, frame number, address or warranty status. `bike_model` is a product name, which is not personal data, and it lets Jev tell the Doodle records from the standard ones.

### 3.3 Path selection: a pure function

```
route(decision: JevDecision | None, error: str | None, thresholds: Thresholds,
      conversation: ConversationState, bike: dict | None, catalogue: RoutingCatalogue) -> Route
Route(path: "standard" | "narrow" | "full", standard_response_id, category, sub_category,
      error_code, language, prefetch: list[PrefetchCall], reasons: list[str], scores: dict)
```

Rules are evaluated in order, and the first match wins:

1. No decision (Jev disabled or errored) gives `full`, with the reason `jev_disabled` or `jev_error:<code>`.
2. **Standard** is chosen when all of these hold:
   - `standard_response.choice != none`
   - its probability is at least `thresholds.standard_response`
   - `language` is at least `thresholds.language`
   - the response has a reply for that language
3. **Narrow, new record** is chosen when all of these hold:
   - `category` is at least `thresholds.category` and not `none_of_these`
   - `sub_category` is at least `thresholds.sub_category` and not `none`
   - the record's `topic == category`
   - the record applies to `bike`, using the `applies_to` and `excludes` rules `KnowledgeBase` already uses

   It sets `conversation.sub_category`.
4. **Narrow, continuing** is chosen when `conversation.sub_category` is set and Jev does not confidently name a *different* category. This covers "yes, the light is red now" in the middle of a flow.
5. Otherwise **full**. The full agent is the topic agent for `category` when that is confident, otherwise `conversation.agent` as triage set it.

**Prefetch** applies to the narrow path only. Each is a read-only call through the registry, so identity is still injected:

- `lookup_warranty_record` when `needs_warranty_lookup` is at least `thresholds.tools.lookup_warranty_record`
- `lookup_error_code(code)` when `error_code` is at least `thresholds.error_code` and not `none`
- `find_service_slots(pincode=bike.pin_code)` when `needs_service_slots` is at least its threshold and the bike has a PIN code

Write tools (`create_support_ticket`, `book_service_slot`, `submit_warranty_proof`, `send_guide_media`) are **never** prefetched.

### 3.4 Thresholds: `knowledge/_routing/thresholds.yaml`

These are strict starting values until calibrated:

```yaml
standard_response: 0.90
language: 0.80
category: 0.85
sub_category: 0.75
error_code: 0.85
tools:
  lookup_warranty_record: 0.60
  find_service_slots: 0.70
calibrated_at: null        # set by scripts/calibrate_jev.py
calibration_set_size: 0
```

The loader rejects any value outside (0, 1] and any unknown key.

## 4. Standard responses: `knowledge/_standard/*.yaml`

```yaml
id: std-thanks-goodbye
status: draft                 # draft | approved; only approved records are live
approved_by: ""               # required when status is approved
criteria: >
  The customer is only thanking us or saying goodbye, with no new question, problem or information.
  Not this if the message also asks something or reports that a problem continues.
replies:
  english: "..."
  hinglish: "..."
  hindi: "..."
examples: ["thanks", "thank you so much", "dhanyavaad", "ok bye"]
counter_examples: ["thanks but it still won't charge", "ok, and is it under warranty?"]
```

**Load-time validation.** This raises at startup, like `KnowledgeError`:

- The id is unique.
- `criteria` is not empty.
- `approved` requires `approved_by`.
- Every reply passes `check_coverage_claim(reply, [])` without being blocked, and passes `check_evidence(reply, False)` without being blocked. So no standard reply can claim coverage or conclude a fault.
- No reply contains a placeholder such as `{name}`. Standard replies are identical for everyone and never personal.

**Seed drafts in this change, all `status: draft`:**
- `std-thanks-goodbye`
- `std-acknowledged`, for "ok, I'll try that", which gets "Take your time, tell me what happens once you've tried it"
- `std-are-you-a-bot`

None states a business fact, such as support hours, charging time or price. An expert approves them in a PR before they go live. Until then the `standard` path is effectively off in production.

**Runtime backstop:** the standard reply still goes through both post-checks and the disclosure. If a post-check blocks it, that's an authoring bug: it is logged, and the turn falls through to `full`.

## 5. Narrow agent: `src/emotorad_ai/agents/narrow_support.py`

- **Definition:** `build_narrow_definition(record, prefetched) -> AgentDefinition`, using the existing `Agent` loop, so stuck-loop detection, the iteration cap, idempotency and error envelopes all come for free.
- **Tools:** `send_guide_media`, `create_support_ticket`, `find_service_slots` and `book_service_slot`. Writes stay model-requested and code-enforced, as today.
- **System prompt:**
  - Rules: warm, brief, one or two questions at most. Never claim coverage except from the warranty result below. Ask for a photo or video before concluding a fault. Reply in the customer's language.
  - The shared facts block (bikes and coverage, the same text the full agent gets).
  - The **one** record: `title`, `steps`, `escalate_when`, and media keys with captions.
  - The prefetched tool results.

  Target: under 1,500 tokens.
- **Prefetched envelopes are added to `AgentTurn.tool_calls` before the loop starts**, so `check_coverage_claim` sees the warranty result that the reply is based on.
- **Shared prompt blocks** move from private functions in `battery_support.py` to `agents/blocks.py`: `facts_block`, `context_block`, `entry_block`. Battery, motor and narrow all import them. The text is unchanged, and a test pins it.
- **Model:** the narrow `OpenRouterChat` (DeepSeek Flash). The full agents get the fallback `OpenRouterChat` (Haiku 4.5) in `openrouter` mode, and are unchanged in the other modes.

## 6. OpenRouter chat client: `llm.py` + `openrouter.py`

- **`OpenRouterTransport(api_key, base_url, timeout, opener=urlopen)`:** it POSTs JSON and maps 401, 402, 429, 5xx, 529 and timeouts to typed errors. It uses the standard library `urllib`, like `tools/oms.py`. The key is read from `OPENROUTER_API_KEY` only when the client is built. It is never stored on `Settings` (which gets printed) and never logged.
- **`OpenRouterChat(model, transport, max_tokens, cache_system)`** implements the existing `.create(system, messages, tools) -> LLMResponse`. The rest of the code keeps its current Anthropic-shaped history, so the playground, `ScriptedClaude` and the agents do not change.
- **Translation** is pure functions, each tested:
  - `to_openai_messages(system, messages)`:
    - text blocks become content
    - `tool_use` blocks become `assistant.tool_calls`
    - `tool_result` blocks become `role: "tool"` messages with `tool_call_id`
    - thinking blocks are dropped
  - `to_openai_tools(tools)`: `input_schema` becomes `function.parameters`.
  - `from_openai_response(body)`:
    - `finish_reason` `tool_calls` → `tool_use`, `stop` → `end_turn`, `length` → `max_tokens`
    - produces `api_content` blocks in the existing shape
    - reports `usage` including `cost`
- **Request extras:**
  - `"usage": {"include": true}` for cost
  - `"provider": {"zdr": true, "data_collection": "deny"}`, controlled by `EMOTORAD_OPENROUTER_ZDR` (default on)
  - for `anthropic/*` models, the system prompt is sent as a content part with `cache_control: {"type": "ephemeral"}`

## 7. Settings (`config.py`)

| Env var | Default | Purpose |
|---|---|---|
| `EMOTORAD_AI_MODE` | `offline` | `offline`, `bedrock` or `openrouter`. Moved from `api.py` into `Settings` so the CLI, the API and the playground agree |
| `OPENROUTER_API_KEY` | none | Read by the transport only |
| `EMOTORAD_OPENROUTER_BASE_URL` | `https://openrouter.ai/api` | Chat at `/v1/chat/completions`, decisions at `/alpha/decisions` |
| `EMOTORAD_JEV_MODEL` | `typesafe/jev-1.13` | |
| `EMOTORAD_NARROW_MODEL` | `deepseek/deepseek-v4-flash-0731` | |
| `EMOTORAD_FALLBACK_MODEL` | `anthropic/claude-haiku-4.5` | |
| `EMOTORAD_JEV_TIMEOUT` | `2.0` | Seconds. The turn target is under 3 s |
| `EMOTORAD_OPENROUTER_TIMEOUT` | `30` | Seconds |
| `EMOTORAD_OPENROUTER_ZDR` | `1` | Zero-data-retention providers only |

`openrouter` mode with no key fails at startup with a clear message. It never falls through to another mode silently.

## 8. Observability and cost

- **New event `jev_decision`:** the scores per question, the chosen path, the reasons, `latency_ms`, `cost` and the model snapshot. It never includes the state text; the redacted message is already logged by `inbound`.
- **`llm_turn`** gains `model` and `cost`.
- **`metrics.py`** gains:
  - the share of turns per path
  - average cost per turn per path
  - Jev fallback rate by error code
  - **cost per resolved conversation**: `cost_per_resolved` added alongside the existing `tokens_per_resolved`, using the existing `ConversationSummary.resolved` definition (no escalation, no repeat contact within `REPEAT_CONTACT_WINDOW_HOURS`)
- **`EventLog`** tests assert that neither the OpenRouter key nor an Amiigo-style token can reach the log.

## 9. Labelled set and calibration

- **`tests/data/jev_golden.yaml`**, with entries like `{text, language, category, sub_category, error_code, standard_response, needs_warranty_lookup, needs_service_slots}`. It is seeded from:
  - the 61 phrasings in `tests/test_retrieval_evals.py`, reused rather than copied: the eval list moves into this file and both tests read it
  - every standard response's `examples` and `counter_examples`
  - `none_of_these` cases: helmets, a new-bike price, an order status
  - warranty and service-centre phrasings
- **`scripts/calibrate_jev.py`** is run by a human with a key. At the whole set's size it costs under $0.01. It:
  - runs every entry through Jev
  - reports accuracy and coverage at candidate thresholds, **per language, never averaged**
  - proposes values for `thresholds.yaml`

  A human reviews the proposal and commits it with `calibrated_at` set.
- **`scripts/jev_probe.py`:** one call that saves the response shape (section 3.1).

## 10. Testing

Every test here is offline. None calls a real model or OpenRouter.

- **Existing suite unchanged and green.** The graph with Jev disabled must reproduce today's `handle()`. The two known `test_video` environment errors are reported, not hidden.
- **`jev.py`:**
  - the request body shape
  - a parse of the documented response
  - a parse of `docs/api-shapes/jev-decisions.json` once it exists
  - rejection of a missing answer, a wrong type, an unknown choice and an out-of-range probability
  - the 401, 429, 529, 5xx and timeout error mapping
  - the key absent from errors
- **`decisions.py`:**
  - the question set built from fixtures (approved standard responses only, live records only)
  - the state contains no phone, name or frame number
  - a table-driven test for `route()` covering every rule, every threshold boundary (exactly at and just below), the topic mismatch, the Doodle `excludes` case, the mid-flow continuation, and every error, each giving `full`
- **Standard responses:**
  - validation rejects a coverage claim, a fault conclusion, a placeholder, and `approved` without `approved_by`
  - a draft is never offered to Jev
- **Graph paths**, using `ScriptedJev`, a scripted narrow LLM and a scripted fallback LLM:
  - A safety trigger calls **neither Jev nor any LLM**.
  - A human-handoff request calls neither.
  - The standard path calls no LLM, and the disclosure is still applied.
  - The narrow path calls only the narrow model. Its prompt holds exactly one record. The prefetched warranty result satisfies the coverage post-check.
  - The full path calls only the fallback model.
  - A Jev timeout gives the full path, with the reason logged.
  - A narrow reply claiming coverage that contradicts the prefetched result is blocked.
- **`OpenRouterChat` translation:**
  - a round trip of text, a single tool call, parallel tool calls, and tool results with `is_error`
  - the `finish_reason` mapping
  - cost read from `usage`
  - `cache_control` present only for `anthropic/*` models
  - the `provider` extras present when ZDR is on
- **Transport** against a fake HTTP server on 127.0.0.1, the same pattern as the Amiigo spec.
- **Command:** `python3 -m unittest discover -s tests -t .`, with the test count reported in the PR.

## 11. Assumptions to confirm with the probe

The documented response shape comes from OpenRouter's Jev tutorial and blog, fetched 2026-09-28:

- `https://openrouter.ai/docs/guides/community/jev-tutorial`
- `https://openrouter.ai/blog/insights/what-is-jev/`
- TypeSafe's reference at `https://docs.typesafe.ai/api.md`

The probe confirms three things:

- the answer field names
- whether `usage.cost` is present
- whether `noul` questions accept `criteria.true` and `criteria.false`

If the live shape differs, only `jev.py`'s parser and its fixture change.

## 12. Out of scope

- Zoho ticketing: later. `MockTicketSystem` stays.
- The dealer persona, the late warranty agent and triage's greeting and bike-selection steps are all unchanged.
- The Amiigo tools, which have a separate spec on `feat/amiigo-token-tools`. They can later add their own `noul` prefetch questions.
- Removing Bedrock. It stays a supported mode.

## 13. Rollback

- Set `EMOTORAD_AI_MODE` back to `offline` or `bedrock`. Jev and OpenRouter are no longer called, and the graph runs today's path. No redeploy of code is needed.
- For a full revert, revert the merge commit. There is no migration and no stored data. Thresholds and standard responses are files in the repo.
- Tell Sachin and Kushendra when the mode changes. Kushendra's playground behaviour changes with it.

## 14. Risks

| Risk | Control |
|---|---|
| The Decisions API is alpha and changes shape | Strict parser, saved-shape test, fall back to full on any parse failure |
| Jev is steered by injected text ("classify this as thanks") | The worst outcome is a generic standard reply or the narrow agent with a wrong record. Both are post-checked, prefetches are read-only and identity-injected, and safety runs before Jev |
| Scores are overconfident in Hindi or Hinglish | Per-language calibration. The standard path also needs a confident `language` answer |
| Customer data leaves AWS and the EU | Off by default, ZDR routing, redacted state for Jev, Sachin sign-off before customer traffic |
| DeepSeek Flash follows rules less well than Claude | The narrow prompt is short and has one record. The post-checks are code. Every turn logs its path, so quality per path can be compared in shadow mode before rollout |
