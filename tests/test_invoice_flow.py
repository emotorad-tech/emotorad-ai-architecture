"""The invoice, end to end (spec 2026-10-08, section 3): the service reads the
OMS copy or the customer's upload and keeps the reading; after the agent's
reply, code raises the warranty-proof ticket and tells the customer; the API
starts the reads before the turn."""

import json
import unittest
from datetime import date
from unittest import mock

from emotorad_ai.config import Settings
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.invoice_ocr import InvoiceReadError, InvoiceService, customer_line, assess
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.oms import OMSUnavailable
from tests.test_invoice_ocr import GOOD
from tests.test_serial_read import InlinePool

PHONE = "+919876543210"
FRAME = "EMXP2025004417"
FILE_ID = "0b6c6e3e-0000-4000-8000-000000000001"
TODAY = date(2025, 10, 1)
CLUSTER = "cl-1"


class FakeReader:
    model = "google/gemini-3.8-flash"

    def __init__(self, found=GOOD, error=None):
        self.found, self.error, self.calls = found, error, []

    def read(self, data, mime, frame_number):
        self.calls.append((data, mime, frame_number))
        if self.error:
            raise self.error
        return dict(self.found)


class FakeOmsDb:
    def __init__(self, row):
        self._row = row

    def row(self, phone, frame):
        return dict(self._row) if self._row and frame == FRAME else None

    def invoice_file(self, phone, frame):
        return (self._row or {}).get("invoice_image") if frame == FRAME else None


class FakeOmsClient:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def download_file(self, file_id):
        self.calls.append(file_id)
        if self.error:
            raise self.error
        return b"%PDF-invoice", "application/pdf"


class FakeBucket:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = data

    def get_bytes(self, key):
        return self.objects[key]


def service(row=None, reader=None, client=None, conversations=None, events=None, persisted=None):
    events = [] if events is None else events
    persisted = [] if persisted is None else persisted
    row = {"frame_number": FRAME, "purchase_date": None, "invoice_image": FILE_ID} if row is None else row
    return InvoiceService(
        reader=reader or FakeReader(), oms_db=FakeOmsDb(row), oms_client=client or FakeOmsClient(),
        media_store=FakeBucket(), conversations=conversations or InMemoryConversationStore(),
        emit=lambda name, cid, **fields: events.append(dict(fields, event=name)),
        persist=lambda **record: persisted.append(record), pool=InlinePool(), today=lambda: TODAY,
    )


class ServiceTests(unittest.TestCase):
    def test_the_oms_invoice_is_read_kept_and_its_copy_recorded(self):
        persisted = []
        svc = service(persisted=persisted)
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        [reading] = svc.conversations.invoice_readings_of("c1")
        self.assertEqual(reading["_id"], "oms:" + FILE_ID)
        self.assertEqual((reading["source"], reading["frame_number"], reading["confident"],
                          reading["purchase_date"], reading["told"]), ("oms", FRAME, True, "2025-03-12", False))
        [record] = persisted
        self.assertTrue(record["key"].startswith("customers/%s/c1/docs/" % CLUSTER))
        self.assertEqual(reading["copy_key"], record["key"])
        self.assertEqual(svc.media_store.objects[record["key"]], b"%PDF-invoice")

    def test_it_is_read_once(self):
        client = FakeOmsClient()
        svc = service(client=client)
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        self.assertEqual(client.calls, [FILE_ID])

    def test_a_download_that_fails_is_logged_by_its_code(self):
        events = []
        svc = service(client=FakeOmsClient(error=OMSUnavailable("OMS returned HTTP 502")), events=events)
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        self.assertEqual(svc.conversations.invoice_readings_of("c1"), [])
        self.assertEqual(events, [{"event": "invoice_read_failed", "error": "OMSUnavailable", "source": "oms"}])

    def test_a_reading_that_fails_is_logged_by_its_code(self):
        events = []
        svc = service(reader=FakeReader(error=InvoiceReadError("bad_json")), events=events)
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        self.assertEqual(events[-1], {"event": "invoice_read_failed", "error": "bad_json", "source": "oms"})

    def test_an_upload_that_is_an_invoice_is_kept(self):
        svc = service(row={"frame_number": FRAME, "purchase_date": None, "invoice_image": None})
        svc.media_store.objects["customers/cl-1/c1/images/u1.jpg"] = b"jpeg"
        svc.read_upload("c1", "user-1", FRAME, "customers/cl-1/c1/images/u1.jpg", "image/jpeg")
        [reading] = svc.conversations.invoice_readings_of("c1")
        self.assertEqual((reading["_id"], reading["source"]), ("customers/cl-1/c1/images/u1.jpg", "customer"))

    def test_an_upload_that_is_not_an_invoice_is_not_kept(self):
        events = []
        svc = service(reader=FakeReader(found={"is_invoice": False}), events=events)
        svc.media_store.objects["k.jpg"] = b"jpeg"
        svc.read_upload("c1", "user-1", FRAME, "k.jpg", "image/jpeg")
        self.assertEqual(svc.conversations.invoice_readings_of("c1"), [])
        self.assertEqual(events[-1], {"event": "invoice_read", "source": "customer", "is_invoice": False,
                                      "confident": False})

    def test_no_value_is_ever_logged(self):
        events = []
        svc = service(events=events)
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        logged = json.dumps(events)
        for value in ("12/03/2025", FRAME, "Ride Shop", FILE_ID, PHONE):
            self.assertNotIn(value, logged)


class Chat:
    def __init__(self, replies, svc):
        self.llm = ScriptedClaude(list(replies))
        self.log = EventLog(path=None)
        self.registry = build_registry(today=TODAY)
        self.conversations = svc.conversations
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm, log=self.log,
            resolver=IdentityResolver(self.registry), conversations=self.conversations, invoice=svc,
        )
        state = self.conversations.get("c1")
        state.select_bike(FRAME, "EMX Plus")
        state.route_to("battery_support")

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=VERIFIED, phone=PHONE, em_aid="aid-1"),
        ))

    def proofs(self):
        return [t for t in self.registry.tickets.tickets.values() if t.get("kind") == "warranty_proof"]


class RuntimeTests(unittest.TestCase):
    def test_a_confident_reading_raises_the_ticket_and_tells_the_customer_once(self):
        svc = service()
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        chat = Chat([say("Let me check your invoice."), say("Anything else?")], svc)
        reply = chat.say("is my battery covered?")
        line = customer_line(assess(GOOD, FRAME, TODAY), TODAY, hindi=False)
        self.assertTrue(reply.text.endswith(line), reply.text)
        [ticket] = chat.proofs()
        self.assertIn("Invoice read by AI (source: OMS)", ticket["description"])
        self.assertIn("2025-03-12", ticket["description"])
        [reading] = svc.conversations.invoice_readings_of("c1")
        self.assertTrue(reading["told"])
        self.assertEqual(reading["ticket_id"], ticket["ticket_id"])
        again = chat.say("thanks")
        self.assertNotIn("Going by your invoice", again.text)
        self.assertEqual(len(chat.proofs()), 1)

    def test_a_reading_that_is_not_confident_names_no_date(self):
        svc = service(reader=FakeReader(found=dict(GOOD, legible=False)))
        svc.read_from_oms("c1", "user-1", CLUSTER, PHONE, FRAME)
        chat = Chat([say("Let me check your invoice.")], svc)
        reply = chat.say("is my battery covered?")
        self.assertTrue(reply.text.endswith("A support executive will confirm your warranty."), reply.text)
        self.assertNotIn("12 March", reply.text)
        self.assertIn("confident: no (not legible)", chat.proofs()[0]["description"])

    def test_with_no_reading_the_reply_is_the_agents(self):
        svc = service()
        chat = Chat([say("Please check the socket.")], svc)
        self.assertTrue(chat.say("my battery won't charge").text.endswith("Please check the socket."))
        self.assertEqual(chat.proofs(), [])


def message(attachments=()):
    return InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="here",
                          identity=Identity(strength=VERIFIED, phone=PHONE, em_aid="aid-1"),
                          attachments=list(attachments))


class Recorder:
    """The service as the API sees it, recording the jobs it is given."""

    def __init__(self, row):
        self._row, self.oms, self.uploads = row, [], []

    def bike(self, phone, frame):
        return dict(self._row) if self._row is not None else None

    def read_from_oms(self, conversation_id, user_key, cluster_id, phone, frame):
        self.oms.append(frame)

    def read_upload(self, conversation_id, user_key, frame, key, mime):
        self.uploads.append((key, mime))

    def start(self, jobs):
        for job in jobs:
            job()


class ApiTests(unittest.TestCase):
    def setUp(self):
        from tests.test_api_health import fresh_api

        self.api = fresh_api({"EMOTORAD_OMS_PG_DSN": ""})
        state = self.api.stores.conversations.get("c1")
        state.select_bike(FRAME, "EMX Plus")
        self.api.stores.conversations.save(state)

    def run_with(self, row, attachments=()):
        recorder = Recorder(row)
        with mock.patch.object(self.api, "INVOICE", recorder):
            self.api._start_invoice_reads(message(attachments))
        return recorder

    def test_a_dated_bike_reads_nothing(self):
        recorder = self.run_with({"frame_number": FRAME, "purchase_date": "2025-03-12", "invoice_image": FILE_ID})
        self.assertEqual((recorder.oms, recorder.uploads), ([], []))

    def test_an_undated_bike_with_an_invoice_on_file_reads_it(self):
        recorder = self.run_with({"frame_number": FRAME, "purchase_date": None, "invoice_image": FILE_ID})
        self.assertEqual(recorder.oms, [FRAME])

    def test_without_one_the_customers_photo_or_pdf_is_read(self):
        photo = Attachment(kind="image", url="s3://customers/cl-1/c1/images/u1.jpg", mime_type="image/jpeg")
        pdf = Attachment(kind="document", url="s3://customers/cl-1/c1/docs/u2.pdf", mime_type="application/pdf")
        inline = Attachment(kind="image", url="data:image/jpeg;base64,AAAA", mime_type="image/jpeg")
        recorder = self.run_with({"frame_number": FRAME, "purchase_date": None, "invoice_image": None},
                                 [photo, pdf, inline])
        self.assertEqual(recorder.uploads, [("customers/cl-1/c1/images/u1.jpg", "image/jpeg"),
                                            ("customers/cl-1/c1/docs/u2.pdf", "application/pdf")])

    def test_off_nothing_is_read(self):
        with mock.patch.object(self.api, "INVOICE", None):
            self.api._start_invoice_reads(message())


if __name__ == "__main__":
    unittest.main()
