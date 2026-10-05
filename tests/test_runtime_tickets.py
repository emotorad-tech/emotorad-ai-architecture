"""The runtime's side of real tickets (spec 2026-10-05, part 2): the facts
the ticket tools get, the state kept for them, the run's transcript on every
turn of the run, where earlier runs end, and dealers kept off Desk."""

import unittest

from emotorad_ai.adapters import DealerWhatsAppAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import (
    ConversationConflict,
    ConversationState,
    InMemoryConversationStore,
    StoreUnavailable,
    utc_now_iso,
)
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.media_records import KIND_IMAGE, SOURCE_INLINE, media_record
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime, coverage_fact
from emotorad_ai.tickets.clock import parse
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import (
    CREATE_SUPPORT_TICKET,
    LOOKUP_WARRANTY_RECORD,
    RAISE_INTAKE_TICKET,
    MockTicketSystem,
    build_registry,
)
from emotorad_ai.tools.registry import ToolError, err, ok
from emotorad_ai.tools.verification import VERIFIED_TTL_SECONDS, VerificationStore
from tests.test_ticket_tools_desk import FAKE, FRAME, TODAY, desk, fake_bikes, runtime_with, whatsapp

# Two people need two numbers with bikes on record: the fixture numbers the
# verify-first tests use (tests/test_origin_runtime.py), invented, not real.
FIRST = fixtures.PHONE_AMIIGO_TEST_RIDER  # two fixture bikes
SECOND = "+919876543210"                 # one fixture bike
STRANGER_FIRST_WORDS = "hello, my motor is making a noise"


class WebChat:
    """A website visitor through verify first (tests.test_verify_first.Chat),
    on a ticket system the test chooses."""

    def __init__(self, tickets=None, replies=(), clock=None, conversations=None):
        self.store = VerificationStore(clock=clock) if clock else VerificationStore()
        self.registry = build_registry(verification=self.store, today=TODAY, ticket_system=tickets)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = conversations if conversations is not None else InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            self_service_identity=True, phone_resolver=self.store.verified_phone,
            otp_verified_at=self.store.verified_on, verify_first=True,
        )

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

    def state(self):
        return self.conversations.peek("c1")


def two_people(tickets=None, before_stranger=None, conversations=None):
    """Person one verifies, picks a bike and reports a swollen pack. Their
    proof lapses; person two, on the same browser, writes, verifies (the
    conversation restarts for them) and reports smoke. Returns the chat,
    both safety replies and person one's run start. `before_stranger` runs
    just before person two's first message, as the API's media record of a
    photo sent with it would."""
    now = [0.0]
    chat = WebChat(tickets=tickets, replies=[say("Is the charger light on?")] * 4, clock=lambda: now[0],
                   conversations=conversations)
    chat.say("my battery isn't charging")
    chat.say(FIRST[3:])
    chat.say(chat.store.pending_code("c1"))
    chat.say("2")
    first = chat.say("my battery is swollen")
    first_started = chat.state().started_at
    now[0] += VERIFIED_TTL_SECONDS + 1  # person one's proof lapses
    if before_stranger is not None:
        before_stranger(chat)
    chat.say(STRANGER_FIRST_WORDS)
    chat.say(SECOND[3:])
    chat.say(chat.store.pending_code("c1"))
    chat.say("yes")
    second = chat.say("there is smoke coming from the battery")
    return chat, first, second, first_started


def web(runtime, text, cid="web-1", **metadata):
    """An anonymous website visitor, with no verify-first step."""
    return runtime.handle(InboundMessage(
        conversation_id=cid, persona="customer", channel="website_chat", message_text=text,
        identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), entry_metadata=metadata))


class ClosingTickets(MockTicketSystem):
    """The mock, noting every close_runs call, in `order` when given."""

    def __init__(self, order=None):
        super().__init__()
        self.closed = []
        self.order = order

    def close_runs(self, conversation_id, new_started_at):
        self.closed.append((conversation_id, new_started_at))
        if self.order is not None:
            self.order.append("close_runs")


class RecordingStore(InMemoryConversationStore):
    """Notes in `order` when a turn is recorded."""

    def __init__(self, order):
        super().__init__()
        self.order = order

    def record_turn(self, state, inbound, reply, summary=None):
        self.order.append("record_turn:%d" % state.turns)
        super().record_turn(state, inbound, reply, summary)


class UnrecordedWhileFailing(InMemoryConversationStore):
    """Records no turn while `failing` is true: the transcript is down, the
    working state is not."""

    def __init__(self):
        super().__init__()
        self.failing = True

    def record_turn(self, state, inbound, reply, summary=None):
        if self.failing:
            raise StoreUnavailable("transcript down")
        super().record_turn(state, inbound, reply, summary)


class BrokenClose(MockTicketSystem):
    def close_runs(self, conversation_id, new_started_at):
        raise RuntimeError("store down")


class OtherServerFirst(InMemoryConversationStore):
    """Copies on every load, as a database does. The next save finds that
    another server saved first, having made the change `change` makes."""

    def __init__(self, change):
        super().__init__()
        self.change = change

    def get(self, conversation_id):
        return ConversationState.from_json(super().get(conversation_id).to_json())

    def save(self, state):
        if self.change is not None:
            change, self.change = self.change, None
            stored = super().get(state.conversation_id)
            change(stored)
            stored.version += 1
            raise ConversationConflict("another server saved first")
        super().save(state)


class RunTranscriptTests(unittest.TestCase):
    def test_the_second_persons_ticket_carries_none_of_the_first_persons_turns(self):
        chat, first, second, _ = two_people()
        self.assertTrue(first.ticket_id and second.ticket_id)
        self.assertNotEqual(first.ticket_id, second.ticket_id)
        transcript = chat.registry.tickets.tickets[second.ticket_id]["transcript"]
        self.assertIn("Customer: there is smoke coming from the battery", transcript)
        for earlier in ("my battery isn't charging", "my battery is swollen", STRANGER_FIRST_WORDS):
            self.assertNotIn(earlier, transcript)

    def test_the_first_persons_ticket_gets_none_of_the_second_persons_turns(self):
        chat, first, _, _ = two_people()
        transcript = chat.registry.tickets.tickets[first.ticket_id]["transcript"]
        self.assertIn("Customer: my battery is swollen", transcript)
        # Written after person one's proof lapsed: not theirs to read.
        for later in (STRANGER_FIRST_WORDS, "there is smoke coming from the battery"):
            self.assertNotIn(later, transcript)

    def test_a_ticket_gets_the_runs_later_turns_too(self):
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, [say("Thank you for letting me know.")] * 3)
        first = runtime.handle(whatsapp("my battery is swollen"))
        runtime.handle(whatsapp("I have moved it outside"))
        transcript = registry.tickets.tickets[first.ticket_id]["transcript"]
        self.assertIn("Customer: my battery is swollen", transcript)
        self.assertIn("Customer: I have moved it outside", transcript)


class CloseRunsTests(unittest.TestCase):
    def test_each_run_is_closed_once_on_its_first_turn(self):
        tickets = ClosingTickets()
        chat, _, _, first_started = two_people(tickets)
        second_started = chat.state().started_at
        self.assertNotEqual(first_started, second_started)
        self.assertEqual(tickets.closed, [("c1", first_started), ("c1", second_started)])

    def test_a_runs_first_turn_closes_earlier_runs_before_it_is_recorded(self):
        # Amendment A, finding 10: a worker pass between the record and the
        # close could post the new run's first turn to the earlier run's ticket.
        order = []
        registry = build_registry(today=TODAY, ticket_system=ClosingTickets(order), warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, conversations=RecordingStore(order))
        runtime.handle(whatsapp("my battery is swollen"))
        self.assertEqual(order[:2], ["close_runs", "record_turn:1"])

    def test_the_first_persons_desk_record_ends_before_the_strangers_first_turn(self):
        # Amendment A, finding 9: between person one's proof lapsing and
        # restart_for, the turns are the next visitor's, so person one's run
        # ends before the first of them, and the worker never posts them.
        router, store = desk()
        chat, first, second, first_started = two_people(router)
        second_started = chat.state().started_at
        self.assertTrue(is_desk_reference(first.ticket_id) and is_desk_reference(second.ticket_id))
        theirs, ours = store.get(first.ticket_id), store.get(second.ticket_id)
        stranger = next(turn for turn in chat.conversations.transcript("c1") if turn.text == STRANGER_FIRST_WORDS)
        self.assertEqual(theirs["started_at"], first_started)
        self.assertIsNotNone(theirs["ended_at"])
        self.assertLess(parse(first_started), parse(theirs["ended_at"]))
        self.assertLessEqual(parse(theirs["ended_at"]), parse(stranger.at))
        swollen = next(turn for turn in chat.conversations.transcript("c1") if turn.text == "my battery is swollen")
        self.assertLess(parse(swollen.at), parse(theirs["ended_at"]))  # person one's own turns stay in
        self.assertEqual((ours["started_at"], ours["ended_at"]), (second_started, None))

    def test_the_strangers_turns_never_wake_the_first_persons_desk_record(self):
        router, store = desk()
        _, first, _, _ = two_people(router)
        # Woken once, at the end of the turn that raised it.
        self.assertEqual(store.get(first.ticket_id)["wake"], 1)

    def test_a_photo_sent_with_the_strangers_first_message_is_outside_the_first_persons_run(self):
        # The API records a photo sent with a message before the turn runs
        # (api._persist_media), so the stranger's photo is older than their
        # turn. Person one's run must end before it too.
        router, store = desk()
        photos = []

        def stranger_sends_a_photo(chat):
            photo = media_record(bucket="media-test", key="customers/aid-1/c1/stranger.jpg", kind=KIND_IMAGE,
                                 mime_type="image/jpeg", size_bytes=1000, conversation_id="c1",
                                 cluster_id="cl-1", source=SOURCE_INLINE, stored_at=utc_now_iso())
            chat.conversations.record_media(photo)
            photos.append(photo)

        _, first, _, _ = two_people(router, before_stranger=stranger_sends_a_photo)
        self.assertLessEqual(parse(store.get(first.ticket_id)["ended_at"]), parse(photos[0]["stored_at"]))

    def test_with_none_of_the_runs_turns_on_record_it_ends_when_the_strangers_message_arrived(self):
        # The run ends before the stranger's turn is recorded (the ruling for
        # Task 13), so with no earlier turn to end after, it ends at the
        # message's arrival, which is before the turn's record.
        router, store = desk()

        def transcript_back(chat):
            chat.conversations.failing = False

        chat, first, _, first_started = two_people(router, before_stranger=transcript_back,
                                                   conversations=UnrecordedWhileFailing())
        stranger = next(turn for turn in chat.conversations.transcript("c1") if turn.text == STRANGER_FIRST_WORDS)
        ended = store.get(first.ticket_id)["ended_at"]
        self.assertLess(parse(first_started), parse(ended))
        self.assertLessEqual(parse(ended), parse(stranger.at))

    def test_a_strangers_turn_ends_the_run_before_it_is_recorded(self):
        # The ruling for Task 13: the run's end is set before the stranger's
        # first turn is recorded, so no worker pass in between can find that
        # turn inside the first person's run.
        order = []
        router, _ = desk()
        closing = router.close_runs

        def close_runs(conversation_id, new_started_at):
            order.append("close_runs")
            closing(conversation_id, new_started_at)

        router.close_runs = close_runs
        chat, _, _, _ = two_people(router, conversations=RecordingStore(order))
        stranger = next(turn for turn in chat.conversations.transcript("c1") if turn.text == STRANGER_FIRST_WORDS)
        recorded = "record_turn:%d" % ((stranger.n + 1) // 2)
        self.assertEqual(order[order.index(recorded) - 1], "close_runs")

    def test_a_failed_close_is_logged_and_the_turn_goes_on(self):
        registry = build_registry(today=TODAY, ticket_system=BrokenClose(), warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        self.assertTrue(reply.ticket_id)
        failed = [e for e in runtime.log.events if e["event"] == "close_runs_failed"]
        self.assertEqual([e["error"] for e in failed], ["RuntimeError"])


class DeskFactsTests(unittest.TestCase):
    def test_a_dealers_safety_report_records_no_desk_ticket_and_calls_no_model(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(DealerWhatsAppAdapter(runtime.resolver).to_message(
            {"from": "919000000001", "text": "a customer's battery is swollen here in the shop",
             "conversation_id": "d1"}))
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertIn(reply.ticket_id, router.tickets)
        self.assertFalse(is_desk_reference(reply.ticket_id))
        started = runtime.conversations.peek("d1").started_at
        self.assertIsNone(store.by_source_key("d1:%s:create_support_ticket:safety:d1:%s" % (started, started)))
        self.assertEqual(store.listing("test"), [])
        self.assertEqual(llm.requests, [])

    def test_a_customers_safety_report_is_a_desk_safety_record_with_the_runs_facts(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        self.assertTrue(is_desk_reference(reply.ticket_id))
        record = store.get(reply.ticket_id)
        started = runtime.conversations.peek("wa-1").started_at
        self.assertEqual((record["kind"], record["urgent"], record["identity"]), ("safety", True, "verified"))
        self.assertEqual((record["conversation_id"], record["started_at"], record["channel"], record["cluster_id"]),
                         ("wa-1", started, "whatsapp", "cl-1"))
        self.assertEqual((record["phone"], record["coverage"], record["customer_name"]),
                         (FAKE, "computed", "Ananya Rao"))
        self.assertEqual(record["source_key"], "wa-1:%s:create_support_ticket:safety:wa-1:%s" % (started, started))
        self.assertEqual(llm.requests, [])

    def test_every_turn_of_the_run_wakes_its_desk_record(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, [say("Thank you. Please keep it outside.")] * 3)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        self.assertEqual(store.get(reply.ticket_id)["wake"], 1)
        runtime.handle(whatsapp("I have moved it outside"))
        self.assertEqual(store.get(reply.ticket_id)["wake"], 2)

    def test_an_agents_ticket_gets_the_runs_facts(self):
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, _ = runtime_with(registry, [
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "t1"),
            say("I have raised a ticket for the team."),
        ])
        # The customer sent a photo earlier: a fault ticket needs one (test_evidence_before_ticket).
        runtime.conversations.get("wa-1").evidence_seen = True
        reply = runtime.handle(whatsapp("the charger light stays off", pinned_agent=BATTERY))
        ticket = registry.tickets.tickets[reply.ticket_id]
        started = runtime.conversations.peek("wa-1").started_at
        self.assertEqual((ticket["started_at"], ticket["channel"], ticket["coverage"], ticket["identity"]),
                         (started, "whatsapp", "computed", "verified"))


class TypedNumberTests(unittest.TestCase):
    def runtime(self, replies=4):
        runtime, _ = runtime_with(build_registry(today=TODAY), [say("Is the charger light on?")] * replies)
        return runtime

    def test_the_latest_indian_mobile_typed_in_the_run_is_kept(self):
        runtime = self.runtime()
        web(runtime, "my battery won't charge, my number is 99999 99999", pinned_agent=BATTERY)
        self.assertEqual(runtime.conversations.peek("web-1").typed_number, "9999999999")
        # Devanagari digits are read as ASCII first (verify_first.ascii_digits).
        web(runtime, "मेरा नंबर ९९९९९ ९९९९८ है", pinned_agent=BATTERY)
        self.assertEqual(runtime.conversations.peek("web-1").typed_number, "9999999998")
        web(runtime, "the light is off", pinned_agent=BATTERY)
        self.assertEqual(runtime.conversations.peek("web-1").typed_number, "9999999998")

    def test_a_number_that_is_not_an_indian_mobile_is_not_kept(self):
        runtime = self.runtime()
        web(runtime, "my Spanish number is +34 612 345 678", pinned_agent=BATTERY)
        web(runtime, "or try 12345 67890", pinned_agent=BATTERY)
        self.assertIsNone(runtime.conversations.peek("web-1").typed_number)

    def test_a_spent_code_is_never_read_as_a_number(self):
        runtime = self.runtime()
        web(runtime, "hello", pinned_agent=BATTERY)
        # Planted as _remember_lookup records a code verify_identity accepted.
        runtime.conversations.peek("web-1").consumed_codes.append("9999999999")
        web(runtime, "9999999999", pinned_agent=BATTERY)
        self.assertIsNone(runtime.conversations.peek("web-1").typed_number)

    def test_a_dealer_typing_a_number_keeps_nothing(self):
        runtime, _ = runtime_with(build_registry(today=TODAY), [say("Noted.")] * 2)
        runtime.handle(DealerWhatsAppAdapter(runtime.resolver).to_message(
            {"from": "919000000001", "text": "my customer's number is 99999 99999", "conversation_id": "d1"}))
        self.assertIsNone(runtime.conversations.peek("d1").typed_number)

    def test_an_unverified_visitors_intake_ticket_calls_back_the_typed_number(self):
        verification = VerificationStore()
        registry = build_registry(today=TODAY, verification=verification)
        runtime, _ = runtime_with(registry, [
            call_tool(RAISE_INTAKE_TICKET, {"summary": "Charger light stays off.", "stated_name": "Radhika",
                                            "idempotency_key": "intake-1"}, "t1"),
            say("I have passed this to our support team."),
        ], self_service_identity=True, phone_resolver=verification.verified_phone)
        reply = web(runtime, "my battery won't charge and the code never came. my number is 99999 99999",
                    pinned_agent=BATTERY)
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"], ticket["phone"]), ("intake", "unverified", FAKE))
        self.assertIn("[phone]", ticket["transcript"])
        self.assertNotIn("99999", ticket["transcript"])


class NewStateFieldTests(unittest.TestCase):
    FIELDS = {"typed_number": "9999999999", "lookup_error": "oms_unavailable", "awaiting_callback": "handover",
              "callback_asks": 1, "last_code_phone": "9999999998"}

    def test_they_round_trip_and_an_older_state_loads_without_them(self):
        state = ConversationState("c1", **self.FIELDS)
        self.assertEqual(ConversationState.from_json(state.to_json()), state)
        old = ConversationState.from_json('{"conversation_id": "c1"}')
        self.assertEqual((old.typed_number, old.lookup_error, old.awaiting_callback, old.callback_asks,
                          old.last_code_phone), (None, None, None, 0, None))

    def test_a_new_person_starts_with_none_of_them(self):
        state = ConversationState("c1", turns=3, user_key="PHONE#" + FIRST, **self.FIELDS)
        state.restart_for("PHONE#" + SECOND, "2026-10-05T10:00:00.000000+00:00")
        self.assertEqual((state.typed_number, state.lookup_error, state.awaiting_callback, state.callback_asks,
                          state.last_code_phone), (None, None, None, 0, None))

    def test_forgetting_the_bike_forgets_its_lookup_and_its_failure_not_the_number(self):
        state = ConversationState("c1", selected_frame=FRAME, coverage_result=ok({"bikes": []}), **self.FIELDS)
        state.forget_bike()
        self.assertIsNone(state.coverage_result)
        self.assertIsNone(state.lookup_error)
        self.assertEqual(state.typed_number, "9999999999")


class LookupErrorTests(unittest.TestCase):
    def test_an_oms_outage_is_kept_until_a_lookup_works(self):
        outage = [True]

        def source(phone):
            if outage[0]:
                raise ToolError("oms_unavailable", "The warranty system is not responding.", retryable=True)
            return fake_bikes(phone)

        runtime, _ = runtime_with(build_registry(today=TODAY, warranty_source=source), [
            call_tool(LOOKUP_WARRANTY_RECORD, {}, "t1"), say("Is the charger light on?"),
            call_tool(LOOKUP_WARRANTY_RECORD, {}, "t2"), say("Thanks. Is it plugged in at the wall?"),
        ])
        runtime.handle(whatsapp("my battery won't charge", pinned_agent=BATTERY))
        state = runtime.conversations.peek("wa-1")
        self.assertEqual(state.lookup_error, "oms_unavailable")
        self.assertEqual(coverage_fact(state), "oms_unavailable")
        outage[0] = False
        runtime.handle(whatsapp("the light is off", pinned_agent=BATTERY))
        state = runtime.conversations.peek("wa-1")
        self.assertIsNone(state.lookup_error)
        self.assertEqual(coverage_fact(state), "computed")

    def test_only_the_two_lookup_failures_are_kept(self):
        runtime, _ = runtime_with(build_registry(today=TODAY))
        state = ConversationState("c1")
        runtime._remember_lookup(state, LOOKUP_WARRANTY_RECORD, {}, err("no_warranty_record", "No bike."))
        self.assertEqual(state.lookup_error, "no_warranty_record")
        runtime._remember_lookup(state, LOOKUP_WARRANTY_RECORD, {},
                                 err("tool_exception", "TypeError: x", retryable=True))
        self.assertEqual(state.lookup_error, "no_warranty_record")
        runtime._remember_lookup(state, LOOKUP_WARRANTY_RECORD, {}, ok({"bike_count": 0, "bikes": []}))
        self.assertIsNone(state.lookup_error)


class CoverageFactTests(unittest.TestCase):
    ONE = {"frame_number": FRAME, "bike_ref": FRAME, "coverage_status": "computed"}
    OTHER = {"frame_number": "DDL32022119302", "bike_ref": "DDL32022119302",
             "coverage_status": "purchase_date_missing"}

    @staticmethod
    def looked_up(*bikes):
        # The envelope lookup_warranty_record answers with (tools/mocks.py).
        return ok({"customer_name": "Ananya Rao", "bike_count": len(bikes), "bikes": list(bikes)},
                  freshness_seconds=300)

    @staticmethod
    def resolved(bikes=(), method="verified", error=None):
        return ResolvedIdentity(persona="customer", method=method, identity=Identity(strength=VERIFIED, phone=FAKE),
                                bikes=list(bikes), error=error)

    def test_the_only_bike_of_the_last_lookup(self):
        self.assertEqual(coverage_fact(ConversationState("c1", coverage_result=self.looked_up(self.ONE))), "computed")

    def test_the_chosen_bike_of_several(self):
        state = ConversationState("c1", coverage_result=self.looked_up(self.ONE, self.OTHER),
                                  selected_frame="DDL32022119302")
        self.assertEqual(coverage_fact(state), "purchase_date_missing")

    def test_several_bikes_and_none_chosen_says_nothing(self):
        self.assertIsNone(coverage_fact(ConversationState("c1", coverage_result=self.looked_up(self.ONE, self.OTHER))))

    def test_this_turns_identity_lookup_when_the_agent_made_none(self):
        self.assertEqual(coverage_fact(ConversationState("c1"), self.resolved([self.ONE])), "computed")

    def test_the_last_lookup_wins_over_this_turns_identity_lookup(self):
        state = ConversationState("c1", coverage_result=self.looked_up(
            dict(self.ONE, coverage_status="computed_from_registration")))
        self.assertEqual(coverage_fact(state, self.resolved([self.ONE])), "computed_from_registration")

    def test_a_failure_only_when_no_bike_is_known(self):
        self.assertEqual(coverage_fact(ConversationState("c1", lookup_error="oms_unavailable")), "oms_unavailable")
        no_record = self.resolved(method="no_warranty_record", error="no_warranty_record")
        self.assertEqual(coverage_fact(ConversationState("c1"), no_record), "no_warranty_record")
        broken = self.resolved(method="oms_error", error="tool_exception")
        self.assertIsNone(coverage_fact(ConversationState("c1"), broken))

    def test_an_unlisted_bike_takes_no_listed_bikes_cover(self):
        state = ConversationState("c1", coverage_result=self.looked_up(self.ONE),
                                  unlisted_bike={"frame_number": "EMXP2026009999", "model": "EMX Plus"})
        self.assertIsNone(coverage_fact(state, self.resolved([self.ONE])))


class MergeTests(unittest.TestCase):
    def test_a_merged_turn_keeps_what_it_changed_and_the_other_servers_rest(self):
        def other_server(state):
            state.last_code_phone, state.callback_asks, state.lookup_error = "9999999997", 1, "oms_unavailable"

        store = OtherServerFirst(other_server)
        runtime, llm = runtime_with(build_registry(today=TODAY, warranty_source=fake_bikes), conversations=store)
        reply = runtime.handle(whatsapp("my battery is swollen, my number is 99999 99998"))
        saved = store.peek("wa-1")
        self.assertIn("merged_after_conflict", saved.transitions)
        self.assertTrue(reply.ticket_id)
        self.assertEqual(saved.typed_number, "9999999998")  # this turn's
        self.assertEqual((saved.last_code_phone, saved.callback_asks, saved.lookup_error),
                         ("9999999997", 1, "oms_unavailable"))  # the other server's
        self.assertEqual(llm.requests, [])


if __name__ == "__main__":
    unittest.main()
