"""Video first: what a bot reply asks for, and what a customer declines
(spec 2026-10-01-video-first-evidence-design.md)."""

import unittest

from emotorad_ai import evidence_asks as ev


class AsksForMediaTests(unittest.TestCase):
    def test_a_video_ask(self):
        for reply in ("Could you send a short video of the charger plugged in, showing its light?",
                      "Please record a quick clip of the switch being turned on.",
                      "Send me a video of the display when you press the power button.",
                      "Can you share a video or a photo of the port?",
                      "Thanks for the photo. Could you also send a short video of the plug going in?",
                      "Can you show me the light in a video?",
                      "Charger ki light ka ek video bhejiye.",
                      "चार्जर की लाइट का एक छोटा वीडियो भेजें।"):
            self.assertEqual(ev.asks_for_media(reply), "video", reply)

    def test_a_photo_only_ask(self):
        for reply in ("Could you send a photo of the charger LED?",
                      "Please take a picture of the sticker on the frame.",
                      "Can you share a pic of the display?",
                      "Is the light red? Please send a photo of it.",
                      "Ek photo bhejiye.",
                      "डिस्प्ले की फ़ोटो भेजें।"):
            self.assertEqual(ev.asks_for_media(reply), "photo", reply)

    def test_not_an_ask(self):
        for reply in ("Here's a picture of the on/off switch.",
                      "Let me send you a picture of where the switch is.",
                      "Thanks for the video, I can see the light stays red.",
                      "I can see in your video that the display stays dark.",
                      "Your photo shows a clean terminal.",
                      "Is the charger light on?",
                      "Could you take a look at the charger?",
                      "Main aapko switch ki photo bhej raha hoon.",
                      "Video mein light dikhai de rahi hai.",
                      "आपने जो वीडियो भेजा, उसमें लाइट लाल है।",
                      "",
                      None):
            self.assertIsNone(ev.asks_for_media(reply), reply)

    def test_the_bots_own_picture_beside_a_video_ask(self):
        self.assertEqual(ev.asks_for_media("Here's a picture of the switch. Could you send a video of yours?"), "video")


class DeclinesVideoTests(unittest.TestCase):
    def test_a_decline(self):
        for text in ("I can't take a video", "Sorry, I cannot record a video right now", "unable to send video",
                     "my camera doesn't record", "can I send a photo instead?", "only a photo, sorry",
                     "video is not possible", "video isn't possible here", "no video, sorry",
                     "I don't have a video", "I can't send photos or videos", "video nahi ho payega",
                     "वीडियो नहीं बन रहा"):
            self.assertTrue(ev.declines_video(text), text)

    def test_not_a_decline(self):
        for text in ("I've sent a video", "the video is uploading", "here is the video", "video bhej diya",
                     "I can send a video", "no video yet, it is still uploading", "", None):
            self.assertFalse(ev.declines_video(text), text)


class AddedLineTests(unittest.TestCase):
    def test_a_photo_ask_gets_the_video_line(self):
        self.assertEqual(ev.added_line("Could you send a photo of the LED?", "photo", False),
                         ("video_first_added", ev.VIDEO_FIRST_LINE))

    def test_after_a_decline_a_photo_ask_gets_nothing(self):
        self.assertIsNone(ev.added_line("Could you send a photo of the LED?", "photo", True))

    def test_after_a_decline_a_video_ask_gets_the_photo_line(self):
        self.assertEqual(ev.added_line("Could you send a video of the LED?", "video", True),
                         ("photo_fallback_added", ev.PHOTO_FALLBACK_LINE))

    def test_a_video_ask_before_any_decline_gets_nothing(self):
        self.assertIsNone(ev.added_line("Could you send a video of the LED?", "video", False))

    def test_no_line_when_the_reply_already_speaks_of_a_video(self):
        self.assertIsNone(ev.added_line("Thanks for the video. Could you also send a photo of the sticker?",
                                        "photo", False))

    def test_a_devanagari_reply_gets_the_hindi_line(self):
        self.assertEqual(ev.added_line("डिस्प्ले की फ़ोटो भेजें।", "photo", False),
                         ("video_first_added", ev.VIDEO_FIRST_LINE_HI))
        self.assertEqual(ev.added_line("चार्जर का वीडियो भेजें।", "video", True),
                         ("photo_fallback_added", ev.PHOTO_FALLBACK_LINE_HI))

    def test_nothing_asked_gets_nothing(self):
        self.assertIsNone(ev.added_line("Is the light on?", None, False))

    def test_three_asks(self):
        self.assertEqual(ev.MAX_EVIDENCE_ASKS, 3)


class FinalReviewDetectionTests(unittest.TestCase):
    """The final review (2026-10-01): an ask is a request whose object is the
    photo or video; a media word somewhere in the sentence is not enough."""

    def test_more_video_asks(self):
        for reply in ("Please record a short video so we can hear the hum from the motor.",
                      "Hum aapse request karte hain ki charger ka ek chhota video bhejiye.",
                      "Kindly share a short video of the issue.",
                      "Would you be able to send a short video of the charger light?",
                      "If possible, send a short video of the light.",
                      "Could you please send:\n- a short video of the charger plugged in\n- a photo of the label",
                      "Could you send a short video of the main power button being pressed?",
                      "क्या आप चार्जर का वीडियो भेज सकते हैं?"):
            self.assertEqual(ev.asks_for_media(reply), "video", reply)

    def test_more_photo_asks(self):
        for reply in ("Could you send a photo of the main switch?",
                      "Are you able to send a photo of the port?",
                      "Aap charger ki photo le sakte hain?"):
            self.assertEqual(ev.asks_for_media(reply), "photo", reply)

    def test_more_replies_that_ask_for_nothing(self):
        for reply in ("Please take a look at the picture: the SOC button is on the side of the pack.",
                      "Can you take a look at the image above and tell me if your switch looks like this?",
                      "Take the battery off the bike, as the picture shows.",
                      "Please share a few more details so I can get a clearer picture of what is happening.",
                      "Could you share when it started, so I have the full picture?",
                      "Please share your pincode and I will send you a picture of the nearest service centre.",
                      "Aapke video mein saaf dikha raha hai ki light red hai.",
                      "Video mein display kuch nahi dikha raha.",
                      "Aapne jo video bhej diya, usmein light red hai."):
            self.assertIsNone(ev.asks_for_media(reply), reply)

    def test_a_document_is_not_fault_evidence(self):
        for reply in ("Thanks. Could you share a photo of your invoice or proof of purchase?",
                      "Could you send a screenshot of the error in the app?"):
            self.assertIsNone(ev.asks_for_media(reply), reply)

    def test_more_declines(self):
        for text in ("the video won't upload, it's too big", "Video is too large to send", "video upload failed",
                     "I tried but the video isn't sending", "I don't know how to send a video",
                     "Can I just send a photo?", "video bhejna possible nahi hai", "वीडियो भेजना संभव नहीं है"):
            self.assertTrue(ev.declines_video(text), text)

    def test_more_that_are_not_declines(self):
        for text in ("Video mein light nahi dikh rahi", "video bhej diya, light nahi jal rahi"):
            self.assertFalse(ev.declines_video(text), text)

    def test_no_video_line_when_the_reply_asks_for_a_photo_instead(self):
        self.assertIsNone(ev.added_line("No problem. Could you send a photo of the charger light instead?",
                                        "photo", False))
