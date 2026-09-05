# Custom bot builder — design

**Date:** 2026-09-05
**Status:** approved design, awaiting implementation plan
**Scope:** let internal staff add a new sub-agent ("bot") from the playground, defined as
configuration rather than Python, and test it through the same pipeline production runs.

## 1. Why now

The build plan defers the AIRO agent-builder front end "until you're hand-building a third or
fourth sub-agent and feel the repetition". There are four sub-agents today: `battery_support`,
`motor_support`, `late_warranty_registration`, `dealer_orders`. The motor agent's docstring says
a second bot "should be almost entirely configuration". In spirit it is; in practice adding one
touches six hardcoded places:

- the agent list in `runtime.py`, `agents/__init__.py`, and the playground's `AGENT_MODULES`;
- `TOPIC_KEYWORDS` in `triage.py` and the "I can help with battery and motor problems" fallback;
- the `topic` enum on the `search_knowledge` tool in `tools/mocks.py`.

The playground is also prompt-only: no triage, no tool execution, no guardrails, and it calls
the Anthropic API directly. A custom bot's routing and tools are never exercised before a PR.

## 2. Bot spec

One YAML file per bot under `bots/`, committed and reviewed like knowledge records.

```yaml
name: brakes_support        # snake_case, unique; the handled_by value in logs and replies
persona: customer           # customer | dealer
topic: brakes               # knowledge topic AND triage topic; unique per persona
keywords:                   # triage; English plus the Hindi/Hinglish forms seen in traffic
  - brake
  - braking
  - ब्रेक
  - brek
tools:                      # names from the tool registry; validated at load
  - lookup_warranty_record
  - search_knowledge
  - create_support_ticket
prompt: |
  You are the brake support assistant for EMotorad, an Indian e-cycle company. ...
```

### Validation (at load, raise rather than skip — same rule as `knowledge.load_records`)

| Rule | Why |
|---|---|
| `name` unique across built-ins, published and drafts; `topic` unique per persona (a customer bot and a dealer bot may share a topic name and its `knowledge/<topic>/` directory) | duplicate names would silently shadow a bot |
| `persona` in `{customer, dealer}` | the runtime only has hydration paths for these two |
| every `tools` entry exists in the registry | an absent tool must be a load failure, not a runtime 500 |
| `tools` within the persona allowlist | codifies the `dealer_orders.py` rule: a dealer bot must never reach `lookup_warranty_record` |
| `keywords` disjoint from every other bot of the same persona | overlapping keywords make `classify_issue` return None for both, and neither bot is ever reached — checked for containment in either direction, because classification matches by substring |
| `prompt` non-empty | |

Persona allowlists live in code (`bots.py`), not in the spec, so a spec cannot widen them.

## 3. Catalogue and routing

### New modules

- `agents/blocks.py` — the shared prompt blocks move here from `battery_support.py`:
  `_describe`, `_coverage_line`, `_facts_block`, `_context_block`, `_entry_block`, plus
  `_account_block` from `dealer_orders.py`. Battery, motor and dealer agents import from here.
  `tests/test_motor_support.py::test_the_context_blocks_are_shared_not_copied` must keep
  passing: the functions are moved, not copied.
- `agents/generic.py` — `definition_from_spec(spec) -> AgentDefinition`. Builds
  `build_system_prompt` as `spec.prompt + persona blocks`: customer bots get
  facts + context + entry; dealer bots get the account block. Nothing changes in `base.py`.
- `bots.py` — `BotSpec` dataclass, `load_specs(directories)`, and `BotCatalogue`:
  - built-ins registered as `BotSpec` objects with their existing `DEFINITION` attached
    (their `topic`/`keywords` move out of `triage.TOPIC_KEYWORDS` into the agent modules);
  - YAML specs from `bots/` (published) and the drafts directory (see §5);
  - `definitions()` → `{name: AgentDefinition}`;
  - `topic_agents(persona)` → `{topic: name}`;
  - `keywords(persona)` → `{topic: [..]}`;
  - `topics()` → sorted list, for the `search_knowledge` enum;
  - `supported_summary(persona)` → "battery, motor and brake problems", for the fallback reply;
  - `validate(registry)` → raises on any rule in §2.

### Changes to existing modules

- `runtime.py` — `Runtime(catalogue=None, ...)` defaults to `BotCatalogue.load()`. Agents,
  `TriageAgent` inputs and the dealer path are derived from it. `TOPIC_AGENTS` / `DEALER_AGENTS`
  constants are removed.
- `triage.py` — `TriageAgent(topic_agents, keywords, supported_summary)`. `classify_issue`
  takes the keyword table as an argument (module-level `TOPIC_KEYWORDS` kept only as the
  built-in default so existing tests do not change). The unsupported-topic reply uses
  `supported_summary`.
- Dealer path in `runtime.handle` — classify the message against dealer-persona keywords;
  route to the match, else `dealer_orders`. No bike selection, no triage conversation:
  dealers keep today's straight-to-agent behaviour. The route is sticky for the
  conversation, as on the customer path; switching topic mid-conversation is a follow-up
  (wire `ConversationState.hand_back`).
- `tools/mocks.build_registry(topics=...)` — the `topic` enum on `search_knowledge` comes from
  the catalogue. Default remains `("battery", "motor")`.
- Safety, handoff, coverage post-check and disclosure are untouched. They run in code before
  and after the model, outside the spec, so a custom bot cannot weaken them.

## 4. Playground builder

New sidebar mode alongside the existing prompt editor.

- **Bot list** — every catalogue entry tagged built-in / published / draft.
- **New bot form** — name, persona, topic, keywords (one per line), tools (multiselect from
  `registry.specs`, filtered by the persona allowlist), prompt (textarea prefilled from a
  per-persona template that already includes the "never ask for model/date/coverage" and
  "every claim comes from search_knowledge" rules), and an optional knowledge editor.
- **Knowledge editor** — N records in the existing `knowledge/README.md` format (id, title,
  symptoms, steps, escalate_when; `applies_to` and `media` optional). Written under
  `<drafts>/knowledge/<topic>/<id>.yaml`. The knowledge base loads the drafts directory in
  addition to `knowledge/` so a fresh topic has something to retrieve.
- **Save draft** — writes `<drafts>/bots/<name>.yaml`, runs `validate`, shows errors inline,
  reloads the catalogue. The bot is immediately selectable for chat.
- **Export for review** — copies the draft spec and its knowledge files into `.playground/`
  as the files an engineer moves into `bots/` and `knowledge/` in a PR. Nothing the UI does
  touches tracked files, same as the prompt diff today.
- The existing prompt editor keeps working for built-ins and now edits a draft's `prompt`
  field in place.

## 5. Test flow: same pipeline as production

The chat panel builds one `Runtime` per playground session instead of calling the Anthropic
SDK directly.

| Component | Playground | Production (`api.py`) |
|---|---|---|
| registry | `build_registry(oms_available, today, topics=catalogue.topics())` | `build_registry(...)` |
| catalogue | published + drafts | published only |
| LLM | `OfflinePlanner` when no key; else `AnthropicClaude(api_key, model)` | `BedrockClaude` |
| resolver | `StaticResolver(resolved)` returning the chosen preset or custom rider | `IdentityResolver` |
| log | `EventLog` to an in-memory list | JSONL file |

- `AnthropicClaude` is a new class in `llm.py` with the same `create(system, messages, tools)`
  contract as `BedrockClaude`, backed by `anthropic.Anthropic(api_key=...)`. It is the only
  new LLM code; the agent loop, tool loop and post-checks are unchanged.
- `StaticResolver` lives in `identity.py`: `hydrate()` returns a fixed `ResolvedIdentity`.
  Preset riders continue to hydrate through the real `IdentityResolver` first, so they stay
  honest as the mocks evolve; the static resolver just pins the result for the session.
- Conversation state persists in `st.session_state` keyed by conversation id, so the
  multi-bike selection, pill, and hand-back paths all work across turns. A "pill" selectbox
  offers the selected bot's topic so the pill path is testable.
- Every turn runs identity, enrichment, safety and handoff guardrails, triage, the
  sub-agent tool loop, the coverage post-check, and disclosure.
- Each reply shows `handled_by`, tools called, escalation and ticket id. A "Trace" expander
  lists the `routed`, `tool_call`, `guardrail` and `classification` events for that turn.
  The prompt-preview expander stays.
- Attachments keep working: uploads become `InboundMessage.attachments`; `AnthropicClaude`
  renders image and PDF blocks the way the playground does today.

## 6. Persistence and deploy

- Drafts directory from `EMOTORAD_AI_BOT_DRAFTS`, default `.playground/drafts` locally (holding `bots/` and `knowledge/`).
- `deploy-staging.yml`: bind-mount `/opt/emotorad-ai/playground` to `/app/.playground` in the
  `docker run` command so drafts survive a redeploy (the tarball excludes `.playground` and
  the container is rebuilt every deploy).
- `bots/` is added to the tarball and to the Dockerfile `COPY` list.
- Drafts are staging-only working state. Anything meant to survive is exported and PR'd.

## 7. Tests

- `tests/test_bots.py` — spec validation (unknown tool, dealer bot with warranty tool,
  duplicate name, overlapping keywords), catalogue derivation (`topics()`,
  `supported_summary`), and drafts loading alongside published.
- `tests/test_generic_agent.py` — a YAML customer bot through `Runtime` with `ScriptedClaude`:
  it routes on its keywords, inherits the customer context verbatim, multi-bike
  disambiguation, disclosure, the coverage post-check and the human handoff. Mirrors
  `InheritedBehaviourTests` in `test_motor_support.py`. A YAML dealer bot routes on keywords
  and falls back to `dealer_orders`.
- `tests/test_llm_anthropic.py` — `AnthropicClaude.create` with a fake client: text, tool_use
  and usage mapping identical to `BedrockClaude`.
- `tests/test_identity_graph.py` gains a `StaticResolver` case.
- Existing tests: `test_triage.py` and `test_motor_support.py` must pass unchanged.
- Streamlit code stays untested beyond pure helpers, as now. The deploy workflow's test
  step remains the gate.

## 8. Out of scope

- New tools, new guardrails or new ticket categories from the UI. These stay code changes
  with a named human review, as the build plan requires for anything financial or
  safety-critical.
- Editing built-in Python bots beyond their prompt.
- A knowledge editor for *published* content (`knowledge/`); the build plan defers that
  until a second SME arrives.
- Dealer triage with disambiguation, channels other than website chat in the playground,
  and roles beyond the existing basic auth on `/playground`.

## 9. Decisions recorded

- Bots are files in Git, not rows in a database. Same reasoning as the knowledge base:
  Git is the audit trail, PRs are the approval, `git revert` is the rollback.
- Custom bots pick from existing tools only. The spec can narrow what a bot may call; it can
  never widen a persona's allowlist.
- The playground calls the real `Runtime`. A prompt-only preview is kept out on purpose:
  the coverage post-check and the tool loop are what a new bot most needs to be tested
  against, and a preview that skips them is a plausible-looking wrong answer.
