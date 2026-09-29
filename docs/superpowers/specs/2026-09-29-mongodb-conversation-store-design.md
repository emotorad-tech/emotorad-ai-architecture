# Conversations in MongoDB

**Status:** Approved design (carried over from the approved DynamoDB design of 2026-09-28, with the user's two changes)

**Date:** 2026-09-29

**Scope:** `emotorad-ai-architecture`, branch `feat/conversation-store`.

**Objective:** Store every conversation, tied to the person it belongs to, so that:

1. **Nothing is lost.** Conversations survive a restart, and the service can run as several copies.
2. **The bot remembers.** A returning customer's earlier conversations inform the next one.
3. **People who pick up an escalation get the whole thread.** The transcript is attached to every support ticket.

## 1. Decisions

1. **One database, `emotorad_ai`**, on the existing Atlas cluster `emotorad.iuxrx.mongodb.net`.
   - The app connects as a user holding `readWrite` on `emotorad_ai` only.
   - The URI comes from `EMOTORAD_MONGO_URI`. It is never in `Settings`, never logged, and never printed.
2. **Retention.** The first two collections are the conversation record, kept until someone deletes them. The last two are not the record, so they expire:

   | Collection | Retention |
   |---|---|
   | `transcript_turns` | **Permanent** (user decision, 2026-09-28) |
   | `conversation_summaries` | **Permanent** |
   | `conversations` (working state) | 48 hours after the last message |
   | `idempotency_keys` | 7 days |

3. **Deletion on request.** `delete_person(user_key)` removes a person's working state, transcript turns and summaries. `scripts/delete_person.py` does a dry run first, then deletes with `--yes`. This serves the right to erasure under DPDP (India) and GDPR (Spain), which permanent retention makes necessary.
4. **The user key** is `PHONE#<verified phone>` for customers and `DEALER#<dealer_id>` for dealers. There is none for an anonymous or merely asserted identity, which gets no memory.
5. **Concurrency is optimistic**, based on a `version` field:
   - a first save uses `insert_one`, where a duplicate `_id` means a conflict;
   - later saves use `update_one({_id, version: loaded})`, where zero matches means a conflict.

   The runtime retries a conflict once from fresh state, then answers "busy".
6. **Idempotency is claim-before-execute.** A claim is an `insert_one` of a pending receipt, and a duplicate `_id` means the key is already claimed. This works across servers.
7. **Memory is built in code, never by a model.** Titles come from knowledge-record titles or fixed labels, never from chat text.
8. **In memory stays the default.** `EMOTORAD_STORE=mongodb` (or the CLI's `--store mongodb`) switches it on.
9. **A Claude session never writes to the cluster.** The org rule forbids it. People run `scripts/mongo_setup.py`, `scripts/mongo_smoke.py` and `scripts/delete_person.py`. Automated tests run on `mongomock` only.

## 2. Collections and indexes

`ensure_indexes` creates these and is idempotent. `scripts/mongo_setup.py` calls it.

| Collection | `_id` | Fields | Indexes |
|---|---|---|---|
| `conversations` | conversation id | `state` (JSON string of `ConversationState`), `version`, `user_key`, `updated_at`, `expires_at` (Date) | `expires_at_ttl` (TTL, expireAfterSeconds 0), `user_key` |
| `transcript_turns` | `<conversation id>#<n, 5 digits>` | `conversation_id`, `user_key`, `n`, `role`, `text` (redacted), `at`, `attachments`, `handled_by`, `path` | `conversation_turn` (unique, conversation_id + n), `user_key`. **No TTL** |
| `conversation_summaries` | conversation id | the `ConversationSummaryItem` fields | `user_recent` (user_key, started_at descending). **No TTL** |
| `idempotency_keys` | scoped key | `status` (`pending`/`done`), `envelope` (JSON string), `expires_at` (Date) | `expires_at_ttl` (TTL, expireAfterSeconds 0) |

State and envelopes are stored as JSON strings. They are loaded whole and never queried inside, and a string sidesteps BSON's rules about field names beginning with `$` or containing `.`, which tool inputs can contain.

A working state larger than 4 MB is trimmed before saving, by whole turns and oldest first, and the trim is logged as `history_trimmed`. MongoDB's document limit is 16 MB.

## 3. Errors

- Any `PyMongoError`, including a server selection timeout, which is set to 3 seconds, becomes `StoreUnavailable`. The runtime answers with the handover message, and never continues on an empty state.
- A failure to write the transcript after a successful save is logged as `transcript_write_failed`, and the customer still gets the reply.
- A read failure while fetching memory is logged as `memory_unavailable`, and the chat continues without memory.

## 4. The runtime

The runtime handles each turn in this order:

1. Load the state.
2. Run the graph.
3. Save the state, with one retry on a conflict and "busy" after a second conflict.
4. Record the turn: two transcript turns and a summary upsert.
5. Attach the transcript to any ticket raised.

On the first turn, a verified person's three most recent summaries become the "Last contact" lines in the context.

## 5. Out of scope

- A chat-history screen for customers.
- Persisting the identity graph and OTP state.
- Zoho itself (`attach_transcript` is the seam it will implement).
- Network setup for deployment (VPC peering or PrivateLink).
- AWS IAM authentication for the service user.

## 6. Rollback

- Set `EMOTORAD_STORE=memory` to return to today's behaviour.
- The `emotorad_ai` database can be dropped by the cluster owner. Nothing else reads it.
