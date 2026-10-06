"""Mutation-audit probes for persistence and concurrency (2026-09-29).

Each test closes a gap that mutation testing demonstrated: a small, plausible
fault in the conversation stores, the idempotency receipts, the registry's
claim-before-execute or the runtime's save loop that the rest of the suite let
through. The docstring names the fault each one kills.
"""

import copy
import hashlib
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import mongomock

from emotorad_ai.contract import Attachment
from emotorad_ai.conversation import (
    ConversationState,
    InMemoryConversationStore,
    StoreUnavailable,
    TranscriptTurn,
    recorded_url,
    render_transcript,
    summary_key,
)
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.llm import call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.stores.mongo import MongoConversationStore, MongoIdempotencyStore, ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, SEND_GUIDE_MEDIA, build_registry
from emotorad_ai.tools.registry import IdempotencyStore, ToolContext, ToolRegistry, ok
from tests.clock import mongomock_clock_at
from tests.store_contract import inbound, reply, summary
from tests.test_runtime_persistence import ConflictingStore, runtime_on, send

TODAY = date(2026, 7, 28)
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)

# mongomock expires TTL documents against the real clock, and these stores run
# on NOW: once the real date passed NOW + 48 hours (1 October 2026, 10:00 UTC),
# every saved conversation counted as expired. mongomock's clock is pinned too.
_MONGOMOCK_CLOCK = mongomock_clock_at(NOW)


def setUpModule():
    _MONGOMOCK_CLOCK.start()


def tearDownModule():
    _MONGOMOCK_CLOCK.stop()
USER = "PHONE#+919876543210"
CTX = ToolContext(conversation_id="c1", phone="+919876543210")
TICKET = {"category": "battery_charging", "severity": "normal", "description": "LED stays off.", "idempotency_key": "k1"}
TICKET_CALL = call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1")
COLLECTIONS = ("conversations", "transcript_turns", "conversation_summaries", "idempotency_keys", "conversation_origins")
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def mongo(**kw):
    db = mongomock.MongoClient()["emotorad_ai"]
    ensure_indexes(db)
    return db, MongoConversationStore(db, now=lambda: NOW, **kw)


def both_stores():
    return (("memory", InMemoryConversationStore()), ("mongodb", mongo()[1]))


def load_script(name):
    spec = importlib.util.spec_from_file_location("audit_" + name, SCRIPTS / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- idempotency receipts: both implementations ------------------------------

class ReceiptLifecycle:
    """The claim contract beyond claim-once: what release, get and a
    take-over must never do. Mixed in per implementation."""

    def make(self):
        """(store, advance(seconds))."""
        raise NotImplementedError

    def test_release_never_undoes_a_finished_receipt(self):
        """Kills: release deletes a done receipt, so the next retry runs the write again."""
        store, _ = self.make()
        self.assertIsNone(store.claim("k"))
        store.put("k", ok({"ticket_id": "EM-00001"}))
        store.release("k")
        self.assertEqual(store.get("k"), ok({"ticket_id": "EM-00001"}))
        self.assertEqual(store.claim("k"), ok({"ticket_id": "EM-00001"}))

    def test_a_pending_claim_is_not_a_result(self):
        """Kills: get hands back an in-progress claim as if it were the result."""
        store, _ = self.make()
        store.claim("k")
        self.assertIsNone(store.get("k"))
        store.put("k", ok({"ticket_id": "EM-00001"}))
        self.assertEqual(store.get("k"), ok({"ticket_id": "EM-00001"}))

    def test_a_taken_over_claim_belongs_to_the_new_owner(self):
        """Kills: a take-over that does not renew the claim, so a third caller
        right after it takes over again and the write runs twice."""
        store, advance = self.make()
        self.assertIsNone(store.claim("k"))
        advance(310)  # past the 5-minute lease: the first server died
        self.assertIsNone(store.claim("k"))
        advance(1)
        self.assertEqual(store.claim("k")["error"]["code"], "write_in_progress")


class InMemoryReceiptTests(ReceiptLifecycle, unittest.TestCase):
    def make(self):
        now = [1000.0]
        store = IdempotencyStore(clock=lambda: now[0], lease_seconds=300)
        return store, lambda s: now.__setitem__(0, now[0] + s)


class MongoReceiptTests(ReceiptLifecycle, unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def make(self):
        now = [NOW]
        store = MongoIdempotencyStore(self.db, now=lambda: now[0], lease_seconds=300)
        return store, lambda s: now.__setitem__(0, now[0] + timedelta(seconds=s))

    def test_a_finished_receipt_expires_after_seven_days(self):
        """Kills: put writes a receipt with no expiry, so a result about the
        person (a booking's customer id) is kept until someone erases it."""
        store, _ = self.make()
        store.put("c1:book_service_slot:k", ok({"customer_id": "+919876543210"}))
        doc = self.db["idempotency_keys"].find_one({"_id": "c1:book_service_slot:k"})
        self.assertEqual(doc["expires_at"].replace(tzinfo=timezone.utc), NOW + timedelta(days=7))


# -- the registry: key scope and claim order -----------------------------------

class KeyScopeTests(unittest.TestCase):
    def test_one_key_in_two_conversations_is_two_tickets(self):
        """Kills: the receipt key without the conversation id. Models reuse
        simple keys, so a second customer would be handed the first one's ticket.
        Otherwise only the smoke script's leftover count notices, by accident."""
        registry = build_registry(today=TODAY)
        first = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), CTX)
        second = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), ToolContext(conversation_id="c2", phone="+919812345678"))
        self.assertNotEqual(first["data"]["ticket_id"], second["data"]["ticket_id"])
        self.assertEqual(len(registry.tickets.tickets), 2)

    def test_one_key_on_two_tools_runs_both(self):
        """Kills: the receipt key without the tool name, where a second write
        tool returns the first tool's result and never runs."""
        registry, calls = ToolRegistry(), []
        for name in ("write_a", "write_b"):
            registry.register(name, "d", parameters={"idempotency_key": {"type": "string"}},
                              required=("idempotency_key",), write=True)(
                lambda idempotency_key, _name=name: calls.append(_name) or {"tool": _name})
        a = registry.call("write_a", {"idempotency_key": "k"}, CTX)
        b = registry.call("write_b", {"idempotency_key": "k"}, CTX)
        self.assertEqual((a["data"], b["data"]), ({"tool": "write_a"}, {"tool": "write_b"}))
        self.assertEqual(calls, ["write_a", "write_b"])

    def test_a_call_refused_for_a_missing_argument_holds_no_claim(self):
        """Kills: required arguments checked after the claim, so the corrected
        retry is told the write is already in progress for five minutes."""
        registry, calls = ToolRegistry(), []

        @registry.register("w", "d", parameters={"idempotency_key": {"type": "string"}, "note": {"type": "string"}},
                           required=("idempotency_key", "note"), write=True)
        def w(idempotency_key, note):
            calls.append(note)
            return {"note": note}

        refused = registry.call("w", {"idempotency_key": "k"}, CTX)
        self.assertEqual(refused["error"]["code"], "missing_arguments")
        self.assertEqual(registry.call("w", {"idempotency_key": "k", "note": "x"}, CTX), ok({"note": "x"}))
        self.assertEqual(calls, ["x"])


# -- the MongoDB store: size trim and photo compaction -------------------------

def photo(fill, size):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": fill * size}}


class PhotoCompactionTests(unittest.TestCase):
    def test_the_latest_photo_is_kept_while_older_ones_are_compacted(self):
        """Kills: compaction that also strips the latest customer turn's photo."""
        log = EventLog(path=None)
        _, store = mongo(max_state_bytes=6_000, log=log)
        state = store.get("c1")
        latest = photo("B", 500)
        state.history = [
            {"role": "user", "content": [{"type": "text", "text": "battery dead, here it is"}, photo("A", 8_000)]},
            {"role": "assistant", "content": [{"type": "text", "text": "Thanks."}]},
            {"role": "user", "content": [{"type": "text", "text": "and the light now"}, latest]},
            {"role": "assistant", "content": [{"type": "text", "text": "Ok."}]},
        ]
        store.save(state)
        saved = store.get("c1").history
        self.assertEqual(len(saved), 4)
        self.assertEqual(saved[0]["content"][1]["type"], "text")
        self.assertNotIn("A" * 100, str(saved))
        self.assertEqual(saved[2]["content"][1], latest)
        [event] = [e for e in log.events if e["event"] == "history_media_compacted"]
        self.assertEqual(event["blocks"], 1)

    def test_a_state_under_the_limit_keeps_every_photo(self):
        """Kills: compaction that runs whatever the size, stripping photos the
        model may still need from every ordinary conversation."""
        _, store = mongo()
        state = store.get("c1")
        history = [
            {"role": "user", "content": [{"type": "text", "text": "here it is"}, photo("A", 1_000)]},
            {"role": "assistant", "content": [{"type": "text", "text": "Thanks."}]},
            {"role": "user", "content": "the light is red"},
            {"role": "assistant", "content": [{"type": "text", "text": "Ok."}]},
        ]
        state.history = copy.deepcopy(history)
        store.save(state)
        self.assertEqual(store.get("c1").history, history)


# -- summaries: one per run of a conversation id -------------------------------

class SummaryPerRunTests(unittest.TestCase):
    EARLIER, CURRENT = "2026-09-02T10:00:00+00:00", "2026-09-20T10:00:00+00:00"

    def test_excluding_the_current_run_keeps_an_earlier_run_of_the_same_id(self):
        """Kills: exclusion by conversation id rather than by summary key, which
        hides the earlier run of a WhatsApp thread that went quiet and restarted."""
        for name, store in both_stores():
            with self.subTest(name):
                state = store.get("c1")
                state.user_key, state.turns = USER, 1
                store.record_turn(state, inbound("x"), reply("y"), summary("c1", started_at=self.EARLIER, title="Motor noise"))
                store.record_turn(state, inbound("x"), reply("y"), summary("c1", started_at=self.CURRENT, title="This run"))
                items = store.recent_summaries(USER, exclude=summary_key("c1", self.CURRENT))
                self.assertEqual([(s.conversation_id, s.started_at, s.title) for s in items],
                                 [("c1", self.EARLIER, "Motor noise")])

    def test_memory_shows_an_earlier_run_and_never_this_one(self):
        """Kills: memory built without excluding this run (the existing
        `test_the_current_conversation_is_never_its_own_memory` cannot fail:
        on a first turn no summary exists yet). Also kills exclusion by
        conversation id at the runtime level."""
        for name, store in both_stores():
            with self.subTest(name):
                state = store.get("c1")
                state.started_at, state.user_key, state.turns = self.CURRENT, USER, 1
                store.record_turn(state, inbound("x"), reply("y"), summary("c1", started_at=self.EARLIER, title="Motor is making a noise"))
                store.record_turn(state, inbound("x"), reply("y"), summary("c1", started_at=self.CURRENT, title="This very run"))
                state.turns = 0
                store.save(state)
                runtime = runtime_on(store, [say("Hello again.")])
                send(runtime, "my battery won't charge", cid="c1")
                prompt = runtime.llm.requests[0]["system"]
                self.assertIn("Motor is making a noise", prompt)
                self.assertNotIn("This very run", prompt)


# -- erasure --------------------------------------------------------------------

class ErasureTests(unittest.TestCase):
    def test_erasing_one_conversation_leaves_look_alike_ids_alone(self):
        """Kills: a receipt pattern without the ':' separator (erasing `web.1`
        also erases `web.10`) or without escaping (`web.1` matches `webX1`)."""
        db, store = mongo()
        receipts = MongoIdempotencyStore(db, now=lambda: NOW)
        for key in ("web.1:create_support_ticket:k", "web.10:create_support_ticket:k", "webX1:create_support_ticket:k"):
            receipts.put(key, ok({"ticket_id": key}))
        counts = store.delete_conversation("web.1")
        self.assertEqual(counts["idempotency_keys"], 1)
        self.assertEqual(sorted(d["_id"] for d in db["idempotency_keys"].find()),
                         ["web.10:create_support_ticket:k", "webX1:create_support_ticket:k"])

    def test_a_dry_run_counts_exactly_what_the_deletion_then_removes(self):
        """Kills: a dry run that reports zero, so the person running an erasure
        confirms counts that say nothing is held."""
        db, store = mongo()
        state = store.get("c1")
        state.turns = 1
        store.save(state)
        store.record_turn(state, inbound("12 MG Road"), reply("Thanks."))  # before sign-in: no user key
        state.user_key, state.turns = USER, 2
        store.save(state)
        store.record_turn(state, inbound("signed in"), reply("Hello."), summary("c1"))
        MongoIdempotencyStore(db, now=lambda: NOW).put("c1:book_service_slot:k", ok({"booking_id": "BK-1"}))
        store.record_origin({"_id": "c1#run", "conversation_id": "c1", "started_at": "run", "channel": "website_chat",
                             "country": "IN", "region": None, "city": None, "source": "phone", "db": None,
                             "user_key": USER})
        before = {name: db[name].count_documents({}) for name in COLLECTIONS}
        dry = store.delete_person(USER, dry_run=True)
        self.assertEqual({name: db[name].count_documents({}) for name in COLLECTIONS}, before)
        real = store.delete_person(USER)
        self.assertEqual(real, {"conversations": 1, "transcript_turns": 4, "conversation_summaries": 1,
                                "idempotency_keys": 1, "media": 0, "conversation_origins": 1,
                                "verification_sessions": 0, "conversation_notices": 0, "amiigo_receipts": 0})
        self.assertEqual(dry, real)

    def test_a_conversation_with_no_summary_yet_is_still_erased(self):
        """Kills: the in-memory store finding a person's conversations by their
        summaries alone, which misses one whose summary was never written."""
        for name, store in both_stores():
            with self.subTest(name):
                state = store.get("c1")
                state.user_key, state.turns = USER, 1
                store.save(state)
                store.record_turn(state, inbound("12 MG Road"), reply("Ok."))  # no summary for this turn
                counts = store.delete_person(USER)
                self.assertEqual((counts["conversations"], counts["transcript_turns"]), (1, 2))
                self.assertEqual(store.transcript("c1"), [])
                self.assertIsNone(store.peek("c1"))


# -- the permanent record --------------------------------------------------------

class RecordTests(unittest.TestCase):
    def test_no_inline_file_of_any_type_is_kept(self):
        """Kills: stripping only `data:image` URLs, so an inline PDF or clip is
        kept whole in a record held for ever."""
        self.assertEqual(recorded_url("data:application/pdf;base64,JVBERi0x"), "data:(inline, not kept)")
        for name, store in both_stores():
            with self.subTest(name):
                state = store.get("c1")
                state.turns = 1
                pdf = Attachment(kind="document", url="data:application/pdf;base64," + "J" * 5000)
                store.record_turn(state, inbound("invoice", attachments=[pdf]), reply("Thanks."))
                self.assertEqual(store.transcript("c1")[0].attachments, ({"kind": "document", "url": "data:(inline, not kept)"},))

    def test_a_ticket_transcript_survives_a_bad_time_and_names_every_attachment(self):
        """Kills: a malformed time raising out of the render (the ticket then
        gets no thread at all)."""
        text = render_transcript([
            TranscriptTurn(1, "customer", "hi", "not a time", attachments=({"kind": "video", "url": "u"}, {"url": "u2"})),
            TranscriptTurn(2, "bot", "Hello.", "2026-09-28T09:41:05+00:00"),
        ])
        self.assertEqual(text, "[--:--] Customer: hi [video] [attachment]\n[09:41] Bot: Hello.")


# -- the runtime's save loop ------------------------------------------------------

class NeighbourStore(ConflictingStore):
    """Another conversation on this server raises a ticket while ours runs;
    then our save loses to a second server."""

    log = None

    def save(self, state):
        if self.conflicts and self.log is not None:
            self.log.tool_call("someone-else", CREATE_SUPPORT_TICKET, {"idempotency_key": "k"},
                               {"data": {"ticket_id": "EM-09999"}})
        super().save(state)


class SaveLoopTests(unittest.TestCase):
    def test_another_conversations_ticket_is_not_this_turns_side_effect(self):
        """Kills: side effects not filtered by conversation. The event log is
        shared by every conversation on the server."""
        store = NeighbourStore(conflicts=1)
        runtime = runtime_on(store, [say("First."), say("Second.")])
        store.log = runtime.log
        answer = send(runtime, "my battery won't charge")
        self.assertIn("Second.", answer.text)  # rerun from fresh state, not merged
        self.assertEqual(store.gets, 2)
        self.assertIsNone(answer.ticket_id)

    def test_a_turn_that_only_read_is_rerun_after_a_conflict(self):
        """Kills: any tool call counting as a side effect, so a lookup-only turn
        is merged onto the other server's state instead of rerun on it."""
        store = ConflictingStore(conflicts=1)
        runtime = runtime_on(store, [call_tool(LOOKUP_WARRANTY_RECORD, {}, "t1"), say("First."),
                                     call_tool(LOOKUP_WARRANTY_RECORD, {}, "t2"), say("Second.")])
        answer = send(runtime, "my battery won't charge")
        self.assertIn("Second.", answer.text)
        self.assertEqual(store.gets, 2)

    def test_a_write_that_failed_is_rerun_after_a_conflict(self):
        """Kills: a failed write counting as a side effect. It changed nothing,
        so the turn is rerun on fresh state like any other."""
        bad = dict(TICKET, category="refund_please")
        store = ConflictingStore(conflicts=1)
        runtime = runtime_on(store, [call_tool(CREATE_SUPPORT_TICKET, bad, "t1"), say("First."),
                                     call_tool(CREATE_SUPPORT_TICKET, bad, "t2"), say("Second.")])
        answer = send(runtime, "my battery won't charge")
        self.assertIn("Second.", answer.text)
        self.assertEqual(runtime.registry.tickets.tickets, {})

    def test_a_guide_picture_sent_is_a_side_effect(self):
        """Kills: send_guide_media missing from SIDE_EFFECT_TOOLS, so a
        conflict reruns the turn and the customer is sent the picture twice."""
        registry = build_registry(today=TODAY, guide_media={"soc-button": {"caption": "The SOC button", "url": "https://x.test/soc.jpg"}})
        runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
        mark = len(runtime.log.events)
        runtime.log.tool_call("c1", SEND_GUIDE_MEDIA, {"key": "soc-button"}, {"data": {"sent": True, "media": []}})
        self.assertEqual([w["tool"] for w in runtime._side_effects_since(mark, "c1")], [SEND_GUIDE_MEDIA])

    def test_a_second_clash_while_merging_a_ticket_hands_over_with_its_reference(self):
        """Kills: the handover after a failed merge dropping the ticket the
        turn raised, so the customer has no reference to quote."""
        store = ConflictingStore(conflicts=2)
        # The customer sent a photo earlier: a fault ticket needs evidence (test_evidence_before_ticket).
        InMemoryConversationStore.get(store, "conv-1").evidence_seen = True
        runtime = runtime_on(store, [TICKET_CALL, say("Your reference is EM-00001; the team will call you.")])
        answer = send(runtime, "my battery won't charge")
        self.assertEqual((answer.handled_by, answer.escalated, answer.ticket_id), ("store_unavailable", True, "EM-00001"))
        self.assertIn("EM-00001", answer.text)
        self.assertEqual(len(runtime.registry.tickets.tickets), 1)
        self.assertEqual(len(runtime.llm.requests), 2)  # the turn was not run again
        [escalation] = [e for e in runtime.log.events if e["event"] == "escalation" and e["reason"] == "store_unavailable"]
        self.assertEqual(escalation["ticket_id"], "EM-00001")

    def test_a_store_failure_later_in_a_turn_that_raised_a_ticket_names_it(self):
        """Kills: the graph backstop handing over without the ticket this turn
        already raised."""
        registry = build_registry(today=TODAY)
        real_call = registry.call

        def call(name, arguments, context, **kw):
            if name == LOOKUP_WARRANTY_RECORD and registry.tickets.tickets:
                raise StoreUnavailable("MongoDB find_one failed")  # an unguarded store read inside the turn
            return real_call(name, arguments, context, **kw)

        registry.call = call
        store = InMemoryConversationStore()
        # The customer sent a photo earlier: a fault ticket needs evidence (test_evidence_before_ticket).
        store.get("conv-1").evidence_seen = True
        runtime = runtime_on(store, [TICKET_CALL, call_tool(LOOKUP_WARRANTY_RECORD, {}, "t2"), say("x")],
                             registry=registry)
        answer = send(runtime, "my battery won't charge")
        self.assertEqual((answer.handled_by, answer.ticket_id), ("store_unavailable", "EM-00001"))
        self.assertIn("EM-00001", answer.text)

    def test_a_merge_keeps_this_turns_flags_and_fills_only_what_is_missing(self):
        """Kills: a merge that drops this turn's escalation, evidence or
        disclosure, leaves the person's key unset, overwrites the other
        server's fields, or is never saved."""
        store = ConflictingStore(conflicts=0)
        runtime = runtime_on(store, [])
        theirs = store.get("c1")
        theirs.channel, theirs.context_block, theirs.turns = "whatsapp", "their context", 3
        store.save(theirs)
        ours = ConversationState(conversation_id="c1", escalated=True, evidence_seen=True, disclosed=True, user_key=USER,
                                 channel="website_chat", context_block="our context", cluster_id="cl-1")
        runtime._merge_onto_fresh(ours, [{"role": "user", "content": "the light is off"}], reply("x", ticket_id="EM-00007"))
        saved = store.get("c1")
        self.assertEqual((saved.escalated, saved.evidence_seen, saved.disclosed), (True, True, True))
        self.assertEqual((saved.user_key, saved.cluster_id), (USER, "cl-1"))  # unset there: taken from this turn
        self.assertEqual((saved.channel, saved.context_block), ("whatsapp", "their context"))  # set there: stands
        self.assertEqual((saved.turns, saved.ticket_id, saved.version), (4, "EM-00007", 2))
        self.assertEqual(saved.history[-1], {"role": "user", "content": "the light is off"})


class ConversationOutcomeTests(unittest.TestCase):
    def test_an_escalation_and_a_ticket_outlast_the_turns_after_them(self):
        """Kills: a later ordinary turn resetting the conversation's escalation
        or forgetting its ticket, so the person's history reads "open" with no
        reference to quote."""
        store = InMemoryConversationStore()
        # The customer sent a photo earlier: a fault ticket needs evidence (test_evidence_before_ticket).
        store.get("conv-1").evidence_seen = True
        runtime = runtime_on(store, [TICKET_CALL, say("Your reference is EM-00001; the team will call you."),
                                     say("Anything else?")])
        send(runtime, "my battery won't charge")
        send(runtime, "can I talk to a human")
        third = send(runtime, "ok, and the charger?")
        self.assertFalse(third.escalated)  # the fixture reaches an ordinary turn after both
        saved = store.get("conv-1")
        self.assertEqual((saved.escalated, saved.ticket_id), (True, "EM-00001"))
        [item] = store.recent_summaries(USER)
        self.assertEqual((item.outcome, item.ticket_id), ("escalated", "EM-00001"))

    def test_the_store_down_handover_says_it_is_a_bot(self):
        """Kills: the store-down handover built on a state marked disclosed.
        Nothing is known about the conversation, so the disclosure must go on."""
        class DownStore(InMemoryConversationStore):
            def get(self, conversation_id):
                raise StoreUnavailable("MongoDB find_one failed")

        answer = send(runtime_on(DownStore(), []), "my battery won't charge")
        self.assertEqual(answer.handled_by, "store_unavailable")
        self.assertTrue(has_disclosure(answer.text))

# -- scripts/delete_person.py -------------------------------------------------------

class DeletePersonScriptTests(unittest.TestCase):
    def setUp(self):
        self.client = mongomock.MongoClient()
        self.db = self.client["emotorad_ai"]
        ensure_indexes(self.db)
        self.db["transcript_turns"].insert_many([
            {"_id": "c#00001", "conversation_id": "c", "n": 1, "user_key": USER},
            {"_id": "web-4f2a#00001", "conversation_id": "web-4f2a", "n": 1, "user_key": None},
            {"_id": "web-4f2a#00002", "conversation_id": "web-4f2a", "n": 2, "user_key": None},
            {"_id": "other#00001", "conversation_id": "other", "n": 1, "user_key": "PHONE#+919812345678"},
        ])
        self.module = load_script("delete_person")

    def run_script(self, *argv):
        out = io.StringIO()
        with mock.patch.object(self.module, "connect", lambda db_name: self.client[db_name]), \
                mock.patch.object(sys, "argv", ["delete_person.py"] + list(argv)), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                code = self.module.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def turns(self):
        return sorted(d["_id"] for d in self.db["transcript_turns"].find())

    def test_a_refused_yes_deletes_nothing_and_logs_nothing(self):
        """Kills: the reason check running after the deletion."""
        everything = self.turns()
        for reason in ([], ["--reason", "   "]):  # none, and a blank one
            with self.subTest(reason=reason):
                code, _ = self.run_script("--phone", "9876543210", "--yes", *reason)
                self.assertEqual(code, 2)
                self.assertEqual(self.turns(), everything)
                self.assertEqual(self.db["erasure_log"].count_documents({}), 0)

    def test_a_dry_run_counts_and_writes_no_audit_record(self):
        """Kills: a dry run recorded as an erasure in the audit log."""
        code, out = self.run_script("--phone", "9876543210")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"transcript_turns\s+1\b")
        self.assertEqual(self.db["erasure_log"].count_documents({}), 0)
        self.assertIn("c#00001", self.turns())

    def test_a_number_quoted_in_the_reason_is_not_kept(self):
        """Kills: the reason stored unredacted, putting the phone the log was
        built to avoid straight back into it."""
        self.run_script("--phone", "9876543210", "--yes", "--reason", "call from 9876543210 on 2026-09-29")
        [entry] = list(self.db["erasure_log"].find())
        self.assertNotIn("9876543210", entry["reason"])
        self.assertIn("2026-09-29", entry["reason"])

    def test_one_unkeyed_conversation_is_erased_by_its_id(self):
        """Kills: --conversation-id routed through delete_person, which finds
        nothing for a chat never tied to a verified person."""
        code, _ = self.run_script("--conversation-id", "web-4f2a", "--yes", "--reason", "visitor email")
        self.assertEqual(code, 0)
        self.assertEqual(self.turns(), ["c#00001", "other#00001"])
        [entry] = list(self.db["erasure_log"].find())
        self.assertEqual((entry["kind"], entry["key_sha256"]),
                         ("CONVERSATION", hashlib.sha256(b"CONVERSATION#web-4f2a").hexdigest()))
        self.assertEqual(entry["deleted"]["transcript_turns"], 2)


# -- scripts/mongo_smoke.py ---------------------------------------------------------

class SmokeStepFailureTests(unittest.TestCase):
    def test_a_step_that_raises_is_reported_and_everything_is_still_cleaned_up(self):
        """Kills: an exception between checks escaping run_smoke, or skipping
        the cleanup, which would leave smoke data on the real cluster."""
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        real_handle, calls, lines = Runtime.handle, [], []

        def handle(runtime, message):
            calls.append(message.conversation_id)
            if len(calls) == 4:  # the first turn after the restart
                raise RuntimeError("model went away")
            return real_handle(runtime, message)

        with mock.patch.object(Runtime, "handle", handle):
            passed = load_script("mongo_smoke").run_smoke(client=client, db_name="emotorad_ai", out=lines.append)
        self.assertFalse(passed)
        self.assertTrue(any(line.startswith("FAIL  stopped") for line in lines), lines)
        self.assertTrue(any(line.startswith("PASS  13.") for line in lines), lines)
        for collection in COLLECTIONS:
            self.assertEqual(db[collection].count_documents({}), 0, collection)


if __name__ == "__main__":
    unittest.main()
