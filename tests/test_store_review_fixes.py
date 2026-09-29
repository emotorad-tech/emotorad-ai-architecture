"""Regression tests for the final-review findings on feat/conversation-store.

Each test reproduces one finding and failed before its fix.
"""

import hashlib
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

import mongomock

from emotorad_ai.conversation import (
    ConversationConflict,
    ConversationState,
    InMemoryConversationStore,
    StoreUnavailable,
)
from emotorad_ai.guardrails import SAFETY_MESSAGE
from emotorad_ai.llm import call_tool, say
from emotorad_ai.stores.mongo import MongoConversationStore, MongoIdempotencyStore, connect, ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, build_registry
from emotorad_ai.tools.registry import IdempotencyStore, ToolContext, ToolRegistry, ok
from tests.store_contract import inbound, reply, summary
from tests.test_runtime_persistence import ConflictingStore, runtime_on, send

TODAY = date(2026, 7, 28)
CTX = ToolContext(conversation_id="c1", phone="+919876543210")
TICKET_TURN = [
    call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                      "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1"),
    say("Your reference is EM-00001; the team will call you."),
]
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- C1: an idempotency store outage never crashes a turn ---------------------

class ReceiptsDown(IdempotencyStore):
    def claim(self, key):
        raise StoreUnavailable("MongoDB insert_one failed")

    def put(self, key, envelope):
        raise StoreUnavailable("MongoDB replace_one failed")


class IdempotencyOutageTests(unittest.TestCase):
    def registry_with(self, store):
        registry, calls = ToolRegistry(idempotency=store), []

        @registry.register("w", "d", parameters={"idempotency_key": {"type": "string"}},
                           required=("idempotency_key",), write=True)
        def w(idempotency_key):
            calls.append(idempotency_key)
            return {"done": True}

        return registry, calls

    def test_an_unreachable_claim_refuses_the_write_rather_than_crashing(self):
        registry, calls = self.registry_with(ReceiptsDown())
        envelope = registry.call("w", {"idempotency_key": "k"}, CTX)
        self.assertEqual(envelope["error"]["code"], "idempotency_unavailable")
        self.assertEqual(calls, [])

    def test_a_write_that_must_not_wait_runs_without_the_receipt(self):
        registry, calls = self.registry_with(ReceiptsDown())
        envelope = registry.call("w", {"idempotency_key": "k"}, CTX, run_without_idempotency=True)
        self.assertEqual(envelope["data"], {"done": True})
        self.assertEqual(calls, ["k"])

    def test_a_receipt_that_cannot_be_written_is_flagged_not_lost(self):
        class PutDown(IdempotencyStore):
            def put(self, key, envelope):
                raise StoreUnavailable("MongoDB replace_one failed")
        registry, _ = self.registry_with(PutDown())
        envelope = registry.call("w", {"idempotency_key": "k"}, CTX)
        self.assertEqual(envelope["data"], {"done": True})
        self.assertIn("idempotency_warning", envelope)

    def test_the_safety_branch_still_raises_its_ticket_and_warns_the_customer(self):
        registry = build_registry(today=TODAY, idempotency=ReceiptsDown())
        runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
        answer = send(runtime, "my battery is swollen")
        self.assertEqual(answer.handled_by, "guardrail:battery_safety")
        self.assertIn(SAFETY_MESSAGE[:60], answer.text)
        self.assertTrue(answer.ticket_id)

    def test_a_store_error_anywhere_in_the_turn_hands_over(self):
        runtime = runtime_on(InMemoryConversationStore(), [])
        runtime.resolver.hydrate = mock.Mock(side_effect=StoreUnavailable("down"))
        answer = send(runtime, "my battery won't charge")
        self.assertEqual(answer.handled_by, "store_unavailable")


# -- C2: the permanent record survives a conversation id reused after expiry -

class ReuseAfterExpiryTests(unittest.TestCase):
    def test_old_turns_and_the_old_summary_survive(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        store = MongoConversationStore(db)
        state = store.get("c1")
        state.user_key = "PHONE#+919876543210"
        for turn in (1, 2):
            state.turns = turn
            store.save(state)
            store.record_turn(state, inbound("old %d" % turn), reply("old reply %d" % turn),
                              summary("c1", started_at="2026-09-20T10:00:00+00:00", turns=turn))
        db["conversations"].delete_one({"_id": "c1"})  # what the 48-hour TTL does

        again = store.get("c1")
        self.assertEqual(again.turn_offset, 4)
        again.user_key, again.turns = "PHONE#+919876543210", 1
        store.save(again)
        store.record_turn(again, inbound("new"), reply("new reply"), summary("c1", started_at=again.started_at, turns=1))

        texts = [t.text for t in store.transcript("c1")]
        self.assertEqual(texts, ["old 1", "old reply 1", "old 2", "old reply 2", "new", "new reply"])
        self.assertEqual(len(store.recent_summaries("PHONE#+919876543210")), 2)


# -- I3: a conflict never repeats or hides a write ------------------------------

class ConflictAfterWriteTests(unittest.TestCase):
    def test_a_turn_that_raised_a_ticket_is_not_rerun_after_a_conflict(self):
        store = ConflictingStore(conflicts=1)
        runtime = runtime_on(store, list(TICKET_TURN))
        answer = send(runtime, "my battery won't charge")
        self.assertEqual(len(runtime.registry.tickets.tickets), 1)
        self.assertEqual(len(runtime.llm.requests), 2)  # not run a second time
        self.assertEqual(answer.ticket_id, "EM-00001")
        self.assertIn("EM-00001", answer.text)
        saved = store.get("conv-1")  # the turn is kept, not lost with the losing save
        self.assertEqual((saved.turns, saved.ticket_id), (1, "EM-00001"))
        self.assertIn({"role": "user", "content": "my battery won't charge"}, saved.history)
        self.assertIn("transcript", runtime.registry.tickets.tickets["EM-00001"])

    def test_a_save_outage_after_a_ticket_still_gives_the_reference(self):
        class SaveDown(InMemoryConversationStore):
            def save(self, state):
                raise StoreUnavailable("MongoDB update_one failed")
        runtime = runtime_on(SaveDown(), list(TICKET_TURN))
        answer = send(runtime, "my battery won't charge")
        self.assertEqual(answer.handled_by, "store_unavailable")
        self.assertIn("EM-00001", answer.text)


# -- I4: an abandoned claim does not block a write for a week -------------------

class LeaseTests(unittest.TestCase):
    def check(self, store, advance):
        self.assertIsNone(store.claim("k"))
        advance(60)
        self.assertEqual(store.claim("k")["error"]["code"], "write_in_progress")
        advance(250)  # 310 s after the first claim: past the 5-minute lease
        self.assertIsNone(store.claim("k"))

    def test_in_memory(self):
        now = [1000.0]
        store = IdempotencyStore(clock=lambda: now[0], lease_seconds=300)
        self.check(store, lambda s: now.__setitem__(0, now[0] + s))

    def test_mongodb(self):
        from datetime import datetime, timedelta, timezone
        now = [datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)]
        db = mongomock.MongoClient()["emotorad_ai"]
        store = MongoIdempotencyStore(db, now=lambda: now[0], lease_seconds=300)
        self.check(store, lambda s: now.__setitem__(0, now[0] + timedelta(seconds=s)))


# -- I5: erasure removes everything, including messages before login ------------

class ErasureTests(unittest.TestCase):
    def mongo(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        return db, MongoConversationStore(db)

    def login_midway(self, store):
        state = store.get("c1")
        state.turns = 1
        store.save(state)
        store.record_turn(state, inbound("my name is Priya, 12 MG Road"), reply("Thanks."))  # not signed in yet
        state.user_key, state.turns = "PHONE#+919876543210", 2
        store.save(state)
        store.record_turn(state, inbound("now signed in"), reply("Hello."), summary("c1"))

    def test_turns_from_before_login_are_erased_too(self):
        for store in (InMemoryConversationStore(), self.mongo()[1]):
            with self.subTest(type(store).__name__):
                self.login_midway(store)
                counts = store.delete_person("PHONE#+919876543210")
                self.assertEqual(counts["transcript_turns"], 4)
                self.assertEqual(store.transcript("c1"), [])

    def test_receipts_for_the_persons_conversations_are_erased(self):
        db, store = self.mongo()
        self.login_midway(store)
        MongoIdempotencyStore(db).put("c1:book_service_slot:k", ok({"customer_id": "+919876543210"}))
        counts = store.delete_person("PHONE#+919876543210")
        self.assertEqual(counts["idempotency_keys"], 1)
        self.assertEqual(db["idempotency_keys"].count_documents({}), 0)

    def test_an_unkeyed_conversation_can_be_erased_by_its_id(self):
        for store in (InMemoryConversationStore(), self.mongo()[1]):
            with self.subTest(type(store).__name__):
                state = store.get("anon")
                state.turns = 1
                store.save(state)
                store.record_turn(state, inbound("12 MG Road"), reply("Ok."))
                counts = store.delete_conversation("anon")
                self.assertEqual(counts["transcript_turns"], 2)
                self.assertEqual(store.transcript("anon"), [])


# -- M3 (re-graded): an unresponsive cluster cannot hang the turn ---------------

class TimeoutTests(unittest.TestCase):
    def test_every_operation_is_bounded(self):
        with mock.patch("pymongo.MongoClient") as client:
            connect(uri="mongodb://example.test", db_name="emotorad_ai")
        self.assertEqual(client.call_args.kwargs.get("timeoutMS"), 5000)


# -- M7 (re-graded): the smoke test reports, never crashes ----------------------

class SmokeRobustnessTests(unittest.TestCase):
    def test_a_short_transcript_is_a_failed_check_not_a_traceback(self):
        client = mongomock.MongoClient()
        ensure_indexes(client["emotorad_ai"])
        lines = []
        whole = MongoConversationStore.transcript
        with mock.patch.object(MongoConversationStore, "transcript", autospec=True,
                               side_effect=lambda store, cid: whole(store, cid)[:4]):
            passed = load_script("mongo_smoke").run_smoke(client=client, db_name="emotorad_ai", out=lines.append)
        self.assertFalse(passed)
        for number in ("4.", "5."):
            self.assertTrue(any(line.startswith("FAIL  " + number) for line in lines), lines)
        self.assertTrue(any(line.startswith("PASS  13.") for line in lines), lines)  # cleanup still ran


# -- M8 (re-graded): every erasure leaves an audit record without the person ---

class ErasureAuditTests(unittest.TestCase):
    def test_a_deletion_needs_a_reason_and_is_logged_by_hash(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        db["transcript_turns"].insert_one({"_id": "c#00001", "conversation_id": "c", "n": 1, "user_key": "PHONE#+919876543210"})
        module = load_script("delete_person")
        with mock.patch.object(module, "connect", lambda db_name: client[db_name]):
            with mock.patch.object(sys, "argv", ["delete_person.py", "--phone", "9876543210", "--yes"]), \
                    redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                module.main()  # no --reason: refused
            with mock.patch.object(sys, "argv", ["delete_person.py", "--phone", "9876543210", "--yes",
                                                 "--reason", "customer email 2026-09-29"]), redirect_stdout(io.StringIO()):
                module.main()
        [entry] = list(db["erasure_log"].find())
        self.assertEqual(entry["key_sha256"], hashlib.sha256(b"PHONE#+919876543210").hexdigest())
        self.assertEqual(entry["reason"], "customer email 2026-09-29")
        self.assertNotIn("9876543210", str(entry))


if __name__ == "__main__":
    unittest.main()
