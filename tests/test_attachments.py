"""Photos a customer sends, and the limits on them.

The agent's evidence gate asks for a picture of the terminal before it will
conclude a fault, and until now there was no way to send one: the attach button
dropped a text note and the conversation walled up. The handoff of 2026-09-20
called this the largest remaining gap.

Nothing is stored. The image is carried inline, passed to the model as vision
content for that turn, and then it is gone. That keeps a storage and retention
decision off the critical path for a surface that is still internal-only.
"""

import base64
import unittest

from emotorad_ai.attachments import (
    MAX_ATTACHMENTS,
    MAX_BYTES,
    AttachmentError,
    to_image_blocks,
    validate,
)

# The smallest valid JPEG header is enough: nothing here decodes the pixels.
_JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg").decode()
_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


class ValidationTests(unittest.TestCase):
    def test_a_jpeg_is_accepted(self):
        self.assertEqual(len(validate([{"kind": "image", "url": _JPEG}])), 1)

    def test_a_png_is_accepted(self):
        self.assertEqual(len(validate([{"kind": "image", "url": _PNG}])), 1)

    def test_nothing_is_fine(self):
        self.assertEqual(validate([]), [])
        self.assertEqual(validate(None), [])

    def test_a_pdf_is_refused(self):
        """The model is being asked to look at a photo. Anything else is either
        a mistake or somebody probing what the endpoint accepts."""
        pdf = "data:application/pdf;base64," + base64.b64encode(b"%PDF").decode()
        with self.assertRaises(AttachmentError):
            validate([{"kind": "image", "url": pdf}])

    def test_a_remote_url_is_refused(self):
        """Only inline data. Fetching a URL the caller supplied would make this
        endpoint fetch arbitrary addresses on their behalf."""
        with self.assertRaises(AttachmentError):
            validate([{"kind": "image", "url": "https://example.com/photo.jpg"}])

    def test_too_many_is_refused(self):
        with self.assertRaises(AttachmentError):
            validate([{"kind": "image", "url": _JPEG}] * (MAX_ATTACHMENTS + 1))

    def test_too_large_is_refused(self):
        """The browser downscales before sending, so anything this big either
        skipped the resize or was not sent by the page at all."""
        huge = "data:image/jpeg;base64," + base64.b64encode(b"x" * (MAX_BYTES + 1)).decode()
        with self.assertRaises(AttachmentError):
            validate([{"kind": "image", "url": huge}])

    def test_rubbish_base64_is_refused(self):
        with self.assertRaises(AttachmentError):
            validate([{"kind": "image", "url": "data:image/jpeg;base64,!!!not base64!!!"}])


class ImageBlockTests(unittest.TestCase):
    def test_it_builds_what_the_api_expects(self):
        block = to_image_blocks(validate([{"kind": "image", "url": _JPEG}]))[0]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["type"], "base64")
        self.assertEqual(block["source"]["media_type"], "image/jpeg")

    def test_the_prefix_is_stripped(self):
        """The API wants the payload, not the data URI wrapper."""
        block = to_image_blocks(validate([{"kind": "image", "url": _JPEG}]))[0]
        self.assertNotIn("data:", block["source"]["data"])

    def test_the_payload_survives_the_round_trip(self):
        block = to_image_blocks(validate([{"kind": "image", "url": _JPEG}]))[0]
        self.assertEqual(base64.b64decode(block["source"]["data"]), b"\xff\xd8\xff\xe0 fake jpeg")


class TheModelActuallySeesThePhotoTests(unittest.TestCase):
    """Carrying the image as far as the agent and no further would be worse than
    not accepting it: the customer would believe they had sent something."""

    def _run(self, attachments):
        from emotorad_ai.config import Settings
        from emotorad_ai.contract import ANONYMOUS, Attachment, Identity, InboundMessage
        from emotorad_ai.identity import IdentityResolver
        from emotorad_ai.llm import ScriptedClaude, say
        from emotorad_ai.observability import EventLog
        from emotorad_ai.runtime import Runtime
        from emotorad_ai.tools.mocks import build_registry

        registry = build_registry()
        llm = ScriptedClaude([say("That terminal looks melted.")])
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False),
            registry=registry,
            llm=llm,
            log=EventLog(path=None),
            resolver=IdentityResolver(registry),
        )
        runtime.conversations.get("photo").route_to("battery_support")
        runtime.handle(
            InboundMessage(
                conversation_id="photo",
                persona="customer",
                identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
                channel="website_chat",
                message_text="here is the terminal",
                attachments=[Attachment(kind="image", url=u) for u in attachments],
            )
        )
        return llm.requests[-1]["messages"]

    def test_the_image_reaches_the_model(self):
        messages = self._run([_JPEG])
        blocks = messages[-1]["content"]
        self.assertIsInstance(blocks, list)
        self.assertTrue(any(b.get("type") == "image" for b in blocks))

    def test_the_customers_words_go_with_it(self):
        """A photo with no sentence around it loses what the customer said about
        it, which is often the half that names the symptom."""
        blocks = self._run([_JPEG])[-1]["content"]
        text = [b for b in blocks if b.get("type") == "text"]
        self.assertEqual(text[0]["text"], "here is the terminal")

    def test_a_photo_with_no_caption_sends_no_empty_text_block(self):
        """Tap attach, pick, send. The API rejects an empty text block, so a
        photo on its own must go as image blocks alone."""
        from emotorad_ai.config import Settings
        from emotorad_ai.contract import ANONYMOUS, Attachment, Identity, InboundMessage
        from emotorad_ai.identity import IdentityResolver
        from emotorad_ai.llm import ScriptedClaude, say
        from emotorad_ai.observability import EventLog
        from emotorad_ai.runtime import Runtime
        from emotorad_ai.tools.mocks import build_registry

        registry = build_registry()
        llm = ScriptedClaude([say("I can see the terminal.")])
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False),
            registry=registry, llm=llm, log=EventLog(path=None),
            resolver=IdentityResolver(registry),
        )
        runtime.conversations.get("nocap").route_to("battery_support")
        runtime.handle(InboundMessage(
            conversation_id="nocap", persona="customer",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
            channel="website_chat", message_text="",
            attachments=[Attachment(kind="image", url=_JPEG)],
        ))
        blocks = llm.requests[-1]["messages"][-1]["content"]
        self.assertTrue(all(b.get("type") == "image" for b in blocks), blocks)

    def test_a_turn_without_a_photo_is_unchanged(self):
        """Every existing channel sends plain text and must keep working
        exactly as it did."""
        self.assertEqual(self._run([])[-1]["content"], "here is the terminal")

if __name__ == "__main__":
    unittest.main()
