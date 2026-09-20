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

    def test_it_accepts_any_image(self):
        """A narrow mime list sent Android straight to the file manager and
        skipped the camera. `image/*` is what the system chooser keys on."""
        self.assertNotIn("image/jpeg,image/png,image/webp", self.html)
        self.assertIn('accept="image/*"', self.html)

    def test_the_attach_button_opens_the_choice(self):
        self.assertIn('addEventListener("click", openAttachSheet)', self.html)

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


class KeyboardBehaviourTests(ChatPageTests):
    """Why the layout lurched every time you sent a message.

    Reported from a phone on 20 September: sending a message threw the whole
    conversation up the screen and left a void above the composer.

    The composer was emptied while the reply was in flight, so the focused
    element left the DOM, Android closed the keyboard, and the viewport
    resized. The reply then rebuilt it and called focus(), which opened the
    keyboard and resized the viewport again. Two resizes per message.
    """

    def test_the_composer_is_not_destroyed_while_the_bot_replies(self):
        """Removing the focused element is what closes the keyboard."""
        self.assertNotIn('footer.innerHTML = ""', self.html)

    def test_sending_is_disabled_rather_than_removed(self):
        self.assertIn("composerDisabled", self.html)

    def test_focus_is_not_stolen_on_every_render(self):
        """render() runs on every message. Focusing there reopens the keyboard
        even when the customer is reading rather than typing."""
        self.assertNotIn("input.focus()", self.html)

    def test_messages_hang_from_the_bottom(self):
        """A flex column with no alignment stacks from the top, so a short
        conversation floats above a void. Real chats grow up from the composer.

        `margin-top: auto` on an inner wrapper rather than
        `justify-content: flex-end`, which clips the top of the content when
        you scroll up in Chrome."""
        self.assertIn("chat-inner", self.html)
        self.assertIn("margin-top: auto", self.html)

    def test_the_layout_follows_the_keyboard(self):
        """`dvh` accounts for browser chrome, not the keyboard. The visual
        viewport is the one that shrinks when the keyboard opens."""
        self.assertIn("visualViewport", self.html)

    def test_the_last_message_stays_in_view_when_the_keyboard_opens(self):
        self.assertIn("pinToBottom", self.html)


class MarkdownRenderingTests(ChatPageTests):
    """The model writes markdown and the page was showing it raw.

    Seen on a phone on 20 September: "Could you check it's set to **ON**",
    asterisks and all. esc() escapes every character, which is right for safety
    and wrong for legibility, and the agent leans on emphasis and lists to make
    a diagnostic step scannable.
    """

    def test_a_renderer_exists(self):
        self.assertIn("renderMarkdown", self.html)

    def test_escaping_happens_before_formatting(self):
        """Order is the whole safety story. Formatting first would put model
        output into the DOM as raw HTML."""
        body = self.html[self.html.index("function renderMarkdown"):]
        body = body[:body.index("\nfunction ", 1)]
        self.assertLess(body.index("esc("), body.index("<strong>"))

    def test_links_are_not_rendered(self):
        """Deliberate. A rendered link from model output is a phishing surface,
        and the prompt already forbids the model typing links: media goes
        through send_guide_media, where code resolves the URL."""
        body = self.html[self.html.index("function renderMarkdown"):]
        body = body[:body.index("\nfunction ", 1)]
        self.assertNotIn("<a ", body)

    def test_bold_and_lists_are_rendered(self):
        body = self.html[self.html.index("function renderMarkdown"):]
        body = body[:body.index("\nfunction ", 1)]
        self.assertIn("<strong>", body)
        self.assertIn("<li>", body)


class GuidePhotoLayoutTests(ChatPageTests):
    """The image sat about 32px left of the bubble above it.

    The text row is indented by the avatar column; the attachment row had no
    such spacer, so it ran flush to the edge. Not centred — left is the
    assistant and right is the customer, and a centred image belongs to
    neither, which matters more once the customer's own photos are in the
    thread too."""

    def test_photos_line_up_with_the_bubble(self):
        self.assertIn("media-row", self.html)

    def test_they_are_not_centred(self):
        block = self.html[self.html.index(".media-row"):]
        block = block[:block.index("}")]
        self.assertNotIn("justify-content: center", block)


class AttachSheetTests(ChatPageTests):
    """Reported from a phone on 20 September: tapping attach went straight to
    the file manager, with no way to open the camera, and after picking a
    photo nothing on the screen said one was attached — pressing send then
    "magically" sent media along with the text.
    """

    def test_attach_opens_a_choice_rather_than_a_picker(self):
        self.assertIn('id="attachSheet"', self.html)

    def test_there_is_a_camera_input(self):
        """`capture` opens straight into the viewfinder on Android and iOS
        rather than asking the OS to guess what the customer meant."""
        self.assertIn('capture="environment"', self.html)

    def test_there_is_a_separate_gallery_input(self):
        camera = self.html.index('capture="environment"')
        inputs = [m.start() for m in re.finditer(r'<input type="file"', self.html)]
        self.assertGreaterEqual(len(inputs), 2, "expected a camera input and a gallery input")

    def test_a_picked_photo_is_shown_before_sending(self):
        self.assertIn('id="pendingStrip"', self.html)
        self.assertIn("pending-thumb", self.html)

    def test_a_picked_photo_can_be_removed(self):
        self.assertIn("removePendingPhoto", self.html)

    def test_no_caption_is_written_for_the_customer(self):
        """The box was being pre-filled with "Here is the photo." A caption is
        theirs to write or leave; a canned one reads as if the bot typed it."""
        self.assertNotIn('"Here is the photo."', self.html)

    def test_send_lights_up_for_a_photo_alone(self):
        body = self.html[self.html.index("function paintSendButton"):]
        body = body[:body.index("\n}", 1)]
        self.assertIn("pendingPhoto", body)


if __name__ == "__main__":
    unittest.main()
