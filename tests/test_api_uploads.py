"""The upload handshake over HTTP: presign, then attach, plus the read redirect.
The store is faked; no AWS is touched."""

import base64
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.contract import Reply


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

    def test_a_presign_flood_is_refused_with_429(self):
        """/message was rate limited and /uploads was not, so an anonymous
        caller could mint presigned PUTs (and pending entries) without bound."""
        from emotorad_ai.ratelimit import RateLimiter

        self.api.upload_limiter = RateLimiter(limit=2, window_seconds=60.0)
        codes = [
            self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).status_code
            for _ in range(4)
        ]
        self.assertEqual(codes[:2], [200, 200], codes)
        self.assertEqual(codes[-1], 429, codes)

    def test_an_iphone_clip_presigns(self):
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "video/quicktime", "size_bytes": 9})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["key"].endswith(".mov"), body["key"])
        self.assertEqual(body["headers"]["Content-Type"], "video/quicktime")

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


class BothAttachmentShapesTests(unittest.TestCase):
    """One `attachments` field, two wire shapes.

    The chat page sends an inline `data:` photo; the presigned route sends an
    upload id. Integrating the two branches put both on the same field, so a
    message may carry either or both, and the count limit has to be on the
    total — otherwise adding the second route quietly doubles how many pictures
    a caller can send.
    """

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)

    def _presigned(self):
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": "image/png", "size_bytes": 9,
        }).json()
        self.store.objects[body["key"]] = {"size": 9, "mime": "image/png"}
        return body

    def test_an_inline_photo_still_reaches_the_model(self):
        """The path the chat page uses today. It must not need media configured
        and it must not go anywhere near S3."""
        inline = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake").decode()
        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "here is the terminal",
            "pill": "battery_issue",
            "attachments": [{"kind": "image", "url": inline}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.fetched, [])

    def test_inline_and_presigned_mix_in_one_message(self):
        presigned = self._presigned()
        inline = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake").decode()
        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "both ends",
            "pill": "battery_issue",
            "attachments": [{"kind": "image", "url": inline}, {"upload_id": presigned["upload_id"]}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.fetched, [presigned["key"]])

    def test_the_count_limit_is_on_the_total_not_on_each_path(self):
        from emotorad_ai.attachments import MAX_ATTACHMENTS

        presigned = self._presigned()
        inline = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake").decode()
        items = [{"kind": "image", "url": inline}] * MAX_ATTACHMENTS
        items.append({"upload_id": presigned["upload_id"]})
        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "x", "attachments": items,
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Too many attachments", r.json()["detail"])

    def test_a_refused_message_does_not_consume_the_upload_id(self):
        """The cap is checked before anything is claimed, so a retry under the
        limit still finds the id the customer already uploaded."""
        presigned = self._presigned()
        inline = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake").decode()
        items = [{"kind": "image", "url": inline}] * 5 + [{"upload_id": presigned["upload_id"]}]
        self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "x", "attachments": items,
        })
        r = self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "x",
            "attachments": [{"upload_id": presigned["upload_id"]}],
        })
        self.assertEqual(r.status_code, 200, r.text)


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


class PlaygroundProxyMethodsTests(unittest.TestCase):
    """The playground reverse proxy's route table.

    Streamlit's file uploader (1.64) sends the file with PUT and removes it
    with DELETE. A proxy route that only lists GET/POST/HEAD answers those
    with 405 before Streamlit ever sees the request, which silently breaks
    every file upload in the playground. This asserts on the route table
    itself, not a live upstream call, so a future tidy-up of the method list
    cannot narrow it again without a test going red.
    """

    def test_playground_routes_accept_put_and_delete(self):
        api = fresh_api(None)
        wanted = {"GET", "POST", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"}
        checked = 0
        for route in api.app.routes:
            # The websocket route shares the /playground/{rest:path} path but
            # has no `methods` (it isn't an HTTP verb route) — skip it.
            if getattr(route, "path", None) in ("/playground", "/playground/{rest:path}") and hasattr(
                route, "methods"
            ):
                self.assertLessEqual(wanted, set(route.methods), route.path)
                checked += 1
        self.assertEqual(checked, 2)


class _Summariser:
    """Stands in for GeminiVideoSummariser: records what it was asked to
    describe, answers with a fixed sentence or raises."""

    def __init__(self, text="the pack is on a table", error=None):
        self.calls = []
        self.text = text
        self.error = error

    def summarise(self, data, mime, name="video"):
        self.calls.append((data, mime, name))
        if self.error is not None:
            raise self.error
        return self.text


class VideoSummaryAtIngestTests(unittest.TestCase):
    """A claimed video is described once, here, before the runtime sees it.
    The summary rides on the attachment so the safety gate and the agent turn
    read it; a summariser failure leaves it absent and the frames path runs."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.summariser = _Summariser()
        self.api.VIDEO_SUMMARISER = self.summariser
        self.seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        self._patch = mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: self.seen.append(m) or scripted)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _upload(self, mime="video/mp4", size=9):
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": mime, "size_bytes": size,
        }).json()
        self.store.objects[body["key"]] = {"size": size, "mime": mime}
        return body

    def _send(self, upload_id):
        return self.client.post("/message", json={
            "conversation_id": "c1", "session_token": "sess-ananya", "text": "video attached",
            "attachments": [{"upload_id": upload_id}],
        })

    def test_a_claimed_mp4_is_summarised_once_and_the_runtime_sees_the_text(self):
        body = self._upload()
        r = self._send(body["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.summariser.calls, [(b"\x89PNG fake", "video/mp4", body["key"].rsplit("/", 1)[-1])])
        self.assertEqual(self.store.fetched, [body["key"]])
        message = self.seen[-1]
        self.assertEqual(message.attachments[0].kind, "video")
        self.assertEqual(message.attachments[0].summary, "the pack is on a table")

    def test_a_summariser_failure_leaves_the_summary_absent(self):
        from emotorad_ai.video_summary import VideoSummaryError

        self.summariser.error = VideoSummaryError("DeadlineExceeded")
        body = self._upload()
        with self.assertLogs("emotorad_ai.api", level="WARNING") as logs:
            r = self._send(body["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.seen[-1].attachments[0].summary)
        self.assertTrue(any("DeadlineExceeded" in line for line in logs.output), logs.output)

    def test_a_store_failure_leaves_the_summary_absent(self):
        from emotorad_ai.storage.s3 import StorageError

        def boom(key):
            raise StorageError("s3 said no")

        self.store.get_bytes = boom
        body = self._upload()
        with self.assertLogs("emotorad_ai.api", level="WARNING"):
            r = self._send(body["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.summariser.calls, [])
        self.assertIsNone(self.seen[-1].attachments[0].summary)

    def test_an_image_upload_is_not_summarised(self):
        body = self._upload(mime="image/png")
        r = self._send(body["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.summariser.calls, [])
        self.assertIsNone(self.seen[-1].attachments[0].summary)

    def test_health_reports_which_video_path_is_live(self):
        self.assertEqual(self.client.get("/health").json()["video_summary"], "gemini")
        self.api.VIDEO_SUMMARISER = None
        self.assertEqual(self.client.get("/health").json()["video_summary"], "frames")


class AnonymousVisitorUploadTests(unittest.TestCase):
    """A visitor who has not verified a phone still has an identity: the
    website cookie (`em_aid`) resolves to an anonymous identity-graph cluster,
    exactly as it does on /message. Without this the presign 400s for every
    customer who tries to send a video before the OTP step."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)

    def _presign(self, **overrides):
        body = {"em_aid": "visitor-abc", "conversation_id": "c-anon", "tree": "customers",
                "mime_type": "video/mp4", "size_bytes": 5 * 1024 * 1024}
        body.update(overrides)
        return self.client.post("/uploads", json=body)

    def test_a_cookie_alone_presigns_under_the_visitors_cluster(self):
        r = self._presign()
        self.assertEqual(r.status_code, 200, r.text)
        key = r.json()["key"]
        self.assertTrue(key.startswith("customers/"), key)
        cluster = key.split("/")[1]
        self.assertNotEqual(cluster, "visitor-abc")
        self.assertNotIn("+91", key)
        self.assertFalse(cluster.isdigit(), cluster)
        self.assertEqual(r.json()["headers"]["Content-Type"], "video/mp4")

    def test_the_same_cookie_can_attach_it_on_message(self):
        body = self._presign().json()
        self.store.objects[body["key"]] = {"size": 5 * 1024 * 1024, "mime": "video/mp4"}
        seen = []
        scripted = Reply(conversation_id="c-anon", text="ok", handled_by="test")
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={
                "conversation_id": "c-anon", "em_aid": "visitor-abc", "text": "here is the clip",
                "attachments": [{"upload_id": body["upload_id"]}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        # Claimed under the cookie's cluster and handed on as the stored object.
        self.assertEqual(seen[-1].attachments[0].kind, "video")
        self.assertEqual(seen[-1].attachments[0].url, "s3://" + body["key"])

    def test_a_different_cookie_is_refused(self):
        body = self._presign().json()
        self.store.objects[body["key"]] = {"size": 5 * 1024 * 1024, "mime": "video/mp4"}
        r = self.client.post("/message", json={
            "conversation_id": "c-anon", "em_aid": "visitor-xyz", "text": "mine?",
            "attachments": [{"upload_id": body["upload_id"]}],
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.store.fetched, [])

    def test_a_different_cookie_cannot_read_it_back(self):
        body = self._presign().json()
        r = self.client.get("/media/" + body["key"], params={"em_aid": "visitor-abc"}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        r = self.client.get("/media/" + body["key"], params={"em_aid": "visitor-xyz"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)

    def test_no_session_and_no_cookie_is_400(self):
        r = self._presign(em_aid=None)
        self.assertEqual(r.status_code, 400, r.text)


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
