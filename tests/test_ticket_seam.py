"""The ticket seam (plan Task 3): the mock's additions, DeskTicketSystem and
TicketRouter. Desk only records: nothing here calls Zoho or the network."""

import copy
import unittest

import mongomock

from emotorad_ai.stores.mongo import MongoTicketStore, ensure_indexes
from emotorad_ai.tickets.clock import plus
from emotorad_ai.tickets.seam import DeskTicketSystem, TicketRouter
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools.mocks import MockTicketSystem, build_registry

T0 = "2026-10-05T10:00:00.000000+00:00"
RUN = "2026-10-05T09:58:00.000000+00:00"
LATER_RUN = "2026-10-05T11:00:00.000000+00:00"
PHONE = "+919999999999"
FRAME = "EMXP2026001234"  # from the fixtures
KEY = "c1:%s:create_support_ticket:k1" % RUN


def support_fields(**overrides):
    """What create_support_ticket passes for a verified customer (Task 4)."""
    fields = dict(kind="support", conversation_id="c1", started_at=RUN, cluster_id="cluster-1", channel="whatsapp",
                  phone=PHONE, identity="verified", category="battery_charging", severity="high",
                  description="LED stays off. Call me on 9876543210.", frame_number=FRAME,
                  frame_number_source="record", bike_model="EMX Plus", coverage="computed",
                  customer_name="Test Rider")
    fields.update(overrides)
    return fields


class MockTicketSystemTests(unittest.TestCase):
    def test_the_same_source_key_returns_the_same_ticket(self):
        mock = MockTicketSystem()
        first = mock.create(source_key=KEY, persona="customer", category="battery_charging")
        again = mock.create(source_key=KEY, persona="customer", category="battery_charging")
        self.assertEqual(again["ticket_id"], first["ticket_id"])
        self.assertEqual(list(mock.tickets), ["EM-00001"])

    def test_without_a_source_key_every_create_is_a_new_ticket(self):
        mock = MockTicketSystem()
        self.assertEqual([mock.create(category="other")["ticket_id"] for _ in range(2)], ["EM-00001", "EM-00002"])

    def test_it_keeps_the_persona_the_key_and_any_field_it_is_given(self):
        ticket = MockTicketSystem().create(source_key=KEY, persona="dealer", category="other", proof_url="x")
        self.assertEqual((ticket["persona"], ticket["source_key"], ticket["proof_url"], ticket["status"]),
                         ("dealer", KEY, "x", "open"))

    def test_a_note_is_kept_on_the_ticket(self):
        mock = MockTicketSystem()
        ticket_id = mock.create(category="other")["ticket_id"]
        mock.add_note(ticket_id, "Customer asked for a person")
        self.assertEqual(mock.tickets[ticket_id]["notes"], ["Customer asked for a person"])
        with self.assertRaises(KeyError):
            mock.add_note("EM-00009", "x")

    def test_close_runs_does_nothing(self):
        mock = MockTicketSystem()
        mock.create(category="other", conversation_id="c1", started_at=RUN)
        before = copy.deepcopy(mock.tickets)
        self.assertIsNone(mock.close_runs("c1", LATER_RUN))
        self.assertEqual(mock.tickets, before)

    def test_it_records_no_real_tickets(self):
        self.assertFalse(MockTicketSystem.records_real_tickets)


class SeamContract:
    """DeskTicketSystem and TicketRouter, on whichever store make_store gives."""

    def make_store(self):
        raise NotImplementedError

    def same_store_again(self, store):
        """The store as a second server would open it."""
        return store

    def desk(self, store=None, mode="test"):
        if not hasattr(self, "now"):
            self.now, self.wakes = [T0], []
        return DeskTicketSystem(store if store is not None else self.make_store(), mode, "stage",
                                clock=lambda: self.now[0], wake=lambda: self.wakes.append(self.now[0]))

    def router(self):
        return TicketRouter(self.desk(), MockTicketSystem())

    # -- DeskTicketSystem -------------------------------------------------------------

    def test_a_customer_ticket_becomes_one_record_with_the_fields_mapped(self):
        desk = self.desk()
        self.assertEqual(desk.create(source_key=KEY, persona="customer", **support_fields()),
                         {"ticket_id": "EM-1000001", "status": "open"})
        record = desk.store.get("EM-1000001")
        self.assertEqual(record["chat_reference"], "stage:EM-1000001")
        self.assertEqual((record["source_key"], record["mode"], record["kind"], record["urgent"]),
                         (KEY, "test", "support", False))
        self.assertEqual((record["conversation_id"], record["started_at"], record["cluster_id"], record["channel"]),
                         ("c1", RUN, "cluster-1", "whatsapp"))
        self.assertEqual((record["phone"], record["identity"], record["category"], record["ai_severity"]),
                         (PHONE, "verified", "battery_charging", "high"))
        self.assertEqual(record["summary"], "LED stays off. Call me on [phone].")
        self.assertEqual(record["bike"], {"model": "EMX Plus", "frame_number": FRAME, "frame_number_source": "record"})
        self.assertEqual((record["coverage"], record["customer_name"], record["claims"]),
                         ("computed", "Test Rider", {}))
        self.assertEqual((record["created_at"], record["next_attempt_at"], record["state"]),
                         (T0, plus(T0, 120), "waiting"))

    def test_the_same_source_key_is_one_record_even_from_a_second_server(self):
        first_desk = self.desk()
        first = first_desk.create(source_key=KEY, persona="customer", **support_fields())
        second_desk = self.desk(store=self.same_store_again(first_desk.store))
        second = second_desk.create(source_key=KEY, persona="customer", **support_fields(severity="low"))
        self.assertEqual(second, first)
        self.assertEqual(len(first_desk.store.listing("test")), 1)
        self.assertEqual(first_desk.store.get("EM-1000001")["ai_severity"], "high")

    def test_urgency_follows_the_kind_and_the_category(self):
        desk = self.desk()
        made = {
            "safety": desk.create(source_key=KEY + ":safety", persona="customer", **support_fields(kind="safety")),
            "model_safety": desk.create(source_key=KEY + ":model", persona="customer",
                                        **support_fields(category="battery_safety")),
            "charging": desk.create(source_key=KEY + ":charging", persona="customer", **support_fields()),
        }
        urgent = {name: desk.store.get(result["ticket_id"])["urgent"] for name, result in made.items()}
        self.assertEqual(urgent, {"safety": True, "model_safety": True, "charging": False})

    def test_an_unverified_ticket_carries_no_bike(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields(identity="unverified"))
        record = desk.store.get("EM-1000001")
        self.assertEqual((record["identity"], record["bike"]), ("unverified", None))

    def test_without_an_identity_a_ticket_is_unverified(self):
        fields = support_fields()
        del fields["identity"]
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **fields)
        self.assertEqual(desk.store.get("EM-1000001")["identity"], "unverified")

    def test_an_intakes_stated_details_are_kept_as_claims(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields(
            kind="intake", category="intake_unverified", identity="unverified", severity="normal",
            stated_name="Test Rider", stated_contact="not given", evidence="none offered",
            frame_number=None, frame_number_source=None, bike_model=None, coverage=None, customer_name=None))
        record = desk.store.get("EM-1000001")
        self.assertEqual(record["claims"],
                         {"stated_name": "Test Rider", "stated_contact": "not given", "evidence": "none offered"})
        self.assertEqual((record["kind"], record["identity"], record["bike"]), ("intake", "unverified", None))

    def test_a_warranty_proofs_date_and_channel_are_claims(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields(
            kind="warranty_proof", category="late_warranty_registration", severity="normal",
            description="Warranty proof submitted.", frame_number=FRAME,
            frame_number_source="given by the customer", bike_model=None,
            claimed_purchase_date="2026-01-15", purchase_channel="dealer", coverage=None, customer_name=None))
        record = desk.store.get("EM-1000001")
        self.assertEqual(record["claims"], {"claimed_purchase_date": "2026-01-15", "purchase_channel": "dealer"})
        self.assertEqual(record["bike"],
                         {"model": None, "frame_number": FRAME, "frame_number_source": "given by the customer"})

    def test_fields_desk_does_not_know_are_never_recorded(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer",
                    **support_fields(proof_url="https://x.test/invoice.jpg", verified=False))
        record = desk.store.get("EM-1000001")
        self.assertNotIn("proof_url", record)
        self.assertNotIn("verified", record)
        self.assertNotIn("invoice.jpg", repr(record))

    def test_only_customer_tickets_with_a_key_kind_conversation_and_run_are_recorded(self):
        desk = self.desk()
        refused = (
            ("dealer", KEY, support_fields()),
            (None, KEY, support_fields()),
            ("customer", None, support_fields()),
            ("customer", KEY, support_fields(kind="complaint")),
            ("customer", KEY, support_fields(conversation_id=None)),
            # Without the run's start the worker could not tell this person's turns from an earlier person's.
            ("customer", KEY, support_fields(started_at=None)),
        )
        for persona, key, fields in refused:
            with self.subTest(persona=persona, key=key, kind=fields["kind"], started_at=fields["started_at"]), \
                    self.assertRaises(ValueError):
                desk.create(source_key=key, persona=persona, **fields)
        self.assertEqual(desk.store.listing("test"), [])
        self.assertEqual(desk.store.next_reference(), "EM-1000001")  # no reference was spent on a refusal

    def test_the_mode_is_stamped_on_the_record(self):
        desk = self.desk(mode="live")
        desk.create(source_key=KEY, persona="customer", **support_fields())
        self.assertEqual(desk.store.get("EM-1000001")["mode"], "live")

    def test_an_unknown_mode_or_no_environment_is_refused(self):
        with self.assertRaises(ValueError):
            DeskTicketSystem(self.make_store(), "staging", "stage")
        with self.assertRaises(ValueError):
            DeskTicketSystem(self.make_store(), "test", "")

    def test_recording_does_not_wake_the_worker(self):
        desk = self.desk()
        desk.create(source_key=KEY, persona="customer", **support_fields())
        self.assertEqual(self.wakes, [])
        self.assertEqual(desk.store.get("EM-1000001")["wake"], 0)

    def test_the_turns_end_makes_the_record_due_now_and_wakes_the_worker(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        self.now[0] = plus(T0, 5)
        desk.attach_transcript(ticket_id, "[10:00] Customer: my battery won't charge")
        record = desk.store.get(ticket_id)
        self.assertEqual((record["wake"], record["next_attempt_at"]), (1, plus(T0, 5)))
        self.assertEqual(self.wakes, [plus(T0, 5)])
        self.assertNotIn("my battery", repr(record))  # the worker reads the conversation store itself

    def test_a_later_turn_reopens_a_sent_record(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        desk.store.take_due(plus(T0, 120), "test", 300, "t1")
        desk.store.save(ticket_id, "t1", {"state": "sent", "lease_until": None, "lease_token": None})
        self.now[0] = plus(T0, 600)
        desk.attach_transcript(ticket_id, "ignored")
        record = desk.store.get(ticket_id)
        self.assertEqual((record["state"], record["due_since"], record["next_attempt_at"]),
                         ("waiting", plus(T0, 600), plus(T0, 600)))

    def test_a_gone_ticket_takes_no_more_work_and_no_wake(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        desk.store.take_due(plus(T0, 120), "test", 300, "t1")
        desk.store.save(ticket_id, "t1", {"state": "gone", "lease_until": None, "lease_token": None})
        desk.attach_transcript(ticket_id, "ignored")
        desk.add_note(ticket_id, "Customer asked for a person")
        record = desk.store.get(ticket_id)
        self.assertEqual((record["state"], record["wake"], record["notes"]), ("gone", 0, []))
        self.assertEqual(self.wakes, [])

    def test_an_unknown_ticket_is_a_key_error(self):
        desk = self.desk()
        with self.assertRaises(KeyError):
            desk.attach_transcript("EM-1000009", "x")
        with self.assertRaises(KeyError):
            desk.add_note("EM-1000009", "x")

    def test_a_note_is_recorded_and_wakes_the_worker(self):
        desk = self.desk()
        ticket_id = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        self.now[0] = plus(T0, 30)
        desk.add_note(ticket_id, "Customer asked for a person at 10:00")
        record = desk.store.get(ticket_id)
        self.assertEqual(record["notes"], [{"text": "Customer asked for a person at 10:00", "at": plus(T0, 30)}])
        self.assertEqual((record["wake"], self.wakes), (1, [plus(T0, 30)]))

    def test_close_runs_marks_where_the_earlier_run_ended(self):
        desk = self.desk()
        earlier = desk.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        later = desk.create(source_key="c1:%s:create_support_ticket:k1" % LATER_RUN, persona="customer",
                            **support_fields(started_at=LATER_RUN))["ticket_id"]
        desk.close_runs("c1", LATER_RUN)
        self.assertEqual(desk.store.get(earlier)["ended_at"], LATER_RUN)
        self.assertIsNone(desk.store.get(later)["ended_at"])

    # -- TicketRouter -------------------------------------------------------------------

    def test_a_customer_ticket_goes_to_desk(self):
        router = self.router()
        self.assertEqual(router.create(source_key=KEY, persona="customer", **support_fields()),
                         {"ticket_id": "EM-1000001", "status": "open"})
        self.assertIsNotNone(router.store.get("EM-1000001"))
        self.assertEqual(router.tickets, {})

    def test_a_dealers_ticket_or_one_with_no_persona_goes_to_the_mock(self):
        router = self.router()
        dealer = router.create(source_key=KEY, persona="dealer", **support_fields())
        nobody = router.create(source_key=KEY + ":2", **support_fields())
        self.assertEqual((dealer["ticket_id"], nobody["ticket_id"]), ("EM-00001", "EM-00002"))
        self.assertEqual(router.store.listing("test"), [])
        self.assertIsNone(router.store.get("EM-1000001"))
        self.assertEqual(router.tickets["EM-00001"]["persona"], "dealer")

    def test_later_calls_go_to_the_system_that_issued_the_id(self):
        router = self.router()
        desk_id = router.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        mock_id = router.create(source_key=KEY + ":dealer", persona="dealer", **support_fields())["ticket_id"]
        router.attach_transcript(desk_id, "thread")
        router.attach_transcript(mock_id, "thread")
        router.add_note(desk_id, "Customer asked for a person")
        router.add_note(mock_id, "Dealer asked for a person")
        self.assertEqual(router.store.get(desk_id)["wake"], 2)
        self.assertEqual([n["text"] for n in router.store.get(desk_id)["notes"]], ["Customer asked for a person"])
        self.assertEqual(router.tickets[mock_id]["transcript"], "thread")
        self.assertEqual(router.tickets[mock_id]["notes"], ["Dealer asked for a person"])
        self.assertNotIn(desk_id, router.tickets)

    def test_close_runs_reaches_the_desk_records(self):
        router = self.router()
        desk_id = router.create(source_key=KEY, persona="customer", **support_fields())["ticket_id"]
        router.close_runs("c1", LATER_RUN)
        self.assertEqual(router.store.get(desk_id)["ended_at"], LATER_RUN)

    def test_it_records_real_tickets_and_exposes_the_mocks_dict_and_the_store(self):
        router = self.router()
        self.assertTrue(router.records_real_tickets)
        self.assertIs(router.tickets, router.mock.tickets)
        self.assertIs(router.store, router.desk.store)

    def test_the_registry_holds_the_router_and_still_reads_the_mocks_dict(self):
        router = self.router()
        registry = build_registry(ticket_system=router)
        self.assertIs(registry.tickets, router)
        self.assertIs(registry.tickets.tickets, router.mock.tickets)


class SeamOnMemoryTests(SeamContract, unittest.TestCase):
    def make_store(self):
        return InMemoryTicketStore()


class SeamOnMongoTests(SeamContract, unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)

    def make_store(self):
        return MongoTicketStore(self.db)

    def same_store_again(self, store):
        return MongoTicketStore(self.db)  # a second server on the same database


if __name__ == "__main__":
    unittest.main()
