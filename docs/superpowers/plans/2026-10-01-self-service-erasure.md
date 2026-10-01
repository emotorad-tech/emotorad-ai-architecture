# Self-Service Erasure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A verified customer can ask, in the chat or with the Amiigo app's button, for everything the chatbot holds about them to be deleted; a nightly job deletes it.

**Architecture:** `erasure.py` holds the phrases, fixed replies, reference and audit-record shape. Both conversation stores gain an `erasure_requests` record per request. A new turn-graph node, `erasure_gate`, turns a deletion phrase into a confirmation and a request (through verification on the website). Three API endpoints let the app do the same. `erasure_job.py`, started nightly by GitHub through SSM in a one-off container, hides the person's files in S3 (plain delete; the lifecycle erases them within 30 days), deletes their records with `delete_person`, writes the audit record and closes the request.

**Tech Stack:** Python 3.12, FastAPI, MongoDB (mongomock in tests), boto3 (botocore Stubber in tests), GitHub Actions, CloudFormation, unittest.

**Spec:** `docs/superpowers/specs/2026-10-01-self-service-erasure-design.md`

## Global Constraints

- The chat and the endpoints only record a request; nothing is deleted outside `erasure_job.py`. `S3Store.hide` is called only by the job.
- Confirmation in the chat: the whole message, trimmed, ignoring case, is `delete`. Nothing else confirms.
- Reference: `DEL-` and six characters from `23456789ABCDEFGHJKMNPQRSTVWXYZ`.
- One pending request per `user_key`, whether asked in the chat or with the button.
- A closed request (`done`, `cancelled`, `failed`) has no `user_key`; it keeps `key_sha256`.
- `delete_person` never deletes `erasure_requests`.
- The job hides every file before deleting any record of a request; a hide failure leaves the records. `MAX_ATTEMPTS = 3`, then `failed`.
- Logged events carry the reference and the exception class only: never a phone, a token or a value.
- Endpoints: `session_token` in the body only, never a URL; verified customers only (403 `ERASURE_SIGN_IN`); the message rate limit.
- Fixed texts exactly as in `erasure.py` (Task 1). British English, no em dashes, in new comments, docs, texts and output.
- Test command (whole suite): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;." python <workspace>/suite.py` (the suite minus `tests.test_video` and `tests.test_start`, which fail only on this Windows machine and pass on Linux CI).

## Review Focus

1. A customer typing `DELETE` when the bot never asked (no `erasure_step`): an ordinary message, never a confirmation.
2. A deletion phrase while the verify step waits for a code: the code is still taken, and the reply after a correct code is the deletion confirmation, not the bike list.
3. A double tap on the app's button, or the button and the chat at once: at most one pending request, the same reference both times.
4. A person whose data was deleted asks again later: the closed request no longer matches their `user_key`, so a new request can be made.
5. A media record whose S3 object is already gone: S3 treats deleting a missing key as success, so the job carries on and closes the request.

---

### Task 1: Phrases, replies, references and the audit record

**Files:**
- Create: `src/emotorad_ai/erasure.py`
- Test: `tests/test_erasure.py`

**Interfaces:**
- Produces: `wants_deletion(text) -> bool`, `wants_cancel(text) -> bool`, `is_confirmation(text) -> bool`, `new_reference(choice=secrets.choice) -> str`, `key_sha256(subject: str) -> str`, `audit_record(subject, reason, run_by, at, deleted=None, s3_objects=None, s3_versions=None, incomplete=False) -> Dict[str, Any]`; constants `REFERENCE_ALPHABET`, `MAX_ATTEMPTS`, `WANTED = "wanted"`, `CANCEL_WANTED = "cancel_wanted"`, `CONFIRMING = "confirming"`, `ERASURE_CONFIRM`, `ERASURE_DIALOG`, `ERASURE_REQUESTED`, `ERASURE_KEPT`, `ERASURE_EXISTING`, `ERASURE_CANCELLED`, `ERASURE_NOTHING_TO_CANCEL`, `ERASURE_FAILED`, `ERASURE_SIGN_IN`.

- [ ] **Step 1: Write the failing tests**

```python
"""Self-service erasure: the phrases, the confirmation, the reference and the
audit record (spec 2026-10-01)."""

import re
import unittest
from datetime import datetime, timezone

from emotorad_ai import erasure


class PhraseTests(unittest.TestCase):
    def test_deletion_phrases(self):
        for text in ("delete my data", "Please DELETE my account", "erase my data", "remove my data",
                     "delete my details", "Delete my conversation data", "forget me",
                     "मेरा डेटा हटाओ", "मेरा डेटा डिलीट कर दो", "quiero borrar mis datos",
                     "eliminar mis datos", "eliminar mi cuenta", "delete   my    data"):
            self.assertTrue(erasure.wants_deletion(text), text)

    def test_cancel_phrases_win_over_deletion(self):
        for text in ("cancel my deletion", "please cancel the deletion", "don't delete my data",
                     "do not delete my data"):
            self.assertTrue(erasure.wants_cancel(text), text)
            self.assertFalse(erasure.wants_deletion(text), text)

    def test_ordinary_sentences_are_not_requests(self):
        for text in ("how do I delete a ride?", "my battery data looks wrong", "remove the battery",
                     "I deleted the app", "", None):
            self.assertFalse(erasure.wants_deletion(text), text)
            self.assertFalse(erasure.wants_cancel(text), text)


class ConfirmationTests(unittest.TestCase):
    def test_only_the_word_delete_confirms(self):
        for text in ("DELETE", "delete", "  Delete  "):
            self.assertTrue(erasure.is_confirmation(text), text)
        for text in ("yes", "delete it", "Delete.", "", None):
            self.assertFalse(erasure.is_confirmation(text), text)


class ReferenceTests(unittest.TestCase):
    def test_the_reference_shape(self):
        self.assertRegex(erasure.new_reference(), r"^DEL-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{6}$")
        self.assertEqual(erasure.new_reference(choice=lambda alphabet: alphabet[0]), "DEL-222222")
        self.assertFalse(set("01OILU") & set(erasure.REFERENCE_ALPHABET))


class AuditTests(unittest.TestCase):
    def test_the_record_names_no_one(self):
        at = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
        record = erasure.audit_record("PHONE#+919700000031", "self-service request DEL-222222",
                                      "nightly erasure job", at, deleted={"conversations": 1}, s3_objects=2)
        self.assertEqual(record, {
            "key_sha256": erasure.key_sha256("PHONE#+919700000031"), "kind": "PHONE",
            "reason": "self-service request DEL-222222", "run_by": "nightly erasure job", "at": at,
            "deleted": {"conversations": 1}, "s3_objects": 2,
        })
        self.assertRegex(record["key_sha256"], r"^[0-9a-f]{64}$")

    def test_an_incomplete_record(self):
        at = datetime(2026, 10, 2, tzinfo=timezone.utc)
        record = erasure.audit_record("DEALER#DLR-1", "r", "me", at, s3_objects=1, s3_versions=3, incomplete=True)
        self.assertEqual((record["kind"], record["incomplete"], record["s3_versions"]), ("DEALER", True, 3))
        self.assertNotIn("deleted", record)


class TextTests(unittest.TestCase):
    def test_no_text_has_an_em_dash_and_the_formats_take_a_reference(self):
        for name in ("ERASURE_CONFIRM", "ERASURE_DIALOG", "ERASURE_REQUESTED", "ERASURE_KEPT", "ERASURE_EXISTING",
                     "ERASURE_CANCELLED", "ERASURE_NOTHING_TO_CANCEL", "ERASURE_FAILED", "ERASURE_SIGN_IN"):
            self.assertNotIn("\u2014", getattr(erasure, name), name)
        for name in ("ERASURE_REQUESTED", "ERASURE_EXISTING", "ERASURE_CANCELLED"):
            self.assertIn("DEL-222222", getattr(erasure, name).format(reference="DEL-222222"))
        self.assertTrue(erasure.ERASURE_CONFIRM.startswith(erasure.ERASURE_DIALOG))
        self.assertTrue(re.search(r"Reply DELETE to confirm", erasure.ERASURE_CONFIRM))
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure`
Expected: ERROR, `ImportError: cannot import name 'erasure'`

- [ ] **Step 3: Write the module**

```python
"""Self-service "delete my data" (spec 2026-10-01).

The chat (runtime erasure_gate) and the Amiigo app (POST /erasure-requests)
only record a request. The nightly job (erasure_job.py) is the only thing that
deletes. This module holds what they share: the phrases, the fixed replies,
the reference and the shape of the erasure_log audit record.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from typing import Any, Callable, Dict, Optional

# No 0/O, 1/I/L or U: a reference is read aloud and typed back.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_ATTEMPTS = 3

# ConversationState.erasure_step
WANTED = "wanted"
CANCEL_WANTED = "cancel_wanted"
CONFIRMING = "confirming"

# No \b around the Hindi: Devanagari vowel signs are not word characters.
_DELETE = re.compile(
    r"\b(?:delete|erase|remove)\s+my\s+(?:data|account|details|conversation\s+data|chat\s+data)\b"
    r"|\bforget\s+me\b"
    r"|मेरा\s+डेटा\s+(?:हटाओ|हटा\s+दो|डिलीट)"
    r"|\b(?:borrar|eliminar)\s+mis\s+datos\b|\beliminar\s+mi\s+cuenta\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"\bcancel\s+(?:my|the)\s+deletion\b|\b(?:don'?t|do\s+not)\s+delete\s+my\s+data\b",
    re.IGNORECASE,
)

ERASURE_DIALOG = (
    "This deletes everything this chat holds about you: your past chats with me, "
    "the photos and videos you sent, and the record of where you chatted from. "
    "It does not delete your warranty registration, orders, invoices or service "
    "tickets, which EMotorad keeps for your warranty and by law. It's done within "
    "30 days."
)
ERASURE_CONFIRM = ERASURE_DIALOG + " Reply DELETE to confirm, or anything else to keep your data."
ERASURE_REQUESTED = (
    "Your deletion request is {reference}. Everything this chat holds about you "
    "will be deleted in tonight's run and removed for good within 30 days. If you "
    "change your mind before then, say 'cancel my deletion'."
)
ERASURE_KEPT = "OK, nothing has been deleted."
ERASURE_EXISTING = (
    "You've already asked for this. Your request is {reference}, and it will be "
    "done in tonight's run."
)
ERASURE_CANCELLED = "Your deletion request {reference} is cancelled. Nothing has been deleted."
ERASURE_NOTHING_TO_CANCEL = "There's no deletion request to cancel."
ERASURE_FAILED = "I couldn't record your request just now. Please try again in a few minutes."
ERASURE_SIGN_IN = "Sign in to the app to delete your data."


def wants_cancel(text: Optional[str]) -> bool:
    return bool(_CANCEL.search(text or ""))


def wants_deletion(text: Optional[str]) -> bool:
    # "don't delete my data" holds "delete my data": a cancel is never a request.
    return bool(_DELETE.search(text or "")) and not wants_cancel(text)


def is_confirmation(text: Optional[str]) -> bool:
    return (text or "").strip().casefold() == "delete"


def new_reference(choice: Callable[[str], str] = secrets.choice) -> str:
    return "DEL-" + "".join(choice(REFERENCE_ALPHABET) for _ in range(6))


def key_sha256(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def audit_record(
    subject: str,
    reason: str,
    run_by: str,
    at: datetime,
    deleted: Optional[Dict[str, int]] = None,
    s3_objects: Optional[int] = None,
    s3_versions: Optional[int] = None,
    incomplete: bool = False,
) -> Dict[str, Any]:
    """An erasure_log record: who was erased, as a hash, and why. The shape
    scripts/delete_person.py and the nightly job both write."""
    record: Dict[str, Any] = {
        "key_sha256": key_sha256(subject),
        "kind": subject.split("#", 1)[0],
        "reason": reason,
        "run_by": run_by,
        "at": at,
    }
    if deleted is not None:
        record["deleted"] = deleted
    if incomplete:
        record["incomplete"] = True
    if s3_objects is not None:
        record["s3_objects"] = s3_objects
    if s3_versions is not None:
        record["s3_versions"] = s3_versions
    return record
```

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure`
Expected: `Ran 9 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/erasure.py tests/test_erasure.py
git commit -m "Erasure: the phrases, replies, reference and audit record"
```

---

### Task 2: The request record in both stores

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`InMemoryConversationStore`)
- Modify: `src/emotorad_ai/stores/mongo.py`
- Modify: `tests/store_contract.py`, `tests/test_mongo_store.py`

**Interfaces:**
- Consumes: `erasure.new_reference`, `erasure.key_sha256` (Task 1).
- Produces, on both stores: `conversations_of(user_key) -> List[str]` (the in-memory store gains it; MongoDB has it), `pending_erasure_of(user_key) -> Optional[Dict]`, `request_erasure(user_key, channel, conversation_id, now: str) -> str`, `cancel_erasure(user_key, now: str) -> Optional[str]`, `pending_erasures() -> List[Dict]` (oldest first), `record_erasure_failure(reference, error: str) -> int`, `close_erasure(reference, status, counts, error, now: str) -> None`, `erasure_record(reference) -> Optional[Dict]`, `log_erasure(entry: Dict) -> None`. MongoDB constants `ERASURE_REQUESTS = "erasure_requests"`, `ERASURE_LOG = "erasure_log"`. The in-memory store keeps `erasure_log: List[Dict]`.

- [ ] **Step 1: Write the failing contract tests**

Append to `class StoreContract` in `tests/store_contract.py`:

```python
    def test_one_pending_erasure_request_per_person(self):
        store = self.make_store()
        first = store.request_erasure("PHONE#+919700000031", "amiigo_app", "c1", "2026-10-01T10:00:00+00:00")
        again = store.request_erasure("PHONE#+919700000031", "website_chat", "c2", "2026-10-01T11:00:00+00:00")
        self.assertEqual(first, again)
        self.assertRegex(first, r"^DEL-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{6}$")
        record = store.pending_erasure_of("PHONE#+919700000031")
        self.assertEqual((record["_id"], record["status"], record["channel"], record["attempts"]),
                         (first, "pending", "amiigo_app", 0))
        self.assertIsNone(store.pending_erasure_of("PHONE#+919812345678"))

    def test_cancel_closes_the_request_without_naming_the_person(self):
        store = self.make_store()
        reference = store.request_erasure("PHONE#+919700000031", "amiigo_app", None, "2026-10-01T10:00:00+00:00")
        self.assertEqual(store.cancel_erasure("PHONE#+919700000031", "2026-10-01T10:05:00+00:00"), reference)
        self.assertIsNone(store.cancel_erasure("PHONE#+919700000031", "2026-10-01T10:06:00+00:00"))
        record = store.erasure_record(reference)
        self.assertEqual(record["status"], "cancelled")
        self.assertNotIn("user_key", record)
        self.assertEqual(record["key_sha256"], erasure.key_sha256("PHONE#+919700000031"))
        again = store.request_erasure("PHONE#+919700000031", "amiigo_app", None, "2026-10-02T10:00:00+00:00")
        self.assertNotEqual(again, reference)

    def test_pending_requests_come_oldest_first_and_failures_count(self):
        store = self.make_store()
        later = store.request_erasure("PHONE#+919812345678", "amiigo_app", None, "2026-10-01T12:00:00+00:00")
        earlier = store.request_erasure("PHONE#+919700000031", "amiigo_app", None, "2026-10-01T09:00:00+00:00")
        self.assertEqual([r["_id"] for r in store.pending_erasures()], [earlier, later])
        self.assertEqual(store.record_erasure_failure(earlier, "StorageError"), 1)
        self.assertEqual(store.record_erasure_failure(earlier, "StorageError"), 2)
        self.assertEqual(store.erasure_record(earlier)["last_error"], "StorageError")
        store.close_erasure(earlier, "done", {"conversations": 1}, None, "2026-10-02T20:30:00+00:00")
        self.assertEqual([r["_id"] for r in store.pending_erasures()], [later])
        self.assertEqual(store.erasure_record(earlier)["counts"], {"conversations": 1})

    def test_erasing_a_person_leaves_their_request_record(self):
        store = self.make_store()
        state = store.get("c1")
        state.user_key, state.turns = "PHONE#+919700000031", 1
        store.save(state)
        reference = store.request_erasure("PHONE#+919700000031", "amiigo_app", "c1", "2026-10-01T10:00:00+00:00")
        self.assertEqual(store.conversations_of("PHONE#+919700000031"), ["c1"])
        store.delete_person("PHONE#+919700000031")
        self.assertEqual(store.erasure_record(reference)["status"], "pending")

    def test_an_audit_record_is_kept(self):
        store = self.make_store()
        store.log_erasure({"key_sha256": "x" * 64, "kind": "PHONE", "reason": "self-service request DEL-222222"})
        self.assertIsNotNone(store)
```

Add `from emotorad_ai import erasure` to the imports of `tests/store_contract.py`.

In `tests/test_mongo_store.py`, `test_every_collection_gets_exactly_its_indexes_and_only_two_expire`: add `"erasure_requests"` to the expected set; `test_the_index_table_has_no_ttl_on_the_permanent_record`: add `"erasure_requests"` to the tuple. Add a MongoDB-only test to `MongoStoreTests`:

```python
    def test_the_audit_record_goes_to_erasure_log(self):
        store = self.make_store()
        store.log_erasure({"key_sha256": "x" * 64, "kind": "PHONE", "reason": "self-service request DEL-222222"})
        self.assertEqual(self.db["erasure_log"].count_documents({"kind": "PHONE"}), 1)
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_mongo_store tests.test_memory_store`
Expected: ERROR, `AttributeError: ... has no attribute 'request_erasure'`

- [ ] **Step 3: The in-memory store**

In `src/emotorad_ai/conversation.py` import `from . import erasure as erasure_rules` beside the other imports (check that `erasure.py` imports nothing from `conversation.py`; it does not).

In `InMemoryConversationStore.__init__`, after `self._origins = {}`:

```python
        # Self-service erasure requests (erasure.py): reference -> request.
        self._erasures: Dict[str, Dict[str, Any]] = {}
        # The erasure_log audit records the nightly job writes.
        self.erasure_log: List[Dict[str, Any]] = []
```

Replace the first lines of `delete_person` that build `mine` with a call to a new method, and add the methods after `origins_of`:

```python
    def conversations_of(self, user_key: str) -> List[str]:
        """Every conversation id tied to one person."""
        mine = {cid for cid, state in self._states.items() if state.user_key == user_key}
        mine |= {s.conversation_id for s in self._summaries.get(user_key, {}).values()}
        mine |= {cid for cid, runs in self._origins.items()
                 if any(r.get("user_key") == user_key for r in runs.values())}
        return sorted(mine)

    def pending_erasure_of(self, user_key: str) -> Optional[Dict[str, Any]]:
        return next((dict(r) for r in self._erasures.values()
                     if r.get("user_key") == user_key and r["status"] == "pending"), None)

    def request_erasure(self, user_key: str, channel: str, conversation_id: Optional[str], now: str) -> str:
        """One pending request per person: asking again returns it."""
        pending = self.pending_erasure_of(user_key)
        if pending is not None:
            return pending["_id"]
        reference = erasure_rules.new_reference()
        while reference in self._erasures:
            reference = erasure_rules.new_reference()
        self._erasures[reference] = {"_id": reference, "user_key": user_key, "status": "pending",
                                     "requested_at": now, "channel": channel, "conversation_id": conversation_id,
                                     "attempts": 0, "last_error": None}
        return reference

    def cancel_erasure(self, user_key: str, now: str) -> Optional[str]:
        pending = self.pending_erasure_of(user_key)
        if pending is None:
            return None
        self.close_erasure(pending["_id"], "cancelled", None, None, now)
        return pending["_id"]

    def pending_erasures(self) -> List[Dict[str, Any]]:
        pending = [dict(r) for r in self._erasures.values() if r["status"] == "pending"]
        return sorted(pending, key=lambda r: r["requested_at"])

    def record_erasure_failure(self, reference: str, error: str) -> int:
        record = self._erasures[reference]
        record["attempts"] += 1
        record["last_error"] = error
        return record["attempts"]

    def close_erasure(self, reference: str, status: str, counts: Optional[Dict[str, int]],
                      error: Optional[str], now: str) -> None:
        """Closed: the person's key is replaced by its hash."""
        record = self._erasures[reference]
        user_key = record.pop("user_key", None)
        if user_key:
            record["key_sha256"] = erasure_rules.key_sha256(user_key)
        record.update(status=status, processed_at=now, counts=counts)
        if error is not None:
            record["last_error"] = error

    def erasure_record(self, reference: str) -> Optional[Dict[str, Any]]:
        record = self._erasures.get(reference)
        return dict(record) if record else None

    def log_erasure(self, entry: Dict[str, Any]) -> None:
        self.erasure_log.append(dict(entry))
```

and `delete_person` begins:

```python
        mine = set(self.conversations_of(user_key))
```

- [ ] **Step 4: The MongoDB store**

In `src/emotorad_ai/stores/mongo.py`, import `from .. import erasure as erasure_rules`. After `CONVERSATION_ORIGINS`:

```python
# Self-service erasure requests (erasure.py), and the audit log every erasure
# writes. Neither is erased with the person: a closed request holds only a hash.
ERASURE_REQUESTS = "erasure_requests"
ERASURE_LOG = "erasure_log"
```

In `INDEXES`:

```python
    ERASURE_REQUESTS: [
        ([("user_key", 1), ("status", 1)], {"name": "user_status"}),
        ([("status", 1), ("requested_at", 1)], {"name": "status_requested"}),
    ],
```

Methods after `origins_of`:

```python
    def pending_erasure_of(self, user_key: str) -> Optional[Dict[str, Any]]:
        requests = self._collection(ERASURE_REQUESTS)
        return self._guard("find_one", lambda: requests.find_one({"user_key": user_key, "status": "pending"}))

    def request_erasure(self, user_key: str, channel: str, conversation_id: Optional[str], now: str) -> str:
        """One pending request per person: asking again returns it."""
        pending = self.pending_erasure_of(user_key)
        if pending is not None:
            return pending["_id"]
        reference = erasure_rules.new_reference()
        record = {"_id": reference, "user_key": user_key, "status": "pending", "requested_at": now,
                  "channel": channel, "conversation_id": conversation_id, "attempts": 0, "last_error": None}
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("insert_one", lambda: requests.insert_one(record))
        return reference

    def cancel_erasure(self, user_key: str, now: str) -> Optional[str]:
        pending = self.pending_erasure_of(user_key)
        if pending is None:
            return None
        self.close_erasure(pending["_id"], "cancelled", None, None, now)
        return pending["_id"]

    def pending_erasures(self) -> List[Dict[str, Any]]:
        requests = self._collection(ERASURE_REQUESTS)
        return self._guard("find", lambda: list(requests.find({"status": "pending"}).sort("requested_at", 1)))

    def record_erasure_failure(self, reference: str, error: str) -> int:
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$inc": {"attempts": 1}, "$set": {"last_error": error}}))
        return self._guard("find_one", lambda: requests.find_one({"_id": reference}))["attempts"]

    def close_erasure(self, reference: str, status: str, counts: Optional[Dict[str, int]],
                      error: Optional[str], now: str) -> None:
        """Closed: the person's key is replaced by its hash."""
        requests = self._collection(ERASURE_REQUESTS)
        record = self._guard("find_one", lambda: requests.find_one({"_id": reference})) or {}
        fields: Dict[str, Any] = {"status": status, "processed_at": now, "counts": counts}
        if record.get("user_key"):
            fields["key_sha256"] = erasure_rules.key_sha256(record["user_key"])
        if error is not None:
            fields["last_error"] = error
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$set": fields, "$unset": {"user_key": ""}}))

    def erasure_record(self, reference: str) -> Optional[Dict[str, Any]]:
        requests = self._collection(ERASURE_REQUESTS)
        return self._guard("find_one", lambda: requests.find_one({"_id": reference}))

    def log_erasure(self, entry: Dict[str, Any]) -> None:
        log = self._collection(ERASURE_LOG)
        self._guard("insert_one", lambda: log.insert_one(dict(entry)))
```

- [ ] **Step 5: Run the store tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_mongo_store tests.test_memory_store tests.test_mongo_scripts tests.test_audit_persistence`
Expected: OK.

- [ ] **Step 6: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/stores/mongo.py tests/store_contract.py tests/test_mongo_store.py
git commit -m "Stores: erasure requests, one pending per person, closed without the person's key"
```

---

### Task 3: The chat flow (runtime and graph)

**Files:**
- Modify: `src/emotorad_ai/graph.py` (`TurnNodes`, `NODE_NAMES`, edges)
- Modify: `src/emotorad_ai/conversation.py` (`ConversationState.erasure_step`)
- Modify: `src/emotorad_ai/runtime.py` (`TurnNodes(...)`, `_node_erasure` and helpers, `_node_verify`)
- Test: `tests/test_erasure_chat.py`

**Interfaces:**
- Consumes: Task 1's functions and texts; Task 2's `pending_erasure_of`, `request_erasure`, `cancel_erasure`.
- Produces: `ConversationState.erasure_step: Optional[str]`; handled_by labels `erasure:confirm`, `erasure:requested`, `erasure:kept`, `erasure:existing`, `erasure:cancelled`, `erasure:nothing_to_cancel`, `erasure:failed`; events `erasure_requested`, `erasure_cancelled`, `erasure_kept`, `erasure_request_failed`.

- [ ] **Step 1: Write the failing tests**

```python
"""Delete my data, in the chat (spec 2026-10-01)."""

import unittest
from unittest import mock

from emotorad_ai import erasure
from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.llm import say
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

APP_RIDER = Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1")


class AppChat(Chat):
    """A verified rider, as the Amiigo app sends them."""

    def ask(self, text):
        return self.say(text, identity=APP_RIDER)


def new_chat():
    return AppChat(replies=[say("Is the charger light on?")] * 6)


class VerifiedRiderTests(unittest.TestCase):
    def test_ask_confirm_and_get_a_reference(self):
        chat = new_chat()
        offered = chat.ask("delete my data")
        self.assertEqual(offered.handled_by, "erasure:confirm")
        self.assertIn(erasure.ERASURE_CONFIRM, offered.text)
        done = chat.ask("DELETE")
        self.assertEqual(done.handled_by, "erasure:requested")
        pending = chat.conversations.pending_erasure_of("PHONE#" + RIDER)
        self.assertEqual(done.text, erasure.ERASURE_REQUESTED.format(reference=pending["_id"]))
        events = [e for e in chat.log.events if e["event"] == "erasure_requested"]
        self.assertEqual(events[0]["reference"], pending["_id"])
        self.assertNotIn(RIDER, repr(events))

    def test_lower_case_delete_confirms(self):
        chat = new_chat()
        chat.ask("delete my data")
        self.assertEqual(chat.ask("delete").handled_by, "erasure:requested")

    def test_any_other_answer_keeps_the_data(self):
        chat = new_chat()
        chat.ask("delete my data")
        kept = chat.ask("no wait")
        self.assertEqual((kept.handled_by, kept.text), ("erasure:kept", erasure.ERASURE_KEPT))
        self.assertIsNone(chat.conversations.pending_erasure_of("PHONE#" + RIDER))
        self.assertNotEqual(chat.ask("DELETE").handled_by, "erasure:requested")

    def test_delete_without_being_asked_is_an_ordinary_message(self):
        chat = new_chat()
        self.assertFalse(chat.ask("DELETE").handled_by.startswith("erasure:"))

    def test_asking_again_gives_the_same_reference(self):
        chat = new_chat()
        chat.ask("delete my data")
        chat.ask("DELETE")
        reference = chat.conversations.pending_erasure_of("PHONE#" + RIDER)["_id"]
        again = chat.ask("please delete my account")
        self.assertEqual((again.handled_by, again.text),
                         ("erasure:existing", erasure.ERASURE_EXISTING.format(reference=reference)))

    def test_cancel_then_nothing_to_cancel(self):
        chat = new_chat()
        chat.ask("delete my data")
        chat.ask("DELETE")
        reference = chat.conversations.pending_erasure_of("PHONE#" + RIDER)["_id"]
        cancelled = chat.ask("cancel my deletion")
        self.assertEqual(cancelled.text, erasure.ERASURE_CANCELLED.format(reference=reference))
        self.assertEqual(chat.conversations.erasure_record(reference)["status"], "cancelled")
        self.assertEqual(chat.ask("cancel my deletion").text, erasure.ERASURE_NOTHING_TO_CANCEL)

    def test_a_store_failure_asks_the_rider_to_try_again(self):
        chat = new_chat()
        chat.ask("delete my data")
        with mock.patch.object(chat.conversations, "request_erasure", side_effect=StoreUnavailable("down")):
            failed = chat.ask("DELETE")
        self.assertEqual((failed.handled_by, failed.text), ("erasure:failed", erasure.ERASURE_FAILED))
        (event,) = [e for e in chat.log.events if e["event"] == "erasure_request_failed"]
        self.assertEqual(event["error"], "StoreUnavailable")

    def test_safety_and_handoff_come_first(self):
        chat = new_chat()
        self.assertEqual(chat.ask("my battery is smoking, delete my data").handled_by, "guardrail:battery_safety")
        chat = new_chat()
        self.assertEqual(chat.ask("talk to a person and delete my data").handled_by, "guardrail:human_handoff")


class WebsiteVisitorTests(unittest.TestCase):
    def test_verify_then_the_confirmation_not_the_bike_list(self):
        chat = new_chat()
        self.assertEqual(chat.say("delete my data").handled_by, "verify_first:ask_number")
        chat.say(ONE_BIKE[3:])
        verified = chat.say(chat.code())
        self.assertTrue(verified.handled_by.startswith("verify_first:verified"), verified.handled_by)
        self.assertIn("Thanks, that's confirmed.", verified.text)
        self.assertIn(erasure.ERASURE_CONFIRM, verified.text)
        self.assertNotIn("I found", verified.text)
        self.assertEqual(chat.say("DELETE").handled_by, "erasure:requested")
        self.assertIsNotNone(chat.conversations.pending_erasure_of("PHONE#" + ONE_BIKE))

    def test_a_deletion_phrase_while_a_code_is_awaited(self):
        chat = new_chat()
        chat.say("my battery isn't charging")
        chat.say(ONE_BIKE[3:])
        self.assertEqual(chat.say("actually, delete my data").handled_by, "verify_first:ask_code")
        verified = chat.say(chat.code())
        self.assertIn(erasure.ERASURE_CONFIRM, verified.text)

    def test_cancel_after_verifying(self):
        chat = new_chat()
        reference = chat.conversations.request_erasure("PHONE#" + ONE_BIKE, "amiigo_app", None,
                                                       "2026-10-01T09:00:00+00:00")
        chat.say("cancel my deletion")
        chat.say(ONE_BIKE[3:])
        verified = chat.say(chat.code())
        self.assertIn(erasure.ERASURE_CANCELLED.format(reference=reference), verified.text)
        self.assertEqual(chat.conversations.erasure_record(reference)["status"], "cancelled")
```

In `tests/test_graph.py` nothing changes: it checks that `NODE_NAMES` are all built.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_chat`
Expected: FAIL: `handled_by` is never `erasure:...`.

- [ ] **Step 3: The state field and the graph**

In `ConversationState`, after `origin`:

```python
    # Self-service erasure (erasure.py): "wanted" or "cancel_wanted" while the
    # verify step runs, "confirming" while the bot waits for DELETE.
    erasure_step: Optional[str] = None
```

In `src/emotorad_ai/graph.py`: add `erasure_gate: Node` to `TurnNodes` after `handoff_gate`; in `NODE_NAMES` put `"erasure_gate"` after `"handoff_gate"`; replace the handoff edge and add one:

```python
    graph.add_conditional_edges("handoff_gate", _replied_or("erasure_gate"), ["erasure_gate", END])
    graph.add_conditional_edges("erasure_gate", _replied_or("verify_gate"), ["verify_gate", END])
```

- [ ] **Step 4: The runtime node**

In `src/emotorad_ai/runtime.py`: `from . import erasure as erasure_rules`; `from .verify_first import CONFIRMED` beside the existing verify_first import; make sure `utc_now_iso` is imported from `.conversation`. In `TurnNodes(...)` add `erasure_gate=self._node_erasure,` after `handoff_gate`.

New methods after `_node_handoff`:

```python
    def _node_erasure(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 2b. Delete my data (erasure.py). A customer's request is recorded,
        #     never carried out here: the nightly job deletes.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if resolved.persona != "customer":
            return {}
        text = message.message_text or ""
        user_key = state.user_key or self._user_key(resolved)
        if state.erasure_step == erasure_rules.CONFIRMING and user_key:
            state.erasure_step = None
            if erasure_rules.is_confirmation(text):
                reply, label = self._erasure_request(message, state, user_key)
                return {"reply": self._finish(message, state, reply, label)}
            self.log.emit("erasure_kept", message.conversation_id)
            return {"reply": self._finish(message, state, erasure_rules.ERASURE_KEPT, "erasure:kept")}
        cancel = erasure_rules.wants_cancel(text)
        if not cancel and not erasure_rules.wants_deletion(text):
            return {}
        if user_key is None:
            # Not verified: the verify step asks for the number, and its
            # verified reply carries this on (_node_verify).
            if self.verify_gate is not None and self.verify_gate.applies(resolved):
                state.erasure_step = erasure_rules.CANCEL_WANTED if cancel else erasure_rules.WANTED
            return {}
        reply, label = (self._erasure_cancel(message, state, user_key) if cancel
                        else self._erasure_offer(message, state, user_key))
        return {"reply": self._finish(message, state, reply, label)}

    def _erasure_offer(self, message: InboundMessage, state: ConversationState, user_key: str) -> Tuple[str, str]:
        try:
            pending = self.conversations.pending_erasure_of(user_key)
        except Exception as exc:
            return self._erasure_failed(message, state, exc)
        if pending is not None:
            return erasure_rules.ERASURE_EXISTING.format(reference=pending["_id"]), "erasure:existing"
        state.erasure_step = erasure_rules.CONFIRMING
        return erasure_rules.ERASURE_CONFIRM, "erasure:confirm"

    def _erasure_request(self, message: InboundMessage, state: ConversationState, user_key: str) -> Tuple[str, str]:
        try:
            reference = self.conversations.request_erasure(user_key, message.channel, message.conversation_id,
                                                           utc_now_iso())
        except Exception as exc:
            return self._erasure_failed(message, state, exc)
        self.log.emit("erasure_requested", message.conversation_id, reference=reference)
        return erasure_rules.ERASURE_REQUESTED.format(reference=reference), "erasure:requested"

    def _erasure_cancel(self, message: InboundMessage, state: ConversationState, user_key: str) -> Tuple[str, str]:
        try:
            reference = self.conversations.cancel_erasure(user_key, utc_now_iso())
        except Exception as exc:
            return self._erasure_failed(message, state, exc)
        if reference is None:
            return erasure_rules.ERASURE_NOTHING_TO_CANCEL, "erasure:nothing_to_cancel"
        self.log.emit("erasure_cancelled", message.conversation_id, reference=reference)
        return erasure_rules.ERASURE_CANCELLED.format(reference=reference), "erasure:cancelled"

    def _erasure_failed(self, message: InboundMessage, state: ConversationState, exc: Exception) -> Tuple[str, str]:
        state.erasure_step = None
        self.log.emit("erasure_request_failed", message.conversation_id, error=type(exc).__name__)
        return erasure_rules.ERASURE_FAILED, "erasure:failed"
```

In `_node_verify`, after `gate = self.verify_gate.handle(message, state)` and its escalation log, compute the text the reply uses:

```python
        text = gate.text
        if gate.resolved is not None and state.erasure_step in (erasure_rules.WANTED, erasure_rules.CANCEL_WANTED):
            # Verified for a deletion or a cancel asked before the number:
            # that, not the bike list (spec 2026-10-01).
            wanted, state.erasure_step = state.erasure_step, None
            user_key = self._user_key(gate.resolved)
            if user_key is not None:
                follow, _ = (self._erasure_cancel(message, state, user_key) if wanted == erasure_rules.CANCEL_WANTED
                             else self._erasure_offer(message, state, user_key))
                text = CONFIRMED + " " + follow
```

and pass `text` instead of `gate.text` to `self._finish(...)` in that node.

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_chat tests.test_graph tests.test_verify_first`
Expected: OK.

- [ ] **Step 6: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/graph.py src/emotorad_ai/conversation.py src/emotorad_ai/runtime.py tests/test_erasure_chat.py
git commit -m "Chat: delete my data, confirmed with DELETE, through verification on the website"
```

---

### Task 4: The nightly job, S3 hide, and the shared audit record

**Files:**
- Modify: `src/emotorad_ai/storage/s3.py` (`S3Store.hide`)
- Create: `src/emotorad_ai/erasure_job.py`
- Modify: `scripts/delete_person.py` (use `erasure.audit_record`)
- Test: `tests/test_erasure_job.py`, `tests/test_storage_s3.py`

**Interfaces:**
- Consumes: Task 1 (`audit_record`, `MAX_ATTEMPTS`); Task 2 (`pending_erasures`, `conversations_of`, `media_of`, `delete_person`, `log_erasure`, `close_erasure`, `record_erasure_failure`).
- Produces: `S3Store.hide(key) -> None` (raises `StorageError`); `erasure_job.Outcome(reference, status, counts, s3_objects, error)`; `erasure_job.process(store, media_store, now) -> List[Outcome]`; `erasure_job.main() -> int`; `RUN_BY = "nightly erasure job"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_erasure_job.py`:

```python
"""The nightly erasure job (spec 2026-10-01): files hidden first, then the
records, the audit record, and the request closed."""

import unittest
from datetime import datetime, timezone

from emotorad_ai import erasure
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.erasure_job import RUN_BY, process
from emotorad_ai.storage.s3 import StorageError

NOW = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
ME, THEM = "PHONE#+919700000031", "PHONE#+919812345678"


class FakeMedia:
    def __init__(self, fail_on=None, store=None):
        self.hidden, self.fail_on, self.store = [], fail_on, store
        self.records_left_when_hiding = []

    def hide(self, key):
        if self.store is not None:
            self.records_left_when_hiding.append(len(self.store.conversations_of(ME)))
        if key == self.fail_on:
            raise StorageError("hide %r failed: AccessDenied" % key)
        self.hidden.append(key)


def person(store, user_key, cid, keys=()):
    state = store.get(cid)
    state.user_key, state.turns = user_key, 1
    store.save(state)
    for n, key in enumerate(keys):
        store.record_media({"_id": key, "key": key, "conversation_id": cid, "stored_at": "2026-10-01T0%d" % n})


class JobTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryConversationStore()
        person(self.store, ME, "mine", ["customers/a/mine/images/1.jpg", "customers/a/mine/images/2.jpg"])
        person(self.store, THEM, "theirs", ["customers/b/theirs/images/1.jpg"])
        self.reference = self.store.request_erasure(ME, "amiigo_app", "mine", "2026-10-01T10:00:00+00:00")

    def test_a_request_is_carried_out_and_closed(self):
        media = FakeMedia(store=self.store)
        (outcome,) = process(self.store, media, lambda: NOW)
        self.assertEqual((outcome.reference, outcome.status, outcome.s3_objects), (self.reference, "done", 2))
        self.assertEqual(media.hidden, ["customers/a/mine/images/1.jpg", "customers/a/mine/images/2.jpg"])
        self.assertEqual(media.records_left_when_hiding, [1, 1])  # files first, records after
        self.assertEqual(self.store.conversations_of(ME), [])
        self.assertEqual(self.store.conversations_of(THEM), ["theirs"])
        (audit,) = self.store.erasure_log
        self.assertEqual(audit, erasure.audit_record(ME, "self-service request %s" % self.reference, RUN_BY, NOW,
                                                     deleted=outcome.counts, s3_objects=2))
        record = self.store.erasure_record(self.reference)
        self.assertEqual(record["status"], "done")
        self.assertNotIn("user_key", record)

    def test_a_hide_failure_keeps_the_records_and_counts_an_attempt(self):
        media = FakeMedia(fail_on="customers/a/mine/images/2.jpg")
        (outcome,) = process(self.store, media, lambda: NOW)
        self.assertEqual((outcome.status, outcome.error), ("retry", "StorageError"))
        self.assertEqual(self.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.store.erasure_record(self.reference)["attempts"], 1)
        self.assertEqual(self.store.erasure_log, [])

    def test_the_third_failure_closes_it_failed(self):
        media = FakeMedia(fail_on="customers/a/mine/images/1.jpg")
        for _ in range(erasure.MAX_ATTEMPTS):
            (outcome,) = process(self.store, media, lambda: NOW)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(self.store.erasure_record(self.reference)["status"], "failed")
        self.assertEqual(process(self.store, media, lambda: NOW), [])

    def test_a_cancelled_request_is_left_alone(self):
        self.store.cancel_erasure(ME, "2026-10-01T11:00:00+00:00")
        self.assertEqual(process(self.store, FakeMedia(), lambda: NOW), [])
        self.assertEqual(self.store.conversations_of(ME), ["mine"])

    def test_files_without_a_bucket_are_a_failure_not_a_skip(self):
        (outcome,) = process(self.store, None, lambda: NOW)
        self.assertEqual(outcome.status, "retry")
        self.assertEqual(self.store.conversations_of(ME), ["mine"])

    def test_a_person_with_no_files_needs_no_bucket(self):
        store = InMemoryConversationStore()
        person(store, ME, "mine")
        store.request_erasure(ME, "amiigo_app", None, "2026-10-01T10:00:00+00:00")
        (outcome,) = process(store, None, lambda: NOW)
        self.assertEqual((outcome.status, outcome.s3_objects), ("done", 0))
```

Append to `tests/test_storage_s3.py`:

```python
class HideTests(unittest.TestCase):
    def test_a_plain_delete_with_no_version(self):
        c = client()
        stub = Stubber(c)
        stub.add_response("delete_object", {}, {"Bucket": "b", "Key": "customers/a/c/images/1.jpg"})
        with stub:
            S3Store("b", client=c).hide("customers/a/c/images/1.jpg")
        stub.assert_no_pending_responses()

    def test_a_refusal_is_a_storage_error_naming_the_class(self):
        c = client()
        stub = Stubber(c)
        stub.add_client_error("delete_object", service_error_code="AccessDenied", http_status_code=403)
        with stub, self.assertRaises(StorageError) as raised:
            S3Store("b", client=c).hide("customers/a/c/images/1.jpg")
        self.assertIn("ClientError", str(raised.exception))
```

(Use the names `S3Store`, `StorageError`, `Stubber`, `client` already imported in that file; add any that are missing.)

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_job tests.test_storage_s3`
Expected: ERROR, `No module named 'emotorad_ai.erasure_job'`; `S3Store` has no `hide`.

- [ ] **Step 3: `S3Store.hide`**

After `delete_every_version` in `src/emotorad_ai/storage/s3.py`:

```python
    def hide(self, key: str) -> None:
        """A plain delete: the object disappears now, and the bucket's
        lifecycle erases its versions for good within 30 days
        (infra/media.yaml). Needs only s3:DeleteObject. Called by the nightly
        erasure job (erasure_job.py), never by the chat."""
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError("hide %r failed: %s" % (key, type(exc).__name__)) from None
```

- [ ] **Step 4: The job**

`src/emotorad_ai/erasure_job.py`:

```python
"""The nightly erasure job (spec 2026-10-01).

    python -m emotorad_ai.erasure_job

Every pending self-service request, oldest first: the person's files hidden in
S3 (the lifecycle erases them within 30 days), then their records deleted, an
erasure_log audit record written and the request closed. A request whose files
could not all be hidden keeps its records, counts an attempt and is tried again
next night; at MAX_ATTEMPTS it is closed as failed. Exits 1 when any request
did not finish, so the GitHub run turns red.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import erasure
from .storage.s3 import StorageError

RUN_BY = "nightly erasure job"


@dataclass(frozen=True)
class Outcome:
    reference: str
    status: str  # "done" | "retry" | "failed"
    counts: Optional[Dict[str, int]]
    s3_objects: int
    error: Optional[str]


def process(store: Any, media_store: Any, now: Callable[[], datetime]) -> List[Outcome]:
    outcomes: List[Outcome] = []
    for request in store.pending_erasures():
        reference, user_key = request["_id"], request["user_key"]
        try:
            keys = [record["key"] for cid in store.conversations_of(user_key) for record in store.media_of(cid)]
            if keys and media_store is None:
                raise StorageError("no media bucket configured")
            for key in keys:
                media_store.hide(key)
            counts = store.delete_person(user_key)
            store.log_erasure(erasure.audit_record(user_key, "self-service request %s" % reference, RUN_BY, now(),
                                                   deleted=counts, s3_objects=len(keys)))
            store.close_erasure(reference, "done", counts, None, now().isoformat())
            outcomes.append(Outcome(reference, "done", counts, len(keys), None))
        except Exception as exc:
            error = type(exc).__name__
            attempts = store.record_erasure_failure(reference, error)
            status = "failed" if attempts >= erasure.MAX_ATTEMPTS else "retry"
            if status == "failed":
                store.close_erasure(reference, "failed", None, error, now().isoformat())
            outcomes.append(Outcome(reference, status, None, 0, error))
    return outcomes


def main() -> int:
    from .config_store import load_into_environ
    from .storage.s3 import store_from_env
    from .stores.mongo import MongoConversationStore, connect

    load_into_environ()
    store = MongoConversationStore(connect(db_name=os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai")))
    outcomes = process(store, store_from_env(), lambda: datetime.now(timezone.utc))
    for outcome in outcomes:
        detail = outcome.error or json.dumps(outcome.counts, sort_keys=True)
        print("%s %s files=%d %s" % (outcome.reference, outcome.status, outcome.s3_objects, detail))
    print("erasure requests processed: %d" % len(outcomes))
    return 1 if any(outcome.status != "done" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: The script shares the audit record**

In `scripts/delete_person.py`, import `from emotorad_ai.erasure import audit_record  # noqa: E402` beside the other `emotorad_ai` imports, and replace both inline dicts:

```python
            db[ERASURE_LOG].insert_one(audit_record(
                subject, redact_pii(args.reason.strip()), getpass.getuser(), datetime.now(timezone.utc),
                s3_objects=s3_objects, s3_versions=s3_versions, incomplete=True,
            ))
```

```python
    audit = audit_record(
        subject, redact_pii(args.reason.strip()),  # a reason can quote a number
        getpass.getuser(), datetime.now(timezone.utc), deleted=deleted,
        s3_objects=s3_objects if media else None, s3_versions=s3_versions if media else None,
    )
    db[ERASURE_LOG].insert_one(audit)
```

Remove the `hashlib` import if nothing else uses it.

- [ ] **Step 6: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_job tests.test_storage_s3 tests.test_mongo_scripts tests.test_audit_persistence`
Expected: OK.

- [ ] **Step 7: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/storage/s3.py src/emotorad_ai/erasure_job.py scripts/delete_person.py tests/test_erasure_job.py tests/test_storage_s3.py
git commit -m "Nightly erasure job: hide the files, delete the records, audit, close"
```

---

### Task 5: The Amiigo app's endpoints

**Files:**
- Modify: `src/emotorad_ai/api.py`
- Test: `tests/test_api_erasure.py`

**Interfaces:**
- Consumes: Task 1 texts; Task 2 store methods; `resolver.resolve_website`; `message_limiter`, `client_ip`, `TRUSTED_PROXIES`.
- Produces: `POST /erasure-requests`, `POST /erasure-requests/status`, `POST /erasure-requests/cancel`.

- [ ] **Step 1: Write the failing tests**

```python
"""The Amiigo app's "Delete my conversation data" button (spec 2026-10-01)."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai import erasure
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.tools.fixtures import PHONE_AMIIGO_TEST_RIDER
from tests.test_api_health import fresh_api

RIDER_KEY = "PHONE#" + PHONE_AMIIGO_TEST_RIDER


class ErasureEndpointTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.client = TestClient(self.api.app)

    def post(self, path, **body):
        return self.client.post(path, json=dict({"session_token": "sess-amiigo-test"}, **body))

    def test_a_signed_in_rider_asks_once(self):
        first = self.post("/erasure-requests", confirm=True)
        self.assertEqual(first.status_code, 201, first.text)
        reference = first.json()["reference"]
        self.assertEqual(first.json(), {"reference": reference, "status": "pending",
                                        "text": erasure.ERASURE_REQUESTED.format(reference=reference)})
        again = self.post("/erasure-requests", confirm=True)
        self.assertEqual((again.status_code, again.json()["reference"]), (200, reference))
        self.assertEqual(again.json()["text"], erasure.ERASURE_EXISTING.format(reference=reference))

    def test_without_confirm_nothing_is_recorded(self):
        self.assertEqual(self.post("/erasure-requests").status_code, 400)
        self.assertIsNone(self.api.stores.conversations.pending_erasure_of(RIDER_KEY))

    def test_an_unknown_session_is_refused(self):
        for token in ("sess-nobody", ""):
            r = self.client.post("/erasure-requests", json={"session_token": token, "confirm": True})
            self.assertEqual((r.status_code, r.json()["detail"]), (403, erasure.ERASURE_SIGN_IN))

    def test_status_then_cancel_then_nothing_to_cancel(self):
        self.assertEqual(self.post("/erasure-requests/status").json(), {"reference": None, "status": "none"})
        reference = self.post("/erasure-requests", confirm=True).json()["reference"]
        status = self.post("/erasure-requests/status").json()
        self.assertEqual((status["reference"], status["status"]), (reference, "pending"))
        self.assertIn("requested_at", status)
        cancelled = self.post("/erasure-requests/cancel")
        self.assertEqual(cancelled.json(), {"reference": reference, "status": "cancelled",
                                            "text": erasure.ERASURE_CANCELLED.format(reference=reference)})
        nothing = self.post("/erasure-requests/cancel")
        self.assertEqual((nothing.status_code, nothing.json()["detail"]), (404, erasure.ERASURE_NOTHING_TO_CANCEL))

    def test_a_request_made_in_the_chat_is_the_one_the_app_sees(self):
        reference = self.api.stores.conversations.request_erasure(RIDER_KEY, "website_chat", "c9",
                                                                  "2026-10-01T09:00:00+00:00")
        self.assertEqual(self.post("/erasure-requests/status").json()["reference"], reference)
        self.assertEqual(self.post("/erasure-requests", confirm=True).json()["reference"], reference)

    def test_a_store_failure_is_503_and_logged_without_the_token(self):
        with mock.patch.object(self.api.stores.conversations, "pending_erasure_of",
                               side_effect=StoreUnavailable("down")):
            r = self.post("/erasure-requests", confirm=True)
        self.assertEqual((r.status_code, r.json()["detail"]), (503, erasure.ERASURE_FAILED))
        (event,) = [e for e in self.api.log.events if e["event"] == "erasure_request_failed"]
        self.assertEqual(event["error"], "StoreUnavailable")
        self.assertNotIn("sess-amiigo-test", repr(self.api.log.events))
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_erasure`
Expected: FAIL, 404 for every endpoint.

- [ ] **Step 3: The endpoints**

In `src/emotorad_ai/api.py`, import `from . import erasure as erasure_rules` beside the other local imports. After `post_message`:

```python
class ErasureIn(BaseModel):
    session_token: str = ""
    confirm: bool = False
    conversation_id: Optional[str] = None


def _erasure_person(request: Request, session_token: str) -> str:
    """The signed-in rider behind the session, or 403. Never from the URL."""
    if not message_limiter.allow(client_ip(request, TRUSTED_PROXIES)):
        raise HTTPException(status_code=429, detail="Too many requests. Wait a moment and try again.")
    persona, identity = resolver.resolve_website(None, session_token or None)
    if persona != "customer" or not identity.may_disclose or not identity.phone:
        raise HTTPException(status_code=403, detail=erasure_rules.ERASURE_SIGN_IN)
    return "PHONE#" + identity.phone


def _erasure_store_down(exc: Exception, conversation_id: Optional[str]) -> HTTPException:
    log.emit("erasure_request_failed", conversation_id or "erasure", error=type(exc).__name__)
    return HTTPException(status_code=503, detail=erasure_rules.ERASURE_FAILED)


@app.post("/erasure-requests", status_code=201)
def post_erasure_request(body: ErasureIn, request: Request, response: Response) -> Dict[str, Any]:
    """The Amiigo app's "Delete my conversation data" button, after its own
    confirmation dialog. Records a request; the nightly job deletes."""
    user_key = _erasure_person(request, body.session_token)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Send confirm: true once the rider has confirmed.")
    try:
        pending = stores.conversations.pending_erasure_of(user_key)
        reference = pending["_id"] if pending else stores.conversations.request_erasure(
            user_key, "amiigo_app", body.conversation_id, utc_now_iso())
    except Exception as exc:
        raise _erasure_store_down(exc, body.conversation_id) from None
    if pending:
        response.status_code = 200
        return {"reference": reference, "status": "pending",
                "text": erasure_rules.ERASURE_EXISTING.format(reference=reference)}
    log.emit("erasure_requested", body.conversation_id or "erasure", reference=reference)
    return {"reference": reference, "status": "pending",
            "text": erasure_rules.ERASURE_REQUESTED.format(reference=reference)}


@app.post("/erasure-requests/status")
def post_erasure_status(body: ErasureIn, request: Request) -> Dict[str, Any]:
    user_key = _erasure_person(request, body.session_token)
    try:
        pending = stores.conversations.pending_erasure_of(user_key)
    except Exception as exc:
        raise _erasure_store_down(exc, body.conversation_id) from None
    if pending is None:
        return {"reference": None, "status": "none"}
    return {"reference": pending["_id"], "status": "pending", "requested_at": pending["requested_at"]}


@app.post("/erasure-requests/cancel")
def post_erasure_cancel(body: ErasureIn, request: Request) -> Dict[str, Any]:
    user_key = _erasure_person(request, body.session_token)
    try:
        reference = stores.conversations.cancel_erasure(user_key, utc_now_iso())
    except Exception as exc:
        raise _erasure_store_down(exc, body.conversation_id) from None
    if reference is None:
        raise HTTPException(status_code=404, detail=erasure_rules.ERASURE_NOTHING_TO_CANCEL)
    log.emit("erasure_cancelled", body.conversation_id or "erasure", reference=reference)
    return {"reference": reference, "status": "cancelled",
            "text": erasure_rules.ERASURE_CANCELLED.format(reference=reference)}
```

- [ ] **Step 4: Run them to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_api_erasure`
Expected: `Ran 6 tests ... OK`

- [ ] **Step 5: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/api.py tests/test_api_erasure.py
git commit -m "API: the Amiigo app's delete-my-data endpoints"
```

---

### Task 6: The nightly workflow, the bucket permission, the contract and the docs

**Files:**
- Create: `.github/workflows/erasure-nightly.yml`
- Modify: `infra/media.yaml`
- Create: `docs/contracts/amiigo-support-chat.md` (from the Desktop copy) with a new section
- Modify: `CLAUDE.md`
- Test: `tests/test_erasure_workflow.py`, `tests/test_media_template.py`

**Interfaces:**
- Consumes: `python -m emotorad_ai.erasure_job` (Task 4); the endpoints (Task 5).

- [ ] **Step 1: Write the failing tests**

`tests/test_erasure_workflow.py`:

```python
"""The nightly erasure workflow: when it runs, what it runs, and no secret in it."""

import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "erasure-nightly.yml"


class ErasureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_nightly_at_two_in_india_and_by_hand(self):
        triggers = self.workflow[True]  # PyYAML reads the key "on" as True
        self.assertEqual(triggers["schedule"], [{"cron": "30 20 * * *"}])
        self.assertIn("workflow_dispatch", triggers)

    def test_it_runs_the_job_in_a_one_off_container_and_stops_on_error(self):
        self.assertIn("python -m emotorad_ai.erasure_job", self.text)
        self.assertIn("docker run --rm", self.text)
        self.assertIn('\\"set -e\\"', self.text)
        self.assertIn("EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app", self.text)

    def test_no_secret_value_is_in_it(self):
        for needle in ("sk-or-", "mongodb+srv://", "postgresql://", "AKIA"):
            self.assertNotIn(needle, self.text)
```

Append to `class MediaTemplateRetentionTests` in `tests/test_media_template.py`:

```python
    def test_the_server_may_only_plainly_delete_customer_files(self):
        statements = load_template()["Resources"]["MediaAccessPolicy"]["Properties"]["PolicyDocument"]["Statement"]
        deletes = [s for s in statements if "s3:DeleteObject" in (s["Action"] if isinstance(s["Action"], list)
                                                                  else [s["Action"]])]
        self.assertEqual(len(deletes), 1)
        self.assertIn("/customers/*", str(deletes[0]["Resource"]))
        for statement in statements:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            self.assertNotIn("s3:DeleteObjectVersion", actions)

    def test_delete_markers_left_after_30_days_are_removed(self):
        _, rules = _lifecycle_rules()
        self.assertTrue(any(_applies_to_customers(rule) and rule.get("ExpiredObjectDeleteMarker") is True
                            for rule in rules))
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_workflow tests.test_media_template`
Expected: ERROR (no workflow file) and FAIL (no delete statement, no marker rule).

- [ ] **Step 3: The workflow**

`.github/workflows/erasure-nightly.yml`:

```yaml
name: Nightly erasure (staging)

# Self-service "delete my data" requests, carried out each night
# (docs/superpowers/specs/2026-10-01-self-service-erasure-design.md).
# GitHub runs a schedule only from the default branch: until this file is on
# main, start it by hand (Actions -> this workflow -> Run workflow).
on:
  schedule:
    - cron: "30 20 * * *"  # 02:00 India time
  workflow_dispatch: {}

env:
  AWS_REGION: ap-south-1
  INSTANCE_ID: i-02e7dc2874e0fdacb

permissions:
  id-token: write  # OIDC role assumption, no stored AWS keys
  contents: read

jobs:
  erase:
    runs-on: ubuntu-latest
    environment: staging
    steps:
      - name: Assume the deploy role (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      # A one-off container from the deployed image. No secret is in this
      # command: the job reads the config store through the instance role.
      # Its output comes back here (default log driver), so the run shows
      # each request's outcome.
      - name: Run the erasure job on the instance (via SSM)
        run: |
          CMD_ID=$(aws ssm send-command \
            --instance-ids "$INSTANCE_ID" \
            --document-name "AWS-RunShellScript" \
            --comment "nightly erasure" \
            --parameters commands="[
              \"set -e\",
              \"docker run --rm -e AWS_REGION=$AWS_REGION -e EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app -e EMOTORAD_AI_MEDIA_BUCKET=emotorad-ai-stage-media emotorad-ai:stage python -m emotorad_ai.erasure_job\"
            ]" \
            --query 'Command.CommandId' --output text)

          for _ in $(seq 1 60); do
            STATUS=$(aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --query 'Status' --output text)
            [ "$STATUS" != "InProgress" ] && [ "$STATUS" != "Pending" ] && break
            sleep 5
          done

          aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
            --query '{Status:Status,Output:StandardOutputContent,Err:StandardErrorContent}'

          [ "$STATUS" = "Success" ]
```

- [ ] **Step 4: The bucket permission and lifecycle**

In `infra/media.yaml`, the `customer-evidence-old-versions-30d` rule gains, after its `NoncurrentVersionExpiration` block:

```yaml
            # A self-service erasure hides a file with a plain delete; once its
            # versions have expired, the delete marker left behind goes too.
            ExpiredObjectDeleteMarker: true
```

and `MediaAccessPolicy`'s statements gain:

```yaml
          # The nightly erasure job (erasure_job.py) hides a customer's files
          # with a plain delete. No s3:DeleteObjectVersion: the lifecycle
          # erases the versions within 30 days, so a mistake can be undone.
          - Effect: Allow
            Action: s3:DeleteObject
            Resource: !Sub arn:aws:s3:::emotorad-ai-${Environment}-media/customers/*
```

- [ ] **Step 5: The contract**

Copy `C:/Users/user/Desktop/Amiigo Support Chat API Contract.md` to `docs/contracts/amiigo-support-chat.md` (LF line endings). Insert this section before `## Errors, limits and retries`:

```markdown
## Deleting conversation data

The app's "Delete my conversation data" button lets a signed-in rider ask for
everything the support chat holds about them to be deleted: their past chats,
the photos and videos they sent, and the record of where they chatted from. It
does not delete their warranty registration, orders, invoices or service
tickets. The request is carried out by a nightly job and the data is gone for
good within 30 days. A rider can also ask in the chat ("delete my data"); both
make the same request, and a rider has at most one pending request.

**The dialog.** Show it before calling anything.

- Title: "Delete my conversation data?"
- Body: "This deletes everything this chat holds about you: your past chats with me, the photos and videos you sent, and the record of where you chatted from. It does not delete your warranty registration, orders, invoices or service tickets, which EMotorad keeps for your warranty and by law. It's done within 30 days."
- Buttons: "Delete" (calls `POST /erasure-requests` with `"confirm": true`) and "Keep my data" (closes the dialog).

**After a request**, show the reference and a "Cancel deletion" action until the request is done. `POST /erasure-requests/status` tells you whether one is pending.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /erasure-requests` | `{"session_token": "...", "confirm": true, "conversation_id": "..."}` (`conversation_id` optional) | 201 `{"reference": "DEL-7K3P9Q", "status": "pending", "text": "..."}`; 200 with the same shape when one was already pending; 400 without `"confirm": true` |
| `POST /erasure-requests/status` | `{"session_token": "..."}` | 200 `{"reference": "DEL-7K3P9Q", "status": "pending", "requested_at": "..."}`, or `{"reference": null, "status": "none"}` |
| `POST /erasure-requests/cancel` | `{"session_token": "..."}` | 200 `{"reference": "DEL-7K3P9Q", "status": "cancelled", "text": "..."}`; 404 when nothing is pending |

Show `text` to the rider as it comes. Other answers: 403 `{"detail": "Sign in to the app to delete your data."}` for a session that is not a signed-in rider; 429 over the message rate limit; 503 when the request could not be recorded (`detail` says to try again in a few minutes).

**Until the Amiigo token is wired**, send `session_token` in the body, as for `POST /message` (`sess-amiigo-test` on staging). The `/amiigo/v1/...` versions of these paths will take the rider's Amiigo token in the `Authorization` header instead; the bodies and answers stay the same. The token is never put in a URL.
```

Then copy the file back over the Desktop copy (LF kept), so both match.

- [ ] **Step 6: The docs**

In `CLAUDE.md`, after the "Where conversations come from" bullet:

```markdown
- **Delete my data, self-service** (spec 2026-10-01): a verified customer asks in the chat ("delete my data", confirmed by typing DELETE; on the website after verifying) or with the Amiigo app's button (`POST /erasure-requests`, `/status`, `/cancel`). Both only record a request in `erasure_requests` (one pending per person; closed records keep only `key_sha256`). `erasure_job.py` deletes, nightly from `.github/workflows/erasure-nightly.yml` through SSM in a one-off container: files hidden with a plain delete (the lifecycle erases them within 30 days; the instance role has `s3:DeleteObject` on `customers/*` only), then `delete_person`, an `erasure_log` record, and the request closed. Scheduled runs need the workflow on `main`; until then, run it by hand. The contract for the app: `docs/contracts/amiigo-support-chat.md`.
```

- [ ] **Step 7: Run the tests to see them pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_workflow tests.test_media_template`
Expected: OK.

- [ ] **Step 8: Run the whole suite, then commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add .github/workflows/erasure-nightly.yml infra/media.yaml docs/contracts/amiigo-support-chat.md CLAUDE.md tests/test_erasure_workflow.py tests/test_media_template.py
git commit -m "Nightly erasure workflow, plain delete on customer files, the app contract, docs"
```
