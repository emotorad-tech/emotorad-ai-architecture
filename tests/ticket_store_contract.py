"""One set of behaviours every ticket store must have (plan Task 2).

Mixed into a TestCase per implementation (tests/test_ticket_store.py), so the
in-memory store and MongoDB are held to the same contract. MongoDB runs on
mongomock one call after another: its atomic updates are exercised in
sequence, never by threads.
"""

from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.tickets.clock import plus
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.record import new_record

T0 = "2026-10-05T10:00:00.000000+00:00"
RUN = "2026-10-05T09:58:00.000000+00:00"
LATER_RUN = "2026-10-05T11:00:00.000000+00:00"
PHONE = "+919999999999"
OTHER_PHONE = "+919999999998"
FRAME = "EMXP2026001234"  # from the fixtures


def at(seconds):
    return plus(T0, seconds)


def zoho(**fields):
    base = {"contact_id": None, "ticket_id": None, "ticket_number": None, "web_url": None,
            "comment_ids": [], "attachment_ids": []}
    base.update(fields)
    return base


def record_for(store, kind="support", mode="test", identity="verified", phone=PHONE, category="battery_charging",
               conversation_id="c1", started_at=RUN, created_at=T0, source_key=None, **stored):
    """A new record, inserted. `stored` overrides fields as written, for the
    states only the worker reaches (sent, stuck, gone)."""
    reference = store.next_reference()
    record = new_record(
        reference=reference, chat_reference="stage:" + reference,
        source_key=source_key or "%s:%s:create_support_ticket:%s" % (conversation_id, started_at, reference),
        mode=mode, kind=kind, conversation_id=conversation_id, started_at=started_at, cluster_id="cluster-1",
        channel="website_chat", phone=phone, identity=identity, category=category, ai_severity="normal",
        summary="LED stays off.", claims={},
        bike={"model": "EMX Plus", "frame_number": FRAME, "frame_number_source": "record"},
        coverage="computed", customer_name=None, created_at=created_at,
    )
    record.update(stored)
    return store.insert(record)


class TicketStoreContract:
    def make_store(self):
        raise NotImplementedError

    # -- references and inserts -------------------------------------------------

    def test_references_count_up_from_the_first_desk_number(self):
        store = self.make_store()
        first, second = store.next_reference(), store.next_reference()
        self.assertEqual((first, second), ("EM-1000001", "EM-1000002"))
        self.assertTrue(is_desk_reference(first))

    def test_an_inserted_record_reads_back_by_reference_and_source_key(self):
        store = self.make_store()
        record = record_for(store)
        self.assertEqual(record["_id"], "EM-1000001")
        self.assertEqual(store.get("EM-1000001"), record)
        self.assertEqual(store.by_source_key(record["source_key"]), record)

    def test_a_second_insert_with_the_same_source_key_returns_the_first(self):
        store = self.make_store()
        key = "c1:%s:create_support_ticket:k1" % RUN
        first = record_for(store, source_key=key)
        again = record_for(store, source_key=key, kind="safety")
        self.assertEqual(again, first)
        self.assertIsNone(store.get("EM-1000002"))
        self.assertEqual([row["reference"] for row in store.listing("test")], ["EM-1000001"])

    def test_a_reference_used_twice_is_refused(self):
        store = self.make_store()
        first = record_for(store)
        with self.assertRaises(StoreUnavailable):
            store.insert(dict(first, source_key="c1:%s:create_support_ticket:other" % RUN))
        self.assertEqual(store.get(first["_id"]), first)

    def test_an_unknown_reference_or_key_is_none(self):
        store = self.make_store()
        self.assertIsNone(store.get("EM-1000001"))
        self.assertIsNone(store.by_source_key("c1:%s:create_support_ticket:k1" % RUN))

    # -- taking a record ------------------------------------------------------------

    def test_a_new_record_waits_two_minutes_for_its_first_attempt(self):
        store = self.make_store()
        record = record_for(store)
        self.assertEqual((record["state"], record["due_since"], record["next_attempt_at"]), ("waiting", T0, at(120)))
        self.assertIsNone(store.take_due(at(119), "test", 300, "t1"))
        self.assertEqual(store.take_due(at(120), "test", 300, "t1")["_id"], record["_id"])

    def test_taking_a_record_leases_it_and_reads_its_wake_count(self):
        store = self.make_store()
        record_for(store)
        taken = store.take_due(at(120), "test", 300, "t1")
        self.assertEqual((taken["lease_token"], taken["lease_until"], taken["wake"]), ("t1", at(420), 0))
        self.assertEqual(store.get(taken["_id"])["lease_token"], "t1")

    def test_a_leased_record_is_taken_again_only_once_its_lease_has_passed(self):
        store = self.make_store()
        record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertIsNone(store.take_due(at(121), "test", 300, "t2"))
        self.assertIsNone(store.take_due(at(420), "test", 300, "t2"))  # the lease ends at 420, exclusive
        self.assertEqual(store.take_due(at(421), "test", 300, "t2")["lease_token"], "t2")

    def test_urgent_records_come_first_then_the_oldest_due(self):
        store = self.make_store()
        late = record_for(store, created_at=at(0))  # due at 120
        early = record_for(store, created_at=at(-60))  # due at 60
        urgent = record_for(store, kind="safety", category="battery_safety", created_at=at(30))  # due at 150
        order = [store.take_due(at(200), "test", 300, "t%d" % i)["_id"] for i in range(3)]
        self.assertEqual(order, [urgent["_id"], early["_id"], late["_id"]])
        self.assertIsNone(store.take_due(at(200), "test", 300, "t4"))

    def test_only_records_of_the_current_mode_are_taken(self):
        store = self.make_store()
        live = record_for(store, mode="live")
        self.assertIsNone(store.take_due(at(200), "test", 300, "t1"))
        self.assertEqual(store.take_due(at(200), "live", 300, "t1")["_id"], live["_id"])

    def test_sent_and_gone_records_are_never_taken_and_stuck_ones_are(self):
        store = self.make_store()
        record_for(store, state="sent")
        record_for(store, state="gone")
        stuck = record_for(store, state="stuck")
        self.assertEqual(store.take_due(at(200), "test", 300, "t1")["_id"], stuck["_id"])
        self.assertIsNone(store.take_due(at(200), "test", 300, "t2"))

    # -- saving under the lease -------------------------------------------------------

    def test_a_save_under_the_lease_sets_adds_and_pushes(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertTrue(store.save(record["_id"], "t1", {"zoho.ticket_id": "z-1", "intent": None},
                                   add_to_set={"posted_turns": [1, 2], "zoho.comment_ids": ["c-1"]}))
        self.assertTrue(store.save(record["_id"], "t1", {}, add_to_set={"posted_turns": [2, 3]},
                                   push={"zoho.comment_ids": ["c-2"]}))
        saved = store.get(record["_id"])
        self.assertEqual(saved["zoho"]["ticket_id"], "z-1")
        self.assertIsNone(saved["zoho"]["contact_id"])  # a dotted set leaves its neighbours alone
        self.assertEqual(saved["posted_turns"], [1, 2, 3])
        self.assertEqual(saved["zoho"]["comment_ids"], ["c-1", "c-2"])

    def test_a_save_without_the_lease_changes_nothing(self):
        store = self.make_store()
        record = record_for(store)
        self.assertFalse(store.save(record["_id"], None, {"state": "sent"}))  # never leased
        store.take_due(at(120), "test", 300, "t1")
        self.assertFalse(store.save(record["_id"], "t2", {"state": "sent"}))
        self.assertFalse(store.save(record["_id"], None, {"state": "sent"}))
        self.assertEqual(store.get(record["_id"])["state"], "waiting")

    def test_a_save_never_creates_a_record(self):
        store = self.make_store()
        self.assertFalse(store.save("EM-1000009", "t1", {"state": "sent"}, add_to_set={"posted_turns": [1]}))
        self.assertIsNone(store.get("EM-1000009"))

    def test_a_wake_during_the_lease_is_not_lost(self):
        store = self.make_store()
        record = record_for(store)
        taken = store.take_due(at(120), "test", 300, "t1")
        self.assertTrue(store.wake(record["_id"], at(130)))
        self.assertFalse(store.save(record["_id"], "t1", {"state": "sent"}, expect_wake=taken["wake"]))
        self.assertEqual(store.get(record["_id"])["state"], "waiting")
        self.assertTrue(store.save(record["_id"], "t1", {"state": "sent"}, expect_wake=taken["wake"] + 1))

    def test_renewing_a_lease_needs_its_token(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertFalse(store.renew_lease(record["_id"], "t2", at(900)))
        self.assertTrue(store.renew_lease(record["_id"], "t1", at(900)))
        self.assertEqual(store.get(record["_id"])["lease_until"], at(900))
        self.assertIsNone(store.take_due(at(600), "test", 300, "t2"))

    def test_renewing_without_a_token_or_for_an_unknown_record_is_refused(self):
        store = self.make_store()
        record = record_for(store)
        self.assertFalse(store.renew_lease(record["_id"], None, at(900)))  # never leased
        self.assertFalse(store.renew_lease("EM-1000009", "t1", at(900)))

    # -- marking stuck -------------------------------------------------------------------

    def test_a_waiting_record_can_be_marked_stuck(self):
        store = self.make_store()
        record = record_for(store)
        self.assertTrue(store.mark_stuck(record["_id"]))
        self.assertEqual(store.get(record["_id"])["state"], "stuck")
        self.assertEqual(store.take_due(at(200), "test", 300, "t1")["_id"], record["_id"])  # still worked on

    def test_a_leased_record_can_be_marked_stuck_without_the_lease(self):
        store = self.make_store()
        record = record_for(store)
        store.take_due(at(120), "test", 300, "t1")
        self.assertTrue(store.mark_stuck(record["_id"]))
        saved = store.get(record["_id"])
        self.assertEqual((saved["state"], saved["lease_token"]), ("stuck", "t1"))  # the lease is left alone

    def test_only_a_waiting_record_is_marked_stuck(self):
        store = self.make_store()
        for state in ("sent", "gone", "stuck"):
            record = record_for(store, state=state)
            self.assertFalse(store.mark_stuck(record["_id"]), state)
            self.assertEqual(store.get(record["_id"])["state"], state)
        self.assertFalse(store.mark_stuck("EM-1000009"))
        self.assertIsNone(store.get("EM-1000009"))

    # -- new content ---------------------------------------------------------------------

    def test_a_wake_makes_a_waiting_record_due_now(self):
        store = self.make_store()
        record = record_for(store)
        self.assertTrue(store.wake(record["_id"], at(5)))
        woken = store.get(record["_id"])
        self.assertEqual((woken["wake"], woken["next_attempt_at"], woken["due_since"]), (1, at(5), T0))
        store.wake(record["_id"], at(9))  # a later wake never makes it due later
        self.assertEqual(store.get(record["_id"])["next_attempt_at"], at(5))

    def test_a_wake_reopens_a_sent_record_from_now(self):
        store = self.make_store()
        record = record_for(store, state="sent")
        self.assertTrue(store.wake(record["_id"], at(900)))
        woken = store.get(record["_id"])
        self.assertEqual((woken["state"], woken["due_since"], woken["next_attempt_at"], woken["wake"]),
                         ("waiting", at(900), at(900), 1))

    def test_a_gone_or_unknown_record_takes_no_wake(self):
        store = self.make_store()
        record = record_for(store, state="gone")
        self.assertFalse(store.wake(record["_id"], at(900)))
        self.assertEqual(store.get(record["_id"]), record)
        self.assertFalse(store.wake("EM-1000009", at(900)))

    def test_a_note_is_kept_and_wakes_the_record(self):
        store = self.make_store()
        record = record_for(store, state="sent")
        self.assertTrue(store.add_note(record["_id"], "Customer asked for a person at 14:02", at(900)))
        noted = store.get(record["_id"])
        self.assertEqual(noted["notes"], [{"text": "Customer asked for a person at 14:02", "at": at(900)}])
        self.assertEqual((noted["state"], noted["wake"], noted["due_since"]), ("waiting", 1, at(900)))

    def test_a_gone_or_unknown_record_takes_no_note(self):
        store = self.make_store()
        record = record_for(store, state="gone")
        self.assertFalse(store.add_note(record["_id"], "Customer asked for a person", at(900)))
        self.assertEqual(store.get(record["_id"])["notes"], [])
        self.assertFalse(store.add_note("EM-1000009", "Customer asked for a person", at(900)))

    def test_close_runs_ends_only_the_earlier_open_runs_of_that_conversation(self):
        store = self.make_store()
        earlier = record_for(store, started_at=RUN)
        current = record_for(store, started_at=LATER_RUN)
        other = record_for(store, conversation_id="c2", started_at=RUN)
        self.assertEqual(store.close_runs("c1", LATER_RUN), 1)
        self.assertEqual(store.get(earlier["_id"])["ended_at"], LATER_RUN)
        self.assertIsNone(store.get(current["_id"])["ended_at"])
        self.assertIsNone(store.get(other["_id"])["ended_at"])
        self.assertEqual(store.close_runs("c1", "2026-10-05T12:00:00.000000+00:00"), 1)  # only the open one
        self.assertEqual(store.get(earlier["_id"])["ended_at"], LATER_RUN)  # an end is never moved

    # -- reporting --------------------------------------------------------------------------

    def test_an_urgent_record_is_overdue_ten_minutes_after_it_fell_due(self):
        store = self.make_store()
        urgent = record_for(store, kind="safety", category="battery_safety")
        self.assertEqual(store.overdue(at(599), "test"), [])
        self.assertEqual([r["_id"] for r in store.overdue(at(600), "test")], [urgent["_id"]])

    def test_any_other_record_is_overdue_after_a_day(self):
        store = self.make_store()
        normal = record_for(store)
        self.assertEqual(store.overdue(at(86399), "test"), [])
        self.assertEqual([r["_id"] for r in store.overdue(at(86400), "test")], [normal["_id"]])

    def test_overdue_counts_from_due_since_however_many_turns_follow(self):
        store = self.make_store()
        urgent = record_for(store, kind="safety", category="battery_safety")
        for minute in range(1, 11):
            store.wake(urgent["_id"], at(60 * minute))  # a new turn every minute
        self.assertEqual([r["_id"] for r in store.overdue(at(600), "test")], [urgent["_id"]])

    def test_overdue_ignores_sent_gone_and_other_mode_records(self):
        store = self.make_store()
        for fields in ({"state": "sent"}, {"state": "gone"}, {"mode": "live"}):
            record_for(store, kind="safety", category="battery_safety", **fields)
        self.assertEqual(store.overdue(at(86400), "test"), [])

    def test_counts_waiting_stuck_held_and_the_age_of_the_oldest(self):
        store = self.make_store()
        record_for(store)  # waiting since T0
        record_for(store, state="stuck", due_since=at(-50))
        record_for(store, mode="live")  # the other mode: held
        record_for(store, state="sent")
        record_for(store, state="gone")
        self.assertEqual(store.counts("test", at(100)),
                         {"waiting": 1, "stuck": 1, "held": 1, "oldest_due_seconds": 150})
        self.assertEqual(store.counts("live", at(100)),
                         {"waiting": 1, "stuck": 0, "held": 2, "oldest_due_seconds": 100})

    def test_counts_on_an_empty_store(self):
        self.assertEqual(self.make_store().counts("test", T0),
                         {"waiting": 0, "stuck": 0, "held": 0, "oldest_due_seconds": None})

    def test_unverified_since_counts_only_non_urgent_unverified_records(self):
        store = self.make_store()
        record_for(store, kind="intake", category="intake_unverified", identity="unverified")
        record_for(store, kind="handover", category=None, identity="unverified", phone=OTHER_PHONE)
        record_for(store, kind="safety", category="battery_safety", identity="unverified")  # urgent: never capped
        record_for(store, identity="verified")
        record_for(store, kind="intake", category="intake_unverified", identity="unverified", created_at=at(-1))
        self.assertEqual(store.unverified_since(T0), 2)
        self.assertEqual(store.unverified_since(T0, phone=PHONE), 1)
        self.assertEqual(store.unverified_since(at(-1)), 3)

    def test_contact_for_uses_only_live_verified_records(self):
        store = self.make_store()
        record_for(store, mode="test", zoho=zoho(contact_id="test-contact"))
        record_for(store, mode="live", identity="unverified", zoho=zoho(contact_id="unverified-contact"))
        record_for(store, mode="live", zoho=zoho())  # not sent yet: no contact
        self.assertIsNone(store.contact_for(PHONE))
        record_for(store, mode="live", zoho=zoho(contact_id="z-old"), created_at=at(-100))
        record_for(store, mode="live", zoho=zoho(contact_id="z-new"), created_at=at(-10))
        self.assertEqual(store.contact_for(PHONE), "z-new")
        self.assertIsNone(store.contact_for(OTHER_PHONE))

    def test_the_listing_shows_outstanding_records_with_the_last_four_digits_only(self):
        store = self.make_store()
        waiting = record_for(store, zoho=zoho(ticket_number="1042"))
        stuck = record_for(store, state="stuck", created_at=at(10))
        held = record_for(store, mode="live", created_at=at(20))
        record_for(store, state="sent")
        record_for(store, state="gone")
        rows = store.listing("test")
        self.assertEqual(rows, [
            {"reference": waiting["_id"], "zoho_number": "1042", "state": "waiting", "mode": "test",
             "last_four": "9999"},
            {"reference": stuck["_id"], "zoho_number": None, "state": "stuck", "mode": "test", "last_four": "9999"},
            {"reference": held["_id"], "zoho_number": None, "state": "held", "mode": "live", "last_four": "9999"},
        ])
        self.assertNotIn(PHONE, repr(rows))

    def test_the_store_keeps_source_keys_unique(self):
        self.assertTrue(self.make_store().has_unique_source_key())
