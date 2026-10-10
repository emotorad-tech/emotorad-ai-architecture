"""The admin media form uploads straight to S3 from the browser, the way the
chat page does: presign, PUT, finish. The Streamlit side only turns the
component's answer into the preview record."""

import unittest
from pathlib import Path

from emotorad_ai import playground


class ComponentFilesTests(unittest.TestCase):
    def test_component_is_shipped_with_the_package_and_speaks_the_protocol(self):
        index = Path(playground.ASSET_UPLOADER_DIR) / "index.html"
        self.assertTrue(index.is_file(), index)
        html = index.read_text()
        for needle in ("streamlit:componentReady", "streamlit:render", "streamlit:setComponentValue", "/uploads", "/finish"):
            self.assertIn(needle, html)
        # The browser PUTs to S3 itself; the file never goes through nginx or Streamlit.
        self.assertIn("XMLHttpRequest", html)
        self.assertNotIn("_stcore/upload_file", html)


class OutcomeTests(unittest.TestCase):
    def test_a_fresh_success_becomes_the_preview_record(self):
        outcome = {"ok": True, "upload_id": "upl_1", "result": {"id": "afs/battery/videos/key-turn.mp4", "original": "assets/afs/battery/videos/key-turn.mp4"}}
        record = playground._asset_upload_record(outcome, previous=None)
        self.assertEqual(record["upload_id"], "upl_1")
        self.assertEqual(record["kind_word"], "video")
        self.assertEqual(record["ext"], "mp4")
        self.assertEqual(record["result"]["id"], "afs/battery/videos/key-turn.mp4")

    def test_the_same_answer_on_a_rerun_keeps_the_existing_record(self):
        outcome = {"ok": True, "upload_id": "upl_1", "result": {"id": "afs/battery/photos/soc.png"}}
        previous = {"upload_id": "upl_1", "result": {"id": "afs/battery/photos/soc.png"}, "kind_word": "image", "ext": "png"}
        self.assertIs(playground._asset_upload_record(outcome, previous=previous), previous)

    def test_nothing_or_a_failure_keeps_what_was_there(self):
        previous = {"upload_id": "upl_0", "result": {"id": "x.png"}, "kind_word": "image", "ext": "png"}
        self.assertIs(playground._asset_upload_record(None, previous=previous), previous)
        self.assertIs(playground._asset_upload_record({"ok": False, "error": "413"}, previous=previous), previous)
        self.assertIsNone(playground._asset_upload_record({"ok": False, "error": "413"}, previous=None))


if __name__ == "__main__":
    unittest.main()
