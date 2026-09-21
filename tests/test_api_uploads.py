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
