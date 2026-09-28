# Conversations in DynamoDB: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every conversation per user in DynamoDB. The stored record gives three things:

- **Continuity:** conversations survive a restart and work across several servers.
- **Memory:** the bot remembers a customer's past chats.
- **Handover:** human agents get the transcript on every ticket.

**Architecture:**

- `ConversationStore` becomes an interface with two implementations: `InMemoryConversationStore`, the default, which behaves as today, and `DynamoConversationStore`.
- `Runtime.handle` does three things per turn:
  1. loads state before the graph runs;
  2. saves it afterwards with a version check, retrying once on a conflict;
  3. records the transcript and the per-user summary.
- Idempotency switches to claim-before-execute, with a DynamoDB implementation that uses the same table.

**Tech stack:** Python 3.12, `unittest`, `boto3`, and `moto` for an offline fake of DynamoDB in tests.

**Spec:** `docs/superpowers/specs/2026-09-28-dynamodb-conversation-store-design.md`

## Global constraints

- **Test command.** `python -m unittest discover -s tests -t .`. The baseline is 598 tests, with 2 known `tests.test_video` errors caused by the missing `imageio_ffmpeg`, and 2 skips.
- **Settings, exactly as named:**
  - `EMOTORAD_STORE` (`memory` | `dynamodb`, default `memory`)
  - `EMOTORAD_DYNAMO_TABLE` (default `emotorad-ai-conversations`)
  - `EMOTORAD_DYNAMO_ENDPOINT` (default none)
  - `EMOTORAD_STATE_TTL_HOURS` = 48
  - `EMOTORAD_TRANSCRIPT_TTL_DAYS` = 90
  - `EMOTORAD_IDEMPOTENCY_TTL_DAYS` = 7
- **Keys.**
  - `PK` and `SK` are strings.
  - TTL uses the attribute `expires_at`, in epoch seconds.
  - Item keys:
    - working state: `CONV#<id>` / `STATE`
    - transcript turn: `CONV#<id>` / `TURN#<5 digits>`
    - summary: `USER#<user_key>` / `CONV#<started_at>#<id>`
    - idempotency key: `IDEM#<scoped key>` / `IDEM`
- **The user key.**
  - A verified customer is `PHONE#<normalised phone>`.
  - A dealer is `DEALER#<dealer_id>`.
  - Anyone else has none.
- **Tests never reach AWS.** Every test that touches boto3 runs inside `moto.mock_aws()`, with fake credentials in the environment and `AWS_PROFILE` removed.
- **The app never creates or deletes the table.** Tests create the table inside moto only.
- **Offline defaults are unchanged.** With the default settings, every existing test passes unchanged.
- **Code style:** British English, LF line endings, and no em dashes in new text.
- **Commits:** commit per task on `feat/dynamodb-conversations`, and never push.

## Review focus

1. **A retry after a conflict loads fresh state.** It must not replay on top of the losing attempt's mutated state. (Task 5.)
2. **A tool_use is never separated from its tool_result** when the size guard trims history. Otherwise the next model call is rejected. (Task 3.)
3. **A write tool that raises releases its claim,** so the customer's retry can run. It must not be stuck at `write_in_progress` for seven days. (Task 4.)
4. **Memory never contains text the customer typed.** Titles come from record titles or fixed labels only. (Task 6.)
5. **A DynamoDB outage in the middle of a turn gives a handover message.** It is never an empty state and never a 500 error. (Task 5.)

---

### Task 1: Settings, dependencies, and state that serialises

**Files:**
- Modify:
  - `src/emotorad_ai/config.py`
  - `requirements.txt`
  - `requirements-dev.txt`
  - `src/emotorad_ai/conversation.py`
- Test: `tests/test_conversation_json.py`

**Interfaces:**
- Produces:
  - `Settings.store`, `.dynamo_table`, `.dynamo_endpoint`, `.state_ttl_hours`, `.transcript_ttl_days`, `.idempotency_ttl_days`
  - `STORES`
  - `ConversationState.version`, `.user_key`, `.started_at`, `.channel`, `.escalated`, `.ticket_id`
  - `ConversationState.to_json() -> str`
  - `ConversationState.from_json(raw) -> ConversationState`

- [ ] **Step 1: Write the failing tests**

`tests/test_conversation_json.py`:

```python
import json
import unittest

from emotorad_ai.config import STORES, Settings
from emotorad_ai.conversation import ConversationState

HISTORY = [
    {"role": "user", "content": "बैटरी चार्ज नहीं हो रही"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "toolu_1", "name": "search_knowledge", "input": {"query": "won't charge"}},
    ]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"data\": {}}", "is_error": False}]},
]


class StoreSettingsTests(unittest.TestCase):
    def test_store_settings_default_to_memory_and_the_spec_windows(self):
        settings = Settings()
        self.assertEqual(STORES, ("memory", "dynamodb"))
        self.assertEqual(settings.store, "memory")
        self.assertEqual(settings.dynamo_table, "emotorad-ai-conversations")
        self.assertEqual((settings.state_ttl_hours, settings.transcript_ttl_days, settings.idempotency_ttl_days), (48, 90, 7))

    def test_an_unknown_store_is_refused(self):
        with self.assertRaises(ValueError):
            Settings(store="redis")


class StateJsonTests(unittest.TestCase):
    def test_every_field_round_trips_including_history(self):
        state = ConversationState(conversation_id="c1", selected_frame="EMXP2025004417", agent="battery_support",
                                  sub_category="battery-wont-charge", turns=2, disclosed=True, evidence_seen=True,
                                  history=list(HISTORY), transitions=["greeting->routed"], version=3,
                                  user_key="PHONE#+919876543210", started_at="2026-09-28T10:00:00+00:00",
                                  channel="whatsapp", escalated=True, ticket_id="EM-00001")
        again = ConversationState.from_json(state.to_json())
        self.assertEqual(again, state)
        self.assertEqual(json.loads(state.to_json())["history"][0]["content"], "बैटरी चार्ज नहीं हो रही")

    def test_unknown_fields_from_a_newer_version_are_ignored(self):
        raw = json.dumps({"conversation_id": "c1", "a_field_from_the_future": 1})
        self.assertEqual(ConversationState.from_json(raw).conversation_id, "c1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_conversation_json -v`. Expected: ERROR, `cannot import name 'STORES'`.

- [ ] **Step 3: Implement**

In `config.py`, add `STORES = ("memory", "dynamodb")` after `MODES`. Add these fields to `Settings`, after `openrouter_zdr`:

```python
    # Where conversations live. `memory` is one process, lost on restart;
    # `dynamodb` survives restarts and scales out (spec 2026-09-28).
    store: str = os.environ.get("EMOTORAD_STORE", "memory")
    dynamo_table: str = os.environ.get("EMOTORAD_DYNAMO_TABLE", "emotorad-ai-conversations")
    dynamo_endpoint: Optional[str] = os.environ.get("EMOTORAD_DYNAMO_ENDPOINT") or None
    state_ttl_hours: int = int(os.environ.get("EMOTORAD_STATE_TTL_HOURS", "48"))
    transcript_ttl_days: int = int(os.environ.get("EMOTORAD_TRANSCRIPT_TTL_DAYS", "90"))
    idempotency_ttl_days: int = int(os.environ.get("EMOTORAD_IDEMPOTENCY_TTL_DAYS", "7"))
```

Add `from typing import Optional`. Extend `__post_init__` with:

```python
        if self.store not in STORES:
            raise ValueError("EMOTORAD_STORE must be one of %s, not %r" % (", ".join(STORES), self.store))
```

In `requirements.txt`, after the langgraph block, add:

```
# Conversation store (src/emotorad_ai/stores/dynamo.py), when EMOTORAD_STORE=dynamodb.
boto3>=1.34
```

In `requirements-dev.txt`, add:

```
# An in-process fake of DynamoDB, so the store's tests never reach AWS.
moto[dynamodb]>=5.0
```

In `conversation.py`, add `import dataclasses` and `import json`. Then add these fields to `ConversationState`, after `sub_category`:

```python
    # Persistence (EMOTORAD_STORE=dynamodb). `version` guards concurrent saves;
    # `user_key` ties the conversation to a person for memory; the last two
    # feed the per-user summary.
    version: int = 0
    user_key: Optional[str] = None
    started_at: Optional[str] = None
    channel: Optional[str] = None
    escalated: bool = False
    ticket_id: Optional[str] = None
```

Add these methods after `__post_init__`:

```python
    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "ConversationState":
        """Tolerant of fields a newer version wrote, so a rolling deploy with two
        versions running cannot make old code fail to load a conversation."""
        data = json.loads(raw)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})
```

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_conversation_json -v`, then the full suite. Expected: all pass, with only the known errors.

- [ ] **Step 5: Commit.** Message: "Add store settings and lossless conversation state serialisation".

---

### Task 2: The store interface and the in-memory store

**Files:**
- Modify: `src/emotorad_ai/conversation.py`
- Create: `tests/store_contract.py`
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Produces:
  - `TranscriptTurn(n, role, text, at, attachments=(), handled_by="", path="")`
  - `ConversationSummaryItem(conversation_id, user_key, started_at, last_at, channel="", title="", frame_number=None, product_name=None, agent=None, sub_category=None, outcome="open", ticket_id=None, turns=0)`
  - `ConversationConflict`, `StoreUnavailable`
  - `transcript_turns(state, inbound, reply, at) -> Tuple[TranscriptTurn, TranscriptTurn]`
  - `InMemoryConversationStore(clock=utc_now_iso)` with `get`, `save`, `record_turn(state, inbound, reply, summary=None)`, `transcript`, `recent_summaries(user_key, limit=3, exclude=None)`, `history`, `__len__`
  - `ConversationStore = InMemoryConversationStore`
  - `utc_now_iso()`
  - `tests/store_contract.StoreContract`, a mixin whose tests need `make_store()`

- [ ] **Step 1: Write the contract and the failing tests**

`tests/store_contract.py`:

```python
"""One set of behaviours every conversation store must have.

Mixed into a TestCase per implementation, so the in-memory store and the
DynamoDB store are held to the same contract, not two similar ones.
"""

from emotorad_ai.contract import Attachment, Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationSummaryItem


def inbound(text, cid="c1", attachments=()):
    return InboundMessage(conversation_id=cid, persona="customer", identity=Identity(), channel="whatsapp",
                          message_text=text, attachments=list(attachments))


def reply(text, cid="c1", handled_by="battery_support", ticket_id=None, route=None):
    return Reply(conversation_id=cid, text=text, handled_by=handled_by, ticket_id=ticket_id,
                 metadata={"route": route} if route else {})


def summary(cid, user_key="PHONE#+919876543210", started_at="2026-09-20T10:00:00+00:00", **fields):
    return ConversationSummaryItem(conversation_id=cid, user_key=user_key, started_at=started_at,
                                   last_at=started_at, **fields)


class StoreContract:
    def make_store(self):
        raise NotImplementedError

    def test_get_creates_a_fresh_state_with_a_start_time(self):
        state = self.make_store().get("c1")
        self.assertEqual((state.conversation_id, state.version, state.turns), ("c1", 0, 0))
        self.assertTrue(state.started_at)

    def test_saved_state_reloads_with_its_version_advanced(self):
        store = self.make_store()
        state = store.get("c1")
        state.agent, state.turns = "battery_support", 1
        state.history.append({"role": "user", "content": "hi"})
        store.save(state)
        again = store.get("c1")
        self.assertEqual((again.agent, again.turns, again.version), ("battery_support", 1, 1))
        self.assertEqual(again.history, [{"role": "user", "content": "hi"}])

    def test_record_turn_writes_the_transcript_in_order_and_redacted(self):
        store = self.make_store()
        state = store.get("c1")
        state.turns = 1
        store.record_turn(state, inbound("call me on 9876543210", attachments=[Attachment(kind="image", url="https://x.test/p.jpg")]),
                          reply("Try another socket.", route="narrow"))
        state.turns = 2
        store.record_turn(state, inbound("still dead"), reply("Raising a ticket.", ticket_id="EM-00001"))
        turns = store.transcript("c1")
        self.assertEqual([(t.n, t.role) for t in turns], [(1, "customer"), (2, "bot"), (3, "customer"), (4, "bot")])
        self.assertNotIn("9876543210", turns[0].text)
        self.assertEqual(turns[0].attachments, ({"kind": "image", "url": "https://x.test/p.jpg"},))
        self.assertEqual((turns[1].handled_by, turns[1].path), ("battery_support", "narrow"))

    def test_record_turn_is_idempotent_per_turn_number(self):
        store = self.make_store()
        state = store.get("c1")
        state.turns = 1
        store.record_turn(state, inbound("hi"), reply("Hello."))
        store.record_turn(state, inbound("hi"), reply("Hello."))
        self.assertEqual(len(store.transcript("c1")), 2)

    def test_recent_summaries_are_newest_first_limited_and_exclude_the_current_one(self):
        store = self.make_store()
        for cid, day in (("a", "01"), ("b", "10"), ("c", "20"), ("d", "25")):
            state = store.get(cid)
            state.user_key, state.turns = "PHONE#+919876543210", 1
            store.record_turn(state, inbound("x", cid), reply("y", cid), summary(cid, started_at="2026-09-%sT10:00:00+00:00" % day))
        self.assertEqual([s.conversation_id for s in store.recent_summaries("PHONE#+919876543210")], ["d", "c", "b"])
        self.assertEqual([s.conversation_id for s in store.recent_summaries("PHONE#+919876543210", limit=2, exclude="d")], ["c", "b"])
        self.assertEqual(store.recent_summaries("PHONE#+910000000000"), [])

    def test_a_summary_is_upserted_not_duplicated(self):
        store = self.make_store()
        state = store.get("a")
        state.user_key, state.turns = "PHONE#+919876543210", 1
        store.record_turn(state, inbound("x", "a"), reply("y", "a"), summary("a", turns=1))
        state.turns = 2
        store.record_turn(state, inbound("x", "a"), reply("y", "a"), summary("a", turns=2, outcome="escalated"))
        [only] = store.recent_summaries("PHONE#+919876543210")
        self.assertEqual((only.turns, only.outcome), (2, "escalated"))

    def test_no_summary_is_written_without_a_user_key(self):
        store = self.make_store()
        state = store.get("a")
        state.turns = 1
        store.record_turn(state, inbound("x", "a"), reply("y", "a"), summary("a", user_key="PHONE#+919876543210"))
        self.assertEqual(store.recent_summaries("PHONE#+919876543210"), [])
```

`tests/test_memory_store.py`:

```python
import unittest

from emotorad_ai.conversation import ConversationStore, InMemoryConversationStore
from tests.store_contract import StoreContract


class InMemoryStoreTests(StoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryConversationStore()

    def test_the_old_name_still_imports(self):
        self.assertIs(ConversationStore, InMemoryConversationStore)

    def test_get_returns_the_same_object_within_a_process(self):
        store = self.make_store()
        self.assertIs(store.get("c1"), store.get("c1"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_memory_store -v`. Expected: ERROR, `cannot import name 'InMemoryConversationStore'`.

- [ ] **Step 3: Implement in `conversation.py`**

Add these imports:

```python
from datetime import datetime, timezone
from typing import Callable, Tuple

from .contract import InboundMessage, Reply
from .observability import redact_pii
```

Then replace the existing `ConversationStore` class with:

```python
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationConflict(Exception):
    """Another server saved this conversation after we loaded it."""


class StoreUnavailable(Exception):
    """The store could not be read or written. Never treated as an empty state."""


@dataclass(frozen=True)
class TranscriptTurn:
    """What was said, and nothing the model saw besides: no tool results."""

    n: int
    role: str  # "customer" | "bot"
    text: str
    at: str
    attachments: Tuple[Dict[str, str], ...] = ()
    handled_by: str = ""
    path: str = ""


@dataclass(frozen=True)
class ConversationSummaryItem:
    """One line of a person's history, built by code for the bot's memory."""

    conversation_id: str
    user_key: str
    started_at: str
    last_at: str
    channel: str = ""
    title: str = ""
    frame_number: Optional[str] = None
    product_name: Optional[str] = None
    agent: Optional[str] = None
    sub_category: Optional[str] = None
    outcome: str = "open"  # "open" | "escalated"
    ticket_id: Optional[str] = None
    turns: int = 0


def transcript_turns(state: "ConversationState", inbound: InboundMessage, reply: Reply, at: str) -> Tuple[TranscriptTurn, TranscriptTurn]:
    """The customer's message and the bot's reply for this turn, redacted.

    Numbered from the turn counter, so recording the same turn twice (a retry)
    overwrites rather than duplicates.
    """
    n = state.turns * 2 - 1
    customer = TranscriptTurn(
        n=n, role="customer", text=redact_pii(inbound.message_text or ""), at=at,
        attachments=tuple({"kind": a.kind, "url": a.url} for a in inbound.attachments),
    )
    bot = TranscriptTurn(
        n=n + 1, role="bot", text=redact_pii(reply.text or ""), at=at,
        attachments=tuple({"kind": a.kind, "url": a.url} for a in reply.attachments),
        handled_by=reply.handled_by or "", path=str(reply.metadata.get("route") or ""),
    )
    return customer, bot


class InMemoryConversationStore:
    """One process, lost on restart. The default, and what every test uses.

    `get` hands back the same object each time, so there is nothing to conflict
    with; `save` only advances the version to match the durable store.
    """

    def __init__(self, clock: Callable[[], str] = utc_now_iso) -> None:
        self._clock = clock
        self._states: Dict[str, ConversationState] = {}
        self._turns: Dict[str, Dict[int, TranscriptTurn]] = {}
        self._summaries: Dict[str, Dict[str, ConversationSummaryItem]] = {}

    def get(self, conversation_id: str) -> ConversationState:
        state = self._states.get(conversation_id)
        if state is None:
            state = ConversationState(conversation_id=conversation_id, started_at=self._clock())
            self._states[conversation_id] = state
        return state

    def save(self, state: ConversationState) -> None:
        state.version += 1
        self._states[state.conversation_id] = state

    def record_turn(self, state: ConversationState, inbound: InboundMessage, reply: Reply,
                    summary: Optional[ConversationSummaryItem] = None) -> None:
        turns = self._turns.setdefault(state.conversation_id, {})
        for turn in transcript_turns(state, inbound, reply, self._clock()):
            turns[turn.n] = turn
        if summary is not None and state.user_key:
            self._summaries.setdefault(state.user_key, {})[state.conversation_id] = summary

    def transcript(self, conversation_id: str) -> List[TranscriptTurn]:
        return [turn for _, turn in sorted(self._turns.get(conversation_id, {}).items())]

    def recent_summaries(self, user_key: str, limit: int = 3, exclude: Optional[str] = None) -> List[ConversationSummaryItem]:
        items = [s for s in self._summaries.get(user_key, {}).values() if s.conversation_id != exclude]
        return sorted(items, key=lambda s: s.started_at, reverse=True)[:limit]

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history

    def __len__(self) -> int:
        return len(self._states)


# The name every caller already imports.
ConversationStore = InMemoryConversationStore
```

Before adding the `conversation.py` imports, check for a cycle: `contract` and `observability` must not import `conversation`. Confirm with `grep -n "conversation" src/emotorad_ai/contract.py src/emotorad_ai/observability.py`. The expected result is none.

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_memory_store tests.test_conversation_state -v`, then the full suite. Expected: all pass.

- [ ] **Step 5: Commit.** Message: "Turn the conversation store into an interface with transcripts and summaries".

---

### Task 3: `DynamoConversationStore`

**Files:**
- Create:
  - `src/emotorad_ai/stores/__init__.py` (empty)
  - `src/emotorad_ai/stores/dynamo.py`
  - `tests/dynamo_fixture.py`
- Test: `tests/test_dynamo_store.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces:
  - `DynamoConversationStore(table_name, client=None, region="ap-south-1", endpoint_url=None, clock=utc_now_iso, now=time.time, state_ttl_hours=48, transcript_ttl_days=90, log=None, max_state_bytes=350_000)`
  - `tests.dynamo_fixture.MotoTable`, a mixin with `setUp` and `tearDown` that provide `self.client` and `self.table`

- [ ] **Step 1: Write the fixture and the failing tests**

`tests/dynamo_fixture.py`:

```python
"""An in-process DynamoDB (moto) with the production table shape.

Fake credentials and no AWS_PROFILE, so a test can never reach a real account
even on a machine that is signed in to one.
"""

import os
from unittest import mock

import boto3
from moto import mock_aws

TABLE = "emotorad-ai-conversations"
FAKE_ENV = {
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "ap-south-1",
}


class MotoTable:
    def setUp(self):
        super().setUp()
        self._env = mock.patch.dict(os.environ, FAKE_ENV)
        self._env.start()
        os.environ.pop("AWS_PROFILE", None)
        self._aws = mock_aws()
        self._aws.start()
        self.table = TABLE
        self.client = boto3.client("dynamodb", region_name="ap-south-1")
        self.client.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )

    def tearDown(self):
        self._aws.stop()
        self._env.stop()
        super().tearDown()
```

`tests/test_dynamo_store.py`:

```python
import json
import unittest

from botocore.exceptions import EndpointConnectionError

from emotorad_ai.conversation import ConversationConflict, ConversationState, StoreUnavailable
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.dynamo import DynamoConversationStore
from tests.dynamo_fixture import MotoTable
from tests.store_contract import StoreContract, inbound, reply, summary

NOW = 1_790_000_000


class DynamoStoreTests(MotoTable, StoreContract, unittest.TestCase):
    def make_store(self, **kw):
        return DynamoConversationStore(self.table, client=self.client, now=lambda: NOW, **kw)

    def item(self, pk, sk):
        return self.client.get_item(TableName=self.table, Key={"PK": {"S": pk}, "SK": {"S": sk}})["Item"]

    def test_a_stale_save_is_refused(self):
        store = self.make_store()
        first, second = store.get("c1"), store.get("c1")
        store.save(first)
        with self.assertRaises(ConversationConflict):
            store.save(second)

    def test_two_saves_from_one_load_conflict(self):
        store = self.make_store()
        state = store.get("c1")
        store.save(state)
        stale = ConversationState.from_json(state.to_json())
        stale.version = 0
        with self.assertRaises(ConversationConflict):
            store.save(stale)

    def test_every_item_carries_its_expiry(self):
        store = self.make_store()
        state = store.get("c1")
        state.user_key, state.turns = "PHONE#+919876543210", 1
        store.save(state)
        store.record_turn(state, inbound("hi"), reply("Hello."), summary("c1"))
        self.assertEqual(int(self.item("CONV#c1", "STATE")["expires_at"]["N"]), NOW + 48 * 3600)
        self.assertEqual(int(self.item("CONV#c1", "TURN#00001")["expires_at"]["N"]), NOW + 90 * 86400)
        summary_item = self.client.query(TableName=self.table, KeyConditionExpression="PK = :pk",
                                         ExpressionAttributeValues={":pk": {"S": "USER#PHONE#+919876543210"}})["Items"][0]
        self.assertEqual(int(summary_item["expires_at"]["N"]), NOW + 90 * 86400)

    def test_a_huge_history_is_trimmed_by_whole_turns_and_logged(self):
        log = EventLog(path=None)
        store = self.make_store(log=log, max_state_bytes=20_000)
        state = store.get("c1")
        for i in range(40):
            state.history.append({"role": "user", "content": "turn %d" % i})
            state.history.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": "x", "input": {}}]})
            state.history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": "x" * 1000}]})
            state.history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        store.save(state)
        kept = store.get("c1").history
        self.assertLess(len(json.dumps(kept)), 20_000)
        self.assertEqual(kept[0]["role"], "user")
        self.assertIsInstance(kept[0]["content"], str)  # starts at a turn boundary
        uses = {b["id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_use"}
        results = {b["tool_use_id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"}
        self.assertEqual(uses, results)
        self.assertEqual(kept[-1], {"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        self.assertTrue(any(e["event"] == "history_trimmed" for e in log.events))

    def test_a_network_failure_is_store_unavailable_not_an_empty_state(self):
        class Broken:
            def get_item(self, **kw):
                raise EndpointConnectionError(endpoint_url="https://dynamodb.ap-south-1.amazonaws.com")
        with self.assertRaises(StoreUnavailable):
            DynamoConversationStore(self.table, client=Broken()).get("c1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_dynamo_store -v`. Expected: ERROR, `No module named 'emotorad_ai.stores'`.

- [ ] **Step 3: Implement `src/emotorad_ai/stores/dynamo.py`**

```python
"""Conversations and idempotency keys in one DynamoDB table (spec 2026-09-28).

Low-level boto3 client, created lazily, so importing this module costs nothing
in offline mode. Every AWS failure becomes a typed error the runtime handles:
ConversationConflict when another server saved first, StoreUnavailable for
anything else. Nothing here ever continues on an empty state after a failed
read, which would silently lose the customer's conversation.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional

from ..contract import InboundMessage, Reply
from ..conversation import (
    ConversationConflict,
    ConversationState,
    ConversationSummaryItem,
    StoreUnavailable,
    TranscriptTurn,
    transcript_turns,
    utc_now_iso,
)

HOUR = 3600
DAY = 86400


def _s(value: Any) -> Dict[str, str]:
    return {"S": "" if value is None else str(value)}


def _n(value: int) -> Dict[str, str]:
    return {"N": str(int(value))}


def _is_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException"


def _turn_starts(history: List[Dict[str, Any]]) -> List[int]:
    """Indexes where a customer turn begins: a user message whose content is
    text. Tool results are user messages too, but carry a list, and must never
    be cut from the tool call before them."""
    return [i for i, m in enumerate(history) if m.get("role") == "user" and isinstance(m.get("content"), str)]


class DynamoConversationStore:
    def __init__(
        self,
        table_name: str,
        client: Any = None,
        region: str = "ap-south-1",
        endpoint_url: Optional[str] = None,
        clock: Callable[[], str] = utc_now_iso,
        now: Callable[[], float] = time.time,
        state_ttl_hours: int = 48,
        transcript_ttl_days: int = 90,
        log: Any = None,
        max_state_bytes: int = 350_000,
    ) -> None:
        self.table = table_name
        self._client = client
        self._region = region
        self._endpoint = endpoint_url
        self._clock = clock
        self._now = now
        self._state_ttl = state_ttl_hours * HOUR
        self._transcript_ttl = transcript_ttl_days * DAY
        self._log = log
        self._max_state_bytes = max_state_bytes

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self._region, endpoint_url=self._endpoint)
        return self._client

    def _call(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            return getattr(self.client, operation)(TableName=self.table, **kwargs)
        except Exception as exc:  # botocore raises ClientError, BotoCoreError and plain network errors
            if _is_conflict(exc):
                raise ConversationConflict("conversation was saved by another server") from None
            raise StoreUnavailable("DynamoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    # -- working state -----------------------------------------------------

    def get(self, conversation_id: str) -> ConversationState:
        response = self._call("get_item", Key={"PK": _s("CONV#" + conversation_id), "SK": _s("STATE")}, ConsistentRead=True)
        item = response.get("Item")
        if not item:
            return ConversationState(conversation_id=conversation_id, started_at=self._clock())
        state = ConversationState.from_json(item["state"]["S"])
        state.version = int(item["version"]["N"])
        return state

    def save(self, state: ConversationState) -> None:
        self._trim(state)
        expected = state.version
        item = {
            "PK": _s("CONV#" + state.conversation_id),
            "SK": _s("STATE"),
            "state": _s(state.to_json()),
            "version": _n(expected + 1),
            "user_key": _s(state.user_key),
            "updated_at": _s(self._clock()),
            "expires_at": _n(self._now() + self._state_ttl),
        }
        if expected == 0:
            self._call("put_item", Item=item, ConditionExpression="attribute_not_exists(PK)")
        else:
            self._call("put_item", Item=item, ConditionExpression="version = :v",
                       ExpressionAttributeValues={":v": _n(expected)})
        state.version = expected + 1

    def _trim(self, state: ConversationState) -> None:
        """Keep the item under DynamoDB's 400 KB limit by dropping the oldest
        whole turns. The transcript is a separate item and keeps everything."""
        dropped = 0
        while len(state.to_json().encode("utf-8")) > self._max_state_bytes:
            starts = _turn_starts(state.history)
            if len(starts) < 2:
                break  # one turn left; the put will fail loudly rather than lose it
            del state.history[: starts[1]]
            dropped += 1
        if dropped and self._log is not None:
            self._log.emit("history_trimmed", state.conversation_id, turns_dropped=dropped)

    # -- transcript and summaries -----------------------------------------

    def record_turn(self, state: ConversationState, inbound: InboundMessage, reply: Reply,
                    summary: Optional[ConversationSummaryItem] = None) -> None:
        expires = _n(self._now() + self._transcript_ttl)
        for turn in transcript_turns(state, inbound, reply, self._clock()):
            self._call("put_item", Item={
                "PK": _s("CONV#" + state.conversation_id), "SK": _s("TURN#%05d" % turn.n),
                "turn": _s(json.dumps(asdict(turn), ensure_ascii=False)), "expires_at": expires,
            })
        if summary is not None and state.user_key:
            self._call("put_item", Item={
                "PK": _s("USER#" + state.user_key),
                "SK": _s("CONV#%s#%s" % (summary.started_at, summary.conversation_id)),
                "summary": _s(json.dumps(asdict(summary), ensure_ascii=False)), "expires_at": expires,
            })

    def _query_all(self, **kwargs: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        while True:
            response = self._call("query", **kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response or kwargs.get("Limit"):
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def transcript(self, conversation_id: str) -> List[TranscriptTurn]:
        items = self._query_all(KeyConditionExpression="PK = :pk AND begins_with(SK, :turn)",
                                ExpressionAttributeValues={":pk": _s("CONV#" + conversation_id), ":turn": _s("TURN#")})
        turns = []
        for item in items:
            raw = json.loads(item["turn"]["S"])
            raw["attachments"] = tuple(raw.get("attachments") or ())
            turns.append(TranscriptTurn(**raw))
        return sorted(turns, key=lambda t: t.n)

    def recent_summaries(self, user_key: str, limit: int = 3, exclude: Optional[str] = None) -> List[ConversationSummaryItem]:
        items = self._query_all(KeyConditionExpression="PK = :pk AND begins_with(SK, :conv)",
                                ExpressionAttributeValues={":pk": _s("USER#" + user_key), ":conv": _s("CONV#")},
                                ScanIndexForward=False, Limit=limit + 1)
        summaries = [ConversationSummaryItem(**json.loads(item["summary"]["S"])) for item in items]
        return [s for s in summaries if s.conversation_id != exclude][:limit]

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history
```

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_dynamo_store -v`, then the full suite. Expected: all pass. The contract tests run twice, once per store.

- [ ] **Step 5: Commit.** Message: "Add the DynamoDB conversation store with versioned saves and TTLs".

---

### Task 4: Claim-before-execute idempotency, with a DynamoDB implementation

**Files:**
- Modify:
  - `src/emotorad_ai/tools/registry.py` (`IdempotencyStore.claim` and `release`, and the change to `ToolRegistry.call`)
  - `src/emotorad_ai/tools/mocks.py` (`build_registry(idempotency=None)`)
  - `src/emotorad_ai/stores/dynamo.py` (`DynamoIdempotencyStore`)
- Test: `tests/test_idempotency_claims.py`

**Interfaces:**
- Produces:
  - `IdempotencyStore.claim(key) -> Optional[Envelope]` and `.release(key)`
  - `DynamoIdempotencyStore(table_name, client=None, region=..., endpoint_url=None, now=time.time, ttl_days=7)` with `claim`, `get`, `put`, `release`
  - `build_registry(..., idempotency=None)`

- [ ] **Step 1: Write the failing tests**

`tests/test_idempotency_claims.py`:

```python
import unittest

from emotorad_ai.stores.dynamo import DynamoIdempotencyStore
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, MockTicketSystem, build_registry
from emotorad_ai.tools.registry import IdempotencyStore, ToolContext, ToolRegistry, is_error, ok
from tests.dynamo_fixture import MotoTable

TICKET = {"category": "battery_charging", "severity": "normal", "description": "LED off.", "idempotency_key": "k1"}
CTX = ToolContext(conversation_id="c1", phone="+919876543210")


class ClaimContract:
    def make(self):
        raise NotImplementedError

    def test_a_key_is_claimed_once_then_returns_the_stored_result(self):
        store = self.make()
        self.assertIsNone(store.claim("k"))
        pending = store.claim("k")
        self.assertEqual(pending["error"]["code"], "write_in_progress")
        store.put("k", ok({"ticket_id": "EM-1"}))
        self.assertEqual(store.claim("k"), ok({"ticket_id": "EM-1"}))

    def test_a_released_claim_can_be_claimed_again(self):
        store = self.make()
        store.claim("k")
        store.release("k")
        self.assertIsNone(store.claim("k"))


class InMemoryClaimTests(ClaimContract, unittest.TestCase):
    def make(self):
        return IdempotencyStore()


class DynamoClaimTests(MotoTable, ClaimContract, unittest.TestCase):
    def make(self):
        return DynamoIdempotencyStore(self.table, client=self.client, now=lambda: 1_790_000_000)

    def test_two_registries_on_one_table_raise_exactly_one_ticket(self):
        tickets = MockTicketSystem()
        first = build_registry(ticket_system=tickets, idempotency=self.make())
        second = build_registry(ticket_system=tickets, idempotency=self.make())
        a = first.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        b = second.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        self.assertEqual(a, b)
        self.assertEqual(len(tickets.tickets), 1)

    def test_the_claim_expires_after_seven_days(self):
        self.make().claim("c1:x:k")
        item = self.client.get_item(TableName=self.table, Key={"PK": {"S": "IDEM#c1:x:k"}, "SK": {"S": "IDEM"}})["Item"]
        self.assertEqual(int(item["expires_at"]["N"]), 1_790_000_000 + 7 * 86400)


class RegistryReleasesOnFailureTests(unittest.TestCase):
    def test_a_write_tool_that_raises_can_be_retried(self):
        registry = ToolRegistry()
        calls = []

        @registry.register("flaky_write", "d", parameters={"idempotency_key": {"type": "string"}},
                           required=("idempotency_key",), write=True)
        def flaky_write(idempotency_key):
            calls.append(idempotency_key)
            if len(calls) == 1:
                raise RuntimeError("upstream timed out")
            return {"done": True}

        first = registry.call("flaky_write", {"idempotency_key": "k"}, CTX)
        second = registry.call("flaky_write", {"idempotency_key": "k"}, CTX)
        self.assertTrue(is_error(first))
        self.assertEqual(second, ok({"done": True}))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_idempotency_claims -v`. Expected: ERROR, `cannot import name 'DynamoIdempotencyStore'`.

- [ ] **Step 3: Implement**

Replace `IdempotencyStore` in `registry.py` with:

```python
_PENDING = object()


class IdempotencyStore:
    """Maps an idempotency key to the envelope its first execution produced.

    Claim-before-execute: `claim` marks a key as in progress and only the
    caller that won the claim runs the tool, so a retry arriving while the
    first attempt is still running cannot execute it a second time.
    In-memory, one process; DynamoIdempotencyStore is the same contract across
    servers.
    """

    def __init__(self) -> None:
        self._seen: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def claim(self, key: str) -> Optional[Envelope]:
        with self._lock:
            existing = self._seen.get(key)
            if existing is None:
                self._seen[key] = _PENDING
                return None
            if existing is _PENDING:
                return err("write_in_progress", "This action is already being carried out; try again shortly.", retryable=True)
            return existing

    def get(self, key: str) -> Optional[Envelope]:
        with self._lock:
            existing = self._seen.get(key)
            return None if existing is _PENDING else existing

    def put(self, key: str, envelope: Envelope) -> None:
        with self._lock:
            self._seen[key] = envelope

    def release(self, key: str) -> None:
        with self._lock:
            if self._seen.get(key) is _PENDING:
                del self._seen[key]
```

In `ToolRegistry.call`, replace everything from the idempotency comment down to the end of the method with the block below. The missing-arguments check now runs **before** the claim, so a rejected call never holds a claim.

```python
        # The idempotency key is checked before the other arguments so a
        # missing key is reported as itself rather than as one more absent field.
        scoped_key = None
        if spec.write:
            key = arguments.get("idempotency_key")
            if not key:
                return err("missing_idempotency_key", "%s requires an idempotency_key." % name)
            scoped_key = "%s:%s:%s" % (context.conversation_id, name, key)

        missing = [key for key in spec.required if key not in arguments]
        if missing:
            return err("missing_arguments", "Missing required argument(s): %s" % ", ".join(missing))

        if scoped_key is not None:
            previous = self.idempotency.claim(scoped_key)
            if previous is not None:
                return previous

        try:
            result = spec.fn(**arguments)
        except ToolError as exc:
            envelope = err(exc.code, exc.message, exc.retryable, exc.remedy)
        except Exception as exc:  # a broken tool must not kill the conversation
            envelope = err("tool_exception", "%s: %s" % (type(exc).__name__, exc), retryable=True)
        else:
            envelope = result if isinstance(result, dict) and ("data" in result or "error" in result) else ok(result)

        if scoped_key is not None:
            if is_error(envelope):
                # A failed write did nothing; let the retry run it.
                self.idempotency.release(scoped_key)
            else:
                self.idempotency.put(scoped_key, envelope)
        return envelope
```

In `mocks.py`, add `idempotency: Optional[Any] = None` as the last parameter of `build_registry`. Then change `registry = ToolRegistry()` to:

```python
    registry = ToolRegistry(idempotency=idempotency) if idempotency is not None else ToolRegistry()
```

Append to `stores/dynamo.py`:

```python
class DynamoIdempotencyStore:
    """The registry's idempotency contract, shared by every server via the table."""

    def __init__(self, table_name: str, client: Any = None, region: str = "ap-south-1",
                 endpoint_url: Optional[str] = None, now: Callable[[], float] = time.time, ttl_days: int = 7) -> None:
        self._conversations = DynamoConversationStore(table_name, client=client, region=region, endpoint_url=endpoint_url, now=now)
        self._now = now
        self._ttl = ttl_days * DAY

    def _key(self, key: str) -> Dict[str, Dict[str, str]]:
        return {"PK": _s("IDEM#" + key), "SK": _s("IDEM")}

    def claim(self, key: str) -> Optional[Dict[str, Any]]:
        from ..tools.registry import err

        item = dict(self._key(key), status=_s("pending"), expires_at=_n(self._now() + self._ttl))
        try:
            self._conversations._call("put_item", Item=item, ConditionExpression="attribute_not_exists(PK)")
            return None
        except ConversationConflict:
            existing = self.get(key)
            if existing is not None:
                return existing
            return err("write_in_progress", "This action is already being carried out; try again shortly.", retryable=True)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        item = self._conversations._call("get_item", Key=self._key(key), ConsistentRead=True).get("Item")
        if not item or item.get("status", {}).get("S") != "done":
            return None
        return json.loads(item["envelope"]["S"])

    def put(self, key: str, envelope: Dict[str, Any]) -> None:
        self._conversations._call("put_item", Item=dict(
            self._key(key), status=_s("done"), envelope=_s(json.dumps(envelope, default=str)),
            expires_at=_n(self._now() + self._ttl),
        ))

    def release(self, key: str) -> None:
        self._conversations._call("delete_item", Key=self._key(key),
                                  ConditionExpression="#s = :pending",
                                  ExpressionAttributeNames={"#s": "status"},
                                  ExpressionAttributeValues={":pending": _s("pending")})
```

Note: a conditional delete that fails because the item is already `done` raises `ConversationConflict`. `release` only runs after a failed tool call, so that can't happen in normal flow. If it ever does, the raise must propagate: that's a real bug, not something to swallow.

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_idempotency_claims tests.test_tools -v`, then the full suite. Expected: all pass, including the existing idempotency tests in `test_tools` and `test_agent_and_runtime`.

- [ ] **Step 5: Commit.** Message: "Claim writes before executing them, in memory and across servers".

---

### Task 5: The runtime loads, saves and records every turn

**Files:**
- Modify: `src/emotorad_ai/runtime.py`
- Test: `tests/test_runtime_persistence.py`

**Interfaces:**
- Consumes: the Task 2 and Task 3 stores.
- Produces:
  - `Runtime(..., conversations=None)`
  - `BUSY_MESSAGE`
  - `Runtime._summary_for(state, resolved) -> Optional[ConversationSummaryItem]`
  - `Runtime._user_key(resolved) -> Optional[str]`
  - `AGENT_TITLES`

- [ ] **Step 1: Write the failing tests**

`tests/test_runtime_persistence.py`:

```python
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import ConversationConflict, InMemoryConversationStore, StoreUnavailable
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import BUSY_MESSAGE, Runtime
from emotorad_ai.stores.dynamo import DynamoConversationStore
from emotorad_ai.tools.mocks import build_registry
from tests.dynamo_fixture import MotoTable

TODAY = date(2026, 7, 28)


def runtime_on(store, responses, registry=None):
    registry = registry or build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(responses)),
                   log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=store)


def send(runtime, text, cid="conv-1", session="sess-ananya"):
    adapter = WebsiteChatAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"conversation_id": cid, "session_token": session, "text": text}))


class RestartTests(MotoTable, unittest.TestCase):
    def store(self):
        return DynamoConversationStore(self.table, client=self.client)

    def test_a_conversation_continues_after_a_restart(self):
        send(runtime_on(self.store(), [say("Try another socket.")]), "my battery won't charge")
        restarted = runtime_on(self.store(), [say("Then it is the charger.")])
        reply = send(restarted, "still nothing")
        self.assertEqual(reply.handled_by, "battery_support")  # routed state survived: no triage again
        state = self.store().get("conv-1")
        self.assertEqual(state.turns, 2)
        self.assertEqual([t.role for t in self.store().transcript("conv-1")], ["customer", "bot", "customer", "bot"])

    def test_a_verified_customer_gets_a_summary_and_an_anonymous_one_does_not(self):
        send(runtime_on(self.store(), [say("Ok.")]), "my battery won't charge")
        [item] = self.store().recent_summaries("PHONE#+919876543210")
        self.assertEqual((item.conversation_id, item.title, item.product_name), ("conv-1", "Battery issue", "EMX Plus"))
        send(runtime_on(self.store(), []), "hello", cid="anon", session="no-such-session")
        self.assertEqual(self.store().get("anon").user_key, None)


class ConflictTests(unittest.TestCase):
    class ConflictingStore(InMemoryConversationStore):
        def __init__(self, conflicts):
            super().__init__()
            self.conflicts, self.gets = conflicts, 0

        def get(self, conversation_id):
            self.gets += 1
            return super().get(conversation_id)

        def save(self, state):
            if self.conflicts:
                self.conflicts -= 1
                raise ConversationConflict("someone else saved")
            super().save(state)

    def test_one_conflict_is_retried_from_fresh_state(self):
        store = self.ConflictingStore(conflicts=1)
        reply = send(runtime_on(store, [say("First."), say("Second.")]), "my battery won't charge")
        self.assertIn("Second.", reply.text)
        self.assertEqual(store.gets, 2)

    def test_two_conflicts_answer_busy_without_escalating(self):
        runtime = runtime_on(self.ConflictingStore(conflicts=2), [say("First."), say("Second.")])
        reply = send(runtime, "my battery won't charge")
        self.assertIn(BUSY_MESSAGE, reply.text)
        self.assertFalse(reply.escalated)
        self.assertTrue(any(e["event"] == "conversation_busy" for e in runtime.log.events))


class OutageTests(unittest.TestCase):
    class DownStore(InMemoryConversationStore):
        def get(self, conversation_id):
            raise StoreUnavailable("DynamoDB get_item failed")

    def test_a_store_outage_hands_over_instead_of_starting_blank(self):
        runtime = runtime_on(self.DownStore(), [])
        reply = send(runtime, "my battery won't charge")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "store_unavailable")
        self.assertTrue(any(e["event"] == "store_unavailable" for e in runtime.log.events))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_runtime_persistence -v`. Expected: ERROR, `cannot import name 'BUSY_MESSAGE'`.

- [ ] **Step 3: Implement in `runtime.py`**

Change `from .conversation import ConversationState, ConversationStore` to:

```python
from .conversation import (
    ConversationConflict,
    ConversationState,
    ConversationSummaryItem,
    InMemoryConversationStore,
    StoreUnavailable,
)
from .disclosure import apply_disclosure
```

Remove the separate `apply_disclosure` import if this duplicates it. Then add these constants after `UNSUPPORTED_MESSAGE`:

```python
BUSY_MESSAGE = "Sorry, I'm still working on your last message. Please send that again in a moment."

# The summary title when no knowledge record was chosen. Fixed labels, never
# chat text, so nothing a customer typed is replayed into a future prompt.
AGENT_TITLES = {
    BATTERY_SUPPORT: "Battery issue", MOTOR_SUPPORT: "Motor issue",
    LATE_WARRANTY: "Warranty registration", DEALER_ORDERS: "Dealer order",
}
```

Add `conversations: Any = None` to `__init__`'s parameters. Replace `self.conversations = ConversationStore()` with:

```python
        self.conversations = conversations if conversations is not None else InMemoryConversationStore()
```

Replace `handle` with:

```python
    def handle(self, message: InboundMessage) -> Reply:
        cid = message.conversation_id
        for attempt in (1, 2):
            try:
                state = self.conversations.get(cid)  # fresh on every attempt
            except StoreUnavailable as exc:
                return self._store_down(message, exc)
            final = self.graph.invoke({"message": message, "conversation": state})
            reply, resolved = final["reply"], final.get("resolved")
            state.escalated = state.escalated or reply.escalated
            state.ticket_id = reply.ticket_id or state.ticket_id
            try:
                self.conversations.save(state)
                break
            except ConversationConflict:
                self.log.emit("conversation_conflict", cid, attempt=attempt)
                if attempt == 2:
                    self.log.emit("conversation_busy", cid)
                    return Reply(conversation_id=cid, text=self._outbound(BUSY_MESSAGE, state, message.channel),
                                 handled_by="conversation_busy")
            except StoreUnavailable as exc:
                return self._store_down(message, exc)
        try:
            self.conversations.record_turn(state, message, reply, self._summary_for(state, resolved))
        except StoreUnavailable as exc:
            # The turn happened and the state is saved; only the record of it
            # failed. Say so in the log, and still answer the customer.
            self.log.emit("transcript_write_failed", cid, error=str(exc))
        return reply

    def _store_down(self, message: InboundMessage, exc: Exception) -> Reply:
        self.log.emit("store_unavailable", message.conversation_id, error=str(exc))
        self.log.escalation(message.conversation_id, "store_unavailable", None)
        # A throwaway state: the disclosure is always added, since we cannot
        # know whether this person has already seen it.
        text = apply_disclosure(HANDOVER_TEXT, ConversationState(conversation_id=message.conversation_id), message.channel)
        return Reply(conversation_id=message.conversation_id, text=text, handled_by="store_unavailable", escalated=True)
```

In `_node_prepare`, replace `state = self.conversations.get(message.conversation_id)` with `state = turn["conversation"]`. After `resolved = self.resolver.hydrate(message)`, add:

```python
        if state.channel is None:
            state.channel = message.channel
        if state.user_key is None:
            state.user_key = self._user_key(resolved)
```

Add these helpers to the helpers section:

```python
    @staticmethod
    def _user_key(resolved: ResolvedIdentity) -> Optional[str]:
        """Who a conversation belongs to, for memory. Only a proven identity:
        a cookie or caller ID never gets a history (disclosure rule)."""
        identity = resolved.identity
        if resolved.persona == "dealer" and identity.dealer_id:
            return "DEALER#" + identity.dealer_id
        if resolved.persona == "customer" and identity.may_disclose and identity.phone:
            return "PHONE#" + identity.phone
        return None

    def _summary_for(self, state: ConversationState, resolved: Optional[ResolvedIdentity]) -> Optional[ConversationSummaryItem]:
        if not state.user_key:
            return None
        record = self.catalogue.records.get(state.sub_category) if (self.catalogue and state.sub_category) else None
        title = record.title if record else AGENT_TITLES.get(state.agent or "", "General question")
        bike = self._selected_bike(resolved, state) if resolved else None
        return ConversationSummaryItem(
            conversation_id=state.conversation_id, user_key=state.user_key,
            started_at=state.started_at or "", last_at=utc_now_iso(), channel=state.channel or "",
            title=title, frame_number=(bike or {}).get("frame_number"), product_name=(bike or {}).get("product_name"),
            agent=state.agent, sub_category=state.sub_category,
            outcome="escalated" if state.escalated else "open", ticket_id=state.ticket_id, turns=state.turns,
        )
```

Import `utc_now_iso` from `.conversation`.

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_runtime_persistence -v`, then the full suite. Expected: all pass. The existing tests still read `runtime.conversations.get()` and `history()` on the in-memory default.

- [ ] **Step 5: Commit.** Message: "Load, save and record every turn, with conflict retry and outage handover".

---

### Task 6: Memory from past conversations

**Files:**
- Modify:
  - `src/emotorad_ai/enrichment.py` (`summarise_past`)
  - `src/emotorad_ai/runtime.py` (`_node_prepare`)
- Test: `tests/test_memory.py`

**Interfaces:**
- Produces: `enrichment.summarise_past(summaries) -> Optional[str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_memory.py`:

```python
import unittest
from datetime import date

from emotorad_ai.conversation import ConversationSummaryItem, InMemoryConversationStore
from emotorad_ai.enrichment import summarise_past
from tests.test_runtime_persistence import runtime_on, send


def item(cid, day, **fields):
    return ConversationSummaryItem(conversation_id=cid, user_key="PHONE#+919876543210",
                                   started_at="2026-09-%02dT10:00:00+00:00" % day, last_at="", **fields)


class SummariseTests(unittest.TestCase):
    def test_lines_are_newest_first_and_built_from_fields_only(self):
        text = summarise_past([
            item("b", 20, title="Battery will not charge", product_name="EMX Plus", frame_number="EMXP2025004417",
                 outcome="escalated", ticket_id="EM-00012"),
            item("a", 2, title="Motor is making a noise", product_name="EMX Plus", frame_number="EMXP2025004417"),
        ])
        self.assertEqual(text, "20 Sep: Battery will not charge on the EMX Plus (…4417), escalated, ticket EM-00012\n"
                               "02 Sep: Motor is making a noise on the EMX Plus (…4417)")

    def test_nothing_to_say_is_none(self):
        self.assertIsNone(summarise_past([]))


class RuntimeMemoryTests(unittest.TestCase):
    def test_a_returning_verified_customer_brings_their_last_contact(self):
        store = InMemoryConversationStore()
        first = runtime_on(store, [say_("Ok.")])
        send(first, "my battery won't charge", cid="first")
        second = runtime_on(store, [say_("Welcome back.")])
        send(second, "my battery won't charge again", cid="second")
        prompt = second.llm.requests[0]["system"]
        self.assertIn("Last contact:", prompt)
        self.assertIn("Battery issue on the EMX Plus", prompt)

    def test_the_current_conversation_is_never_its_own_memory(self):
        store = InMemoryConversationStore()
        runtime = runtime_on(store, [say_("Ok.")])
        send(runtime, "my battery won't charge", cid="only")
        self.assertNotIn("Last contact:", runtime.llm.requests[0]["system"])

    def test_a_dealers_memory_is_keyed_by_dealer_and_never_shows_customer_chats(self):
        from emotorad_ai.runtime import Runtime
        self.assertEqual(Runtime._user_key(type("R", (), {"persona": "dealer", "identity": type("I", (), {"dealer_id": "DLR-PUN-014", "may_disclose": True, "phone": "+919000000001"})()})()), "DEALER#DLR-PUN-014")


def say_(text):
    from emotorad_ai.llm import say
    return say(text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_memory -v`. Expected: ERROR, `cannot import name 'summarise_past'`.

- [ ] **Step 3: Implement**

In `enrichment.py`, add after `summarise_signals`:

```python
def summarise_past(summaries: Sequence[Any]) -> Optional[str]:
    """Earlier conversations, one line each, newest first, at most three.

    Built only from fields code wrote (a record title or a fixed label, the
    bike, the outcome, the ticket), never from what the customer typed, so a
    past message cannot steer a future prompt.
    """
    lines: List[str] = []
    for item in list(summaries)[:3]:
        try:
            day = datetime.fromisoformat(item.started_at).strftime("%d %b")
        except (TypeError, ValueError):
            day = "Earlier"
        line = "%s: %s" % (day, item.title or "General question")
        if item.product_name:
            line += " on the %s" % item.product_name
        if item.frame_number:
            line += " (…%s)" % item.frame_number[-4:]
        if item.outcome == "escalated":
            line += ", escalated"
        if item.ticket_id:
            line += ", ticket %s" % item.ticket_id
        lines.append(line)
    return "\n".join(lines) if lines else None
```

Add `from datetime import datetime`.

In `runtime._node_prepare`, change the enrichment call from `context = self.enricher.build(resolved)` to:

```python
            last_contact = None
            if state.user_key:
                try:
                    last_contact = summarise_past(self.conversations.recent_summaries(
                        state.user_key, limit=3, exclude=state.conversation_id))
                except StoreUnavailable as exc:
                    # Memory is a nicety; the conversation goes on without it.
                    self.log.emit("memory_unavailable", message.conversation_id, error=str(exc))
            context = self.enricher.build(resolved, last_conversation=last_contact)
```

Import `summarise_past` from `.enrichment`.

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_memory tests.test_enrichment -v`, then the full suite. Expected: all pass.

- [ ] **Step 5: Commit.** Message: "Remember a verified person's recent conversations, built in code".

---

### Task 7: The transcript on every ticket

**Files:**
- Modify:
  - `src/emotorad_ai/tools/mocks.py` (`MockTicketSystem.attach_transcript`)
  - `src/emotorad_ai/conversation.py` (`render_transcript`)
  - `src/emotorad_ai/runtime.py` (attach after `record_turn`)
- Test: `tests/test_ticket_transcript.py`

**Interfaces:**
- Produces:
  - `render_transcript(turns) -> str`
  - `MockTicketSystem.attach_transcript(ticket_id, transcript)`

- [ ] **Step 1: Write the failing tests**

`tests/test_ticket_transcript.py`:

```python
import unittest

from emotorad_ai.conversation import TranscriptTurn, render_transcript
from emotorad_ai.llm import call_tool, say
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET
from tests.test_runtime_persistence import InMemoryConversationStore, runtime_on, send


class RenderTests(unittest.TestCase):
    def test_plain_text_with_times_and_speakers(self):
        text = render_transcript([
            TranscriptTurn(1, "customer", "won't charge", "2026-09-28T09:41:00+00:00"),
            TranscriptTurn(2, "bot", "Try another socket.", "2026-09-28T09:41:05+00:00"),
        ])
        self.assertEqual(text, "[09:41] Customer: won't charge\n[09:41] Bot: Try another socket.")


class TicketTranscriptTests(unittest.TestCase):
    def test_an_agent_ticket_carries_the_thread_including_this_turn(self):
        runtime = runtime_on(InMemoryConversationStore(), [
            say("Is the charger light on?"),
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1"),
            say("I have raised a ticket."),
        ])
        send(runtime, "my battery won't charge")
        reply = send(runtime, "no, the light is off")
        transcript = runtime.registry.tickets.tickets[reply.ticket_id]["transcript"]
        self.assertIn("Customer: my battery won't charge", transcript)
        self.assertIn("Customer: no, the light is off", transcript)
        self.assertIn("Bot: I have raised a ticket.", transcript)

    def test_a_safety_ticket_carries_it_too(self):
        runtime = runtime_on(InMemoryConversationStore(), [])
        reply = send(runtime, "my battery is swollen")
        self.assertIn("Customer: my battery is swollen", runtime.registry.tickets.tickets[reply.ticket_id]["transcript"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_ticket_transcript -v`. Expected: ERROR, `cannot import name 'render_transcript'`.

- [ ] **Step 3: Implement**

In `conversation.py`, add:

```python
def render_transcript(turns: Sequence[TranscriptTurn]) -> str:
    """The thread as a person reads it on a ticket: time, speaker, words."""
    lines = []
    for turn in turns:
        try:
            clock = datetime.fromisoformat(turn.at).strftime("%H:%M")
        except (TypeError, ValueError):
            clock = "--:--"
        lines.append("[%s] %s: %s" % (clock, "Customer" if turn.role == "customer" else "Bot", turn.text))
    return "\n".join(lines)
```

Add `Sequence` to the typing import.

In `MockTicketSystem`, add:

```python
    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        """Zoho will implement this as a thread on the ticket; the mock keeps it."""
        if ticket_id not in self.tickets:
            raise KeyError("no ticket %s" % ticket_id)
        self.tickets[ticket_id]["transcript"] = transcript
```

In `runtime.handle`, after the `record_turn` try block and before `return reply`, add:

```python
        tickets = getattr(self.registry, "tickets", None)
        if reply.ticket_id and hasattr(tickets, "attach_transcript"):
            try:
                tickets.attach_transcript(reply.ticket_id, render_transcript(self.conversations.transcript(cid)))
            except Exception as exc:  # the ticket exists either way; say the thread did not reach it
                self.log.emit("transcript_attach_failed", cid, ticket_id=reply.ticket_id, error="%s: %s" % (type(exc).__name__, exc))
```

Import `render_transcript` from `.conversation`.

- [ ] **Step 4: Run the tests.** Run `python -m unittest tests.test_ticket_transcript -v`, then the full suite. Expected: all pass. Existing tests that read tickets by key only gain a `transcript` field.

- [ ] **Step 5: Commit.** Message: "Attach the conversation transcript to every support ticket".

---

### Task 8: Wiring, a real-table smoke check, and docs

**Files:**
- Modify:
  - `src/emotorad_ai/wiring.py` (`Stores`, `build_stores`)
  - `src/emotorad_ai/cli.py`
  - `src/emotorad_ai/api.py`
  - `CLAUDE.md`
- Create: `scripts/dynamo_smoke.py`
- Test: `tests/test_store_wiring.py`

**Interfaces:**
- Produces:
  - `wiring.Stores(conversations, idempotency)`
  - `wiring.build_stores(settings, log=None, client=None) -> Stores`

- [ ] **Step 1: Write the failing tests**

`tests/test_store_wiring.py`:

```python
import unittest

from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.stores.dynamo import DynamoConversationStore, DynamoIdempotencyStore
from emotorad_ai.tools.registry import IdempotencyStore
from emotorad_ai.wiring import build_stores
from tests.dynamo_fixture import MotoTable


class BuildStoresTests(MotoTable, unittest.TestCase):
    def test_memory_is_the_default(self):
        stores = build_stores(Settings(store="memory"))
        self.assertIsInstance(stores.conversations, InMemoryConversationStore)
        self.assertIsInstance(stores.idempotency, IdempotencyStore)

    def test_dynamodb_builds_both_stores_on_one_table_with_the_configured_windows(self):
        stores = build_stores(Settings(store="dynamodb", state_ttl_hours=24), client=self.client)
        self.assertIsInstance(stores.conversations, DynamoConversationStore)
        self.assertIsInstance(stores.idempotency, DynamoIdempotencyStore)
        self.assertEqual(stores.conversations.table, "emotorad-ai-conversations")
        self.assertEqual(stores.conversations._state_ttl, 24 * 3600)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests.** Command: `python -m unittest tests.test_store_wiring -v`. Expected: ERROR, `cannot import name 'build_stores'`.

- [ ] **Step 3: Implement**

Append to `wiring.py`:

```python
@dataclass
class Stores:
    conversations: Any
    idempotency: Any


def build_stores(settings: Settings, log: Any = None, client: Any = None) -> Stores:
    if settings.store == "memory":
        from .conversation import InMemoryConversationStore
        from .tools.registry import IdempotencyStore

        return Stores(conversations=InMemoryConversationStore(), idempotency=IdempotencyStore())
    from .stores.dynamo import DynamoConversationStore, DynamoIdempotencyStore

    common = dict(client=client, region=settings.aws_region, endpoint_url=settings.dynamo_endpoint)
    return Stores(
        conversations=DynamoConversationStore(
            settings.dynamo_table, state_ttl_hours=settings.state_ttl_hours,
            transcript_ttl_days=settings.transcript_ttl_days, log=log, **common,
        ),
        idempotency=DynamoIdempotencyStore(settings.dynamo_table, ttl_days=settings.idempotency_ttl_days, **common),
    )
```

In both `cli.py` and `api.py`:
- After `log = EventLog(...)`, add `stores = build_stores(settings, log=log)`.
- Build the registry with `build_registry(..., idempotency=stores.idempotency)`.
- Pass `conversations=stores.conversations` to `Runtime`.
- Import `build_stores` from `.wiring`.

In `cli.py`, move the `log` line above `registry` if needed.

`scripts/dynamo_smoke.py`. A person runs this against the real table, with `AWS_PROFILE` set to a non-root identity. It writes one conversation, reads it back, and deletes everything it wrote:

```python
"""Round-trip one conversation through the real DynamoDB table, then clean up.

    AWS_PROFILE=emotorad python scripts/dynamo_smoke.py

Uses the same store classes as the service. Writes under a unique smoke-test
conversation id and user key, and deletes every item it wrote before exiting,
whether the checks pass or fail.
"""

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.contract import Identity, InboundMessage, Reply  # noqa: E402
from emotorad_ai.conversation import ConversationSummaryItem  # noqa: E402
from emotorad_ai.stores.dynamo import DynamoConversationStore, DynamoIdempotencyStore  # noqa: E402
from emotorad_ai.tools.registry import ok  # noqa: E402


def main() -> int:
    settings = load_settings()
    cid, user = "smoke-%s" % uuid.uuid4().hex[:12], "SMOKE#%s" % uuid.uuid4().hex[:8]
    store = DynamoConversationStore(settings.dynamo_table, region=settings.aws_region)
    idem = DynamoIdempotencyStore(settings.dynamo_table, region=settings.aws_region)
    written = [("CONV#" + cid, "STATE"), ("CONV#" + cid, "TURN#00001"), ("CONV#" + cid, "TURN#00002"), ("IDEM#" + cid, "IDEM")]
    try:
        state = store.get(cid)
        state.user_key, state.turns = user, 1
        store.save(state)
        assert store.get(cid).version == 1, "state did not round-trip"
        message = InboundMessage(conversation_id=cid, persona="customer", identity=Identity(), channel="whatsapp", message_text="smoke test")
        summary = ConversationSummaryItem(conversation_id=cid, user_key=user, started_at=state.started_at, last_at=state.started_at, title="Smoke test")
        written.append(("USER#" + user, "CONV#%s#%s" % (state.started_at, cid)))
        store.record_turn(state, message, Reply(conversation_id=cid, text="ok", handled_by="smoke"), summary)
        assert [t.role for t in store.transcript(cid)] == ["customer", "bot"], "transcript did not round-trip"
        assert [s.conversation_id for s in store.recent_summaries(user)] == [cid], "summary did not round-trip"
        assert idem.claim(cid) is None and idem.claim(cid)["error"]["code"] == "write_in_progress", "claim failed"
        idem.put(cid, ok({"smoke": True}))
        assert idem.claim(cid) == ok({"smoke": True}), "claimed result did not round-trip"
        print("smoke OK: state, transcript, summary and idempotency all round-trip on %s" % settings.dynamo_table)
        return 0
    finally:
        for pk, sk in written:
            store.client.delete_item(TableName=settings.dynamo_table, Key={"PK": {"S": pk}, "SK": {"S": sk}})
        print("cleaned up %d smoke items" % len(written))


if __name__ == "__main__":
    raise SystemExit(main())
```

Add this line to the `CLAUDE.md` Commands section:

```markdown
- Conversation store: `EMOTORAD_STORE=memory|dynamodb` (default memory). DynamoDB uses one table, `emotorad-ai-conversations` in account 851725486214 `ap-south-1`, created 2026-09-28 by CLI (spec `docs/superpowers/specs/2026-09-28-dynamodb-conversation-store-design.md`). Tests run on moto and never reach AWS. `AWS_PROFILE=emotorad python scripts/dynamo_smoke.py` round-trips one conversation on the real table and deletes it; run by a person, never CI, never as root.
```

- [ ] **Step 4: Run everything.** Run `python -m unittest tests.test_store_wiring -v`, then the full suite, then `PYTHONPATH=src python -m emotorad_ai.cli --offline --channel amiigo --session sess-amiigo-test "hi"`. Expected: all pass, and the CLI greets as before.

- [ ] **Step 5: Commit.** Message: "Wire the store setting into the CLI and the API, and add a real-table smoke check".
