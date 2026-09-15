# Handoff — battery prompt migration & playground hosting

Written 2026-09-11, at the end of a long session. Read this plus `CLAUDE.md`
before picking the work up. `CLAUDE.md` is the architecture; this is *where
things currently stand and what is about to bite you*.

---

## How we work together (do not re-litigate)

- **One agent at a time, battery first.** Motor is next.
- **Kushendra owns the prompt copy. Claude owns the code.** He writes and edits
  the system prompt in the Streamlit playground and chat-tests it; when he finds
  a bug he reports it and Claude fixes the *harness*, not the wording.
- Bugs get reported by chat id (e.g. `20260909-cc16dd19`) — those are archived
  under `.playground/chats/<agent>/`.
- The guru's `bot-prompt-hardening` playbook lives at
  `.claude/skills/bot-prompt-hardening/SKILL.md` and Kushendra expects it to be
  followed on prompt work.

---

## Where things stand

Branch `playground-persistence`, **pushed** to
`github.com/emotorad-tech/emotorad-ai-architecture` (⚠️ this repo is **public**).
443 tests pass. Playground build **0.15.0**. Battery prompt **v24**.

### The battery knowledge migration is DONE

This was the session's main work: battery diagnostic flows moved out of the
system prompt into knowledge records, following the hardening playbook.

**Prompt size: 29,416 → 15,276 characters (48% smaller).**

| Was in prompt | Now a record | Scope |
|---|---|---|
| §5c standard power-on | `battery-wont-power-on` | excludes Doodle |
| §5d Doodle power-on | `battery-doodle-wont-power-on` | Doodle only |
| §5e SOC indicator dead | `battery-soc-indicator-dead` | excludes Doodle |
| §5f on/off switch dead | `battery-onoff-switch-dead` | excludes Doodle |
| §5b1 melting, both ends | `battery-melted-terminal` | every bike |

13 records total (9 battery, 4 motor) in `knowledge/`.

**Why records beat prompt text here:** a rule stated generally in one place was
being ignored by a specific procedure elsewhere — found four separate times. On
prompt v15 the evidence rule was 26% of the prompt and the procedure 74%. Every
fix that worked put the requirement *inside the step*. Records make that the
default, because a record arrives whole.

**The method, per record** (repeat this for motor):
golden queries added and passing first → verify retrieval *and* scoping → parity
check the rules → only then remove from the prompt → run the **full** suite, not
just touched tests.

### How the migration is protected

`tests/test_knowledge_migration.py` — 44 assertions across five flows. Each pins a
rule the prompt carried, by the phrase that carries it. **These exist because
retrieval passing does not mean the rule survived the rewrite**; a record can be
found correctly and still have quietly lost "ask for the second photo anyway".
Every pinned rule is one that exists because a real conversation went wrong
without it: the branch lock, revival-is-green-only, under-four-hours-is-normal,
no-dead-conclusion-without-the-video, the second melting photo.

`tests/test_retrieval_evals.py` — golden set, top-2 accuracy floor 95%, plus a
reachability check per record. This caught a real bug when `excludes` semantics
were wrong (top-2 fell to 93%).

One cross-file link worth knowing: **E-06 in `knowledge/_errors/codes.yaml`
points at the melting flow** but imports nothing from it. A test asserts that
link still resolves, because otherwise a rename would surface as a dead end for
a customer with a melted charging port.

---

## Running the playground

```bash
cd emotorad-ai-architecture
PYTHONPATH=src EMOTORAD_CLOUDINARY_CLOUD=cjdq4bv8 \
EMOTORAD_OMS_API_KEY=<key> \
  .venv/bin/python -m streamlit run src/emotorad_ai/playground.py \
  --server.port 8501 --server.address 127.0.0.1 \
  --server.baseUrlPath playground --server.headless true
```

→ http://127.0.0.1:8501/playground/  (the `baseUrlPath` matters; without it the
page 404s at that URL)

Tests: `.venv/bin/python -m unittest discover -s tests -t .`
Also asserted in-suite: **pyflakes**, after a `NameError` shipped to a live chat
that no test could catch (AppTest cannot drive a file-accepting `chat_input`).

Publish a prompt from outside the browser:
`.venv/bin/python scripts/publish_prompt.py battery_support <file.txt>`

**Two instances were left running at the end of the session** — 8501 (Kushendra's
battery testing, has the OMS key) and 8502 (a team instance with an isolated
state dir at `.playground-team/`, no OMS key). 8502 is redundant if Streamlit
Cloud hosting goes ahead — kill it.

---

## OPEN: hosting the playground for the motor team

Kushendra has a team who will write the **motor** prompt. They need a link.
Motor is already wired — it is in the agent dropdown, has its own tool slice and
four knowledge records, and `search_knowledge` works from turn one. No build
needed, only hosting.

**Decision made: host on Streamlit Community Cloud.** (I first went down a
tunnel/Tailscale path — he did not want that. Don't repeat it.)

Deploy steps, which need *his* browser at share.streamlit.io:

```
Repository   emotorad-tech/emotorad-ai-architecture
Branch       playground-persistence
Main file    src/emotorad_ai/playground.py
Secrets      EMOTORAD_CLOUDINARY_CLOUD = "cjdq4bv8"
```

`requirements.txt` is already correct and `playground.py` already puts `src/` on
`sys.path`, which is the usual reason a `streamlit run` app fails on Cloud. Heavy
deps (faster-whisper, imageio-ffmpeg) are lazily imported so boot is unaffected.

**Never put `EMOTORAD_OMS_API_KEY` in those secrets.** Without it, "Live
customer" mode fails cleanly and the team uses preset/custom riders. With it,
anyone who opens the app can pull a real customer's warranty and orders by typing
a phone number.

### ⚠️ The blocker nobody has solved yet

**Prompt versions will not survive on Streamlit Cloud.** `.playground/` is
gitignored and Cloud's disk is ephemeral — it resets when the app sleeps or
redeploys. The app starts with zero prompt history and loses every version the
team saves. For a team iterating over days this is the thing that will actually
hurt. Cheapest real fix is writing prompt versions to S3 (already on AWS).
**Offered, not yet built. Ask before assuming it's wanted.**

### Also unfixed

**Concurrent saves can silently drop a version.**
`_save_prompt_version` (`src/emotorad_ai/playground.py:539`) is a
read-modify-write with no locking. Never hit it with one writer; a team of
writers is exactly the case that triggers it. Fix = file lock + a
version-conflict message. **Offered, not yet built.**

---

## Security / data notes

- **The repo is PUBLIC.** Check anything before committing.
- **The OMS API key must be rotated.** `e6254f7b…` was pasted in plaintext in
  chat. It is absent from the tracked tree and from git history (verified), and
  is passed only via `EMOTORAD_OMS_API_KEY` at server start — but it has been
  exposed in a transcript.
- **A real customer's number was scrubbed from this branch before its first
  push.** `+916005150250`, from a live OMS lookup, was in
  `tests/test_verification.py` and one commit's history. Replaced with fixture
  `+919000000055`; history rewritten with `filter-branch` while the branch was
  still unpushed. `backup-pre-scrub` holds the original locally.
- **`/proof` is a test-evidence bypass that lives in the harness, never in a
  shipped prompt** — a test asserts `PROOF_COMMAND` appears in no prompt.
  Kushendra: *"this phrase is like a password that only i know"*. Decide whether
  the motor team gets told about it.
- **Safety gate:** `swollen`/`swelling` stay in the gate; `melted` was
  deliberately **removed** so the two-photo damage assessment can run. Fire,
  sparks, smoke, heat, leaks and cracks still stop the conversation.

---

## Live OMS findings (hard-won, don't rediscover)

- `purchase_date` is **NULL on every live row** → coverage falls back to
  `created_at` with `warranty_start_source: registration_date`.
- `9876543210`, `9850985092`, `9840442620`, `8310506596` are **real customers**.
- Amazon invoices carry Amazon identifiers (`403-8449077-3847527`) that can
  **never** match OMS.
- `uni_code` is neither searchable nor returned by the API.
- OMS ships Doodles with no version (`Doodle Black`), which is why model matching
  is on the family name substring.

---

## The playbook gap, stated plainly

Steps 3 and 16 of `bot-prompt-hardening` want the test corpus run 3–4 times to
separate a real regression from a stochastic one, and step 2 wants multi-turn
conversation tests. **We have neither.** Everything in the suite tests whether
the right content is *retrievable*, not whether the model *uses it as well* when
it arrives as a tool result instead of sitting in the system prompt. That
difference is real and currently unmeasured — live testing is the only thing
answering it. A session runner would need real API spend.

This is the single biggest known weakness in the current setup. If the motor bot
is built the same way, it inherits it.

---

## Remaining smaller items

- Upload a `multimeter_tip` video and an "all-sides battery" reference photo.
  Supply the voltage threshold that means a dead battery — the Doodle record
  currently sends people to a multimeter without one.
- 3 `cdn.emotorad.test` placeholder media URLs still 404 (one,
  `motor-no-assist`, is live-retrievable).
- Motor has no saved prompt version yet; the editor seeds from
  `motor_support._BASE_PROMPT`.
- `.playground-team/` was created this session for the 8502 instance. If
  Streamlit Cloud is used instead, delete it.
- Decide how motor prompts get promoted from wherever the team writes them back
  into the repo, *before* they have written twenty versions.
