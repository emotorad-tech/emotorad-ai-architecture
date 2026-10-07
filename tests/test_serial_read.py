"""Reading the serial off a customer's photo (serial_read.py, 7 October 2026).

Once the battery bot has asked for the serial photos, each stored photo on
that bike is read by Gemini after the reply, and a reading of the battery's
sticker or the controller's label is kept in `serial_readings`, unconfirmed,
and erased with the person or the conversation. The serial is never logged.
"""

import base64
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import mongomock
from fastapi.testclient import TestClient

from emotorad_ai import serial_read
from emotorad_ai.contract import Attachment
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.openrouter import CHAT_PATH, OpenRouterRateLimited
from emotorad_ai.serial_read import (OpenRouterSerialReader, SerialReadError, parse, photos_to_read, read_photos,
                                     reading_doc, serial_reader_from_env)
from emotorad_ai.stores.mongo import MongoConversationStore, ensure_indexes
from tests.test_api_media_persistence import _Store, fresh_api, jpeg_data_url

FRAME = "EMXP2025004417"  # Ananya's EMX Plus (fixtures)
BATTERY = {"part": "battery", "serial": "EMIN2407150123", "legible": True, "seal": None}
NOW = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, answer=None, text=None, error=None):
        self.text = text if text is not None else json.dumps(answer if answer is not None else BATTERY)
        self.error, self.posts = error, []

    def post(self, path, body, timeout=None):
        self.posts.append({"path": path, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return {"model": serial_read.OPENROUTER_SERIAL_MODEL,
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": self.text}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 30}}


class FakeReader:
    provider, model = "openrouter", "google/gemini-3.8-flash"

    def __init__(self, answers=(BATTERY,), error=None):
        self.answers, self.error, self.seen = list(answers), error, []

    def read(self, data, mime, references=None):
        self.seen.append((data, mime))
        self.references = references
        if self.error is not None:
            raise self.error
        return self.answers[min(len(self.seen), len(self.answers)) - 1]


class Events:
    def __init__(self):
        self.events = []

    def __call__(self, name, conversation_id, **fields):
        self.events.append(dict(fields, event=name, conversation_id=conversation_id))


def asked_state(frame=FRAME, serial=True, melt=False):
    return SimpleNamespace(selected_frame=frame, serials_asked_frames=[frame] if serial else [],
                           melt_asked_frames=[frame] if melt else [], user_key="cluster-1")


def photo(key="customers/cl/c1/images/a.jpg", mime="image/jpeg", kind="image"):
    return Attachment(kind=kind, url="s3://" + key, mime_type=mime)


class ReaderTests(unittest.TestCase):
    def test_the_photo_goes_inline_once_asking_for_json_with_no_retention(self):
        transport = FakeTransport()
        self.assertEqual(OpenRouterSerialReader(transport).read(b"\xff\xd8jpeg", "image/jpeg"), BATTERY)
        [post] = transport.posts
        self.assertEqual((post["path"], post["timeout"]), (CHAT_PATH, serial_read.TIMEOUT_SECONDS))
        body = post["body"]
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": serial_read.PROMPT})
        self.assertEqual(content[1]["image_url"]["url"],
                         "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode())

    def test_the_prompt_names_the_patent_number_and_the_controllers_s_n(self):
        self.assertIn("Patent No", serial_read.PROMPT)
        self.assertIn("S/N", serial_read.PROMPT)

    def test_references_go_first_labelled_as_ours_then_the_customers_photo(self):
        transport = FakeTransport()
        ref = (b"RIFFref", "image/webp", "Example: the frame number sticker on the seat tube")
        OpenRouterSerialReader(transport).read(b"jpeg", "image/jpeg", references=[ref])
        content = transport.posts[0]["body"]["messages"][0]["content"]
        self.assertEqual([part.get("text") for part in content if part["type"] == "text"],
                         [serial_read.PROMPT, serial_read.REFERENCES_INTRO, ref[2], serial_read.CUSTOMER_PHOTO_LINE])
        self.assertEqual(content[-1]["image_url"]["url"], "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode())
        self.assertIn("Never read a serial from them", serial_read.REFERENCES_INTRO)

    def test_the_prompt_names_the_frame_and_the_seal(self):
        for words in ("frame number sticker", "warranty_seal", "intact", "torn", "unclear"):
            self.assertIn(words, serial_read.PROMPT)

    def test_a_provider_failure_is_its_code_only(self):
        reader = OpenRouterSerialReader(FakeTransport(error=OpenRouterRateLimited("rate_limited")))
        with self.assertRaises(SerialReadError) as caught:
            reader.read(b"j", "image/jpeg")
        self.assertEqual(str(caught.exception), "rate_limited")

    def test_a_photo_too_large_is_not_sent(self):
        transport = FakeTransport()
        with self.assertRaises(SerialReadError):
            OpenRouterSerialReader(transport).read(b"x" * (serial_read.INLINE_LIMIT + 1), "image/jpeg")
        self.assertEqual(transport.posts, [])


class ParseTests(unittest.TestCase):
    def test_a_clear_battery_sticker(self):
        self.assertEqual(parse(json.dumps(BATTERY)), BATTERY)

    def test_json_in_a_code_fence_is_read(self):
        text = "```json\n" + json.dumps({"part": "controller", "serial": "KT36-2210-0456", "legible": True}) + "\n```"
        self.assertEqual(parse(text), {"part": "controller", "serial": "KT36-2210-0456", "legible": True, "seal": None})

    def test_an_unknown_part_is_none_and_keeps_no_serial(self):
        self.assertEqual(parse(json.dumps({"part": "display", "serial": "ABC12345", "legible": True})),
                         {"part": "none", "serial": None, "legible": False, "seal": None})

    def test_a_motor_serial(self):
        self.assertEqual(parse(json.dumps({"part": "motor", "serial": "220516001234", "legible": True})),
                         {"part": "motor", "serial": "220516001234", "legible": True, "seal": None})
        self.assertIn("rear hub motor", serial_read.PROMPT)
        self.assertIn("motor_serial_number", serial_read.REFERENCE_KEYS["serial"])

    def test_something_that_is_not_a_serial_is_dropped(self):
        for serial in ("Patent No. 2023 1 0456", "EM", "<script>", "x" * 41, 12345678):
            self.assertEqual(parse(json.dumps({"part": "battery", "serial": serial, "legible": True})),
                             {"part": "battery", "serial": None, "legible": False, "seal": None}, serial)

    def test_a_frame_number(self):
        self.assertEqual(parse(json.dumps({"part": "frame", "serial": "EMXP2025004417", "legible": True})),
                         {"part": "frame", "serial": "EMXP2025004417", "legible": True, "seal": None})

    def test_the_seal_is_intact_torn_or_unclear_and_never_has_a_serial(self):
        for said, kept in (("intact", "intact"), ("torn", "torn"), ("unclear", "unclear"), ("broken", "unclear"),
                           (None, "unclear")):
            reading = parse(json.dumps({"part": "warranty_seal", "serial": "EMIN123456", "legible": True,
                                        "seal": said}))
            self.assertEqual(reading, {"part": "warranty_seal", "serial": None, "legible": False, "seal": kept}, said)

    def test_a_seal_on_another_part_is_dropped(self):
        self.assertIsNone(parse(json.dumps(dict(BATTERY, seal="torn")))["seal"])

    def test_only_a_real_true_is_legible(self):
        for legible in ("yes", 1, None):
            self.assertFalse(parse(json.dumps(dict(BATTERY, legible=legible)))["legible"], legible)

    def test_not_json_is_bad_json(self):
        for text in ("the serial is EMIN123", "[1, 2]"):
            with self.assertRaises(SerialReadError):
                parse(text)


class PhotosToReadTests(unittest.TestCase):
    def test_stored_photos_on_an_asked_bike(self):
        self.assertEqual(photos_to_read([photo()], asked_state()), [("customers/cl/c1/images/a.jpg", "image/jpeg")])

    def test_the_melt_ask_counts_as_asked(self):
        self.assertEqual(len(photos_to_read([photo()], asked_state(serial=False, melt=True))), 1)

    def test_nothing_before_the_ask(self):
        self.assertEqual(photos_to_read([photo()], asked_state(serial=False)), [])
        self.assertEqual(photos_to_read([photo()], None), [])

    def test_with_no_bike_chosen_the_conversations_ask_counts(self):
        state = SimpleNamespace(selected_frame=None, serials_asked_frames=["-"], melt_asked_frames=[])
        self.assertEqual(len(photos_to_read([photo()], state)), 1)
        state.serials_asked_frames = []
        self.assertEqual(photos_to_read([photo()], state), [])

    def test_a_video_or_an_unstored_photo_is_not_read(self):
        unstored = Attachment(kind="image", url=jpeg_data_url(), mime_type="image/jpeg")
        video = photo(key="customers/cl/c1/videos/a.mp4", mime="video/mp4", kind="video")
        self.assertEqual(photos_to_read([unstored, video], asked_state()), [])


class ReadPhotosTests(unittest.TestCase):
    def run_it(self, reader, photos, store=None, load=None):
        events = Events()
        store = InMemoryConversationStore() if store is None else store
        kept = read_photos(reader, photos, load or (lambda key: b"bytes of " + key.encode()), store,
                           conversation_id="c1", user_key="cluster-1", frame_number=FRAME, emit=events,
                           now=lambda: NOW)
        return kept, store, events.events

    def test_a_reading_is_kept_unconfirmed_with_the_bike_and_the_photos_key(self):
        kept, store, events = self.run_it(FakeReader(), [("customers/cl/c1/images/a.jpg", "image/jpeg")])
        self.assertEqual(kept, 1)
        self.assertEqual(store.serial_readings_of("c1"), [{
            "_id": "customers/cl/c1/images/a.jpg", "conversation_id": "c1", "user_key": "cluster-1",
            "frame_number": FRAME, "part": "battery", "serial": "EMIN2407150123", "legible": True, "seal": None,
            "media_key": "customers/cl/c1/images/a.jpg", "model": "google/gemini-3.8-flash",
            "read_at": NOW.isoformat(), "confirmed": False, "source": "ocr",
        }])
        self.assertEqual(events, [{"event": "serial_read", "conversation_id": "c1", "part": "battery",
                                   "legible": True, "seal": None}])

    def test_the_references_are_shown_and_a_missing_one_is_named(self):
        class Refs:
            def for_component(self, component):
                assert component == "serial"
                return [(b"r", "image/webp", "ref")], ["frame_number_sticker"]

        reader = FakeReader()
        events = Events()
        read_photos(reader, [("k.jpg", "image/jpeg")], lambda key: b"x", InMemoryConversationStore(),
                    conversation_id="c1", user_key="u", frame_number=FRAME, emit=events, references=Refs())
        self.assertEqual(reader.references, [(b"r", "image/webp", "ref")])
        self.assertEqual(events.events[0], {"event": "serial_read_references_missing", "conversation_id": "c1",
                                            "keys": ["frame_number_sticker"]})

    def test_a_seal_reading_is_kept_with_its_state(self):
        seal = {"part": "warranty_seal", "serial": None, "legible": False, "seal": "torn"}
        kept, store, events = self.run_it(FakeReader(answers=(seal,)), [("k1.jpg", "image/jpeg")])
        self.assertEqual(kept, 1)
        [reading] = store.serial_readings_of("c1")
        self.assertEqual((reading["part"], reading["seal"], reading["serial"]), ("warranty_seal", "torn", None))

    def test_the_serial_is_never_logged(self):
        _, _, events = self.run_it(FakeReader(), [("k1.jpg", "image/jpeg")])
        self.assertNotIn("EMIN2407150123", json.dumps(events))

    def test_a_photo_of_neither_part_is_not_kept(self):
        none = {"part": "none", "serial": None, "legible": False}
        kept, store, _ = self.run_it(FakeReader(answers=(none,)), [("k1.jpg", "image/jpeg")])
        self.assertEqual((kept, store.serial_readings_of("c1")), (0, []))

    def test_the_same_photo_read_twice_keeps_one_reading(self):
        store = InMemoryConversationStore()
        self.run_it(FakeReader(), [("k1.jpg", "image/jpeg")], store=store)
        self.run_it(FakeReader(), [("k1.jpg", "image/jpeg")], store=store)
        self.assertEqual(len(store.serial_readings_of("c1")), 1)

    def test_a_failure_is_logged_by_its_code_and_the_next_photo_goes_on(self):
        reader = FakeReader()
        calls = []

        def load(key):
            calls.append(key)
            if key == "bad.jpg":
                raise OSError("bucket says no")
            return b"ok"

        kept, _, events = self.run_it(reader, [("bad.jpg", "image/jpeg"), ("good.jpg", "image/jpeg")], load=load)
        self.assertEqual((kept, calls), (1, ["bad.jpg", "good.jpg"]))
        self.assertEqual(events[0], {"event": "serial_read_failed", "conversation_id": "c1", "error": "OSError"})
        _, _, events = self.run_it(FakeReader(error=SerialReadError("bad_json")), [("k.jpg", "image/jpeg")])
        self.assertEqual(events, [{"event": "serial_read_failed", "conversation_id": "c1", "error": "bad_json"}])

    def test_a_store_that_fails_is_logged_not_raised(self):
        class Broken:
            def add_serial_reading(self, doc):
                raise RuntimeError("down")

        kept, _, events = self.run_it(FakeReader(), [("k.jpg", "image/jpeg")], store=Broken())
        self.assertEqual(kept, 0)
        self.assertEqual(events[-1]["error"], "store:RuntimeError")


class SwitchTests(unittest.TestCase):
    def test_on_only_with_the_serial_ask_and_the_key(self):
        self.assertIsNone(serial_reader_from_env({"EMOTORAD_SERIAL_ASK": "on"}))
        self.assertIsNone(serial_reader_from_env({"OPENROUTER_API_KEY": "k"}))
        self.assertIsNone(serial_reader_from_env({"EMOTORAD_SERIAL_ASK": "yes", "OPENROUTER_API_KEY": "k"}))
        reader = serial_reader_from_env({"EMOTORAD_SERIAL_ASK": "on", "OPENROUTER_API_KEY": "k"})
        self.assertEqual(reader.provider, "openrouter")


def a_reading(conversation_id, key, user_key="cluster-1"):
    return reading_doc(conversation_id, user_key, FRAME, key, BATTERY, "m", NOW.isoformat())


class StoreErasureContract:
    def test_readings_are_erased_with_the_conversation(self):
        store = self.make_store()
        store.add_serial_reading(a_reading("c1", "k1"))
        store.add_serial_reading(a_reading("c2", "k2"))
        self.assertEqual(store.delete_conversation("c1")["serial_readings"], 1)
        self.assertEqual(store.serial_readings_of("c1"), [])
        self.assertEqual(len(store.serial_readings_of("c2")), 1)

    def test_readings_are_erased_with_the_person(self):
        store = self.make_store()
        store.add_serial_reading(a_reading("c1", "k1"))
        store.add_serial_reading(a_reading("c1", "k2"))
        store.add_serial_reading(a_reading("c9", "k9", user_key="someone-else"))
        self.assertIn("c1", store.conversations_of("cluster-1"))
        self.assertEqual(store.delete_person("cluster-1", dry_run=True)["serial_readings"], 2)
        self.assertEqual(len(store.serial_readings_of("c1")), 2)
        self.assertEqual(store.delete_person("cluster-1")["serial_readings"], 2)
        self.assertEqual(store.serial_readings_of("c1"), [])
        self.assertEqual(len(store.serial_readings_of("c9")), 1)


class InMemoryErasureTests(StoreErasureContract, unittest.TestCase):
    def make_store(self):
        return InMemoryConversationStore()


class MongoErasureTests(StoreErasureContract, unittest.TestCase):
    def make_store(self):
        db = mongomock.MongoClient()["emotorad_ai"]
        ensure_indexes(db)
        return MongoConversationStore(db)


class InlinePool:
    """Runs the reader at once and hands back a finished future, as the
    server's pool does once the read is done."""

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def post(self, reader):
        body = {"conversation_id": "c1", "session_token": "sess-ananya", "text": "here are the photos",
                "attachments": [{"kind": "image", "url": jpeg_data_url()}]}
        with mock.patch.object(self.api, "SERIAL_READER", reader), \
                mock.patch.object(self.api, "SERIAL_READ_POOL", InlinePool()):
            r = self.client.post("/message", json=body)
        self.assertEqual(r.status_code, 200, r.text)

    def ask(self):
        state = self.api.stores.conversations.get("c1")
        state.select_bike(FRAME, "EMX Plus")
        state.serials_asked_frames.append(FRAME)
        self.api.stores.conversations.save(state)

    def test_a_photo_after_the_ask_is_read_and_kept(self):
        self.ask()
        reader = FakeReader()
        self.post(reader)
        [(data, mime)] = reader.seen
        [key] = list(self.store.objects)
        self.assertEqual((data, mime), (self.store.objects[key], "image/jpeg"))
        [reading] = self.api.stores.conversations.serial_readings_of("c1")
        self.assertEqual((reading["media_key"], reading["frame_number"], reading["serial"]),
                         (key, FRAME, "EMIN2407150123"))

    def test_a_photo_before_the_ask_is_not_read(self):
        reader = FakeReader()
        self.post(reader)
        self.assertEqual(reader.seen, [])

    def test_switched_off_nothing_is_read(self):
        self.ask()
        self.post(None)
        self.assertEqual(self.api.stores.conversations.serial_readings_of("c1"), [])


if __name__ == "__main__":
    unittest.main()
