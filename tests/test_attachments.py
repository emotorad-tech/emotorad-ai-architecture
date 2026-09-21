"""What an attachment becomes in the model's history — in production, not only
in the playground.

Two inbound paths meet in one module, so they are tested together here. The
inline `/chat` path (`validate` + `to_image_blocks`) carries a photo in the
request and stores it nowhere; the media path (`content_blocks` /
`user_content`) carries `Attachment` objects from a `data:` URL, an `s3://` id
minted by `POST /uploads`, or an http(s) URL. Text-only turns keep the plain
string shape they had, on both.
"""

import base64
import io
import os
import unittest
from unittest import mock

from PIL import Image

from emotorad_ai import attachments
from emotorad_ai.attachments import (
    MAX_ATTACHMENTS,
    MAX_BYTES,
    TRANSCRIBE_ENV,
    AttachmentError,
    content_blocks,
    fit_for_model,
    to_image_blocks,
    user_content,
    validate,
)
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage

# The smallest valid JPEG header is enough: nothing here decodes the pixels.
_JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg").decode()
_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()

PNG = base64.b64encode(b"\x89PNG fake").decode()


def _png_bytes(width, height):
    out = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(out, format="PNG")
    return out.getvalue()


def message(text, attachments=()):
    return InboundMessage(
        "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", text,
        attachments=list(attachments),
    )


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


class UserContentTests(unittest.TestCase):
    def test_text_only_is_still_a_plain_string(self):
        self.assertEqual(user_content(message("battery won't charge")), "battery won't charge")

    def test_a_data_url_image_becomes_a_base64_block_before_the_text(self):
        content = user_content(message("what is this light", [Attachment("image", "data:image/png;base64," + PNG, "image/png")]))
        self.assertEqual(content[0], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}})
        self.assertEqual(content[-1], {"type": "text", "text": "what is this light"})

    def test_an_s3_key_is_fetched_server_side_and_sent_as_base64(self):
        fetched = []

        def fetch(key):
            fetched.append(key)
            return b"\x89PNG fake"

        content = user_content(message("", [Attachment("image", "s3://customers/clu_1/conv_1/images/upl_1.png", "image/png")]), fetch=fetch)
        self.assertEqual(fetched, ["customers/clu_1/conv_1/images/upl_1.png"])
        self.assertEqual(content[0]["source"], {"type": "base64", "media_type": "image/png", "data": PNG})
        # No caption, so no text block: the merged module follows the inline
        # path's rule rather than inventing an "(attachment)" placeholder the
        # customer never typed.
        self.assertEqual(len(content), 1)

    def test_a_pdf_becomes_a_document_block(self):
        content = content_blocks([Attachment("document", "data:application/pdf;base64,QUJD", "application/pdf")])
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")

    def test_an_http_image_is_passed_by_url(self):
        content = content_blocks([Attachment("image", "https://cdn.test/a.jpg", "image/jpeg")])
        self.assertEqual(content, [{"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.jpg"}}])

    def test_a_fetch_failure_becomes_a_warning_block_not_an_exception(self):
        def fetch(key):
            raise RuntimeError("boom")

        content = content_blocks([Attachment("image", "s3://customers/x/y/images/z.png", "image/png")], fetch=fetch)
        self.assertEqual(content, [{"type": "text", "text": "[An attachment could not be retrieved; do not describe it.]"}])

    def test_an_s3_key_with_no_fetcher_is_a_warning_block(self):
        content = content_blocks([Attachment("image", "s3://customers/x/y/images/z.png", "image/png")])
        self.assertEqual(content[0]["type"], "text")

    def test_an_unreadable_video_says_so(self):
        content = content_blocks([Attachment("video", "data:video/mp4;base64,AAAA", "video/mp4")])
        self.assertEqual(len(content), 1)
        self.assertIn("could not be read", content[0]["text"])

    def test_unknown_types_are_dropped(self):
        self.assertEqual(content_blocks([Attachment("document", "data:text/csv;base64,QQ==", "text/csv")]), [])


class OneDownscaleForBothPathsTests(unittest.TestCase):
    """Integrating the two branches left one image-block builder. A photo the
    inline path carries gets the same 1600px/3MB treatment as one fetched from
    S3 — before, only the media path downscaled, so a full-resolution photo
    posted straight at /message went to the model untouched."""

    def test_an_inline_photo_over_the_edge_cap_is_downscaled(self):
        big = base64.b64encode(_png_bytes(4000, 3000)).decode()
        block = to_image_blocks(validate([{"kind": "image", "url": "data:image/png;base64," + big}]))[0]
        self.assertEqual(block["source"]["media_type"], "image/jpeg")
        with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as image:
            self.assertLessEqual(max(image.size), 1600)

    def test_a_photo_within_the_cap_is_untouched_on_both_paths(self):
        small = _png_bytes(200, 200)
        inline = to_image_blocks(validate([
            {"kind": "image", "url": "data:image/png;base64," + base64.b64encode(small).decode()}
        ]))[0]
        fetched = content_blocks(
            [Attachment("image", "s3://customers/c/x/images/u.png", "image/png")],
            fetch=lambda key: small,
        )[0]
        self.assertEqual(inline, fetched)
        self.assertEqual(inline["source"]["media_type"], "image/png")


class FitForModelTests(unittest.TestCase):
    def test_a_large_image_is_downscaled_to_jpeg_within_the_edge_cap(self):
        data, mime = fit_for_model(_png_bytes(4000, 3000), "image/png")
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(data)) as image:
            self.assertLessEqual(max(image.size), 1600)

    def test_a_small_image_passes_through_unchanged(self):
        original = _png_bytes(200, 200)
        data, mime = fit_for_model(original, "image/png")
        self.assertEqual(mime, "image/png")
        self.assertEqual(data, original)


class TranscriptionGateTests(unittest.TestCase):
    def _video_content(self):
        with mock.patch.object(attachments.video, "extract_audio", return_value=b"fake wav"), mock.patch.object(
            attachments.video, "extract_frames", return_value=["deadbeef"]
        ), mock.patch.object(
            attachments.video, "transcribe_audio", return_value={"text": "it wheezes", "language": "hi"}
        ) as transcribe:
            content = content_blocks([Attachment("video", "data:video/mp4;base64,AAAA", "video/mp4")])
        return content, transcribe

    def test_transcription_is_off_by_default_in_the_api_path(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TRANSCRIBE_ENV, None)
            content, transcribe = self._video_content()
        transcribe.assert_not_called()
        self.assertFalse(any("wheezes" in block.get("text", "") for block in content))

    def test_transcription_runs_when_the_env_var_is_set(self):
        with mock.patch.dict(os.environ, {TRANSCRIBE_ENV: "1"}):
            content, transcribe = self._video_content()
        transcribe.assert_called_once()
        self.assertTrue(any("wheezes" in block.get("text", "") for block in content))


class PlaygroundCompatibilityTests(unittest.TestCase):
    def test_the_playground_still_exposes_its_helper_names(self):
        from emotorad_ai import playground, video

        self.assertIs(playground._transcribe_audio, video.transcribe_audio)
        self.assertEqual(playground.VIDEO_FRAMES, video.VIDEO_FRAMES)
        self.assertTrue(callable(playground._extract_frames))
        self.assertTrue(playground._is_video({"mime_type": "video/mp4"}))


if __name__ == "__main__":
    unittest.main()
