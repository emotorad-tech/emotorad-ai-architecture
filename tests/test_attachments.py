"""What an attachment becomes in the model's history — in production, not only
in the playground. Text-only turns keep the plain-string shape they had."""

import base64
import unittest

from emotorad_ai.attachments import content_blocks, user_content
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage

PNG = base64.b64encode(b"\x89PNG fake").decode()


def message(text, attachments=()):
    return InboundMessage(
        "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", text,
        attachments=list(attachments),
    )


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
        self.assertEqual(content[-1], {"type": "text", "text": "(attachment)"})

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


class PlaygroundCompatibilityTests(unittest.TestCase):
    def test_the_playground_still_exposes_its_helper_names(self):
        from emotorad_ai import playground, video

        self.assertIs(playground._transcribe_audio, video.transcribe_audio)
        self.assertEqual(playground.VIDEO_FRAMES, video.VIDEO_FRAMES)
        self.assertTrue(callable(playground._extract_frames))
        self.assertTrue(playground._is_video({"mime_type": "video/mp4"}))


if __name__ == "__main__":
    unittest.main()
