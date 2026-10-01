"""One set of behaviours every conversation store must have.

Mixed into a TestCase per implementation, so the in-memory store and any
durable store (MongoDB next) are held to the same contract, not two similar ones.
"""

from emotorad_ai.contract import Attachment, Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationSummaryItem, summary_key


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
        current = summary_key("d", "2026-09-25T10:00:00+00:00")
        self.assertEqual([s.conversation_id for s in store.recent_summaries("PHONE#+919876543210", limit=2, exclude=current)], ["c", "b"])
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

    def test_delete_person_removes_everything_for_one_person_only(self):
        store = self.make_store()
        for cid, user in (("mine", "PHONE#+919876543210"), ("theirs", "PHONE#+919812345678")):
            state = store.get(cid)
            state.user_key, state.turns = user, 1
            store.save(state)
            store.record_turn(state, inbound("x", cid), reply("y", cid), summary(cid, user_key=user))
        counts = store.delete_person("PHONE#+919876543210")
        self.assertEqual({k: counts[k] for k in ("conversations", "transcript_turns", "conversation_summaries")},
                         {"conversations": 1, "transcript_turns": 2, "conversation_summaries": 1})
        self.assertEqual(store.transcript("mine"), [])
        self.assertEqual(store.recent_summaries("PHONE#+919876543210"), [])
        self.assertEqual(store.get("mine").turns, 0)  # a fresh state: the old one is gone
        self.assertEqual(len(store.transcript("theirs")), 2)
        self.assertEqual(len(store.recent_summaries("PHONE#+919812345678")), 1)

    def test_no_summary_is_written_without_a_user_key(self):
        store = self.make_store()
        state = store.get("a")
        state.turns = 1
        store.record_turn(state, inbound("x", "a"), reply("y", "a"), summary("a", user_key="PHONE#+919876543210"))
        self.assertEqual(store.recent_summaries("PHONE#+919876543210"), [])

    def origin(self, cid, started_at="2026-10-01T09:00:00+00:00", user_key=None, **place):
        record = {"_id": "%s#%s" % (cid, started_at), "conversation_id": cid, "started_at": started_at,
                  "channel": "amiigo_app", "country": "IN", "region": "Maharashtra", "city": "Pune",
                  "source": "ip", "db": "dbip-city-lite-2026-10", "user_key": user_key}
        record.update(place)
        return record

    def test_an_origin_is_upserted_per_run(self):
        store = self.make_store()
        store.record_origin(self.origin("c1"))
        store.record_origin(self.origin("c1", user_key="PHONE#+919700000031"))
        store.record_origin(self.origin("c1", started_at="2026-10-03T09:00:00+00:00", city="Mumbai"))
        records = store.origins_of("c1")
        self.assertEqual([r["city"] for r in records], ["Pune", "Mumbai"])
        self.assertEqual(records[0]["user_key"], "PHONE#+919700000031")

    def test_an_erasure_by_person_takes_their_origins(self):
        store = self.make_store()
        state = store.get("mine")
        state.user_key, state.turns = "PHONE#+919700000031", 1
        store.save(state)
        store.record_origin(self.origin("mine", user_key="PHONE#+919700000031"))
        store.record_origin(self.origin("theirs", user_key="PHONE#+919812345678"))
        counts = store.delete_person("PHONE#+919700000031")
        self.assertEqual(counts["conversation_origins"], 1)
        self.assertEqual(store.origins_of("mine"), [])
        self.assertEqual(len(store.origins_of("theirs")), 1)

    def test_an_origin_alone_ties_a_conversation_to_its_person(self):
        # A run that began anonymous and verified: only its origin may carry the key.
        store = self.make_store()
        store.record_origin(self.origin("web-1", user_key="PHONE#+919700000031"))
        self.assertEqual(store.delete_person("PHONE#+919700000031")["conversation_origins"], 1)

    def test_an_erasure_by_conversation_takes_an_anonymous_origin(self):
        store = self.make_store()
        store.record_origin(self.origin("anon"))
        self.assertEqual(store.delete_conversation("anon")["conversation_origins"], 1)
        self.assertEqual(store.origins_of("anon"), [])
