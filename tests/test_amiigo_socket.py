"""The chat socket, /amiigo/v1/chat (docs/contracts/amiigo-support-chat.md:
"The chat socket", "Connecting", "Frames the app sends", "Frames the server
sends", "Resending", "Close codes"), and the receipts behind "Resending".

Through the real API (FastAPI's TestClient), with a token minted by a
keypair made here (tests/amiigo_tokens.py) and a fake bucket: no network, no
AWS. Most turns run on a fake runtime that records a turn the way the real
one does (transcript, summary, origin), so a test can hold a turn open with a
gate; one end-to-end turn runs through the real offline runtime. Every
receive has a timeout: a socket test fails, it never hangs.

The receipts run on both stores, in memory and MongoDB (mongomock, its TTL
clock pinned to the store's: the plan's Ruling 3).
"""

import importlib
import json
import os
import re
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

import anyio
import mongomock
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from emotorad_ai.amiigo import auth as auth_module
from emotorad_ai.amiigo import history
from emotorad_ai.amiigo import receipts as receipts_module
from emotorad_ai.amiigo.auth import LEEWAY, rider_hash_of
from emotorad_ai.amiigo.receipts import (
    BUSY,
    CLAIMED,
    DONE,
    DUPLICATE,
    LEASE,
    PROCESSING,
    RECEIPT_TTL,
    InMemoryAmiigoReceipts,
    receipt_id,
)
from emotorad_ai.amiigo.sockets import SocketRegistry
from emotorad_ai.config import Settings
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage, Reply
from emotorad_ai.conversation import (
    SHARED_OWNER,
    ConversationSummaryItem,
    StoreUnavailable,
    summary_key,
)
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.observability import EventLog
from emotorad_ai.storage.uploads import UploadRegistry
from emotorad_ai.stores.mongo import (
    AMIIGO_RECEIPTS,
    INDEXES,
    MongoAmiigoReceipts,
    MongoConversationStore,
    ensure_indexes,
)
from emotorad_ai.wiring import build_stores
from tests.amiigo_tokens import RIDER_PHONE, Keypair
from tests.clock import mongomock_clock_at
from tests.test_api_health import zoho_blank
from tests.test_api_uploads import _Store, _Summariser

PATH = "/amiigo/v1/chat"
NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=timezone.utc)
RIDER_KEY = "PHONE#" + RIDER_PHONE
OTHER_PHONE = "+919700000011"
OTHER_KEY = "PHONE#" + OTHER_PHONE
CID = "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
CID_2 = "4a3d0b2f-6c8e-4f9b-8d32-8e5f6a7b9c01"
CID_OTHER = "5b4e1c3a-7d9f-4a0c-9e43-9f6a7b8c0d12"
TIMEOUT = 5.0
BOT_TEXT = "Is the battery switched on?"
_ID = re.compile(r"^[0-9a-f-]{36}#[0-9]{5}$")
_Z_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def cmid(n):
    """A client message id: a UUID the app makes."""
    return "6c0b3f6a-1d2e-4f4b-9a7e-%012d" % n


def message(n=1, cid=CID, text="my battery isn't charging", **extra):
    return dict({"type": "message", "client_message_id": cmid(n), "conversation_id": cid, "text": text}, **extra)


# -- the socket from the app's side, with a timeout on every receive -----------


def next_message(ws, timeout=TIMEOUT):
    """The next ASGI message the server sent, or TimeoutError."""

    async def get():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    return ws.portal.call(get)


def frame(ws, timeout=TIMEOUT):
    sent = next_message(ws, timeout)
    if sent["type"] != "websocket.send":
        raise AssertionError("expected a frame, got %r" % (sent,))
    return json.loads(sent["text"])


def closed(ws, timeout=TIMEOUT):
    """(code, reason) of the close the server sent next."""
    sent = next_message(ws, timeout)
    if sent["type"] != "websocket.close":
        raise AssertionError("expected the socket to close, got %r" % (sent,))
    return sent.get("code"), sent.get("reason")


def quiet(ws, seconds=0.3):
    """Nothing arrives for `seconds`."""
    try:
        sent = next_message(ws, seconds)
    except TimeoutError:
        return
    raise AssertionError("expected nothing, got %r" % (sent,))


def wait_until(check, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("still not so after %.1f s" % timeout)


# -- the API, a fake bucket and a fake runtime ----------------------------------


class MediaStore(_Store):
    """The fake bucket of the upload tests, which also signs links as S3Store does."""

    def presign_get(self, key, expires_in=900):
        return "https://fake.s3.ap-south-1.amazonaws.com/%s?X-Amz-Signature=fake" % key


def fresh_api(key="", store=None):
    env = dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_STORE": "memory",
                "EMOTORAD_AI_MEDIA_BUCKET": "fake" if store else "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb",
                "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "", "EMOTORAD_EVIDENCE_CHECK": "",
                "EMOTORAD_AMIIGO_PUBLIC_KEY": key, "EMOTORAD_AMIGO_PG_DSN": "", "EMOTORAD_OMS_API_KEY": ""},
               **zoho_blank())
    with mock.patch.dict(os.environ, env), mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store):
        import emotorad_ai.api as api

        return importlib.reload(api)


def record_like_the_runtime(conversations, message, reply):
    """What Runtime.handle records for a rider's turn: the working state,
    where the run came from (with the rider), the transcript pair and the
    run's summary."""
    cid = message.conversation_id
    user_key = "PHONE#" + message.identity.phone
    state = conversations.get(cid)
    state.user_key = state.user_key or user_key
    state.channel = state.channel or message.channel
    state.turns += 1
    if state.turns == 1:
        conversations.record_origin({
            "_id": summary_key(cid, state.started_at), "conversation_id": cid, "started_at": state.started_at,
            "channel": message.channel, "user_key": user_key, "country": "IN", "region": None, "city": None,
            "source": "phone", "db": None})
    conversations.save(state)
    summary = ConversationSummaryItem(conversation_id=cid, user_key=user_key, started_at=state.started_at,
                                      last_at=datetime.now(timezone.utc).isoformat(), channel=message.channel,
                                      turns=state.turns)
    conversations.record_turn(state, message, reply, summary)


class FakeTurns:
    """Stands in for Runtime.handle: records each message it is given, waits
    on `gate` when one is set, then records the turn as the runtime does
    (unless `record` is off) and answers with a scripted reply."""

    def __init__(self, conversations, text=BOT_TEXT, **reply_fields):
        self.conversations = conversations
        self.text = text
        self.reply_fields = reply_fields
        self.calls = []
        self.gate = None
        self.record = True
        self.error = None
        self.started = threading.Event()
        self.finished = threading.Event()

    def __call__(self, message):
        self.calls.append(message)
        self.started.set()
        if self.gate is not None and not self.gate.wait(TIMEOUT):
            raise AssertionError("the test never opened the gate")
        if self.error is not None:
            raise self.error
        reply = Reply(conversation_id=message.conversation_id, text=self.text, handled_by="narrow_support",
                      **self.reply_fields)
        if self.record:
            record_like_the_runtime(self.conversations, message, reply)
        self.finished.set()
        return reply


class SocketCase(unittest.TestCase):
    """The API with Amiigo's public key set to this test's pair, a fake bucket
    and, unless a test says otherwise, the fake runtime."""

    use_fake_turns = True
    with_media = True

    @classmethod
    def setUpClass(cls):
        cls.keys = Keypair()

    @classmethod
    def tearDownClass(cls):
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            fresh_api()

    def setUp(self):
        self.store = MediaStore() if self.with_media else None
        self.api = fresh_api(self.keys.public_hex, self.store)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.ctx = self.api.app.state.amiigo
        self.ctx.socket_poll_seconds = 0.02
        self.conversations = self.api.stores.conversations
        self.turns = FakeTurns(self.conversations)
        if self.use_fake_turns:
            self.ctx.handle_turn = self.turns
        self.client = TestClient(self.api.app)
        self.token = self.keys.token()
        self.other_token = self.keys.token(phone=OTHER_PHONE)

    def tearDown(self):
        if self.turns.gate is not None:
            self.turns.gate.set()  # never leave a turn waiting

    def auth(self, token=None):
        return {"Authorization": "Bearer " + (self.token if token is None else token)}

    @contextmanager
    def socket(self, token=None, headers=None):
        """An open socket that has had its `ready`."""
        with self.client.websocket_connect(PATH, headers=self.auth(token) if headers is None else headers) as ws:
            ready = frame(ws, 2.0)
            self.assertEqual(ready["type"], "ready", ready)
            yield ws

    def send(self, ws, body):
        ws.send_text(json.dumps(body))

    def exchange(self, ws, body):
        """Send a message and read what a handled one gets: bot_typing, ack, reply."""
        self.send(ws, body)
        typing, ack, reply = frame(ws), frame(ws), frame(ws)
        self.assertEqual([typing["type"], ack["type"], reply["type"]], ["bot_typing", "ack", "reply"],
                         (typing, ack, reply))
        return typing, ack, reply

    def resend(self, ws, body):
        """Send a message already answered: the same ack and reply, and nothing else."""
        self.send(ws, body)
        ack, reply = frame(ws), frame(ws)
        self.assertEqual([ack["type"], reply["type"]], ["ack", "reply"], (ack, reply))
        return ack, reply

    def refused(self, ws, body, detail):
        self.send(ws, body)
        error = frame(ws)
        self.assertEqual(error, {"type": "error", "client_message_id": body.get("client_message_id"),
                                 "detail": detail})
        return error

    def gated(self):
        self.turns.gate = threading.Event()
        return self.turns.gate

    def events(self, name):
        return [e for e in self.api.log.events if e["event"] == name]

    def their_chat(self, user_key=OTHER_KEY, cid=CID_OTHER, text="their words, frame EMXP2026001234"):
        """Another rider's app chat, recorded as the runtime records one."""
        phone = user_key.split("#", 1)[1]
        inbound = InboundMessage(conversation_id=cid, persona="customer", channel="amiigo_app", message_text=text,
                                 identity=Identity(strength=VERIFIED, phone=phone))
        record_like_the_runtime(self.conversations, inbound, Reply(cid, "their reply", "narrow_support"))
        return cid


# -- connecting -------------------------------------------------------------------


class ConnectingTests(SocketCase):
    def test_ready_comes_first_with_the_protocol_and_the_time(self):
        with self.client.websocket_connect(PATH, headers=self.auth()) as ws:
            ready = frame(ws, 2.0)
        self.assertEqual(set(ready), {"type", "protocol", "server_time"})
        self.assertEqual((ready["type"], ready["protocol"]), ("ready", 1))
        self.assertRegex(ready["server_time"], _Z_TIME)

    def test_a_refused_token_closes_4401_with_its_code_before_ready(self):
        refresh = self.keys.token(token_type="refresh")
        expired = self.keys.token(now=datetime.now(timezone.utc) - timedelta(hours=2), lifetime=timedelta(hours=1))
        stranger = Keypair().token()
        cases = [({}, "token_missing"), ({"Authorization": "Bearer "}, "token_missing"),
                 ({"Authorization": "Bearer not-a-token"}, "token_invalid"),
                 ({"Authorization": "Basic " + self.token}, "token_invalid"),
                 ({"Authorization": "Bearer " + stranger}, "token_invalid"),
                 ({"Authorization": "Bearer " + expired}, "token_expired"),
                 ({"Authorization": "Bearer " + refresh}, "token_type_not_allowed")]
        for headers, code in cases:
            with self.subTest(code=code, headers=list(headers)):
                with self.client.websocket_connect(PATH, headers=headers) as ws:
                    self.assertEqual(closed(ws), (4401, code))
        self.assertEqual(self.turns.calls, [])
        logged = self.events("amiigo_socket_close")
        self.assertEqual([(e["code"], e["reason"]) for e in logged], [(4401, code) for _, code in cases])
        dumped = json.dumps(self.api.log.events)
        for token in (self.token, refresh, expired, stranger):
            self.assertNotIn(token, dumped)

    def test_with_the_token_check_off_the_socket_closes_1011_before_ready(self):
        # The plan's Ruling 1: a missing key is our outage, not the rider's.
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            self.ctx.tokens = auth_module.TokenCheck(None)
        with self.client.websocket_connect(PATH, headers=self.auth()) as ws:
            code, _ = closed(ws)
        self.assertEqual(code, 1011)

    def test_a_ping_gets_a_pong(self):
        with self.socket() as ws:
            self.send(ws, {"type": "ping"})
            self.assertEqual(frame(ws), {"type": "pong"})

    def test_ten_minutes_of_nothing_closes_4408_idle_and_a_ping_keeps_it_open(self):
        self.assertEqual(self.ctx.socket_idle_seconds, 600)
        self.ctx.socket_idle_seconds = 0.8
        with self.socket() as ws:
            for _ in range(4):
                time.sleep(0.3)
                self.send(ws, {"type": "ping"})
                self.assertEqual(frame(ws), {"type": "pong"})
            self.assertEqual(closed(ws, 2.0), (4408, "idle"))
        [logged] = self.events("amiigo_socket_close")
        self.assertEqual((logged["code"], logged["reason"]), (4408, "idle"))

    def test_the_socket_is_in_the_registry_while_it_is_open(self):
        self.assertIsInstance(self.ctx.sockets, SocketRegistry)
        with self.socket():
            self.assertEqual(len(self.ctx.sockets.of(RIDER_KEY)), 1)
            with self.socket():
                self.assertEqual(len(self.ctx.sockets.of(RIDER_KEY)), 2)
                self.assertEqual(self.ctx.sockets.of(OTHER_KEY), [])
            wait_until(lambda: len(self.ctx.sockets.of(RIDER_KEY)) == 1)
        wait_until(lambda: self.ctx.sockets.of(RIDER_KEY) == [])

    def test_a_frame_pushed_from_another_thread_reaches_every_open_socket_of_the_rider(self):
        # The seam ticket closure (Task 6) sends ticket_update through.
        update = {"type": "ticket_update", "conversation_id": CID}
        with self.socket() as ws, self.socket() as second, self.socket(token=self.other_token) as theirs:
            pushed = []
            worker = threading.Thread(target=lambda: pushed.append(self.ctx.sockets.push(RIDER_KEY, update)))
            worker.start()
            worker.join(TIMEOUT)
            self.assertEqual(pushed, [2])
            self.assertEqual(frame(ws), update)
            self.assertEqual(frame(second), update)
            quiet(theirs)

    def test_open_and_close_are_logged_with_the_riders_hash_only(self):
        with self.socket() as ws:
            self.send(ws, {"type": "ping"})
            frame(ws)
        wait_until(lambda: self.events("amiigo_socket_close"))
        [opened], [shut] = self.events("amiigo_socket_open"), self.events("amiigo_socket_close")
        self.assertEqual(opened["rider_hash"], rider_hash_of(RIDER_KEY))
        self.assertEqual(shut["rider_hash"], rider_hash_of(RIDER_KEY))
        self.assertIn("code", shut)
        self.assertIn("reason", shut)


# -- frames the server cannot act on ---------------------------------------------


class BadFrameTests(SocketCase):
    def test_what_is_not_a_frame_is_bad_frame_and_the_socket_stays_open(self):
        with self.socket() as ws:
            ws.send_text("not json")
            self.assertEqual(frame(ws), {"type": "error", "client_message_id": None, "detail": "bad_frame"})
            self.send(ws, {"type": "ping"})
            self.assertEqual(frame(ws), {"type": "pong"})

    def test_three_bad_frames_in_a_row_close_1008(self):
        bad = ["not json", json.dumps(["a", "list"]), json.dumps({"type": "shout"}),
               json.dumps({"no": "type"}), json.dumps(message(text="x" * 70_000))]
        for first, second, third in ((bad[0], bad[1], bad[2]), (bad[3], bad[4], bad[0])):
            with self.subTest(first=first[:20]):
                with self.socket() as ws:
                    for text in (first, second):
                        ws.send_text(text)
                        self.assertEqual(frame(ws)["detail"], "bad_frame")
                    ws.send_text(third)
                    self.assertEqual(frame(ws)["detail"], "bad_frame")
                    self.assertEqual(closed(ws)[0], 1008)

    def test_a_good_frame_between_bad_ones_starts_the_count_again(self):
        with self.socket() as ws:
            for _ in range(3):
                ws.send_text("not json")
                ws.send_text("not json")
                self.assertEqual(frame(ws)["detail"], "bad_frame")
                self.assertEqual(frame(ws)["detail"], "bad_frame")
                self.send(ws, {"type": "ping"})
                self.assertEqual(frame(ws), {"type": "pong"})

    def test_a_binary_frame_is_refused(self):
        with self.socket() as ws:
            ws.send_bytes(json.dumps({"type": "ping"}).encode("utf-8"))
            self.assertEqual(frame(ws)["detail"], "bad_frame")

    def test_a_frame_over_64_kb_is_bad_frame_and_one_under_is_read(self):
        padding = "x" * (64 * 1024)
        with self.socket() as ws:
            ws.send_text(json.dumps({"type": "ping", "pad": padding}))
            self.assertEqual(frame(ws)["detail"], "bad_frame")
            ws.send_text(json.dumps({"type": "ping", "pad": padding[:60_000]}))
            self.assertEqual(frame(ws), {"type": "pong"})

    def test_a_message_missing_a_field_or_with_a_wrong_one_is_bad_frame(self):
        without = {key: value for key, value in message().items() if key != "text"}
        cases = [
            without,
            message(client_message_id="not-a-uuid"),
            message(conversation_id="not-a-uuid"),
            message(conversation_id=CID + "\n"),
            message(text=42),
            message(attachments="upl_1"),
            message(attachments=[{"upload_id": "upl_1", "kind": "image"}]),
            message(attachments=[{"url": "https://example.com/x.jpg"}]),
            message(attachments=[{"upload_id": ""}]),
            message(screen=7),
            message(pill=["battery"]),
            message(location={"latitude": 18.52}),
            message(location={"latitude": 91, "longitude": 73.85}),
            message(location={"latitude": True, "longitude": 73.85}),
        ]
        with self.socket() as ws:
            for body in cases:
                with self.subTest(body=body):
                    self.send(ws, body)
                    error = frame(ws)
                    self.assertEqual(error["detail"], "bad_frame")
                    named = body.get("client_message_id")
                    self.assertEqual(error["client_message_id"], named if named == cmid(1) else None)
                    self.send(ws, {"type": "ping"})  # never three in a row
                    self.assertEqual(frame(ws), {"type": "pong"})
        self.assertEqual(self.turns.calls, [])

    def test_text_over_4000_characters_and_more_than_3_attachments_are_refused(self):
        with self.socket() as ws:
            self.refused(ws, message(text="न" * 4001), "text_too_long")
            self.refused(ws, message(attachments=[{"upload_id": "upl_%d" % i} for i in range(4)]),
                         "too_many_attachments")
            self.exchange(ws, message(n=2, text="न" * 4000))
        self.assertEqual(len(self.turns.calls), 1)


# -- one message: ack, bot_typing, reply ------------------------------------------


class MessageTests(SocketCase):
    def test_a_message_gets_typing_then_its_ack_and_reply_from_the_stored_turns(self):
        with self.socket() as ws:
            typing, ack, reply = self.exchange(ws, message(text="call me on 9876543210"))
        self.assertEqual(typing, {"type": "bot_typing", "conversation_id": CID, "state": "thinking"})
        [rider_turn, bot_turn] = self.conversations.turns_of(CID)
        self.assertEqual(ack, {
            "type": "ack", "client_message_id": cmid(1), "conversation_id": CID,
            "message": {"id": CID + "#00001", "sender": "rider", "text": "call me on [phone]",
                        "sent_at": ack["message"]["sent_at"], "attachments": []}})
        self.assertEqual(rider_turn.text, "call me on [phone]")
        self.assertRegex(ack["message"]["sent_at"], _Z_TIME)
        self.assertEqual(reply, {
            "type": "reply", "conversation_id": CID, "in_reply_to": cmid(1),
            "message": {"id": CID + "#00002", "sender": "bot", "text": BOT_TEXT,
                        "sent_at": reply["message"]["sent_at"], "attachments": []},
            "actions": [], "escalated": False, "ticket": None, "handled_by": "narrow_support"})
        self.assertEqual(bot_turn.text, BOT_TEXT)

    def test_the_turn_is_the_riders_on_the_app_channel(self):
        with self.socket() as ws:
            self.exchange(ws, message(screen="battery_health", pill="battery"))
        [made] = self.turns.calls
        self.assertEqual((made.channel, made.persona, made.identity.phone, made.identity.strength),
                         ("amiigo_app", "customer", RIDER_PHONE, "verified"))
        self.assertEqual((made.entry_metadata["screen"], made.entry_metadata["pill_clicked"]),
                         ("battery_health", "battery"))
        self.assertEqual(made.message_text, "my battery isn't charging")

    def test_a_shared_location_becomes_the_riders_words_and_the_point_is_kept_nowhere(self):
        class Geocoder:
            def reverse(self, lat, lon):
                return {"postcode": "122018", "suburb": "Sector 49"}

        self.api.geocoder = Geocoder()
        with self.socket() as ws:
            _, ack, _ = self.exchange(ws, message(text="", location={"latitude": 28.41, "longitude": 77.05}))
        [made] = self.turns.calls
        self.assertIn("Pincode 122018", made.message_text)
        self.assertIn("Pincode 122018", ack["message"]["text"])
        self.assertNotIn("28.41", json.dumps(made.to_dict()) + json.dumps(self.api.log.events))

    def test_the_reply_carries_actions_escalation_and_the_ticket_it_raised(self):
        self.turns.reply_fields = {"escalated": True, "ticket_id": "EM-1000042",
                                   "actions": [{"kind": "request_location", "label": "Share my location"}]}
        with self.socket() as ws:
            _, _, reply = self.exchange(ws, message())
        self.assertEqual((reply["escalated"], reply["ticket"], reply["actions"]),
                         (True, {"reference": "EM-1000042", "status": "open", "closed_at": None},
                          [{"kind": "request_location", "label": "Share my location"}]))

    def test_the_replys_pictures_come_with_fresh_links_and_their_captions(self):
        asset = "https://fake.s3.ap-south-1.amazonaws.com/assets/afs/battery/photos/switch.w900.webp"
        self.turns.reply_fields = {"attachments": [Attachment(
            kind="image", url=asset + "?X-Amz-Signature=old", mime_type="image/webp",
            caption="The battery On/Off switch", poster=None)]}
        with self.socket() as ws:
            _, _, reply = self.exchange(ws, message())
        [shown] = reply["message"]["attachments"]
        self.assertEqual(shown["url"], asset + "?X-Amz-Signature=fake")
        self.assertRegex(shown["url_expires_at"], _Z_TIME)
        self.assertEqual((shown["kind"], shown["mime_type"], shown["caption"], shown["poster"]),
                         ("image", "image/webp", "The battery On/Off switch", None))

    def test_the_socket_answers_pings_while_a_turn_runs(self):
        gate = self.gated()
        with self.socket() as ws:
            self.send(ws, message())
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            for _ in range(3):
                self.send(ws, {"type": "ping"})
                self.assertEqual(frame(ws), {"type": "pong"})
            gate.set()
            self.assertEqual([frame(ws)["type"], frame(ws)["type"]], ["ack", "reply"])

    def test_two_chats_on_one_socket_run_side_by_side(self):
        gate = self.gated()
        with self.socket() as ws:
            self.send(ws, message(n=1, cid=CID))
            self.send(ws, message(n=2, cid=CID_2))
            self.assertEqual({frame(ws)["conversation_id"], frame(ws)["conversation_id"]}, {CID, CID_2})
            wait_until(lambda: len(self.turns.calls) == 2)
            gate.set()
            replies = [frame(ws) for _ in range(4)]
        self.assertEqual(sorted(f["type"] for f in replies), ["ack", "ack", "reply", "reply"])

    def test_a_handled_message_is_logged_with_its_outcome_and_the_riders_hash_only(self):
        with self.socket() as ws:
            self.exchange(ws, message(text="call me on 9876543210 about EMXP2026001234"))
            self.refused(ws, message(n=2, text="x" * 4001), "text_too_long")
        logged = self.events("amiigo_message")
        self.assertEqual([e["outcome"] for e in logged], ["ok", "text_too_long"])
        self.assertTrue(all(e["rider_hash"] == rider_hash_of(RIDER_KEY) for e in logged))
        mine = json.dumps([e for e in self.api.log.events if e["event"].startswith("amiigo_")])
        for secret in (RIDER_PHONE, "9700000010", "9876543210", "EMXP2026001234", "call me", self.token, cmid(1)):
            self.assertNotIn(secret, mine)

    def test_the_twenty_first_message_in_a_minute_is_rate_limited_and_not_handled(self):
        self.assertEqual(self.ctx.message_limiter.per_minute, 20)
        with self.socket() as ws:
            for n in range(20):
                self.exchange(ws, message(n=n + 1, cid="%08d-0000-4000-8000-000000000000" % n))
            self.refused(ws, message(n=21, cid=CID_2), "rate_limited")
        self.assertEqual(len(self.turns.calls), 20)
        self.assertEqual(self.conversations.count_turns(CID_2), 0)


# -- whose chat it is -------------------------------------------------------------


class WhoseChatTests(SocketCase):
    def assert_nothing_of_theirs(self, ws, cid, said="their words"):
        before = [t.text for t in self.conversations.turns_of(cid)]
        error = self.refused(ws, message(cid=cid), "conversation_not_found")
        quiet(ws)
        self.assertNotIn(said, json.dumps(error))
        self.assertEqual([t.text for t in self.conversations.turns_of(cid)], before)
        self.assertEqual(self.turns.calls, [])
        self.assertIsNone(self.ctx.receipts.get(receipt_id(RIDER_KEY, cmid(1))))

    def test_another_riders_chat_is_conversation_not_found_and_nothing_of_it_leaks(self):
        # Review Focus 3.
        cid = self.their_chat()
        with self.socket() as ws:
            self.assert_nothing_of_theirs(ws, cid)
        [logged] = self.events("amiigo_message")
        self.assertEqual((logged["outcome"], logged["conversation_id"]), ("not_found", "amiigo"))

    def test_a_chat_another_rider_is_writing_in_right_now_is_not_found_not_busy(self):
        gate = self.gated()
        with self.socket(token=self.other_token) as theirs, self.socket() as ws:
            self.send(theirs, message(n=9, cid=CID_OTHER, text="their words"))
            self.assertEqual(frame(theirs)["type"], "bot_typing")
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            self.refused(ws, message(cid=CID_OTHER), "conversation_not_found")
            gate.set()
            self.assertEqual([frame(theirs)["type"], frame(theirs)["type"]], ["ack", "reply"])
            quiet(ws)
        self.assertEqual(len(self.turns.calls), 1)

    def test_a_chat_shared_by_two_people_is_refused(self):
        # The plan's Ruling 5: the rider's run, its working state gone, then
        # another person's run under the same id.
        self.their_chat(user_key=RIDER_KEY, cid=CID_OTHER, text="the rider's words")
        self.conversations._states.pop(CID_OTHER)
        self.their_chat(user_key=OTHER_KEY, cid=CID_OTHER)
        self.assertEqual(self.conversations.owner_of(CID_OTHER), SHARED_OWNER)
        with self.socket() as ws:
            self.assert_nothing_of_theirs(ws, CID_OTHER)

    def test_a_chat_with_turns_and_no_owner_is_refused(self):
        # The plan's Ruling 10: an anonymous web chat under this id.
        self.api.runtime.handle(self.api.prepare_turn(conversation_id=CID_OTHER, text="their words", em_aid="aid-x"))
        self.assertIsNone(self.conversations.owner_of(CID_OTHER))
        self.assertGreater(self.conversations.count_turns(CID_OTHER), 0)
        with self.socket() as ws:
            self.assert_nothing_of_theirs(ws, CID_OTHER)

    def test_a_working_state_that_is_not_the_riders_with_nothing_recorded_is_refused(self):
        # A turn whose records all failed to write leaves only its working
        # state, which holds what was said: never continued by another rider
        # (the review of Task 3).
        cases = ((OTHER_KEY, "amiigo_app", CID_OTHER), (None, "website_chat", CID_2),
                 (RIDER_KEY, "website_chat", "6d5f2d4b-8e0a-4b1d-8f54-0a7b8c9d1e23"))
        for user_key, channel, cid in cases:
            with self.subTest(user_key=user_key, channel=channel):
                state = self.conversations.get(cid)
                state.user_key, state.channel = user_key, channel
                state.history.append({"role": "user", "content": "their words"})
                self.conversations.save(state)
                self.assertTrue(history.is_new_conversation(self.conversations, cid))
                with self.socket() as ws:
                    self.assert_nothing_of_theirs(ws, cid)

    def test_the_riders_own_working_state_with_nothing_recorded_carries_on(self):
        state = self.conversations.get(CID)
        state.user_key, state.channel = RIDER_KEY, "amiigo_app"
        self.conversations.save(state)
        with self.socket() as ws:
            self.exchange(ws, message())
        self.assertEqual(len(self.turns.calls), 1)

    def test_the_riders_own_chat_carries_on(self):
        with self.socket() as ws:
            self.exchange(ws, message(n=1))
            _, ack, reply = self.exchange(ws, message(n=2, text="yes it is on"))
        self.assertEqual((ack["message"]["id"], reply["message"]["id"]), (CID + "#00003", CID + "#00004"))

    def test_storage_that_cannot_say_whose_chat_it_is_closes_1011(self):
        down = StoreUnavailable("MongoDB find failed")
        with mock.patch.object(self.conversations, "origins_of", side_effect=down):
            with self.socket() as ws:
                self.send(ws, message())
                self.assertEqual(closed(ws)[0], 1011)
        self.assertEqual(self.turns.calls, [])


# -- one message at a time, and resending --------------------------------------------


class OneAtATimeTests(SocketCase):
    def test_a_second_message_while_the_first_has_no_reply_is_busy_and_not_handled(self):
        gate = self.gated()
        with self.socket() as ws:
            self.send(ws, message(n=1))
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            self.refused(ws, message(n=2, text="hello?"), "conversation_busy")
            gate.set()
            self.assertEqual([frame(ws)["type"], frame(ws)["type"]], ["ack", "reply"])
            # Sent again after the reply, with the same id: handled.
            self.exchange(ws, message(n=2, text="hello?"))
        self.assertEqual([m.message_text for m in self.turns.calls], ["my battery isn't charging", "hello?"])

    def test_busy_holds_across_the_riders_sockets(self):
        gate = self.gated()
        with self.socket() as ws, self.socket() as second:
            self.send(ws, message(n=1))
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            self.refused(second, message(n=2), "conversation_busy")
            gate.set()
        wait_until(lambda: self.turns.finished.is_set())
        self.assertEqual(len(self.turns.calls), 1)

    def test_a_message_sent_again_after_a_reconnect_gets_the_same_ack_and_reply(self):
        with self.socket() as ws:
            _, ack, reply = self.exchange(ws, message())
        with self.socket() as ws:
            again_ack, again_reply = self.resend(ws, message())
        self.assertEqual((again_ack, again_reply), (ack, reply))
        self.assertEqual(len(self.turns.calls), 1)
        self.assertEqual(self.conversations.count_turns(CID), 2)

    def test_the_same_message_on_two_sockets_at_once_is_handled_once_and_both_get_its_answer(self):
        # Review Focus 2.
        gate = self.gated()
        with self.socket() as first, self.socket() as second:
            self.send(first, message())
            self.assertEqual(frame(first)["type"], "bot_typing")
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            self.send(second, message())
            self.assertEqual(frame(second), {"type": "bot_typing", "conversation_id": CID, "state": "thinking"})
            gate.set()
            answers_first = [frame(first), frame(first)]
            answers_second = [frame(second), frame(second)]
        self.assertEqual(len(self.turns.calls), 1)
        self.assertEqual([f["type"] for f in answers_first], ["ack", "reply"])
        self.assertEqual(answers_second, answers_first)

    def test_a_message_whose_socket_closed_mid_turn_is_saved_and_answered_on_resend(self):
        gate = self.gated()
        with self.socket() as ws:
            self.send(ws, message())
            self.assertTrue(self.turns.started.wait(TIMEOUT))
        gate.set()
        self.assertTrue(self.turns.finished.wait(TIMEOUT))
        rid = receipt_id(RIDER_KEY, cmid(1))
        wait_until(lambda: (self.ctx.receipts.get(rid) or {}).get("state") == DONE)
        self.assertEqual(self.conversations.count_turns(CID), 2)
        with self.socket() as ws:
            ack, reply = self.resend(ws, message())
        self.assertEqual((ack["message"]["id"], reply["message"]["id"]), (CID + "#00001", CID + "#00002"))
        self.assertEqual(len(self.turns.calls), 1)

    def test_a_message_sent_again_after_its_chat_was_deleted_is_not_found(self):
        with self.socket() as ws:
            self.exchange(ws, message())
            self.conversations.delete_conversation(CID)
            self.refused(ws, message(), "conversation_not_found")
        self.assertEqual(len(self.turns.calls), 1)
        self.assertEqual([e["conversation_id"] for e in self.events("amiigo_message")
                          if e["outcome"] == "not_found"], ["amiigo"])

    def test_the_same_id_for_another_chat_is_bad_frame(self):
        with self.socket() as ws:
            self.exchange(ws, message())
            self.refused(ws, message(cid=CID_2), "bad_frame")
        self.assertEqual(len(self.turns.calls), 1)

    def test_two_riders_may_use_the_same_message_id(self):
        with self.socket() as ws, self.socket(token=self.other_token) as theirs:
            self.exchange(ws, message(cid=CID))
            self.exchange(theirs, message(cid=CID_2))
        self.assertEqual(len(self.turns.calls), 2)

    def test_a_turn_the_store_could_not_record_still_gets_one_answer_and_the_same_on_resend(self):
        self.turns.record = False
        with self.socket() as ws:
            _, ack, reply = self.exchange(ws, message(text="call me on 9876543210"))
        with self.socket() as ws:
            again_ack, again_reply = self.resend(ws, message(text="call me on 9876543210"))
        self.assertEqual((again_ack, again_reply), (ack, reply))
        self.assertEqual(len(self.turns.calls), 1)
        self.assertEqual((ack["message"]["text"], reply["message"]["text"]), ("call me on [phone]", BOT_TEXT))
        self.assertNotRegex(ack["message"]["id"], _ID)
        self.assertNotEqual(ack["message"]["id"], reply["message"]["id"])
        logged = [e["outcome"] for e in self.events("amiigo_message")]
        self.assertEqual(logged, ["not_recorded", "duplicate"])

    def test_a_fault_in_the_turn_closes_1011_and_the_message_may_be_sent_again(self):
        self.turns.error = RuntimeError("a bug")
        with self.socket() as ws:
            self.send(ws, message())
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertEqual(closed(ws)[0], 1011)
        self.assertIsNone(self.ctx.receipts.get(receipt_id(RIDER_KEY, cmid(1))))
        self.turns.error = None
        with self.socket() as ws:
            self.exchange(ws, message())
        self.assertEqual(len(self.turns.calls), 2)
        [fault] = [e for e in self.events("amiigo_message") if e["outcome"] == "fault"]
        self.assertEqual(fault["error"], "RuntimeError")


# -- the token running out ----------------------------------------------------------


class TokenExpiryTests(SocketCase):
    def expiring_in(self, seconds):
        """A token, and the server's clock set so it runs out `seconds` from now."""
        issued = datetime.now(timezone.utc).replace(microsecond=0)
        token = self.keys.token(now=issued, lifetime=timedelta(hours=1))
        ends = issued + timedelta(hours=1) + LEEWAY
        self.ctx.clock = lambda: ends - timedelta(seconds=seconds)
        return token

    def test_the_socket_closes_4401_token_expired_when_the_token_runs_out(self):
        token = self.expiring_in(0.5)
        with self.socket(token=token) as ws:
            self.assertEqual(closed(ws, 3.0), (4401, "token_expired"))

    def test_a_reply_being_prepared_is_saved_and_sent_before_the_close(self):
        # Review Focus 1.
        gate = self.gated()
        token = self.expiring_in(1.0)
        with self.socket(token=token) as ws:
            self.send(ws, message())
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            quiet(ws, 1.5)  # the token has run out; the reply is still coming
            gate.set()
            ack, reply = frame(ws), frame(ws)
            self.assertEqual(closed(ws), (4401, "token_expired"))
        self.assertEqual((ack["type"], reply["type"]), ("ack", "reply"))
        self.assertEqual([t.text for t in self.conversations.turns_of(CID)], ["my battery isn't charging", BOT_TEXT])
        self.assertEqual(self.ctx.receipts.get(receipt_id(RIDER_KEY, cmid(1)))["state"], DONE)

    def test_a_message_after_the_token_ran_out_is_not_handled(self):
        gate = self.gated()
        token = self.expiring_in(1.0)
        with self.socket(token=token) as ws:
            self.send(ws, message(n=1))
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            time.sleep(1.3)
            self.send(ws, message(n=2, cid=CID_2))
            self.send(ws, {"type": "ping"})
            self.assertEqual(frame(ws), {"type": "pong"})
            gate.set()
            self.assertEqual([frame(ws)["type"], frame(ws)["type"]], ["ack", "reply"])
            self.assertEqual(closed(ws), (4401, "token_expired"))
        self.assertEqual(len(self.turns.calls), 1)
        self.assertIsNone(self.ctx.receipts.get(receipt_id(RIDER_KEY, cmid(2))))
        self.assertIn("token_expired", [e["outcome"] for e in self.events("amiigo_message")])


# -- photos and videos ---------------------------------------------------------------


class AttachmentTests(SocketCase):
    def rider_upload(self, mime="image/jpeg", phone=RIDER_PHONE, finished=True, cid=CID):
        """An upload slot as POST /amiigo/v1/uploads makes one, and its PUT."""
        pending, _ = self.api.UPLOADS.begin_customer(self.api._cluster_for_phone(phone), cid, mime, 9)
        if finished:
            self.store.objects[pending.key] = {"size": 9, "mime": mime}
        return pending

    def test_a_photo_is_claimed_and_the_ack_shows_it_with_a_fresh_link(self):
        photo = self.rider_upload()
        with self.socket() as ws:
            typing, ack, _ = self.exchange(ws, message(text="", attachments=[{"upload_id": photo.upload_id}]))
        self.assertEqual(typing["state"], "thinking")
        [shown] = ack["message"]["attachments"]
        self.assertEqual((shown["kind"], shown["url"]),
                         ("image", "https://fake.s3.ap-south-1.amazonaws.com/%s?X-Amz-Signature=fake" % photo.key))
        self.assertRegex(shown["url_expires_at"], _Z_TIME)
        self.assertIsNone(self.api.UPLOADS.peek(photo.upload_id), "claimed")
        [made] = self.turns.calls
        self.assertEqual([(a.kind, a.url) for a in made.attachments], [("image", "s3://" + photo.key)])

    def test_a_video_shows_the_bot_looking_at_it(self):
        self.api.VIDEO_SUMMARISER = _Summariser("the pack is on a table")
        clip = self.rider_upload(mime="video/mp4")
        with self.socket() as ws:
            typing, ack, _ = self.exchange(ws, message(attachments=[{"upload_id": clip.upload_id}]))
        self.assertEqual(typing, {"type": "bot_typing", "conversation_id": CID, "state": "looking_at_video"})
        self.assertEqual(ack["message"]["attachments"][0]["kind"], "video")
        self.assertEqual(self.turns.calls[0].attachments[0].summary, "the pack is on a table")

    def test_an_unknown_upload_is_upload_not_found_before_anything_runs(self):
        with self.socket() as ws:
            self.refused(ws, message(attachments=[{"upload_id": "upl_nothing"}]), "upload_not_found")
            quiet(ws)
        self.assertEqual(self.turns.calls, [])
        self.assertIsNone(self.ctx.receipts.get(receipt_id(RIDER_KEY, cmid(1))))

    def test_another_riders_upload_is_upload_not_found_and_stays_theirs(self):
        theirs = self.rider_upload(phone=OTHER_PHONE)
        with self.socket() as ws:
            self.send(ws, message(attachments=[{"upload_id": theirs.upload_id}]))
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertEqual(frame(ws), {"type": "error", "client_message_id": cmid(1), "detail": "upload_not_found"})
        self.assertIsNotNone(self.api.UPLOADS.peek(theirs.upload_id))
        self.assertEqual(self.turns.calls, [])

    def test_an_upload_whose_put_has_not_finished_can_be_sent_again_once_it_has(self):
        photo = self.rider_upload(finished=False)
        body = message(attachments=[{"upload_id": photo.upload_id}])
        with self.socket() as ws:
            self.send(ws, body)
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertEqual(frame(ws)["detail"], "upload_not_finished")
            self.store.objects[photo.key] = {"size": 9, "mime": "image/jpeg"}
            self.exchange(ws, body)
        self.assertEqual(len(self.turns.calls), 1)


class NoMediaTests(SocketCase):
    with_media = False

    def test_an_upload_on_a_server_without_media_storage_is_upload_not_found(self):
        # No code of its own on the socket: uploading again meets POST
        # /amiigo/v1/uploads, which answers 503 storage_unavailable to retry.
        self.assertIsNone(self.ctx.uploads)
        with self.socket() as ws:
            self.refused(ws, message(attachments=[{"upload_id": "upl_1"}]), "upload_not_found")
            self.exchange(ws, message(n=2))
        self.assertEqual(len(self.turns.calls), 1)

    def test_prepare_turns_503_for_missing_media_storage_is_upload_not_found_and_sent_again(self):
        # An upload slot the socket can see, on a server whose turn has no
        # bucket (prepare_turn's 503): the message is let go, to be sent again.
        registry = UploadRegistry(MediaStore())
        pending, _ = registry.begin_customer(self.api._cluster_for_phone(RIDER_PHONE), CID, "image/jpeg", 9)
        self.ctx.uploads = registry
        body = message(attachments=[{"upload_id": pending.upload_id}])
        with self.socket() as ws:
            self.send(ws, body)
            self.assertEqual(frame(ws)["type"], "bot_typing")
            self.assertEqual(frame(ws), {"type": "error", "client_message_id": cmid(1), "detail": "upload_not_found"})
        self.assertIsNone(self.ctx.receipts.get(receipt_id(RIDER_KEY, cmid(1))))
        self.assertEqual(self.turns.calls, [])


# -- end to end, through the real runtime ----------------------------------------------


class EndToEndTests(SocketCase):
    use_fake_turns = False
    # No bucket: a guide picture's link would be signed again, a moment apart,
    # by the reply and by the history.
    with_media = False

    def test_a_chat_on_the_socket_comes_back_whole_from_the_history(self):
        with self.socket() as ws:
            typing, ack, reply = self.exchange(ws, message(text="my battery is not charging"))
        self.assertEqual(typing["state"], "thinking")
        self.assertTrue(reply["message"]["text"].startswith(DISCLOSURE_TEXT), reply["message"]["text"])
        self.assertFalse(reply["handled_by"].startswith("verify_first"), reply["handled_by"])
        self.assertRegex(ack["message"]["id"], _ID)
        headers = self.auth()
        listed = self.client.get("/amiigo/v1/conversations", headers=headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        [chat] = listed.json()["conversations"]
        self.assertEqual((chat["conversation_id"], chat["channel"]), (CID, "amiigo_app"))
        read = self.client.get("/amiigo/v1/conversations/%s/messages" % CID, headers=headers)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["messages"], [ack["message"], reply["message"]])
        state = self.api.runtime.conversations.peek(CID)
        self.assertEqual((state.channel, state.user_key), ("amiigo_app", RIDER_KEY))


# -- the receipts, on both stores --------------------------------------------------


class Clock:
    """The receipts' clock, moved by the test. Under MongoDB, mongomock's TTL
    clock is pinned to it with tests/clock.py each time it moves."""

    def __init__(self, case, pin):
        self.now = NOW
        self.case = case
        self.pin = mongomock_clock_at(NOW) if pin else None
        if self.pin is not None:
            self.pin.start()
            case.addCleanup(lambda: self.pin.stop())

    def __call__(self):
        return self.now

    def move(self, by):
        self.now += by
        if self.pin is not None:
            self.pin.stop()
            self.pin = mongomock_clock_at(self.now)
            self.pin.start()


class ReceiptsContract:
    RID = receipt_id(RIDER_KEY, cmid(1))
    RID_2 = receipt_id(RIDER_KEY, cmid(2))

    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.make()

    def test_a_message_is_claimed_once(self):
        self.assertEqual(self.receipts.claim(self.RID, RIDER_KEY, CID), (CLAIMED, None))
        outcome, doc = self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.assertEqual((outcome, doc["state"], doc["conversation_id"]), (DUPLICATE, PROCESSING, CID))
        self.assertEqual(self.receipts.in_flight(CID), RIDER_KEY)

    def test_a_finished_message_answers_with_what_it_was_answered(self):
        self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.assertTrue(self.receipts.finish(self.RID, {"id": CID + "#00001"}, {"id": CID + "#00002"}))
        outcome, doc = self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.assertEqual((outcome, doc["state"], doc["ack"], doc["reply"]),
                         (DUPLICATE, DONE, {"id": CID + "#00001"}, {"id": CID + "#00002"}))
        self.assertIsNone(self.receipts.in_flight(CID))

    def test_one_message_of_a_conversation_at_a_time(self):
        self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.assertEqual(self.receipts.claim(self.RID_2, RIDER_KEY, CID), (BUSY, None))
        self.assertEqual(self.receipts.claim(self.RID_2, RIDER_KEY, CID_2), (CLAIMED, None))
        self.receipts.finish(self.RID, {"id": "a"}, {"id": "b"})
        self.assertEqual(self.receipts.claim(receipt_id(RIDER_KEY, cmid(3)), RIDER_KEY, CID), (CLAIMED, None))

    def test_a_released_message_may_be_claimed_again_and_a_finished_one_is_never_released(self):
        self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.receipts.release(self.RID)
        self.assertIsNone(self.receipts.get(self.RID))
        self.assertIsNone(self.receipts.in_flight(CID))
        self.assertEqual(self.receipts.claim(self.RID, RIDER_KEY, CID), (CLAIMED, None))
        self.receipts.finish(self.RID, {"id": "a"}, {"id": "b"})
        self.receipts.release(self.RID)
        self.assertEqual(self.receipts.get(self.RID)["state"], DONE)

    def test_a_claim_past_its_lease_no_longer_holds_the_conversation(self):
        self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.clock.move(LEASE + timedelta(seconds=1))
        self.assertIsNone(self.receipts.in_flight(CID))
        self.assertEqual(self.receipts.claim(self.RID_2, RIDER_KEY, CID), (CLAIMED, None))
        self.receipts.finish(self.RID_2, {"id": "a"}, {"id": "b"})
        # Its own message, sent again, is handled by whoever takes it.
        self.assertEqual(self.receipts.claim(self.RID, RIDER_KEY, CID), (CLAIMED, None))

    def test_receipts_are_kept_for_24_hours(self):
        self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.receipts.finish(self.RID, {"id": "a"}, {"id": "b"})
        self.clock.move(RECEIPT_TTL - timedelta(seconds=1))
        self.assertEqual(self.receipts.claim(self.RID, RIDER_KEY, CID)[0], DUPLICATE)
        self.clock.move(timedelta(seconds=2))
        self.assertIsNone(self.receipts.get(self.RID))
        self.assertEqual(self.receipts.claim(self.RID, RIDER_KEY, CID), (CLAIMED, None))

    def test_finishing_a_receipt_that_is_gone_says_so(self):
        self.assertFalse(self.receipts.finish(self.RID, {"id": "a"}, {"id": "b"}))

    def test_each_rider_has_their_own_ids(self):
        self.assertNotEqual(receipt_id(RIDER_KEY, cmid(1)), receipt_id(OTHER_KEY, cmid(1)))
        self.receipts.claim(receipt_id(RIDER_KEY, cmid(1)), RIDER_KEY, CID)
        self.assertEqual(self.receipts.claim(receipt_id(OTHER_KEY, cmid(1)), OTHER_KEY, CID_2), (CLAIMED, None))
        self.assertEqual(self.receipts.in_flight(CID_2), OTHER_KEY)


class MemoryReceiptsTests(ReceiptsContract, unittest.TestCase):
    def make(self):
        self.clock = Clock(self, pin=False)
        self.receipts = InMemoryAmiigoReceipts(now=self.clock)


class MongoReceiptsTests(ReceiptsContract, unittest.TestCase):
    def make(self):
        self.clock = Clock(self, pin=True)
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.receipts = MongoAmiigoReceipts(self.db, now=self.clock)

    def test_the_collection_expires_and_holds_one_processing_message_per_conversation(self):
        [(ttl_keys, ttl), (busy_keys, busy)] = INDEXES[AMIIGO_RECEIPTS]
        self.assertEqual((ttl_keys, ttl["expireAfterSeconds"]), ([("expires_at", 1)], 0))
        self.assertEqual((busy_keys, busy["unique"], busy["partialFilterExpression"]),
                         ([("conversation_id", 1)], True, {"state": PROCESSING}))
        self.assertTrue(self.receipts.has_ttl_index())
        # Two servers racing: the database refuses the second processing message.
        collection = self.db[AMIIGO_RECEIPTS]
        collection.insert_one(receipts_module.new_receipt(self.RID, RIDER_KEY, CID, NOW))
        with self.assertRaises(DuplicateKeyError):
            collection.insert_one(receipts_module.new_receipt(self.RID_2, RIDER_KEY, CID, NOW))

    def test_a_receipt_holds_no_text_of_a_recorded_turn(self):
        self.receipts.claim(self.RID, RIDER_KEY, CID)
        self.receipts.finish(self.RID, {"id": CID + "#00001"}, {"id": CID + "#00002", "actions": []})
        doc = self.db[AMIIGO_RECEIPTS].find_one({"_id": self.RID})
        self.assertEqual(set(doc), {"_id", "user_key", "conversation_id", "state", "ack", "reply", "at",
                                    "expires_at"})

    def test_storage_that_is_down_is_store_unavailable(self):
        from pymongo.errors import ServerSelectionTimeoutError

        with mock.patch.object(mongomock.collection.Collection, "find_one",
                               side_effect=ServerSelectionTimeoutError("no servers")):
            with self.assertRaises(StoreUnavailable):
                self.receipts.get(self.RID)


class MongoSocketTests(SocketCase):
    """The socket on MongoDB: the conversations and the receipts on
    mongomock, the receipts' clock and mongomock's pinned (Ruling 3)."""

    def setUp(self):
        super().setUp()
        pin = mongomock_clock_at(NOW)
        pin.start()
        self.addCleanup(pin.stop)
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.conversations = MongoConversationStore(self.db, now=lambda: NOW)
        self.api.stores.conversations = self.conversations
        self.turns.conversations = self.conversations
        self.ctx.receipts = MongoAmiigoReceipts(self.db, now=lambda: NOW)

    def test_resending_busy_and_the_ack_on_mongodb(self):
        gate = self.gated()
        with self.socket() as ws, self.socket() as second:
            self.send(ws, message(n=1))
            self.assertTrue(self.turns.started.wait(TIMEOUT))
            self.refused(second, message(n=2), "conversation_busy")
            self.send(second, message(n=1))
            self.assertEqual(frame(second)["type"], "bot_typing")
            gate.set()
            first = [frame(ws) for _ in range(3)]
            again = [frame(second), frame(second)]
        self.assertEqual(first[1:], again)
        self.assertEqual(first[1]["message"]["id"], CID + "#00001")
        self.assertEqual(len(self.turns.calls), 1)
        doc = self.db[AMIIGO_RECEIPTS].find_one({"_id": receipt_id(RIDER_KEY, cmid(1))})
        self.assertEqual((doc["state"], doc["ack"]), (DONE, {"id": CID + "#00001"}))


# -- wiring -----------------------------------------------------------------------


class WiringTests(unittest.TestCase):
    def missing(self, log):
        return [e for e in log.events if e["event"] == "amiigo_receipts_ttl_missing"]

    def test_memory_gives_in_memory_receipts(self):
        self.assertIsInstance(build_stores(Settings(store="memory")).amiigo_receipts, InMemoryAmiigoReceipts)

    def test_mongodb_gives_mongodb_receipts_once_their_ttl_index_exists(self):
        client = mongomock.MongoClient()
        ensure_indexes(client["emotorad_ai"])
        log = EventLog(path=None)
        stores = build_stores(Settings(store="mongodb"), log=log, client=client)
        self.assertIsInstance(stores.amiigo_receipts, MongoAmiigoReceipts)
        self.assertEqual(self.missing(log), [])

    def test_without_the_ttl_index_receipts_stay_in_memory_and_it_says_so(self):
        client = mongomock.MongoClient()
        log = EventLog(path=None)
        with self.assertLogs("emotorad_ai.wiring", level="ERROR") as logged:
            stores = build_stores(Settings(store="mongodb"), log=log, client=client)
        self.assertIsInstance(stores.amiigo_receipts, InMemoryAmiigoReceipts)
        [event] = self.missing(log)
        self.assertEqual(event["level"], "error")
        self.assertIn("amiigo_receipts_ttl_missing", "\n".join(logged.output))
        self.assertNotIn(AMIIGO_RECEIPTS, client["emotorad_ai"].list_collection_names())

    def test_the_api_hands_the_socket_its_receipts_its_turns_and_its_limits(self):
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            api = fresh_api()
        ctx = api.app.state.amiigo
        self.assertIs(ctx.receipts, api.stores.amiigo_receipts)
        self.assertIsNotNone(ctx.prepare_turn)
        self.assertIsNotNone(ctx.handle_turn)
        self.assertEqual((ctx.message_limiter.per_minute, ctx.socket_idle_seconds), (20, 600))


if __name__ == "__main__":
    unittest.main()
