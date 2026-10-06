"""One turn-preparation function for every way a message arrives
(the Amiigo support chat v1 plan, Task 4).

`api.prepare_turn` is what `POST /message` does between the parsed body and
`runtime.handle`: the attachments (inline photos stored, uploads claimed),
the photo safety check, the evidence check at ingest, the video summary,
the shared location, the place, the proved identity, the cluster and the
pinned agent. The chat socket (Task 5) calls it too, with
`channel="amiigo_app"` and the rider's phone from the token.

Here: it gives the runtime the same message `POST /message` gave before,
for a text turn, a photo turn and an upload turn; a rider's phone makes the
turn a verified customer's, as a verified web chat is, without the session
table; and `POST /message` refuses an Amiigo app chat's id (the plan's
Rulings 8 and 9). The bucket and the checkers are fakes; no network, no AWS.
"""

import importlib
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import mongomock
from fastapi import HTTPException
from fastapi.testclient import TestClient

from emotorad_ai.amiigo import history
from emotorad_ai.amiigo.auth import Rider, rider_hash_of
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationState, ConversationSummaryItem, StoreUnavailable, summary_key
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes
from emotorad_ai.tools import fixtures
from tests.amiigo_tokens import RIDER_PHONE
from tests.test_api_evidence_check import PASS, FakeEvidenceChecker
from tests.test_api_health import zoho_blank
from tests.test_api_media_persistence import jpeg_data_url
from tests.test_api_photo_check import FakeChecker as FakePhotoChecker
from tests.test_api_uploads import _Store, _Summariser

RIDER_KEY = "PHONE#" + RIDER_PHONE
# Chats the app has made: UUIDs, as the contract has them.
APP_CID = "aaaaaaaa-0000-4000-8000-0000000000a1"
OTHER_CID = "bbbbbbbb-0000-4000-8000-0000000000b2"
# An upload id as keys.new_upload_id makes one: two paths claim two ids.
_UPLOAD_ID = re.compile(r"upl_[0-9A-Za-z]+")
REFUSED = {"detail": "not your conversation"}


def fresh_api(store=None):
    env = dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory",
                "EMOTORAD_AI_MEDIA_BUCKET": "fake" if store else "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb",
                "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "", "EMOTORAD_EVIDENCE_CHECK": "",
                "EMOTORAD_AMIIGO_PUBLIC_KEY": "", "EMOTORAD_AMIGO_PG_DSN": "", "EMOTORAD_OMS_API_KEY": ""},
               **zoho_blank())
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store):
        import emotorad_ai.api as api

        return importlib.reload(api)


def comparable(message):
    """The message as the runtime reads it, less the moment it was made, and
    with each upload id the same (each path claims its own)."""
    out = message.to_dict()
    out.pop("timestamp")
    return json.loads(_UPLOAD_ID.sub("upl_X", json.dumps(out, sort_keys=True)))


def rider(phone=RIDER_PHONE):
    """The rider a token proves (amiigo/auth.rider_from_header)."""
    return Rider(phone=phone, user_key="PHONE#" + phone, emuser_id=None,
                 expires_at=datetime.now(timezone.utc) + timedelta(hours=1), rider_hash=rider_hash_of("PHONE#" + phone))


class ApiCase(unittest.TestCase):
    """The API with a fake bucket; the runtime's handle records the message
    it is given when `self.capture()` is on."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.addCleanup(fresh_api)
        self.client = TestClient(self.api.app)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.seen = []

    def capture(self):
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        patch = mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: self.seen.append(m) or scripted)
        patch.start()
        self.addCleanup(patch.stop)

    def post(self, **body):
        r = self.client.post("/message", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return self.seen[-1]

    def upload(self, mime="image/png", size=9, session="sess-ananya", cid="c1"):
        body = self.client.post("/uploads", json={"session_token": session, "conversation_id": cid,
                                                  "tree": "customers", "mime_type": mime, "size_bytes": size})
        self.assertEqual(body.status_code, 200, body.text)
        body = body.json()
        self.store.objects[body["key"]] = {"size": size, "mime": mime}  # the PUT, done
        return body

    def rider_upload(self, cid, mime="image/jpeg", size=9, phone=RIDER_PHONE):
        """An upload slot as POST /amiigo/v1/uploads makes one: under the
        cluster of the token's phone."""
        pending, _ = self.api.UPLOADS.begin_customer(self.api._cluster_for_phone(phone), cid, mime, size)
        self.store.objects[pending.key] = {"size": size, "mime": mime}
        return pending


class SameMessageAsPostMessageTests(ApiCase):
    """prepare_turn with what the body carried gives the message POST /message
    handed the runtime before the extraction."""

    def setUp(self):
        super().setUp()
        self.capture()

    def test_a_text_turn(self):
        sent = self.post(conversation_id="c1", session_token="sess-ananya", em_aid="aid-1", text="  hi there ",
                         pill="battery_issue", agent="battery_support")
        made = self.api.prepare_turn(conversation_id="c1", text="  hi there ", attachments=None, pill="battery_issue",
                                     screen=None, location=None, channel="website_chat", session_token="sess-ananya",
                                     em_aid="aid-1", agent="battery_support")
        self.assertEqual(comparable(made), comparable(sent))
        self.assertEqual(made.channel, "website_chat")
        self.assertEqual(made.identity.phone, fixtures.SESSIONS["sess-ananya"])
        self.assertEqual(made.entry_metadata["pinned_agent"], "battery_support")
        self.assertEqual(made.entry_metadata["cluster_id"], self.api._cluster_for_session("sess-ananya"))

    def test_an_anonymous_visitor_with_a_shared_location(self):
        class Geocoder:
            def reverse(self, lat, lon):
                return {"postcode": "122018", "suburb": "Sector 49"}

        self.api.geocoder = Geocoder()
        sent = self.post(em_aid="aid-loc", conversation_id="c2", text="",
                         location={"latitude": 28.41, "longitude": 77.05})
        made = self.api.prepare_turn(conversation_id="c2", text="", em_aid="aid-loc",
                                     location=self.api.LocationIn(latitude=28.41, longitude=77.05))
        self.assertEqual(comparable(made), comparable(sent))
        self.assertIn("Pincode 122018", made.message_text)
        self.assertNotIn("cluster_id", made.entry_metadata)  # a cookie alone records no cluster, as before

    def test_a_photo_turn_stored_checked_and_judged(self):
        # The photo is stored and recorded, safety-checked (nothing seen) and
        # judged as the battery fault's evidence, on both paths.
        self.api.PHOTO_CHECKER = FakePhotoChecker(answers=([],))
        self.api.EVIDENCE_CHECKER = FakeEvidenceChecker(PASS)
        self.api.runtime.evidence_check = True
        photo = {"kind": "image", "url": jpeg_data_url()}
        sent = self.post(conversation_id="c3", session_token="sess-ananya", text="my battery light is red",
                         attachments=[photo])
        made = self.api.prepare_turn(conversation_id="c3", text="my battery light is red", attachments=[photo],
                                     session_token="sess-ananya")
        self.assertEqual(comparable(made), comparable(sent))
        self.assertTrue(made.attachments[0].url.startswith("s3://customers/"), made.attachments[0].url)
        self.assertEqual(made.entry_metadata["evidence_verdict"]["passed"], True)
        self.assertEqual(len(self.api.stores.conversations.media_of("c3")), 2)  # one per path
        self.assertEqual(len(self.api.PHOTO_CHECKER.seen), 2)

    def test_a_photo_that_shows_a_hazard_carries_the_note(self):
        self.api.PHOTO_CHECKER = FakePhotoChecker(answers=(["smoke"],))
        photo = {"kind": "image", "url": jpeg_data_url()}
        sent = self.post(conversation_id="c4", session_token="sess-ananya", text="look", attachments=[photo])
        made = self.api.prepare_turn(conversation_id="c4", text="look", attachments=[photo],
                                     session_token="sess-ananya")
        self.assertEqual(comparable(made), comparable(sent))
        self.assertTrue(made.attachments[0].summary)

    def test_an_upload_turn_with_a_video_described(self):
        self.api.VIDEO_SUMMARISER = _Summariser("the pack is on a table")
        first, second = self.upload(mime="video/mp4"), self.upload(mime="video/mp4")
        sent = self.post(conversation_id="c1", session_token="sess-ananya", text="video attached",
                         attachments=[{"upload_id": first["upload_id"]}])
        made = self.api.prepare_turn(conversation_id="c1", text="video attached",
                                     attachments=[{"upload_id": second["upload_id"]}], session_token="sess-ananya")
        self.assertEqual(comparable(made), comparable(sent))
        self.assertEqual(made.attachments[0].url, "s3://" + second["key"])
        self.assertEqual(made.attachments[0].summary, "the pack is on a table")
        self.assertIsNone(self.api.UPLOADS.peek(second["upload_id"]), "claimed")

    def test_another_sessions_upload_is_refused_and_left_for_its_owner(self):
        theirs = self.upload(session="sess-rohit")
        with self.assertRaises(HTTPException) as refused:
            self.api.prepare_turn(conversation_id="c1", text="x", attachments=[{"upload_id": theirs["upload_id"]}],
                                  session_token="sess-ananya")
        self.assertEqual((refused.exception.status_code, refused.exception.detail), (403, "not your upload"))
        self.assertIsNotNone(self.api.UPLOADS.peek(theirs["upload_id"]))

    def test_the_place_comes_from_the_ip_given(self):
        class Locator:
            def __init__(self):
                self.seen = []

            def place(self, ip):
                from emotorad_ai.origin import Place

                self.seen.append(ip)
                return Place("IN", "Maharashtra", "Pune", "ip", "dbip")

        locator = Locator()
        with mock.patch.object(self.api, "IP_LOCATOR", locator):
            made = self.api.prepare_turn(conversation_id="c5", text="hi", em_aid="aid-5", client_ip="49.36.1.1")
        self.assertEqual(locator.seen, ["49.36.1.1"])
        self.assertEqual(made.entry_metadata["origin"]["city"], "Pune")
        self.assertNotIn("49.36.1.1", repr(made.to_dict()))


class ChannelAndRiderTests(ApiCase):
    """The channel argument, and the rider's phone from the token."""

    def test_the_channel_is_the_website_chat_unless_named(self):
        self.assertEqual(self.api.prepare_turn(conversation_id="c1", text="hi", em_aid="aid-1").channel,
                         "website_chat")

    def test_a_riders_turn_is_a_verified_customer_on_the_app(self):
        made = self.api.prepare_turn(conversation_id=APP_CID, text="hi", channel="amiigo_app",
                                     rider_phone=RIDER_PHONE, screen="battery_health", pill="battery")
        cluster = self.api._cluster_for_phone(RIDER_PHONE)
        self.assertEqual((made.channel, made.persona), ("amiigo_app", "customer"))
        self.assertEqual(made.identity, Identity(cluster_id=cluster, strength=VERIFIED, phone=RIDER_PHONE))
        self.assertTrue(made.identity.may_disclose)
        self.assertEqual(made.entry_metadata, {"cluster_id": cluster, "screen": "battery_health",
                                               "pill_clicked": "battery"})

    def test_the_riders_phone_skips_the_session_lookup(self):
        sessions = dict(self.api.resolver._sessions._sessions)
        with mock.patch.object(self.api.resolver, "resolve_website", side_effect=AssertionError("session lookup")):
            made = self.api.prepare_turn(conversation_id=APP_CID, text="hi", channel="amiigo_app",
                                         rider_phone=RIDER_PHONE, session_token="sess-ananya", em_aid="aid-1")
        self.assertEqual(made.identity.phone, RIDER_PHONE)
        self.assertIsNone(made.identity.em_aid)
        self.assertIsNone(made.identity.channel_user_id)
        # Never a back door through the fixture sessions (Ruling 2).
        self.assertEqual(self.api.resolver._sessions._sessions, sessions)

    def test_an_app_turn_without_the_riders_phone_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.prepare_turn(conversation_id=APP_CID, text="hi", channel="amiigo_app",
                                  session_token="sess-amiigo-test")

    def test_an_unknown_channel_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.prepare_turn(conversation_id="c1", text="hi", channel="whatsapp", em_aid="aid-1")

    def test_the_riders_upload_is_claimed_under_the_phones_cluster(self):
        mine = self.rider_upload(APP_CID)
        made = self.api.prepare_turn(conversation_id=APP_CID, text="", channel="amiigo_app", rider_phone=RIDER_PHONE,
                                     attachments=[{"upload_id": mine.upload_id}])
        self.assertEqual([(a.kind, a.url) for a in made.attachments], [("image", "s3://" + mine.key)])
        [record] = self.api.stores.conversations.media_of(APP_CID)
        self.assertEqual(record["key"], mine.key)

    def test_another_riders_upload_is_refused(self):
        theirs = self.rider_upload(APP_CID, phone="+919700000011")
        with self.assertRaises(HTTPException) as refused:
            self.api.prepare_turn(conversation_id=APP_CID, text="", channel="amiigo_app", rider_phone=RIDER_PHONE,
                                  attachments=[{"upload_id": theirs.upload_id}])
        self.assertEqual(refused.exception.status_code, 403)
        self.assertIsNotNone(self.api.UPLOADS.peek(theirs.upload_id))

    def test_the_runtime_treats_the_rider_as_verified(self):
        reply = self.api.runtime.handle(self.api.prepare_turn(
            conversation_id=APP_CID, text="my battery is not charging", channel="amiigo_app",
            rider_phone=RIDER_PHONE))
        # No verify-first question: the bikes on the rider's number instead.
        self.assertFalse(reply.handled_by.startswith("verify_first"), reply.handled_by)
        self.assertIn("EMXP2026001234", reply.text)
        self.assertIn("DDL32023045678", reply.text)
        [resolved] = [e for e in self.api.log.events if e["event"] == "identity_resolved"
                      and e["conversation_id"] == APP_CID]
        self.assertEqual((resolved["method"], resolved["strength"]), ("verified", "verified"))
        state = self.api.runtime.conversations.peek(APP_CID)
        self.assertEqual((state.user_key, state.channel), (RIDER_KEY, "amiigo_app"))
        self.assertEqual(state.cluster_id, self.api._cluster_for_phone(RIDER_PHONE))
        # The key the history reads by: the chat is the rider's app chat.
        self.assertTrue(history.is_riders_app_chat(self.api.stores, rider(), APP_CID))
        self.assertFalse(history.is_riders_app_chat(self.api.stores, rider("+919700000011"), APP_CID))


class AppChatRefusedOnTheWebsiteTests(ApiCase):
    """POST /message refuses an Amiigo app chat's id (Rulings 8 and 9): 403,
    the existing wording, and nothing recorded."""

    def app_chat(self, cid=APP_CID):
        self.api.runtime.handle(self.api.prepare_turn(conversation_id=cid, text="my battery is not charging",
                                                      channel="amiigo_app", rider_phone=RIDER_PHONE))
        return cid

    def assert_untouched(self, cid, turns, media):
        conversations = self.api.stores.conversations
        self.assertEqual(conversations.count_turns(cid), turns)
        self.assertEqual(len(conversations.media_of(cid)), media)
        self.assertEqual(self.store.objects, {})

    def test_a_website_message_to_an_app_chat_is_refused_and_nothing_is_recorded(self):
        cid = self.app_chat()
        turns = self.api.stores.conversations.count_turns(cid)
        handle = mock.patch.object(self.api.runtime, "handle", side_effect=AssertionError("a turn ran"))
        with handle:
            for body in ({"session_token": "sess-ananya"}, {"em_aid": "aid-1"}, {"session_token": "sess-amiigo-test"},
                         {}):
                with self.subTest(body=body):
                    r = self.client.post("/message", json=dict(body, conversation_id=cid, text="hello",
                                                               attachments=[{"kind": "image",
                                                                             "url": jpeg_data_url()}]))
                    self.assertEqual((r.status_code, r.json()), (403, REFUSED))
        self.assert_untouched(cid, turns, 0)

    def test_an_upload_is_not_claimed_by_a_refused_message(self):
        cid = self.app_chat()
        mine = self.rider_upload(cid)
        r = self.client.post("/message", json={"conversation_id": cid, "session_token": "sess-amiigo-test",
                                               "text": "", "attachments": [{"upload_id": mine.upload_id}]})
        self.assertEqual((r.status_code, r.json()), (403, REFUSED))
        self.assertIsNotNone(self.api.UPLOADS.peek(mine.upload_id), "still the rider's to send")
        self.assertEqual(self.api.stores.conversations.media_of(cid), [])

    def test_an_app_chat_past_its_working_state_is_still_refused(self):
        # Ruling 9: the permanent records decide, not the 48-hour state.
        cid = self.app_chat()
        self.api.stores.conversations._states.pop(cid)
        self.assertIsNone(self.api.stores.conversations.peek(cid))
        r = self.client.post("/message", json={"conversation_id": cid, "em_aid": "aid-1", "text": "hello"})
        self.assertEqual((r.status_code, r.json()), (403, REFUSED))
        self.assertIsNone(self.api.stores.conversations.peek(cid), "no website run started")

    def test_a_run_known_only_by_where_it_came_from_is_refused(self):
        self.api.stores.conversations.record_origin({
            "_id": summary_key(APP_CID, "2026-10-06T10:00:00+00:00"), "conversation_id": APP_CID,
            "started_at": "2026-10-06T10:00:00+00:00", "channel": "amiigo_app", "user_key": RIDER_KEY,
            "country": "IN", "region": None, "city": None, "source": "phone", "db": None})
        r = self.client.post("/message", json={"conversation_id": APP_CID, "em_aid": "aid-1", "text": "hello"})
        self.assertEqual((r.status_code, r.json()), (403, REFUSED))

    def test_a_run_known_only_by_its_summary_is_refused(self):
        state = ConversationState(conversation_id=APP_CID, user_key=RIDER_KEY, started_at="2026-10-06T10:00:00+00:00",
                                  turns=1)
        inbound = InboundMessage(conversation_id=APP_CID, persona="customer", channel="amiigo_app",
                                 message_text="hi", identity=Identity(strength=VERIFIED, phone=RIDER_PHONE))
        summary = ConversationSummaryItem(conversation_id=APP_CID, user_key=RIDER_KEY,
                                          started_at=state.started_at, last_at=state.started_at, channel="amiigo_app")
        self.api.stores.conversations.record_turn(state, inbound, Reply(APP_CID, "hello", "test"), summary)
        self.assertEqual(self.api.stores.conversations.origins_of(APP_CID), [])
        r = self.client.post("/message", json={"conversation_id": APP_CID, "em_aid": "aid-1", "text": "hello"})
        self.assertEqual((r.status_code, r.json()), (403, REFUSED))

    def test_a_website_chat_carries_on_as_before(self):
        for n in (1, 2):
            r = self.client.post("/message", json={"conversation_id": OTHER_CID, "session_token": "sess-amiigo-test",
                                                   "text": "my battery is not charging"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.api.stores.conversations.count_turns(OTHER_CID), 2 * n)
        self.assertEqual({o["channel"] for o in self.api.stores.conversations.origins_of(OTHER_CID)},
                         {"website_chat"})

    def test_a_store_that_cannot_answer_lets_the_turn_go_on_and_says_so(self):
        # The turn decides what a store outage means (a handover, or the
        # safety steps): never a 503 in front of a safety report.
        down = StoreUnavailable("MongoDB find failed")
        with mock.patch.object(self.api.stores.conversations, "origins_of", side_effect=down):
            r = self.client.post("/message", json={"conversation_id": OTHER_CID, "em_aid": "aid-1",
                                                   "text": "my battery is smoking"})
        self.assertEqual(r.status_code, 200, r.text)
        [failed] = [e for e in self.api.log.events if e["event"] == "app_chat_check_failed"]
        self.assertEqual((failed["conversation_id"], failed["error"]), (OTHER_CID, "StoreUnavailable"))


class AppChatRefusedOnMongoTests(unittest.TestCase):
    """The same refusal with the conversations on MongoDB (mongomock), which
    the store runs on the real clock: no TTL pin needed."""

    def setUp(self):
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.api = fresh_api()
        self.addCleanup(fresh_api)
        store = MongoConversationStore(self.db)
        self.api.runtime.conversations = store
        self.api.stores.conversations = store
        self.client = TestClient(self.api.app)

    def test_an_app_chat_is_refused_and_a_website_chat_is_not(self):
        self.api.runtime.handle(self.api.prepare_turn(conversation_id=APP_CID, text="my battery is not charging",
                                                      channel="amiigo_app", rider_phone=RIDER_PHONE))
        self.db["conversations"].delete_many({})  # the working state gone (Ruling 9)
        r = self.client.post("/message", json={"conversation_id": APP_CID, "em_aid": "aid-1", "text": "hello"})
        self.assertEqual((r.status_code, r.json()), (403, REFUSED))
        self.assertEqual(self.api.runtime.conversations.count_turns(APP_CID), 2)
        r = self.client.post("/message", json={"conversation_id": OTHER_CID, "em_aid": "aid-1", "text": "hello"})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
