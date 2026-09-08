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
            "f_auto,q_auto,w_900,c_limit/emotorad/kb/battery/soc-button.png",
        )
        self.assertFalse(got["unresolved"])

    def test_delivery_never_enlarges_a_small_source(self):
        # Without c_limit, Cloudinary scales a small original *up* to the target
        # width: the 480px SOC photo was delivered as a blurrier 67KB version of
        # a sharp 36KB original. Guide photos are often small crops.
        from emotorad_ai.media import IMAGE_TRANSFORM, POSTER_TRANSFORM

        self.assertIn("c_limit", IMAGE_TRANSFORM)
        self.assertIn("c_limit", POSTER_TRANSFORM)

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


class CatalogueTests(unittest.TestCase):
    """The fixed set of pictures the bot may send, and nothing else."""

    def test_the_shipped_catalogue_loads_and_every_entry_resolves(self):
        from emotorad_ai.media import load_catalogue

        catalogue = load_catalogue()
        self.assertTrue(catalogue, "no guide media authored")
        with _WithCloud("emotorad-demo"):
            for key, item in catalogue.items():
                got = resolve(item)
                self.assertFalse(got["unresolved"], "%s: %s" % (key, got.get("reason")))
                self.assertTrue(got["caption"], key)

    def test_the_catalogue_is_not_mistaken_for_a_knowledge_record(self):
        # It lives under an underscore-prefixed directory. If the loader stopped
        # skipping those it would raise on every startup, since a catalogue has
        # none of the fields a record requires.
        self.assertEqual(len(load_records()), 9)

    def test_a_malformed_record_still_raises(self):
        # The skip is an explicit namespace, not a licence to drop bad files: a
        # silently dropped record is a topic the bot has quietly stopped knowing
        # about, with nothing anywhere to say so.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "battery").mkdir()
            (Path(folder) / "battery" / "broken.yaml").write_text("id: x\ntitle: y\n")
            with self.assertRaises(KnowledgeError):
                load_records(Path(folder))

    def _catalogue_from(self, text):
        import tempfile
        from pathlib import Path

        from emotorad_ai.media import load_catalogue

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "_media").mkdir()
            (Path(folder) / "_media" / "catalogue.yaml").write_text(text)
            return load_catalogue(Path(folder))

    def test_an_entry_without_a_caption_is_refused(self):
        from emotorad_ai.media import CatalogueError

        with self.assertRaises(CatalogueError):
            self._catalogue_from("soc:\n  id: X\n  kind: image\n")

    def test_an_entry_without_an_id_or_url_is_refused(self):
        from emotorad_ai.media import CatalogueError

        with self.assertRaises(CatalogueError):
            self._catalogue_from("soc:\n  kind: image\n  caption: c\n")


class SendGuideMediaToolTests(unittest.TestCase):
    """The model chooses a key from a set. It never names a file or a URL."""

    def setUp(self):
        from datetime import date

        from emotorad_ai.media import load_catalogue
        from emotorad_ai.tools.mocks import build_registry

        self.catalogue = load_catalogue()
        self.registry = build_registry(today=date.today(), guide_media=self.catalogue)

    def _send(self, key):
        from emotorad_ai.tools.registry import ToolContext

        return self.registry.call("send_guide_media", {"key": key}, ToolContext(conversation_id="c"))

    def test_the_keys_are_an_enum_in_the_schema(self):
        # So an invented key is rejected before it reaches the tool, and the
        # model can see what it is allowed to pick.
        schema = self.registry.specs["send_guide_media"].schema()
        self.assertEqual(
            sorted(schema["input_schema"]["properties"]["key"]["enum"]), sorted(self.catalogue)
        )

    def test_the_schema_offers_no_way_to_supply_a_file_or_url(self):
        properties = self.registry.specs["send_guide_media"].schema()["input_schema"]["properties"]
        self.assertEqual(set(properties), {"key"})

    def test_sending_a_known_key_returns_renderable_media(self):
        from emotorad_ai.tools.registry import is_error

        with _WithCloud("emotorad-demo"):
            envelope = self._send("soc_button")
        self.assertFalse(is_error(envelope))
        item = envelope["data"]["media"][0]
        self.assertTrue(item["url"])
        self.assertEqual(item["kind"], "image")

    def test_an_unknown_key_errors_and_names_the_valid_ones(self):
        from emotorad_ai.tools.registry import is_error

        envelope = self._send("a_picture_i_made_up")
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "unknown_guide_media")
        self.assertIn("soc_button", envelope["error"]["message"])

    def test_unresolvable_media_errors_rather_than_claiming_to_have_sent(self):
        # Otherwise the reply says "see the photo" and no photo exists.
        from emotorad_ai.tools.registry import is_error

        with _WithCloud(None):
            envelope = self._send("soc_button")
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "guide_media_unavailable")
        self.assertIn("not tell the customer", envelope["error"]["message"])

    def test_the_tool_is_absent_when_no_catalogue_is_supplied(self):
        from datetime import date

        from emotorad_ai.tools.mocks import build_registry

        self.assertNotIn("send_guide_media", build_registry(today=date.today()).specs)
