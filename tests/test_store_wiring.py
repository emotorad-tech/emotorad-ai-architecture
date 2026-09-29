import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mongomock

from emotorad_ai import cli
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.stores.mongo import MONGO_URI_ENV, MongoConversationStore, MongoIdempotencyStore
from emotorad_ai.tools.registry import IdempotencyStore
from emotorad_ai.wiring import build_stores


class BuildStoresTests(unittest.TestCase):
    def test_memory_is_the_default(self):
        stores = build_stores(Settings(store="memory"))
        self.assertIsInstance(stores.conversations, InMemoryConversationStore)
        self.assertIsInstance(stores.idempotency, IdempotencyStore)

    def test_mongodb_builds_both_stores_on_one_database_with_the_configured_windows(self):
        client = mongomock.MongoClient()
        stores = build_stores(Settings(store="mongodb", state_ttl_hours=24, idempotency_ttl_days=3), client=client)
        self.assertIsInstance(stores.conversations, MongoConversationStore)
        self.assertIsInstance(stores.idempotency, MongoIdempotencyStore)
        self.assertEqual(stores.conversations._state_ttl.total_seconds(), 24 * 3600)
        self.assertEqual(stores.idempotency._ttl.days, 3)

    def test_mongodb_without_a_connection_string_fails_loudly(self):
        with mock.patch.dict(os.environ, {MONGO_URI_ENV: ""}):
            with self.assertRaises(StoreUnavailable):
                build_stores(Settings(store="mongodb"))


class CliStoreFlagTests(unittest.TestCase):
    def test_the_cli_runs_on_the_memory_store_by_flag(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--offline", "--store", "memory", "--channel", "amiigo", "--session", "sess-amiigo-test", "hi"])
        self.assertEqual(code, 0)
        self.assertIn("assistant:", out.getvalue())

    def test_the_cli_runs_on_mongodb_by_flag(self):
        client = mongomock.MongoClient()
        real = build_stores
        with mock.patch.object(cli, "build_stores", lambda settings, log=None: real(settings, log=log, client=client)):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["--offline", "--store", "mongodb", "--channel", "amiigo", "--session", "sess-amiigo-test", "hi"])
        self.assertEqual(code, 0)
        self.assertEqual(client["emotorad_ai"]["transcript_turns"].count_documents({}), 2)


if __name__ == "__main__":
    unittest.main()
