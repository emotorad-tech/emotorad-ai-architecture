"""The guide pictures the bot may send: only the two the person chose
(2026-09-30), both in emotorad-ai-stage-media under assets/afs/battery/photos/."""

import pathlib
import unittest

import yaml

from emotorad_ai.knowledge import load_records
from emotorad_ai.media import load_catalogue

CATALOGUE_FILE = pathlib.Path(__file__).resolve().parents[1] / "knowledge" / "_media" / "catalogue.yaml"
EM_DASH = chr(0x2014)

KEPT = {
    "soc_button": "afs/battery/photos/soc-button-non-doodle.png",
    "battery_onoff_switch": "afs/battery/photos/battery-onoff-switch.png",
}


class CatalogueTests(unittest.TestCase):
    def test_exactly_the_two_pictures(self):
        catalogue = load_catalogue()
        self.assertEqual({key: item["id"] for key, item in catalogue.items()}, KEPT)

    def test_no_caption_has_an_em_dash(self):
        # Read as UTF-8 here: load_catalogue() uses the platform encoding, which
        # on Windows turns an em dash into other characters and hides it.
        raw = yaml.safe_load(CATALOGUE_FILE.read_text(encoding="utf-8"))
        for key, item in raw.items():
            self.assertNotIn(EM_DASH, item["caption"], key)

    def test_no_knowledge_record_names_another_picture(self):
        for record in load_records():
            for item in record.media:
                self.assertIn(item.get("id"), set(KEPT.values()), "%s: %r" % (record.id, item))

    def test_no_record_tells_the_bot_to_send_a_picture_it_no_longer_has(self):
        for record in load_records():
            body = " ".join(record.steps).lower()
            for promise in ("comparison picture", "comparison photograph", "melted-versus-normal picture",
                            "revival clip"):
                self.assertNotIn(promise, body, record.id)


if __name__ == "__main__":
    unittest.main()
