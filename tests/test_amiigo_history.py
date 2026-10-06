"""A rider's chats and messages, restored with GET (docs/contracts/amiigo-support-chat.md:
"History", "Messages", "What is masked", "Errors on the HTTP endpoints",
"Caching and privacy").

Everything runs twice, on the in-memory stores and on MongoDB (mongomock),
through the real API with a token minted by a keypair made here
(tests/amiigo_tokens.py). The history's clock is pinned; the token check runs
on the real clock, so every token is minted from the real time.
"""

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import mongomock
from fastapi.testclient import TestClient

from emotorad_ai.amiigo import auth as auth_module
from emotorad_ai.amiigo import history
from emotorad_ai.amiigo.auth import TokenCheck
from emotorad_ai.amiigo.common import RiderLimiter
from emotorad_ai.contract import Attachment, Identity, InboundMessage, Reply
from emotorad_ai.conversation import (
    SHARED_OWNER,
    ConversationSummaryItem,
    InMemoryConversationStore,
    StoreUnavailable,
    TranscriptTurn,
    summary_key,
)
from emotorad_ai.observability import EventLog
from emotorad_ai.storage.s3 import S3Store
from emotorad_ai.stores.mongo import (
    CONVERSATION_NOTICES,
    CONVERSATION_SUMMARIES,
    INDEXES,
    MongoConversationStore,
    MongoTicketStore,
    ensure_indexes,
)
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.wiring import Stores
from tests.amiigo_tokens import RIDER_PHONE, Keypair
from tests.clock import mongomock_clock_at

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=timezone.utc)
RIDER_KEY = "PHONE#" + RIDER_PHONE
OTHER_PHONE = "+919700000011"
OTHER_KEY = "PHONE#" + OTHER_PHONE
CID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
CID_B = "bbbbbbbb-0000-4000-8000-00000000000b"
CID_C = "cccccccc-0000-4000-8000-00000000000c"
CID_D = "dddddddd-0000-4000-8000-00000000000d"
CID_E = "eeeeeeee-0000-4000-8000-00000000000e"
FRAME = "EMXP2026001234"  # from the fixtures
BUCKET = "emotorad-ai-media-test"
PHOTO_KEY = "customers/cl-rider/%s/images/upl_0000000001abcdefgh.jpg" % CID_A
ASSET_KEY = "assets/afs/battery/photos/soc-button.w900.webp"
ASSET_URL = "https://%s.s3.ap-south-1.amazonaws.com/%s" % (BUCKET, ASSET_KEY)
CLOSED_TEXT = "Your support request EM-1000001 was closed by our support team."
DROP = object()


def iso(moment):
    return moment.isoformat()


def z(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tid(cid, n):
    return "%s#%05d" % (cid, n)


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Clock:
    """The stores' clock for transcript times, moved by the test."""

    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now.isoformat()


class FakeMedia:
    """The media bucket: signs a key the way S3Store does, and remembers it."""

    bucket = BUCKET

    def __init__(self, fail=False):
        self.signed = []
        self.fail = fail

    def presign_get(self, key, expires_in=900):
        if self.fail:
            raise RuntimeError("no credentials")
        self.signed.append((key, expires_in))
        return "https://%s.s3.ap-south-1.amazonaws.com/%s?X-Amz-Signature=fake" % (BUCKET, key)


def signed(key):
    return "https://%s.s3.ap-south-1.amazonaws.com/%s?X-Amz-Signature=fake" % (BUCKET, key)


def notice(cid, seq, at, user_key=RIDER_KEY, text=CLOSED_TEXT):
    """A notice as Task 6 writes it."""
    return {"_id": "%s#N%05d" % (cid, seq), "conversation_id": cid, "user_key": user_key,
            "kind": "ticket_closed", "text": text, "at": iso(at)}


def ticket_record(store, cid=CID_A, **stored):
    """A ticket record inserted as the seam writes one; `stored` overrides
    fields as written (DROP leaves one out, as a record from before the field)."""
    reference = store.next_reference()
    record = new_record(
        reference=reference, chat_reference="stage:" + reference,
        source_key="%s:%s:create_support_ticket:%s" % (cid, iso(NOW), reference), mode="test", kind="support",
        conversation_id=cid, started_at=iso(NOW), cluster_id="cl-rider", channel="amiigo_app",
        phone="+919999999999", identity="verified", category="battery_charging", ai_severity="normal",
        summary="LED stays off.", claims={}, bike=None, coverage="computed", customer_name=None, created_at=iso(NOW),
    )
    for name, value in stored.items():
        if value is DROP:
            record.pop(name)
        else:
            record[name] = value
    store.insert(record)
    return reference


class StoreKinds:
    """The two places a chat lives. Each test case mixes in one."""

    def make_stores(self):
        raise NotImplementedError

    def put_notice(self, doc):
        raise NotImplementedError

    def exchange(self, cid, at, user_key=RIDER_KEY, customer="My battery is not charging",
                 bot="Is the battery switched on?", run_started=None, channel="amiigo_app", title="Battery issue",
                 frame=None, product=None, outcome="open", ticket_id=None, customer_files=(), bot_files=(),
                 origin=True):
        """One rider message and its reply, recorded as the runtime records a
        turn: the transcript pair, the run's summary (only with a user key,
        as the runtime writes one) and where the run came from (every run,
        anonymous ones too: Runtime._note_origin; `origin=False` is a write
        of it that failed, which the runtime only logs)."""
        self.clock.now = at
        if origin:
            self.conversations.record_origin({
                "_id": summary_key(cid, iso(run_started or at)), "conversation_id": cid,
                "started_at": iso(run_started or at), "channel": channel, "user_key": user_key, "country": "IN",
                "region": None, "city": None, "source": "phone", "db": None})
        state = self.conversations.get(cid)
        state.user_key, state.channel = user_key, channel
        state.turns += 1
        message = InboundMessage(conversation_id=cid, persona="customer", identity=Identity(), channel=channel,
                                 message_text=customer,
                                 attachments=[Attachment(kind=k, url=u) for k, u in customer_files])
        reply = Reply(conversation_id=cid, text=bot, handled_by="battery_support", ticket_id=ticket_id,
                      attachments=[Attachment(kind=k, url=u) for k, u in bot_files])
        summary = ConversationSummaryItem(
            conversation_id=cid, user_key=user_key, started_at=iso(run_started or at), last_at=iso(at),
            channel=channel, title=title, frame_number=frame, product_name=product, outcome=outcome,
            ticket_id=ticket_id, turns=state.turns)
        self.conversations.record_turn(state, message, reply, summary)


class MemoryStores(StoreKinds):
    def make_stores(self):
        self.clock = Clock()
        self.conversations = InMemoryConversationStore(clock=self.clock)
        self.tickets = InMemoryTicketStore()

    def put_notice(self, doc):
        # Writing notices is Task 6's; the read side is tested here.
        self.conversations._notices.setdefault(doc["conversation_id"], {})[doc["_id"]] = dict(doc)


class MongoStores(StoreKinds):
    def make_stores(self):
        # Ruling 3: `conversations` has a TTL index; mongomock reads this clock.
        pin = mongomock_clock_at(NOW)
        pin.start()
        self.addCleanup(pin.stop)
        self.db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(self.db)
        self.clock = Clock()
        self.conversations = MongoConversationStore(self.db, clock=self.clock, now=lambda: NOW)
        self.tickets = MongoTicketStore(self.db)

    def put_notice(self, doc):
        self.db[CONVERSATION_NOTICES].insert_one(dict(doc))


# -- the ticket record -----------------------------------------------------------


class TicketRecordTests(unittest.TestCase):
    def test_a_new_record_is_open_and_not_closed(self):
        store = InMemoryTicketStore()
        record = store.get(ticket_record(store))
        self.assertEqual((record["support_status"], record["closed_at"]), ("open", None))


# -- the store reads, held to one contract ------------------------------------


class StoreReadsContract:
    def setUp(self):
        self.make_stores()

    def test_runs_of_gives_every_run_of_the_person_and_filters_by_channel(self):
        self.exchange(CID_A, NOW - timedelta(hours=3))
        self.exchange(CID_A, NOW - timedelta(hours=1), run_started=NOW - timedelta(hours=1))
        self.exchange(CID_B, NOW - timedelta(hours=2), channel="website_chat")
        self.exchange(CID_C, NOW - timedelta(hours=2), user_key=OTHER_KEY)
        runs = self.conversations.runs_of(RIDER_KEY)
        self.assertEqual(sorted((r.conversation_id, r.started_at) for r in runs),
                         [(CID_A, iso(NOW - timedelta(hours=3))), (CID_A, iso(NOW - timedelta(hours=1))),
                          (CID_B, iso(NOW - timedelta(hours=2)))])
        self.assertTrue(all(isinstance(r, ConversationSummaryItem) for r in runs))
        self.assertEqual({r.conversation_id for r in self.conversations.runs_of(RIDER_KEY, channel="website_chat")},
                         {CID_B})
        self.assertEqual(self.conversations.runs_of("PHONE#+919000000000"), [])

    def test_owner_of_is_the_person_whose_runs_they_are(self):
        self.exchange(CID_A, NOW)
        self.assertEqual(self.conversations.owner_of(CID_A), RIDER_KEY)
        self.assertIsNone(self.conversations.owner_of("never-seen"))
        # A shared browser: a second person proved their number in the same
        # chat after the first person's proof lapsed (restart_for).
        self.exchange(CID_B, NOW - timedelta(hours=2))
        self.exchange(CID_B, NOW, user_key=OTHER_KEY, run_started=NOW)
        self.assertEqual(self.conversations.owner_of(CID_B), SHARED_OWNER)
        self.assertNotIn(SHARED_OWNER, (RIDER_KEY, OTHER_KEY))

    def test_turns_of_carries_the_conversation_and_count_turns_counts_them(self):
        self.exchange(CID_A, NOW - timedelta(minutes=2))
        self.exchange(CID_A, NOW)
        turns = self.conversations.turns_of(CID_A)
        self.assertEqual([(t.n, t.role, t.conversation_id) for t in turns],
                         [(1, "customer", CID_A), (2, "bot", CID_A), (3, "customer", CID_A), (4, "bot", CID_A)])
        self.assertEqual(self.conversations.count_turns(CID_A), 4)
        self.assertEqual(self.conversations.count_turns("never-seen"), 0)

    def test_notices_of_come_in_time_order(self):
        # A chat says one thing once (one_notice_per_text): the second differs.
        self.put_notice(notice(CID_A, 2, NOW, text=CLOSED_TEXT.replace("EM-1000001", "EM-1000002")))
        self.put_notice(notice(CID_A, 1, NOW - timedelta(minutes=5)))
        self.put_notice(notice(CID_B, 1, NOW))
        self.assertEqual([n["_id"] for n in self.conversations.notices_of(CID_A)],
                         ["%s#N00001" % CID_A, "%s#N00002" % CID_A])
        self.assertEqual(self.conversations.notices_of(CID_A)[0]["text"], CLOSED_TEXT)
        self.assertEqual(self.conversations.notices_of("never-seen"), [])

    def test_deleting_a_conversation_or_the_person_takes_the_notices(self):
        self.exchange(CID_A, NOW)
        self.put_notice(notice(CID_A, 1, NOW))
        self.exchange(CID_B, NOW)
        self.put_notice(notice(CID_B, 1, NOW))
        self.assertEqual(self.conversations.delete_conversation(CID_A)["conversation_notices"], 1)
        self.assertEqual(self.conversations.notices_of(CID_A), [])
        self.assertEqual(self.conversations.delete_person(RIDER_KEY, dry_run=True)["conversation_notices"], 1)
        self.assertEqual(self.conversations.delete_person(RIDER_KEY)["conversation_notices"], 1)
        self.assertEqual(self.conversations.notices_of(CID_B), [])

    def test_ticket_status_open_closed_missing_and_from_before_the_field(self):
        open_ref = ticket_record(self.tickets)
        closed_ref = ticket_record(self.tickets, support_status="closed", closed_at="2026-10-06T09:30:00+00:00")
        old_ref = ticket_record(self.tickets, support_status=DROP, closed_at=DROP)
        other_ref = ticket_record(self.tickets, support_status="on_hold")
        self.assertEqual(self.tickets.ticket_status(open_ref), {"reference": open_ref, "status": "open", "closed_at": None})
        self.assertEqual(self.tickets.ticket_status(closed_ref),
                         {"reference": closed_ref, "status": "closed", "closed_at": "2026-10-06T09:30:00+00:00"})
        self.assertEqual(self.tickets.ticket_status(old_ref), {"reference": old_ref, "status": "open", "closed_at": None})
        # Zoho's other states show as open in v1.
        self.assertEqual(self.tickets.ticket_status(other_ref)["status"], "open")
        self.assertIsNone(self.tickets.ticket_status("EM-00001"))


class MemoryStoreReadsTests(MemoryStores, StoreReadsContract, unittest.TestCase):
    pass


class MongoStoreReadsTests(MongoStores, StoreReadsContract, unittest.TestCase):
    pass


class IndexTests(unittest.TestCase):
    def test_history_reads_have_their_indexes_and_notices_never_expire(self):
        summaries = {options["name"]: keys for keys, options in INDEXES[CONVERSATION_SUMMARIES]}
        self.assertEqual(summaries["user_last"], [("user_key", 1), ("last_at", -1)])
        self.assertEqual(summaries["conversation"], [("conversation_id", 1)])
        self.assertEqual([(keys, options) for keys, options in INDEXES[CONVERSATION_NOTICES]],
                         [([("conversation_id", 1), ("at", 1)], {"name": "conversation_at"}),
                          ([("conversation_id", 1), ("kind", 1), ("text", 1)],
                           {"name": "one_notice_per_text", "unique": True})])
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        self.assertIn(CONVERSATION_NOTICES, db.list_collection_names())
        for info in db[CONVERSATION_NOTICES].index_information().values():
            self.assertNotIn("expireAfterSeconds", info)


# -- links and the message object --------------------------------------------------


class SignerTests(unittest.TestCase):
    def setUp(self):
        self.media = FakeMedia()
        self.log = EventLog()
        self.sign = history.signer_for(self.media, NOW, self.log)
        self.expires = "2026-10-06T10:15:00Z"

    def test_a_stored_file_is_signed_for_fifteen_minutes(self):
        self.assertEqual(self.sign("s3://" + PHOTO_KEY), (signed(PHOTO_KEY), self.expires))
        self.assertEqual(self.sign("s3://%s/%s" % (BUCKET, PHOTO_KEY)), (signed(PHOTO_KEY), self.expires))
        self.assertEqual(self.media.signed, [(PHOTO_KEY, 900), (PHOTO_KEY, 900)])

    def test_a_guide_picture_in_our_bucket_is_signed_again(self):
        self.assertEqual(self.sign(ASSET_URL), (signed(ASSET_KEY), self.expires))
        path_style = "https://s3.ap-south-1.amazonaws.com/%s/%s" % (BUCKET, ASSET_KEY)
        self.assertEqual(self.sign(path_style), (signed(ASSET_KEY), self.expires))

    def test_anything_else_has_no_link(self):
        for url in ("data:(inline, not kept)", "https://other-bucket.s3.ap-south-1.amazonaws.com/" + ASSET_KEY,
                    "https://res.cloudinary.com/emotorad/image/upload/f_auto/x.jpg", "https://x.test/p.jpg",
                    "s3://customers/../assets/afs/x/photos/y.jpg", "s3://other-bucket/" + PHOTO_KEY,
                    "https://%s.s3.ap-south-1.amazonaws.com/%s" % (BUCKET, PHOTO_KEY), "", None):
            with self.subTest(url):
                self.assertEqual(self.sign(url), (None, None))
        self.assertEqual(self.media.signed, [])

    def test_no_bucket_means_no_links(self):
        self.assertEqual(history.signer_for(None, NOW, self.log)("s3://" + PHOTO_KEY), (None, None))

    def test_a_link_that_cannot_be_signed_is_null_and_logged_without_the_key(self):
        sign = history.signer_for(FakeMedia(fail=True), NOW, self.log)
        self.assertEqual(sign("s3://" + PHOTO_KEY), (None, None))
        (event,) = [e for e in self.log.events if e["event"] == "amiigo_media_unsigned"]
        self.assertEqual(event["error"], "RuntimeError")
        self.assertNotIn("upl_0000000001", json.dumps(self.log.events))


class MessageViewTests(unittest.TestCase):
    def sign(self, url):
        return (None, None)

    def test_a_rider_turn_a_bot_turn_and_a_notice(self):
        rider = TranscriptTurn(n=3, role="customer", text="hi", at=iso(NOW), conversation_id=CID_A)
        bot = TranscriptTurn(n=4, role="bot", text="Hello.", at="2026-10-06T10:00:00.250000+00:00",
                             attachments=({"kind": "image", "url": "data:(inline, not kept)"},), conversation_id=CID_A)
        self.assertEqual(history.message_view(rider, self.sign, NOW),
                         {"id": tid(CID_A, 3), "sender": "rider", "text": "hi", "sent_at": "2026-10-06T10:00:00Z",
                          "attachments": []})
        self.assertEqual(history.message_view(bot, self.sign, NOW),
                         {"id": tid(CID_A, 4), "sender": "bot", "text": "Hello.", "sent_at": "2026-10-06T10:00:00Z",
                          "attachments": [{"kind": "image", "url": None, "url_expires_at": None}]})
        self.assertEqual(history.message_view(notice(CID_A, 1, NOW), self.sign, NOW),
                         {"id": CID_A + "#N00001", "sender": "system", "text": CLOSED_TEXT,
                          "sent_at": "2026-10-06T10:00:00Z", "attachments": []})

    def test_a_turn_that_does_not_know_its_conversation_is_refused(self):
        with self.assertRaises(ValueError):
            history.message_view(TranscriptTurn(n=1, role="customer", text="hi", at=iso(NOW)), self.sign, NOW)


class S3PresignTests(unittest.TestCase):
    def test_presign_get_takes_how_long_the_link_lives(self):
        class Client:
            def generate_presigned_url(self, operation, Params, ExpiresIn):
                self.asked = (operation, Params, ExpiresIn)
                return "https://signed"

        client = Client()
        store = S3Store(BUCKET, client=client)
        store.presign_get("assets/afs/x/photos/y.jpg", expires_in=120)
        self.assertEqual(client.asked, ("get_object", {"Bucket": BUCKET, "Key": "assets/afs/x/photos/y.jpg"}, 120))
        store.presign_get("assets/afs/x/photos/y.jpg")
        self.assertEqual(client.asked[2], 900)


# -- the two endpoints ------------------------------------------------------------


class HistoryApiContract:
    """GET /amiigo/v1/conversations and GET /amiigo/v1/conversations/{id}/messages."""

    @classmethod
    def setUpClass(cls):
        cls.keys = Keypair()
        cls.api = cls.api_with(cls.keys.public_hex)
        cls.ctx = cls.api.app.state.amiigo
        cls.token_check = cls.ctx.tokens

    @classmethod
    def tearDownClass(cls):
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            cls.api_with("")

    @staticmethod
    def api_with(key):
        from tests.test_api_health import fresh_api, zoho_blank

        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
            return fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AMIIGO_PUBLIC_KEY": key,
                                   "EMOTORAD_STORE": "memory", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"}, **zoho_blank()))

    def setUp(self):
        self.make_stores()
        self.media = FakeMedia()
        self.ctx.stores = Stores(conversations=self.conversations, idempotency=None, tickets=self.tickets)
        self.ctx.clock = lambda: NOW
        self.ctx.media_store = self.media
        self.ctx.tokens = self.token_check
        self.ctx.history_limiter = RiderLimiter(60)
        self.api.log.events.clear()
        self.client = TestClient(self.api.app)
        self.rider_token = self.keys.token()
        self.other_token = self.keys.token(phone=OTHER_PHONE)

    def get(self, path, token=None, **params):
        token = self.rider_token if token is None else token
        headers = {"Authorization": "Bearer " + token} if token else {}
        return self.client.get("/amiigo/v1" + path, params=params, headers=headers)

    def ok(self, path, token=None, **params):
        r = self.get(path, token=token, **params)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        return r.json()

    def refused(self, r, status, code):
        self.assertEqual((r.status_code, r.json()), (status, {"detail": code}), r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def chats(self, token=None, **params):
        return self.ok("/conversations", token=token, **params)

    def messages(self, cid, token=None, **params):
        return self.ok("/conversations/%s/messages" % cid, token=token, **params)

    def five_exchanges(self, cid=CID_A):
        for i in range(5):
            self.exchange(cid, NOW - timedelta(minutes=50 - i), customer="message %d" % i, bot="reply %d" % i)

    # -- the list -------------------------------------------------------------

    def test_an_empty_history(self):
        self.assertEqual(self.chats(), {"conversations": [], "next_cursor": None})

    def test_chats_come_most_recent_first_then_by_id(self):
        self.exchange(CID_B, NOW - timedelta(hours=2))
        self.exchange(CID_D, NOW - timedelta(hours=1))
        self.exchange(CID_A, NOW - timedelta(hours=1))
        self.exchange(CID_C, NOW - timedelta(minutes=30))
        self.exchange(CID_E, NOW - timedelta(hours=1, microseconds=-500000))
        listed = [c["conversation_id"] for c in self.chats()["conversations"]]
        self.assertEqual(listed, [CID_C, CID_E, CID_A, CID_D, CID_B])

    def test_the_runs_of_one_chat_are_one_item(self):
        first, second = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
        reference = ticket_record(self.tickets, support_status="closed", closed_at="2026-10-06T09:30:00+00:00")
        self.exchange(CID_A, first)
        self.exchange(CID_A, first + timedelta(minutes=5), run_started=first)
        self.exchange(CID_A, second, run_started=second, title="Battery charges very slowly", frame=FRAME,
                      product="EMX Plus", outcome="escalated", ticket_id=reference)
        self.assertEqual(self.chats(), {"conversations": [{
            "conversation_id": CID_A,
            "channel": "amiigo_app",
            "title": "Battery charges very slowly",
            "started_at": z(first),
            "last_message_at": z(second),
            "bike": {"product_name": "EMX Plus", "frame_number": FRAME},
            "status": "handed_to_support",
            "ticket": {"reference": reference, "status": "closed", "closed_at": "2026-10-06T09:30:00Z"},
            "message_count": 6,
            "can_continue": True,
        }], "next_cursor": None})

    def test_no_bike_no_ticket_and_open(self):
        self.exchange(CID_A, NOW)
        (chat,) = self.chats()["conversations"]
        self.assertEqual((chat["bike"], chat["ticket"], chat["status"]), (None, None, "open"))

    def assertNotInHistory(self, cid, words, token=None):
        """Neither listed nor readable, and nothing of it in either answer."""
        listed = self.get("/conversations", token=token)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(cid, [c["conversation_id"] for c in listed.json()["conversations"]])
        read = self.get("/conversations/%s/messages" % cid, token=token)
        self.refused(read, 404, "conversation_not_found")
        for said in words:
            self.assertNotIn(said, listed.text + read.text)

    def test_only_app_chats_are_in_the_history(self):
        # Ruling 6: in v1 the app's history holds Amiigo app chats only, even
        # where the rider proved the same number in a website chat.
        self.exchange(CID_A, NOW - timedelta(hours=1))
        self.exchange(CID_B, NOW, channel="website_chat", customer="typed on the website")
        self.assertEqual([(c["conversation_id"], c["channel"]) for c in self.chats()["conversations"]],
                         [(CID_A, "amiigo_app")])
        self.assertEqual([c["conversation_id"] for c in self.chats(channel="amiigo_app")["conversations"]], [CID_A])
        self.assertNotInHistory(CID_B, ["typed on the website"])
        for channel in ("website_chat", "whatsapp", "telegram"):
            with self.subTest(channel=channel):
                r = self.get("/conversations", channel=channel)
                self.assertEqual((r.status_code, r.json()["detail"][0]["loc"]), (422, ["query", "channel"]))
                self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_a_chat_with_any_website_run_is_not_in_the_history(self):
        self.exchange(CID_A, NOW - timedelta(hours=3), channel="website_chat", customer="on the website")
        self.exchange(CID_A, NOW - timedelta(hours=1), run_started=NOW - timedelta(hours=1))
        self.exchange(CID_B, NOW)
        self.assertEqual([c["conversation_id"] for c in self.chats()["conversations"]], [CID_B])
        self.assertNotInHistory(CID_A, ["on the website"])

    def test_a_website_run_whose_origin_was_not_recorded_still_hides_the_chat(self):
        # The summary's channel is the backstop when the origin write failed.
        self.exchange(CID_A, NOW - timedelta(hours=3), channel="website_chat", customer="on the website",
                      origin=False)
        self.exchange(CID_A, NOW - timedelta(hours=1), run_started=NOW - timedelta(hours=1))
        self.assertNotInHistory(CID_A, ["on the website"])

    def test_someone_who_never_verified_in_the_riders_website_chat_is_never_shown(self):
        # The review's case 1: the rider verified in a website chat, their
        # working state expired, and the next person on that browser wrote
        # without verifying. Their turns are recorded with no summary.
        self.exchange(CID_A, NOW - timedelta(hours=60), channel="website_chat", customer="the rider's words")
        self.exchange(CID_A, NOW - timedelta(hours=1), user_key=None, run_started=NOW - timedelta(hours=1),
                      channel="website_chat", customer="a stranger's words")
        self.assertEqual(self.conversations.owner_of(CID_A), RIDER_KEY)  # the summaries alone name the rider
        self.assertNotInHistory(CID_A, ["a stranger's words", "the rider's words"])

    def test_an_anonymous_run_under_an_app_chats_id_hides_the_chat(self):
        # A run with no summary still records where it came from: a website
        # message sent under the id of an app chat is never read out to the rider.
        self.exchange(CID_A, NOW - timedelta(hours=60))
        self.exchange(CID_A, NOW - timedelta(hours=1), user_key=None, run_started=NOW - timedelta(hours=1),
                      channel="website_chat", customer="a stranger's words")
        self.assertNotInHistory(CID_A, ["a stranger's words"])

    def test_another_riders_app_run_known_only_by_where_it_came_from_hides_the_chat(self):
        # The plan's Ruling 10: a run whose summary was never written (its
        # write failed) is known by its origin record alone, and that record
        # names another rider. The summaries name only this rider.
        self.exchange(CID_A, NOW - timedelta(hours=60), customer="the rider's words")
        later = NOW - timedelta(hours=1)
        self.conversations.record_origin({
            "_id": summary_key(CID_A, iso(later)), "conversation_id": CID_A, "started_at": iso(later),
            "channel": "amiigo_app", "user_key": OTHER_KEY, "country": "IN", "region": None, "city": None,
            "source": "phone", "db": None})
        self.exchange(CID_A, later, user_key=None, run_started=later, customer="the other rider's words",
                      origin=False)
        self.assertEqual(self.conversations.owner_of(CID_A), RIDER_KEY)
        self.assertNotInHistory(CID_A, ["the other rider's words", "the rider's words"])
        self.assertFalse(history.rider_may_use(self.ctx.stores, self.rider(), CID_A))

    def test_an_anonymous_app_run_under_an_app_chats_id_hides_the_chat(self):
        # Ruling 10: an app run nobody was proved in records where it came
        # from with no user key; its words are nobody's to read out.
        self.exchange(CID_A, NOW - timedelta(hours=60), customer="the rider's words")
        self.exchange(CID_A, NOW - timedelta(hours=1), user_key=None, run_started=NOW - timedelta(hours=1),
                      customer="a stranger's words")
        self.assertEqual([o["channel"] for o in self.conversations.origins_of(CID_A)], ["amiigo_app"] * 2)
        self.assertNotInHistory(CID_A, ["a stranger's words", "the rider's words"])
        self.assertFalse(history.rider_may_use(self.ctx.stores, self.rider(), CID_A))

    def rider(self):
        return auth_module.rider_from_header("Bearer " + self.rider_token, self.token_check)

    def test_limits_outside_the_range_are_422_with_the_field_list(self):
        for path, limit in (("/conversations", 0), ("/conversations", 51),
                            ("/conversations/%s/messages" % CID_A, 0), ("/conversations/%s/messages" % CID_A, 101)):
            with self.subTest(path=path, limit=limit):
                r = self.get(path, limit=limit)
                self.assertEqual(r.status_code, 422)
                self.assertEqual(r.json()["detail"][0]["loc"], ["query", "limit"])
                self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.exchange(CID_A, NOW)
        self.assertEqual(len(self.chats(limit=50)["conversations"]), 1)
        self.assertEqual(len(self.messages(CID_A, limit=100)["messages"]), 2)

    def test_paging_loses_and_repeats_nothing(self):
        cids = [CID_A, CID_B, CID_C, CID_D, CID_E]
        for i, cid in enumerate(cids):
            self.exchange(cid, NOW - timedelta(hours=i))
        seen, cursor, pages = [], None, 0
        while True:
            page = self.chats(limit=2, **({"cursor": cursor} if cursor else {}))
            seen += [c["conversation_id"] for c in page["conversations"]]
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual((seen, pages), (cids, 3))

    def test_a_cursor_the_server_did_not_issue_is_refused(self):
        self.exchange(CID_A, NOW)
        self.exchange(CID_B, NOW - timedelta(hours=1))
        self.exchange(CID_C, NOW, user_key=OTHER_KEY)
        self.exchange(CID_D, NOW - timedelta(hours=1), user_key=OTHER_KEY)
        for cursor in ("not-a-cursor!", b64(b"hello"), b64(b"[1, 2]"), b64(json.dumps({"v": 1}).encode()),
                       "x" * 2000):
            with self.subTest(cursor=cursor[:20]):
                self.refused(self.get("/conversations", cursor=cursor), 400, "cursor_invalid")

    def test_a_cursor_holds_nothing_from_the_phone(self):
        # Ruling 7: a short unsalted hash of PHONE#... can be brute-forced
        # back to the number, and cursors end up in app logs.
        for i in range(3):
            self.exchange((CID_A, CID_B, CID_C)[i], NOW - timedelta(hours=i))
        self.five_exchanges(CID_D)
        cursors = [self.chats(limit=1)["next_cursor"], self.messages(CID_D, limit=2)["older_cursor"]]
        for cursor in cursors:
            with self.subTest(cursor=cursor[:12]):
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
                for derived in (auth_module.rider_hash_of(RIDER_KEY), RIDER_PHONE, RIDER_PHONE[3:], RIDER_KEY):
                    self.assertNotIn(derived, decoded)
                self.assertNotIn('"r"', decoded)

    def test_another_riders_cursor_reads_only_this_riders_chats(self):
        # Ruling 7: the cursor positions; the query by user key scopes.
        for i, cid in enumerate((CID_A, CID_B, CID_C)):
            self.exchange(cid, NOW - timedelta(hours=2 * i))
        for i, cid in enumerate((CID_D, CID_E)):
            self.exchange(cid, NOW - timedelta(hours=2 * i + 1), user_key=OTHER_KEY)
        theirs = self.chats(token=self.other_token, limit=1)["next_cursor"]
        r = self.get("/conversations", cursor=theirs)
        if r.status_code == 200:
            listed = [c["conversation_id"] for c in r.json()["conversations"]]
            self.assertTrue(set(listed) <= {CID_A, CID_B, CID_C}, listed)
            self.assertEqual(listed, [CID_B, CID_C])  # after the other rider's position, NOW - 1 h
        else:
            self.refused(r, 400, "cursor_invalid")
        mine = self.chats(limit=1)["next_cursor"]
        r = self.get("/conversations", token=self.other_token, cursor=mine)
        self.assertNotIn(CID_B, r.text)
        self.assertNotIn(CID_C, r.text)
        # A messages cursor of this rider's chat, used by the other rider: not found, never read.
        self.five_exchanges(CID_A)
        older = self.messages(CID_A, limit=2)["older_cursor"]
        self.refused(self.get("/conversations/%s/messages" % CID_A, token=self.other_token, before=older),
                     404, "conversation_not_found")

    def test_another_riders_chats_are_never_listed(self):
        self.exchange(CID_A, NOW)
        self.exchange(CID_B, NOW, user_key=OTHER_KEY)
        self.assertEqual([c["conversation_id"] for c in self.chats()["conversations"]], [CID_A])
        self.assertEqual([c["conversation_id"] for c in self.chats(token=self.other_token)["conversations"]], [CID_B])

    def test_a_deleted_chat_is_never_listed(self):
        self.exchange(CID_A, NOW)
        self.exchange(CID_B, NOW - timedelta(hours=1))
        self.conversations.delete_conversation(CID_A)
        self.assertEqual([c["conversation_id"] for c in self.chats()["conversations"]], [CID_B])
        self.refused(self.get("/conversations/%s/messages" % CID_A), 404, "conversation_not_found")

    def test_a_chat_shared_with_another_person_is_neither_listed_nor_read(self):
        # Review Focus 3, history side: nothing of the other person's chat leaks.
        self.exchange(CID_A, NOW - timedelta(hours=2), customer="the first person")
        self.exchange(CID_A, NOW, user_key=OTHER_KEY, run_started=NOW, customer="the second person")
        self.exchange(CID_B, NOW - timedelta(hours=1))
        for token in (None, self.other_token):
            self.assertNotIn(CID_A, [c["conversation_id"] for c in self.chats(token=token)["conversations"]])
            self.refused(self.get("/conversations/%s/messages" % CID_A, token=token), 404, "conversation_not_found")

    def test_the_tickets_status(self):
        open_ref = ticket_record(self.tickets)
        closed_ref = ticket_record(self.tickets, support_status="closed", closed_at="2026-10-06T09:30:00+00:00")
        old_ref = ticket_record(self.tickets, support_status=DROP, closed_at=DROP)
        self.exchange(CID_A, NOW - timedelta(minutes=1), ticket_id=open_ref)
        self.exchange(CID_B, NOW - timedelta(minutes=2), ticket_id=closed_ref)
        self.exchange(CID_C, NOW - timedelta(minutes=3), ticket_id="EM-00001")  # the mock's: no record
        self.exchange(CID_D, NOW - timedelta(minutes=4), ticket_id=old_ref)
        tickets = [c["ticket"] for c in self.chats()["conversations"]]
        self.assertEqual(tickets, [
            {"reference": open_ref, "status": "open", "closed_at": None},
            {"reference": closed_ref, "status": "closed", "closed_at": "2026-10-06T09:30:00Z"},
            {"reference": "EM-00001", "status": "open", "closed_at": None},
            {"reference": old_ref, "status": "open", "closed_at": None},
        ])

    def test_can_continue_either_side_of_48_hours(self):
        self.exchange(CID_A, NOW - timedelta(hours=47, minutes=59))
        self.exchange(CID_B, NOW - timedelta(hours=48, minutes=1))
        self.assertEqual([(c["conversation_id"], c["can_continue"]) for c in self.chats()["conversations"]],
                         [(CID_A, True), (CID_B, False)])

    def test_message_count_counts_every_sender(self):
        self.exchange(CID_A, NOW - timedelta(minutes=10))
        self.exchange(CID_A, NOW - timedelta(minutes=5))
        self.put_notice(notice(CID_A, 1, NOW))
        (chat,) = self.chats()["conversations"]
        self.assertEqual(chat["message_count"], 5)

    # -- the messages ---------------------------------------------------------

    def test_the_newest_page_comes_oldest_first_with_a_cursor_for_the_one_before(self):
        self.five_exchanges()
        page = self.messages(CID_A, limit=4)
        self.assertEqual([m["id"] for m in page["messages"]], [tid(CID_A, n) for n in (7, 8, 9, 10)])
        self.assertEqual([(m["sender"], m["text"]) for m in page["messages"][:2]],
                         [("rider", "message 3"), ("bot", "reply 3")])
        self.assertEqual(page["messages"][0]["sent_at"], z(NOW - timedelta(minutes=47)))
        self.assertEqual((page["conversation_id"], page["more_after"]), (CID_A, False))
        self.assertIsNotNone(page["older_cursor"])
        everything = self.messages(CID_A)
        self.assertEqual((len(everything["messages"]), everything["older_cursor"]), (10, None))

    def test_before_walks_back_to_the_start(self):
        self.five_exchanges()
        seen, cursor = [], None
        while True:
            page = self.messages(CID_A, limit=4, **({"before": cursor} if cursor else {}))
            seen = [m["id"] for m in page["messages"]] + seen
            cursor = page["older_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, [tid(CID_A, n) for n in range(1, 11)])

    def test_after_the_last_message_gives_nothing(self):
        self.five_exchanges()
        page = self.messages(CID_A, after=tid(CID_A, 10))
        self.assertEqual(page, {"conversation_id": CID_A, "messages": [], "older_cursor": None, "more_after": False})

    def test_after_a_message_in_the_middle(self):
        self.five_exchanges()
        page = self.messages(CID_A, after=tid(CID_A, 4), limit=3)
        self.assertEqual(([m["id"] for m in page["messages"]], page["more_after"]),
                         ([tid(CID_A, 5), tid(CID_A, 6), tid(CID_A, 7)], True))
        page = self.messages(CID_A, after=tid(CID_A, 7), limit=3)
        self.assertEqual(([m["id"] for m in page["messages"]], page["more_after"]),
                         ([tid(CID_A, 8), tid(CID_A, 9), tid(CID_A, 10)], False))

    def test_a_notice_takes_its_place_in_time_as_a_system_message(self):
        self.exchange(CID_A, NOW - timedelta(minutes=10))
        self.put_notice(notice(CID_A, 1, NOW - timedelta(minutes=5)))
        self.exchange(CID_A, NOW - timedelta(minutes=1))
        # At the same instant as a turn, the turn comes first.
        self.put_notice(notice(CID_A, 2, NOW - timedelta(minutes=1), text="Second notice."))
        page = self.messages(CID_A)
        self.assertEqual([m["id"] for m in page["messages"]],
                         [tid(CID_A, 1), tid(CID_A, 2), CID_A + "#N00001", tid(CID_A, 3), tid(CID_A, 4),
                          CID_A + "#N00002"])
        self.assertEqual(page["messages"][2], {"id": CID_A + "#N00001", "sender": "system", "text": CLOSED_TEXT,
                                               "sent_at": z(NOW - timedelta(minutes=5)), "attachments": []})
        after = self.messages(CID_A, after=CID_A + "#N00001")
        self.assertEqual([m["id"] for m in after["messages"]],
                         [tid(CID_A, 3), tid(CID_A, 4), CID_A + "#N00002"])

    def test_text_comes_back_masked_as_stored(self):
        self.exchange(CID_A, NOW, customer="call me on 9876543210 or mail a.rider@example.com")
        (rider, _) = self.messages(CID_A)["messages"]
        self.assertEqual(rider["text"], self.conversations.turns_of(CID_A)[0].text)
        self.assertIn("[phone]", rider["text"])
        self.assertIn("[email]", rider["text"])
        self.assertNotIn("9876543210", rider["text"])

    def test_attachments_come_with_fresh_links(self):
        self.exchange(CID_A, NOW, customer="", bot="This is the switch.",
                      customer_files=[("image", "s3://" + PHOTO_KEY), ("image", "data:image/jpeg;base64,AAAA")],
                      bot_files=[("image", ASSET_URL + "?X-Amz-Signature=old")])
        rider, bot = self.messages(CID_A)["messages"]
        expires = z(NOW + timedelta(seconds=900))
        self.assertEqual(rider["text"], "")
        self.assertEqual(rider["attachments"], [
            {"kind": "image", "url": signed(PHOTO_KEY), "url_expires_at": expires},
            {"kind": "image", "url": None, "url_expires_at": None},
        ])
        self.assertEqual(bot["attachments"], [{"kind": "image", "url": signed(ASSET_KEY), "url_expires_at": expires}])
        self.assertEqual(self.media.signed, [(PHOTO_KEY, 900), (ASSET_KEY, 900)])

    def test_with_no_bucket_every_link_is_null(self):
        self.ctx.media_store = None
        self.exchange(CID_A, NOW, customer_files=[("image", "s3://" + PHOTO_KEY)], bot_files=[("image", ASSET_URL)])
        rider, bot = self.messages(CID_A)["messages"]
        self.assertEqual(rider["attachments"], [{"kind": "image", "url": None, "url_expires_at": None}])
        self.assertEqual(bot["attachments"], [{"kind": "image", "url": None, "url_expires_at": None}])

    def test_another_riders_chat_and_a_made_up_id_are_not_found(self):
        self.exchange(CID_B, NOW, user_key=OTHER_KEY, customer="their words")
        for cid in (CID_B, "made-up", "+919700000011"):
            with self.subTest(cid):
                r = self.get("/conversations/%s/messages" % cid)
                self.refused(r, 404, "conversation_not_found")
                self.assertNotIn("their words", r.text)
        self.assertNotIn("+919700000011", json.dumps(self.api.log.events))

    def test_a_before_or_after_the_server_did_not_issue_is_refused(self):
        self.five_exchanges(CID_A)
        self.five_exchanges(CID_B)
        other_chat_cursor = self.messages(CID_B, limit=2)["older_cursor"]
        self.exchange(CID_C, NOW - timedelta(hours=5))
        list_cursor = self.chats(limit=1)["next_cursor"]
        path = "/conversations/%s/messages" % CID_A
        for before in ("garbage!", b64(b"{}"), other_chat_cursor, list_cursor):
            with self.subTest(before=before):
                self.refused(self.get(path, before=before), 400, "cursor_invalid")
        for after in (tid(CID_A, 99), tid(CID_B, 1), "nonsense"):
            with self.subTest(after=after):
                self.refused(self.get(path, after=after), 400, "cursor_invalid")
        r = self.get(path, before=self.messages(CID_A, limit=2)["older_cursor"], after=tid(CID_A, 1))
        self.assertEqual(r.status_code, 422)

    def test_a_turn_recorded_between_pages_is_neither_skipped_nor_repeated(self):
        # Review Focus 5.
        for i in range(3):
            self.exchange(CID_A, NOW - timedelta(minutes=30 - i))
        first = self.messages(CID_A, limit=4)
        self.assertEqual([m["id"] for m in first["messages"]], [tid(CID_A, n) for n in (3, 4, 5, 6)])
        self.exchange(CID_A, NOW - timedelta(minutes=1))
        self.put_notice(notice(CID_A, 1, NOW))
        older = self.messages(CID_A, limit=4, before=first["older_cursor"])
        newer = self.messages(CID_A, after=first["messages"][-1]["id"])
        ids = [m["id"] for m in older["messages"] + first["messages"] + newer["messages"]]
        self.assertEqual(ids, [tid(CID_A, n) for n in range(1, 9)] + [CID_A + "#N00001"])
        self.assertEqual((older["older_cursor"], newer["more_after"]), (None, False))

    # -- refusals -------------------------------------------------------------

    def test_every_token_refusal_is_401_with_its_code(self):
        self.exchange(CID_A, NOW)
        real_now = datetime.now(timezone.utc)
        cases = (
            ({}, "token_missing"),
            ({"Authorization": "Bearer"}, "token_missing"),
            ({"Authorization": "Bearer garbage"}, "token_invalid"),
            ({"Authorization": "Basic " + self.rider_token}, "token_invalid"),
            ({"Authorization": "Bearer " + Keypair().token()}, "token_invalid"),
            ({"Authorization": "Bearer " + self.keys.token(now=real_now - timedelta(hours=3))}, "token_expired"),
            ({"Authorization": "Bearer " + self.keys.token(token_type="refresh")}, "token_type_not_allowed"),
            ({"Authorization": "Bearer " + self.keys.token(token_type="otp")}, "token_type_not_allowed"),
        )
        for path in ("/amiigo/v1/conversations", "/amiigo/v1/conversations/%s/messages" % CID_A):
            for headers, code in cases:
                with self.subTest(path=path, code=code, headers=list(headers.values())[:1]):
                    self.refused(self.client.get(path, headers=headers), 401, code)

    def test_the_61st_history_request_in_a_minute_is_rate_limited(self):
        self.exchange(CID_A, NOW)
        for _ in range(40):
            self.chats()
        for _ in range(20):
            self.messages(CID_A)
        # The two endpoints share one allowance per rider.
        self.refused(self.get("/conversations"), 429, "rate_limited")
        self.refused(self.get("/conversations/%s/messages" % CID_A), 429, "rate_limited")
        # Another rider has their own.
        self.chats(token=self.other_token)

    def test_storage_that_is_down_is_503(self):
        self.exchange(CID_A, NOW, ticket_id="EM-1000001")
        down = StoreUnavailable("MongoDB find failed (ServerSelectionTimeoutError)")
        with mock.patch.object(self.conversations, "runs_of", side_effect=down):
            self.refused(self.get("/conversations"), 503, "history_unavailable")
        with mock.patch.object(self.tickets, "ticket_status", side_effect=down):
            self.refused(self.get("/conversations"), 503, "history_unavailable")
        with mock.patch.object(self.conversations, "owner_of", side_effect=down):
            self.refused(self.get("/conversations/%s/messages" % CID_A), 503, "history_unavailable")
        with mock.patch.object(self.conversations, "turns_of", side_effect=down):
            self.refused(self.get("/conversations/%s/messages" % CID_A), 503, "history_unavailable")
        outcomes = [e["outcome"] for e in self.api.log.events if e["event"].startswith("amiigo_history_")]
        self.assertEqual(outcomes, ["store_unavailable"] * 4)

    def test_with_the_token_check_off_the_answer_is_503_before_the_token_is_read(self):
        with mock.patch.object(auth_module, "_not_configured_logged", True):
            self.ctx.tokens = TokenCheck(None)
        for headers in ({}, {"Authorization": "Bearer " + self.rider_token}):
            for path in ("/amiigo/v1/conversations", "/amiigo/v1/conversations/%s/messages" % CID_A):
                with self.subTest(path=path, headers=bool(headers)):
                    self.refused(self.client.get(path, headers=headers), 503, "history_unavailable")

    def test_every_answer_under_amiigo_v1_is_no_store_and_nothing_else_changes(self):
        self.exchange(CID_A, NOW)
        for r in (self.get("/conversations"), self.get("/conversations/%s/messages" % CID_A),
                  self.get("/conversations", cursor="bad!"), self.client.get("/amiigo/v1/conversations"),
                  self.get("/conversations/nope/messages"), self.get("/conversations", limit=0),
                  self.client.get("/amiigo/v1/nothing-here"), self.client.post("/amiigo/v1/conversations")):
            with self.subTest(status=r.status_code, url=str(r.url)):
                self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertNotEqual(self.client.get("/health").headers.get("cache-control"), "no-store")

    def test_no_log_line_holds_the_phone_the_token_the_text_or_a_link(self):
        self.exchange(CID_A, NOW, customer="my battery smells odd", bot="Please describe the smell.",
                      customer_files=[("image", "s3://" + PHOTO_KEY)])
        self.chats()
        self.messages(CID_A)
        self.messages(CID_A, after=tid(CID_A, 1))
        self.get("/conversations", cursor="bad!")
        self.get("/conversations/nope/messages")
        self.client.get("/amiigo/v1/conversations", headers={"Authorization": "Bearer " + self.rider_token + "x"})
        logged = json.dumps(self.api.log.events)
        for secret in (RIDER_PHONE, RIDER_PHONE[3:], self.rider_token, "my battery smells odd",
                       "Please describe the smell.", "X-Amz-Signature", PHOTO_KEY):
            self.assertNotIn(secret, logged)
        events = [e for e in self.api.log.events if e["event"] in ("amiigo_history_list", "amiigo_history_messages")]
        self.assertEqual([(e["event"], e["outcome"]) for e in events],
                         [("amiigo_history_list", "ok"), ("amiigo_history_messages", "ok"),
                          ("amiigo_history_messages", "ok"), ("amiigo_history_list", "cursor_invalid"),
                          ("amiigo_history_messages", "not_found")])
        self.assertEqual([e["count"] for e in events], [1, 2, 1, 0, 0])
        self.assertEqual({e["rider_hash"] for e in events}, {auth_module.rider_hash_of(RIDER_KEY)})


class MemoryHistoryApiTests(MemoryStores, HistoryApiContract, unittest.TestCase):
    pass


class MongoHistoryApiTests(MongoStores, HistoryApiContract, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
