import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = mime


class UploadAssetTests(unittest.TestCase):
    def test_original_and_derivative_are_uploaded_and_the_id_is_printed(self):
        upload_asset = load("upload_asset")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "soc.png"
            Image.new("RGB", (1200, 800)).save(path)
            store = _Store()
            out = upload_asset.upload_asset(store, str(path), "afs", "battery", "photos", "soc-button")
        self.assertEqual(out["id"], "afs/battery/photos/soc-button.png")
        self.assertEqual(store.objects, {"assets/afs/battery/photos/soc-button.png": "image/png", "assets/afs/battery/photos/soc-button.w900.webp": "image/webp"})


class MigrateTests(unittest.TestCase):
    def test_plan_maps_cloudinary_ids_to_asset_ids(self):
        migrate = load("migrate_cloudinary")
        records = [{"topic": "battery", "id": "SOC_Button_non_doodle", "kind": "image"}, {"topic": "battery", "id": "Battery_Revival_Steps.mp4", "kind": "video"}, {"topic": "motor", "id": "afs/motor/photos/pas.jpg", "kind": "image"}]
        self.assertEqual(
            migrate.plan(records),
            [("SOC_Button_non_doodle", "afs/battery/photos/soc-button-non-doodle.jpg"), ("Battery_Revival_Steps.mp4", "afs/battery/videos/battery-revival-steps.mp4")],
        )

    def test_rewrite_ids_touches_only_id_lines(self):
        migrate = load("migrate_cloudinary")
        text = "media:\n  - id: SOC_Button_non_doodle\n    caption: SOC_Button_non_doodle\n"
        out = migrate.rewrite_ids(text, {"SOC_Button_non_doodle": "afs/battery/photos/soc-button-non-doodle.jpg"})
        self.assertIn("  - id: afs/battery/photos/soc-button-non-doodle.jpg\n", out)
        self.assertIn("caption: SOC_Button_non_doodle", out)


if __name__ == "__main__":
    unittest.main()
