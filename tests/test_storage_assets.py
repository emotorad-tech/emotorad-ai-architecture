import io
import unittest

from PIL import Image

from emotorad_ai.storage import keys
from emotorad_ai.storage.assets import upload_asset, yaml_snippet


class _Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = mime


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
