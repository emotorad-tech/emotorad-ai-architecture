"""A ticket closed in Zoho Desk reaches the rider (docs/contracts/amiigo-support-chat.md:
`ticket_update`, "Tickets and Zoho Desk", "For the server team: the Zoho Desk
webhook"; the plan's Task 6 and Rulings 18 to 21).

The ticket record is closed once, a `system` notice is saved in the chat, and
`ticket_update` goes to every open socket of the rider whose app chat it is.
The stores run twice, in memory and on MongoDB (mongomock, its TTL clock
pinned: Ruling 3). The webhook runs through the real API with a fake secret,
and the payloads are built from Zoho's documented sample
(docs/api-shapes/zoho-webhook-ticket-update.json). No network, no Zoho.
"""

import copy
import hashlib
import json
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import mongomock
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from emotorad_ai.amiigo import auth as auth_module
from emotorad_ai.amiigo import history
from emotorad_ai.amiigo import tickets as closure
from emotorad_ai.amiigo import webhooks
from emotorad_ai.amiigo.auth import Rider, rider_hash_of
from emotorad_ai.amiigo.sockets import SocketRegistry
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage, Reply
from emotorad_ai.conversation import (
    ConversationSummaryItem,
    InMemoryConversationStore,
    StoreUnavailable,
    summary_key,
)
from emotorad_ai.stores.mongo import (
    CONVERSATION_NOTICES,
    INDEXES,
    TICKETS,
    MongoConversationStore,
    MongoTicketStore,
    ensure_indexes,
)
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.wiring import Stores
from tests.amiigo_tokens import RIDER_PHONE
from tests.clock import mongomock_clock_at
from tests.fake_zoho import shape
from tests.test_amiigo_socket import TIMEOUT, SocketCase, frame, message, quiet
from tests.ticket_store_contract import record_for, zoho

NOW = datetime(2026, 10, 7, 11, 2, 12, tzinfo=timezone.utc)
CLOSED_AT = datetime(2026, 10, 7, 11, 2, 10, tzinfo=timezone.utc)
RIDER_KEY = "PHONE#" + RIDER_PHONE
OTHER_PHONE = "+919700000011"
OTHER_KEY = "PHONE#" + OTHER_PHONE
CID = "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
CID_WEB = "4a3d0b2f-6c8e-4f9b-8d32-8e5f6a7b9c01"
CID_OTHER = "5b4e1c3a-7d9f-4a0c-9e43-9f6a7b8c0d12"
ZOHO_ID = "55430000000000101"
OTHER_ZOHO_ID = "55430000000000202"
SAMPLE_ZOHO_ID = "31138000011967402"  # the documented sample's ticket
SECRET = "fake-webhook-secret-for-tests-0123456789"
WEBHOOK = "/webhooks/zoho/tickets"


def notice_text(reference):
    return "Your support request %s was closed by our support team." % reference


def ticket_hash(zoho_ticket_id):
    return hashlib.sha256(zoho_ticket_id.encode("utf-8")).hexdigest()[:12]


def rider(user_key=RIDER_KEY):
    return Rider(phone=user_key.split("#", 1)[1], user_key=user_key, emuser_id=None,
                 expires_at=NOW + timedelta(hours=1), rider_hash=rider_hash_of(user_key))


def no_links(_url):
    return None, None


def quiet_tokens():
    """amiigo_tokens_not_configured is logged once a process: a reload here
    would log it again."""
    return mock.patch.object(auth_module, "_not_configured_logged", True)


# -- Zoho's documented sample, and changes made from it --------------------------


def documented_body():
    """The body Zoho documents for a Ticket_Update (an On Hold ticket reopened)."""
    return shape("zoho-webhook-ticket-update.json")["body"]


def closure_body(zoho_ticket_id=ZOHO_ID, closed_time="2026-10-07T11:02:10.000Z", status_type="Closed",
                 status="Closed", event_type="Ticket_Update"):
    """The documented sample as a closure of one of our tickets: the fields
    the sample carries, with a closed ticket's values."""
    body = copy.deepcopy(documented_body())
    event = body[0]
    event["eventType"] = event_type
    event["prevState"]["id"] = zoho_ticket_id
    event["payload"].update(id=zoho_ticket_id, status=status, statusType=status_type, closedTime=closed_time)
    return body


# -- the stores ------------------------------------------------------------------


class Clock:
    """The conversation store's clock for transcript times, moved by the test."""

    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now.isoformat()


class FakeSocket:
    """An open socket in the registry: what was pushed to it."""

    def __init__(self):
        self.frames = []
        self.closed = False

    def push(self, sent):
        self.frames.append(sent)
        return True


class StoreKinds:
    def make_stores(self):
        raise NotImplementedError

    def setUp(self):
        self.make_stores()
        self.stores = Stores(conversations=self.conversations, idempotency=None, tickets=self.tickets)
        self.registry = SocketRegistry()
        self.mine = [FakeSocket(), FakeSocket()]
        self.theirs = FakeSocket()
        for sock in self.mine:
            self.registry.add(RIDER_KEY, sock)
        self.registry.add(OTHER_KEY, self.theirs)

    def chat(self, cid=CID, user_key=RIDER_KEY, channel="amiigo_app", at=None, text="My battery is not charging"):
        """One turn of a chat, recorded as the runtime records one: where the
        run came from, the transcript pair and the run's summary."""
        self.clock.now = at or NOW - timedelta(minutes=5)
        state = self.conversations.get(cid)
        state.user_key, state.channel = user_key, channel
        state.turns += 1
        if state.turns == 1:
            self.conversations.record_origin({
                "_id": summary_key(cid, state.started_at), "conversation_id": cid, "started_at": state.started_at,
                "channel": channel, "user_key": user_key, "country": "IN", "region": None, "city": None,
                "source": "phone", "db": None})
        self.conversations.save(state)
        inbound = InboundMessage(conversation_id=cid, persona="customer", channel=channel, message_text=text,
                                 identity=Identity(strength=VERIFIED, phone=user_key.split("#", 1)[1]))
        reply = Reply(cid, "Is the battery switched on?", "narrow_support", ticket_id=None)
        summary = ConversationSummaryItem(conversation_id=cid, user_key=user_key, started_at=state.started_at,
                                          last_at=self.clock(), channel=channel, title="Battery issue",
                                          turns=state.turns, ticket_id=self.reference_of.get(cid))
        self.conversations.record_turn(state, inbound, reply, summary)

    def ticket(self, cid=CID, zoho_ticket_id=ZOHO_ID, **stored):
        """Our ticket record for the chat, sent to Zoho Desk as `zoho_ticket_id`."""
        record = record_for(self.tickets, conversation_id=cid, zoho=zoho(ticket_id=zoho_ticket_id), **stored)
        self.reference_of[cid] = record["_id"]
        return record["_id"]

    def close(self, zoho_ticket_id=ZOHO_ID, closed_at=CLOSED_AT, now=NOW):
        return closure.close_ticket(self.stores, self.registry, zoho_ticket_id=zoho_ticket_id, closed_at=closed_at,
                                    now=now)

    def pushed(self):
        return [sock.frames for sock in self.mine], self.theirs.frames


class MemoryStores(StoreKinds):
    def make_stores(self):
        self.clock = Clock()
        self.reference_of = {}
        self.conversations = InMemoryConversationStore(clock=self.clock)
        self.tickets = InMemoryTicketStore()


class MongoStores(StoreKinds):
    def make_stores(self):
        # Ruling 3: `conversations` has a TTL index; mongomock reads this clock.
        pin = mongomock_clock_at(NOW)
        pin.start()
        self.addCleanup(pin.stop)
        self.clock = Clock()
        self.reference_of = {}
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.conversations = MongoConversationStore(self.db, clock=self.clock, now=lambda: NOW)
        self.tickets = MongoTicketStore(self.db)


# -- the ticket record: closed once, atomically --------------------------------------


class TicketCloseContract(StoreKinds):
    def test_a_record_closes_once_and_keeps_its_first_closing_time(self):
        reference = self.ticket()
        record, closed_now = self.tickets.close_support(ZOHO_ID, CLOSED_AT.isoformat())
        self.assertTrue(closed_now)
        self.assertEqual((record["_id"], record["conversation_id"], record["support_status"], record["closed_at"]),
                         (reference, CID, "closed", CLOSED_AT.isoformat()))
        later = (CLOSED_AT + timedelta(hours=1)).isoformat()
        record, closed_now = self.tickets.close_support(ZOHO_ID, later)
        self.assertFalse(closed_now)
        self.assertEqual((record["support_status"], record["closed_at"]), ("closed", CLOSED_AT.isoformat()))
        self.assertEqual(self.tickets.ticket_status(reference),
                         {"reference": reference, "status": "closed", "closed_at": CLOSED_AT.isoformat()})

    def test_a_zoho_ticket_that_is_not_ours_is_none_and_changes_nothing(self):
        reference = self.ticket()
        self.assertIsNone(self.tickets.close_support(OTHER_ZOHO_ID, CLOSED_AT.isoformat()))
        self.assertIsNone(self.tickets.close_support("", CLOSED_AT.isoformat()))
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "open")

    def test_a_record_not_yet_sent_to_zoho_is_never_matched(self):
        unsent = record_for(self.tickets, conversation_id=CID)["_id"]  # zoho.ticket_id is None
        self.assertIsNone(self.tickets.close_support(None, CLOSED_AT.isoformat()))
        self.assertEqual(self.tickets.ticket_status(unsent)["status"], "open")

    def test_a_record_from_before_the_support_fields_closes(self):
        reference = self.ticket()
        if isinstance(self.tickets, MongoTicketStore):
            self.db[TICKETS].update_one({"_id": reference}, {"$unset": {"support_status": "", "closed_at": ""}})
        else:
            for name in ("support_status", "closed_at"):
                self.tickets._records[reference].pop(name)
        _, closed_now = self.tickets.close_support(ZOHO_ID, CLOSED_AT.isoformat())
        self.assertTrue(closed_now)
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "closed")

    def test_closing_changes_nothing_the_worker_owns(self):
        reference = self.ticket()
        before = self.tickets.get(reference)
        self.tickets.close_support(ZOHO_ID, CLOSED_AT.isoformat())
        after = self.tickets.get(reference)
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        self.assertEqual(changed, {"support_status", "closed_at"})


class MemoryTicketCloseTests(MemoryStores, TicketCloseContract, unittest.TestCase):
    pass


class MongoTicketCloseTests(MongoStores, TicketCloseContract, unittest.TestCase):
    def test_a_store_that_cannot_answer_is_store_unavailable(self):
        self.ticket()
        broken = MongoTicketStore(self.db)
        with mock.patch.object(broken._tickets, "find_one_and_update",
                               side_effect=ServerSelectionTimeoutError("no servers")):
            with self.assertRaises(StoreUnavailable):
                broken.close_support(ZOHO_ID, CLOSED_AT.isoformat())

    def test_the_zoho_ticket_id_is_indexed(self):
        info = self.db[TICKETS].index_information()
        self.assertEqual(info["zoho_ticket"]["key"], [("zoho.ticket_id", 1)])
        self.assertNotIn("unique", info["zoho_ticket"])
        self.assertNotIn("expireAfterSeconds", info["zoho_ticket"])


# -- notices: written once, read by history, erased with the person -----------------


class NoticeContract(StoreKinds):
    def add(self, cid=CID, text=None, at=NOW, user_key=RIDER_KEY, kind="ticket_closed"):
        return self.conversations.add_notice(cid, user_key, kind, text or notice_text("EM-1000001"), at.isoformat())

    def test_a_notice_has_exactly_the_fields_history_reads(self):
        notice, written = self.add()
        self.assertTrue(written)
        expected = {"_id": CID + "#N00001", "conversation_id": CID, "user_key": RIDER_KEY, "kind": "ticket_closed",
                    "text": notice_text("EM-1000001"), "at": NOW.isoformat()}
        self.assertEqual(dict(notice), expected)
        self.assertEqual([dict(n) for n in self.conversations.notices_of(CID)], [expected])

    def test_notices_are_numbered_in_their_chat(self):
        self.add(text=notice_text("EM-1000001"))
        self.add(text=notice_text("EM-1000002"), at=NOW + timedelta(seconds=1))
        self.add(cid=CID_OTHER, text=notice_text("EM-1000003"))
        self.assertEqual([n["_id"] for n in self.conversations.notices_of(CID)], [CID + "#N00001", CID + "#N00002"])
        self.assertEqual([n["_id"] for n in self.conversations.notices_of(CID_OTHER)], [CID_OTHER + "#N00001"])

    def test_the_same_notice_twice_is_written_once(self):
        first, _ = self.add()
        again, written = self.add(at=NOW + timedelta(minutes=3))
        self.assertFalse(written)
        self.assertEqual(again["_id"], first["_id"])
        self.assertEqual(again["at"], NOW.isoformat())
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)

    def test_erasure_finds_counts_and_removes_the_persons_notices(self):
        # Ruling 21, on the write side: a chat that holds only a notice is
        # still the person's, and both deletions take it.
        self.chat()
        self.add()
        self.add(cid=CID_OTHER, text=notice_text("EM-1000002"))
        self.assertEqual(self.conversations.conversations_of(RIDER_KEY), sorted([CID, CID_OTHER]))
        self.assertEqual(self.conversations.delete_person(RIDER_KEY, dry_run=True)["conversation_notices"], 2)
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)
        self.assertEqual(self.conversations.delete_conversation(CID_OTHER)["conversation_notices"], 1)
        self.assertEqual(self.conversations.delete_person(RIDER_KEY)["conversation_notices"], 1)
        self.assertEqual(self.conversations.notices_of(CID), [])
        self.assertEqual(self.conversations.conversations_of(RIDER_KEY), [])


class MemoryNoticeTests(MemoryStores, NoticeContract, unittest.TestCase):
    pass


class MongoNoticeTests(MongoStores, NoticeContract, unittest.TestCase):
    def test_a_dry_run_of_one_conversation_counts_its_notices(self):
        self.add()
        self.assertEqual(self.conversations.delete_conversation(CID, dry_run=True)["conversation_notices"], 1)
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)

    def test_one_notice_per_kind_and_text_is_held_by_the_database(self):
        # Two servers writing the same closure: the second insert collides.
        info = self.db[CONVERSATION_NOTICES].index_information()
        self.assertEqual(info["one_notice_per_text"]["key"], [("conversation_id", 1), ("kind", 1), ("text", 1)])
        self.assertTrue(info["one_notice_per_text"]["unique"])
        first, _ = self.add()
        notices = self.db[CONVERSATION_NOTICES]
        real_find_one = notices.find_one
        looks = []

        def find_one(query, *args, **kwargs):
            # The other server's notice lands between this one's look and its write.
            looks.append(query)
            return None if len(looks) == 1 else real_find_one(query, *args, **kwargs)

        with mock.patch.object(notices, "find_one", side_effect=find_one):
            again, written = self.add(at=NOW + timedelta(minutes=1))
        self.assertFalse(written)
        self.assertEqual(again["_id"], first["_id"])
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)

    def test_a_store_that_cannot_answer_is_store_unavailable(self):
        with mock.patch.object(self.db[CONVERSATION_NOTICES], "insert_one",
                               side_effect=ServerSelectionTimeoutError("no servers")):
            with self.assertRaises(StoreUnavailable):
                self.add()

    def test_the_index_table_says_so_and_nothing_expires(self):
        self.assertIn(([("conversation_id", 1), ("kind", 1), ("text", 1)],
                       {"name": "one_notice_per_text", "unique": True}), INDEXES[CONVERSATION_NOTICES])
        for _, options in INDEXES[CONVERSATION_NOTICES]:
            self.assertNotIn("expireAfterSeconds", options)


# -- close_ticket: the record, the notice and the push ----------------------------


class CloseTicketContract(StoreKinds):
    def test_a_closure_closes_the_record_saves_the_notice_and_pushes_to_every_socket_of_the_rider(self):
        self.chat()
        reference = self.ticket()
        self.assertEqual(self.close(), "closed")
        self.assertEqual(self.tickets.ticket_status(reference),
                         {"reference": reference, "status": "closed", "closed_at": CLOSED_AT.isoformat()})
        [notice] = self.conversations.notices_of(CID)
        self.assertEqual(dict(notice), {"_id": CID + "#N00001", "conversation_id": CID, "user_key": RIDER_KEY,
                                        "kind": "ticket_closed", "text": notice_text(reference),
                                        "at": NOW.isoformat()})
        expected = {
            "type": "ticket_update",
            "conversation_id": CID,
            "ticket": {"reference": reference, "status": "closed", "closed_at": "2026-10-07T11:02:10Z"},
            "message": {"id": CID + "#N00001", "sender": "system", "text": notice_text(reference),
                        "sent_at": "2026-10-07T11:02:12Z", "attachments": []},
        }
        mine, theirs = self.pushed()
        self.assertEqual(mine, [[expected], [expected]])
        self.assertEqual(theirs, [])

    def test_the_same_closure_again_changes_nothing_and_sends_nothing(self):
        # Review Focus 4: Zoho calls twice for one closure, or the agent
        # edits a ticket that is already closed.
        self.chat()
        self.ticket()
        self.assertEqual(self.close(), "closed")
        self.assertEqual(self.close(closed_at=CLOSED_AT + timedelta(hours=2), now=NOW + timedelta(hours=2)),
                         "already_closed")
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)
        self.assertEqual(self.tickets.ticket_status(self.reference_of[CID])["closed_at"], CLOSED_AT.isoformat())
        mine, _ = self.pushed()
        self.assertEqual([len(frames) for frames in mine], [1, 1])

    def test_a_zoho_ticket_that_is_not_ours_is_not_ours_and_nothing_is_written(self):
        self.chat()
        reference = self.ticket()
        self.assertEqual(self.close(zoho_ticket_id=OTHER_ZOHO_ID), "not_ours")
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "open")
        self.assertEqual(self.conversations.notices_of(CID), [])
        self.assertEqual(self.pushed(), ([[], []], []))

    def test_a_website_chat_gets_the_record_closed_and_the_notice_but_no_push(self):
        # Ruling 18: v1 history holds app chats only, so a push would name a
        # chat the app cannot open.
        self.chat(cid=CID_WEB, channel="website_chat")
        reference = self.ticket(cid=CID_WEB)
        self.assertEqual(self.close(), "closed")
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "closed")
        self.assertEqual([n["text"] for n in self.conversations.notices_of(CID_WEB)], [notice_text(reference)])
        self.assertEqual(self.pushed(), ([[], []], []))

    def test_a_rider_with_no_open_socket_gets_it_through_history(self):
        # The ticket first, so the run's summary names it, as the runtime's does.
        reference = self.ticket()
        self.chat()
        for sock in self.mine:
            self.registry.remove(RIDER_KEY, sock)
        self.assertEqual(self.close(), "closed")
        page = history.list_messages(self.stores, rider(), CID, limit=50, before=None, after=None, now=NOW,
                                     signer=no_links)
        self.assertEqual(page["messages"][-1], {"id": CID + "#N00001", "sender": "system",
                                                "text": notice_text(reference), "sent_at": "2026-10-07T11:02:12Z",
                                                "attachments": []})
        [listed] = history.list_conversations(self.stores, rider(), limit=20, cursor=None, channel=None,
                                              now=NOW)["conversations"]
        self.assertEqual(listed["ticket"], {"reference": reference, "status": "closed",
                                            "closed_at": "2026-10-07T11:02:10Z"})
        self.assertEqual(listed["message_count"], 3)

    def test_another_riders_chat_never_reaches_this_riders_sockets(self):
        self.chat(cid=CID_OTHER, user_key=OTHER_KEY)
        reference = self.ticket(cid=CID_OTHER)
        self.assertEqual(self.close(), "closed")
        [notice] = self.conversations.notices_of(CID_OTHER)
        self.assertEqual(notice["user_key"], OTHER_KEY)
        mine, theirs = self.pushed()
        self.assertEqual(mine, [[], []])
        self.assertEqual([f["ticket"]["reference"] for f in theirs], [reference])

    def test_a_chat_with_nothing_recorded_is_closed_but_gets_no_notice(self):
        # Erased at the person's request (erasure does not reach `tickets`
        # yet): nothing is written back into a chat that is gone.
        reference = self.ticket()
        self.assertEqual(self.close(), "unknown")
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "closed")
        self.assertEqual(self.conversations.notices_of(CID), [])
        self.assertEqual(self.conversations.conversations_of(RIDER_KEY), [])
        self.assertEqual(self.pushed(), ([[], []], []))

    def test_no_closing_time_from_zoho_is_the_time_it_arrived(self):
        self.chat()
        reference = self.ticket()
        self.assertEqual(self.close(closed_at=None), "closed")
        self.assertEqual(self.tickets.ticket_status(reference)["closed_at"], NOW.isoformat())

    def test_a_closure_whose_notice_was_not_written_is_finished_by_the_retry(self):
        # Ruling 19: the store failed after the record closed; Zoho's retry
        # writes the notice and pushes it, once.
        self.chat()
        reference = self.ticket()
        with mock.patch.object(self.conversations, "add_notice", side_effect=StoreUnavailable("down")):
            with self.assertRaises(StoreUnavailable):
                self.close()
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "closed")
        self.assertEqual(self.close(), "closed")
        self.assertEqual(self.close(), "already_closed")
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)
        mine, _ = self.pushed()
        self.assertEqual([len(frames) for frames in mine], [1, 1])

    def test_a_turn_and_a_notice_at_the_same_instant_show_the_turn_first(self):
        # Ruling 20 pins today's order: a notice written while a turn is in
        # flight sorts after the turn recorded at the same instant.
        self.chat(at=NOW)
        reference = self.ticket()
        self.assertEqual(self.close(now=NOW), "closed")
        page = history.list_messages(self.stores, rider(), CID, limit=50, before=None, after=None, now=NOW,
                                     signer=no_links)
        self.assertEqual([(m["sender"], m["sent_at"]) for m in page["messages"]],
                         [("rider", "2026-10-07T11:02:12Z"), ("bot", "2026-10-07T11:02:12Z"),
                          ("system", "2026-10-07T11:02:12Z")])
        self.assertEqual(page["messages"][-1]["text"], notice_text(reference))


class MemoryCloseTicketTests(MemoryStores, CloseTicketContract, unittest.TestCase):
    pass


class MongoCloseTicketTests(MongoStores, CloseTicketContract, unittest.TestCase):
    pass


# -- the webhook's payload ---------------------------------------------------------


class ParserTests(unittest.TestCase):
    def parsed(self, body):
        return webhooks.parse_events(json.dumps(body).encode("utf-8"))

    def test_the_documented_sample_parses_as_an_open_ticket(self):
        [event] = self.parsed(documented_body())
        self.assertEqual(event, webhooks.WebhookEvent(zoho_ticket_id=SAMPLE_ZOHO_ID, closed=False,
                                                      closed_at=None))

    def test_a_closure_is_its_status_type_with_zohos_closing_time(self):
        [event] = self.parsed(closure_body())
        self.assertEqual(event, webhooks.WebhookEvent(zoho_ticket_id=ZOHO_ID, closed=True, closed_at=CLOSED_AT))
        # The Tickets API's own spelling.
        [event] = self.parsed(closure_body(status_type="CLOSED"))
        self.assertTrue(event.closed)

    def test_the_status_name_alone_never_closes_a_ticket(self):
        # Status names are each department's: Zoho's own example has a status
        # called "Closed" whose type is Open.
        [event] = self.parsed(closure_body(status="Closed", status_type="Open"))
        self.assertFalse(event.closed)
        [event] = self.parsed(closure_body(status="Resolved", status_type="Closed"))
        self.assertTrue(event.closed)
        for other in ("On Hold", "ONHOLD", "Open", "", None, 7):
            with self.subTest(status_type=other):
                [event] = self.parsed(closure_body(status_type=other))
                self.assertFalse(event.closed)

    def test_without_a_closing_time_the_event_time_is_used_and_then_nothing(self):
        body = closure_body(closed_time=None)
        [event] = self.parsed(body)
        self.assertEqual(event.closed_at, datetime(2021, 4, 15, 13, 34, 27, 780000, tzinfo=timezone.utc))
        del body[0]["eventTime"]
        [event] = self.parsed(body)
        self.assertIsNone(event.closed_at)
        body[0]["payload"]["closedTime"] = "not a time"
        body[0]["eventTime"] = "१६१८४९३६६७७८०"  # Devanagari digits are not a time
        [event] = self.parsed(body)
        self.assertIsNone(event.closed_at)
        self.assertTrue(event.closed)

    def test_another_event_type_is_ignored(self):
        [event] = self.parsed(closure_body(event_type="Ticket_Add"))
        self.assertEqual(event, webhooks.WebhookEvent(skip="ignored"))

    def test_a_broken_event_is_unparsable_and_the_others_still_read(self):
        good = closure_body()[0]
        no_id = copy.deepcopy(good)
        del no_id["payload"]["id"]
        indic_id = copy.deepcopy(good)
        indic_id["payload"]["id"] = "५५४३००"  # an id is ASCII digits only
        events = self.parsed([good, "text", {"eventType": "Ticket_Update"}, no_id, indic_id,
                              dict(good, payload=[1, 2])])
        self.assertEqual(events[0].zoho_ticket_id, ZOHO_ID)
        self.assertEqual([e.skip for e in events[1:]], ["unparsable"] * 5)

    def test_a_numeric_id_reads_as_its_digits(self):
        body = closure_body()
        body[0]["payload"]["id"] = 55430000000000101
        [event] = self.parsed(body)
        self.assertEqual(event.zoho_ticket_id, ZOHO_ID)

    def test_a_body_that_is_not_a_json_list_is_unparsable(self):
        for raw in (b"", b"not json", b"{}", json.dumps(closure_body()[0]).encode(), b"\xff\xfe", b"[1, 2"):
            with self.subTest(raw=raw):
                with self.assertRaises(webhooks.Unparsable):
                    webhooks.parse_events(raw)


# -- the webhook, through the API ----------------------------------------------------


class WebhookTests(SocketCase):
    """POST /webhooks/zoho/tickets/<secret> on the real API, with the fake
    runtime of the socket tests recording the rider's app chat."""

    def setUp(self):
        super().setUp()
        self.ctx.zoho_webhook_secret = SECRET
        self.tickets = self.api.stores.tickets

    def app_chat(self):
        """The rider's app chat, through the socket, and our ticket for it in Zoho Desk."""
        with self.socket() as ws:
            self.exchange(ws, message())
        record = record_for(self.tickets, conversation_id=CID, zoho=zoho(ticket_id=ZOHO_ID))
        return record["_id"]

    def post(self, body, path=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        return self.client.post(path or "%s/%s" % (WEBHOOK, SECRET), content=data,
                                headers={"Content-Type": "application/json"})

    def logged(self):
        return [e for e in self.api.log.events if e["event"] == "zoho_webhook"]

    def test_a_closure_reaches_every_open_socket_of_the_rider_and_the_history(self):
        reference = self.app_chat()
        with self.socket() as first, self.socket() as second, self.socket(self.other_token) as theirs:
            answer = self.post(closure_body())
            self.assertEqual((answer.status_code, answer.json()), (200, {"status": "ok"}))
            pushed = [frame(first), frame(second)]
            quiet(theirs)
        for sent in pushed:
            self.assertEqual(sent["type"], "ticket_update")
            self.assertEqual(sent["conversation_id"], CID)
            self.assertEqual(sent["ticket"], {"reference": reference, "status": "closed",
                                              "closed_at": "2026-10-07T11:02:10Z"})
            self.assertEqual(sent["message"]["sender"], "system")
            self.assertEqual(sent["message"]["text"], notice_text(reference))
            self.assertEqual(sent["message"]["id"], CID + "#N00001")
        messages = self.client.get("/amiigo/v1/conversations/%s/messages" % CID, headers=self.auth()).json()
        self.assertEqual(messages["messages"][-1], pushed[0]["message"])
        [entry] = self.logged()
        self.assertEqual((entry["outcome"], entry["ticket_hash"]), ("closed", ticket_hash(ZOHO_ID)))

    def test_the_same_closure_twice_sends_one_update(self):
        self.app_chat()
        with self.socket() as ws:
            self.assertEqual(self.post(closure_body()).status_code, 200)
            self.assertEqual(frame(ws)["type"], "ticket_update")
            self.assertEqual(self.post(closure_body()).status_code, 200)
            quiet(ws)
        self.assertEqual(len(self.conversations.notices_of(CID)), 1)
        self.assertEqual([e["outcome"] for e in self.logged()], ["closed", "already_closed"])

    def test_a_ticket_that_is_not_ours_is_200_and_a_log_line(self):
        reference = self.app_chat()
        with self.socket() as ws:
            self.assertEqual(self.post(closure_body(zoho_ticket_id=OTHER_ZOHO_ID)).status_code, 200)
            quiet(ws)
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "open")
        [entry] = self.logged()
        self.assertEqual((entry["outcome"], entry["ticket_hash"]), ("not_ours", ticket_hash(OTHER_ZOHO_ID)))

    def test_a_status_other_than_closed_is_ignored(self):
        reference = self.app_chat()
        body = documented_body()
        body[0]["payload"]["id"] = ZOHO_ID
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "open")
        self.assertEqual(self.conversations.notices_of(CID), [])
        self.assertEqual([(e["outcome"], e["ticket_hash"]) for e in self.logged()],
                         [("ignored", ticket_hash(ZOHO_ID))])

    def test_a_payload_it_cannot_read_is_200_and_logged(self):
        self.app_chat()
        for raw in (b"not json", b"{}", b""):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(None, raw=raw).status_code, 200)
        self.assertEqual([e["outcome"] for e in self.logged()], ["unparsable"] * 3)

    def test_a_body_over_the_limit_is_not_read(self):
        with mock.patch.object(webhooks, "MAX_BODY_BYTES", 64):
            self.assertEqual(self.post(closure_body()).status_code, 200)
        self.assertEqual([e["outcome"] for e in self.logged()], ["too_large"])

    def test_a_missing_or_wrong_secret_is_401_and_nothing_is_read(self):
        reference = self.app_chat()
        for path in (WEBHOOK, "%s/%s" % (WEBHOOK, SECRET[:-1] + "x"), "%s/%s" % (WEBHOOK, SECRET + "x"),
                     "%s/%s" % (WEBHOOK, "short")):
            with self.subTest(path=path):
                answer = self.post(closure_body(), path=path)
                self.assertEqual((answer.status_code, answer.json()), (401, {"detail": "secret_invalid"}))
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "open")
        self.assertEqual([e["outcome"] for e in self.logged()], ["secret_invalid"] * 4)

    def test_no_secret_configured_is_503_and_does_nothing(self):
        reference = self.app_chat()
        self.ctx.zoho_webhook_secret = None
        for path in (WEBHOOK, "%s/%s" % (WEBHOOK, SECRET)):
            with self.subTest(path=path):
                answer = self.post(closure_body(), path=path)
                self.assertEqual((answer.status_code, answer.json()), (503, {"detail": "not_configured"}))
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "open")
        self.assertEqual([e["outcome"] for e in self.logged()], ["not_configured"] * 2)

    def test_a_store_that_fails_is_503_so_zoho_tries_again(self):
        # Ruling 19: never a 200 for a closure that was not recorded.
        reference = self.app_chat()
        with mock.patch.object(self.tickets, "close_support", side_effect=StoreUnavailable("MongoDB down")):
            answer = self.post(closure_body())
        self.assertEqual((answer.status_code, answer.json()), (503, {"detail": "store_unavailable"}))
        [entry] = self.logged()
        self.assertEqual((entry["outcome"], entry["ticket_hash"]), ("store_unavailable", ticket_hash(ZOHO_ID)))
        self.assertEqual(self.post(closure_body()).status_code, 200)
        self.assertEqual(self.tickets.ticket_status(reference)["status"], "closed")

    def test_the_log_never_carries_the_payload_the_secret_or_the_zoho_id(self):
        self.app_chat()
        body = closure_body()
        self.post(body)
        self.post(body, path="%s/%s" % (WEBHOOK, SECRET[:-1] + "x"))
        written = json.dumps(self.api.log.events, default=str)
        for private in (SECRET, SECRET[:-1] + "x", ZOHO_ID, "1618493667780", "54983163", "31138000000006907"):
            self.assertNotIn(private, written)

    def test_every_event_of_a_delivery_is_handled(self):
        reference = self.app_chat()
        body = closure_body(zoho_ticket_id=OTHER_ZOHO_ID) + documented_body() + closure_body()
        with self.socket() as ws:
            self.assertEqual(self.post(body).status_code, 200)
            self.assertEqual(frame(ws)["ticket"]["reference"], reference)
        self.assertEqual([e["outcome"] for e in self.logged()], ["not_ours", "ignored", "closed"])


class WebhookSecretTests(unittest.TestCase):
    def test_the_secret_comes_from_its_variable_and_must_be_long_and_plain(self):
        self.assertEqual(webhooks.SECRET_ENV, "EMOTORAD_ZOHO_WEBHOOK_SECRET")
        self.assertEqual(webhooks.webhook_secret_from_env({webhooks.SECRET_ENV: SECRET}), SECRET)
        self.assertEqual(webhooks.webhook_secret_from_env({webhooks.SECRET_ENV: " %s\n" % SECRET}), SECRET)
        for value in ("", "x" * 31, "x" * 257, SECRET + "/", SECRET + "?a=1", SECRET + " tail",
                      "x" * 31 + "é", "x" * 31 + "५"):
            with self.subTest(value=value):
                self.assertIsNone(webhooks.webhook_secret_from_env({webhooks.SECRET_ENV: value}))
        self.assertIsNone(webhooks.webhook_secret_from_env({}))

    def test_health_says_whether_the_webhook_is_on_never_the_secret(self):
        from tests.test_api_health import fresh_api, zoho_blank

        for value, shown in (("", "not configured"), ("too-short", "misconfigured: secret must be 32 to 256 "
                                                                    "letters, digits, - or _"), (SECRET, "on")):
            with self.subTest(shown=shown):
                with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None), quiet_tokens():
                    api = fresh_api(dict(zoho_blank(), EMOTORAD_AI_MODE="offline",
                                         EMOTORAD_ZOHO_WEBHOOK_SECRET=value))
                report = TestClient(api.app).get("/health").json()
                self.assertEqual(report["zoho_webhook"], shown)
                self.assertNotIn(SECRET, json.dumps(report))
                self.assertEqual(api.app.state.amiigo.zoho_webhook_secret, SECRET if shown == "on" else None)
        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None), quiet_tokens():
            fresh_api(dict(zoho_blank(), EMOTORAD_AI_MODE="offline"))

    def test_the_access_log_never_shows_the_secret_in_the_path(self):
        webhooks.hide_secret_in_access_log()
        webhooks.hide_secret_in_access_log()  # once is enough, however often it is called
        access = logging.getLogger("uvicorn.access")
        self.assertEqual(sum(isinstance(f, webhooks.HideWebhookSecret) for f in access.filters), 1)
        for path in ("%s/%s" % (WEBHOOK, SECRET), "%s/%s?x=1" % (WEBHOOK, SECRET)):
            with self.subTest(path=path):
                # The record uvicorn's access logger writes for a request.
                record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                                           ("127.0.0.1:50000", "POST", path, "1.1", 200), None)
                for f in access.filters:
                    self.assertTrue(f.filter(record))
                line = record.getMessage()
                self.assertNotIn(SECRET, line)
                self.assertIn("POST %s/[secret] HTTP/1.1" % WEBHOOK, line)
        other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                                  ("127.0.0.1:50000", "GET", "/health", "1.1", 200), None)
        for f in access.filters:
            f.filter(other)
        self.assertIn("GET /health HTTP/1.1", other.getMessage())

    def test_the_api_hides_the_secret_from_the_access_log_once_imported(self):
        from tests.test_api_health import fresh_api, zoho_blank

        access = logging.getLogger("uvicorn.access")
        for f in [f for f in access.filters if isinstance(f, webhooks.HideWebhookSecret)]:
            access.removeFilter(f)
        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None), quiet_tokens():
            fresh_api(dict(zoho_blank(), EMOTORAD_AI_MODE="offline"))
        self.assertTrue(any(isinstance(f, webhooks.HideWebhookSecret)
                            for f in logging.getLogger("uvicorn.access").filters))


if __name__ == "__main__":
    unittest.main()
