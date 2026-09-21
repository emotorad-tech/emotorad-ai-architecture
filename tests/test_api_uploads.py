"""The upload handshake over HTTP: presign, then attach, plus the read redirect.
The store is faked; no AWS is touched."""

import base64
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class _Store:
    bucket = "fake"

    def __init__(self):
        self.objects = {}
        self.fetched = []

    def presign_put(self, key, mime, size):
        return {"url": "https://signed/put/" + key, "headers": {"Content-Type": mime, "Content-Length": str(size)}, "expires_in": 300}

    def presign_get(self, key):
        return "https://signed/get/" + key

    def head(self, key):
        return self.objects.get(key)

    def get_bytes(self, key):
        self.fetched.append(key)
        return b"\x89PNG fake"


def fresh_api(store):
    with mock.patch.dict(os.environ, {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_MEDIA_BUCKET": "fake" if store else ""}):
        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store):
            import emotorad_ai.api as api

            api = importlib.reload(api)
    return api


AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:dev").decode()}


class UploadFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_PLAYGROUND_USER": "dev", "EMOTORAD_AI_PLAYGROUND_PASSWORD": "dev"}):
            self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)

    def test_health_reports_media(self):
        self.assertEqual(self.client.get("/health").json()["media"], "configured")

    def test_customer_presign_then_attach(self):
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["key"].startswith("customers/"))
        self.assertNotIn("+91", body["key"])
        self.assertEqual(body["headers"]["Content-Type"], "image/png")
        self.store.objects[body["key"]] = {"size": 9, "mime": "image/png"}
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "what is this", "pill": "battery_issue", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.fetched, [body["key"]])

    def test_attaching_before_the_put_completes_is_409(self):
        body = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).json()
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "x", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 409)

    def test_unsupported_type_and_over_cap(self):
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/gif", "size_bytes": 9})
        self.assertEqual(r.status_code, 415)
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 11 * 1024 * 1024})
        self.assertEqual(r.status_code, 413)

    def test_asset_presign_needs_playground_auth(self):
        body = {"tree": "assets", "mime_type": "image/png", "size_bytes": 9, "path": {"programme": "afs", "category": "battery", "kind": "photos", "slug": "soc-button"}}
        self.assertEqual(self.client.post("/uploads", json=body).status_code, 401)
        r = self.client.post("/uploads", json=body, headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["key"], "assets/afs/battery/photos/soc-button.png")

    def test_media_redirects_to_a_presigned_get_for_the_owner_only(self):
        body = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).json()
        r = self.client.get("/media/" + body["key"], params={"session_token": "sess-ananya"}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "https://signed/get/" + body["key"])
        r = self.client.get("/media/" + body["key"], params={"session_token": "sess-rohit"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/media/assets/afs/battery/photos/x.jpg", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/media/deploy/app.tar.gz", follow_redirects=False).status_code, 404)

    def test_media_rejects_a_key_that_only_matches_by_prefix(self):
        r = self.client.get("/media/assets/%2e%2e/customers/clu_1/c1/images/u.jpg", follow_redirects=False)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/media/assets%2f..%2fcustomers/x/y/images/z.jpg", follow_redirects=False)
        self.assertEqual(r.status_code, 404)


    def test_message_refuses_another_sessions_upload(self):
        body = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).json()
        self.store.objects[body["key"]] = {"size": 9, "mime": "image/png"}
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-rohit", "text": "x", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.store.fetched, [])
        # The wrong-session 403 must not have consumed the upload id: the
        # rightful owner can still attach the same upload afterwards.
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "what is this", "pill": "battery_issue", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.fetched, [body["key"]])

    def test_presign_refuses_a_conversation_started_by_another_session(self):
        r = self.client.post("/message", json={"conversation_id": "c-a", "session_token": "sess-ananya", "text": "hi"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/uploads", json={"session_token": "sess-rohit", "conversation_id": "c-a", "tree": "customers", "mime_type": "image/png", "size_bytes": 9})
        self.assertEqual(r.status_code, 403)

    def test_message_with_an_asset_upload_id_is_403_not_500(self):
        asset_body = {"tree": "assets", "mime_type": "image/png", "size_bytes": 9, "path": {"programme": "afs", "category": "battery", "kind": "photos", "slug": "soc-button"}}
        body = self.client.post("/uploads", json=asset_body, headers=AUTH).json()
        self.store.objects[body["key"]] = {"size": 9, "mime": "image/png"}
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "x", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.store.fetched, [])


class ReplyAttachmentsOnMessageTests(unittest.TestCase):
    """The reply's attachments (guide photos/clips) must reach /message's JSON,
    caption and poster included, so a web chat can render them."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)

    def test_reply_attachments_carry_kind_url_mime_caption_poster(self):
        from emotorad_ai.contract import Attachment, Reply

        scripted_reply = Reply(
            conversation_id="c1",
            text="Here you go.",
            handled_by="battery_agent",
            attachments=[
                Attachment(
                    kind="image", url="https://signed.test/soc.png", mime_type="image/png",
                    caption="The SOC button", poster=None,
                ),
                Attachment(
                    kind="video", url="https://signed.test/onoff.mp4", mime_type="video/mp4",
                    caption=None, poster="https://signed.test/onoff.jpg",
                ),
            ],
        )
        with mock.patch.object(self.api.runtime, "handle", return_value=scripted_reply):
            r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "how do I turn it off"})
        self.assertEqual(r.status_code, 200, r.text)
        attachments = r.json()["attachments"]
        self.assertEqual(len(attachments), 2)
        self.assertEqual(
            attachments[0],
            {"kind": "image", "url": "https://signed.test/soc.png", "mime_type": "image/png", "caption": "The SOC button", "poster": None},
        )
        self.assertEqual(
            attachments[1],
            {"kind": "video", "url": "https://signed.test/onoff.mp4", "mime_type": "video/mp4", "caption": None, "poster": "https://signed.test/onoff.jpg"},
        )


class NoBucketTests(unittest.TestCase):
    def test_uploads_and_media_are_503_without_a_bucket(self):
        api = fresh_api(None)
        client = TestClient(api.app)
        self.assertEqual(client.get("/health").json()["media"], "not configured")
        self.assertEqual(client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).status_code, 503)
        self.assertEqual(client.get("/media/assets/x/y/photos/z.jpg", follow_redirects=False).status_code, 503)

    @classmethod
    def tearDownClass(cls):
        fresh_api(None)


if __name__ == "__main__":
    unittest.main()
