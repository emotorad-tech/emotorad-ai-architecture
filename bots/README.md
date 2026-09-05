# Published bots

One YAML file per sub-agent. A file here is live: the runtime loads every
`*.yaml` in this directory at startup, alongside the four built-in agents
(`src/emotorad_ai/agents/`). Git is the audit trail and a PR is the approval —
same convention as `knowledge/`.

Drafts do not live here. The playground writes them under
`EMOTORAD_AI_BOT_DRAFTS` (default `.playground/drafts/`, gitignored) and
"Export for review" copies a finished draft and its knowledge records into
`.playground/export/`, laid out as `bots/` and `knowledge/`. Move them into
place and open a PR.

## Format

```yaml
name: brakes_support        # snake_case, unique; appears as handled_by in logs
persona: customer           # customer | dealer
topic: brakes               # knowledge topic AND triage topic; unique per persona
keywords:                   # triage; English plus the Hindi/Hinglish forms seen in traffic
  - brake
  - braking
  - ब्रेक
tools:                      # from the registry; the persona allowlist is enforced at load
  - lookup_warranty_record
  - search_knowledge
  - create_support_ticket
prompt: |
  You are the brake support assistant for EMotorad ...
```

## Rules (enforced at load — a bad file stops the service, on purpose)

- `name` and `topic` are unique across built-ins, published bots and drafts.
- Every tool exists and is allowed for the persona. Dealer bots can never reach
  `lookup_warranty_record`; the allowlists are in `src/emotorad_ai/bots.py`.
- Keywords do not overlap with any other bot of the same persona: an overlap
  makes triage refuse to choose, and neither bot is ever reached.
- A bot with `search_knowledge` needs records under `knowledge/<topic>/`.

The safety branch, the human handoff, the coverage post-check and the AI
disclosure are not in this file and cannot be changed from it.
