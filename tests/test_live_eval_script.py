import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.live_eval_helpers import Factory

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_eval.py"
TINY = """scenarios:
  - id: tiny
    family: routing
    who: {channel: website, session: sess-ananya}
    turns:
      - text: "my battery won't charge"
        expect: {path: narrow}
cost_mixes:
  typical: {tiny: 10}
  worst: {tiny: 10}
"""


def load():
    spec = importlib.util.spec_from_file_location("live_eval_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(argv, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out):
        code = load().main(argv, **kwargs)
    return code, out.getvalue()


class LiveEvalScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tiny = Path(self.directory.name) / "tiny.yaml"
        self.tiny.write_text(TINY, encoding="utf-8")
        self.out = Path(self.directory.name) / "reports"

    def test_list_sends_nothing_and_needs_no_key(self):
        def never(settings):
            raise AssertionError("--list must not build a model")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            code, text = run(["--list"], models_factory=never)
        self.assertEqual(code, 0)
        self.assertIn("routing-narrow-wont-charge", text)
        self.assertIn("Nothing was sent.", text)

    def test_a_missing_key_stops_before_anything_is_sent(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            code, text = run(["--scenarios", str(self.tiny), "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("cannot start: OPENROUTER_API_KEY is not set", text)

    def test_a_run_writes_the_report_and_exits_0_when_everything_passes(self):
        code, text = run(["--scenarios", str(self.tiny), "--out", str(self.out)],
                         models_factory=Factory(), sleep=lambda s: None)
        self.assertEqual(code, 0, text)
        [report] = list(self.out.glob("*/report.html"))
        self.assertIn("Report: %s" % report, text)
        self.assertIn("1 pass", text)

    def test_a_broken_scenario_file_is_reported_not_raised(self):
        self.tiny.write_text(TINY.replace("family: routing", "family: routng"), encoding="utf-8")
        code, text = run(["--scenarios", str(self.tiny), "--list"])
        self.assertEqual(code, 2)
        self.assertIn("scenario file:", text)


if __name__ == "__main__":
    unittest.main()
