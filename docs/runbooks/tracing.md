# Runbook: tracing with Langfuse

Every turn the runtime handles becomes one Langfuse trace, grouped into a session by
conversation id. Langfuse prices each model call from its token usage, so the UI shows
cost per turn and per conversation without any code of ours. Code:
`src/emotorad_ai/tracing.py`, a sink behind `observability.EventLog`.

The build plan (§6, "Evals and tracing") chose Langfuse in R0. This is that, on the
Cloud free tier first; self-hosting is a one-variable change (§5).

## 1. What a trace holds

| Langfuse | From | Notes |
|---|---|---|
| Trace `customer-turn` / `dealer-turn` | `inbound` | input = the customer's message, redacted. `session_id` = conversation id. `user_id` = identity cluster, never the phone. Tags = persona, channel |
| `agent` observation named after the sub-agent | `routed` | `battery_support`, `motor_support`, `late_warranty`, `dealer_orders` |
| `generation` `video-summary` | `video_summary` / `video_summary_failed` | The Gemini call that described a customer's clip: model, tokens, the fixed prompt and the clip's key, MIME and size as input (never the bytes), the description as output. Attached to the turn the clip arrived on. Logged on every environment by the owner's decision of 2026-09-22 |
| `generation` | `llm_request` → `llm_turn` | model id, `input`/`output`/`cache_read_input_tokens`/`cache_creation_input_tokens`, stop reason, iteration. This is what gets priced |
| `tool` / `retriever` | `tool_request` → `tool_call` | arguments and result, redacted; error level on an error envelope. Knowledge searches are retrievers |
| `guardrail` | `guardrail_triggered` | `battery_safety`, `human_handoff`, coverage and order post-checks |
| `event` `escalation` | `escalation` | reason and ticket id |
| Trace output and metadata | `outcome` | the reply, `handled_by`, `escalated`, `ticket_id`, `duration_ms` (the whole handler time) |

Not traced: the playground. It bypasses `runtime.handle()` and has its own loop.

What is deliberately **not** sent: the Claude system prompt and the message history the model
saw. They carry the customer's warranty facts and every earlier message. The JSONL log has
the same policy. If a debugging need arises, that is a decision to take with Sachin, not a
flag to flip.

## 2. Keys

One Langfuse project per environment (`emotorad-ai-stage`, later `emotorad-ai-prod`), in
the EU region. Project → Settings → API Keys gives a public and a secret key.

- **Staging**: add `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to the config-store
  secret (`docs/runbooks/config-store.md` §2) and redeploy. The workflow already passes
  `LANGFUSE_HOST` and `EMOTORAD_AI_ENV=stage`.
- **Locally**: type them at the prompt like the other keys, never in a file:

```bash
read -rs "LANGFUSE_PUBLIC_KEY?Langfuse public key: " && export LANGFUSE_PUBLIC_KEY
read -rs "LANGFUSE_SECRET_KEY?Langfuse secret key: " && export LANGFUSE_SECRET_KEY
export EMOTORAD_AI_ENV=local
```

Without both keys the sink is not built and nothing changes. `/health` says
`"tracing":"on"` or `"off"`.

## 3. Check it works

Send one message through `/message` (or the chat page), then open the project in
Langfuse. Expected within a few seconds: one trace named `customer-turn`, a generation
under the sub-agent with a non-zero cost, and the session view grouping the conversation.

If the trace is there but cost is blank, the model id did not match a Langfuse price
definition. Project → Settings → Models lists them; add the id the generation shows.

If nothing arrives: the container log has one line `tracing sink failed: <ErrorType>` per
failed send. A wrong key is `UnauthorizedError`; a wrong host is a connection error.

## 4. What to look at

- **Traces**: one per turn. Sort by cost or latency to find the expensive turns.
- **Sessions**: one per conversation. Cost per *resolved* conversation is this, filtered
  on `escalated = false` in the outcome metadata.
- **Dashboard**: filter on the `channel` tag to compare surfaces, on the agent name to
  compare sub-agents.
- The HLD's health signals (escalation rate, tokens per conversation p95) are all
  derivable here; `metrics.py` over the JSONL stays the offline source of truth.

## 5. Self-host later

Set `LANGFUSE_HOST` on the deploy line to the self-hosted URL and issue keys there.
Nothing else changes. Langfuse v3 self-hosting needs Postgres, ClickHouse, Redis and S3,
which is why staging starts on the Cloud free tier (50k observations a month; a turn is
roughly 3 to 8 observations).

## 6. Delete a customer's traces on request

Traces carry the cluster id as `user_id`. In Langfuse: Users → the cluster id → delete,
or via the API with the cluster id. The text on the traces is already redacted; this is
for an explicit erasure request, alongside the S3 deletion in `media.md` §6.
