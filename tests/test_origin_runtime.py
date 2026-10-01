"""The origin is recorded once per conversation run, from its first message."""

import itertools
import unittest
from unittest import mock

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import (ConversationConflict, ConversationState, InMemoryConversationStore,
                                      StoreUnavailable)
from emotorad_ai.llm import say
from tests.test_amigo_flow import Chat
from tests.test_verify_first import ONE_BIKE, RIDER
from tests.test_verify_first import Chat as VerifyChat

PUNE = {"country": "IN", "region": "Maharashtra", "city": "Pune", "source": "ip", "db": "dbip-city-lite-2026-10"}
DELHI = dict(PUNE, region="Delhi", city="New Delhi")


class OriginChat(Chat):
    def __init__(self, **kw):
        kw.setdefault("replies", [say("ok")] * 10)  # the agent turns a chat reaches
        super().__init__(**kw)
        ticks = ("2026-10-01T%02d:%02d:00+00:00" % divmod(n, 60) for n in itertools.count())
        self.conversations = InMemoryConversationStore(clock=lambda: next(ticks))
        self.runtime.conversations = self.conversations

    def send(self, text, origin=None, identity=None, channel="website_chat", cid="c1"):
        return self.runtime.handle(InboundMessage(
            conversation_id=cid, persona="customer", channel=channel, message_text=text,
            identity=identity or Identity(strength=ANONYMOUS, em_aid="aid-1"),
            entry_metadata={"origin": origin} if origin else {}))

    def verify_as(self, phone, origin=None):
        self.send("my battery isn't charging", origin)
        self.send(phone[3:])
        self.send(self.store.pending_code("c1"))
        return self.send("yes")


class OriginRuntimeTests(unittest.TestCase):
    def test_the_first_message_sets_the_runs_origin(self):
        chat = OriginChat()
        chat.send("hi", PUNE)
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["country"], record["region"], record["city"], record["source"]),
                         ("IN", "Maharashtra", "Pune", "ip"))
        self.assertEqual((record["channel"], record["user_key"]), ("website_chat", None))
        self.assertEqual(record["_id"], "c1#" + record["started_at"])

    def test_later_messages_do_not_change_it(self):
        chat = OriginChat()
        chat.send("hi", PUNE)
        chat.send("9700000031", DELHI)
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual(record["city"], "Pune")

    def test_an_unknown_origin_takes_the_country_of_a_phone_proven_later(self):
        chat = OriginChat()
        chat.verify_as("+919700000033")
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["country"], record["source"], record["city"]), ("IN", "phone", None))
        self.assertEqual(record["user_key"], "PHONE#+919700000033")

    def test_a_known_origin_takes_the_person_but_keeps_its_place(self):
        chat = OriginChat()
        chat.verify_as("+919700000033", origin=PUNE)
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["city"], record["source"]), ("Pune", "ip"))
        self.assertEqual(record["user_key"], "PHONE#+919700000033")

    def test_an_app_message_with_no_usable_ip_records_the_phones_country(self):
        chat = OriginChat()
        chat.send("my battery isn't charging", channel="amiigo_app",
                  identity=Identity(strength=VERIFIED, phone="+919700000031", em_aid="aid-1"))
        (record,) = chat.conversations.origins_of("c1")
        self.assertEqual((record["country"], record["source"], record["channel"]), ("IN", "phone", "amiigo_app"))
        self.assertEqual(record["user_key"], "PHONE#+919700000031")

    def test_a_new_run_records_its_own(self):
        chat = OriginChat()
        chat.send("hi", PUNE)
        chat.conversations._states.pop("c1")  # the working state expired
        chat.send("hello again", DELHI)
        self.assertEqual([r["city"] for r in chat.conversations.origins_of("c1")], ["Pune", "New Delhi"])

    def test_a_photo_only_first_message_still_records(self):
        chat = OriginChat()
        chat.send("", PUNE)
        self.assertEqual(len(chat.conversations.origins_of("c1")), 1)

    def test_a_store_that_cannot_record_does_not_stop_the_reply(self):
        chat = OriginChat()
        with mock.patch.object(chat.conversations, "record_origin", side_effect=StoreUnavailable("down")):
            reply = chat.send("hi", PUNE)
        self.assertTrue(reply.text)
        (event,) = [e for e in chat.runtime.log.events if e["event"] == "origin_record_failed"]
        self.assertEqual(event["error"], "StoreUnavailable")
        chat.send("still there?", DELHI)  # the next turn tries again
        self.assertEqual(len(chat.conversations.origins_of("c1")), 1)


class RacingStore(InMemoryConversationStore):
    """A fresh copy on every load, and refuses the next `conflicts` saves, as
    a durable store does when another server saved first."""

    def __init__(self, clock):
        super().__init__(clock=clock)
        self.conflicts = 0

    def get(self, cid):
        return ConversationState.from_json(super().get(cid).to_json())

    def save(self, state):
        if self.conflicts:
            self.conflicts -= 1
            raise ConversationConflict("another server saved first")
        super().save(state)


class SaveRaceTests(unittest.TestCase):
    def send(self, chat, text, origin):
        return chat.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), entry_metadata={"origin": origin}))

    def test_a_new_persons_run_never_takes_the_first_persons_origin(self):
        # The final review (2026-10-01): when the turn where a different person
        # verifies on an expired conversation loses the save race, the merge
        # kept the first run's origin, with the first person's key, for the
        # second run, and the second run was never recorded.
        now = [0.0]
        ticks = ("2026-10-01T%02d:%02d:00+00:00" % divmod(n, 60) for n in itertools.count())
        store = RacingStore(clock=lambda: next(ticks))
        chat = VerifyChat(replies=[say("ok")] * 8, clock=lambda: now[0], conversations=store)
        for text in ("my battery isn't charging", RIDER[3:]):
            self.send(chat, text, PUNE)
        self.send(chat, chat.code(), PUNE)
        self.send(chat, "2", PUNE)
        now[0] += 12 * 60 * 60 + 1  # the first person's verification expires
        self.send(chat, "hello again", DELHI)
        self.send(chat, ONE_BIKE[3:], DELHI)
        store.conflicts = 1
        self.send(chat, chat.code(), DELHI)  # the restart turn loses the race
        state = store.peek("c1")
        self.assertNotEqual((state.origin or {}).get("user_key"), "PHONE#" + RIDER)
        self.send(chat, "yes", DELHI)
        records = store.origins_of("c1")
        self.assertEqual([r["city"] for r in records], ["Pune", "New Delhi"])
        self.assertEqual([r["user_key"] for r in records], ["PHONE#" + RIDER, "PHONE#" + ONE_BIKE])
