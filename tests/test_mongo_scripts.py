"""The scripts a person runs against the real cluster, proven on mongomock first,
so the first run on the cluster is not also the first run of the script."""

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import mongomock

from emotorad_ai.stores.mongo import ensure_indexes

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SmokeScriptTests(unittest.TestCase):
    def test_every_check_passes_on_a_set_up_database_and_nothing_is_left_behind(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        lines = []
        passed = load("mongo_smoke").run_smoke(client=client, db_name="emotorad_ai", out=lines.append)
        self.assertTrue(passed, "\n".join(lines))
        self.assertEqual(sum(line.startswith("PASS") for line in lines), 14)
        for collection in ("conversations", "transcript_turns", "conversation_summaries", "idempotency_keys"):
            self.assertEqual(db[collection].count_documents({}), 0, collection)

    def test_it_refuses_to_run_before_setup(self):
        lines = []
        passed = load("mongo_smoke").run_smoke(client=mongomock.MongoClient(), db_name="emotorad_ai", out=lines.append)
        self.assertFalse(passed)
        self.assertIn("run mongo_setup.py", "\n".join(lines))


class SetupScriptTests(unittest.TestCase):
    def test_setup_reports_every_collection_and_no_expiry_on_the_record(self):
        client = mongomock.MongoClient()
        module = load("mongo_setup")
        out = io.StringIO()
        env = {"EMOTORAD_MONGO_URI": "mongodb+srv://emotorad-ai-dev:SECRET@emotorad.example.mongodb.net/"}
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "connect", lambda db_name: client[db_name]), redirect_stdout(out):
            code = module.main()
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("setup OK", text)
        self.assertIn("emotorad.example.mongodb.net", text)
        self.assertNotIn("SECRET", text)
        self.assertRegex(text, r"transcript_turns\s+permanent")


class DeletePersonScriptTests(unittest.TestCase):
    def test_a_dry_run_counts_and_yes_deletes(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        db["transcript_turns"].insert_one({"_id": "c#00001", "conversation_id": "c", "n": 1, "user_key": "PHONE#+919876543210"})
        module = load("delete_person")
        with mock.patch.object(module, "connect", lambda db_name: client[db_name]):
            for argv, remaining in ((["--phone", "98765 43210"], 1),
                                    (["--phone", "+91 98765 43210", "--yes", "--reason", "customer email"], 0)):
                out = io.StringIO()
                with mock.patch.object(sys, "argv", ["delete_person.py"] + argv), redirect_stdout(out):
                    module.main()
                self.assertEqual(db["transcript_turns"].count_documents({}), remaining, out.getvalue())


if __name__ == "__main__":
    unittest.main()
