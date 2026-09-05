"""A knowledge record's id becomes a filename wherever a record is written —
the playground drafts one, `export_for_review` copies it. `load_records` must
reject anything that is not a safe filename component, so a malformed id is
caught at authoring time rather than trusted all the way to a write."""

import tempfile
import unittest
from pathlib import Path

import yaml

from emotorad_ai.knowledge import KnowledgeError, load_records

VALID_RECORD = {
    "id": "brakes-squeak",
    "title": "Brakes squeak",
    "topic": "brakes",
    "symptoms": ["squeak", "squeal"],
    "steps": ["Check the pads for glazing."],
}


def _write(directory: Path, record: dict) -> None:
    (directory / "record.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")


class KnowledgeIdValidationTests(unittest.TestCase):
    def test_a_well_formed_id_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, VALID_RECORD)
            records = load_records(directory)
            self.assertEqual(records[0].id, "brakes-squeak")

    def test_an_id_with_spaces_and_uppercase_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, dict(VALID_RECORD, id="Bad Id"))
            with self.assertRaises(KnowledgeError):
                load_records(directory)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, dict(VALID_RECORD, id="abc\n"))
            with self.assertRaises(KnowledgeError):
                load_records(directory)

    def test_an_id_that_looks_like_a_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, dict(VALID_RECORD, id="../../escape"))
            with self.assertRaises(KnowledgeError):
                load_records(directory)


if __name__ == "__main__":
    unittest.main()
