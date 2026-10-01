"""The daily erasure check (manual erasure spec, section 4): when it runs, that
it only checks, and no secret in it."""

import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "erasure-check.yml"
DELETES = ("erasure_admin delete", "erasure_job", "delete_person")


class ErasureCheckWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_daily_at_nine_in_india_and_by_hand(self):
        triggers = self.workflow[True]  # PyYAML reads the key "on" as True
        self.assertEqual(triggers["schedule"], [{"cron": "30 3 * * *"}])
        self.assertIn("workflow_dispatch", triggers)

    def test_it_runs_only_the_check_in_a_one_off_container_and_stops_on_error(self):
        self.assertIn("python -m emotorad_ai.erasure_admin check", self.text)
        for needle in DELETES:
            self.assertNotIn(needle, self.text)
        self.assertIn("docker run --rm", self.text)
        self.assertIn('\\"set -e\\"', self.text)
        self.assertIn("EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app", self.text)

    def test_the_nightly_workflow_is_gone(self):
        self.assertFalse((WORKFLOWS / "erasure-nightly.yml").exists())

    def test_no_scheduled_workflow_deletes(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            if "schedule" in (yaml.safe_load(text).get(True) or {}):
                for needle in DELETES:
                    self.assertNotIn(needle, text, path.name)

    def test_no_secret_value_is_in_it(self):
        for needle in ("sk-or-", "mongodb+srv://", "postgresql://", "AKIA"):
            self.assertNotIn(needle, self.text)
