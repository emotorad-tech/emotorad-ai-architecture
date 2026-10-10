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

from emotorad_ai.storage.s3 import StorageError
from emotorad_ai.stores.mongo import ensure_indexes

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

MEDIA_KEY = "customers/cl_1/c/images/upl_1.jpg"


def seed_person_with_media(db, conversation_id="c", user_key="PHONE#+919876543210"):
    db["transcript_turns"].insert_one(
        {"_id": conversation_id + "#00001", "conversation_id": conversation_id, "n": 1, "user_key": user_key}
    )
    db["media"].insert_one({
        "_id": MEDIA_KEY,
        "bucket": "emotorad-ai-stage-media",
        "key": MEDIA_KEY,
        "uri": "s3://emotorad-ai-stage-media/" + MEDIA_KEY,
        "kind": "image",
        "mime_type": "image/jpeg",
        "size_bytes": 100,
        "conversation_id": conversation_id,
        "cluster_id": "cl_1",
        "source": "upload",
        "stored_at": "2026-09-29T00:00:00+00:00",
    })


class FakeMediaStore:
    """Stands in for S3Store: no boto3, no network, just the one method
    delete_person.py calls."""

    def __init__(self, versions=None, fail_on=None):
        self.calls = []
        self._versions = versions or {}
        self._fail_on = fail_on

    def delete_every_version(self, key):
        self.calls.append(key)
        if key == self._fail_on:
            raise StorageError("delete %r failed: AccessDenied" % key)
        return self._versions.get(key, 1)


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
        # Customer media is kept permanently (decision 2026-09-29), so its
        # record is part of the permanent record and must never get a TTL.
        self.assertRegex(text, r"\bmedia\s+permanent")
        # So are the ticket records, and the counter behind their references:
        # one that expired would hand out EM-1000001 again.
        self.assertRegex(text, r"\btickets\s+permanent")
        self.assertRegex(text, r"\bcounters\s+permanent")

    def test_only_a_collection_with_a_ttl_index_is_printed_as_one_that_expires(self):
        # The Task 7 review: conversation_origins and erasure_requests have
        # no TTL index, yet were printed as "expires". And a chat's notices
        # are part of the chat (Ruling 21): permanent.
        client = mongomock.MongoClient()
        module = load("mongo_setup")
        out = io.StringIO()
        env = {"EMOTORAD_MONGO_URI": "mongodb+srv://emotorad-ai-dev:SECRET@emotorad.example.mongodb.net/"}
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "connect", lambda db_name: client[db_name]), \
                redirect_stdout(out):
            self.assertEqual(module.main(), 0)
        kept = dict(line.split()[:2] for line in out.getvalue().splitlines() if line.startswith("  "))
        expiring = {name for name, label in kept.items() if label == "expires"}
        self.assertEqual(expiring, {"conversations", "idempotency_keys", "verification_sessions", "amiigo_receipts"})
        for name in ("conversation_notices", "conversation_origins", "erasure_requests", "serial_readings",
                     "invoice_readings"):
            self.assertEqual(kept[name], "permanent", name)
        db = client["emotorad_ai"]
        for name, label in kept.items():
            ttl = any("expireAfterSeconds" in index for index in db[name].index_information().values())
            self.assertEqual(label == "expires", ttl, name)


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
                self.assertRegex(out.getvalue(), r"transcript_turns\s+1\b")  # the count, before anything goes
                self.assertEqual(db["transcript_turns"].count_documents({}), remaining, out.getvalue())


class DeletePersonMediaScriptTests(unittest.TestCase):
    def _run(self, module, client, argv, store_from_env):
        out = io.StringIO()
        with mock.patch.object(module, "connect", lambda db_name: client[db_name]), \
             mock.patch.object(module, "store_from_env", store_from_env), \
             mock.patch.object(sys, "argv", ["delete_person.py"] + argv), redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    def test_dry_run_lists_media_keys_and_deletes_nothing(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        seed_person_with_media(db)
        module = load("delete_person")
        fake_s3 = FakeMediaStore()
        code, text = self._run(module, client, ["--phone", "98765 43210"], lambda: fake_s3)
        self.assertEqual(code, 0)
        self.assertIn(MEDIA_KEY, text)
        self.assertEqual(fake_s3.calls, [])
        self.assertEqual(db["media"].count_documents({}), 1)

    def test_yes_deletes_s3_objects_then_records_and_audits_the_counts(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        seed_person_with_media(db)
        module = load("delete_person")
        fake_s3 = FakeMediaStore(versions={MEDIA_KEY: 3})
        argv = ["--phone", "98765 43210", "--yes", "--reason", "customer email"]
        code, text = self._run(module, client, argv, lambda: fake_s3)
        self.assertEqual(code, 0, text)
        self.assertEqual(fake_s3.calls, [MEDIA_KEY])
        self.assertEqual(db["media"].count_documents({}), 0)
        self.assertEqual(db["transcript_turns"].count_documents({}), 0)
        audit = db["erasure_log"].find_one()
        self.assertEqual(audit["s3_objects"], 1)
        self.assertEqual(audit["s3_versions"], 3)

    def test_yes_refuses_when_the_bucket_is_not_configured_and_media_exists(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        seed_person_with_media(db)
        module = load("delete_person")
        argv = ["--phone", "98765 43210", "--yes", "--reason", "customer email"]
        code, text = self._run(module, client, argv, lambda: None)
        self.assertEqual(code, 2)
        self.assertIn("nothing was deleted", text.lower())
        self.assertEqual(db["media"].count_documents({}), 1)
        self.assertEqual(db["transcript_turns"].count_documents({}), 1)
        self.assertEqual(db["erasure_log"].count_documents({}), 0)

    def test_yes_proceeds_as_before_when_there_is_no_media_even_without_a_bucket(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        db["transcript_turns"].insert_one(
            {"_id": "c#00001", "conversation_id": "c", "n": 1, "user_key": "PHONE#+919876543210"}
        )
        module = load("delete_person")
        argv = ["--phone", "98765 43210", "--yes", "--reason", "customer email"]
        code, text = self._run(module, client, argv, lambda: None)
        self.assertEqual(code, 0, text)
        self.assertEqual(db["transcript_turns"].count_documents({}), 0)

    def test_an_s3_failure_stops_before_any_record_is_deleted_and_marks_the_audit_incomplete(self):
        client = mongomock.MongoClient()
        db = client["emotorad_ai"]
        ensure_indexes(db)
        seed_person_with_media(db)
        module = load("delete_person")
        fake_s3 = FakeMediaStore(fail_on=MEDIA_KEY)
        argv = ["--phone", "98765 43210", "--yes", "--reason", "customer email"]
        code, text = self._run(module, client, argv, lambda: fake_s3)
        self.assertEqual(code, 1)
        self.assertEqual(db["media"].count_documents({}), 1)
        self.assertEqual(db["transcript_turns"].count_documents({}), 1)
        audit = db["erasure_log"].find_one()
        self.assertIsNotNone(audit)
        self.assertTrue(audit["incomplete"])


if __name__ == "__main__":
    unittest.main()
