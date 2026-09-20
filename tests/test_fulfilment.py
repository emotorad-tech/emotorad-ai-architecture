"""Replacement fulfilment: the deterministic half.

Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.
Everything here is code the model cannot argue with.
"""

import os
import unittest
from unittest import mock

from emotorad_ai.config import Settings, load_settings


class ApprovalModeSettingTests(unittest.TestCase):
    def test_defaults_to_reasonable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMOTORAD_AI_APPROVAL_MODE", None)
            self.assertEqual(load_settings().approval_mode, "reasonable")

    def test_reads_the_environment(self):
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_APPROVAL_MODE": "human"}):
            self.assertEqual(load_settings().approval_mode, "human")

    def test_an_unknown_mode_is_refused_at_startup(self):
        """A typo must not silently become 'the bot approves everything'."""
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_APPROVAL_MODE": "yolo"}):
            with self.assertRaises(ValueError):
                load_settings()


if __name__ == "__main__":
    unittest.main()
