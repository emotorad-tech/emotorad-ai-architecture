"""Uploads and deletion requests with the rider's token
(docs/contracts/amiigo-support-chat.md: "Photos and videos", "Deleting
conversation data", "Errors on the HTTP endpoints").

POST /amiigo/v1/uploads and POST /amiigo/v1/erasure-requests, /status and
/cancel, through the real API with a token minted by a keypair made here
(tests/amiigo_tokens.py). The bucket is the real S3Store on a fake client:
no AWS is touched. The chat checks run on both stores, in memory and on
mongomock, with the history tests' fixtures (tests/test_amiigo_history.py).
The website's /uploads and /erasure-requests* keep their own tests
(tests/test_api_uploads.py, tests/test_api_erasure.py); the classes at the
end pin what the two paths share.
"""

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai import erasure
from emotorad_ai.amiigo import auth as auth_module
from emotorad_ai.amiigo import history
from emotorad_ai.amiigo.auth import TokenCheck
from emotorad_ai.amiigo.common import CONVERSATION_ID_PATTERN, RiderLimiter
from emotorad_ai.conversation import StoreUnavailable, summary_key
from emotorad_ai.storage.keys import cluster_of
from emotorad_ai.storage.s3 import S3Store
from emotorad_ai.storage.uploads import UploadRegistry
from emotorad_ai.tools.fixtures import PHONE_AMIIGO_TEST_RIDER
from emotorad_ai.wiring import Stores
from tests.amiigo_tokens import RIDER_PHONE, Keypair
from tests.test_amiigo_history import (
    CID_A,
    CID_B,
    CID_C,
    CID_D,
    CID_E,
    NOW,
    OTHER_KEY,
    OTHER_PHONE,
    RIDER_KEY,
    MemoryStores,
    MongoStores,
)

BUCKET = "emotorad-ai-media-test"
# A chat the app has just made: nothing is recorded under it yet.
NEW_CID = "ffffffff-0000-4000-8000-00000000000f"
MB = 1024 * 1024
APP_TYPES = (("image/jpeg", "images", "jpg"), ("image/png", "images", "png"), ("image/webp", "images", "webp"),
             ("video/mp4", "videos", "mp4"), ("video/quicktime", "videos", "mov"), ("video/3gpp", "videos", "3gp"))
# The website's chat session for the Amiigo test rider (fixtures.SESSIONS).
APP_SESSION = "sess-amiigo-test"


class FakeS3Client:
    """What S3Store asks of boto3 to sign a PUT: a link, made here, never sent anywhere."""

    def __init__(self):
        self.asked = []
        self.fail = False

    def generate_presigned_url(self, operation, Params, ExpiresIn, HttpMethod=None):
        if self.fail:
            raise RuntimeError("Unable to locate credentials")
        self.asked.append((operation, dict(Params), ExpiresIn, HttpMethod))
        return "https://%s.s3.ap-south-1.amazonaws.com/%s?X-Amz-Signature=fake" % (Params["Bucket"], Params["Key"])


def api_with(key):
    from tests.test_api_health import fresh_api, zoho_blank

    with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
        return fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AMIIGO_PUBLIC_KEY": key,
                               "EMOTORAD_STORE": "memory", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"}, **zoho_blank()))


class AmiigoApi:
    """The API, its Amiigo context pointed at this test's stores and bucket."""

    use_api_stores = False

    @classmethod
    def setUpClass(cls):
        cls.keys = Keypair()
        cls.api = api_with(cls.keys.public_hex)
        cls.ctx = cls.api.app.state.amiigo
        cls.token_check = cls.ctx.tokens

    @classmethod
    def tearDownClass(cls):
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            api_with("")

    def setUp(self):
        if self.use_api_stores:
            self.ctx.stores = self.api.stores
        else:
            self.make_stores()
            self.ctx.stores = Stores(conversations=self.conversations, idempotency=None, tickets=self.tickets)
        self.s3 = FakeS3Client()
        self.bucket = S3Store(BUCKET, client=self.s3)
        self.ctx.media_store = self.bucket
        self.ctx.uploads = UploadRegistry(self.bucket)
        self.ctx.upload_limiter = RiderLimiter(20)
        self.ctx.tokens = self.token_check
        self.ctx.clock = lambda: NOW
        self.api.log.events.clear()
        self.client = TestClient(self.api.app)
        self.rider_token = self.keys.token()
        self.other_token = self.keys.token(phone=OTHER_PHONE)

    def headers(self, token):
        token = self.rider_token if token is None else token
        return {"Authorization": "Bearer " + token} if token else {}

    def refused(self, r, status, code):
        self.assertEqual((r.status_code, r.json()), (status, {"detail": code}), r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def malformed(self, r):
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIsInstance(r.json()["detail"], list)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def events(self, name):
        return [e for e in self.api.log.events if e["event"] == name]


# -- uploads ------------------------------------------------------------------------


class UploadContract(AmiigoApi):
    def upload(self, token=None, **body):
        payload = dict({"conversation_id": NEW_CID, "mime_type": "image/jpeg", "size_bytes": 245760}, **body)
        return self.client.post("/amiigo/v1/uploads", json=payload, headers=self.headers(token))

    def ok(self, r):
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        return r.json()

    def test_a_new_chat_gets_a_slot_keyed_under_the_riders_cluster(self):
        body = self.ok(self.upload())
        self.assertEqual(set(body), {"upload_id", "url", "headers", "expires_in"})
        self.assertEqual(body["expires_in"], 300)
        self.assertEqual(body["headers"], {"Content-Type": "image/jpeg", "Content-Length": "245760"})
        pending = self.ctx.uploads.peek(body["upload_id"])
        self.assertEqual(pending.tree, "customers")
        # The cluster the website's session path gives the same verified
        # phone, so the upload is claimed by the same person either way.
        self.assertEqual(PHONE_AMIIGO_TEST_RIDER, RIDER_PHONE)
        cluster = self.api._cluster_for_session(APP_SESSION)
        self.assertEqual(cluster_of(pending.key), cluster)
        self.assertEqual(pending.key, "customers/%s/%s/images/%s.jpg" % (cluster, NEW_CID, body["upload_id"]))
        self.assertNotIn(RIDER_PHONE[3:], pending.key)
        self.assertEqual(self.s3.asked, [("put_object", {"Bucket": BUCKET, "Key": pending.key,
                                                        "ContentType": "image/jpeg", "ContentLength": 245760},
                                          300, "PUT")])
        self.assertEqual(body["url"], "https://%s.s3.ap-south-1.amazonaws.com/%s?X-Amz-Signature=fake"
                         % (BUCKET, pending.key))

    def test_every_listed_photo_and_video_type_gets_a_slot(self):
        for mime, kind, extension in APP_TYPES:
            with self.subTest(mime=mime):
                body = self.ok(self.upload(mime_type=mime))
                key = self.ctx.uploads.peek(body["upload_id"]).key
                self.assertEqual(key.split("/")[3], kind)
                self.assertTrue(key.endswith("." + extension), key)
                self.assertEqual(body["headers"]["Content-Type"], mime)

    def test_the_riders_own_app_chat_takes_uploads(self):
        self.exchange(CID_A, NOW - timedelta(hours=1))
        self.ok(self.upload(conversation_id=CID_A))

    def test_a_chat_that_is_not_new_and_not_the_riders_app_chat_is_not_found(self):
        self.exchange(CID_B, NOW - timedelta(hours=1), user_key=OTHER_KEY)  # another rider's app chat
        self.exchange(CID_C, NOW - timedelta(hours=1), channel="website_chat")  # the rider's website chat
        self.exchange(CID_D, NOW - timedelta(hours=1), user_key=None, channel="website_chat",
                      origin=False)  # an anonymous chat: turns, no owner
        self.conversations.record_origin({  # a run that recorded only where it came from
            "_id": summary_key(CID_E, NOW.isoformat()), "conversation_id": CID_E, "started_at": NOW.isoformat(),
            "channel": "website_chat", "user_key": None, "country": "IN", "region": None, "city": None,
            "source": "ip", "db": None})
        for cid in (CID_B, CID_C, CID_D, CID_E):
            with self.subTest(cid=cid):
                self.refused(self.upload(conversation_id=cid), 404, "conversation_not_found")
        self.assertEqual(self.s3.asked, [])
        # The other rider's own chat is theirs to upload into.
        self.ok(self.upload(token=self.other_token, conversation_id=CID_B))

    def test_photos_up_to_10_mb_and_videos_up_to_100_mb(self):
        self.ok(self.upload(mime_type="image/png", size_bytes=10 * MB))
        self.refused(self.upload(mime_type="image/png", size_bytes=10 * MB + 1), 413, "file_too_large")
        self.ok(self.upload(mime_type="video/mp4", size_bytes=100 * MB))
        self.refused(self.upload(mime_type="video/mp4", size_bytes=100 * MB + 1), 413, "file_too_large")
        self.refused(self.upload(mime_type="video/quicktime", size_bytes=10 ** 12), 413, "file_too_large")

    def test_a_type_not_listed_is_refused_even_one_the_website_takes(self):
        for mime in ("image/gif", "application/pdf", "image/heic", "IMAGE/JPEG", "video/x-msvideo", ""):
            with self.subTest(mime=mime):
                self.refused(self.upload(mime_type=mime), 415, "file_type_not_accepted")
        self.assertEqual(self.s3.asked, [])

    def test_a_malformed_body_is_422_with_the_field_list(self):
        for body in ({"conversation_id": None}, {"conversation_id": "c1"}, {"conversation_id": NEW_CID + "\n"},
                     {"conversation_id": NEW_CID.replace("-", "")}, {"size_bytes": 0}, {"size_bytes": -5},
                     {"size_bytes": "lots"}, {"mime_type": None}):
            with self.subTest(body=body):
                self.malformed(self.upload(**body))
        self.malformed(self.client.post("/amiigo/v1/uploads", json={}, headers=self.headers(None)))
        self.assertEqual(self.s3.asked, [])

    def test_an_upper_case_uuid_is_a_chat_id_too(self):
        # iOS writes a UUID in capitals; the id is used as the app sent it.
        body = self.ok(self.upload(conversation_id=NEW_CID.upper()))
        self.assertIn("/%s/" % NEW_CID.upper(), self.ctx.uploads.peek(body["upload_id"]).key)

    def test_the_twenty_first_slot_in_a_minute_is_rate_limited(self):
        for _ in range(20):
            self.ok(self.upload())
        self.refused(self.upload(), 429, "rate_limited")
        # Another rider has their own allowance.
        self.ok(self.upload(token=self.other_token, conversation_id=CID_B))
        (event,) = self.events("amiigo_rate_limited")
        self.assertEqual((event["limit"], event["rider_hash"]), ("uploads", auth_module.rider_hash_of(RIDER_KEY)))

    def test_history_and_uploads_have_separate_allowances(self):
        self.ctx.history_limiter = RiderLimiter(1)
        self.client.get("/amiigo/v1/conversations", headers=self.headers(None))
        self.refused(self.client.get("/amiigo/v1/conversations", headers=self.headers(None)), 429, "rate_limited")
        self.ok(self.upload())

    def test_no_bucket_is_503_storage_unavailable(self):
        self.ctx.uploads = None
        self.refused(self.upload(), 503, "storage_unavailable")
        self.assertEqual([e["outcome"] for e in self.events("amiigo_upload")], ["storage_not_configured"])

    def test_storage_that_is_down_is_503_storage_unavailable(self):
        self.exchange(CID_A, NOW - timedelta(hours=1))
        down = StoreUnavailable("MongoDB find failed (ServerSelectionTimeoutError)")
        with mock.patch.object(self.conversations, "count_turns", side_effect=down):
            self.refused(self.upload(), 503, "storage_unavailable")
        with mock.patch.object(self.conversations, "runs_of", side_effect=down):
            self.refused(self.upload(conversation_id=CID_A), 503, "storage_unavailable")
        with mock.patch.object(self.conversations, "origins_of", side_effect=down):
            self.refused(self.upload(), 503, "storage_unavailable")
        self.s3.fail = True
        self.refused(self.upload(), 503, "storage_unavailable")
        self.assertEqual([(e["outcome"], e["error"]) for e in self.events("amiigo_upload")],
                         [("store_unavailable", str(down))] * 3 + [("presign_failed", "RuntimeError")])

    def test_a_refused_token_is_401_with_its_code(self):
        expired = self.keys.token(now=datetime.now(timezone.utc) - timedelta(hours=3))
        for token, code in (("", "token_missing"), (expired, "token_expired"),
                            (self.keys.token(token_type="refresh"), "token_type_not_allowed"),
                            (self.rider_token + "x", "token_invalid"), (Keypair().token(), "token_invalid")):
            with self.subTest(code=code):
                self.refused(self.upload(token=token), 401, code)
        self.assertEqual(self.s3.asked, [])

    def test_no_log_line_holds_the_phone_the_token_the_link_or_the_key(self):
        body = self.ok(self.upload())
        self.refused(self.upload(conversation_id=CID_B, token=self.other_token, mime_type="image/gif"), 415,
                     "file_type_not_accepted")
        self.exchange(CID_B, NOW, user_key=OTHER_KEY)
        self.refused(self.upload(conversation_id=CID_B), 404, "conversation_not_found")
        self.refused(self.upload(size_bytes=10 ** 12), 413, "file_too_large")
        logged = json.dumps(self.api.log.events)
        key = self.ctx.uploads.peek(body["upload_id"]).key
        for secret in (RIDER_PHONE, RIDER_PHONE[3:], OTHER_PHONE[3:], self.rider_token, self.other_token,
                       body["url"], "X-Amz-Signature", key, cluster_of(key)):
            self.assertNotIn(secret, logged)
        events = self.events("amiigo_upload")
        self.assertEqual([e["outcome"] for e in events],
                         ["ok", "file_type_not_accepted", "not_found", "file_too_large"])
        self.assertEqual([e["rider_hash"] for e in events],
                         [auth_module.rider_hash_of(k) for k in (RIDER_KEY, OTHER_KEY, RIDER_KEY, RIDER_KEY)])
        self.assertEqual(events[0]["kind"], "images")


class MemoryUploadTests(MemoryStores, UploadContract, unittest.TestCase):
    pass


class MongoUploadTests(MongoStores, UploadContract, unittest.TestCase):
    pass


# -- deletion requests -------------------------------------------------------------------


class ErasureContract(AmiigoApi):
    def erase(self, path="", token=None, body=None):
        return self.client.post("/amiigo/v1/erasure-requests" + path, json={} if body is None else body,
                                headers=self.headers(token))

    def ask(self, token=None, **body):
        return self.erase(token=token, body=dict({"confirm": True}, **body))

    def answer(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        return r.json()

    def test_a_rider_asks_once_and_asking_again_finds_the_same_request(self):
        first = self.answer(self.ask(conversation_id=NEW_CID), 201)
        reference = first["reference"]
        self.assertRegex(reference, r"^DEL-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{6}$")
        self.assertEqual(first, {"reference": reference, "status": "pending",
                                 "text": erasure.ERASURE_REQUESTED.format(reference=reference)})
        record = self.conversations.erasure_record(reference)
        self.assertEqual({name: record[name] for name in ("user_key", "channel", "proof", "conversation_id",
                                                          "requested_at", "status")},
                         {"user_key": RIDER_KEY, "channel": "amiigo_app", "proof": {"method": "app_sign_in"},
                          "conversation_id": NEW_CID, "requested_at": NOW.isoformat(), "status": "pending"})
        again = self.answer(self.ask(), 200)
        self.assertEqual(again, {"reference": reference, "status": "pending",
                                 "text": erasure.ERASURE_EXISTING.format(reference=reference)})

    def test_the_chat_id_is_optional_and_a_uuid_when_given(self):
        self.malformed(self.ask(conversation_id="c1"))
        self.assertIsNone(self.conversations.pending_erasure_of(RIDER_KEY))
        reference = self.answer(self.ask(), 201)["reference"]
        self.assertIsNone(self.conversations.erasure_record(reference)["conversation_id"])

    def test_without_confirm_true_nothing_is_recorded(self):
        for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {"confirm": None},
                     {"conversation_id": NEW_CID}):
            with self.subTest(body=body):
                self.refused(self.erase(body=body), 400, "confirm_required")
        self.refused(self.client.post("/amiigo/v1/erasure-requests", headers=self.headers(None)), 400,
                     "confirm_required")
        self.assertIsNone(self.conversations.pending_erasure_of(RIDER_KEY))

    def test_status_then_cancel_then_nothing_pending(self):
        self.assertEqual(self.answer(self.erase("/status")), {"reference": None, "status": "none"})
        reference = self.answer(self.ask(), 201)["reference"]
        self.assertEqual(self.answer(self.erase("/status")),
                         {"reference": reference, "status": "pending", "requested_at": NOW.isoformat()})
        self.assertEqual(self.answer(self.erase("/cancel")),
                         {"reference": reference, "status": "cancelled",
                          "text": erasure.ERASURE_CANCELLED.format(reference=reference)})
        self.refused(self.erase("/cancel"), 404, "nothing_pending")
        self.assertEqual(self.answer(self.erase("/status")), {"reference": None, "status": "none"})
        # A new request after a cancel is a new reference.
        self.assertNotEqual(self.answer(self.ask(), 201)["reference"], reference)

    def test_status_and_cancel_need_no_body(self):
        reference = self.answer(self.ask(), 201)["reference"]
        for path in ("/status", "/cancel"):
            with self.subTest(path=path):
                r = self.client.post("/amiigo/v1/erasure-requests" + path, headers=self.headers(None))
                self.assertEqual(self.answer(r)["reference"], reference)

    def test_one_riders_request_is_never_anothers(self):
        reference = self.answer(self.ask(), 201)["reference"]
        self.assertEqual(self.answer(self.erase("/status", token=self.other_token)),
                         {"reference": None, "status": "none"})
        self.refused(self.erase("/cancel", token=self.other_token), 404, "nothing_pending")
        self.assertEqual(self.answer(self.erase("/status"))["reference"], reference)
        theirs = self.answer(self.ask(token=self.other_token), 201)["reference"]
        self.assertNotEqual(theirs, reference)
        self.assertEqual(self.conversations.erasure_record(theirs)["user_key"], OTHER_KEY)

    def test_storage_that_is_down_is_503_storage_unavailable(self):
        down = StoreUnavailable("MongoDB find_one failed (ServerSelectionTimeoutError)")
        with mock.patch.object(self.conversations, "pending_erasure_of", side_effect=down):
            self.refused(self.ask(conversation_id=NEW_CID), 503, "storage_unavailable")
            self.refused(self.erase("/status"), 503, "storage_unavailable")
        with mock.patch.object(self.conversations, "cancel_erasure", side_effect=down):
            self.refused(self.erase("/cancel"), 503, "storage_unavailable")
        events = self.events("erasure_request_failed")
        self.assertEqual([(e["error"], e["rider_hash"]) for e in events],
                         [("StoreUnavailable", auth_module.rider_hash_of(RIDER_KEY))] * 3)
        self.assertEqual([e["conversation_id"] for e in events], [NEW_CID, "erasure", "erasure"])

    def test_a_request_and_a_cancel_are_logged_by_reference_and_the_riders_hash(self):
        reference = self.answer(self.ask(conversation_id=NEW_CID), 201)["reference"]
        self.answer(self.erase("/cancel"))
        rider_hash = auth_module.rider_hash_of(RIDER_KEY)
        self.assertEqual([(e["event"], e["conversation_id"], e["reference"], e["rider_hash"])
                          for e in self.api.log.events if e["event"].startswith("erasure_")],
                         [("erasure_requested", NEW_CID, reference, rider_hash),
                          ("erasure_cancelled", "erasure", reference, rider_hash)])
        logged = json.dumps(self.api.log.events)
        for secret in (RIDER_PHONE, RIDER_PHONE[3:], self.rider_token):
            self.assertNotIn(secret, logged)

    def test_a_refused_token_is_401_with_its_code(self):
        expired = self.keys.token(now=datetime.now(timezone.utc) - timedelta(hours=3))
        for path in ("", "/status", "/cancel"):
            for token, code in (("", "token_missing"), (expired, "token_expired"),
                                (self.keys.token(token_type="otp"), "token_type_not_allowed"),
                                (self.rider_token[:-4], "token_invalid")):
                with self.subTest(path=path, code=code):
                    self.refused(self.erase(path, token=token, body={"confirm": True}), 401, code)
        self.assertIsNone(self.conversations.pending_erasure_of(RIDER_KEY))


class MemoryErasureTests(MemoryStores, ErasureContract, unittest.TestCase):
    pass


class MongoErasureTests(MongoStores, ErasureContract, unittest.TestCase):
    pass


# -- the token check off (the plan's Ruling 1) ------------------------------------------


class TokenCheckOffTests(MemoryStores, AmiigoApi, unittest.TestCase):
    def test_every_route_is_503_storage_unavailable_before_the_token_is_read(self):
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            self.ctx.tokens = TokenCheck(None)
        routes = (("/amiigo/v1/uploads", {"conversation_id": NEW_CID, "mime_type": "image/jpeg", "size_bytes": 9}),
                  ("/amiigo/v1/erasure-requests", {"confirm": True}),
                  ("/amiigo/v1/erasure-requests/status", {}), ("/amiigo/v1/erasure-requests/cancel", {}))
        for path, body in routes:
            for headers in ({}, {"Authorization": "Bearer " + self.rider_token}, {"Authorization": "Basic x"}):
                with self.subTest(path=path, headers=headers):
                    self.refused(self.client.post(path, json=body, headers=headers), 503, "storage_unavailable")
        self.assertEqual(self.s3.asked, [])
        self.assertIsNone(self.conversations.pending_erasure_of(RIDER_KEY))
        self.assertEqual(self.events("amiigo_token_refused"), [])


# -- what the two paths share ------------------------------------------------------------


class SharedPiecesTests(unittest.TestCase):
    def test_the_app_rider_asks_as_their_number_on_the_app_with_its_sign_in(self):
        self.assertEqual(erasure.app_requester("+919700000010"),
                         ("PHONE#+919700000010", "amiigo_app", {"method": "app_sign_in"}))

    def test_a_chat_id_is_a_uuid_in_either_case(self):
        pattern = re.compile(CONVERSATION_ID_PATTERN)
        for text in (NEW_CID, NEW_CID.upper(), "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90"):
            self.assertTrue(pattern.fullmatch(text), text)
        for text in ("c1", NEW_CID.replace("-", ""), NEW_CID + "\n", " " + NEW_CID, "{%s}" % NEW_CID,
                     "urn:uuid:" + NEW_CID, NEW_CID[:-1] + "g", "३f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90", ""):
            self.assertIsNone(pattern.fullmatch(text), text)


class NewChatContract:
    def setUp(self):
        self.make_stores()
        self.stores = Stores(conversations=self.conversations, idempotency=None, tickets=self.tickets)

    def test_a_chat_with_nothing_recorded_is_new(self):
        self.assertTrue(history.is_new_conversation(self.conversations, NEW_CID))

    def test_a_turn_a_summary_or_an_origin_makes_a_chat_not_new(self):
        self.exchange(CID_A, NOW)  # turns, a summary and an origin
        self.exchange(CID_D, NOW, user_key=None, origin=False)  # turns only
        self.conversations.record_origin({
            "_id": summary_key(CID_E, NOW.isoformat()), "conversation_id": CID_E, "started_at": NOW.isoformat(),
            "channel": "website_chat", "user_key": None, "country": "IN", "region": None, "city": None,
            "source": "ip", "db": None})
        for cid in (CID_A, CID_D, CID_E):
            self.assertFalse(history.is_new_conversation(self.conversations, cid), cid)

    def test_a_rider_may_use_a_new_chat_or_their_own_app_chat_only(self):
        from emotorad_ai.amiigo.auth import Rider

        rider = Rider(phone=RIDER_PHONE, user_key=RIDER_KEY, emuser_id=None, expires_at=NOW,
                      rider_hash=auth_module.rider_hash_of(RIDER_KEY))
        self.exchange(CID_A, NOW)
        self.exchange(CID_B, NOW, user_key=OTHER_KEY)
        self.exchange(CID_C, NOW, channel="website_chat")
        self.assertEqual([history.rider_may_use(self.stores, rider, cid) for cid in (NEW_CID, CID_A, CID_B, CID_C)],
                         [True, True, False, False])


class MemoryNewChatTests(MemoryStores, NewChatContract, unittest.TestCase):
    pass


class MongoNewChatTests(MongoStores, NewChatContract, unittest.TestCase):
    pass


class TheTwoPathsShareOneRequestTests(AmiigoApi, unittest.TestCase):
    """The app's old session path and the token path ask for the same rider:
    one request between them, whichever path made it."""

    use_api_stores = True

    def setUp(self):
        super().setUp()
        # The API's own store outlives each test: start with nothing pending.
        self.api.stores.conversations.cancel_erasure(RIDER_KEY, NOW.isoformat())

    def old(self, path="", **body):
        return self.client.post("/erasure-requests" + path, json=dict({"session_token": APP_SESSION}, **body))

    def test_a_request_made_one_way_is_the_one_the_other_sees(self):
        reference = self.client.post("/amiigo/v1/erasure-requests", json={"confirm": True},
                                     headers=self.headers(None)).json()["reference"]
        self.assertEqual(self.old("/status").json()["reference"], reference)
        again = self.old(confirm=True)
        self.assertEqual((again.status_code, again.json()["reference"]), (200, reference))
        record = self.api.stores.conversations.erasure_record(reference)
        self.assertEqual((record["channel"], record["proof"]), ("amiigo_app", {"method": "app_sign_in"}))
        self.assertEqual(self.old("/cancel").json()["status"], "cancelled")
        status = self.client.post("/amiigo/v1/erasure-requests/status", headers=self.headers(None))
        self.assertEqual(status.json(), {"reference": None, "status": "none"})

    def test_the_old_answers_keep_their_words_and_are_not_no_store(self):
        missing_confirm = self.old()
        self.assertEqual((missing_confirm.status_code, missing_confirm.json()["detail"]),
                         (400, "Send confirm: true once the rider has confirmed."))
        nothing = self.old("/cancel")
        self.assertEqual((nothing.status_code, nothing.json()["detail"]), (404, erasure.ERASURE_NOTHING_TO_CANCEL))
        with mock.patch.object(self.api.stores.conversations, "pending_erasure_of",
                               side_effect=StoreUnavailable("down")):
            down = self.old(confirm=True)
        self.assertEqual((down.status_code, down.json()["detail"]), (503, erasure.ERASURE_FAILED))
        created = self.old(confirm=True)
        self.assertEqual(created.status_code, 201)
        for r in (missing_confirm, nothing, down, created):
            self.assertNotEqual(r.headers.get("cache-control"), "no-store")
        # The old path's events carry nothing new.
        (requested,) = self.events("erasure_requested")
        self.assertEqual(set(requested) - {"ts", "event", "conversation_id", "reference"}, set(), requested)


if __name__ == "__main__":
    unittest.main()
