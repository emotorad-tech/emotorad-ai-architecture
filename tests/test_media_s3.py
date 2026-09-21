import unittest
from unittest import mock

from emotorad_ai import media
from emotorad_ai.media import resolve


class _Store:
    def presign_get(self, key):
        return "https://signed/" + key


class S3IdTests(unittest.TestCase):
    def test_an_id_with_a_slash_is_an_asset_key(self):
        out = resolve({"id": "afs/battery/photos/soc-button.jpg", "caption": "SOC button"}, store=_Store())
        self.assertEqual(out["url"], "https://signed/assets/afs/battery/photos/soc-button.w900.webp")
        self.assertEqual(out["fallback"], "https://signed/assets/afs/battery/photos/soc-button.jpg")
        self.assertIsNone(out["poster"])
        self.assertFalse(out["unresolved"])

    def test_a_video_gets_the_original_and_a_poster(self):
        out = resolve({"id": "afs/battery/videos/key-turn.mp4", "kind": "video", "caption": "c"}, store=_Store())
        self.assertEqual(out["url"], "https://signed/assets/afs/battery/videos/key-turn.mp4")
        self.assertEqual(out["poster"], "https://signed/assets/afs/battery/videos/key-turn.poster.jpg")

    def test_no_store_reports_the_gap_by_name(self):
        media._store = None
        media._store_loaded = False
        with mock.patch.dict("os.environ", {"EMOTORAD_AI_MEDIA_BUCKET": ""}):
            out = resolve({"id": "afs/battery/photos/soc-button.jpg", "caption": "c"})
        self.assertTrue(out["unresolved"])
        self.assertIn("EMOTORAD_AI_MEDIA_BUCKET", out["reason"])

    def test_a_cloudinary_id_still_resolves_through_cloudinary(self):
        with mock.patch.dict("os.environ", {"EMOTORAD_CLOUDINARY_CLOUD": "cloudx"}):
            out = resolve({"id": "SOC_Button_non_doodle", "caption": "c"}, store=_Store())
        self.assertTrue(out["url"].startswith("https://res.cloudinary.com/cloudx/"))

    def test_an_absolute_url_is_untouched(self):
        out = resolve({"url": "https://cdn.test/a.png", "caption": "c"}, store=_Store())
        self.assertEqual(out["url"], "https://cdn.test/a.png")

    def test_a_folder_qualified_cloudinary_id_still_goes_to_cloudinary(self):
        with mock.patch.dict("os.environ", {"EMOTORAD_CLOUDINARY_CLOUD": "cloudx"}):
            out = resolve({"id": "emotorad/kb/battery/soc-button.png", "caption": "c"}, store=_Store())
        self.assertTrue(out["url"].startswith("https://res.cloudinary.com/cloudx/"))

    def test_is_asset_id_needs_a_programme_prefix(self):
        from emotorad_ai.media import is_asset_id

        self.assertTrue(is_asset_id("afs/battery/photos/x.jpg"))
        self.assertTrue(is_asset_id("dealer/orders/docs/x.pdf"))
        self.assertFalse(is_asset_id("emotorad/kb/battery/x.png"))
        self.assertFalse(is_asset_id("afs"))
        self.assertFalse(is_asset_id("SOC_Button_non_doodle"))

    def test_is_asset_id_needs_a_known_extension_too(self):
        from emotorad_ai.media import is_asset_id

        # A programme prefix without a known extension on the last segment is
        # not enough: without this check, `resolve` falls through to the
        # Cloudinary path exactly as an id without a programme prefix does.
        self.assertFalse(is_asset_id("afs/battery/photos/soc-button"))

    def test_an_asset_id_without_an_extension_is_unresolved(self):
        with mock.patch.dict("os.environ", {"EMOTORAD_CLOUDINARY_CLOUD": ""}):
            out = resolve({"id": "afs/battery/photos/soc-button", "caption": "c"}, store=_Store())
        from emotorad_ai.media import is_asset_id

        self.assertFalse(is_asset_id("afs/battery/photos/soc-button"))
        self.assertTrue(out["unresolved"])


if __name__ == "__main__":
    unittest.main()
