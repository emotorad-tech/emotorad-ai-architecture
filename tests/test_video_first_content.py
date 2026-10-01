"""Video first in what the model reads (spec 2026-10-01-video-first-evidence-design.md)."""

import re
import unittest
from pathlib import Path

from emotorad_ai.agents import motor_support, narrow_support
from emotorad_ai.knowledge import load_records
from emotorad_ai.prompts import load_base_prompt

ROOT = Path(__file__).resolve().parents[1]
# A step telling the bot to get a photo from the customer.
ASKS_FOR_PHOTO = re.compile(
    r"(?<!not\s)(?<!never\s)\b(?:ask|asks|asking)\s+(?:the\s+customer\s+)?(?:for|to)\b[^.]{0,90}?"
    r"\b(?:photos?|photographs?|pictures?)\b"
    r"|\bcollect\b[^.]{0,40}?\bphotographs?\b"
    r"|\bwith\s+a\s+photo\b|\b(?:send|take)\s+a\s+photo\b|\bphotograph\s+the\b",
    re.IGNORECASE,
)
MEDIA = re.compile(r"\b(?:videos?|photos?|photographs?|pictures?)\b", re.IGNORECASE)


class KnowledgeTests(unittest.TestCase):
    def test_every_step_that_asks_for_a_photo_asks_for_a_video_first(self):
        offenders = []
        for record in load_records():
            for number, step in enumerate(record.steps, start=1):
                match = ASKS_FOR_PHOTO.search(step)
                if match is None:
                    continue
                first = MEDIA.search(step, match.start())
                if first is None or not first.group(0).lower().startswith("video"):
                    offenders.append("%s step %d" % (record.id, number))
        self.assertEqual(offenders, [])


class PromptTests(unittest.TestCase):
    def test_the_battery_prompt(self):
        text = load_base_prompt("battery_support")
        self.assertIn("**A short video first, a photo only if they can't.**", text)
        self.assertIn("they get a request for a video instead", text)
        self.assertIn("may ask for a short video", text)
        self.assertNotIn("close the chat gracefully", text)

    def test_the_motor_prompt(self):
        self.assertIn("ask for a short video of it", motor_support._BASE_PROMPT)

    def test_the_narrow_rules(self):
        self.assertIn("ask for a short video first, and a photo only if they cannot take one", narrow_support._RULES)

    def test_the_contract(self):
        contract = (ROOT / "docs" / "contracts" / "amiigo-support-chat.md").read_text(encoding="utf-8")
        self.assertIn("The bot asks for a short video first, and a photo if the rider can't take one.", contract)
