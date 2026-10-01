"""Evidence is what reached the model, not what arrived.

The person's decision, 2026-09-29, on the mutation audit's finding: an s3://
photo whose fetch failed reached the model as "[An attachment could not be
retrieved...]" and still counted as evidence, so a fault conclusion, a fault
ticket and a replacement order's `is_sure` all opened on a photo nobody had
seen. A PDF counted too. Now a photo or video counts only when the model is
shown it: an image, video frames, or a video description. An unreadable file
or a PDF does not, and the bot asks once for the photo, then hands over.
"""

import base64
import io
import unittest
from datetime import date

from PIL import Image

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import HANDOVER_TEXT
from emotorad_ai.attachments import UNRETRIEVABLE, VIDEO_DESCRIPTION_LABEL, shows_media
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.guardrails import EVIDENCE_BLOCKED_MESSAGE
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, build_registry

TODAY = date(2026, 7, 28)
FAULT = "That battery is faulty and needs to be replaced."
TICKET = {"category": "battery_charging", "severity": "normal", "description": "Dead after the steps.",
          "idempotency_key": "k1"}
S3_PHOTO = {"kind": "image", "url": "s3://customers/cl/c1/images/x.jpg", "mime_type": "image/jpeg"}


def jpeg_bytes():
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (50, 50, 50)).save(out, format="JPEG")
    return out.getvalue()


class Store:
    """A media store that answers, or fails, and counts what it was asked."""

    def __init__(self, fail=False):
        self.fail = fail
        self.fetched = []

    def get_bytes(self, key):
        self.fetched.append(key)
        if self.fail:
            raise OSError("S3 GetObject failed")
        return jpeg_bytes()


def runtime(replies, media_store=None):
    registry = build_registry(today=TODAY)
    rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(replies)),
                 log=EventLog(path=None), resolver=IdentityResolver(registry),
                 conversations=InMemoryConversationStore(), media_store=media_store)
    rt.conversations.get("c1").route_to("battery_support")
    return rt


def send(rt, text, attachments=()):
    return rt.handle(WebsiteChatAdapter(rt.resolver).to_message({
        "conversation_id": "c1", "session_token": "sess-ananya", "text": text, "attachments": list(attachments)}))


class ShowsMediaTests(unittest.TestCase):
    def test_an_image_or_a_video_description_is_media(self):
        self.assertTrue(shows_media([{"type": "image", "source": {"type": "url", "url": "https://x.test/p.jpg"}}]))
        self.assertTrue(shows_media([{"type": "text", "text": VIDEO_DESCRIPTION_LABEL + " 'clip.mp4', ...]\nA battery."}]))

    def test_text_a_document_or_nothing_is_not(self):
        self.assertFalse(shows_media("my battery won't charge"))
        self.assertFalse(shows_media([{"type": "text", "text": UNRETRIEVABLE}, {"type": "text", "text": "here"}]))
        self.assertFalse(shows_media([{"type": "document", "source": {"type": "base64", "data": "JVBE"}}]))
        self.assertFalse(shows_media([]))


class UnreadableIsNotEvidenceTests(unittest.TestCase):
    def test_a_photo_that_could_not_be_fetched_opens_nothing(self):
        rt = runtime([say(FAULT)], media_store=Store(fail=True))
        reply = send(rt, "here is the terminal", [S3_PHOTO])
        self.assertEqual(reply.handled_by, "guardrail:evidence_post_check")
        self.assertIn(EVIDENCE_BLOCKED_MESSAGE, reply.text)
        self.assertFalse(rt.conversations.peek("c1").evidence_seen)
        [event] = [e for e in rt.log.events if e["event"] == "attachment_not_evidence"]
        self.assertEqual(event["kinds"], ["image"])
        self.assertNotIn("s3://", str(event))

    def test_the_same_photo_fetched_is_evidence(self):
        # The pair to the test above: only the fetch differs.
        rt = runtime([say(FAULT)], media_store=Store())
        reply = send(rt, "here is the terminal", [S3_PHOTO])
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertTrue(rt.conversations.peek("c1").evidence_seen)

    def test_no_fault_ticket_on_a_photo_nobody_could_see(self):
        rt = runtime([call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"), say("Raised.")],
                     media_store=Store(fail=True))
        send(rt, "still dead, photo attached", [S3_PHOTO])
        self.assertEqual(rt.registry.tickets.tickets, {})

    def test_asked_three_times_then_a_person_takes_it(self):
        # Three asks in all (video-first spec, 2026-10-01), then a person.
        rt = runtime([say(FAULT)] * 4, media_store=Store(fail=True))
        replies = [send(rt, text, [S3_PHOTO]) for text in ("here is the terminal", "I sent it again", "and again",
                                                            "once more")]
        for reply in replies[:3]:
            self.assertIn(EVIDENCE_BLOCKED_MESSAGE, reply.text)
        self.assertIn(HANDOVER_TEXT, replies[3].text)
        self.assertTrue(replies[3].escalated)
    def test_a_pdf_is_not_evidence(self):
        pdf = {"kind": "document", "url": "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 invoice").decode()}
        rt = runtime([say(FAULT)])
        reply = send(rt, "here is my invoice", [pdf])
        self.assertEqual(reply.handled_by, "guardrail:evidence_post_check")


class SeenIsEvidenceTests(unittest.TestCase):
    def test_a_described_video_is_evidence_and_is_never_fetched(self):
        store = Store(fail=True)
        clip = {"kind": "video", "url": "s3://customers/cl/c1/videos/v.mp4", "mime_type": "video/mp4",
                "summary": "The charger LED stays dark when plugged into the battery."}
        rt = runtime([say(FAULT)], media_store=store)
        reply = send(rt, "video attached", [clip])
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual(store.fetched, [])

    def test_a_photo_is_fetched_once_for_the_turn(self):
        store = Store()
        rt = runtime([say("Thanks, I can see it.")], media_store=store)
        send(rt, "here is the terminal", [S3_PHOTO])
        self.assertEqual(store.fetched, ["customers/cl/c1/images/x.jpg"])

    def test_a_photo_on_a_turn_no_agent_answered_still_counts_later(self):
        # Triage answers a photo with no words ("What is happening?") without an
        # agent; the photo it put in the history is evidence on the next turn.
        registry = build_registry(today=TODAY)
        rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([say(FAULT)]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry),
                     conversations=InMemoryConversationStore(), media_store=Store())
        first = send(rt, "", [S3_PHOTO])
        self.assertEqual(first.handled_by, "triage")
        self.assertTrue(rt.conversations.peek("c1").evidence_seen)
        second = send(rt, "my battery won't charge")
        self.assertNotEqual(second.handled_by, "guardrail:evidence_post_check")

    def test_the_ticket_in_the_turn_the_photo_arrives_is_raised(self):
        rt = runtime([call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"), say("Raised EM-00001.")],
                     media_store=Store())
        answer = send(rt, "still dead, photo attached", [S3_PHOTO])
        self.assertEqual(answer.ticket_id, "EM-00001")


if __name__ == "__main__":
    unittest.main()
