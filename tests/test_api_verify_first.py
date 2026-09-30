"""Verify first through POST /message, as the web chat uses it."""

import base64
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:dev").decode()}


def fresh_api(dev_codes=True):
    env = {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory", "EMOTORAD_OMS_API_KEY": "",
           "EMOTORAD_AI_PLAYGROUND_USER": "dev", "EMOTORAD_AI_PLAYGROUND_PASSWORD": "dev",
           "EMOTORAD_AI_DEV_CODES": "1" if dev_codes else "0", "EMOTORAD_AI_MEDIA_BUCKET": ""}
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
        import emotorad_ai.api as api
        return importlib.reload(api)


class ApiVerifyFirstTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api()
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(dev_codes=False))

    def post(self, text, **extra):
        body = dict({"conversation_id": "c-v", "em_aid": "aid-v", "text": text}, **extra)
        response = self.client.post("/message", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def code(self):
        return self.client.get("/dev/verification/c-v", headers=AUTH).json()["pending_code"]

    def test_an_anonymous_visitor_gives_the_number_the_code_then_sees_the_bikes(self):
        self.assertEqual(self.post("my battery isn't charging")["handled_by"], "verify_first:ask_number")
        with self.assertLogs("emotorad_ai.tools.verification", level="INFO") as logs:
            self.assertEqual(self.post("9700000010")["handled_by"], "verify_first:code_sent")
        code = self.code()
        said = "\n".join(logs.output)
        self.assertIn("•••••••010", said)
        self.assertNotIn(code, said)
        listed = self.post(code)
        self.assertEqual(listed["handled_by"], "verify_first:verified")
        self.assertIn("DDL32023045678", listed["text"])

    def test_the_test_order_number_works_without_the_oms_key(self):
        self.post("hi")
        self.post("I can't remember my number")
        self.assertEqual(self.post("EMO-100234")["handled_by"], "verify_first:order_code_sent")

    def test_a_signed_in_rider_is_not_asked(self):
        reply = self.client.post("/message", json={"conversation_id": "c-s", "session_token": "sess-amiigo-test",
                                                   "text": "my battery isn't charging"}).json()
        self.assertEqual(reply["handled_by"], "triage")
        self.assertIn("EMXP2026001234", reply["text"])


if __name__ == "__main__":
    unittest.main()
