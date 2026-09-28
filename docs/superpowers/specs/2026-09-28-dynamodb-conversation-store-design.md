# Conversations in DynamoDB

**Status:** Approved design, awaiting spec review

**Date:** 2026-09-28

**Scope:** `emotorad-ai-architecture`. Builds on `feat/jev-routing` (the runtime is a LangGraph graph).

**Objective:** Store every conversation, tied to the person it belongs to, so that:

1. **Nothing is lost.** Conversations survive a restart, and the service can run as several copies behind a load balancer.
2. **The bot remembers.** A returning customer's earlier conversations inform the next one.
3. **People who pick up an escalation get the whole thread.** The transcript is attached to every support ticket (Zoho, when it is integrated).

A customer-facing chat history in the app is **out of scope**.

## 1. Decisions

1. **One DynamoDB table**, `emotorad-ai-conversations`, on-demand billing. It holds four item types, each with its own time to live (TTL):

   | Item type | Deleted after |
   |---|---|
   | Working state | 48 hours after the last message |
   | Transcript turns | 90 days |
   | Per-user summaries | 90 days |
   | Idempotency keys | 7 days |

2. **The user key is the verified phone** (`+91XXXXXXXXXX`) for customers and the `dealer_id` for dealers. It is never `cluster_id`, which comes from an identity graph that is itself in memory and changes on every restart.
   - An anonymous or merely asserted identity gets working state only. It gets no summary and no memory.
   - This matches the existing disclosure rule: a cookie never unlocks personal facts.
3. **Working state and transcript are separate things.**
   - Working state is everything the model needs to continue a live chat, including tool results. It lives for 48 hours.
   - The transcript is only what the customer and the bot said, with `redact_pii` applied. It is what memory and human agents read.
4. **Concurrency is optimistic.** Each save carries the version it loaded. A conflicting save is refused, the turn is retried once from fresh state, and a second conflict answers "busy". A conversation is never silently overwritten.
5. **Idempotency moves to the same table,** using claim-before-execute. A retried or duplicated write (a Zoho ticket, a booking) executes once, even across servers.
6. **Memory is summarised in code, never by a model.** This follows the rule stated in `enrichment.py`: selection and formatting, no LLM.
7. **In memory stays the default.** `EMOTORAD_STORE=dynamodb` switches it on. Tests, the CLI and offline mode behave exactly as today.
8. **The app never creates or deletes the table.** Infrastructure code owns it. The app's IAM role may only get, put, update, query and conditionally write items on this one table.

## 2. The table

The table has a partition key `PK` (string), a sort key `SK` (string), and TTL on the attribute `expires_at` (epoch seconds). Point-in-time recovery is on, and encryption uses the AWS-owned KMS key.

| Item | PK | SK | Attributes | TTL |
|---|---|---|---|---|
| Working state | `CONV#<conversation_id>` | `STATE` | `state` (JSON string of `ConversationState`), `version` (number), `user_key`, `updated_at` | now + 48 h, refreshed every turn |
| Transcript turn | `CONV#<conversation_id>` | `TURN#<n, 5 digits>` | `role` (`customer` or `bot`), `text` (redacted), `attachments` (kind + url), `handled_by`, `path`, `at` | now + 90 days |
| Conversation summary | `USER#<user_key>` | `CONV#<started_at ISO>#<conversation_id>` | `conversation_id`, `channel`, `frame_number`, `product_name`, `agent`, `sub_category`, `title`, `outcome` (`open`, `resolved` or `escalated`), `ticket_id`, `started_at`, `last_at`, `turns` | now + 90 days, refreshed every turn |
| Idempotency key | `IDEM#<scoped key>` | `IDEM` | `status` (`pending` or `done`), `envelope` (JSON string), `claimed_at` | now + 7 days |

**Why these choices:**

- **`state` is stored as one JSON string.** DynamoDB's own maps turn every number into a `Decimal` and limit nesting depth. The state is loaded whole and saved whole, never queried inside.
- **Summaries sort by `started_at`.** A `Query` on `USER#<key>` with `ScanIndexForward=False, Limit=3` returns the newest conversations first.
- **Items must stay under 400 KB.** A working state whose JSON passes 350 KB has its oldest whole turns trimmed from `history` before saving. A tool_use is never separated from its tool_result, and the trim is logged as `history_trimmed`. The transcript turns are unaffected.

## 3. Components

### 3.1 The store interface: `src/emotorad_ai/conversation.py`

Here `ConversationStore` becomes the interface. The current class becomes `InMemoryConversationStore`, and `ConversationStore` stays importable as an alias for it, so existing imports keep working.

```
get(conversation_id) -> ConversationState                 # loads, or creates a fresh one
save(state) -> None                                       # raises ConversationConflict if the stored version moved on
record_turn(state, inbound: InboundMessage, reply: Reply) -> None   # transcript turns + summary upsert
transcript(conversation_id) -> List[TranscriptTurn]
recent_summaries(user_key, limit=3, exclude=None) -> List[ConversationSummaryItem]
history(conversation_id)                                  # unchanged
```

`ConversationState` gains four fields:

- `version: int = 0`
- `user_key: Optional[str] = None`
- `started_at: Optional[str] = None`
- `channel: Optional[str] = None`

It also gains `to_json()` and `from_json()`. They are lossless for every field, and `history` round-trips exactly.

### 3.2 The DynamoDB implementation: `src/emotorad_ai/stores/dynamo.py`

`DynamoConversationStore(table_name, client=None, clock=...)` implements the interface above. It uses boto3's low-level `dynamodb` client, created lazily, with the region from `Settings.aws_region` and an optional `EMOTORAD_DYNAMO_ENDPOINT` for DynamoDB Local.

**`save`** is a single `PutItem` with a condition:

- For a new conversation: `attribute_not_exists(PK)`.
- Otherwise: `version = :loaded`.
- The new `version` is the loaded one plus one.

A `ConditionalCheckFailedException` becomes `ConversationConflict`.

**`record_turn`** runs once per turn, after a successful save:

- two `PutItem` calls, one for the customer turn and one for the bot turn;
- one `PutItem` for the summary, if `state.user_key` is set.

Transcript failures are logged as `transcript_write_failed` and re-raised. They are never swallowed.

**Read errors** (throttling, network) become `StoreUnavailable`. The runtime answers with the handover message and logs `store_unavailable`, rather than carrying on with an empty state that would lose the customer's conversation.

### 3.3 Durable idempotency: `src/emotorad_ai/stores/dynamo.py`

`DynamoIdempotencyStore(table_name, client)` implements the same interface as the registry's `IdempotencyStore`, and adds one method:

```
claim(key) -> Optional[Envelope]
```

It is a conditional `PutItem` of `status=pending` with `attribute_not_exists(PK)`.

- The claim succeeds: it returns `None` and the caller executes the tool.
- The item exists with `done`: it returns the stored envelope.
- The item exists with `pending`: it returns `err("write_in_progress", ..., retryable=True)`.

`put(key, envelope)` sets `status=done` and the envelope.

`ToolRegistry.call` changes to claim before executing when the store has a `claim` method, and to `put` afterwards. When the tool fails, the claim is deleted so a retry can run. The in-memory store gains a matching `claim`, so the behaviour is identical and tested once.

### 3.4 Memory: `runtime._node_prepare`

On the first turn of a conversation, when the context block is built:

1. The runtime sets `state.user_key`:
   - customer + verified: the normalised phone;
   - dealer: `dealer_id`;
   - otherwise `None`.
2. If there is a `user_key`, it calls `recent_summaries(user_key, limit=3, exclude=conversation_id)`.
3. `summarise_past(summaries) -> Optional[str]` (new, in `enrichment.py`) renders at most three lines, newest first, one line per conversation:

   ```
   20 Sep: Battery will not charge on the EMX Plus (…1234), escalated, ticket EM-00012
   02 Sep: Motor is making a noise on the EMX Plus (…1234), resolved
   ```

4. The result is passed as `last_conversation` to `ContextEnricher.build`, which already puts it only in a verified person's context and already applies the token budget.

The title comes from code, never from the chat, so nothing a customer typed is replayed into a future prompt. It is chosen in this order:

1. the knowledge record's title, when the conversation had one;
2. otherwise a fixed label for the agent that handled it: "Battery issue", "Motor issue", "Warranty registration" or "Dealer order";
3. otherwise "General question".

### 3.5 The transcript on tickets: `runtime`

After every turn that produced a `ticket_id`, whether from an agent or the safety branch, the runtime calls:

```
registry.tickets.attach_transcript(ticket_id, render_transcript(state))
```

This happens after `save` and `record_turn`, so the thread includes the current turn. `render_transcript` is plain text: `[09:41] Customer: …` and `[09:41] Bot: …`, redacted.

`MockTicketSystem` gains `attach_transcript`, which stores the transcript on the ticket. The Zoho integration later implements the same method as a ticket thread or comment.

### 3.6 The turn: `Runtime.handle`

```
for attempt in (1, 2):
    state = store.get(id)                      # fresh each attempt
    reply = graph.invoke(message, state)
    try:
        store.save(state); break
    except ConversationConflict:
        if attempt == 2: return BUSY_MESSAGE reply (escalated=False, logged "conversation_busy")
store.record_turn(state, message, reply)
attach transcript if reply.ticket_id
```

- `BUSY_MESSAGE` reads: "Sorry, I'm still working on your last message. Please send that again in a moment." It passes through the disclosure like every other reply. It is not a handover, because nothing has gone wrong that a person needs to fix.
- The `prepare` node receives the loaded state instead of calling `self.conversations.get`.
- A retried turn may repeat model calls. Writes are safe, because claim-before-execute returns the first result.
- Log events from the losing attempt stay in the log, which records what actually happened.

### 3.7 Settings

| Env var | Default | Purpose |
|---|---|---|
| `EMOTORAD_STORE` | `memory` | `memory` or `dynamodb` |
| `EMOTORAD_DYNAMO_TABLE` | `emotorad-ai-conversations` | Table name |
| `EMOTORAD_DYNAMO_ENDPOINT` | none | DynamoDB Local, for development only |
| `EMOTORAD_STATE_TTL_HOURS` | `48` | Working state |
| `EMOTORAD_TRANSCRIPT_TTL_DAYS` | `90` | Transcript turns and summaries |
| `EMOTORAD_IDEMPOTENCY_TTL_DAYS` | `7` | Idempotency keys |

`wiring.build_stores(settings) -> Stores(conversations, idempotency)` builds both. The CLI and the API pass them to `Runtime` and to `build_registry`. `dynamodb` mode needs AWS credentials from the environment or the task role. There is no key in code or config.

### 3.8 Dependencies

- `boto3>=1.34` in `requirements.txt`.
- `moto[dynamodb]>=5.0` in `requirements-dev.txt`, for tests.

## 4. Testing

Everything runs offline, against `moto`'s in-process DynamoDB.

- **Round trip.** `ConversationState.to_json`/`from_json` preserves every field, including a history with tool_use and tool_result blocks and non-ASCII text.
- **Store contract.** One test class runs against both `InMemoryConversationStore` and `DynamoConversationStore`:
  - get-or-create, save, reload;
  - version increment;
  - `ConversationConflict` on a stale save;
  - `record_turn` writes two turns and one summary;
  - `transcript` comes back in order and redacted;
  - `recent_summaries` is newest first, respects `limit` and `exclude`, and returns nothing for an unknown user;
  - no summary is written when there is no `user_key`.
- **TTL.** `expires_at` is set to the configured windows on every item type.
- **Size guard.** A 400 KB history is trimmed below 350 KB, turn pairs stay intact, and `history_trimmed` is logged.
- **Idempotency.**
  - A claim is granted once.
  - A second claim returns `write_in_progress` while pending, and the first envelope once done.
  - A failed tool releases its claim.
  - Two registries on the same table create exactly one ticket.
  - The in-memory store behaves the same way.
- **Runtime.**
  - A conversation continues after the `Runtime` is rebuilt on the same table, which simulates a restart.
  - Two runtimes on one table, which simulates two servers, do not corrupt each other: the conflict is retried, and a second conflict answers busy.
  - A verified customer's second conversation carries "Last contact" lines; an anonymous one never does.
  - A dealer's memory is keyed by `dealer_id` and never shows customer conversations.
  - A ticket, including a safety ticket, has the transcript attached, with the current turn in it.
  - A read failure gives the handover message, not an empty state.
- **Parity.** With `EMOTORAD_STORE=memory` the whole existing suite passes unchanged.
- **Command:** `python -m unittest discover -s tests -t .`

## 5. Out of scope

- A chat list or API for customers to browse past conversations.
- Persisting the identity graph (`cluster_id`) and OTP verification.
- The Zoho integration itself. `attach_transcript` is the seam it will implement.
- Moving the JSONL event log to DynamoDB. It goes to CloudWatch in deployment.

## 6. Rollback

- Set `EMOTORAD_STORE=memory` to return to today's behaviour. Nothing reads the table any more, and its items expire on their own.
- For a full revert, revert the merge commit. There is no migration, and nothing outside this service reads the table.

## 7. Risks

| Risk | Control |
|---|---|
| Personal data at rest: names, bikes, frame numbers, typed text | The transcript is redacted and tool results live only 48 hours. The TTL windows are stated and configurable. Encryption at rest, a narrow IAM role, and no read path outside the service |
| Memory replays something a customer typed into a future prompt (injection) | Summaries hold record titles, outcomes and ticket ids from code, never chat text |
| Hot conversation under concurrent writes | Optimistic versioning, one retry, then an explicit busy reply |
| DynamoDB throttling or an outage mid-turn | A typed `StoreUnavailable` leads to a handover message. Nothing continues on empty state |
| Items larger than 400 KB | Trimming at 350 KB, logged |
