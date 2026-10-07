"""The Amiigo chat tester's relay (scripts/amiigo_local.py), offline.

The relay holds the tester's pasted token and talks to the chat server the
way the app does: the token in `Authorization: Bearer` on the socket's
handshake and on every /amiigo/v1 call. These tests check it does exactly
that, passes frames and close codes through unchanged, never echoes the
token, and serves no other page's origin.
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.amiigo_tokens import RIDER_PHONE, Keypair

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE_ORIGIN = "http://localhost:8000"


def load_relay():
    spec = importlib.util.spec_from_file_location("amiigo_local", ROOT / "scripts" / "amiigo_local.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass needs its module registered while it loads
    spec.loader.exec_module(module)
    return module


class FakeUpstream:
    """The chat server's socket: sends `ready`, answers each message, then
    closes with the code a test chooses."""

    def __init__(self, close_code=1000, close_reason=""):
        self.sent = []
        self.inbox = asyncio.Queue()
        self.close_code = None
        self.close_reason = None
        self._final = (close_code, close_reason)

    async def send(self, text):
        self.sent.append(json.loads(text))
        await self.inbox.put(json.loads(text))

    async def close(self):
        if self.close_code is None:
            self.close_code, self.close_reason = 1000, ""

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        yield json.dumps({"type": "ready", "protocol": 1, "server_time": "2026-10-07T09:00:00Z"})
        frame = await self.inbox.get()
        yield json.dumps({"type": "bot_typing", "conversation_id": frame["conversation_id"], "state": "thinking"})
        self.close_code, self.close_reason = self._final


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.module = load_relay()
        self.token = Keypair().token()
        self.http_calls = []
        self.upstreams = []

        def handler(request: httpx.Request):
            self.http_calls.append(request)
            return httpx.Response(200, json={"conversations": [], "next_cursor": None})

        async def connect(url, additional_headers=None, **_):
            self.connect_args = (url, dict(additional_headers or {}))
            upstream = FakeUpstream(close_code=self.close_code, close_reason=self.close_reason)
            self.upstreams.append(upstream)
            return upstream

        self.close_code, self.close_reason = 4401, "token_expired"
        self.relay = self.module.Relay(target="https://chat.test", port=8000)
        self.app = self.module.build_app(self.relay, http_transport=httpx.MockTransport(handler), upstream_connect=connect)
        self.client = TestClient(self.app)

    def set_token(self, origin=PAGE_ORIGIN):
        return self.client.post("/relay/token", json={"token": self.token}, headers={"Origin": origin})

    def test_it_serves_the_page(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Amiigo Chat Tester", page.text)

    def test_a_token_is_described_never_echoed(self):
        answer = self.set_token()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["token_type"], "access")
        self.assertEqual(answer.json()["phone_ends"], RIDER_PHONE[-2:])
        self.assertNotIn(self.token, answer.text)
        self.assertNotIn(RIDER_PHONE, answer.text)
        status = self.client.get("/relay/status", headers={"Origin": PAGE_ORIGIN}).json()
        self.assertEqual(status, {"target": "https://chat.test", "token_set": True})
        self.assertNotIn(self.token, repr(self.relay))

    def test_something_that_is_not_a_v4_public_token_is_refused(self):
        for bad in ("", "Bearer abc", "v4.local.abc", "v4.public.!!!"):
            answer = self.client.post("/relay/token", json={"token": bad}, headers={"Origin": PAGE_ORIGIN})
            self.assertEqual(answer.status_code, 400, bad)
        self.assertIsNone(self.relay.token)

    def test_http_calls_carry_the_token_as_the_app_sends_it(self):
        self.set_token()
        answer = self.client.get("/amiigo/v1/conversations?limit=20", headers={"Origin": PAGE_ORIGIN})
        self.assertEqual(answer.status_code, 200)
        call = self.http_calls[0]
        self.assertEqual(str(call.url), "https://chat.test/amiigo/v1/conversations?limit=20")
        self.assertEqual(call.headers["authorization"], "Bearer " + self.token)

    def test_a_post_body_and_its_type_pass_through(self):
        self.set_token()
        self.client.post("/amiigo/v1/uploads", json={"conversation_id": "c", "mime_type": "image/jpeg", "size_bytes": 5},
                         headers={"Origin": PAGE_ORIGIN})
        call = self.http_calls[0]
        self.assertEqual(call.method, "POST")
        self.assertEqual(json.loads(call.content), {"conversation_id": "c", "mime_type": "image/jpeg", "size_bytes": 5})
        self.assertEqual(call.headers["content-type"], "application/json")

    def test_without_a_token_nothing_reaches_the_target(self):
        answer = self.client.get("/amiigo/v1/conversations", headers={"Origin": PAGE_ORIGIN})
        self.assertEqual((answer.status_code, answer.json()), (401, {"detail": "token_missing"}))
        self.assertEqual(self.http_calls, [])

    def test_another_page_s_origin_cannot_use_the_token(self):
        self.set_token()
        for origin in ("https://evil.example", "http://localhost:9999"):
            self.assertEqual(self.client.get("/amiigo/v1/conversations", headers={"Origin": origin}).status_code, 403)
            self.assertEqual(self.client.post("/relay/token", json={"token": self.token},
                                              headers={"Origin": origin}).status_code, 403)
            with self.assertRaises(WebSocketDisconnect) as closed:
                with self.client.websocket_connect("/relay/chat", headers={"Origin": origin}) as ws:
                    ws.receive_text()
            self.assertEqual(closed.exception.code, 1008)
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.upstreams, [])

    def test_the_socket_opens_with_the_header_and_passes_frames_and_the_close_code(self):
        self.set_token()
        with self.assertRaises(WebSocketDisconnect) as closed:
            with self.client.websocket_connect("/relay/chat", headers={"Origin": PAGE_ORIGIN}) as ws:
                self.assertEqual(json.loads(ws.receive_text())["type"], "ready")
                ws.send_text(json.dumps({"type": "message", "client_message_id": "m1", "conversation_id": "c1",
                                         "text": "my battery is not charging"}))
                self.assertEqual(json.loads(ws.receive_text())["type"], "bot_typing")
                ws.receive_text()
        self.assertEqual((closed.exception.code, closed.exception.reason), (4401, "token_expired"))
        url, headers = self.connect_args
        self.assertEqual(url, "wss://chat.test/amiigo/v1/chat")
        self.assertEqual(headers, {"Authorization": "Bearer " + self.token})
        self.assertEqual(self.upstreams[0].sent[0]["text"], "my battery is not charging")

    def test_a_close_with_no_code_reaches_the_page_as_1011(self):
        self.close_code, self.close_reason = None, None
        self.set_token()
        with self.assertRaises(WebSocketDisconnect) as closed:
            with self.client.websocket_connect("/relay/chat", headers={"Origin": PAGE_ORIGIN}) as ws:
                ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "client_message_id": "m1", "conversation_id": "c1", "text": "x"}))
                ws.receive_text()
                ws.receive_text()
        self.assertEqual(closed.exception.code, 1011)

    def test_without_a_token_the_socket_closes_4401(self):
        with self.assertRaises(WebSocketDisconnect) as closed:
            with self.client.websocket_connect("/relay/chat", headers={"Origin": PAGE_ORIGIN}) as ws:
                ws.receive_text()
        self.assertEqual((closed.exception.code, closed.exception.reason), (4401, "token_missing"))
        self.assertEqual(self.upstreams, [])

    def test_forgetting_the_token_stops_the_calls(self):
        self.set_token()
        self.client.delete("/relay/token", headers={"Origin": PAGE_ORIGIN})
        self.assertIsNone(self.relay.token)
        self.assertEqual(self.client.get("/amiigo/v1/conversations", headers={"Origin": PAGE_ORIGIN}).status_code, 401)

    def test_it_only_ever_listens_on_this_machine_and_defaults_to_staging_on_port_8000(self):
        self.assertEqual(self.module.HOST, "127.0.0.1")
        self.assertEqual(self.module.DEFAULT_PORT, 8000)  # the media bucket's allowed origin
        self.assertEqual(self.module.DEFAULT_TARGET, "https://ai-release-stage.emotorad.com")
        args = self.module.parse_args([])
        self.assertEqual((args.target, args.port), (self.module.DEFAULT_TARGET, 8000))

    def test_a_local_target_uses_a_plain_socket(self):
        relay = self.module.Relay(target="http://127.0.0.1:8001", port=8000)
        self.assertEqual(relay.socket_url, "ws://127.0.0.1:8001/amiigo/v1/chat")

    def test_the_page_keeps_the_token_out_of_storage_and_urls(self):
        page = (ROOT / "web" / "amiigo-test.html").read_text(encoding="utf-8")
        self.assertNotIn("localStorage", page)
        self.assertNotIn("sessionStorage", page)
        self.assertIn('type="password"', page)
        self.assertNotIn("?token=", page)


if __name__ == "__main__":
    unittest.main()
