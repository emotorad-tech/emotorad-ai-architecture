"""A reply that says data was deleted when it was not (staging, 2026-10-01)."""

import unittest

from emotorad_ai.guardrails import claims_deletion


class DeletionClaimTests(unittest.TestCase):
    def test_claims(self):
        for reply in ("OK, I've deleted this chat and all the data it held.",
                      "I have removed your data.",
                      "Your chat has been deleted.",
                      "Done. Your conversation history has been erased."):
            self.assertTrue(claims_deletion(reply), reply)

    def test_not_claims(self):
        for reply in ("I cannot delete the case.",
                      "I haven't deleted anything.",
                      "Your deletion request is DEL-H9FECT. Everything will be deleted in tonight's run.",
                      "Shall I delete it?",
                      ""):
            self.assertFalse(claims_deletion(reply), reply)
