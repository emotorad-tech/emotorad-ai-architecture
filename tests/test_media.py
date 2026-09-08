"""Outbound guide media — the photos and clips the bot sends to help someone
find a button, rather than describing where it is in a paragraph.

Two properties are load-bearing here. Records store an **id**, not a URL, so the
cloud name and the transformations live in one module and a CDN move is one
edit. And an item that cannot be resolved is **reported**, never dropped: a
missing picture that fails silently is indistinguishable from a step that never
had one, and the customer is left reading the paragraph the picture replaced.
"""

import os
import unittest

from emotorad_ai import media
from emotorad_ai.knowledge import KnowledgeError, load_records
from emotorad_ai.media import resolve


class _WithCloud:
    """Set the cloud name for the duration of a test, and put it back."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.previous = os.environ.get(media.CLOUD_NAME_ENV)
        if self.value is None:
            os.environ.pop(media.CLOUD_NAME_ENV, None)
        else:
            os.environ[media.CLOUD_NAME_ENV] = self.value

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop(media.CLOUD_NAME_ENV, None)
        else:
            os.environ[media.CLOUD_NAME_ENV] = self.previous
        return False


class DeliveryUrlTests(unittest.TestCase):
    def test_an_id_becomes_an_image_url_with_the_delivery_transform(self):
        with _WithCloud("emotorad-demo"):
            got = resolve({"id": "emotorad/kb/battery/soc-button.png", "caption": "The SOC button"})
        self.assertEqual(
            got["url"],
            "https://res.cloudinary.com/emotorad-demo/image/upload/"
            "f_auto,q_auto,w_900/emotorad/kb/battery/soc-button.png",
        )
        self.assertFalse(got["unresolved"])

    def test_a_video_uses_the_video_resource_and_gains_a_poster(self):
        # A player with no poster shows a black rectangle until it is tapped,
        # which is not much of a visual aid.
        with _WithCloud("emotorad-demo"):
            got = resolve({"id": "emotorad/kb/battery/key-turn.mp4", "kind": "video", "caption": "Key to ON"})
        self.assertIn("/video/upload/", got["url"])
        # The poster frame is extracted from the clip, so it is served from the
        # video resource too. /image/upload/so_0/... has no clip to seek into and
        # 404s — verified against a real asset, after this test first asserted
        # the broken URL and happily passed.
        self.assertIn("/video/upload/so_0", got["poster"])
        self.assertTrue(got["poster"].endswith("key-turn.jpg"))

    def test_an_absolute_url_is_passed_through_untouched(self):
        # Records written before ids existed, and anything hosted elsewhere.
        with _WithCloud("emotorad-demo"):
            got = resolve({"url": "https://cdn.example.test/a.png", "caption": "c"})
        self.assertEqual(got["url"], "https://cdn.example.test/a.png")
        self.assertFalse(got["unresolved"])

    def test_the_cloud_name_lives_in_one_place(self):
        # Moving CDN, or changing the account, must not touch content files.
        item = {"id": "emotorad/kb/x.png", "caption": "c"}
        with _WithCloud("cloud-one"):
            first = resolve(item)["url"]
        with _WithCloud("cloud-two"):
            second = resolve(item)["url"]
        self.assertIn("cloud-one", first)
        self.assertIn("cloud-two", second)


class UnresolvableMediaTests(unittest.TestCase):
    def test_a_missing_cloud_name_is_reported_not_swallowed(self):
        with _WithCloud(None):
            got = resolve({"id": "emotorad/kb/x.png", "caption": "c"})
        self.assertTrue(got["unresolved"])
        self.assertIsNone(got["url"])
        self.assertIn(media.CLOUD_NAME_ENV, got["reason"])

    def test_an_item_with_neither_id_nor_url_is_reported(self):
        got = resolve({"caption": "c"})
        self.assertTrue(got["unresolved"])
        self.assertIn("no id or url", got["reason"])

    def test_the_caption_survives_even_when_the_url_does_not(self):
        # It is what the model reasons about, and what the warning names.
        with _WithCloud(None):
            got = resolve({"id": "emotorad/kb/x.png", "caption": "The SOC button"})
        self.assertEqual(got["caption"], "The SOC button")


class AuthoringRulesTests(unittest.TestCase):
    """Malformed media must fail at load, not silently at send time."""

    def _record(self, media_items):
        return {
            "id": "r1",
            "title": "t",
            "topic": "battery",
            "symptoms": ["x"],
            "steps": ["do a thing"],
            "media": media_items,
        }

    def _validate(self, media_items):
        from emotorad_ai.knowledge import _validate  # noqa: PLC0415

        _validate(self._record(media_items), "test")

    def test_an_item_with_neither_id_nor_url_is_refused(self):
        with self.assertRaises(KnowledgeError) as caught:
            self._validate([{"caption": "c"}])
        self.assertIn("id", str(caught.exception))

    def test_a_caption_is_still_required(self):
        with self.assertRaises(KnowledgeError):
            self._validate([{"id": "emotorad/kb/x.png"}])

    def test_an_unknown_kind_is_refused(self):
        # The channel has to know whether to render a picture or a player.
        with self.assertRaises(KnowledgeError) as caught:
            self._validate([{"id": "emotorad/kb/x.gif", "caption": "c", "kind": "animation"}])
        self.assertIn("image", str(caught.exception))

    def test_id_and_url_are_both_accepted_shapes(self):
        self._validate([{"id": "emotorad/kb/x.png", "caption": "c"}])
        self._validate([{"url": "https://cdn.example.test/x.png", "caption": "c", "kind": "video"}])


class ShippedRecordsTests(unittest.TestCase):
    def test_every_authored_media_item_resolves_or_says_why(self):
        with _WithCloud("emotorad-demo"):
            for record in load_records():
                for item in record.media:
                    got = resolve(item)
                    self.assertTrue(
                        got["url"] or got["unresolved"],
                        "%s: media neither resolved nor reported" % record.id,
                    )
                    self.assertTrue(got["caption"], "%s: media item lost its caption" % record.id)


if __name__ == "__main__":
    unittest.main()
