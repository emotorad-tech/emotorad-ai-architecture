"""An uploaded image or PDF has to reach the model as a content block, or the
playground's upload widget is testing nothing. Text-only turns keep the exact
history shape they had, so nothing about prefix caching or the offline planner
moves."""

import unittest

from emotorad_ai.agents.base import user_content
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage


def message(text, attachments=()):
    return InboundMessage(
        "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", text,
        attachments=list(attachments),
    )


class UserContentTests(unittest.TestCase):
    def test_text_only_is_still_a_plain_string(self):
        self.assertEqual(user_content(message("battery won't charge")), "battery won't charge")

    def test_a_data_url_image_becomes_a_base64_block_before_the_text(self):
        content = user_content(
            message("what is this light", [Attachment(kind="image", url="data:image/png;base64,AAAA", mime_type="image/png")])
        )
        self.assertEqual(
            content,
            [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {"type": "text", "text": "what is this light"},
            ],
        )

    def test_a_pdf_becomes_a_document_block(self):
        content = user_content(
            message("", [Attachment(kind="document", url="data:application/pdf;base64,QUJD", mime_type="application/pdf")])
        )
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")
        # The API rejects an empty text block, so an attachment with no words
        # still carries one.
        self.assertEqual(content[1], {"type": "text", "text": "(attachment)"})

    def test_an_http_image_is_passed_by_url(self):
        content = user_content(message("see", [Attachment(kind="image", url="https://cdn.test/a.jpg", mime_type="image/jpeg")]))
        self.assertEqual(content[0], {"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.jpg"}})

    def test_unknown_types_are_dropped_not_sent(self):
        content = user_content(message("hi", [Attachment(kind="document", url="data:text/csv;base64,QQ==", mime_type="text/csv")]))
        self.assertEqual(content, "hi")

    def test_an_image_attachment_with_no_mime_type_falls_back_to_its_kind(self):
        # This is every adapter's actual default payload: kind="image",
        # mime_type=None, an http URL with no recognisable extension. Without
        # the kind fallback this photo silently vanishes from the history.
        content = user_content(message("see", [Attachment(kind="image", url="https://cdn.test/photo")]))
        self.assertEqual(content[0], {"type": "image", "source": {"type": "url", "url": "https://cdn.test/photo"}})

    def test_an_image_url_with_no_mime_type_is_guessed_from_its_extension(self):
        content = user_content(message("see", [Attachment(kind="image", url="https://cdn.test/a.jpg")]))
        self.assertEqual(content[0], {"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.jpg"}})

    def test_a_document_url_with_no_mime_type_is_guessed_from_its_extension(self):
        content = user_content(message("see", [Attachment(kind="document", url="https://cdn.test/warranty.pdf")]))
        self.assertEqual(content[0], {"type": "document", "source": {"type": "url", "url": "https://cdn.test/warranty.pdf"}})


if __name__ == "__main__":
    unittest.main()
