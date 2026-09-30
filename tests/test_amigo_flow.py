"""Amigo through a whole conversation, and the web chat's wiring."""

import importlib
import os
import unittest
from datetime import date
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.agents import battery_support
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.amigo import merged_source
from emotorad_ai.tools.mocks import GET_RECENT_TRIPS, build_registry
from emotorad_ai.tools.verification import VerificationStore
from tests.amigo_fake import RIDER_A, RIDER_B, RIDER_C, FakeAmigo

SECRET_BITS = ("860000000000032", "FRPVINTEST", "vin:", "TESTVIN")


class Chat:
    def __init__(self, replies=(), amigo=None):
        self.amigo = amigo or FakeAmigo()
        self.store = VerificationStore()
        self.registry = build_registry(
            verification=self.store, today=date(2026, 9, 30), amigo=self.amigo,
            warranty_source=merged_source(fixtures.WARRANTY_RECORDS.get, self.amigo))
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=self.registry,
                               llm=self.llm, log=EventLog(path=None), resolver=IdentityResolver(self.registry),
                               conversations=self.conversations, self_service_identity=True,
                               phone_resolver=self.store.verified_phone, verify_first=True)

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

    def verify(self, phone, first="my battery isn't charging"):
        self.say(first)
        self.say(phone[3:])
        return self.say(self.store.pending_code("c1"))


class FlowTests(unittest.TestCase):
    def test_rider_a_sees_both_app_bikes_and_the_agent_reads_trips(self):
        chat = Chat(replies=[call_tool(GET_RECENT_TRIPS, {}, "t1"), say("Thanks. How far do you usually ride?")])
        listed = chat.verify(RIDER_A)
        self.assertIn("EMX Plus (Aqua), frame TESTEMXP0000001", listed.text)
        self.assertIn("Doodle Pro (Nativepop), frame TESTDDLP0000002", listed.text)
        chat.say("2")
        self.assertEqual(chat.conversations.peek("c1").selected_frame, "TESTDDLP0000002")
        self.assertEqual(chat.conversations.peek("c1").agent, battery_support.AGENT_NAME)
        self.assertIn(("recent_trips", RIDER_A), chat.amigo.calls)

    def test_rider_b_never_sees_the_imei_or_vin(self):
        chat = Chat(replies=[say("Is the charger light on?")])
        listed = chat.verify(RIDER_B)
        self.assertIn("T-Rex Smart (Grey), frame number not on record", listed.text)
        chat.say("yes")
        said = " ".join(t.text for t in chat.conversations.transcript("c1")) + repr(chat.llm.requests)
        for bit in SECRET_BITS:
            self.assertNotIn(bit, said)

    def test_amigo_down_still_lets_the_rider_verify(self):
        chat = Chat(amigo=FakeAmigo(down=True))
        with self.assertLogs("emotorad_ai.tools.amigo", level="WARNING"):
            listed = chat.verify(RIDER_C)
        self.assertEqual(listed.handled_by, "verify_first:verified_no_bikes")


def fresh_api(amigo_dsn=""):
    env = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory", "EMOTORAD_OMS_API_KEY": "",
           "EMOTORAD_AI_MEDIA_BUCKET": "", "EMOTORAD_AMIGO_PG_DSN": amigo_dsn}
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
        import emotorad_ai.api as api
        return importlib.reload(api)


class WiringTests(unittest.TestCase):
    def tearDown(self):
        fresh_api()

    def test_health_says_whether_amigo_is_configured(self):
        self.assertEqual(TestClient(fresh_api().app).get("/health").json()["amigo"], "not configured")
        api = fresh_api("postgresql://ro_chatbot:x@localhost:5433/userbike?sslmode=require")
        self.assertEqual(TestClient(api.app).get("/health").json()["amigo"], "configured")

    def test_with_a_dsn_the_tools_exist(self):
        api = fresh_api("postgresql://ro_chatbot:x@localhost:5433/userbike?sslmode=require")
        self.assertIn(GET_RECENT_TRIPS, api.registry.specs)
        self.assertNotIn(GET_RECENT_TRIPS, fresh_api().registry.specs)


if __name__ == "__main__":
    unittest.main()
