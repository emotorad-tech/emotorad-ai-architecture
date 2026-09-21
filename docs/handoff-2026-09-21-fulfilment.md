# Handoff: the chat on a real phone, and the first replacement order

Written 2026-09-21, after the session of 2026-09-20. Read this plus `CLAUDE.md`
before touching anything. The previous handoff, `docs/handoff-2026-09-20-chat-ui.md`,
is still accurate about the two-loop trap and the working agreement; this one
supersedes its open-bug and open-decision sections.

**Branch `feat/chat-identity-and-mobile` is 49 commits ahead of `origin/main`
and nothing is pushed.** Seven of those are from 2026-09-19. Do not push without
the person saying yes in the session.

---

## What a fresh session needs to know first

The chat works on a real phone, for a real customer number, end to end: verify
by one-time code, diagnose from two photos, conclude a defect, confirm coverage,
collect an address, place a mocked replacement order, hand back an order id. It
did that for the first time at 17:21 UTC on 2026-09-20 (conversation
`b186a5dd`). Every rule in this document was learned by watching that and the
runs before it fail.

The two-loop trap from the previous handoff still holds: the playground bypasses
`runtime.handle()`. It also still drops narration the model writes beside a
tool call, which the production loop no longer does. "Works in the playground"
proves nothing about triage, the post-checks, or what the customer reads.

## Running it

Keys are typed at the prompt, never in a file. The repo is public.

```bash
cd emotorad-ai-architecture

read -rs "ANTHROPIC_API_KEY?Anthropic key: " && export ANTHROPIC_API_KEY
read -rs "EMOTORAD_OMS_API_KEY?OMS key: "     && export EMOTORAD_OMS_API_KEY

EMOTORAD_AI_APPROVAL_MODE=bot \
EMOTORAD_AI_DEV_CODES=1 EMOTORAD_CLOUDINARY_CLOUD=cjdq4bv8 EMOTORAD_AI_MODE=anthropic \
PYTHONPATH=src ../.venv/bin/python -m uvicorn emotorad_ai.api:app \
  --host 0.0.0.0 --port 8000 --reload
```

- `--host 0.0.0.0` is what lets a phone on the same Wi-Fi reach it. The Mac's
  address moves (DHCP): it was `192.168.29.100` on the 20th and `192.168.29.44`
  on the 21st. `ipconfig getifaddr en0` gives today's. Chrome's "offline copy of
  this page" on the phone means the address changed, not that the server is down.
- `EMOTORAD_AI_APPROVAL_MODE` is read at startup, not on reload. Values `bot`,
  `reasonable`, `human`; a typo refuses to start. The owner set `bot` for
  testing on the 20th. Default in code is `reasonable`.
- `--reload` picks up Python edits; the chat HTML and the prompt are read from
  disk per request. A restart is only needed for a bind-address or environment
  change. A restart wipes conversations, pending codes, placed orders and
  rate-limit counters, all in memory.
- Tests: `../.venv/bin/python -m unittest discover -s tests -t .`, 715, offline.
  The machine's default `python3` lacks the dependencies.
- Live log tail, in a second terminal:

```bash
tail -f -n 30 logs/conversations.jsonl | python3 -c '
import sys, json
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    e, c, t = d.get("event"), (d.get("conversation_id") or "")[:8], (d.get("ts") or "")[11:19]
    if   e == "inbound":   print(t, c, " YOU  ", d.get("text","")[:90], flush=True)
    elif e == "outcome":   print(t, c, " BOT  ", (d.get("text") or "").replace("\n"," ")[:90], flush=True)
    elif e == "tool_call": print(t, c, " tool ", d.get("tool"), "ok" if d.get("ok") else "ERROR", flush=True)
    elif e in ("escalation","stuck_agent","empty_reply","guardrail_triggered"): print(t, c, " !!   ", e, flush=True)
'
```

The second column is the conversation id. Given that id, the whole session can
be reconstructed from the log: every message, tool call and result, and for a
blocked reply, the text the guardrail suppressed.

## Never do these

- **Never send the fixture phone `+919876543210` (session `sess-ananya`)
  through the live server.** With the OMS key set it is looked up for real and
  belongs to a real person. Use `9876500000` for a number with no record, or the
  owner's own number.
- Never use `EMOTORAD_AI_APPROVAL_MODE=bot` with real customers. "Sure" is
  weaker than the spec intends (see open decisions) and `bot` approves not-sure
  cases too.
- Never expose `/chat` beyond the LAN. No auth, real OMS data, and
  `/dev/verification/{id}` hands any conversation's code to anyone while
  `EMOTORAD_AI_DEV_CODES=1` is set.

## What landed on 2026-09-20, in the order it happened

Each has commits with full reasoning; `git log --format=%B` on the branch reads
as a narrative. The short version:

**Identity on `/chat`.** `Runtime(self_service_identity=True)` extends the tool
slice with the verification tools at the surface, so `battery_support.TOOL_NAMES`
stays right for channels that resolve identity upstream.
`apply_verified_identity` carries the proved phone onto the identity.
`ToolContext.late` resolves facts at call time, because the model verifies and
looks up in the same turn and a snapshot taken before the first tool is stale
by the second. The prompt named tools that did not exist (`send_otp`,
`verify_otp`); fixed.

**Hardening.** `CHAT_AGENTS` allowlist (an unknown agent name used to brick a
conversation with HTTP 500 forever). Redaction at the log sink
(`observability.redact_fields`), not at call sites. OTP codes expire (10 min)
and verified sessions expire (12 h), on a monotonic clock. History is windowed
to 12 customer turns, cut only at a customer turn; a tool result is also a
`user` entry and cutting there strands a `tool_result`. Rate limit, 20 per
caller per minute, sliding. The media catalogue is registered on `/chat` (it
was in `TOOL_NAMES` and never registered, so no photo could be sent).

**Mobile.** The page was a 390x844 desktop mockup frame; it now fills the phone
with `visualViewport` sizing. Photos open full screen. The composer is never
destroyed (that closed the keyboard on every reply). Markdown renders, escaped
first; links deliberately do not. Attach opens a camera-or-gallery sheet with a
pending thumbnail and remove button. Photos ride inline to Claude as vision
content, downscaled in the browser, stored nowhere, never logged.

**Three conversational bugs found on the phone, all fixed in code.** The agent
sent only the last block of what the model wrote (the customer got "The picture
should be just above this message" and nothing else); everything said in a turn
is now kept. The coverage post-check saw only the current turn's tool results,
so a correct "you are covered" three turns after the lookup was blocked;
coverage is remembered on the conversation. The same check read "chargeable
even within warranty" as a denial of cover; it now recognises the two-question
answer (in the term, and no physical damage), and got stricter on the way.

**Policy from the owner, in the records.** A melted terminal is a defect, never
impact damage, whatever caused the heat; in warranty means a replacement placed
from the conversation, free. `knowledge/battery/melted-terminal-or-connector.yaml`
and `warranty-replacement.yaml` say so, and the old "nothing in this
conversation ships a part" step is gone.

**Replacement fulfilment, first build.** Spec
`docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md`, plan
`docs/superpowers/plans/2026-09-20-replacement-fulfilment-first-build.md`.
`place_replacement_order` is the one write tool: the model names the part and
passes back the address it confirmed; code decides technician-or-not from
`knowledge/_replacement/parts.yaml`, resolves a mock item code, checks for an
order in flight (48 h, frame plus part, across conversations), evaluates "sure"
from four runtime facts, and consults `Settings.approval_mode`. Ships `battery`
and `charger` only in this build. Chargeable is refused with a handover; a
missing item code places `pending_approval` in every mode. A post-check blocks
any reply naming an `RO-` order no tool returned, remembered across turns.

**Address, display photo, log readability.** Plan
`docs/superpowers/plans/2026-09-20-address-display-photo-log-fixes.md`. The
address backstop is per word (every word of the confirmed address must have
been typed by the customer or be on the record) and requires a six-digit
pincode, because the live model dropped the pincode to get past the old check.
A spent one-time code is subtracted from provenance, because on `/chat` the
customer types it and it looks like a pincode. The prompt collects pincode
first when the record is blank and reads the address back once. A display
photo is asked for on any error code, decoupled from `lookup_error_code`.
Error envelopes' `code` is readable in the log again.

**Org repo.** `emotorad-tech/claude` PR #4 adds `ai-reviewer`, the sixth
per-repo reviewer agent, for this repo. Open for Sachin. `bootstrap/setup.sh`
in that repo was modified and uncommitted on `main` before the session started;
not ours, left alone.

## Open bugs

The full list with evidence lives in the QC doc
(https://claude.ai/code/artifact/b3d1b6f9-460c-4d10-a3d7-6e43cc5def60);
twelve remain there, none high severity. The ones a fresh session is most
likely to hit:

- **`lookup_error_code` is not in the chat slice**, so the error-code table is
  dead on `/chat`, and there is no table for the X1 C anyway. Retrieval on
  "E-06" alone found the right record, which is why the flow worked. Wiring it
  needs per-conversation bike context that the module-level registry in
  `api.py` cannot supply.
- **Page reload loses the conversation** (id lives in JS memory). On a phone,
  backgrounding the browser can do this mid-OTP.
- **Enter sends mid-IME-composition** (no `isComposing` check), which hits
  Hindi, Marathi and Tamil input directly.
- `em_aid` regenerates per message when localStorage is blocked; re-requesting
  a code un-verifies; the local greeting and the model's first reply both say
  "I'm an AI"; `ConversationStore` never evicts; `submit_warranty_proof` never
  validates frame ownership (pre-existing).

Follow-ups the reviewers left, recorded in the plans and in `is_sure`'s docstring:

- The spec's fourth "sure" fact (the knowledge flow reached its concluding
  step) is **not implemented**. Today, a photo of anything plus an in-warranty
  record plus a known part is "sure". In `reasonable` mode that approves a
  battery. Build it or launch on `human`.
- `evidence_seen` is per conversation, not per bike.
- Address provenance is a token set: words from unrelated customer messages
  count, and the record's tokens can be mixed into a new address. The read-back
  plus "yes" is the mitigation. Scope it to address-bearing messages.
- The address check reads the raw record's `full_address`; `_coverage()` reads
  the same field for `delivery_address`. When the real OMS returns a structured
  address, change both together, in one helper, and fold a separate pincode
  field into the line or the record's own address will fail `pincode_required`.
- `_model_name` splits on the first hyphen, so `T-Rex Air` resolves to `T`.
  Not in the item-code table yet. Take `product_id` instead of parsing.
- In-memory stores everywhere (`IdempotencyStore`, `ReplacementOrders`,
  `VerificationStore`, `ConversationStore`); `placed_at` is `time.monotonic`.
  All change before any real write.
- The playground loop still drops the model's narration beside a tool call.

## Open decisions for the owner

1. **Undocumented error codes.** The results table says `unknown_code` hands
   over. On the 20th, retrieval found the right record from "E-06" with no table
   at all. Should an undocumented code continue the symptom flow instead? Not
   changed; it is policy.
2. **Home addresses in the log.** Phones and codes are redacted; `confirmed_address`
   is logged in full. The org rule says no customer personal details in logs.
   Redacting it costs seeing the address while testing.
3. **Approval mode for production.** `reasonable` was recommended for launch;
   `bot` is set for testing. Neither is right until the fourth "sure" fact
   exists, or the answer is `human`.
4. **Location sharing.** Pincode-first is built. Location sharing was agreed to
   wait for the WhatsApp channel, where it is native.
5. **Video.** The playground samples video into stills (needs `ffmpeg`, missing
   here); `/chat` accepts images only. Agreed as its own design later; the
   recommendation is sampling frames in the browser so no clip reaches the server.
6. **Auth for `/chat`.** Accepted as LAN-only internal testing for now.
7. **Fulfilment builds 2 to 4**: chargeable with Razorpay through the outbox,
   the technician and dealer branch, then real ERP reads and OMS writes with
   8848 for the ERP.

## Things that will mislead you

- **Order `RO-00001` is in flight for frame `E10624095`** (the owner's bike)
  from the 17:21 conversation, in memory, until the server restarts or 48 hours
  pass. Re-running the flow on that number returns `already_placed: true` and
  that id. That is the duplicate check working.
- The owner's OMS row has **no `full_address`**. That is why the bot asks for
  the address; it is not a mapping bug.
- Two test numbers behave differently with the OMS key set: `9876500000` has
  no record and exercises the registration path; the owner's number has a real
  record with a null purchase date, so coverage is computed from the
  registration date and labelled provisional.
- `logs/conversations.jsonl` is gitignored and untracked. Six-digit messages
  log as `[6 digits]`; `[code]` no longer appears anywhere.
- `docs/superpowers/` holds the spec and the two plans. `.superpowers/` and
  `.claude/worktrees/` are ignored scratch.

## Working agreement with this owner

Unchanged from the previous handoff, and confirmed repeatedly on the 20th:

- They own the prompt, the knowledge records and every policy in them. Ask;
  never invent a rule. When they state a policy in chat, write it into the
  record that governs the flow, with the date.
- They dislike fixes in prompt wording for things code should decide, and they
  are right: the model ignored a "wait a turn" instruction within the hour.
  Guardrails in code.
- Verify before asserting. Several confident claims on the 20th were wrong until
  checked: "the natural conversation will not hit this", "several call sites",
  "the suite is clean". Say what command ran and what it printed.
- Review front-end changes on a phone. Four days of reading the code missed
  three defects that two minutes on a handset found.
- British English, plain short sentences, no em dashes in anything we write.
  The prompt file has its own house style; leave it.
- Never push without an explicit yes in the session.
