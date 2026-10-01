"""The nightly erasure workflow: when it runs, what it runs, and no secret in it."""

import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "erasure-nightly.yml"


class ErasureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_nightly_at_two_in_india_and_by_hand(self):
        triggers = self.workflow[True]  # PyYAML reads the key "on" as True
        self.assertEqual(triggers["schedule"], [{"cron": "30 20 * * *"}])
        self.assertIn("workflow_dispatch", triggers)

    def test_it_runs_the_job_in_a_one_off_container_and_stops_on_error(self):
        self.assertIn("python -m emotorad_ai.erasure_job", self.text)
        self.assertIn("docker run --rm", self.text)
        self.assertIn('\\"set -e\\"', self.text)
        self.assertIn("EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app", self.text)

    def test_no_secret_value_is_in_it(self):
        for needle in ("sk-or-", "mongodb+srv://", "postgresql://", "AKIA"):
            self.assertNotIn(needle, self.text)
