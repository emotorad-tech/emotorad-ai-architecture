"""The melt ask (the person's brief, 6 October 2026).

When a customer says something melted, the bot answers with one fixed message
written by code, asking for all three items at once (the battery's serial
sticker, the controller's label, a short video of both ends), with a reference
picture for each in the same reply, and tells them not to use or charge the
bike meanwhile. No model is called on that turn.

It is off unless EMOTORAD_MELT_ASK is exactly "on" and all three catalogue
pictures exist and resolve. The battery serial sticker has no photo yet, so in
the shipped catalogue it stays off.
"""

import unittest

from emotorad_ai import media, melt_ask
from emotorad_ai.knowledge import load_records
from emotorad_ai.media import load_catalogue

SERIAL_KEY = "afs/battery/photos/battery-serial-label.jpg"
SERIAL = {"id": SERIAL_KEY, "kind": "image", "caption": "Example: the serial number sticker on the battery",
          "code_only": True}
ON = {"EMOTORAD_MELT_ASK": "on"}

# The exact texts the person approved (6 October 2026). The Hindi is a draft
# for a Hindi speaker to check before real traffic.
TEXT_EN = (
    "Thanks for telling me. Until we've checked it, please don't use or charge the bike.\n"
    "\n"
    "To check it, please send these three together:\n"
    "1. A photo of the serial number sticker on your battery.\n"
    "2. A photo of the controller's label, on the frame where the battery slots in.\n"
    "3. A short video of the battery's metal terminals and the connector on the frame. "
    "Hold the camera still on each for a second.\n"
    "\n"
    "The pictures below show what each one looks like."
)
TEXT_HI = (
    "बताने के लिए धन्यवाद। जब तक हम इसकी जाँच न कर लें, कृपया बाइक न चलाएँ और न ही चार्ज करें।\n"
    "\n"
    "जाँच के लिए कृपया ये तीनों एक साथ भेजें:\n"
    "1. आपकी बैटरी पर लगे सीरियल नंबर स्टिकर की फ़ोटो।\n"
    "2. फ़्रेम पर, जहाँ बैटरी लगती है, वहाँ कंट्रोलर के लेबल की फ़ोटो।\n"
    "3. बैटरी के धातु वाले टर्मिनल और फ़्रेम के कनेक्टर का एक छोटा वीडियो। "
    "हर एक पर कैमरा एक सेकंड स्थिर रखें।\n"
    "\n"
    "नीचे दी गई तस्वीरों में हर एक का उदाहरण है।"
)

GOLDEN = (
    "something is melted in battery",
    "my battery connector is melted",
    "charging port melted",
    "connector melt ho gaya",
    "terminal pighal gaya",
    "बैटरी का टर्मिनल पिघल गया",
    "E-06",
    # Case, and the code inside a sentence.
    "MY CONNECTOR IS MELTED",
    "the display shows e-06 while charging",
    "pins fused",
)
NEAR_MISSES = ("unmelted", "meltdown", "my battery won't charge", "the bike is fine", "E-061", "XE-06")


class FakeStore:
    """Signs every key, or fails for the keys named in `fail`."""

    def __init__(self, fail=()):
        self.fail = set(fail)

    def presign_get(self, key):
        if any(part in key for part in self.fail):
            raise OSError("cannot sign %s" % key)
        return "https://signed.test/" + key


def full_catalogue():
    """The shipped catalogue with the battery serial picture added, as the
    runbook says to once the photo exists."""
    return dict(load_catalogue(), melt_battery_serial=dict(SERIAL))


def active(store=None):
    ask, status = melt_ask.from_env(full_catalogue(), store or FakeStore(), ON)
    assert ask is not None, status
    return ask


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.pattern = melt_ask.compile_trigger(melt_ask.trigger_phrases())

    def test_the_golden_phrases_trigger(self):
        for text in GOLDEN:
            with self.subTest(text=text):
                self.assertTrue(melt_ask.triggered(self.pattern, text))

    def test_the_near_misses_do_not(self):
        for text in NEAR_MISSES:
            with self.subTest(text=text):
                self.assertFalse(melt_ask.triggered(self.pattern, text))

    def test_the_phrases_are_the_records_symptoms_and_the_devanagari_list(self):
        (record,) = [r for r in load_records() if r.id == "battery-melted-terminal"]
        phrases = melt_ask.trigger_phrases()
        self.assertEqual(set(phrases), set(record.symptoms) | {"पिघल गया", "पिघल गई", "पिघला", "पिघल गए", "मेल्ट"})
        for phrase in ("पिघल गई", "पिघल गए", "मेल्ट"):
            self.assertTrue(melt_ask.triggered(self.pattern, "टर्मिनल " + phrase), phrase)

    def test_the_pattern_uses_neither_word_boundaries_nor_word_classes(self):
        # Devanagari vowel signs fall outside \\w, so \\b and \\w miss Hindi
        # (the rule in CLAUDE.md). ASCII phrases use explicit lookarounds.
        self.assertNotIn("\\b", self.pattern.pattern)
        self.assertNotIn("\\w", self.pattern.pattern)
        self.assertIn("(?<![a-z0-9])", self.pattern.pattern)
        self.assertIn("(?![a-z0-9])", self.pattern.pattern)

    def test_empty_text_does_not_trigger(self):
        self.assertFalse(melt_ask.triggered(self.pattern, ""))
        self.assertFalse(melt_ask.triggered(self.pattern, None))


class SwitchTests(unittest.TestCase):
    def test_on_only_when_exactly_on(self):
        self.assertTrue(melt_ask.switch_on({"EMOTORAD_MELT_ASK": "on"}))
        for value in ("", "ON", "On", " on", "yes", "true", "1"):
            with self.subTest(value=value):
                self.assertFalse(melt_ask.switch_on({"EMOTORAD_MELT_ASK": value}))
        self.assertFalse(melt_ask.switch_on({}))

    def test_off_by_default(self):
        self.assertEqual(melt_ask.from_env(full_catalogue(), FakeStore(), {}), (None, "off"))

    def test_on_but_the_battery_serial_picture_missing_is_off_and_says_so(self):
        # The shipped catalogue today: no serial sticker photo exists yet.
        self.assertEqual(melt_ask.from_env(load_catalogue(), FakeStore(), ON),
                         (None, "off: missing melt_battery_serial"))

    def test_on_with_all_three_resolving_is_on(self):
        ask, status = melt_ask.from_env(full_catalogue(), FakeStore(), ON)
        self.assertEqual(status, "on")
        self.assertEqual([key for key, _ in ask.items], list(melt_ask.KEYS))

    def test_a_picture_that_cannot_resolve_keeps_it_off_and_is_named(self):
        ask, status = melt_ask.from_env(full_catalogue(), FakeStore(fail=("battery-terminals",)), ON)
        self.assertIsNone(ask)
        self.assertEqual(status, "off: unresolvable melt_terminals")

    def test_with_no_media_store_none_resolves(self):
        # No bucket on this deployment: media.resolve falls back to the store
        # from the environment, pinned here to none.
        previous = (media._store, media._store_loaded)
        media._store, media._store_loaded = None, True
        try:
            ask, status = melt_ask.from_env(load_catalogue(), None, ON)
        finally:
            media._store, media._store_loaded = previous
        self.assertIsNone(ask)
        self.assertEqual(status, "off: missing melt_battery_serial; unresolvable melt_controller_label, melt_terminals")

    def test_without_the_knowledge_record_it_is_off_and_says_so(self):
        self.assertEqual(melt_ask.from_env(full_catalogue(), FakeStore(), ON, records=[]),
                         (None, "off: no knowledge record battery-melted-terminal"))

    def test_a_melt_picture_the_model_could_be_offered_keeps_it_off(self):
        catalogue = full_catalogue()
        catalogue["melt_battery_serial"] = dict(SERIAL, code_only=False)
        self.assertEqual(melt_ask.from_env(catalogue, FakeStore(), ON), (None, "off: not code_only melt_battery_serial"))


class TextAndPictureTests(unittest.TestCase):
    def test_the_texts_are_the_approved_ones(self):
        self.assertEqual(melt_ask.TEXT_EN, TEXT_EN)
        self.assertEqual(melt_ask.TEXT_HI, TEXT_HI)
        self.assertNotIn(chr(0x2014), TEXT_EN + TEXT_HI)

    def test_the_pictures_come_in_order_serial_controller_terminals(self):
        pictures, missing = active().pictures()
        self.assertEqual(missing, [])
        self.assertEqual([p.url for p in pictures], [
            "https://signed.test/assets/afs/battery/photos/battery-serial-label.w900.webp",
            "https://signed.test/assets/afs/battery/photos/controller-pins-closeup.w900.webp",
            "https://signed.test/assets/afs/battery/photos/battery-terminals.w900.webp",
        ])
        self.assertEqual({p.kind for p in pictures}, {"image"})
        self.assertEqual(pictures[1].caption, "Example: the controller's label, next to the battery pins on the frame")

    def test_a_picture_that_fails_at_send_time_is_left_out_and_named(self):
        store = FakeStore()
        ask = active(store)
        store.fail.add("controller-pins")
        pictures, missing = ask.pictures()
        self.assertEqual(len(pictures), 2)
        self.assertEqual(missing, ["melt_controller_label"])


if __name__ == "__main__":
    unittest.main()
