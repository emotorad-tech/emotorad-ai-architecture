import io
import unittest

from PIL import Image

from emotorad_ai.storage import keys
from emotorad_ai.storage.assets import finish_asset, upload_asset, yaml_snippet


class _Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = mime
        self.data = getattr(self, "data", {})
        self.data[key] = data
        self.fetched = getattr(self, "fetched", [])

    def get_bytes(self, key):
        self.fetched = getattr(self, "fetched", [])
        self.fetched.append(key)
        return self.data[key]


def _png_bytes(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


class UploadAssetTests(unittest.TestCase):
    def test_original_and_derivative_are_uploaded_for_an_image(self):
        store = _Store()
        out = upload_asset(store, _png_bytes(1200, 800), "image/png", "afs", "battery", "photos", "soc-button")
        self.assertEqual(out["id"], "afs/battery/photos/soc-button.png")
        self.assertEqual(
            store.objects,
            {
                "assets/afs/battery/photos/soc-button.png": "image/png",
                "assets/afs/battery/photos/soc-button.w900.webp": "image/webp",
            },
        )

    def test_bad_slug_raises_before_any_put(self):
        store = _Store()
        with self.assertRaises(keys.KeyValidationError):
            upload_asset(store, _png_bytes(10, 10), "image/png", "afs", "battery", "photos", "Not A Slug")
        self.assertEqual(store.objects, {})


class FinishAssetTests(unittest.TestCase):
    """The browser has already PUT the original straight to S3 (presigned);
    `finish_asset` reads it back and writes only the derivatives."""

    def test_derivative_is_made_from_the_object_already_in_the_bucket(self):
        store = _Store()
        store.put_bytes("assets/afs/battery/photos/soc-button.png", _png_bytes(1200, 800), "image/png")
        out = finish_asset(store, "assets/afs/battery/photos/soc-button.png")
        self.assertEqual(out["id"], "afs/battery/photos/soc-button.png")
        self.assertEqual(out["original"], "assets/afs/battery/photos/soc-button.png")
        self.assertEqual(out["w900"], "assets/afs/battery/photos/soc-button.w900.webp")
        self.assertEqual(store.objects["assets/afs/battery/photos/soc-button.w900.webp"], "image/webp")
        self.assertEqual(store.fetched, ["assets/afs/battery/photos/soc-button.png"])

    def test_a_document_has_no_derivatives_and_is_never_fetched(self):
        store = _Store()
        store.put_bytes("assets/afs/battery/docs/manual.pdf", b"%PDF-1.4 fake", "application/pdf")
        out = finish_asset(store, "assets/afs/battery/docs/manual.pdf")
        self.assertEqual(out, {"id": "afs/battery/docs/manual.pdf", "original": "assets/afs/battery/docs/manual.pdf"})
        self.assertEqual(store.fetched, [])


class YamlSnippetTests(unittest.TestCase):
    def test_yaml_snippet_for_an_image(self):
        self.assertEqual(
            yaml_snippet("afs/battery/photos/soc-button.png", "image", "Where the SOC button is"),
            "  - id: afs/battery/photos/soc-button.png\n    kind: image\n    caption: Where the SOC button is\n",
        )

    def test_yaml_snippet_uses_kind_video_for_an_mp4_id(self):
        self.assertEqual(
            yaml_snippet("afs/battery/videos/key-turn.mp4", "video", "Turning the key"),
            "  - id: afs/battery/videos/key-turn.mp4\n    kind: video\n    caption: Turning the key\n",
        )


if __name__ == "__main__":
    unittest.main()
