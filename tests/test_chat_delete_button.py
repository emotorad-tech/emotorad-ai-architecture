"""The HTML chat's "Delete my chat data" button (2026-10-01): a dialog asks,
and the request goes straight to /erasure-requests; nothing is sent into the
chat."""

import re
import unittest
from pathlib import Path

from emotorad_ai import erasure

PAGE = Path(__file__).resolve().parents[1] / "web" / "emotorad-support-chat-dev.html"


class DeleteButtonTests(unittest.TestCase):
    def setUp(self):
        self.page = PAGE.read_text(encoding="utf-8")

    def test_the_header_menu_offers_it(self):
        self.assertIn('id="headerMenu"', self.page)
        self.assertRegex(self.page, r'id="menuDelete"[^>]*>\s*Delete my chat data\s*<')

    def test_the_dialog_says_what_the_app_says(self):
        self.assertIn("Delete my conversation data?", self.page)
        self.assertIn(erasure.ERASURE_DIALOG, self.page)
        self.assertRegex(self.page, r'id="eraseConfirm"[^>]*>\s*Delete\s*<')
        self.assertRegex(self.page, r'id="eraseClose"[^>]*>\s*Keep my data\s*<')

    def test_it_calls_the_endpoints_and_not_the_chat(self):
        handlers = self.page[self.page.index("// -- Delete my chat data"):self.page.index("// -- end delete")]
        self.assertIn('erasureCall("/erasure-requests")', handlers)
        self.assertIn('erasureCall("/erasure-requests/cancel")', handlers)
        self.assertIn("confirm: true", handlers)
        self.assertIn("conversation_id: state.conversationId", handlers)
        self.assertNotRegex(handlers, r"send\(")
        self.assertNotIn('"/message"', handlers)
