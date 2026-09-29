"""Photos in the live-eval scenarios.

Two things changed underneath the scenario file on 2026-09-29. A fault ticket
now needs a photo the model was shown (the evidence rule, enforced at the
tool), so the scenarios that expect a ticket must send one. And images now
reach OpenRouter (llm.to_openai_messages), so the provider fetches an http(s)
image itself: the old https://example.test/... photo never resolves, and a
live run would fail on it. A scenario names a photo in tests/data/live_media/
as `fixture:<file>`, and the loader inlines it as a data URL.
"""

import base64
import tempfile
import textwrap
import unittest
from pathlib import Path

from emotorad_ai.live_eval.scenarios import DEFAULT_PATH, Known, ScenarioError, known_from_code, load_suite

KNOWN = Known(
    tools=frozenset({"create_support_ticket"}),
    records=frozenset(),
    standard=frozenset(),
    agents=frozenset({"battery_support"}),
)

SUITE = """
scenarios:
  - id: a
    family: tools
    who: {channel: website, session: sess-ananya}
    turns:
      - text: "here it is"
        attachments: [{kind: image, url: '%s'}]
cost_mixes:
  typical: {a: 10}
  worst: {a: 10}
"""


def load_with(url, files=()):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "live_media").mkdir()
        for name, data in files:
            (root / "live_media" / name).write_bytes(data)
        path = root / "scenarios.yaml"
        path.write_text(textwrap.dedent(SUITE % url), encoding="utf-8")
        return load_suite(path, KNOWN)


class FixturePhotoTests(unittest.TestCase):
    def test_a_fixture_photo_is_inlined_as_a_data_url(self):
        suite = load_with("fixture:charger.jpg", [("charger.jpg", b"\xff\xd8\xffJPEG")])
        [attachment] = suite.scenarios[0].turns[0].attachments
        self.assertEqual(attachment["kind"], "image")
        self.assertEqual(attachment["url"], "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffJPEG").decode())

    def test_a_png_keeps_its_type(self):
        suite = load_with("fixture:p.png", [("p.png", b"\x89PNG")])
        self.assertTrue(suite.scenarios[0].turns[0].attachments[0]["url"].startswith("data:image/png;base64,"))

    def test_a_missing_fixture_fails_when_the_file_is_read(self):
        with self.assertRaises(ScenarioError) as caught:
            load_with("fixture:nope.jpg")
        self.assertIn("nope.jpg", str(caught.exception))

    def test_a_fixture_must_be_a_plain_file_name(self):
        for name in ("../secret.jpg", "sub/p.jpg", "..\\p.jpg"):
            with self.subTest(name=name), self.assertRaises(ScenarioError):
                load_with("fixture:" + name, [("p.jpg", b"x")])

    def test_only_image_types_are_accepted(self):
        with self.assertRaises(ScenarioError):
            load_with("fixture:notes.txt", [("notes.txt", b"hello")])


class TheRealFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_suite(DEFAULT_PATH, known_from_code())

    def test_no_scenario_sends_a_photo_the_provider_would_have_to_fetch(self):
        for scenario in self.suite.scenarios:
            for turn in scenario.turns:
                for attachment in turn.attachments:
                    with self.subTest(scenario=scenario.id):
                        self.assertTrue(attachment["url"].startswith("data:image/"), attachment["url"][:40])

    def test_every_turn_that_expects_a_ticket_follows_a_photo(self):
        for scenario in self.suite.scenarios:
            photo_seen = False
            for index, turn in enumerate(scenario.turns):
                photo_seen = photo_seen or bool(turn.attachments)
                # A safety ticket is raised on the customer's word (exempt).
                if turn.expect.ticket and "guardrail:battery_safety" not in turn.expect.handled_by:
                    with self.subTest(scenario=scenario.id, turn=index + 1):
                        self.assertTrue(photo_seen, "a fault ticket needs a photo the model was shown")

    def test_the_rule_itself_is_checked_live(self):
        [scenario] = [s for s in self.suite.scenarios if s.id == "tools-no-ticket-without-photo"]
        self.assertFalse(any(turn.attachments for turn in scenario.turns))
        last = scenario.turns[-1].expect
        self.assertIs(last.ticket, False)
        self.assertIn("photo", last.mentions_any)


if __name__ == "__main__":
    unittest.main()
