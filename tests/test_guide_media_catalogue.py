"""The guide pictures the bot may send: only the two the person chose
(2026-09-30), both in emotorad-ai-stage-media under assets/afs/battery/photos/.

The melt ask's pictures (6 October 2026) sit in the same catalogue marked
`code_only: true`: code attaches them to its fixed reply, and the model is
never offered them."""

import pathlib
import unittest

import yaml

from emotorad_ai.knowledge import load_records
from emotorad_ai.media import code_only, load_catalogue, model_offered

CATALOGUE_FILE = pathlib.Path(__file__).resolve().parents[1] / "knowledge" / "_media" / "catalogue.yaml"
EM_DASH = chr(0x2014)

KEPT = {
    "soc_button": "afs/battery/photos/soc-button-non-doodle.png",
    "battery_onoff_switch": "afs/battery/photos/battery-onoff-switch.png",
}
# The melt ask's pictures, code-only (the person's brief, 6 October 2026). The
# battery serial sticker has no photo yet, so it is not here.
CODE_ONLY = {
    "melt_controller_label": ("afs/battery/photos/controller-pins-closeup.jpg",
                              "Example: the controller's label, next to the battery pins on the frame"),
    "melt_terminals": ("afs/battery/photos/battery-terminals.jpg",
                       "Example: the battery's metal terminals, to film up close"),
}


class CatalogueTests(unittest.TestCase):
    def test_the_model_is_offered_exactly_the_two_pictures(self):
        catalogue = model_offered(load_catalogue())
        self.assertEqual({key: item["id"] for key, item in catalogue.items()}, KEPT)

    def test_the_code_only_pictures_are_exactly_the_two_melt_pictures(self):
        catalogue = code_only(load_catalogue())
        self.assertEqual({key: (item["id"], item["caption"]) for key, item in catalogue.items()}, CODE_ONLY)
        for key, item in catalogue.items():
            self.assertIs(item["code_only"], True, key)
            self.assertEqual(item["kind"], "image", key)

    def test_the_battery_serial_picture_is_not_there_yet_and_the_header_says_how_to_add_it(self):
        self.assertNotIn("melt_battery_serial", load_catalogue())
        # One comment line names the key, the planned S3 key, its webp copy
        # and the switch.
        needed = ("melt_battery_serial", "afs/battery/photos/battery-serial-label.jpg", ".w900.webp",
                  "EMOTORAD_MELT_ASK=on")
        lines = [line for line in CATALOGUE_FILE.read_text(encoding="utf-8").splitlines() if line.startswith("#")]
        self.assertTrue(any(all(part in line for part in needed) for line in lines), lines)

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

    def test_no_prompt_or_error_code_names_a_picture_it_no_longer_has(self):
        # Finding 8 of the final review: the battery prompt still listed the
        # revival clip as a key, and E-06's text promised comparison pictures.
        root = CATALOGUE_FILE.parents[2]
        files = sorted((root / "prompts").glob("*.md")) + sorted((root / "knowledge" / "_errors").glob("*.yaml"))
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for promise in ("battery_revival", "comparison picture", "comparison photo", "short clip",
                            "melted_battery_terminal", "melted_controller_connector"):
                self.assertNotIn(promise, text, "%s: %s" % (path.name, promise))

    def test_no_record_tells_the_bot_to_send_a_picture_it_no_longer_has(self):
        for record in load_records():
            body = " ".join(record.steps).lower()
            for promise in ("comparison picture", "comparison photograph", "melted-versus-normal picture",
                            "revival clip"):
                self.assertNotIn(promise, body, record.id)


if __name__ == "__main__":
    unittest.main()
