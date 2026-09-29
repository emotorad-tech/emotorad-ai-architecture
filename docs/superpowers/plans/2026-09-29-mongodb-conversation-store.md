# Conversations in MongoDB: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Each task runs red, then green, then the full suite, then a commit.

**Goal:** The MongoDB conversation store, wired into the runtime, covering persistence, memory, transcripts on tickets and deletion on request. Scripts let a person set up and smoke-test the real cluster.

**Architecture:** Two stores implement the interface already on this branch, `conversation.py` and `tools/registry.py`:

- `MongoConversationStore` and `MongoIdempotencyStore`, in `stores/mongo.py`.
- The runtime loads, saves and records each turn through whichever store `EMOTORAD_STORE` selects.

**Spec:** `docs/superpowers/specs/2026-09-29-mongodb-conversation-store-design.md`

**Deviation from the writing-plans format:** the user asked for immediate execution after the DynamoDB plan had already been reviewed. This plan therefore lists exact interfaces and test names, and the code lives in each task's commit. The runtime logic in Tasks 3 to 5 is the same as Tasks 5 to 7 of the reviewed DynamoDB plan (commit dd32df2).

## Global constraints

- **Test command.** `python -m unittest discover -s tests -t .`. The baseline is 613 tests, with 2 known `test_video` errors and 2 skips.
- **Tests never touch the Atlas cluster.** Every MongoDB test uses a `mongomock.MongoClient()`.
- **`EMOTORAD_MONGO_URI` is read only by `stores/mongo.connect`,** and never logged or printed.
- **Collection names and index names are exactly as in spec §2.** There is no TTL index on `transcript_turns` or `conversation_summaries`.
- **Dependencies:** `pymongo>=4.6` in `requirements.txt`, and `mongomock>=4.1` in `requirements-dev.txt`.
- **Commits:** commit per task on `feat/conversation-store`, and never push.

## Review focus

1. **A retry after a conflict starts from fresh state.**
2. **Trimming never separates a tool_use from its tool_result.**
3. **A failed write releases its claim.**
4. **Memory never contains customer text.**
5. **An unreachable cluster gives a handover,** not an empty state or a crash.

## Tasks

1. **Settings, dependencies and `delete_person`.**
   - Settings gain `store` (`memory`|`mongodb`) with `STORES`, `mongo_db`, `state_ttl_hours` and `idempotency_ttl_days`.
   - Add `InMemoryConversationStore.delete_person(user_key) -> Dict[str, int]`.
   - Tests: settings defaults and validation; the contract gains `test_delete_person_removes_everything_for_one_person_only`.
2. **`stores/mongo.py`.**
   - Adds `connect`, `ensure_indexes`, `INDEXES`, `MongoConversationStore` and `MongoIdempotencyStore`.
   - Tests in `tests/test_mongo_store.py`:
     - the full `StoreContract`;
     - a stale save conflicts;
     - a duplicate first save conflicts;
     - TTL and index definitions per collection, with no TTL on transcripts or summaries;
     - `ensure_indexes` is idempotent;
     - the size guard trims whole turns;
     - `ServerSelectionTimeoutError` becomes `StoreUnavailable`;
     - a missing URI becomes `StoreUnavailable`.
   - Tests in `tests/test_idempotency_claims.py`: `MongoClaimTests(ClaimContract)`, and two registries on one database raise one ticket.
3. **The runtime loads, saves and records.**
   - Adds `Runtime(conversations=)`, `BUSY_MESSAGE`, `AGENT_TITLES`, `_user_key`, `_summary_for` and `_store_down`.
   - Tests in `tests/test_runtime_persistence.py`:
     - restart continuity on mongomock;
     - summaries only for verified people;
     - one conflict is retried from fresh state;
     - two conflicts answer busy;
     - an outage hands over.
4. **Memory.**
   - Adds `enrichment.summarise_past`, and last contact on the first turn.
   - Tests in `tests/test_memory.py`.
5. **The transcript on tickets.**
   - Adds `render_transcript`, `MockTicketSystem.attach_transcript`, and attaching after `record_turn`.
   - Tests in `tests/test_ticket_transcript.py`, covering an agent ticket and a safety ticket.
6. **Wiring and scripts.**
   - Adds `wiring.build_stores`, the CLI `--store`, and the API wiring.
   - Adds `scripts/mongo_setup.py`, `scripts/mongo_smoke.py` and `scripts/delete_person.py`, and a `CLAUDE.md` line.
   - Tests in `tests/test_store_wiring.py`.
