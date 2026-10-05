"""The three ticket tools write what a ticket record needs (spec 2026-10-05,
sections 2, 3 and 6): the run, the persona, the kind, the identity and the
facts code worked out, never anything the model chose."""

import unittest
from datetime import date

from emotorad_ai.adapters import VoiceAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, ASSERTED, VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import (
    CREATE_SUPPORT_TICKET,
    RAISE_INTAKE_TICKET,
    SUBMIT_WARRANTY_PROOF,
    MockTicketSystem,
    build_registry,
)
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.tools.verification import VerificationStore

TODAY = date(2026, 10, 5)
FAKE = "+919999999999"
# Ananya's fixture bike, on the fake number, so no test leans on a number
# that looks like someone's.
FAKE_BIKE = dict(fixtures.WARRANTY_RECORDS["+919876543210"][0], mobile=FAKE)
FRAME = FAKE_BIKE["frame_number"]
STARTED = "2026-10-05T09:00:00.000000+00:00"
LATER_RUN = "2026-10-05T11:00:00.000000+00:00"
TICKET = {"category": "battery_charging", "description": "Charger LED stays off; tried another socket.",
          "severity": "normal", "idempotency_key": "k1"}
INTAKE = {"summary": "Charger light stays off.", "stated_name": "Radhika", "stated_contact": "r@example.com",
          "idempotency_key": "i1"}
PROOF = {"frame_number": FRAME, "claimed_purchase_date": "2025-03-14", "purchase_channel": "dealer",
         "proof_url": "https://example.test/invoice.jpg", "idempotency_key": "p1"}
# Every fact the runtime injects into a ticket tool. None may be in a schema.
FACTS = {"phone", "conversation_id", "persona", "started_at", "cluster_id", "channel", "identity_strength",
         "coverage", "typed_number", "ticket_kind", "evidence_seen", "selected_bike", "unlisted_bike"}


def fake_bikes(phone):
    return [dict(FAKE_BIKE)] if phone == FAKE else None


def desk():
    """The router as api.py wires it with Zoho on, over an in-memory store."""
    store = InMemoryTicketStore()
    return TicketRouter(DeskTicketSystem(store, "test", "stage"), MockTicketSystem()), store


def context(phone=FAKE, persona="customer", started_at=STARTED, **facts):
    """A customer's run as the runtime builds it: identity and run on the
    context, conversation facts as late values (Agent._late_facts)."""
    return ToolContext(conversation_id="c1", phone=phone, cluster_id="cl-1", persona=persona, started_at=started_at,
                       late={name: (lambda value=value: value) for name, value in facts.items()})


def verified(**facts):
    return context(identity_strength=VERIFIED, **facts)


def runtime_with(registry, replies=(), **kw):
    llm = ScriptedClaude(list(replies))
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), **kw)
    return runtime, llm


def whatsapp(text, cid="wa-1", phone=FAKE, **metadata):
    return InboundMessage(conversation_id=cid, persona="customer", channel="whatsapp", message_text=text,
                          identity=Identity(cluster_id="cl-1", strength=VERIFIED, phone=phone),
                          entry_metadata=metadata)


class SupportTicketOnDeskTests(unittest.TestCase):
    def setUp(self):
        self.router, self.store = desk()
        self.registry = build_registry(today=TODAY, ticket_system=self.router, warranty_source=fake_bikes)

    def record(self, arguments, ctx):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, arguments, ctx)
        self.assertFalse(is_error(envelope), envelope)
        reference = envelope["data"]["ticket_id"]
        self.assertTrue(is_desk_reference(reference))
        return self.store.get(reference)

    def test_a_verified_customers_ticket_carries_the_run_and_what_code_worked_out(self):
        record = self.record(dict(TICKET), verified(channel="whatsapp", coverage="computed"))
        self.assertEqual((record["kind"], record["identity"], record["urgent"]), ("support", "verified", False))
        self.assertEqual((record["conversation_id"], record["started_at"], record["cluster_id"], record["channel"]),
                         ("c1", STARTED, "cl-1", "whatsapp"))
        self.assertEqual((record["phone"], record["coverage"], record["customer_name"]),
                         (FAKE, "computed", "Ananya Rao"))
        self.assertEqual(record["source_key"], "c1:%s:create_support_ticket:k1" % STARTED)
        self.assertIn(FRAME, str(record["bike"]))

    def test_a_model_battery_safety_ticket_is_urgent_and_still_a_support_ticket(self):
        record = self.record(dict(TICKET, category="battery_safety", severity="critical"), verified())
        self.assertEqual((record["kind"], record["urgent"]), ("support", True))

    def test_only_the_safety_branchs_fact_makes_a_safety_ticket(self):
        key = "safety:c1:%s" % STARTED
        record = self.record(dict(TICKET, category="battery_safety", severity="critical", idempotency_key=key),
                             verified(ticket_kind="safety"))
        self.assertEqual((record["kind"], record["urgent"]), ("safety", True))
        self.assertEqual(record["source_key"], "c1:%s:create_support_ticket:%s" % (STARTED, key))

    def test_the_model_can_set_none_of_the_facts_nor_the_kind(self):
        forged = dict(TICKET, ticket_kind="safety", identity_strength=VERIFIED, coverage="computed",
                      channel="whatsapp", started_at=LATER_RUN, persona="dealer", cluster_id="cl-x",
                      conversation_id="someone-else")
        record = self.record(forged, context())
        self.assertEqual((record["kind"], record["identity"], record["coverage"], record["channel"]),
                         ("support", "unverified", None, None))
        self.assertEqual((record["conversation_id"], record["started_at"], record["cluster_id"]),
                         ("c1", STARTED, "cl-1"))

    def test_one_ticket_per_key_and_run_across_retries_and_registries(self):
        # A second registry has its own receipts, as a second server would.
        other = build_registry(today=TODAY, ticket_system=self.router, warranty_source=fake_bikes)
        first = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        again = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        elsewhere = other.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        reference = first["data"]["ticket_id"]
        self.assertEqual({again["data"]["ticket_id"], elsewhere["data"]["ticket_id"]}, {reference})
        self.assertEqual(self.store.by_source_key("c1:%s:create_support_ticket:k1" % STARTED)["_id"], reference)

    def test_the_same_key_in_a_new_run_is_a_new_ticket(self):
        first = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        later = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified(started_at=LATER_RUN))
        self.assertNotEqual(first["data"]["ticket_id"], later["data"]["ticket_id"])

    def test_a_dealers_or_an_unknown_personas_ticket_stays_on_the_mock(self):
        for persona, phone in (("dealer", "+919000000001"), (None, FAKE)):
            with self.subTest(persona=persona):
                router, store = desk()
                registry = build_registry(today=TODAY, ticket_system=router, warranty_source=fake_bikes)
                envelope = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET),
                                         context(phone=phone, persona=persona, identity_strength=VERIFIED))
                reference = envelope["data"]["ticket_id"]
                self.assertRegex(reference, r"^EM-\d{5}$")
                self.assertIn(reference, router.tickets)
                self.assertIsNone(store.by_source_key("c1:%s:create_support_ticket:k1" % STARTED))

    def test_a_caller_id_gets_no_bike_no_name_no_cover_and_is_unverified(self):
        record = self.record(dict(TICKET, frame_number=FRAME), context(identity_strength=ASSERTED, coverage="computed"))
        self.assertEqual((record["identity"], record["customer_name"], record["coverage"]), ("unverified", None, None))
        self.assertNotIn(FRAME, str(record))


class SupportTicketOnTheMockTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY, warranty_source=fake_bikes)

    def call(self, arguments, ctx):
        return self.registry.call(CREATE_SUPPORT_TICKET, arguments, ctx)

    def test_the_mock_keeps_every_field_it_kept_and_the_new_ones(self):
        envelope = self.call(dict(TICKET), verified(channel="whatsapp", coverage="computed"))
        reference = envelope["data"]["ticket_id"]
        self.assertRegex(reference, r"^EM-\d{5}$")
        ticket = self.registry.tickets.tickets[reference]
        self.assertEqual((ticket["phone"], ticket["category"], ticket["severity"], ticket["description"]),
                         (FAKE, "battery_charging", "normal", TICKET["description"]))
        self.assertEqual((ticket["frame_number"], ticket["frame_number_source"], ticket["bike_model"]),
                         (FRAME, None, "EMX Plus"))
        self.assertEqual((ticket["kind"], ticket["conversation_id"], ticket["started_at"], ticket["channel"]),
                         ("support", "c1", STARTED, "whatsapp"))
        self.assertEqual((ticket["identity"], ticket["coverage"], ticket["customer_name"]),
                         ("verified", "computed", "Ananya Rao"))
        self.assertEqual(envelope["data"]["expected_response"], "within 24 hours on working days")

    def test_a_caller_id_is_never_checked_against_the_bikes_on_that_number(self):
        stranger = "DDL32022119302"  # Rohit's fixture bike, not on the fake number
        refused = self.call(dict(TICKET, frame_number=stranger, idempotency_key="k2"), verified())
        self.assertEqual(refused["error"]["code"], "frame_number_not_owned")
        raised = self.call(dict(TICKET, frame_number=stranger, idempotency_key="k3"),
                           context(identity_strength=ASSERTED))
        ticket = self.registry.tickets.tickets[raised["data"]["ticket_id"]]
        self.assertEqual((ticket["frame_number"], ticket["bike_model"], ticket["identity"]),
                         (None, None, "unverified"))

    def test_with_no_strength_given_the_bike_is_still_checked_and_the_ticket_is_unverified(self):
        # A direct caller with no conversation facts: as before, except that
        # nothing says the phone was proved, so no name and no cover.
        ticket = self.registry.tickets.tickets[self.call(dict(TICKET), context())["data"]["ticket_id"]]
        self.assertEqual((ticket["frame_number"], ticket["identity"], ticket["customer_name"]),
                         (FRAME, "unverified", None))

    def test_the_name_comes_only_from_an_oms_record(self):
        app_bike = {"customer_name": "speedy_rider", "frame_number": FRAME, "bike_ref": FRAME,
                    "frame_on_record": True, "product_name": "EMX Plus", "warranty_on_record": False, "in_app": True}
        registry = build_registry(today=TODAY,
                                  warranty_source=lambda phone: [dict(app_bike)] if phone == FAKE else None)
        envelope = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        self.assertIsNone(registry.tickets.tickets[envelope["data"]["ticket_id"]]["customer_name"])

    def test_the_mock_too_keeps_one_ticket_per_key_and_run(self):
        shared = MockTicketSystem()
        first = build_registry(today=TODAY, ticket_system=shared, warranty_source=fake_bikes)
        second = build_registry(today=TODAY, ticket_system=shared, warranty_source=fake_bikes)
        a = first.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        b = second.call(CREATE_SUPPORT_TICKET, dict(TICKET), verified())
        self.assertEqual(a["data"]["ticket_id"], b["data"]["ticket_id"])
        self.assertEqual(len(shared.tickets), 1)


class ModelFacingTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY, verification=VerificationStore(), warranty_source=fake_bikes)

    def test_none_of_the_facts_is_in_any_ticket_tools_schema(self):
        for name in (CREATE_SUPPORT_TICKET, RAISE_INTAKE_TICKET, SUBMIT_WARRANTY_PROOF):
            with self.subTest(name):
                properties = self.registry.specs[name].schema()["input_schema"]["properties"]
                self.assertEqual(set(properties) & FACTS, set())

    def test_an_argument_a_tool_does_not_take_is_refused_and_records_nothing(self):
        calls = ((CREATE_SUPPORT_TICKET, dict(TICKET, priority="high"), verified()),
                 (RAISE_INTAKE_TICKET, dict(INTAKE, priority="high"), context(typed_number="9999999999")),
                 (SUBMIT_WARRANTY_PROOF, dict(PROOF, priority="high"), verified()))
        for name, arguments, ctx in calls:
            with self.subTest(name):
                self.assertEqual(self.registry.call(name, arguments, ctx)["error"]["code"], "tool_exception")
        self.assertEqual(self.registry.tickets.tickets, {})


class IntakeOnDeskTests(unittest.TestCase):
    def setUp(self):
        self.router, self.store = desk()
        self.registry = build_registry(today=TODAY, ticket_system=self.router, verification=VerificationStore())

    def test_an_intake_ticket_calls_back_the_number_typed_in_the_chat(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE),
                                      context(phone=None, typed_number="9999999999", channel="website_chat",
                                              identity_strength=ANONYMOUS))
        record = self.store.get(envelope["data"]["ticket_id"])
        self.assertEqual((record["kind"], record["identity"], record["phone"]), ("intake", "unverified", FAKE))
        self.assertEqual((record["channel"], record["started_at"]), ("website_chat", STARTED))
        self.assertEqual(record["source_key"], "c1:%s:raise_intake_ticket:i1" % STARTED)
        self.assertIn("Radhika", str(record["claims"]))
        self.assertIn("r@example.com", str(record["claims"]))

    def test_a_verified_phone_wins_over_a_typed_one(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE), verified(typed_number="9999999998"))
        record = self.store.get(envelope["data"]["ticket_id"])
        self.assertEqual((record["identity"], record["phone"]), ("verified", FAKE))

    def test_a_typed_number_that_is_not_an_indian_mobile_is_no_number(self):
        # _TEN_DIGIT_MOBILE: ten ASCII digits, the first 6 to 9, matched whole.
        for typed in ("1234567890", "99999", "+919999999999"):
            with self.subTest(typed):
                envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE, idempotency_key=typed),
                                              context(phone=None, typed_number=typed))
                self.assertEqual(envelope["error"]["code"], "contact_number_required")


class WarrantyProofTests(unittest.TestCase):
    def test_a_proof_is_recorded_as_claims_and_its_url_never_kept(self):
        router, store = desk()
        registry = build_registry(today=TODAY, ticket_system=router)
        data = registry.call(SUBMIT_WARRANTY_PROOF, dict(PROOF), verified(coverage="no_warranty_record"))["data"]
        self.assertEqual(data["ticket_id"], data["reference"])
        record = store.get(data["ticket_id"])
        self.assertEqual((record["kind"], record["identity"], record["coverage"]),
                         ("warranty_proof", "verified", "no_warranty_record"))
        self.assertEqual(record["source_key"], "c1:%s:submit_warranty_proof:p1" % STARTED)
        self.assertIn("2025-03-14", str(record["claims"]))
        self.assertIn("dealer", str(record["claims"]))
        self.assertNotIn("example.test", str(record))

    def test_the_mock_never_keeps_the_url_either(self):
        registry = build_registry(today=TODAY)
        data = registry.call(SUBMIT_WARRANTY_PROOF, dict(PROOF), verified())["data"]
        ticket = registry.tickets.tickets[data["ticket_id"]]
        self.assertNotIn("proof_url", ticket)
        self.assertNotIn("example.test", str(ticket))
        self.assertEqual((ticket["kind"], ticket["purchase_channel"], ticket["verified"]),
                         ("warranty_proof", "dealer", False))


class ThroughTheRuntimeTests(unittest.TestCase):
    def test_a_warranty_proof_ticket_gets_the_transcript(self):
        registry = build_registry(today=TODAY, warranty_source=lambda phone: None)
        runtime, _ = runtime_with(registry, [
            call_tool(SUBMIT_WARRANTY_PROOF, dict(PROOF), "t1"),
            say("Thanks. A colleague will check your invoice and come back to you."),
        ])
        reply = runtime.handle(whatsapp("I bought it from a dealer in March and never registered it"))
        self.assertTrue(reply.ticket_id)
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"]), ("warranty_proof", "verified"))
        self.assertIn("Customer: I bought it from a dealer in March and never registered it", ticket["transcript"])
        self.assertNotIn("proof_url", ticket)

    def test_a_whatsapp_safety_ticket_is_verified_and_carries_the_bike(self):
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(whatsapp("my battery is swollen"))
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"], ticket["frame_number"], ticket["customer_name"]),
                         ("safety", "verified", FRAME, "Ananya Rao"))
        self.assertEqual(llm.requests, [])

    def test_a_caller_ids_safety_ticket_carries_no_bike_and_is_unverified(self):
        # Before: the caller ID's registered bike went on the ticket, though
        # anyone can send that caller ID.
        registry = build_registry(today=TODAY, warranty_source=fake_bikes)
        runtime, llm = runtime_with(registry)
        reply = runtime.handle(VoiceAdapter(runtime.resolver).to_message(
            {"caller_id": FAKE, "transcript": "my battery is swollen", "call_id": "CALL-1", "confidence": 0.9}))
        ticket = registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["kind"], ticket["identity"]), ("safety", "unverified"))
        self.assertEqual((ticket["frame_number"], ticket["bike_model"], ticket["customer_name"]), (None, None, None))
        self.assertEqual(llm.requests, [])


class IdentityStrengthTests(unittest.TestCase):
    def runtime(self, phone_resolver=None):
        runtime, _ = runtime_with(build_registry(today=TODAY), phone_resolver=phone_resolver)
        return runtime

    @staticmethod
    def resolved(identity):
        return ResolvedIdentity(persona="customer", method="unverified", identity=identity)

    def test_a_phone_proved_by_a_code_in_this_chat_is_verified(self):
        anonymous = self.resolved(Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertEqual(self.runtime(lambda cid: FAKE)._identity_strength("c1", anonymous), VERIFIED)

    def test_the_channels_phone_keeps_the_channels_strength(self):
        caller = self.resolved(Identity(strength=ASSERTED, phone=FAKE))
        self.assertEqual(self.runtime(lambda cid: FAKE)._identity_strength("c1", caller), ASSERTED)

    def test_nobody_proved_is_anonymous(self):
        anonymous = self.resolved(Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertEqual(self.runtime()._identity_strength("c1", anonymous), ANONYMOUS)
        self.assertEqual(self.runtime(lambda cid: None)._identity_strength("c1", anonymous), ANONYMOUS)


if __name__ == "__main__":
    unittest.main()
