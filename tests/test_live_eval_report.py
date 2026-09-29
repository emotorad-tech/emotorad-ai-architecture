import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emotorad_ai.live_eval.report import write_report
from emotorad_ai.live_eval.runner import run_suite
from emotorad_ai.live_eval.scenarios import Expect
from emotorad_ai.openrouter import OpenRouterTransport
from emotorad_ai.wiring import build_models
from tests.live_eval_helpers import BASE, TODAY, Factory, priced, scenario

MIXES = {"typical": {"s": 10}, "worst": {"s": 10}}


def run_with(reply_text):
    return run_suite([scenario(expect=Expect(path=("narrow",)))], BASE, budget=1.0,
                     models_factory=Factory(replies=[priced(reply_text)]), sleep=lambda s: None, today=TODAY)


def write(run):
    with tempfile.TemporaryDirectory() as directory:
        html_path, json_path = write_report(run, MIXES, Path(directory) / "out")
        return html_path.read_text(encoding="utf-8"), json.loads(json_path.read_text(encoding="utf-8"))


class ReportTests(unittest.TestCase):
    def test_both_files_are_written_and_agree(self):
        page, data = write(run_with("Try another socket."))
        [only] = data["scenarios"]
        self.assertEqual((only["id"], only["status"], only["runs"]), ("s", "pass", 1))
        self.assertEqual(data["projection"]["worst"]["ten"], round(only["cost"] * 10, 6))
        for text in ("my battery won&#x27;t charge", "Try another socket.", "Wording notes", "Projected cost",
                     "$%.4f" % data["projection"]["worst"]["ten"]):
            self.assertIn(text, page)

    def test_model_output_is_shown_as_text_never_run(self):
        page, _ = write(run_with("Check the <script>alert(1)</script> fuse."))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_a_blocked_reply_is_shown_with_why(self):
        claim = "Good news: your battery is covered under warranty, so the replacement is free."
        page, data = write(run_with(claim))
        [turn] = data["scenarios"][0]["outcomes"][0][0]["turns"]
        self.assertEqual((turn["blocked_reason"], turn["suppressed"]), ("coverage_claim_without_tool_result", claim))
        self.assertIn("Blocked by guardrail:coverage_post_check (coverage_claim_without_tool_result). The model wrote:", page)
        self.assertIn(claim, page)

    def test_the_key_never_reaches_the_report(self):
        # The real wiring, so the key is read from the environment and put on
        # the request. Scripted models never touch the key, so a test on them
        # could not fail. The leak is an error that echoes the request's
        # headers, as a proxy's refusal can; nothing leaves this machine.
        def echoing_opener(request, timeout=None):
            raise RuntimeError("proxy refused the request: %s" % dict(request.header_items()))

        def models(settings):
            return build_models(settings, transport=OpenRouterTransport(opener=echoing_opener))

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-LIVEEVALTESTKEY"}):
            run = run_suite([scenario(expect=Expect(path=("narrow",)))], BASE, budget=1.0,
                            models_factory=models, sleep=lambda s: None, today=TODAY)
            page, data = write(run)
        self.assertIn("RuntimeError: proxy refused the request", data["scenarios"][0]["outcomes"][0][0]["error"])
        self.assertNotIn("LIVEEVALTESTKEY", page)
        self.assertNotIn("LIVEEVALTESTKEY", json.dumps(data))

    def test_an_aborted_or_budget_limited_run_says_so(self):
        run = run_with("Try another socket.")
        run.aborted, run.skipped = "OpenRouter rejected the key", ["later-one"]
        page, data = write(run)
        self.assertIn("Stopped: OpenRouter rejected the key", page)
        self.assertIn("Not run, budget reached: later-one", page)
        self.assertEqual(data["skipped"], ["later-one"])


if __name__ == "__main__":
    unittest.main()
