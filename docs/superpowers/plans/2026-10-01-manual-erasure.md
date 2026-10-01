# Manual Erasure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Self-service deletion requests are no longer deleted by a nightly job: a person reviews each one with an admin command in the app image and deletes it by hand, and a daily check reminds the team of old requests.

**Architecture:** Both conversation stores learn to record a request's proof, its reviews and a hold, and to count a person's data without deleting it. The runtime and the API record the proof when a request is made. A new module, `erasure_admin.py`, run in the live container through SSM, lists, shows, holds and deletes requests and runs the daily check. `erasure_job.py` goes, and the nightly workflow becomes a check that deletes nothing.

**Tech Stack:** Python 3.12, FastAPI, MongoDB (mongomock in tests), boto3, GitHub Actions, unittest.

**Spec:** `docs/superpowers/specs/2026-10-01-manual-erasure-design.md`

## Global Constraints

- Nothing deletes a self-service request without a person: only `erasure_admin delete` calls `S3Store.hide` and `delete_person` for one, and no schedule runs it.
- `delete` refuses, deleting nothing, unless the request is `pending`, the same name (trimmed, compared with `casefold`) ran `show` on it in the last 24 hours, the totals now equal that review's totals, and the reference typed back (trimmed, in capitals) is the request's.
- Files are hidden before any record is deleted. A hide failure leaves every record, and the request stays pending with `attempts` + 1 and `last_error`.
- `list` and `check` print no phone number, no chat text and no note. `check` exits 1 when any open request (held included) is 25 days old or more, or the store cannot be read.
- A closed request has no `user_key` (only `key_sha256`) and keeps `reviews`, `held` and `by`.
- An error is printed by its class name only, never its message (a driver's message can carry a host or a value).
- Customer texts exactly as spec section 3. British English, no em dashes, in code comments, docs, texts and output.
- A Claude session never runs `erasure_admin show` or `delete` against a real store: they print or remove customer data.
- Files are LF. Write them with Python `write_bytes`, or normalise CRLF to LF after the Write tool.
- Test command (whole suite): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;." python <workspace>/suite.py` (the suite minus `tests.test_video` and `tests.test_start`, Windows-only failures that pass on Linux CI; copy `suite.py` from the session scratchpad into the workspace).

## Spec corrections

- The spec says `VerificationStore` "already keeps `verified_at`". It does, but on the monotonic clock, which is not a time of day. Task 2 adds a wall-clock `verified_on` beside it, and the proof records that.

## Review Focus

1. The customer cancels while the person is at the "Type DEL-XXXXXX" prompt: delete re-reads the request after the prompt and stops (Task 4, `test_a_cancel_while_the_person_types_stops_it`).
2. A reference typed in lower case, on the command line or at the prompt: accepted (Task 4, `test_the_reference_in_lower_case_is_accepted`).
3. A chat whose working copy MongoDB dropped after 48 hours: still found through its summary or origin, and its transcript still shown (Task 3, `test_a_chat_whose_working_copy_expired_is_still_shown`).
4. A person with no files on a box with no bucket configured: delete still works; with files and no bucket, it is a hide failure (Task 4, `test_a_person_with_no_files_needs_no_bucket`, `test_files_and_no_bucket_is_a_hide_failure`).
5. The same person typing their name with other spaces or case at `delete` than at `show`: the review counts (Task 4, `test_the_same_name_in_another_case_counts`).

---

### Task 1: The stores record proof, reviews and holds, and count without deleting

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`InMemoryConversationStore`: `request_erasure`, `close_erasure`, `delete_person`; new `record_erasure_review`, `hold_erasure`, `erasure_history`)
- Modify: `src/emotorad_ai/stores/mongo.py` (`MongoConversationStore`: the same)
- Test: `tests/store_contract.py` (runs for both stores through `tests/test_memory_store.py` and `tests/test_mongo_store.py`)

**Interfaces:**
- Produces, on both stores:
  - `request_erasure(user_key: str, channel: str, conversation_id: Optional[str], now: str, proof: Optional[Dict[str, str]] = None) -> str`; the record gains `"proof": proof`.
  - `record_erasure_review(reference: str, by: str, at: str, totals: Dict[str, int]) -> None`: appends `{"by", "at", "totals"}` to the record's `reviews`.
  - `hold_erasure(reference: str, by: str, at: str, note: str) -> None`: sets `held` to `{"by", "at", "note"}`.
  - `erasure_history(user_key: str) -> List[Dict[str, Any]]`: every request whose `user_key` is this one or whose `key_sha256` is its hash, oldest `requested_at` first.
  - `close_erasure(reference, status, counts, error, now, by: Optional[str] = None) -> None`; sets `by` when given.
  - `delete_person(user_key: str, dry_run: bool = False) -> Dict[str, int]` (the in-memory store gains `dry_run`; MongoDB already has it).

- [ ] **Step 1: Write the failing tests**

Append to `class StoreContract` in `tests/store_contract.py`, after `test_an_audit_record_is_kept`:

```python
    def test_a_request_records_its_proof(self):
        store = self.make_store()
        otp = {"method": "otp", "verified_at": "2026-10-01T09:58:00+00:00"}
        reference = store.request_erasure("PHONE#+919700000031", "website_chat", "c1", "2026-10-01T10:00:00+00:00",
                                          proof=otp)
        self.assertEqual(store.erasure_record(reference)["proof"], otp)
        older = store.request_erasure("PHONE#+919812345678", "amiigo_app", None, "2026-10-01T10:00:00+00:00")
        self.assertIsNone(store.erasure_record(older).get("proof"))

    def test_reviews_and_a_hold_are_recorded_and_kept_when_closed(self):
        store = self.make_store()
        reference = store.request_erasure("PHONE#+919700000031", "amiigo_app", None, "2026-10-01T10:00:00+00:00")
        store.record_erasure_review(reference, "Asha", "2026-10-02T09:00:00+00:00", {"conversations": 1})
        store.record_erasure_review(reference, "Ravi", "2026-10-02T10:00:00+00:00", {"conversations": 2})
        store.hold_erasure(reference, "Asha", "2026-10-02T11:00:00+00:00", "safety case open")
        store.hold_erasure(reference, "Asha", "2026-10-02T12:00:00+00:00", "safety case EM-00001 open")
        record = store.erasure_record(reference)
        self.assertEqual(record["status"], "pending")
        self.assertEqual([(r["by"], r["at"], r["totals"]) for r in record["reviews"]],
                         [("Asha", "2026-10-02T09:00:00+00:00", {"conversations": 1}),
                          ("Ravi", "2026-10-02T10:00:00+00:00", {"conversations": 2})])
        self.assertEqual(record["held"], {"by": "Asha", "at": "2026-10-02T12:00:00+00:00",
                                          "note": "safety case EM-00001 open"})
        self.assertEqual(store.pending_erasure_of("PHONE#+919700000031")["_id"], reference)
        store.close_erasure(reference, "done", {"conversations": 2}, None, "2026-10-02T13:00:00+00:00", by="Asha")
        closed = store.erasure_record(reference)
        self.assertEqual((closed["status"], closed["by"], len(closed["reviews"]), closed["held"]["by"]),
                         ("done", "Asha", 2, "Asha"))
        self.assertNotIn("user_key", closed)

    def test_a_persons_history_has_open_and_closed_requests(self):
        store = self.make_store()
        me = "PHONE#+919700000031"
        first = store.request_erasure(me, "amiigo_app", None, "2026-09-01T10:00:00+00:00")
        store.cancel_erasure(me, "2026-09-01T11:00:00+00:00")
        store.request_erasure("PHONE#+919812345678", "amiigo_app", None, "2026-09-15T10:00:00+00:00")
        second = store.request_erasure(me, "website_chat", "c2", "2026-10-01T10:00:00+00:00")
        self.assertEqual([(r["_id"], r["status"]) for r in store.erasure_history(me)],
                         [(first, "cancelled"), (second, "pending")])

    def test_a_dry_run_counts_and_deletes_nothing(self):
        store = self.make_store()
        state = store.get("c1")
        state.user_key, state.turns = "PHONE#+919700000031", 1
        store.save(state)
        store.record_turn(state, inbound("my battery is dead", cid="c1"), reply("Is the light on?", cid="c1"))
        store.record_origin(self.origin("c1", user_key="PHONE#+919700000031"))
        counted = store.delete_person("PHONE#+919700000031", dry_run=True)
        self.assertEqual(store.conversations_of("PHONE#+919700000031"), ["c1"])
        self.assertEqual(len(store.transcript("c1")), 2)
        self.assertEqual(counted["transcript_turns"], 2)
        self.assertEqual(counted, store.delete_person("PHONE#+919700000031"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_memory_store tests.test_mongo_store`
Expected: FAIL. `request_erasure() got an unexpected keyword argument 'proof'`, no attribute `record_erasure_review`, no attribute `erasure_history`, and the in-memory `delete_person() got an unexpected keyword argument 'dry_run'`.

- [ ] **Step 3: Implement the in-memory store**

In `src/emotorad_ai/conversation.py`, `InMemoryConversationStore`:

Replace the `request_erasure` signature and record:

```python
    def request_erasure(self, user_key: str, channel: str, conversation_id: Optional[str], now: str,
                        proof: Optional[Dict[str, str]] = None) -> str:
        """One pending request per person: asking again returns it."""
        with self._erasure_lock:
            pending = self.pending_erasure_of(user_key)
            if pending is not None:
                return pending["_id"]
            reference = erasure_rules.new_reference()
            while reference in self._erasures:
                reference = erasure_rules.new_reference()
            self._erasures[reference] = {"_id": reference, "user_key": user_key, "status": "pending",
                                         "requested_at": now, "channel": channel,
                                         "conversation_id": conversation_id, "attempts": 0, "last_error": None,
                                         "proof": proof}
            return reference
```

Replace `close_erasure`:

```python
    def close_erasure(self, reference: str, status: str, counts: Optional[Dict[str, int]],
                      error: Optional[str], now: str, by: Optional[str] = None) -> None:
        """Closed: the person's key is replaced by its hash. Reviews, a hold
        and who closed it stay: names and times only."""
        record = self._erasures[reference]
        user_key = record.pop("user_key", None)
        if user_key:
            record["key_sha256"] = erasure_rules.key_sha256(user_key)
        record.update(status=status, processed_at=now, counts=counts)
        if error is not None:
            record["last_error"] = error
        if by is not None:
            record["by"] = by
```

Add after `close_erasure`:

```python
    def record_erasure_review(self, reference: str, by: str, at: str, totals: Dict[str, int]) -> None:
        """Who read a request before deciding (erasure_admin show), and what it then held."""
        self._erasures[reference].setdefault("reviews", []).append({"by": by, "at": at, "totals": dict(totals)})

    def hold_erasure(self, reference: str, by: str, at: str, note: str) -> None:
        self._erasures[reference]["held"] = {"by": by, "at": at, "note": note}

    def erasure_history(self, user_key: str) -> List[Dict[str, Any]]:
        """Every request of one person, open or closed (a closed one by its hash)."""
        digest = erasure_rules.key_sha256(user_key)
        mine = [dict(r) for r in self._erasures.values()
                if r.get("user_key") == user_key or r.get("key_sha256") == digest]
        return sorted(mine, key=lambda r: r["requested_at"])
```

Replace the start of `delete_person` so it takes `dry_run`:

```python
    def delete_person(self, user_key: str, dry_run: bool = False) -> Dict[str, int]:
        """Everything held about one person: the right to erasure (DPDP, GDPR).

        Every conversation that is theirs, whole: the working state, every
        transcript turn (those from before they signed in too) and the
        summaries. The counts say what went, for the person running the
        deletion to confirm. With `dry_run`, counts what would go and deletes
        nothing (erasure_admin's review).
        """
        mine = set(self.conversations_of(user_key))
        if dry_run:
            return {
                "conversations": sum(1 for cid in mine if cid in self._states),
                "transcript_turns": sum(len(self._turns.get(cid, {})) for cid in mine),
                "media": sum(len(self._media.get(cid, {})) for cid in mine),
                "conversation_origins": sum(len(self._origins.get(cid, {})) for cid in mine),
                "conversation_summaries": len(self._summaries.get(user_key, {})) + sum(
                    1 for key, items in self._summaries.items() if key != user_key
                    for item in items.values() if item.conversation_id in mine),
            }
        counts = {"conversations": 0, "transcript_turns": 0, "media": 0, "conversation_origins": 0,
                  "conversation_summaries": len(self._summaries.pop(user_key, {}))}
        for cid in mine:
            for name, count in self.delete_conversation(cid).items():
                counts[name] += count
        return counts
```

Change the comment above `self.erasure_log` in `__init__` from "The erasure_log audit records the nightly job writes." to "The erasure_log audit records erasure_admin and delete_person.py write."

- [ ] **Step 4: Implement the MongoDB store**

In `src/emotorad_ai/stores/mongo.py`, `MongoConversationStore`:

`request_erasure`: add `proof: Optional[Dict[str, str]] = None` as the last parameter, and `"proof": proof` to `record` (after `"last_error": None`).

Replace `close_erasure`:

```python
    def close_erasure(self, reference: str, status: str, counts: Optional[Dict[str, int]],
                      error: Optional[str], now: str, by: Optional[str] = None) -> None:
        """Closed: the person's key is replaced by its hash. Reviews, a hold
        and who closed it stay: names and times only."""
        requests = self._collection(ERASURE_REQUESTS)
        record = self._guard("find_one", lambda: requests.find_one({"_id": reference})) or {}
        fields: Dict[str, Any] = {"status": status, "processed_at": now, "counts": counts}
        if record.get("user_key"):
            fields["key_sha256"] = erasure_rules.key_sha256(record["user_key"])
        if error is not None:
            fields["last_error"] = error
        if by is not None:
            fields["by"] = by
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$set": fields, "$unset": {"user_key": ""}}))
```

Add after `close_erasure`:

```python
    def record_erasure_review(self, reference: str, by: str, at: str, totals: Dict[str, int]) -> None:
        """Who read a request before deciding (erasure_admin show), and what it then held."""
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$push": {"reviews": {"by": by, "at": at, "totals": dict(totals)}}}))

    def hold_erasure(self, reference: str, by: str, at: str, note: str) -> None:
        requests = self._collection(ERASURE_REQUESTS)
        self._guard("update_one", lambda: requests.update_one(
            {"_id": reference}, {"$set": {"held": {"by": by, "at": at, "note": note}}}))

    def erasure_history(self, user_key: str) -> List[Dict[str, Any]]:
        """Every request of one person, open or closed (a closed one by its hash)."""
        requests = self._collection(ERASURE_REQUESTS)
        query = {"$or": [{"user_key": user_key}, {"key_sha256": erasure_rules.key_sha256(user_key)}]}
        return self._guard("find", lambda: list(requests.find(query).sort("requested_at", 1)))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_memory_store tests.test_mongo_store`
Expected: OK.

- [ ] **Step 6: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/stores/mongo.py tests/store_contract.py
git commit -m "feat: stores record an erasure request's proof, reviews and hold, and count without deleting"
```

---

### Task 2: A request records how the person was proven

**Files:**
- Modify: `src/emotorad_ai/tools/verification.py` (`_Pending.verified_on`, `VerificationStore.wall_clock`, `verified_on()`)
- Modify: `src/emotorad_ai/erasure.py` (`proof_of`)
- Modify: `src/emotorad_ai/runtime.py` (constructor `otp_verified_at`, `_erasure_request`, new `_otp_verified_at`)
- Modify: `src/emotorad_ai/api.py` (`Runtime(...)`, `_erasure_person`, the three erasure endpoints)
- Modify: `tests/test_verify_first.py` (`Chat` passes `otp_verified_at`)
- Test: `tests/test_erasure_proof.py` (new), `tests/test_erasure_chat.py`, `tests/test_api_erasure.py`

**Interfaces:**
- Consumes: Task 1's `request_erasure(..., proof=...)`.
- Produces: `VerificationStore.verified_on(conversation_id: str) -> Optional[str]` (ISO time of day, or None under the same conditions as `verified_phone`); `erasure.proof_of(otp_verified_at: Optional[str]) -> Dict[str, str]` returning `{"method": "otp", "verified_at": t}` or `{"method": "app_sign_in"}`; `Runtime(..., otp_verified_at: Optional[Callable[[str], Optional[str]]] = None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_erasure_proof.py`:

```python
"""How the person asking for a deletion was proven (manual erasure spec,
section 2): the review shows it to the person deciding."""

import unittest

from emotorad_ai import erasure
from emotorad_ai.tools.verification import VERIFIED_TTL_SECONDS, VerificationStore


class VerifiedOnTests(unittest.TestCase):
    def test_the_time_of_day_a_number_was_proved(self):
        now = [1000.0]
        store = VerificationStore(clock=lambda: now[0], wall_clock=lambda: "2026-10-01T08:58:00+00:00")
        store.issue("c1", "+919700000033", "123456")
        self.assertIsNone(store.verified_on("c1"))
        self.assertTrue(store.check("c1", "123456"))
        self.assertEqual(store.verified_on("c1"), "2026-10-01T08:58:00+00:00")
        now[0] += VERIFIED_TTL_SECONDS + 1
        self.assertIsNone(store.verified_on("c1"))
        self.assertIsNone(store.verified_on("nobody"))


class ProofOfTests(unittest.TestCase):
    def test_otp_or_the_app(self):
        self.assertEqual(erasure.proof_of("2026-10-01T08:58:00+00:00"),
                         {"method": "otp", "verified_at": "2026-10-01T08:58:00+00:00"})
        self.assertEqual(erasure.proof_of(None), {"method": "app_sign_in"})
```

Append to `tests/test_erasure_chat.py` (add `from datetime import datetime` to its imports):

```python
class RecordedProofTests(unittest.TestCase):
    def test_a_website_visitor_proved_by_otp(self):
        chat = new_chat()
        chat.say("delete my data")
        chat.say(ONE_BIKE[3:])
        chat.say(chat.code())
        self.assertEqual(chat.say("DELETE").handled_by, "erasure:requested")
        proof = chat.conversations.pending_erasure_of("PHONE#" + ONE_BIKE)["proof"]
        self.assertEqual(proof["method"], "otp")
        self.assertIsNotNone(datetime.fromisoformat(proof["verified_at"]))

    def test_an_app_rider_proved_by_signing_in(self):
        chat = new_chat()
        chat.ask("delete my data")
        self.assertEqual(chat.ask("DELETE").handled_by, "erasure:requested")
        self.assertEqual(chat.conversations.pending_erasure_of("PHONE#" + RIDER)["proof"], {"method": "app_sign_in"})
```

Append to `class ErasureEndpointTests` in `tests/test_api_erasure.py`:

```python
    def test_the_request_records_the_app_sign_in(self):
        reference = self.post("/erasure-requests", confirm=True).json()["reference"]
        self.assertEqual(self.api.stores.conversations.erasure_record(reference)["proof"], {"method": "app_sign_in"})
```

Append to `class WebsiteChatButtonTests` in `tests/test_api_erasure.py` (add `from datetime import datetime` to its imports):

```python
    def test_the_request_records_the_otp_and_when(self):
        self.verify("web-3")
        r = self.client.post("/erasure-requests", json={"conversation_id": "web-3", "confirm": True})
        proof = self.api.stores.conversations.erasure_record(r.json()["reference"])["proof"]
        self.assertEqual(proof["method"], "otp")
        self.assertIsNotNone(datetime.fromisoformat(proof["verified_at"]))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_proof tests.test_erasure_chat tests.test_api_erasure`
Expected: FAIL. `VerificationStore.__init__() got an unexpected keyword argument 'wall_clock'`, no attribute `proof_of`, and `KeyError: 'proof'` or a `None` proof in the chat and API tests.

- [ ] **Step 3: The OTP store keeps the time of day**

In `src/emotorad_ai/tools/verification.py`, add `from datetime import datetime, timezone` to the imports, and above `class _Pending` (after the constants):

```python
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

In `_Pending`, after `verified_at: float = 0.0`:

```python
    # The time of day of the same moment, for a person to read (an erasure
    # request's proof). verified_at is monotonic and means nothing to them.
    verified_on: str = ""
```

In `VerificationStore`, after `clock: Callable[[], float] = time.monotonic`:

```python
    wall_clock: Callable[[], str] = _utc_now_iso
```

In `check`, after `pending.verified_at = self.clock()`:

```python
            pending.verified_on = self.wall_clock()
```

After `verified_phone`:

```python
    def verified_on(self, conversation_id: str) -> Optional[str]:
        """When this conversation proved its number, as an ISO time of day, or
        None under the same conditions as verified_phone."""
        with self._lock:
            pending = self._pending.get(conversation_id)
            if pending is None or not pending.verified or pending.session_expired(self.clock()):
                return None
            return pending.verified_on or None
```

- [ ] **Step 4: `proof_of`**

In `src/emotorad_ai/erasure.py`, after `is_confirmation`:

```python
def proof_of(otp_verified_at: Optional[str]) -> Dict[str, str]:
    """How the person asking was proven, for the review (manual erasure spec):
    a code by SMS in this chat, and when, or the app's sign-in."""
    if otp_verified_at:
        return {"method": "otp", "verified_at": otp_verified_at}
    return {"method": "app_sign_in"}
```

- [ ] **Step 5: The runtime records it**

In `src/emotorad_ai/runtime.py`, add a constructor parameter after `phone_resolver`:

```python
        otp_verified_at: Optional[Callable[[str], Optional[str]]] = None,
```

and after `self.phone_resolver = phone_resolver`:

```python
        # When a conversation proved its number by SMS code, for an erasure
        # request's proof (VerificationStore.verified_on).
        self.otp_verified_at = otp_verified_at
```

In `_erasure_request`, replace the `request_erasure` call:

```python
            reference = self.conversations.request_erasure(
                user_key, message.channel, message.conversation_id, utc_now_iso(),
                proof=erasure_rules.proof_of(self._otp_verified_at(message.conversation_id, user_key)))
```

Add after `_erasure_request`:

```python
    def _otp_verified_at(self, conversation_id: str, user_key: str) -> Optional[str]:
        """When this chat proved the asking number by SMS code, or None: the
        identity then came from the app's sign-in."""
        if self.otp_verified_at is None or self.phone_resolver is None:
            return None
        if "PHONE#" + (self.phone_resolver(conversation_id) or "") != user_key:
            return None
        return self.otp_verified_at(conversation_id)
```

In `tests/test_verify_first.py`, `Chat.__init__`, add `otp_verified_at=self.store.verified_on,` after `phone_resolver=self.store.verified_phone,` in the `Runtime(...)` call.

- [ ] **Step 6: The API records it**

In `src/emotorad_ai/api.py`, in the `Runtime(...)` construction, after `phone_resolver=verification_store.verified_phone,` add `otp_verified_at=verification_store.verified_on,`.

Replace `_erasure_person`:

```python
def _erasure_person(request: Request, body: "ErasureIn") -> Tuple[str, str, Dict[str, str]]:
    """Who is asking, on which channel and how they were proven, or 403. Never
    from the URL.

    The Amiigo app's signed-in rider (session_token), or a website visitor
    whose chat verified a number in the last 12 hours (conversation_id): the
    HTML chat's delete button (2026-10-01)."""
    if not message_limiter.allow(client_ip(request, TRUSTED_PROXIES)):
        raise HTTPException(status_code=429, detail="Too many requests. Wait a moment and try again.")
    persona, identity = resolver.resolve_website(None, body.session_token or None)
    if persona == "customer" and identity.may_disclose and identity.phone:
        return "PHONE#" + identity.phone, "amiigo_app", erasure_rules.proof_of(None)
    phone = verification_store.verified_phone(body.conversation_id) if body.conversation_id else None
    if phone:
        return ("PHONE#" + phone, "website_chat",
                erasure_rules.proof_of(verification_store.verified_on(body.conversation_id)))
    detail = erasure_rules.ERASURE_SIGN_IN if body.session_token is not None else erasure_rules.ERASURE_VERIFY_FIRST
    raise HTTPException(status_code=403, detail=detail)
```

In `post_erasure_request`: `user_key, channel, proof = _erasure_person(request, body)`, and pass `proof=proof` to `stores.conversations.request_erasure(...)`. In `post_erasure_status` and `post_erasure_cancel`: `user_key, _, _ = _erasure_person(request, body)`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_proof tests.test_erasure_chat tests.test_api_erasure tests.test_verification tests.test_verification_expiry`
Expected: OK.

- [ ] **Step 8: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/tools/verification.py src/emotorad_ai/erasure.py src/emotorad_ai/runtime.py src/emotorad_ai/api.py tests/test_verify_first.py tests/test_erasure_proof.py tests/test_erasure_chat.py tests/test_api_erasure.py
git commit -m "feat: an erasure request records how the person was proven"
```

---

### Task 3: `erasure_admin`: list, show and hold

**Files:**
- Create: `src/emotorad_ai/erasure_admin.py`
- Test: `tests/test_erasure_admin.py` (new)

**Interfaces:**
- Consumes: Task 1's `record_erasure_review`, `hold_erasure`, `erasure_history`, `delete_person(..., dry_run=True)`; the stores' `pending_erasures`, `erasure_record`, `conversations_of`, `transcript`, `media_of`, `origins_of`, `recent_summaries`; `S3Store.presign_get(key) -> str`.
- Produces: `class Refused(Exception)`; `class Admin(store, media_store, now: Callable[[], datetime], ask: Callable[[str], str], out: Callable[[str], None])` with `list_open() -> int`, `show(reference) -> int`, `hold(reference) -> int`, `totals(user_key) -> Dict[str, int]` (the dry-run counts plus `"s3_objects"`); `STEPS: Dict[str, Tuple[str, int]]`; `run(argv: Sequence[str], admin: Admin) -> int`; `main(argv=None) -> int`. Task 4 adds `delete` and `check` to `Admin` and to `STEPS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_erasure_admin.py`:

```python
"""Manual erasure: the admin command (spec 2026-10-01-manual-erasure-design.md)."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from emotorad_ai import erasure
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.erasure_admin import Admin, run
from emotorad_ai.storage.s3 import StorageError
from tests.store_contract import inbound, reply, summary

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
ME, THEM = "PHONE#+919700000031", "PHONE#+919812345678"
PHOTO = "customers/a/mine/images/upl_1.jpg"
OTP = {"method": "otp", "verified_at": "2026-10-01T08:58:00+00:00"}


class FakeMedia:
    def __init__(self, fail_on=None, store=None):
        self.hidden, self.fail_on, self.store = [], fail_on, store
        self.records_left_when_hiding = []

    def hide(self, key):
        if self.store is not None:
            self.records_left_when_hiding.append(len(self.store.transcript("mine")))
        if key == self.fail_on:
            raise StorageError("hide %r failed: AccessDenied" % key)
        self.hidden.append(key)

    def presign_get(self, key):
        return "https://media.example/%s?X-Amz-Expires=900" % key


class Desk:
    """One person at the terminal: what they type, and what they see."""

    def __init__(self):
        self.at = ["2026-10-01T08:44:00+00:00"]
        self.store = InMemoryConversationStore(clock=lambda: self.at[0])
        self.media = FakeMedia()
        self.now = NOW

    def seed(self, proof=OTP):
        """ME: one chat with a safety hand-over, a ticket, a photo and where it
        came from. THEM: one chat that must never show or go."""
        mine = self.store.get("mine")
        mine.user_key, mine.turns = ME, 1
        self.store.save(mine)
        self.store.record_turn(
            mine, inbound("my battery is smoking", cid="mine"),
            reply("Please stop using it and move it outside.", cid="mine", handled_by="guardrail:battery_safety",
                  ticket_id="EM-00001"),
            summary=summary("mine", user_key=ME, started_at="2026-10-01T08:44:00+00:00", title="Battery smoking",
                            outcome="escalated", ticket_id="EM-00001"),
        )
        self.store.record_media({"_id": PHOTO, "key": PHOTO, "conversation_id": "mine", "kind": "image",
                                 "mime_type": "image/jpeg", "size_bytes": 220168,
                                 "stored_at": "2026-10-01T08:44:00+00:00"})
        self.store.record_origin({"_id": "mine#1", "conversation_id": "mine", "started_at": "2026-10-01T08:44:00+00:00",
                                  "channel": "website_chat", "country": "IN", "region": "Maharashtra",
                                  "city": "Pune", "source": "ip", "user_key": ME})
        theirs = self.store.get("theirs")
        theirs.user_key, theirs.turns = THEM, 1
        self.store.save(theirs)
        self.store.record_turn(theirs, inbound("my brakes squeal", cid="theirs"),
                               reply("Let's check the pads.", cid="theirs"))
        return self.store.request_erasure(ME, "website_chat", "mine", "2026-10-01T09:00:00+00:00", proof=proof)

    def quiet_person(self):
        """ME with one ordinary chat, no file, no ticket, signed in to the app."""
        state = self.store.get("quiet")
        state.user_key, state.turns = ME, 1
        self.store.save(state)
        self.store.record_turn(state, inbound("hello", cid="quiet"), reply("Hi!", cid="quiet"))
        return self.store.request_erasure(ME, "amiigo_app", None, "2026-10-01T09:00:00+00:00",
                                          proof={"method": "app_sign_in"})

    def run(self, *argv, answers=()):
        queue, lines = list(answers), []
        admin = Admin(self.store, self.media, lambda: self.now, lambda prompt: queue.pop(0), lines.append)
        return run(list(argv), admin), "\n".join(lines)


class ListTests(unittest.TestCase):
    def test_references_ages_and_counts_only(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("list")
        self.assertEqual(code, 0)
        self.assertRegex(text, reference + r"\s+2 days\s+website_chat\s+pending\s+chats=1 turns=2 files=1")
        self.assertIn("open erasure requests: 1", text)
        for private in ("+919700000031", "smoking", "Pune", PHOTO):
            self.assertNotIn(private, text)


class ShowTests(unittest.TestCase):
    def setUp(self):
        self.desk = Desk()
        self.reference = self.desk.seed()

    def test_everything_that_will_go_and_the_review_is_recorded(self):
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        for expected in ("ticket EM-00001 raised in chat mine",
                         "safety hand-over in chat mine",
                         "phone: +919700000031",
                         "channel: website_chat",
                         "proof: OTP, verified at 2026-10-01T08:58:00+00:00",
                         "asked from conversation: mine",
                         "earlier requests: none",
                         "came from: ip, IN, Maharashtra, Pune (website_chat)",
                         "summary: Battery smoking",
                         "customer: my battery is smoking",
                         "bot (guardrail:battery_safety): Please stop using it and move it outside.",
                         "open for 15 minutes: https://media.example/" + PHOTO,
                         "transcript_turns: 2",
                         "s3_objects: 1",
                         "Review recorded: Asha"):
            self.assertIn(expected, text)
        self.assertNotIn("my brakes squeal", text)
        (review,) = self.desk.store.erasure_record(self.reference)["reviews"]
        self.assertEqual((review["by"], review["at"]), ("Asha", NOW.isoformat()))
        self.assertEqual((review["totals"]["transcript_turns"], review["totals"]["s3_objects"]), (2, 1))

    def test_no_flags_for_an_ordinary_old_chat(self):
        desk = Desk()
        reference = desk.quiet_person()
        code, text = desk.run("show", reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        self.assertIn("No flags", text)
        self.assertIn("proof: app sign-in", text)
        self.assertIn("No files", text)

    def test_a_chat_used_in_the_last_48_hours_is_flagged(self):
        self.desk.at[0] = "2026-10-03T08:00:00+00:00"
        mine = self.desk.store.get("mine")
        mine.turns = 2
        self.desk.store.record_turn(mine, inbound("is it safe now?", cid="mine"),
                                    reply("Please keep it outside.", cid="mine"))
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertIn("chat mine was used in the last 48 hours", text)

    def test_an_unrecorded_proof_and_an_earlier_request(self):
        desk = Desk()
        desk.store.request_erasure(ME, "amiigo_app", None, "2026-09-01T10:00:00+00:00")
        earlier = desk.store.cancel_erasure(ME, "2026-09-01T11:00:00+00:00")
        reference = desk.seed(proof=None)
        code, text = desk.run("show", reference, answers=["Asha"])
        self.assertIn("proof: not recorded", text)
        self.assertIn("earlier request %s: cancelled, asked 2026-09-01T10:00:00+00:00, "
                      "closed 2026-09-01T11:00:00+00:00" % earlier, text)

    def test_a_chat_whose_working_copy_expired_is_still_shown(self):
        # MongoDB drops the working copy 48 hours after the last message; the
        # transcript, summary and origin stay (Review Focus 3).
        self.desk.store._states.pop("mine")
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 0, text)
        self.assertIn("customer: my battery is smoking", text)
        self.assertIn("conversations: 0", text)

    def test_a_name_is_needed(self):
        code, text = self.desk.run("show", self.reference, answers=["  "])
        self.assertEqual(code, 1)
        self.assertIn("Your name is needed", text)
        self.assertNotIn("my battery is smoking", text)
        self.assertNotIn("reviews", self.desk.store.erasure_record(self.reference))

    def test_an_unknown_or_closed_request(self):
        self.assertEqual(self.desk.run("show", "DEL-ZZZZZZ", answers=["Asha"]), (1, "No erasure request DEL-ZZZZZZ."))
        self.desk.store.cancel_erasure(ME, "2026-10-02T10:00:00+00:00")
        code, text = self.desk.run("show", self.reference, answers=["Asha"])
        self.assertEqual(code, 1)
        self.assertIn("is cancelled, not pending", text)


class HoldTests(unittest.TestCase):
    def test_held_stays_pending_with_the_note(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("hold", reference, answers=["Asha", "safety case EM-00001 open"])
        self.assertEqual(code, 0, text)
        record = desk.store.erasure_record(reference)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["held"], {"by": "Asha", "at": NOW.isoformat(), "note": "safety case EM-00001 open"})
        code, listed = desk.run("list")
        self.assertRegex(listed, reference + r"\s+2 days\s+website_chat\s+held")
        self.assertNotIn("safety case", listed)

    def test_a_note_is_needed(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("hold", reference, answers=["Asha", ""])
        self.assertEqual(code, 1)
        self.assertIn("A note is needed", text)
        self.assertNotIn("held", desk.store.erasure_record(reference))


class RunTests(unittest.TestCase):
    def test_usage(self):
        desk = Desk()
        for argv in ((), ("erase",), ("show",), ("list", "DEL-AAAAAA")):
            code, text = desk.run(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage:", text)

    def test_a_store_failure_stops_with_the_class_only(self):
        desk = Desk()
        with mock.patch.object(desk.store, "pending_erasures",
                               side_effect=StoreUnavailable("mongodb://user:secret@host")):
            self.assertEqual(desk.run("list"), (1, "Stopped on an error (StoreUnavailable)."))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_admin`
Expected: ERROR, `ModuleNotFoundError: No module named 'emotorad_ai.erasure_admin'`.

- [ ] **Step 3: Write the module**

Create `src/emotorad_ai/erasure_admin.py`:

```python
"""Manual erasure (spec 2026-10-01-manual-erasure-design.md).

A person reviews each self-service deletion request and deletes it by hand,
in the live container through an SSM session:

    aws ssm start-session --target i-02e7dc2874e0fdacb
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin list
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin show DEL-XXXXXX
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin hold DEL-XXXXXX

`show` prints the customer's phone number, chats and photo links: it is for
the person deciding, never for a Claude session or a CI log. The review it
records in the request is the log of who read what.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import erasure
from .storage.s3 import StorageError

REVIEW_VALID = timedelta(hours=24)
RECENT = timedelta(hours=48)
REMIND_AFTER_DAYS = 25
FILES = "s3_objects"


class Refused(Exception):
    """A step that will not go ahead. The message is for the person."""


def _when(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _proof(proof: Optional[Dict[str, Any]]) -> str:
    if not proof:
        return "not recorded"
    if proof.get("method") == "otp":
        return "OTP, verified at %s" % (proof.get("verified_at") or "an unrecorded time")
    if proof.get("method") == "app_sign_in":
        return "app sign-in"
    return str(proof.get("method"))


class Admin:
    def __init__(self, store: Any, media_store: Any, now: Callable[[], datetime],
                 ask: Callable[[str], str], out: Callable[[str], None]) -> None:
        self.store, self.media_store, self.now, self.ask, self.out = store, media_store, now, ask, out

    # -- shared ---------------------------------------------------------------

    def _request(self, reference: str) -> Dict[str, Any]:
        record = self.store.erasure_record(reference)
        if record is None:
            raise Refused("No erasure request %s." % reference)
        if record["status"] != "pending":
            raise Refused("%s is %s, not pending. Nothing was changed." % (reference, record["status"]))
        return record

    def _answer(self, prompt: str, what: str) -> str:
        answer = (self.ask(prompt) or "").strip()
        if not answer:
            raise Refused("%s is needed. Nothing was changed." % what)
        return answer

    def _files(self, user_key: str) -> List[Dict[str, Any]]:
        return [record for cid in self.store.conversations_of(user_key) for record in self.store.media_of(cid)]

    def totals(self, user_key: str) -> Dict[str, int]:
        """Exactly what delete would remove: the records per collection and the files."""
        counts = dict(self.store.delete_person(user_key, dry_run=True))
        counts[FILES] = len(self._files(user_key))
        return counts

    def _age(self, record: Dict[str, Any]) -> int:
        return (self.now() - _when(record["requested_at"])).days

    def _print_totals(self, totals: Dict[str, int]) -> None:
        self.out("Totals (what delete removes)")
        for key in sorted(totals):
            self.out("  %s: %d" % (key, totals[key]))

    # -- the steps ------------------------------------------------------------

    def list_open(self) -> int:
        """Every open request, oldest first. No phone number, chat text or note."""
        requests = self.store.pending_erasures()
        for record in requests:
            user_key = record["user_key"]
            totals = self.totals(user_key)
            self.out("%s  %3d days  %-12s  %-7s  chats=%d turns=%d files=%d" % (
                record["_id"], self._age(record), record.get("channel") or "-",
                "held" if record.get("held") else "pending", len(self.store.conversations_of(user_key)),
                totals.get("transcript_turns", 0), totals[FILES]))
        self.out("open erasure requests: %d" % len(requests))
        return 0

    def show(self, reference: str) -> int:
        record = self._request(reference)
        name = self._answer("Your name: ", "Your name")
        user_key = record["user_key"]
        now = self.now()
        totals = self.totals(user_key)
        conversations = self.store.conversations_of(user_key)
        transcripts = {cid: self.store.transcript(cid) for cid in conversations}
        summaries: Dict[str, List[Any]] = {}
        for item in self.store.recent_summaries(user_key, limit=1000):
            summaries.setdefault(item.conversation_id, []).append(item)
        self.store.record_erasure_review(reference, name, now.isoformat(), totals)

        out = self.out
        out("%s  %s  asked %s (%d days ago)" % (
            reference, "held" if record.get("held") else "pending", record["requested_at"], self._age(record)))
        out("")
        out("Flags")
        for flag in self._flags(conversations, summaries, transcripts, now) or ["No flags"]:
            out("  " + flag)
        out("")
        out("Is it genuine")
        out("  phone: %s" % user_key.split("#", 1)[-1])
        out("  channel: %s" % (record.get("channel") or "-"))
        out("  proof: %s" % _proof(record.get("proof")))
        out("  asked from conversation: %s" % (record.get("conversation_id") or "-"))
        held = record.get("held")
        if held:
            out("  held by %s at %s: %s" % (held["by"], held["at"], held["note"]))
        earlier = [r for r in self.store.erasure_history(user_key) if r["_id"] != reference]
        if not earlier:
            out("  earlier requests: none")
        for r in earlier:
            out("  earlier request %s: %s, asked %s, closed %s" % (
                r["_id"], r["status"], r["requested_at"], r.get("processed_at") or "-"))
        for cid in conversations:
            self._chat(cid, summaries.get(cid, []), transcripts[cid])
        out("")
        out("Files")
        files = self._files(user_key)
        if not files:
            out("  No files")
        for f in files:
            out("  %s  %s  %s bytes  stored %s  %s" % (
                f.get("kind") or "-", f.get("mime_type") or "-", f.get("size_bytes", "-"),
                f.get("stored_at") or "-", f["key"]))
            out("    open for 15 minutes: %s" % self._link(f["key"]))
        out("")
        self._print_totals(totals)
        out("")
        out("Review recorded: %s at %s." % (name, now.isoformat()))
        return 0

    def hold(self, reference: str) -> int:
        self._request(reference)
        name = self._answer("Your name: ", "Your name")
        note = self._answer("Note (why it is held, no customer details): ", "A note")
        self.store.hold_erasure(reference, name, self.now().isoformat(), note)
        self.out("%s is held: %s" % (reference, note))
        return 0

    # -- show's parts ---------------------------------------------------------

    @staticmethod
    def _flags(conversations: Sequence[str], summaries: Dict[str, List[Any]],
               transcripts: Dict[str, List[Any]], now: datetime) -> List[str]:
        """What may still be needed: a ticket, a safety hand-over, a chat in use."""
        flags: List[str] = []
        for cid in conversations:
            for item in summaries.get(cid, []):
                if item.ticket_id:
                    flags.append("ticket %s raised in chat %s (%s)" % (item.ticket_id, cid, item.started_at))
            turns = transcripts[cid]
            for turn in turns:
                if turn.role == "bot" and turn.handled_by.startswith("guardrail:") and "safety" in turn.handled_by:
                    flags.append("safety hand-over in chat %s at %s (%s)" % (cid, turn.at, turn.handled_by))
            if turns and now - _when(turns[-1].at) < RECENT:
                flags.append("chat %s was used in the last 48 hours (last turn %s)" % (cid, turns[-1].at))
        return flags

    def _chat(self, cid: str, summaries: List[Any], turns: List[Any]) -> None:
        out = self.out
        out("")
        out("Chat %s  (first %s, last %s)" % (cid, turns[0].at if turns else "-", turns[-1].at if turns else "-"))
        origins = self.store.origins_of(cid)
        if not origins:
            out("  came from: not recorded")
        for o in origins:
            out("  came from: %s, %s, %s, %s (%s)" % (
                o.get("source") or "-", o.get("country") or "-", o.get("region") or "-",
                o.get("city") or "-", o.get("channel") or "-"))
        for s in summaries:
            out("  summary: %s | %s | outcome %s | ticket %s" % (
                s.title or "-", s.product_name or "-", s.outcome, s.ticket_id or "-"))
        if not turns:
            out("  No transcript turns")
        for t in turns:
            who = "customer" if t.role == "customer" else "bot (%s)" % (t.handled_by or "-")
            out("  [%s] %s: %s" % (t.at, who, t.text))
            for a in t.attachments:
                out("      attachment: %s %s" % (a.get("kind") or "attachment", a.get("url") or ""))

    def _link(self, key: str) -> str:
        if self.media_store is None:
            return "unavailable (no media bucket configured)"
        try:
            return self.media_store.presign_get(key)
        except Exception as exc:
            return "unavailable (%s)" % type(exc).__name__


# step -> (Admin method, how many arguments: a reference, or none)
STEPS: Dict[str, Tuple[str, int]] = {"list": ("list_open", 0), "show": ("show", 1), "hold": ("hold", 1)}
USAGE = "usage: python -m emotorad_ai.erasure_admin %s" % " | ".join(
    name + (" DEL-XXXXXX" if nargs else "") for name, (_, nargs) in STEPS.items())


def run(argv: Sequence[str], admin: Admin) -> int:
    if not argv or argv[0] not in STEPS or len(argv) != 1 + STEPS[argv[0]][1]:
        admin.out(USAGE)
        return 2
    method, _ = STEPS[argv[0]]
    try:
        return getattr(admin, method)(*[arg.strip().upper() for arg in argv[1:]])
    except Refused as exc:
        admin.out(str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        admin.out("Stopped.")
        return 1
    except Exception as exc:
        # The class only: a driver's message can carry a host or a value.
        admin.out("Stopped on an error (%s)." % type(exc).__name__)
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    import os

    from .config_store import load_into_environ
    from .storage.s3 import store_from_env
    from .stores.mongo import MongoConversationStore, connect

    try:
        load_into_environ()
        store = MongoConversationStore(connect(db_name=os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai")))
        media_store = store_from_env()
    except Exception as exc:
        print("Could not reach the store (%s). Nothing was changed." % type(exc).__name__)
        return 1
    admin = Admin(store, media_store, lambda: datetime.now(timezone.utc), input, print)
    return run(sys.argv[1:] if argv is None else list(argv), admin)


if __name__ == "__main__":
    raise SystemExit(main())
```

`StorageError`, `erasure` and `REVIEW_VALID` are used by Task 4; leave the imports.

USAGE is computed from `STEPS` when the module loads; Task 4 adds its steps to `STEPS` before `USAGE`, so the usage line lists them.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_admin`
Expected: OK.

- [ ] **Step 5: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/erasure_admin.py tests/test_erasure_admin.py
git commit -m "feat: erasure_admin lists, shows and holds erasure requests"
```

---

### Task 4: `erasure_admin`: delete and check; the nightly job goes

**Files:**
- Modify: `src/emotorad_ai/erasure_admin.py` (`Admin.delete`, `Admin.check`, `_latest_review`, `STEPS`, module docstring)
- Delete: `src/emotorad_ai/erasure_job.py`, `tests/test_erasure_job.py`
- Rename and rewrite: `.github/workflows/erasure-nightly.yml` to `.github/workflows/erasure-check.yml`
- Rewrite: `tests/test_erasure_workflow.py`
- Test: `tests/test_erasure_admin.py`

**Interfaces:**
- Consumes: Task 3's `Admin`, `Refused`, `run`, `STEPS`, `totals`, `_request`, `_answer`, `_files`, `_print_totals`, `_age`; Task 1's `close_erasure(..., by=)`; the stores' `record_erasure_failure`, `delete_person`, `log_erasure`; `erasure.audit_record`; `S3Store.hide(key)`.
- Produces: `Admin.delete(reference) -> int`, `Admin.check() -> int`; `STEPS` gains `"delete": ("delete", 1)` and `"check": ("check", 0)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_erasure_admin.py` (add `import importlib.util` to its imports):

```python
class DeleteTests(unittest.TestCase):
    REASON = "asked in the chat; no open case"

    def setUp(self):
        self.desk = Desk()
        self.reference = self.desk.seed()

    def reviewed(self, name="Asha"):
        self.assertEqual(self.desk.run("show", self.reference, answers=[name])[0], 0)

    def delete(self, name="Asha", reason=None, typed=None):
        return self.desk.run("delete", self.reference,
                             answers=[name, self.REASON if reason is None else reason, typed or self.reference])

    def assert_nothing_deleted(self):
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.media.hidden, [])
        self.assertEqual(self.desk.store.erasure_log, [])
        self.assertEqual(self.desk.store.erasure_record(self.reference)["status"], "pending")

    def test_after_a_review_everything_goes_files_first(self):
        self.desk.media = FakeMedia(store=self.desk.store)
        self.reviewed()
        code, text = self.delete()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.desk.media.hidden, [PHOTO])
        self.assertEqual(self.desk.media.records_left_when_hiding, [2])  # files first, records after
        self.assertEqual(self.desk.store.conversations_of(ME), [])
        self.assertEqual(self.desk.store.conversations_of(THEM), ["theirs"])
        record = self.desk.store.erasure_record(self.reference)
        self.assertEqual((record["status"], record["by"]), ("done", "Asha"))
        self.assertNotIn("user_key", record)
        (audit,) = self.desk.store.erasure_log
        self.assertEqual(audit, erasure.audit_record(
            ME, "self-service request %s: %s" % (self.reference, self.REASON), "Asha", NOW,
            deleted=record["counts"], s3_objects=1))

    def test_a_held_request_can_still_be_deleted(self):
        self.desk.run("hold", self.reference, answers=["Asha", "safety case EM-00001 open"])
        self.reviewed()
        self.assertEqual(self.delete()[0], 0)

    def test_refused_without_a_review(self):
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Run show %s first" % self.reference, text)
        self.assert_nothing_deleted()

    def test_refused_when_someone_else_reviewed(self):
        self.reviewed("Ravi")
        code, text = self.delete("Asha")
        self.assertEqual(code, 1)
        self.assertIn("No review of %s by Asha" % self.reference, text)
        self.assert_nothing_deleted()

    def test_the_same_name_in_another_case_counts(self):
        self.reviewed("Asha")
        self.assertEqual(self.delete(" asha ")[0], 0)

    def test_refused_when_the_review_is_over_24_hours_old(self):
        self.reviewed()
        self.desk.now = NOW + timedelta(hours=24, minutes=1)
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("more than 24 hours old", text)
        self.assert_nothing_deleted()

    def test_refused_when_something_new_arrived_after_the_review(self):
        self.reviewed()
        mine = self.desk.store.get("mine")
        mine.turns = 2
        self.desk.store.record_turn(mine, inbound("one more thing", cid="mine"), reply("Thanks.", cid="mine"))
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("has changed since your review", text)
        self.assertEqual(self.desk.media.hidden, [])
        self.assertEqual(len(self.desk.store.transcript("mine")), 4)

    def test_refused_when_the_wrong_reference_is_typed(self):
        self.reviewed()
        code, text = self.delete(typed="DEL-AAAAAA")
        self.assertEqual(code, 1)
        self.assertIn("Stopped. Nothing was deleted.", text)
        self.assert_nothing_deleted()

    def test_the_reference_in_lower_case_is_accepted(self):
        self.reviewed()
        code, text = self.desk.run("delete", self.reference.lower(),
                                   answers=["Asha", self.REASON, self.reference.lower()])
        self.assertEqual(code, 0, text)

    def test_refused_without_a_reason(self):
        self.reviewed()
        code, text = self.delete(reason=" ")
        self.assertEqual(code, 1)
        self.assertIn("A reason is needed", text)
        self.assert_nothing_deleted()

    def test_refused_once_the_customer_cancelled(self):
        self.reviewed()
        self.desk.store.cancel_erasure(ME, "2026-10-03T08:30:00+00:00")
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("is cancelled, not pending", text)
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.media.hidden, [])

    def test_a_cancel_while_the_person_types_stops_it(self):
        self.reviewed()
        answers = ["Asha", self.REASON]

        def ask(prompt):
            if prompt.startswith("Type "):
                self.desk.store.cancel_erasure(ME, "2026-10-03T09:00:00+00:00")
                return self.reference
            return answers.pop(0)

        lines = []
        code = run(["delete", self.reference], Admin(self.desk.store, self.desk.media, lambda: NOW, ask, lines.append))
        self.assertEqual(code, 1)
        self.assertIn("is cancelled, not pending", "\n".join(lines))
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.media.hidden, [])

    def test_a_file_that_cannot_be_hidden_keeps_every_record(self):
        self.desk.media = FakeMedia(fail_on=PHOTO)
        self.reviewed()
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Could not hide the files (StorageError). Nothing in the database was deleted.", text)
        record = self.desk.store.erasure_record(self.reference)
        self.assertEqual((record["status"], record["attempts"], record["last_error"]), ("pending", 1, "StorageError"))
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.desk.store.erasure_log, [])
        self.desk.media.fail_on = None
        self.assertEqual(self.delete()[0], 0)

    def test_files_and_no_bucket_is_a_hide_failure(self):
        self.desk.media = None
        self.reviewed()
        code, text = self.delete()
        self.assertEqual(code, 1)
        self.assertIn("Could not hide the files (StorageError)", text)
        self.assertEqual(self.desk.store.conversations_of(ME), ["mine"])

    def test_a_person_with_no_files_needs_no_bucket(self):
        desk = Desk()
        reference = desk.quiet_person()
        desk.media = None
        self.assertEqual(desk.run("show", reference, answers=["Asha"])[0], 0)
        code, text = desk.run("delete", reference, answers=["Asha", self.REASON, reference])
        self.assertEqual(code, 0, text)
        self.assertEqual(desk.store.conversations_of(ME), [])


class CheckTests(unittest.TestCase):
    def test_quiet_below_25_days(self):
        desk = Desk()
        reference = desk.seed()
        code, text = desk.run("check")
        self.assertEqual(code, 0, text)
        self.assertRegex(text, reference + r"\s+2 days\s+pending")
        self.assertIn("open erasure requests: 1", text)

    def test_red_at_25_days_held_included(self):
        desk = Desk()
        reference = desk.seed()
        desk.run("hold", reference, answers=["Asha", "safety case EM-00001 open"])
        desk.now = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
        code, text = desk.run("check")
        self.assertEqual(code, 1)
        self.assertRegex(text, reference + r"\s+25 days\s+held")
        self.assertIn("25 days old or more", text)
        for private in ("+919700000031", "safety case", "smoking", PHOTO, "chats="):
            self.assertNotIn(private, text)

    def test_red_when_the_store_cannot_be_read(self):
        desk = Desk()
        with mock.patch.object(desk.store, "pending_erasures", side_effect=StoreUnavailable("down")):
            self.assertEqual(desk.run("check"), (1, "Stopped on an error (StoreUnavailable)."))


class NothingDeletesOnItsOwnTests(unittest.TestCase):
    def test_the_nightly_job_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("emotorad_ai.erasure_job"))
```

Replace `tests/test_erasure_workflow.py` with:

```python
"""The daily erasure check (manual erasure spec, section 4): when it runs, that
it only checks, and no secret in it."""

import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "erasure-check.yml"
DELETES = ("erasure_admin delete", "erasure_job", "delete_person")


class ErasureCheckWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_daily_at_nine_in_india_and_by_hand(self):
        triggers = self.workflow[True]  # PyYAML reads the key "on" as True
        self.assertEqual(triggers["schedule"], [{"cron": "30 3 * * *"}])
        self.assertIn("workflow_dispatch", triggers)

    def test_it_runs_only_the_check_in_a_one_off_container_and_stops_on_error(self):
        self.assertIn("python -m emotorad_ai.erasure_admin check", self.text)
        for needle in DELETES:
            self.assertNotIn(needle, self.text)
        self.assertIn("docker run --rm", self.text)
        self.assertIn('\\"set -e\\"', self.text)
        self.assertIn("EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app", self.text)

    def test_the_nightly_workflow_is_gone(self):
        self.assertFalse((WORKFLOWS / "erasure-nightly.yml").exists())

    def test_no_scheduled_workflow_deletes(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            if "schedule" in (yaml.safe_load(text).get(True) or {}):
                for needle in DELETES:
                    self.assertNotIn(needle, text, path.name)

    def test_no_secret_value_is_in_it(self):
        for needle in ("sk-or-", "mongodb+srv://", "postgresql://", "AKIA"):
            self.assertNotIn(needle, self.text)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_admin tests.test_erasure_workflow`
Expected: FAIL and ERROR. Usage (exit 2) for every `delete` and `check` run, `find_spec` finds `erasure_job`, and `FileNotFoundError` for `erasure-check.yml`.

- [ ] **Step 3: Add delete and check**

In `src/emotorad_ai/erasure_admin.py`:

Add the two usage lines to the module docstring, after the `hold` line:

```
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin delete DEL-XXXXXX

`check` is the daily workflow's (.github/workflows/erasure-check.yml): open
requests by reference and age only.
```

Add above `class Admin`:

```python
def _latest_review(record: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    mine = [r for r in record.get("reviews") or [] if r["by"].strip().casefold() == name.casefold()]
    return max(mine, key=lambda r: _when(r["at"])) if mine else None


def _counts(counts: Dict[str, int]) -> str:
    return ", ".join("%s %d" % (key, counts[key]) for key in sorted(counts))
```

Add to `Admin`, after `hold`:

```python
    def delete(self, reference: str) -> int:
        record = self._request(reference)
        name = self._answer("Your name: ", "Your name")
        reason = self._answer("Reason (who asked, and why it can go): ", "A reason")
        user_key = record["user_key"]
        review = _latest_review(record, name)
        if review is None:
            raise Refused("No review of %s by %s. Run show %s first. Nothing was deleted." % (reference, name, reference))
        if self.now() - _when(review["at"]) > REVIEW_VALID:
            raise Refused("Your review of %s is more than 24 hours old. Run show %s again. Nothing was deleted."
                          % (reference, reference))
        totals = self.totals(user_key)
        if totals != review["totals"]:
            raise Refused("What %s holds has changed since your review. Run show %s again. Nothing was deleted."
                          % (reference, reference))
        self._print_totals(totals)
        typed = (self.ask("Type %s to delete it, or anything else to stop: " % reference) or "").strip().upper()
        if typed != reference:
            raise Refused("Stopped. Nothing was deleted.")
        # The customer may have cancelled while the person read and typed.
        self._request(reference)
        keys = [f["key"] for f in self._files(user_key)]
        try:
            if keys and self.media_store is None:
                raise StorageError("no media bucket configured")
            for key in keys:
                self.media_store.hide(key)
        except Exception as exc:
            error = type(exc).__name__
            self.store.record_erasure_failure(reference, error)
            self.out("Could not hide the files (%s). Nothing in the database was deleted. "
                     "Fix the cause and run delete %s again." % (error, reference))
            return 1
        now = self.now()
        counts = self.store.delete_person(user_key)
        self.store.log_erasure(erasure.audit_record(
            user_key, "self-service request %s: %s" % (reference, reason), name, now,
            deleted=counts, s3_objects=len(keys)))
        self.store.close_erasure(reference, "done", counts, None, now.isoformat(), by=name)
        self.out("Deleted %s: %d files hidden; records: %s. The audit record is written."
                 % (reference, len(keys), _counts(counts)))
        return 0

    def check(self) -> int:
        """References and ages only: red at 25 days, held ones included."""
        requests = self.store.pending_erasures()
        late = 0
        for record in requests:
            age = self._age(record)
            late += age >= REMIND_AFTER_DAYS
            self.out("%s  %3d days  %s" % (record["_id"], age, "held" if record.get("held") else "pending"))
        self.out("open erasure requests: %d" % len(requests))
        if late:
            self.out("%d request(s) are %d days old or more: deal with them before 30 days."
                     % (late, REMIND_AFTER_DAYS))
        return 1 if late else 0
```

Replace the `STEPS` line:

```python
STEPS: Dict[str, Tuple[str, int]] = {"list": ("list_open", 0), "show": ("show", 1), "hold": ("hold", 1),
                                     "delete": ("delete", 1), "check": ("check", 0)}
```

- [ ] **Step 4: The nightly job goes; the workflow becomes the check**

```bash
git rm -q src/emotorad_ai/erasure_job.py tests/test_erasure_job.py
git mv .github/workflows/erasure-nightly.yml .github/workflows/erasure-check.yml
```

In `.github/workflows/erasure-check.yml`, replace the header (the `name:` line through the `cron:` line) with:

```yaml
name: Erasure check (staging)

# Manual erasure (docs/superpowers/specs/2026-10-01-manual-erasure-design.md):
# a person reviews and deletes each request with erasure_admin. This run deletes
# nothing. It lists open requests by reference and age only, and is red when one
# is 25 days old or more, so GitHub emails the workflow's owner.
# GitHub runs a schedule only from the default branch: until this file is on
# main, start it by hand (Actions -> this workflow -> Run workflow).
on:
  schedule:
    - cron: "30 3 * * *"  # 09:00 India time
```

and in the rest of the file: the job id `erase:` becomes `check:`; the step name "Run the erasure job on the instance (via SSM)" becomes "Run the erasure check on the instance (via SSM)"; `--comment "nightly erasure"` becomes `--comment "erasure check"`; `python -m emotorad_ai.erasure_job` becomes `python -m emotorad_ai.erasure_admin check`; the comment "Its output comes back here (default log driver), so the run shows each request's outcome." becomes "Its output comes back here (default log driver): references and ages only."

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure_admin tests.test_erasure_workflow`
Expected: OK.

- [ ] **Step 6: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK. `grep -rn "erasure_job" src tests .github scripts` prints nothing (`storage/s3.py`'s docstring is Task 5's; if it names `erasure_job.py`, update it here).

```bash
git add src/emotorad_ai/erasure_admin.py tests/test_erasure_admin.py tests/test_erasure_workflow.py .github/workflows/erasure-check.yml
git commit -m "feat: erasure_admin deletes a reviewed request by hand; the nightly job becomes a daily check"
```

---

### Task 5: What the customer is told, the contract and the docs

**Files:**
- Modify: `src/emotorad_ai/erasure.py` (`ERASURE_REQUESTED`, `ERASURE_EXISTING`, module docstring, the `ERASURE_NOT_BY_MODEL` comment)
- Modify: `docs/contracts/amiigo-support-chat.md`
- Modify: `CLAUDE.md` (the self-service erasure bullet)
- Modify: comments in `src/emotorad_ai/runtime.py`, `src/emotorad_ai/api.py`, `src/emotorad_ai/storage/s3.py`, `web/emotorad-support-chat-dev.html`
- Modify: `docs/superpowers/specs/2026-10-01-self-service-erasure-design.md` (a note at the top)
- Refresh: `C:\Users\user\Desktop\Amiigo Support Chat API Contract.md` and `.pdf`
- Test: `tests/test_erasure.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: the texts below, exactly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_erasure.py` (add `from pathlib import Path` to its imports):

```python
ROOT = Path(__file__).resolve().parents[1]


class ManualPromiseTests(unittest.TestCase):
    """Manual erasure spec, section 3: a person deletes, within 30 days."""

    def test_no_text_promises_a_nightly_run(self):
        for name in ("ERASURE_CONFIRM", "ERASURE_DIALOG", "ERASURE_REQUESTED", "ERASURE_EXISTING"):
            text = getattr(erasure, name)
            self.assertNotIn("tonight", text.lower(), name)
            self.assertIn("within 30 days", text, name)

    def test_the_two_new_texts(self):
        self.assertEqual(erasure.ERASURE_REQUESTED.format(reference="DEL-7K3P9Q"), (
            "Your deletion request is DEL-7K3P9Q. Our team will check it and delete everything this chat "
            "holds about you within 30 days. If you change your mind before then, say 'cancel my deletion'."))
        self.assertEqual(erasure.ERASURE_EXISTING.format(reference="DEL-7K3P9Q"), (
            "You've already asked for this. Your request is DEL-7K3P9Q, and our team will complete it "
            "within 30 days."))

    def test_the_contract_promises_the_team_not_a_job(self):
        contract = (ROOT / "docs" / "contracts" / "amiigo-support-chat.md").read_text(encoding="utf-8")
        self.assertNotIn("nightly", contract)
        self.assertNotIn("tonight", contract)
        self.assertIn("Our team checks each request and deletes the data within 30 days.", contract)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure`
Expected: FAIL. `'tonight' unexpectedly found` for `ERASURE_REQUESTED`, the two texts differ, and the contract still says "nightly".

- [ ] **Step 3: The texts**

In `src/emotorad_ai/erasure.py`:

```python
ERASURE_REQUESTED = (
    "Your deletion request is {reference}. Our team will check it and delete "
    "everything this chat holds about you within 30 days. If you change your "
    "mind before then, say 'cancel my deletion'."
)
ERASURE_EXISTING = (
    "You've already asked for this. Your request is {reference}, and our team "
    "will complete it within 30 days."
)
```

The module docstring's "The nightly job (erasure_job.py) is the only thing that deletes." becomes "A person deletes, with erasure_admin.py, after reading the request (manual erasure spec)." The comment above `ERASURE_NOT_BY_MODEL`, "(the erasure gate and the nightly job are the only things that delete)", becomes "(the erasure gate only records a request; a person deletes it with erasure_admin)". The `audit_record` docstring's "scripts/delete_person.py and the nightly job both write" becomes "scripts/delete_person.py and erasure_admin both write".

- [ ] **Step 4: The contract**

In `docs/contracts/amiigo-support-chat.md`, section "Deleting conversation data", replace "The request is carried out by a nightly job and the data is gone for\ngood within 30 days." with "Our team checks each request and deletes the data within 30 days." (keep the paragraph's line wrapping at about 78 characters).

- [ ] **Step 5: CLAUDE.md, comments and the earlier spec**

In `CLAUDE.md`, replace the self-service erasure bullet (it starts "- **Delete my data, self-service**") with:

```markdown
- **Delete my data, self-service** (specs 2026-10-01: self-service erasure, manual erasure): a verified customer asks in the chat ("delete my data", or "delete" on its own, confirmed by typing DELETE; on the website after verifying) or with a button: the Amiigo app's (`POST /erasure-requests`, `/status`, `/cancel`, by `session_token`) or the HTML chat's header menu (the same endpoints, by the `conversation_id` of a chat that verified a number in the last 12 hours; nothing is sent into the chat). Both only record a request in `erasure_requests` (one pending per person, with its `proof`: app sign-in, or OTP and when; closed records keep only `key_sha256`). Nothing deletes on its own: a person runs `erasure_admin` in the live container through an SSM session (`sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin list | show DEL-XXXXXX | hold DEL-XXXXXX | delete DEL-XXXXXX`). `show` prints the customer's chats, photo links and phone, with flags (a ticket, a safety hand-over, a chat used in the last 48 hours), and records the review. `delete` refuses without that person's `show` in the last 24 hours or if anything changed since; it hides the files with a plain delete first (the lifecycle erases them within 30 days; the instance role has `s3:DeleteObject` on `customers/*` only), then `delete_person`, an `erasure_log` record with the person's name and reason, and the request closed. A Claude session never runs `erasure_admin show` or `delete`: they print or remove customer data. `.github/workflows/erasure-check.yml` runs `erasure_admin check` daily (references and ages only) and is red when a request is 25 days old. Scheduled runs need the workflow on `main`; until then, run it by hand. The contract for the app: `docs/contracts/amiigo-support-chat.md`.
```

Comments, each a wording change only:
- `src/emotorad_ai/runtime.py`, in `_node_erasure`: "never carried out here: the nightly job deletes." becomes "never carried out here: a person deletes it with erasure_admin."
- `src/emotorad_ai/runtime.py`, above the `claims_deletion` check: "Only the erasure gate records a deletion and only the nightly job makes one" becomes "Only the erasure gate records a deletion and only a person makes one (erasure_admin)".
- `src/emotorad_ai/api.py`, `post_erasure_request` docstring: "Records a request; the nightly job deletes." becomes "Records a request; a person deletes it with erasure_admin."
- `src/emotorad_ai/storage/s3.py`, `hide` docstring: "Called by the nightly erasure job (erasure_job.py), never by the chat." becomes "Called by erasure_admin delete, never by the chat."
- `web/emotorad-support-chat-dev.html`, the delete block's comment: "The nightly job does the deleting." becomes "A person deletes it after reading it (erasure_admin)."

In `docs/superpowers/specs/2026-10-01-self-service-erasure-design.md`, add after the title line:

```markdown
> Changed by `2026-10-01-manual-erasure-design.md`: there is no nightly job. A
> person reviews and deletes each request with `erasure_admin`.
```

- [ ] **Step 6: Run the tests, then the whole suite**

Run: `PYTHONPATH="src;." python -m unittest tests.test_erasure tests.test_erasure_chat tests.test_api_erasure tests.test_chat_delete_button`
Expected: OK.

Run: the Global Constraints test command. Expected: OK. `grep -rn "nightly\|tonight" src web docs/contracts CLAUDE.md .github` prints only lines that are not about erasure (for example `enrichment.py`'s "a nightly job would precompute").

- [ ] **Step 7: Refresh the Desktop copies**

```bash
cp docs/contracts/amiigo-support-chat.md "/c/Users/user/Desktop/Amiigo Support Chat API Contract.md"
```

Render the PDF as last time, with the session scratchpad's `md_to_html.py` (it reads the Desktop `.md`, converts it with Python `markdown` and A4 print CSS, and writes the HTML file named on its command line):

```bash
SP="C:/Users/user/AppData/Local/Temp/claude/E--irctc-test/9594a7d0-9e01-461a-848c-e6222b53eaa4/scratchpad"
python "$SP/md_to_html.py" "$SP/contract.html"
"/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf="C:/Users/user/Desktop/Amiigo Support Chat API Contract.pdf" "file:///$SP/contract.html"
python -c "import pathlib; d = pathlib.Path(r'C:/Users/user/Desktop/Amiigo Support Chat API Contract.pdf').read_bytes(); print(d[:5])"
grep -c "Our team checks each request" "$SP/contract.html"
```

Expected: `b'%PDF-'`, and `1`.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/erasure.py src/emotorad_ai/runtime.py src/emotorad_ai/api.py src/emotorad_ai/storage/s3.py web/emotorad-support-chat-dev.html docs/contracts/amiigo-support-chat.md docs/superpowers/specs/2026-10-01-self-service-erasure-design.md CLAUDE.md tests/test_erasure.py
git commit -m "docs: customer texts, contract and docs say a person deletes within 30 days"
```
