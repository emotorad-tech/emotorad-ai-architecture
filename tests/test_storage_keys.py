"""Keys are derived by code from closed vocabularies. The client never chooses
one, and a phone number can never end up in one."""

import re
import unittest

from emotorad_ai.storage.keys import (
    ASSET_KINDS,
    CUSTOMER_KINDS,
    PROGRAMMES,
    SIZE_CAPS,
    KeyValidationError,
    asset_key,
    cluster_of,
    customer_key,
    customer_kind_for,
    derivative_keys,
    extension_for,
    is_asset_key,
    is_customer_key,
    is_valid_key,
    new_upload_id,
    playground_key,
)


class VocabularyTests(unittest.TestCase):
    def test_the_vocabularies_are_the_ones_in_the_spec(self):
        self.assertEqual(PROGRAMMES, ("afs", "presales", "dealer"))
        self.assertEqual(ASSET_KINDS, ("photos", "videos", "tips", "docs"))
        self.assertEqual(CUSTOMER_KINDS, ("images", "videos", "docs"))
        self.assertEqual(SIZE_CAPS, {"images": 10 * 1024 * 1024, "videos": 100 * 1024 * 1024, "docs": 10 * 1024 * 1024})

    def test_extension_comes_from_the_mime_type(self):
        self.assertEqual(extension_for("image/jpeg"), "jpg")
        self.assertEqual(extension_for("image/png"), "png")
        self.assertEqual(extension_for("image/webp"), "webp")
        self.assertEqual(extension_for("video/mp4"), "mp4")
        self.assertEqual(extension_for("application/pdf"), "pdf")
        with self.assertRaises(KeyValidationError):
            extension_for("image/gif")

    def test_customer_kind_follows_the_mime_type(self):
        self.assertEqual(customer_kind_for("image/png"), "images")
        self.assertEqual(customer_kind_for("video/mp4"), "videos")
        self.assertEqual(customer_kind_for("application/pdf"), "docs")


class AssetKeyTests(unittest.TestCase):
    def test_a_well_formed_asset_key(self):
        self.assertEqual(
            asset_key("afs", "battery", "photos", "soc-button", "image/jpeg"),
            "assets/afs/battery/photos/soc-button.jpg",
        )

    def test_every_segment_is_validated(self):
        with self.assertRaises(KeyValidationError):
            asset_key("marketing", "battery", "photos", "x", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "Battery", "photos", "x", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "battery", "pictures", "x", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "battery", "photos", "SOC Button", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "../battery", "photos", "x", "image/jpeg")

    def test_derivatives_for_an_image_and_a_video(self):
        self.assertEqual(
            derivative_keys("assets/afs/battery/photos/soc-button.jpg"),
            {"w900": "assets/afs/battery/photos/soc-button.w900.webp"},
        )
        self.assertEqual(
            derivative_keys("assets/afs/battery/videos/key-turn.mp4"),
            {"poster": "assets/afs/battery/videos/key-turn.poster.jpg"},
        )
        self.assertEqual(derivative_keys("assets/afs/battery/docs/manual.pdf"), {})


class CustomerKeyTests(unittest.TestCase):
    def test_a_well_formed_customer_key(self):
        key = customer_key("clu_8f3a12", "conv_01J9K3", "images", "upl_01J9K4ab", "image/jpeg")
        self.assertEqual(key, "customers/clu_8f3a12/conv_01J9K3/images/upl_01J9K4ab.jpg")
        self.assertTrue(is_customer_key(key))
        self.assertFalse(is_asset_key(key))
        self.assertEqual(cluster_of(key), "clu_8f3a12")

    def test_a_phone_number_is_refused_as_a_cluster_id(self):
        for bad in ("+919876543210", "919876543210", "9876543210"):
            with self.assertRaises(KeyValidationError):
                customer_key(bad, "conv_1", "images", "upl_1", "image/jpeg")

    def test_kind_must_match_the_mime_type(self):
        with self.assertRaises(KeyValidationError):
            customer_key("clu_1", "conv_1", "videos", "upl_1", "image/jpeg")

    def test_upload_ids_sort_by_time(self):
        a = new_upload_id()
        b = new_upload_id()
        self.assertTrue(a.startswith("upl_") and b.startswith("upl_"))
        self.assertLessEqual(a[:14], b[:14])
        self.assertRegex(a, r"^upl_[0-9a-z]{10}[0-9a-z]{8}$")
        self.assertNotEqual(a, b)


class PlaygroundKeyTests(unittest.TestCase):
    def test_a_well_formed_playground_key(self):
        self.assertEqual(
            playground_key("20260921-ab12cd34", "images", "deadbeef", "image/png"),
            "customers/playground/20260921-ab12cd34/images/deadbeef.png",
        )

    def test_a_chat_id_with_a_slash_is_refused(self):
        with self.assertRaises(KeyValidationError):
            playground_key("20260921/ab12cd34", "images", "deadbeef", "image/png")


class IsValidKeyTests(unittest.TestCase):
    def test_accepts_a_customer_key(self):
        self.assertTrue(is_valid_key("customers/clu_1/conv_1/images/upl_1.jpg"))

    def test_accepts_an_asset_key_and_its_derivatives(self):
        self.assertTrue(is_valid_key("assets/afs/battery/photos/soc-button.jpg"))
        self.assertTrue(is_valid_key("assets/afs/battery/photos/soc-button.w900.webp"))
        self.assertTrue(is_valid_key("assets/afs/battery/videos/key-turn.poster.jpg"))

    def test_rejects_a_prefix_only_match(self):
        self.assertFalse(is_valid_key("assets/../customers/x/y/images/z.jpg"))
        self.assertFalse(is_valid_key("assets/%2e%2e/customers/x/y/images/z.jpg"))

    def test_rejects_an_unknown_extension(self):
        self.assertFalse(is_valid_key("customers/clu/conv/images/z.exe"))

    def test_rejects_anything_outside_the_two_trees(self):
        self.assertFalse(is_valid_key("deploy/app.tar.gz"))


if __name__ == "__main__":
    unittest.main()
