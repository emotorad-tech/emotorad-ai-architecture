"""Structural checks on the chat page.

There is no browser in this suite, so these assert the properties that were
actually wrong rather than pretending to test rendering. They exist because the
page was only ever opened on a laptop until 20 September 2026, when opening it
on a phone showed it was rendering a desktop mockup frame: a hard-coded
390x844 card inside 24px of padding, which overflowed a 412px viewport
horizontally and put a second scroller around the chat's own.
"""

import pathlib
import re
import unittest

CHAT = pathlib.Path(__file__).resolve().parents[1] / "web" / "emotorad-support-chat-dev.html"


class ChatPageTests(unittest.TestCase):
    def setUp(self):
        self.html = CHAT.read_text(encoding="utf-8")


class MobileLayoutTests(ChatPageTests):
    def test_there_is_a_phone_breakpoint(self):
        self.assertRegex(self.html, r"@media\s*\(max-width:\s*\d+px\)")

    def test_the_mockup_frame_is_dropped_on_a_phone(self):
        """The 390x844 card and its padding are what overflowed the viewport."""
        mobile = self._mobile_block()
        self.assertIn("width: 100%", mobile)
        self.assertIn("border-radius: 0", mobile)

    def test_height_follows_the_visible_viewport(self):
        """`vh` counts the address bar as visible, so the composer sat below the
        fold until the bar scrolled away. `dvh` is the one that shrinks."""
        self.assertIn("100dvh", self._mobile_block())

    def test_the_page_cannot_scroll_sideways(self):
        self.assertIn("overflow-x: hidden", self.html)

    def _mobile_block(self):
        match = re.search(r"@media\s*\(max-width:\s*\d+px\)\s*\{(.*?)\n  \}", self.html, re.S)
        self.assertIsNotNone(match, "no phone breakpoint found")
        return match.group(1)


class LightboxTests(ChatPageTests):
    def test_a_lightbox_exists(self):
        self.assertIn('id="lightbox"', self.html)

    def test_it_lives_outside_the_chat_scroll(self):
        """`render()` replaces the scroll region's innerHTML wholesale. A
        lightbox inside it would be destroyed by the next message arriving."""
        scroll = self.html.index('id="chatScroll"')
        lightbox = self.html.index('id="lightbox"')
        self.assertGreater(lightbox, scroll)
        between = self.html[scroll:lightbox]
        self.assertIn("</div>", between)

    def test_guide_photos_are_tappable(self):
        self.assertIn("openLightbox", self.html)

    def test_it_can_be_closed(self):
        """A full-screen overlay with no way out is a trap on a phone, where
        there is no Escape key and Back leaves the page."""
        self.assertIn("closeLightbox", self.html)
        self.assertIn("lightbox-close", self.html)

    def test_escape_closes_it_too(self):
        self.assertIn("Escape", self.html)


class ScrollEconomyTests(ChatPageTests):
    def test_long_runs_of_blank_lines_are_collapsed(self):
        """`white-space: pre-line` honours every newline the model emits, and it
        emits generous ones. Three or more become two."""
        self.assertIn("collapseBlankLines", self.html)


class PhotoPickerTests(ChatPageTests):
    """The attach button used to drop a text note into the composer, so the
    customer was asked for a photo they could not send."""

    def test_there_is_a_real_file_input(self):
        self.assertIn('type="file"', self.html)

    def test_it_only_offers_image_types(self):
        self.assertIn("accept=\"image/jpeg,image/png,image/webp\"", self.html)

    def test_the_attach_button_opens_it(self):
        self.assertIn('document.getElementById("photoInput").click()', self.html)

    def test_the_photo_is_downscaled_before_sending(self):
        """A 6MB camera photo would be refused by the endpoint, and the big
        version has no reason to leave the phone."""
        self.assertIn("shrinkToDataUri", self.html)
        self.assertIn("MAX_EDGE", self.html)

    def test_it_is_sent_with_the_message(self):
        self.assertIn("attachments: sentPhoto", self.html)

    def test_the_same_photo_is_not_sent_twice(self):
        """It is captured and cleared before the request goes out, so the next
        message does not carry it again."""
        self.assertIn("state.pendingPhoto = null", self.html)

    def test_the_old_stub_is_gone(self):
        self.assertNotIn("[photo/video attached]", self.html)


if __name__ == "__main__":
    unittest.main()
