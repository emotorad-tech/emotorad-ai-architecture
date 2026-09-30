"""The end-to-end test console (/dev/e2e) and the two dev routes it reads.

Internal only, like /dev/verification: behind the playground login, and 404
unless EMOTORAD_AI_DEV_CODES=1. /dev/media/{conversation} shows the permanent
media records for one conversation, so a tester can see the address a photo
was stored at without a database client.
"""

import base64
import importlib
import io
import os
import pathlib
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "web" / "e2e-console.html"
AUTH = "Basic " + base64.b64encode(b"dev:dev").decode()


class _Store:
    bucket = "emotorad-ai-stage-media"

    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = (data, mime)

    def get_bytes(self, key):
        return self.objects[key][0]

    def head(self, key):
        return None

    def presign_put(self, key, mime, size):
        return {"url": "https://signed/put/" + key, "headers": {}, "expires_in": 300}

    def presign_get(self, key):
        return "https://signed/get/" + key


def fresh_api(dev_codes, store=None):
    env = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory",
           "EMOTORAD_AI_PLAYGROUND_USER": "dev", "EMOTORAD_AI_PLAYGROUND_PASSWORD": "dev",
           "EMOTORAD_AI_DEV_CODES": "1" if dev_codes else "0",
           "EMOTORAD_AI_MEDIA_BUCKET": "fake" if store else ""}
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store):
        import emotorad_ai.api as api
        return importlib.reload(api)


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (60, 60, 60)).save(out, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


class RouteTests(unittest.TestCase):
    def tearDown(self):
        fresh_api(dev_codes=False)

    def test_the_console_needs_the_login(self):
        client = TestClient(fresh_api(dev_codes=True).app)
        self.assertEqual(client.get("/dev/e2e").status_code, 401)
        self.assertEqual(client.get("/dev/media/c1").status_code, 401)
        self.assertEqual(client.get("/dev/e2e/sample.jpg").status_code, 401)

    def test_without_dev_codes_it_is_not_there(self):
        client = TestClient(fresh_api(dev_codes=False).app)
        for path in ("/dev/e2e", "/dev/media/c1", "/dev/e2e/sample.jpg"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path, headers={"Authorization": AUTH}).status_code, 404)

    def test_with_both_it_serves_the_console_and_its_sample_photo(self):
        client = TestClient(fresh_api(dev_codes=True).app)
        page = client.get("/dev/e2e", headers={"Authorization": AUTH})
        self.assertEqual(page.status_code, 200)
        self.assertIn("end-to-end test console", page.text)
        photo = client.get("/dev/e2e/sample.jpg", headers={"Authorization": AUTH})
        self.assertEqual((photo.status_code, photo.headers["content-type"]), (200, "image/jpeg"))

    def test_the_media_route_shows_a_stored_photos_record(self):
        store = _Store()
        api = fresh_api(dev_codes=True, store=store)
        client = TestClient(api.app)
        sent = client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                             "text": "here is the charger", "attachments": [{"kind": "image", "url": jpeg()}]})
        self.assertEqual(sent.status_code, 200)
        shown = client.get("/dev/media/c1", headers={"Authorization": AUTH}).json()
        self.assertEqual(shown["conversation_id"], "c1")
        [record] = shown["media"]
        self.assertTrue(record["uri"].startswith("s3://emotorad-ai-stage-media/customers/"))
        self.assertEqual(record["source"], "inline")


class SavedResultsTests(unittest.TestCase):
    """A run is saved on the server (logs/e2e/), so it survives the browser tab
    and scripts/e2e_report.py can turn it into a report."""

    def setUp(self):
        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api = fresh_api(dev_codes=True)
        self.api.E2E_RESULTS_DIR = pathlib.Path(self.dir.name)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(dev_codes=False))

    def test_a_run_is_saved_and_named_by_its_time(self):
        run = {"at": "2026-09-29T16:56:05Z", "results": [{"id": "smoke-typed", "status": "finding", "steps": []}]}
        saved = self.client.post("/dev/e2e/results", json=run, headers={"Authorization": AUTH})
        self.assertEqual(saved.status_code, 200)
        [path] = list(pathlib.Path(self.dir.name).glob("*.json"))
        self.assertEqual(saved.json()["saved"], "logs/e2e/" + path.name)
        import json

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), run)

    def test_it_needs_the_login_and_dev_codes(self):
        self.assertEqual(self.client.post("/dev/e2e/results", json={"results": []}).status_code, 401)
        off = TestClient(fresh_api(dev_codes=False).app)
        self.assertEqual(off.post("/dev/e2e/results", json={"results": []}, headers={"Authorization": AUTH}).status_code, 404)

    def test_a_run_without_results_or_too_large_is_refused(self):
        self.assertEqual(self.client.post("/dev/e2e/results", json={"at": "x"}, headers={"Authorization": AUTH}).status_code, 400)
        big = {"results": [{"text": "x" * (2 * 1024 * 1024 + 1)}]}
        self.assertEqual(self.client.post("/dev/e2e/results", json=big, headers={"Authorization": AUTH}).status_code, 413)
        self.assertEqual(list(pathlib.Path(self.dir.name).glob("*.json")), [])


class ConsolePageTests(unittest.TestCase):
    def setUp(self):
        self.html = CONSOLE.read_text(encoding="utf-8")

    def test_it_sends_what_the_chat_page_sends(self):
        self.assertIn('fetch("/message"', self.html)
        self.assertIn("1280", self.html)  # the same downscale
        self.assertIn('toDataURL("image/jpeg", 0.8)', self.html)
        self.assertNotRegex(self.html, r"agent:\s*\"")  # routes as staging does, no pin

    def test_it_checks_the_permanent_record_and_the_one_step_limits(self):
        self.assertIn("/dev/media/", self.html)
        self.assertIn("x-amz-", self.html)
        self.assertIn("m.words <= 80 && m.sentences <= 4", self.html)

    def test_it_runs_the_smoke_scenarios(self):
        for scenario in ("smoke-neutral-words", "smoke-typed", "smoke-hinglish", "smoke-photo-only"):
            self.assertIn('"%s"' % scenario, self.html)

    def test_it_runs_the_verify_first_scenarios(self):
        for scenario in ("verify-then-warranty", "verify-by-order-number"):
            self.assertIn('"%s"' % scenario, self.html)
        self.assertIn("verify_first:order_code_sent", self.html)


if __name__ == "__main__":
    unittest.main()
