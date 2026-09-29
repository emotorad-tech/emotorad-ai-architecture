"""scripts/e2e_report.py: a saved console run as a static, chat-like report."""

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

RUN = {
    "at": "2026-09-29T16:56:05Z",
    "server": "mode openrouter · store memory · media configured",
    "results": [
        {"id": "smoke-typed", "title": "Smoke photo, and it says so", "why": "Typed hazard words.",
         "conversation_id": "e2e-smoke-typed-1", "status": "finding",
         "steps": [{"sent": "There is <smoke>!", "photo": True, "ms": 27,
                    "reply": {"text": "Please stop using the battery.\n\nThey will call you.", "handled_by": "guardrail:battery_safety",
                              "escalated": True, "ticket_id": None},
                    "media": [{"uri": "s3://local-folder/customers/cl/c/images/u.jpg", "source": "inline", "size_bytes": 247810}],
                    "checks": [{"ok": True, "label": "handed to a person", "got": ""},
                               {"ok": False, "label": "does not say \"number linked to your account\"", "got": "call you on the number"}]}]},
        {"id": "verify", "title": "Verifies by code", "why": "Identity.", "conversation_id": "e2e-verify-1", "status": "pass",
         "steps": [{"sent": "My battery won't charge.", "photo": False, "ms": 2458,
                    "reply": {"text": "Does your wall socket work?", "handled_by": "narrow_support", "escalated": False, "ticket_id": None},
                    "media": None, "checks": [{"ok": True, "label": "one step per reply", "got": ""}]}]},
    ],
}


def load():
    spec = importlib.util.spec_from_file_location("e2e_report", ROOT / "scripts" / "e2e_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = load()

    def test_the_whole_run_lists_findings_first_then_every_conversation(self):
        html = self.report.render(RUN, photo="smoke.jpg")
        self.assertLess(html.index("Findings"), html.index("Smoke photo, and it says so"))
        self.assertIn("1 of 2 scenarios passed", html)
        self.assertIn("does not say &quot;number linked to your account&quot;", html)
        self.assertIn("Verifies by code", html)

    def test_what_the_customer_and_the_bot_wrote_is_escaped(self):
        html = self.report.render(RUN, photo="smoke.jpg")
        self.assertIn("There is &lt;smoke&gt;!", html)
        self.assertNotIn("<smoke>", html)

    def test_a_photo_turn_shows_the_photo_and_its_permanent_address(self):
        html = self.report.render(RUN, photo="smoke.jpg")
        self.assertIn('src="smoke.jpg"', html)
        self.assertIn("s3://local-folder/customers/cl/c/images/u.jpg", html)
        self.assertIn("guardrail:battery_safety", html)

    def test_one_scenario_alone_for_a_screenshot(self):
        html = self.report.render(RUN, photo="smoke.jpg", only="verify")
        self.assertIn("Verifies by code", html)
        self.assertNotIn("Smoke photo, and it says so", html)

    def test_the_script_writes_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory) / "run.json"
            import json

            run.write_text(json.dumps(RUN), encoding="utf-8")
            out = pathlib.Path(directory) / "report.html"
            self.assertEqual(self.report.main([str(run), "--photo", "smoke.jpg", "--out", str(out)]), 0)
            self.assertIn("Smoke photo, and it says so", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
