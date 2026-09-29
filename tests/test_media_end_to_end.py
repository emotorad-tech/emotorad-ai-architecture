"""Task 5 of the customer-media-in-s3 plan (spec section 5, "Testing"): the
whole path proved end to end, in one process, with nothing real touched.

Two scenarios, each its own fresh reload of emotorad_ai.api:

1. One conversation: an inline photo, then a complaint. EMOTORAD_STORE=mongodb
   on a mongomock database, and a fake S3 bucket, so the assertions can look
   at exactly what a real deploy would have written: the object in the
   bucket, the permanent `media` record, the transcript, and the ticket.
2. A presigned video upload, claimed and recorded the same way.

The reload pattern follows tests/test_api_uploads.py and
tests/test_api_media_persistence.py: `EMOTORAD_STORE=mongodb` is new here (
those two run on the in-memory store), so `build_stores` is pushed onto
mongomock by patching `emotorad_ai.stores.mongo.connect` (imported inside
`build_stores` at call time, per that function's own docstring) before the
reload, and the model is scripted by patching `emotorad_ai.wiring.build_models`
the same way, so `api.py`'s own `from .wiring import build_models` picks up
the patched version when it re-imports.

Every scripted and authored string below is written to avoid "?", "X-Amz-",
"Signature=" and "signed/" on purpose: the no-signed-value scan at the end of
each scenario reads every document in every collection as JSON text, and a
customer's ordinary "?" would otherwise be a false positive unrelated to the
thing the scan exists to catch (a presigned URL leaking into a permanent
record).
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import io
import json
import os
import unittest
from unittest import mock

import mongomock
from fastapi.testclient import TestClient
from PIL import Image

from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.stores.mongo import ensure_indexes
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET
from emotorad_ai.wiring import Models

BUCKET = "emotorad-ai-stage-media"

# The markers a presigned URL carries, and the host our own fake one uses.
# Checked case-insensitively, the same way media_records._check_not_signed does.
FORBIDDEN_MARKERS = ("x-amz-", "signature=", "signed/", "?")

TICKET = {
    "category": "battery_charging",
    "severity": "normal",
    "description": "Tried the usual steps already; still will not charge.",
    "idempotency_key": "k1",
}


def jpeg_bytes() -> bytes:
    """A real, small, decodable JPEG: attachments.fit_for_model opens it with
    Pillow on the way to the model, so placeholder bytes will not do."""
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (60, 90, 120)).save(out, format="JPEG")
    return out.getvalue()


def jpeg_data_url() -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes()).decode()


class _Store:
    """A fake S3 bucket. Bytes and metadata kept in memory; `presign_put` and
    `presign_get` return obviously fake, obviously signed-looking URLs (host
    "signed", never a real AWS query string) precisely so that one leaking
    into a stored document is not a coincidence the scan could miss."""

    bucket = BUCKET

    def __init__(self) -> None:
        self.objects: dict = {}  # key -> bytes
        self.meta: dict = {}  # key -> {"size": int, "mime": str}
        self.fetched: list = []

    def put_bytes(self, key, data, mime) -> None:
        self.objects[key] = data
        self.meta[key] = {"size": len(data), "mime": mime}

    def get_bytes(self, key):
        self.fetched.append(key)
        return self.objects[key]

    def head(self, key):
        return self.meta.get(key)

    def presign_put(self, key, mime, size):
        return {
            "url": "https://signed/put/" + key,
            "headers": {"Content-Type": mime, "Content-Length": str(size)},
            "expires_in": 300,
        }

    def presign_get(self, key):
        return "https://signed/get/" + key


class _Summariser:
    """Stands in for the real video summariser (GeminiVideoSummariser or the
    OpenRouter one): records what it was asked to describe and answers with a
    fixed sentence, so the video scenario never needs a real, decodable clip
    or a network call, and the model call count stays predictable (see
    tests/test_api_uploads.py's VideoSummaryAtIngestTests, the same idea)."""

    def __init__(self, text: str = "a battery pack on a workbench, charger light off") -> None:
        self.calls: list = []
        self.text = text

    def summarise(self, data, mime, name="video"):
        self.calls.append((data, mime, name))
        return self.text


def fresh_api(store=None, mongo_db=None, scripted=None):
    """Reload emotorad_ai.api with the fakes patched in, following the exact
    pattern of tests/test_api_uploads.py's and
    tests/test_api_media_persistence.py's own `fresh_api`. Called with no
    arguments (as every test here does in addCleanup) it puts the module back
    on the real in-memory store, the offline planner and no bucket -- a clean
    module for whichever test runs next.
    """
    env = {
        "EMOTORAD_AI_MODE": "offline",
        "EMOTORAD_AI_MEDIA_BUCKET": BUCKET if store is not None else "",
        "EMOTORAD_STORE": "mongodb" if mongo_db is not None else "memory",
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store))
        if mongo_db is not None:
            stack.enter_context(mock.patch("emotorad_ai.stores.mongo.connect", return_value=mongo_db))
        if scripted is not None:
            stack.enter_context(mock.patch("emotorad_ai.wiring.build_models", return_value=Models(llm=scripted)))
        import emotorad_ai.api as api

        api = importlib.reload(api)
    return api


def signed_value_scan(db) -> list:
    """Every document in every collection, dumped as JSON text (default=str
    copes with the datetime fields on the working-state and idempotency
    collections) and checked for the marks of a presigned URL. Returns the
    (collection, _id, marker) triples found, empty when the database is clean.
    """
    offending = []
    for name in db.list_collection_names():
        for doc in db[name].find():
            text = json.dumps(doc, default=str).lower()
            for marker in FORBIDDEN_MARKERS:
                if marker in text:
                    offending.append((name, doc.get("_id"), marker))
    return offending


class PhotoThenComplaintEndToEndTests(unittest.TestCase):
    """Scenario 1 (task 5 brief, step 1): one conversation, an inline photo,
    then a complaint, on mongomock plus the fake bucket."""

    def setUp(self):
        self.store = _Store()
        self.mongo = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.mongo)
        self.scripted = ScriptedClaude(
            [
                say(
                    "Thanks for the photo. Try holding the charger button in for ten "
                    "seconds; let me know if the light still does not come on."
                ),
                call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"),
                say(
                    "I have raised a complaint for you. Your reference is EM-00001; "
                    "the team will be in touch shortly."
                ),
            ]
        )
        self.api = fresh_api(self.store, self.mongo, self.scripted)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api())

    def test_photo_then_complaint_persists_the_object_the_record_and_the_ticket(self):
        r1 = self.client.post(
            "/message",
            json={
                "conversation_id": "c1",
                "session_token": "sess-ananya",
                "agent": "battery_support",
                "text": "my battery won't charge, here is the charger",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}],
            },
        )
        self.assertEqual(r1.status_code, 200, r1.text)

        # Exactly one object landed in the fake bucket, under this
        # conversation's own customer key.
        self.assertEqual(len(self.store.objects), 1, self.store.objects)
        [key] = list(self.store.objects.keys())
        self.assertTrue(key.startswith("customers/"), key)
        self.assertIn("/c1/images/", key)
        self.assertTrue(key.endswith(".jpg"), key)

        # Exactly one media document, and it has the right shape.
        media_docs = list(self.mongo["media"].find())
        self.assertEqual(len(media_docs), 1, media_docs)
        [record] = media_docs
        self.assertEqual(record["_id"], key)
        self.assertEqual(record["bucket"], BUCKET)
        self.assertEqual(record["key"], key)
        self.assertEqual(record["uri"], "s3://%s/%s" % (BUCKET, key))
        self.assertEqual(record["kind"], "image")
        self.assertEqual(record["source"], "inline")
        self.assertEqual(record["conversation_id"], "c1")

        # The model was actually shown the photo: the first request's
        # customer turn carries an image block, not text alone.
        first_request = self.scripted.requests[0]
        customer_content = first_request["messages"][-1]["content"]
        self.assertIsInstance(customer_content, list, customer_content)
        self.assertTrue(any(block.get("type") == "image" for block in customer_content), customer_content)

        r2 = self.client.post(
            "/message",
            json={
                "conversation_id": "c1",
                "session_token": "sess-ananya",
                "agent": "battery_support",
                "text": "still dead, please raise a complaint",
            },
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["ticket_id"], "EM-00001")

        # The evidence rule was satisfied, not bypassed: the ticket tool ran
        # (rather than being refused with evidence_required), which only
        # happens once the tool call above actually reached the registry.
        self.assertEqual(list(self.api.registry.tickets.tickets.keys()), ["EM-00001"])

        # A transcript_turns document names the object's key -- never the
        # data: URL the customer actually sent, never a presigned link.
        turns = list(self.mongo["transcript_turns"].find({"conversation_id": "c1"}))
        self.assertTrue(turns, "no transcript turns were recorded for c1")
        named_urls = [
            attachment["url"]
            for turn in turns
            for attachment in (turn.get("attachments") or [])
            if attachment.get("url")
        ]
        self.assertIn("s3://" + key, named_urls)

        # Nowhere in the database does a presigned value or a query string
        # appear in what was kept.
        offending = signed_value_scan(self.mongo)
        self.assertEqual(offending, [], offending)


class PresignedVideoUploadEndToEndTests(unittest.TestCase):
    """Scenario 2 (task 5 brief, step 2): a presigned video upload, simulated
    and claimed, recorded the same way the inline photo was."""

    def setUp(self):
        self.store = _Store()
        self.mongo = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.mongo)
        self.scripted = ScriptedClaude(
            [say("Thanks, I can see the clip; let me know if the fan spins up once it is switched on.")]
        )
        self.api = fresh_api(self.store, self.mongo, self.scripted)
        self.api.VIDEO_SUMMARISER = _Summariser()
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api())

    def test_a_claimed_video_upload_is_recorded(self):
        video_bytes = b"not a real mp4, just some bytes to head() and store"

        presign = self.client.post(
            "/uploads",
            json={
                "session_token": "sess-ananya",
                "conversation_id": "c2",
                "tree": "customers",
                "mime_type": "video/mp4",
                "size_bytes": len(video_bytes),
            },
        )
        self.assertEqual(presign.status_code, 200, presign.text)
        body = presign.json()
        key = body["key"]
        self.assertTrue(key.startswith("customers/"), key)
        self.assertIn("/c2/videos/", key)

        # Simulate the browser's PUT straight to the bucket: the server never
        # sees the bytes at this point, only the object's presence and shape.
        self.store.objects[key] = video_bytes
        self.store.meta[key] = {"size": len(video_bytes), "mime": "video/mp4"}

        r = self.client.post(
            "/message",
            json={
                "conversation_id": "c2",
                "session_token": "sess-ananya",
                "agent": "battery_support",
                "text": "here is a clip of it",
                "attachments": [{"upload_id": body["upload_id"]}],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)

        media_docs = list(self.mongo["media"].find())
        self.assertEqual(len(media_docs), 1, media_docs)
        [record] = media_docs
        self.assertEqual(record["key"], key)
        self.assertEqual(record["uri"], "s3://%s/%s" % (BUCKET, key))
        self.assertEqual(record["kind"], "video")
        self.assertEqual(record["mime_type"], "video/mp4")
        self.assertEqual(record["source"], "upload")
        self.assertEqual(record["conversation_id"], "c2")

        # The summariser stood in for the real one and was actually asked --
        # proof the ingest path read the object back rather than skipping it.
        self.assertEqual(len(self.api.VIDEO_SUMMARISER.calls), 1, self.api.VIDEO_SUMMARISER.calls)

        offending = signed_value_scan(self.mongo)
        self.assertEqual(offending, [], offending)


if __name__ == "__main__":
    unittest.main()
