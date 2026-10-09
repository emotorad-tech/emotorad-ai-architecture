"""Store cards reach the transcript, the Amiigo message and the website (spec 2026-10-09, section 5)."""

import unittest

import mongomock

from emotorad_ai.amiigo import history
from emotorad_ai.contract import Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationState, InMemoryConversationStore, TranscriptTurn, transcript_turns
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes

CARD = {"ref": "D1", "name": "Test Cycles Pune", "address": "Shop 1, Test Road, Pune, Maharashtra - 411014",
        "pincode": "411014", "manager_name": "Test Manager One", "phone": "+91 9000000001", "distance_km": 3}


def turn_pair():
    state = ConversationState(conversation_id="c1", turns=1)
    inbound = InboundMessage(conversation_id="c1", persona="customer", identity=Identity(),
                             channel="website_chat", message_text="where can I take it?")
    reply = Reply(conversation_id="c1", text="Details below.", handled_by="battery_support", stores=[CARD])
    return state, inbound, reply


class TranscriptTests(unittest.TestCase):
    def test_the_bot_turn_keeps_the_cards_and_the_customer_turn_none(self):
        state, inbound, reply = turn_pair()
        customer, bot = transcript_turns(state, inbound, reply, "2026-10-09T10:00:00+00:00")
        self.assertEqual(customer.stores, ())
        self.assertEqual(bot.stores, (CARD,))

    def test_kept_and_read_back_by_both_stores(self):
        for store in (InMemoryConversationStore(), MongoConversationStore(self._db())):
            with self.subTest(store=type(store).__name__):
                state, inbound, reply = turn_pair()
                store.record_turn(state, inbound, reply)
                self.assertEqual(store.transcript("c1")[1].stores, (CARD,))

    def _db(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        return db


class HistoryTests(unittest.TestCase):
    def test_the_bot_message_carries_its_cards_and_others_carry_none(self):
        signer = lambda url: (url, None)
        bot = TranscriptTurn(n=2, role="bot", text="Details below.", at="2026-10-09T10:00:00+00:00",
                             stores=(CARD,), conversation_id="c1")
        plain = TranscriptTurn(n=1, role="customer", text="hi", at="2026-10-09T10:00:00+00:00", conversation_id="c1")
        self.assertEqual(history.message_view(bot, signer)["stores"], [CARD])
        self.assertNotIn("stores", history.message_view(plain, signer))


class WebsiteTests(unittest.TestCase):
    def test_message_out_carries_the_cards(self):
        from tests.test_prepare_turn import fresh_api

        api = fresh_api()
        out = api._message_out("c1", Reply(conversation_id="c1", text="Details below.", handled_by="x", stores=[CARD]))
        self.assertEqual(out.stores, [CARD])

    def test_the_page_renders_store_cards(self):
        from pathlib import Path

        page = (Path(__file__).resolve().parents[1] / "web" / "emotorad-support-chat-dev.html").read_text()
        self.assertIn("turn.stores", page)
        self.assertIn("store-card", page)
        self.assertIn("stores: reply.stores", page)


if __name__ == "__main__":
    unittest.main()
