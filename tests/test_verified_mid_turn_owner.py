"""A conversation belongs to the person the moment they verify, not one message later.

Found on 2026-09-29, from the person's question "why is there nothing in
conversation summary": a summary is written only for a conversation tied to a
proven identity (a cookie never gets a history, by design). The tie was made
only at the start of a turn, from how that message arrived. A visitor who
verified their number in their last message (code, then a ticket, in one turn)
was never tied: no summary, their next visit had no memory of it, and
delete_person.py --phone could not find the conversation to erase it. Now a
number verified during the turn ties the conversation before it is saved.
"""

import unittest
from datetime import date

import mongomock

from emotorad_ai.agents import battery_support
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.verification import VerificationStore

TODAY = date(2026, 7, 28)
PHONE = "+919876543210"
OWNER = "PHONE#" + PHONE


def runtime(store, replies):
    verification = VerificationStore()
    registry = build_registry(verification=verification, today=TODAY)
    rt = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry,
                 llm=ScriptedClaude(list(replies)), log=EventLog(path=None), resolver=IdentityResolver(registry),
                 conversations=store, self_service_identity=True, phone_resolver=verification.verified_phone)
    verification.issue("conv-v", PHONE, "123456")
    return rt


def anonymous(text):
    # Pinned in the message, as the chat page can: a MongoDB store hands out a
    # fresh copy on every get, so routing a copy here would never be saved.
    return InboundMessage(conversation_id="conv-v", persona="customer",
                          identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), channel="website_chat",
                          message_text=text, entry_metadata={"pinned_agent": battery_support.AGENT_NAME})


VERIFY_THEN_ANSWER = [call_tool("verify_identity", {"code": "123456"}), say("Thanks, that code checked out.")]


class OwnerTests(unittest.TestCase):
    def test_a_number_verified_in_the_turn_ties_the_conversation_and_writes_the_summary(self):
        store = InMemoryConversationStore()
        rt = runtime(store, VERIFY_THEN_ANSWER)
        rt.handle(anonymous("123456"))
        self.assertEqual(store.peek("conv-v").user_key, OWNER)
        [summary] = store.recent_summaries(OWNER)
        self.assertEqual(summary.conversation_id, "conv-v")

    def test_a_turn_that_verifies_nothing_ties_nothing(self):
        # The pair to the test above: the same anonymous visitor, no code.
        store = InMemoryConversationStore()
        rt = runtime(store, [say("What is happening with the bike?")])
        rt.handle(anonymous("hello"))
        self.assertIsNone(store.peek("conv-v").user_key)
        self.assertEqual(store.recent_summaries(OWNER), [])

    def test_erasure_by_phone_now_finds_the_conversation(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        store = MongoConversationStore(db)
        rt = runtime(store, VERIFY_THEN_ANSWER)
        rt.handle(anonymous("123456"))
        self.assertIn("conv-v", store.conversations_of(OWNER))
        counts = store.delete_person(OWNER, dry_run=True)
        self.assertGreater(counts["transcript_turns"], 0)
        self.assertEqual(counts["conversation_summaries"], 1)


if __name__ == "__main__":
    unittest.main()
