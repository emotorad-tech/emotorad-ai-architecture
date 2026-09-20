"""Sentences in the live prompt that a code change depends on.

The prompt is the owner's, and it is tuned in the playground. These tests do
not judge its wording; they stop a retune from silently dropping a rule the
tools or the guardrails rely on. Each test names the code that relies on it.
"""

import pathlib
import unittest

PROMPT = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "battery_support.md"


class PromptRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = PROMPT.read_text(encoding="utf-8")

    def test_the_address_is_collected_pincode_first(self):
        """place_replacement_order refuses an address without a pincode."""
        self.assertIn("pincode first", self.text)

    def test_the_address_is_read_back_once_before_ordering(self):
        """The provenance check accepts a read-back the customer agreed to."""
        self.assertIn("read the whole address back once", self.text)

    def test_pincode_required_is_handled(self):
        self.assertIn("`pincode_required`", self.text)

    def test_a_picture_is_not_narrated_as_arriving(self):
        """Seen live: 'That comparison photo is on its way to you now', twice."""
        self.assertIn("do not narrate its arrival", self.text)

    def test_the_idempotency_key_carries_no_name(self):
        """A model-chosen key put the customer's name in the order store."""
        self.assertIn("frame number and the part", self.text)

    def test_a_display_photo_is_asked_for_on_any_code(self):
        """lookup_error_code is not in the chat slice. The photo request was
        bundled with the call, so when the model skipped the call it skipped
        the photo too. Conversation b186a5dd never saw the display."""
        self.assertIn("whether or not `lookup_error_code` is available", self.text)
