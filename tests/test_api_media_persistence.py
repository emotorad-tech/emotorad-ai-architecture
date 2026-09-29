"""Task 2 of the customer-media-in-s3 plan (spec §1, §2, §3): the server
stores an inline photo in S3 itself when a bucket is configured, and every
object that becomes part of a conversation — an inline photo just stored, or
an upload just claimed — gets a permanent record in the conversation store.

Through FastAPI's TestClient, the way tests/test_api_uploads.py does it: a
fake S3 store, no AWS, no Mongo (EMOTORAD_STORE is unset, so the in-memory
conversation store is what `api.stores.conversations` ends up being).
"""

import base64
import importlib
import io
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.contract import Reply
from emotorad_ai.storage.s3 import StorageError


def jpeg_data_url() -> str:
    """A real, small, decodable JPEG — not the placeholder bytes other tests
    use, because this path's own code (fit_for_model) opens it with Pillow."""
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (60, 90, 120)).save(out, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


class _Store:
    """Keeps what it is given, in memory, exactly like `_Store` in
    test_api_uploads.py, plus a `put_bytes` that either stores the object or
    raises `StorageError`, on demand."""

    bucket = "fake-media"

    def __init__(self, fail_put=False):
        self.objects = {}  # key -> bytes
        self.meta = {}  # key -> {"size":.., "mime":..}
        self.fetched = []
        self.fail_put = fail_put

    def put_bytes(self, key, data, mime):
        if self.fail_put:
            raise StorageError("put %r failed: boom" % key)
        self.objects[key] = data
        self.meta[key] = {"size": len(data), "mime": mime}

    def get_bytes(self, key):
        self.fetched.append(key)
        return self.objects[key]

    def head(self, key):
        return self.meta.get(key)

    def presign_put(self, key, mime, size):
        return {"url": "https://signed/put/" + key, "headers": {"Content-Type": mime, "Content-Length": str(size)}, "expires_in": 300}

    def presign_get(self, key):
        return "https://signed/get/" + key


def fresh_api(store):
    with mock.patch.dict(os.environ, {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_MEDIA_BUCKET": "fake-media" if store else ""}):
        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store):
            import emotorad_ai.api as api

            api = importlib.reload(api)
    return api


class InlinePhotoStoredTests(unittest.TestCase):
    """The photo lands in the bucket, its record lands in the conversation
    store, and the turn the model was shown carries the s3:// reference."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_an_inline_photo_is_put_under_the_callers_cluster_and_recorded(self):
        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
            "pill": "battery_issue",
            "attachments": [{"kind": "image", "url": jpeg_data_url()}],
        })
        self.assertEqual(r.status_code, 200, r.text)

        # Exactly one object landed in the bucket, under the caller's cluster.
        [key] = list(self.store.objects.keys())
        self.assertTrue(key.startswith("customers/"), key)
        self.assertIn("/c1/images/", key)
        self.assertTrue(key.endswith(".jpg"), key)
        self.assertNotIn("+91", key)

        # Its permanent record: bucket, key, s3:// uri, kind, source, size.
        [record] = self.api.stores.conversations.media_of("c1")
        self.assertEqual(record["key"], key)
        self.assertEqual(record["bucket"], "fake-media")
        self.assertEqual(record["uri"], "s3://fake-media/" + key)
        self.assertEqual(record["kind"], "image")
        self.assertEqual(record["mime_type"], "image/jpeg")
        self.assertEqual(record["size_bytes"], len(self.store.objects[key]))
        self.assertEqual(record["source"], "inline")
        self.assertEqual(record["conversation_id"], "c1")

    def test_the_model_was_shown_the_stored_photo(self):
        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
            "pill": "battery_issue",
            "attachments": [{"kind": "image", "url": jpeg_data_url()}],
        })
        self.assertEqual(r.status_code, 200, r.text)

        state = self.api.stores.conversations.peek("c1")
        self.assertTrue(state.evidence_seen)
        customer_turns = [e for e in state.history if e["role"] == "user"]
        self.assertTrue(customer_turns, state.history)
        blocks = customer_turns[0]["content"]
        self.assertIsInstance(blocks, list)
        self.assertTrue(any(b.get("type") == "image" for b in blocks), blocks)

    def test_the_reply_carries_the_s3_reference_not_the_data_url(self):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={
                "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        [key] = list(self.store.objects.keys())
        self.assertEqual(seen[-1].attachments[0].url, "s3://" + key)
        self.assertEqual(seen[-1].attachments[0].mime_type, "image/jpeg")


class ClaimedVideoRecordedTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_a_claimed_video_upload_is_recorded_with_source_upload(self):
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": "video/mp4", "size_bytes": 12345,
        }).json()
        self.store.objects[body["key"]] = b"x" * 12345
        self.store.meta[body["key"]] = {"size": 12345, "mime": "video/mp4"}

        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "video attached",
            "attachments": [{"upload_id": body["upload_id"]}],
        })
        self.assertEqual(r.status_code, 200, r.text)

        [record] = self.api.stores.conversations.media_of("c1")
        self.assertEqual(record["key"], body["key"])
        self.assertEqual(record["kind"], "video")
        self.assertEqual(record["mime_type"], "video/mp4")
        self.assertEqual(record["size_bytes"], 12345)
        self.assertEqual(record["source"], "upload")


class NoMediaStoreTests(unittest.TestCase):
    """With no bucket configured, an inline photo behaves exactly as it did
    before this task: inline, nothing recorded, nothing logged."""

    def setUp(self):
        self.api = fresh_api(None)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_the_photo_stays_inline_and_nothing_is_recorded(self):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={
                "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen[-1].attachments[0].url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.api.stores.conversations.media_of("c1"), [])
        self.assertEqual([e for e in self.api.log.events if e["event"].startswith("media_")], [])


class StorePutFailureTests(unittest.TestCase):
    """`put_bytes` failing must not stop the reply, must keep the photo
    inline, and must never write a record for an object that never landed."""

    def setUp(self):
        self.store = _Store(fail_put=True)
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_a_storage_failure_keeps_the_photo_inline_and_logs_media_not_stored(self):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={
                "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen[-1].attachments[0].url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.api.stores.conversations.media_of("c1"), [])

        [event] = [e for e in self.api.log.events if e["event"] == "media_not_stored"]
        self.assertEqual(event["reason"], "store_failed")
        self.assertEqual(event["error"], "StorageError")
        self.assertNotIn("data:", str(event))
        # The key's last segment only, as every other media log line: the
        # full key names the customer's cluster and conversation.
        self.assertNotIn("/", event["key"])
        self.assertTrue(event["key"].endswith(".jpg"), event["key"])
        self.assertNotIn("customers/", str(event))


class MalformedConversationIdTests(unittest.TestCase):
    """`conversation_id` is client-supplied; the chat page always echoes back
    the UUID the server minted, but nothing on the wire enforces that. A
    value outside the key grammar must degrade the same way a refused S3
    write does — never a 500 for a photo that simply is not stored."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_a_key_validation_failure_keeps_the_photo_inline_and_logs_media_not_stored(self):
        seen = []
        scripted = Reply(conversation_id="c/1", text="ok", handled_by="test")
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={
                "conversation_id": "c/1", "session_token": "sess-ananya", "text": "here is the terminal",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen[-1].attachments[0].url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.api.stores.conversations.media_of("c/1"), [])

        [event] = [e for e in self.api.log.events if e["event"] == "media_not_stored"]
        # Its own reason: nothing was sent to S3, so "store_failed" would send
        # the person to check the bucket and credentials for nothing.
        self.assertEqual(event["reason"], "bad_key")
        self.assertEqual(event["error"], "KeyValidationError")


class RecordFailureTests(unittest.TestCase):
    """The object is safely in the bucket; only the database row fails. The
    turn still carries the s3:// reference — the object really is there."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_a_record_media_failure_logs_media_record_failed_but_the_item_becomes_s3(self):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api.stores.conversations, "record_media", side_effect=StoreUnavailable("mongo down")):
            with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
                r = self.client.post("/message", json={
                    "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
                    "attachments": [{"kind": "image", "url": jpeg_data_url()}],
                })
        self.assertEqual(r.status_code, 200, r.text)

        [key] = list(self.store.objects.keys())
        self.assertEqual(seen[-1].attachments[0].url, "s3://" + key)

        [event] = [e for e in self.api.log.events if e["event"] == "media_record_failed"]
        self.assertEqual(event["key"], key)
        self.assertEqual(event["error"], "StoreUnavailable")


class UnknownAgentTests(unittest.TestCase):
    """A request refused for an unknown agent is refused before anything is
    stored: otherwise the photo is put and recorded for a conversation that
    never exists, and a claimed upload id is spent."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_an_unknown_agent_with_an_inline_photo_stores_and_records_nothing(self):
        with mock.patch.object(self.api.stores.conversations, "record_media") as record:
            r = self.client.post("/message", json={
                "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
                "agent": "no_such_agent",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Unknown agent", r.text)
        self.assertEqual(self.store.objects, {})
        record.assert_not_called()
        self.assertEqual([e for e in self.api.log.events if e["event"].startswith("media_")], [])

    def test_an_unknown_agent_leaves_an_upload_unclaimed(self):
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": "video/mp4", "size_bytes": 12345,
        }).json()
        self.store.objects[body["key"]] = b"x" * 12345
        self.store.meta[body["key"]] = {"size": 12345, "mime": "video/mp4"}

        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "video attached",
            "agent": "no_such_agent", "attachments": [{"upload_id": body["upload_id"]}],
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNotNone(self.api.UPLOADS.peek(body["upload_id"]))
        self.assertEqual(self.api.stores.conversations.media_of("c1"), [])


class NoClusterTests(unittest.TestCase):
    """No session and no cookie: the caller does not resolve to a cluster,
    there is nowhere to derive a customer key from, so the photo stays
    inline and the reason is on the log line, not a 400 to the customer."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def test_no_session_and_no_cookie_keeps_the_photo_inline(self):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={
                "conversation_id": "c1", "text": "here is the terminal",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen[-1].attachments[0].url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.api.stores.conversations.media_of("c1"), [])

        [event] = [e for e in self.api.log.events if e["event"] == "media_not_stored"]
        self.assertEqual(event["reason"], "no_cluster")


if __name__ == "__main__":
    unittest.main()
