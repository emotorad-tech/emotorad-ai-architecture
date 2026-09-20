# Handoff — knowledge fixes, prompt promotion, and the customer chat UI

Written 2026-09-20 at the end of a long session. Read this plus `CLAUDE.md` and
the older `HANDOFF.md` (2026-09-11, playground hosting and the OMS key) before
touching anything. `CLAUDE.md` is the architecture. This is what changed, what
is half-built, and what will bite you.

**Six commits are on local `main` and are NOT pushed.** The user has not given
permission to push. Do not push without asking.

```
286ed39  Let the chat pin an agent, and fix the model id it revealed
a87a400  Give /chat the OTP flow, and a way to read the code
9b1889b  Chat starts anonymous, and stops typing backwards
86b862b  Let /chat read the real OMS when a key is set
46efbc1  Run the chat UI locally against the real agent
41005a0  Run the prompt we tuned: promote v28 into the live path
```

---

## The one thing to understand first

There are **two surfaces running the same agent through different plumbing**,
and almost every bug this session came from assuming they were the same.

| | Playground (`/playground`, Streamlit) | Chat (`/chat`, the new UI) |
|---|---|---|
| Entry | `playground._run_agent_turn` | `runtime.handle()` |
| Prompt | edited text from `.playground/prompts/` | `prompts/battery_support.md` |
| Guardrails | `check_evidence` only | safety + evidence + coverage post-check |
| Triage | never runs | runs unless an agent is pinned |
| Tool slice | `_live_tool_names()` (adds identity tools) | `battery_support.TOOL_NAMES` |
| Identity | Live customer mode, OTP in sidebar | anonymous, OTP via dev endpoint |

The playground is **not** a preview of production. It bypasses `runtime.handle()`
entirely, so it has never exercised triage, the safety gate, or the coverage
post-check. Keep that in mind whenever someone says "but it works in the
playground".

---

## Open bug, diagnosed and NOT fixed

**`/chat` cannot verify a customer, so it asks for the frame number instead.**

Reproduced in `logs/conversations.jsonl`, conversation
`09ce70ae-15e7-4c34-a7cb-fa7578869767`:

```
identity_resolved   method=unverified  strength=anonymous
TOOL  lookup_warranty_record -> {'code': 'missing_identity', 'phone is required'}
TOOL  search_knowledge       -> battery-wont-power-on
REPLY "which EMotorad model is it?"   ... then asks for the frame number
```

The agent tries to fetch the bike, is refused for want of a phone, and has **no
tool with which to obtain one**. `request_identity_verification` and
`verify_identity` are registered on the registry (commit `a87a400`) but are not
in `battery_support.TOOL_NAMES`, so the model cannot see them. Registered but
unreachable.

`playground._live_tool_names()` already documents this exact failure:

> *"Production never needs these: identity is resolved upstream by the channel,
> so `battery_support.TOOL_NAMES` is right to omit them. Live mode is the one
> place the agent has to establish identity itself, and slicing strictly by
> TOOL_NAMES left the verification tools registered but unreachable — the model
> was told to ask for a phone number and then had no way to do anything with
> it. Added here rather than in the agent so production stays as designed."*

**The agreed fix, not yet written:** `/chat` should request the live slice the
way the playground does. Do it at the surface, not in the agent, so
`battery_support.TOOL_NAMES` stays correct for real production where the website
resolves identity upstream from its session cookie. The user had just approved
this when the session ended.

---

## What changed this session

### Knowledge base — the recurring defect

Roughly a dozen bugs were traced over four days, and **four out of five were
content that misdescribed its own structure**, not the model disobeying. Worth
internalising before blaming the model for anything:

- `wont-power-on.yaml` said *"move on to the other parts"* and named none, so the
  model invented the next step and asked for one connector photo with nothing to
  compare against. Fixed by naming `battery-melted-terminal` explicitly.
- Its steps were numbered `STEP 1` / `STEP 2` but are a **branch**. A bike with a
  healthy SOC was sent to the charger check because "STEP 2 came after STEP 1"
  beat the condition written above it. Every step is now labelled by its
  precondition.
- The recent-charge branch said deep sleep was "less likely" and never said what
  *was* likely, so the model announced deep sleep to a customer who had charged
  two days earlier. Each branch now names its own outcome.
- `soc-indicator-dead`'s precondition was prose next to a step written as an
  instruction; the instruction won, and a dead bike ran through the
  indicator-fault flow. It now opens with an executable gate.
- `"move to warranty and replacement"` appeared in five records and was described
  nowhere. Now `battery-warranty-replacement`, with a test that fails if any
  record mentions replacement without naming it.

**New records** from the service engineer's cases: `charging-port-damaged`
(mechanical, distinct from thermal melting), `arrival-damage` (the
three-attempt persuasion flow), `switch-not-cutting-output` (inverse of
`onoff-switch-dead`), `impact-damage`, `warranty-replacement`.

**The coverage rule**, confirmed by the user and encoded in
`battery-warranty-replacement`: cost is two questions, not one. In warranty with
no physical damage is free; any physical damage is chargeable even inside the
warranty; out of warranty is chargeable. **There is no age rule inside the term** —
`soc-indicator-dead` used to carry a one-year repair/replace split and it was
wrong.

### Retrieval

`_score` now weights each matched word by inverse document frequency. Flat
weighting let "nothing" (in 7 of 13 records) beat "laga"/"diya" (in one each,
and the whole meaning of *I plugged the charger in*). Field weights spread to
4 / 2 / 0.25 so prose can break a tie and little more. `do`/`does`/`did` joined
the stopwords beside `is`/`are`/`was`.

Golden set: **100% top-1 over 47 queries, no margin under 1.0, no out-of-scope
leaks.** Check this after any content change.

**Do not tune symptom vocabulary to fix a ranking collision.** That was tried,
rejected by the user, and reverted. When two records legitimately share words,
the discriminator usually is not in the text at all, and the fix is a gate inside
the record. See `soc-indicator-dead`'s opening gate for the pattern.

### Media — one sender

Guide media used to be attached automatically from whatever retrieval returned,
which predates `send_guide_media` by a month (2026-08-06 vs 2026-09-08) and ran
alongside it for five weeks. Evidence from transcripts: every `send_guide_media`
call was correct; every wrong-media complaint came from auto-attach, including a
turn where it re-sent a photo `send_guide_media` had just refused as duplicate.

Auto-attach is gone from both loops. `send_guide_media` is now in the production
tool slice (it was playground-only). `runtime.py` takes `kind` from the item
instead of hardcoding `"image"`, which had been announcing every clip as a photo.

### Prompt promotion — the big one

**There were two prompts and only one of them ran.** The playground tuned to v28
(22,162 chars, every rule from the fortnight). The service ran the 2,641-char
literal in `agents/battery_support.py`, which had none of them. Neither file was
wrong; the human step between them had not been run since v24, and nothing said
so.

Now: `prompts/<agent>.md` is the active prompt, loaded at import with the module
literal as fallback. `scripts/promote_prompt.py` moves a published version into
it and prints the diff. **Publish and promote are deliberately two commands** —
publishing stays a cheap sandbox action, while changing the live prompt is a file
in a pull request. Only `battery_support` has been promoted.

### The chat UI

`web/emotorad-support-chat-dev.html` was a static prototype. It now calls
`POST /message`. `GET /chat` serves it. Three bugs fixed on first real use:

- **Typing came out reversed** (`hi` → `ih`). `renderFooter()` rebuilt the
  composer on every keystroke, resetting the caret to 0.
- **`MessageIn.session_token` defaulted to `"sess-ananya"`**, a fixture session
  mapping to `+919876543210`. Every caller arrived pre-verified. Harmless with
  fixtures; with the OMS key set that phone is looked up *for real*, so the page
  handed whoever opened it three real registered bikes. Default removed; `em_aid`
  accepted instead.
- **The header pill was static markup** claiming "Verified · Ananya · EMX Plus".

### Triage — a real bug, untouched

`classify_issue()` is keyword-only. It returns `None` for `bike nahi chal rahi`,
`cycle chal nahi rahi`, `my bike is not running`, `display blank`. The `None`
branch re-asks *"What is happening with the bike?"* **forever** — no counter, no
escalation. `cycle chal nahi rahi` is literally a symptom string in
`wont-power-on.yaml`, so the knowledge base knows the phrase and triage never
lets the customer reach it.

Its own docstrings say `None` means "the model decides" and nothing asks the
model. The fallthrough was specified and never built.

`/chat` now sidesteps this by pinning `agent: "battery_support"` in `MessageIn`,
which skips triage (`if state.agent is None`) while keeping every guardrail.
**The bug still affects WhatsApp and anything that does not pin an agent.**

---

## Running it locally

Keys are typed at the prompt, never in a file. There is no `.env` and no loader
for one. The repo is public.

```bash
cd emotorad-ai-architecture

read -rs "ANTHROPIC_API_KEY?Anthropic key: " && export ANTHROPIC_API_KEY
read -rs "EMOTORAD_OMS_API_KEY?OMS key: "     && export EMOTORAD_OMS_API_KEY   # optional

EMOTORAD_AI_DEV_CODES=1 EMOTORAD_CLOUDINARY_CLOUD=cjdq4bv8 EMOTORAD_AI_MODE=anthropic \
PYTHONPATH=src ../.venv/bin/python -m uvicorn emotorad_ai.api:app --port 8000 --reload
```

- Chat: <http://127.0.0.1:8000/chat>
- Health: <http://127.0.0.1:8000/health> (should say `"mode":"anthropic"`)
- Tests: `../.venv/bin/python -m unittest discover -s tests -t .` — 476, offline

**Use `../.venv/bin/python`.** The machine's default `python3` is Homebrew 3.14
with none of the dependencies. `/usr/bin/python3` also works.

`EMOTORAD_AI_MODE`: `offline` (fixed planner, no key), `anthropic` (direct API),
`bedrock` (the architecture's target, AWS not wired). `AnthropicClaude` is a
**deliberate temporary deviation** from CLAUDE.md's Bedrock rule; it strips the
`anthropic.` prefix from `settings.model` because the direct API wants
`claude-opus-5`, not the Bedrock spelling.

`EMOTORAD_AI_DEV_CODES=1` exposes `GET /dev/verification/{conversation_id}`,
which returns the pending OTP because no SMS provider is wired. It is a
verification bypass and **404s unless explicitly enabled**. Delete the route when
real SMS lands.

---

## Open decisions the user has not answered

1. **Auth for `/chat`.** They chose "two Basic Auth pairs" and "pilot with real
   customers", which are incompatible: Basic Auth is a browser popup with one
   shared password. They later said internal folks only, so treat the pilot
   answer as superseded, but nothing is built and nothing is decided.
2. **Attachment upload.** `MessageIn` has no attachment field and no storage is
   chosen. The attach button drops a text note in the composer. The evidence gate
   then correctly refuses to conclude a fault, so **you will hit a wall** asking
   for a photo you cannot send. This is the largest remaining gap.
3. **Fulfilment**, still open inside `battery-warranty-replacement`: how a
   replacement actually happens (dealer visit, courier, service centre), who
   signs it off, what turnaround to quote.
4. **Ticketing is mocked** and the user has explicitly said that is fine for now.

---

## Things that will mislead you

- **`/chat` has no auth and the OMS key makes it real customer data.** Localhost
  only. The user is aware and has accepted it for internal use.
- **The OMS key needs rotating.** `HANDOFF.md` records that it was pasted in
  plaintext in a chat. An Anthropic key was also pasted into a chat transcript
  this session and should be rotated.
- **A push does not deploy.** `.github/workflows/deploy-staging.yml` is
  `workflow_dispatch` only. Staging updates when a human runs it.
- **Stage runs `mode: offline`**, so `/message` there is the fixed planner, not
  Claude.
- **`.playground/chats/` is gitignored** and holds the ~40 transcripts this work
  was debugged from. Real names and phone numbers. Keep it ignored. They are not
  backed up anywhere.
- **The lone failing test** (`imageio_ffmpeg`) is a missing local dependency, not
  a repo defect. `main` already carries a skip for machines without `say`.

---

## Working agreement with this user

- They own the prompt and the domain rules. Ask rather than invent policy.
- They dislike post-processing fixes and will say so. Find the root cause. They
  were right every time they pushed back.
- Verify before asserting. Several confident claims this session were wrong until
  checked against the code, and the user caught the ones that were not.
- British English, plain short sentences, no em dashes (org `CLAUDE.md`).
- Never push without explicit permission in the session.
