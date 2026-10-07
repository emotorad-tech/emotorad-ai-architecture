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
from emotorad_ai.storage.keys import is_valid_key

CATALOGUE_FILE = pathlib.Path(__file__).resolve().parents[1] / "knowledge" / "_media" / "catalogue.yaml"
EM_DASH = chr(0x2014)

KEPT = {
    "soc_button": "afs/battery/photos/soc-button-non-doodle.png",
    "battery_onoff_switch": "afs/battery/photos/battery-onoff-switch.png",
}
# The melt ask's pictures, code-only (the person's brief, 6 October 2026). The
# battery serial sticker has no photo yet, so it is not here.
CODE_ONLY = {
    "melt_battery_serial": ("library/battery/photos/serial-label-downtube.jpg",
                            "Example: the serial number sticker on the battery"),
    "melt_controller_label": ("library/controller/photos/serial-label.jpg",
                              "Example: the controller's label, next to the battery pins on the frame"),
    "melt_terminals": ("afs/battery/photos/battery-terminals.jpg",
                       "Example: the battery's metal terminals, to film up close"),
}
# The reference library (7 October 2026): assets/library/<domain>/, rebuilt
# from scratch, one entry per photo. Code-only until the rules for when each is
# sent (to the customer, or to the checker) are written.
LIBRARY = {
    "battery_serial_label": ("library/battery/photos/serial-label-downtube.jpg", "battery", "customer_example"),
    "battery_serial_label_doodle": ("library/battery/photos/serial-label-doodle.jpg", "battery", "customer_example"),
    "battery_warranty_seal_intact": ("library/battery/photos/warranty-seal-intact.jpg", "battery", "checker_reference"),
    "battery_warranty_seal_torn": ("library/battery/photos/warranty-seal-torn.jpg", "battery", "checker_reference"),
    "controller_serial_label": ("library/controller/photos/serial-label.jpg", "controller", "customer_example"),
    "motor_serial_number": ("library/motor/photos/serial-number.jpg", "motor", "customer_example"),
    "display_serial_label": ("library/display/photos/serial-label-back.jpg", "display", "customer_example"),
    "frame_number_sticker": ("library/frame/photos/frame-number-sticker.jpg", "frame", "customer_example"),
    # The second batch (7 October 2026): battery help and the melted checks.
    "battery_onoff_switch_photo": ("library/battery/photos/onoff-switch.jpg", "battery", "customer_guide"),
    "battery_soc_button_photo": ("library/battery/photos/soc-button.jpg", "battery", "customer_guide"),
    "battery_soc_button_non_doodle": ("library/battery/photos/soc-button-non-doodle.png", "battery", "customer_guide"),
    "battery_switch_on_position": ("library/battery/photos/switch-on-position.jpg", "battery", "customer_guide"),
    "battery_revival_steps": ("library/battery/videos/revival-steps.mp4", "battery", "customer_guide"),
    "battery_terminals_melted_vs_normal": ("library/battery/photos/terminals-melted-vs-normal.png", "battery",
                                           "checker_reference"),
    "controller_connector_not_melted": ("library/controller/photos/connector-not-melted.jpg", "controller",
                                        "checker_reference"),
    "controller_connector_melted_vs_normal": ("library/controller/photos/connector-melted-vs-normal.png", "controller",
                                              "checker_reference"),
}
USES = ("customer_guide", "customer_example", "checker_reference")


class CatalogueTests(unittest.TestCase):
    def test_the_model_is_offered_exactly_the_two_pictures(self):
        catalogue = model_offered(load_catalogue())
        self.assertEqual({key: item["id"] for key, item in catalogue.items()}, KEPT)

    def test_the_code_only_pictures_are_the_melt_pictures_and_the_library(self):
        catalogue = code_only(load_catalogue())
        self.assertEqual(set(catalogue), set(CODE_ONLY) | set(LIBRARY))
        for key, (asset_id, caption) in CODE_ONLY.items():
            self.assertEqual((catalogue[key]["id"], catalogue[key]["caption"]), (asset_id, caption), key)
        for key, item in catalogue.items():
            self.assertIs(item["code_only"], True, key)
            self.assertEqual(item["kind"], "video" if item["id"].endswith(".mp4") else "image", key)

    def test_every_library_photo_is_catalogued_by_domain_and_use(self):
        catalogue = load_catalogue()
        for key, (asset_id, domain, use) in LIBRARY.items():
            item = catalogue[key]
            self.assertEqual((item["id"], item["domain"], item["use"]), (asset_id, domain, use), key)
            self.assertTrue(is_valid_key("assets/" + asset_id), asset_id)
            self.assertTrue(item["about"].strip(), key)
            self.assertIn(item["use"], USES, key)

    def test_the_library_is_never_offered_to_the_model(self):
        self.assertFalse(set(LIBRARY) & set(model_offered(load_catalogue())))

    def test_the_melt_ask_s_serial_pictures_are_the_library_s(self):
        # 7 October 2026: the battery serial and the controller's serial come
        # from the reference library, so the melt ask can be switched on.
        catalogue = load_catalogue()
        self.assertEqual(catalogue["melt_battery_serial"]["id"], LIBRARY["battery_serial_label"][0])
        self.assertEqual(catalogue["melt_controller_label"]["id"], LIBRARY["controller_serial_label"][0])

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
