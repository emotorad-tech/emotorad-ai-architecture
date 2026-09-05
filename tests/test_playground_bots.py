import os
import tempfile
import unittest
from pathlib import Path

from emotorad_ai.bots import DRAFT, BotSpecError
from emotorad_ai.knowledge import KnowledgeError, load_records
from emotorad_ai.playground_bots import (
    delete_draft,
    export_for_review,
    load_catalogue,
    prompt_template,
    save_draft,
    save_draft_knowledge,
)

BRAKES = {
    "name": "brakes_support",
    "persona": "customer",
    "topic": "brakes",
    "keywords": ["brake", "braking"],
    "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
    "prompt": "You are the brake support assistant.\nBe brief.\n",
}

RECORD = {
    "id": "brakes-squeak",
    "title": "Brakes squeak",
    "symptoms": ["squeak", "squeal"],
    "steps": ["Check the pads for glazing."],
    "escalate_when": "Metal-on-metal grinding.",
}


class DraftTests(unittest.TestCase):
    def test_save_draft_writes_yaml_the_catalogue_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            path = save_draft(BRAKES, drafts)
            self.assertEqual(path, drafts / "bots" / "brakes_support.yaml")
            text = path.read_text(encoding="utf-8")
            self.assertIn("prompt: |", text, "multi-line prompts are written in block style so they are reviewable")
            spec = load_catalogue(drafts).get("brakes_support")
            self.assertEqual(spec.source, DRAFT)
            self.assertEqual(spec.prompt, BRAKES["prompt"])

    def test_save_draft_rejects_a_conflict_with_a_built_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BotSpecError):
                save_draft(dict(BRAKES, keywords=["brake", "motor"]), Path(tmp))
            self.assertFalse((Path(tmp) / "bots" / "brakes_support.yaml").exists(), "nothing is written on failure")

    def test_save_draft_overwrites_the_same_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft(BRAKES, drafts)
            save_draft(dict(BRAKES, prompt="Shorter.\n"), drafts)
            self.assertEqual(load_catalogue(drafts).get("brakes_support").prompt, "Shorter.\n")

    def test_delete_draft_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft(BRAKES, drafts)
            delete_draft("brakes_support", drafts)
            with self.assertRaises(KeyError):
                load_catalogue(drafts).get("brakes_support")

    def test_delete_draft_also_removes_its_knowledge_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft(BRAKES, drafts)
            save_draft_knowledge("brakes", [RECORD], drafts)
            self.assertTrue((drafts / "knowledge" / "brakes").exists())

            delete_draft("brakes_support", drafts)

            self.assertFalse((drafts / "bots" / "brakes_support.yaml").exists())
            self.assertFalse(
                (drafts / "knowledge" / "brakes").exists(),
                "deleting a draft must not leave its knowledge/<topic>/ behind",
            )

    def test_delete_draft_keeps_shared_topic_knowledge_until_the_last_claimant_is_gone(self):
        """Topic uniqueness is per persona, so a customer and a dealer draft can
        share a topic (and its knowledge/<topic>/ directory) — deleting one must
        not destroy knowledge the other still needs."""
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft(BRAKES, drafts)
            save_draft(
                {
                    "name": "brakes_dealer",
                    "persona": "dealer",
                    "topic": "brakes",
                    "keywords": ["pad_stock", "rotor_stock"],
                    "tools": ["get_dealer_account", "search_knowledge"],
                    "prompt": "You are the dealer brake support assistant.\n",
                },
                drafts,
            )
            save_draft_knowledge("brakes", [RECORD], drafts)

            delete_draft("brakes_support", drafts)

            self.assertFalse((drafts / "bots" / "brakes_support.yaml").exists())
            self.assertTrue(
                load_catalogue(drafts).get("brakes_dealer"),
                "the dealer draft that still claims the topic must survive",
            )
            self.assertTrue(
                (drafts / "knowledge" / "brakes").exists(),
                "knowledge shared with the surviving dealer draft must not be removed",
            )

            delete_draft("brakes_dealer", drafts)

            self.assertFalse(
                (drafts / "knowledge" / "brakes").exists(),
                "once nothing claims the topic, its knowledge must be removed",
            )

    def test_delete_draft_deletes_a_malformed_spec_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            broken = drafts / "bots"
            broken.mkdir(parents=True)
            (broken / "broken.yaml").write_text("not: [valid, yaml: at all\n", encoding="utf-8")

            delete_draft("broken", drafts)  # must not raise

            self.assertFalse((broken / "broken.yaml").exists())

    def test_delete_draft_is_a_no_op_when_the_spec_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            delete_draft("no_such_bot", Path(tmp))  # must not raise

    def test_draft_knowledge_is_written_in_the_repo_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            paths = save_draft_knowledge("brakes", [RECORD], drafts)
            self.assertEqual(paths, [drafts / "knowledge" / "brakes" / "brakes-squeak.yaml"])
            records = load_records(drafts / "knowledge")
            self.assertEqual(records[0].topic, "brakes")
            self.assertEqual(records[0].symptoms, ("squeak", "squeal"))

    def test_draft_knowledge_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KnowledgeError):
                save_draft_knowledge("brakes", [dict(RECORD, steps=[])], Path(tmp))

    def test_a_path_traversing_id_is_rejected_and_nothing_is_written_outside_its_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            with self.assertRaises(KnowledgeError):
                save_draft_knowledge("brakes", [dict(RECORD, id="../../escape")], drafts)

            allowed = (drafts / "knowledge" / "brakes").resolve()
            for root, _dirs, files in os.walk(tmp):
                for name in files:
                    written = Path(root, name).resolve()
                    self.assertTrue(
                        written.is_relative_to(allowed),
                        "%s was written outside knowledge/brakes/" % written,
                    )

    def test_a_failed_save_restores_previously_valid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft_knowledge("brakes", [RECORD], drafts)
            with self.assertRaises(KnowledgeError):
                save_draft_knowledge(
                    "brakes",
                    [RECORD, dict(RECORD, id="brakes-noise", steps=[])],
                    drafts,
                )
            squeak_path = drafts / "knowledge" / "brakes" / "brakes-squeak.yaml"
            self.assertTrue(squeak_path.exists(), "the previously valid record must survive rollback")
            records = load_records(drafts / "knowledge")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].id, "brakes-squeak")
            self.assertFalse((drafts / "knowledge" / "brakes" / "brakes-noise.yaml").exists())

    def test_a_failed_save_restores_the_pre_call_bytes_of_an_overwritten_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft_knowledge("brakes", [RECORD], drafts)
            with self.assertRaises(KnowledgeError):
                save_draft_knowledge(
                    "brakes",
                    [dict(RECORD, title="Changed"), dict(RECORD, id="brakes-noise", steps=[])],
                    drafts,
                )
            records = load_records(drafts / "knowledge")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].id, "brakes-squeak")
            self.assertEqual(records[0].title, "Brakes squeak")

    def test_export_copies_spec_and_knowledge_into_the_export_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp) / "drafts"
            export = Path(tmp) / "export"
            save_draft(BRAKES, drafts)
            save_draft_knowledge("brakes", [RECORD], drafts)
            paths = export_for_review("brakes_support", drafts, export)
            self.assertEqual(
                sorted(p.relative_to(export).as_posix() for p in paths),
                ["bots/brakes_support.yaml", "knowledge/brakes/brakes-squeak.yaml"],
            )

    def test_prompt_templates_carry_the_persona_rules(self):
        self.assertIn("search_knowledge", prompt_template("customer"))
        self.assertIn("Never ask the customer for their bike model", prompt_template("customer"))
        self.assertIn("Never state a price", prompt_template("dealer"))


if __name__ == "__main__":
    unittest.main()
