"""The nightly erasure job (spec 2026-10-01): files hidden first, then the
records, the audit record, and the request closed."""

import unittest
from datetime import datetime, timezone

from emotorad_ai import erasure
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.erasure_job import RUN_BY, process
from emotorad_ai.storage.s3 import StorageError

NOW = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
ME, THEM = "PHONE#+919700000031", "PHONE#+919812345678"


class FakeMedia:
    def __init__(self, fail_on=None, store=None):
        self.hidden, self.fail_on, self.store = [], fail_on, store
        self.records_left_when_hiding = []

    def hide(self, key):
        if self.store is not None:
            self.records_left_when_hiding.append(len(self.store.conversations_of(ME)))
        if key == self.fail_on:
            raise StorageError("hide %r failed: AccessDenied" % key)
        self.hidden.append(key)


def person(store, user_key, cid, keys=()):
    state = store.get(cid)
    state.user_key, state.turns = user_key, 1
    store.save(state)
    for n, key in enumerate(keys):
        store.record_media({"_id": key, "key": key, "conversation_id": cid, "stored_at": "2026-10-01T0%d" % n})


class JobTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryConversationStore()
        person(self.store, ME, "mine", ["customers/a/mine/images/1.jpg", "customers/a/mine/images/2.jpg"])
        person(self.store, THEM, "theirs", ["customers/b/theirs/images/1.jpg"])
        self.reference = self.store.request_erasure(ME, "amiigo_app", "mine", "2026-10-01T10:00:00+00:00")

    def test_a_request_is_carried_out_and_closed(self):
        media = FakeMedia(store=self.store)
        (outcome,) = process(self.store, media, lambda: NOW)
        self.assertEqual((outcome.reference, outcome.status, outcome.s3_objects), (self.reference, "done", 2))
        self.assertEqual(media.hidden, ["customers/a/mine/images/1.jpg", "customers/a/mine/images/2.jpg"])
        self.assertEqual(media.records_left_when_hiding, [1, 1])  # files first, records after
        self.assertEqual(self.store.conversations_of(ME), [])
        self.assertEqual(self.store.conversations_of(THEM), ["theirs"])
        (audit,) = self.store.erasure_log
        self.assertEqual(audit, erasure.audit_record(ME, "self-service request %s" % self.reference, RUN_BY, NOW,
                                                     deleted=outcome.counts, s3_objects=2))
        record = self.store.erasure_record(self.reference)
        self.assertEqual(record["status"], "done")
        self.assertNotIn("user_key", record)

    def test_a_hide_failure_keeps_the_records_and_counts_an_attempt(self):
        media = FakeMedia(fail_on="customers/a/mine/images/2.jpg")
        (outcome,) = process(self.store, media, lambda: NOW)
        self.assertEqual((outcome.status, outcome.error), ("retry", "StorageError"))
        self.assertEqual(self.store.conversations_of(ME), ["mine"])
        self.assertEqual(self.store.erasure_record(self.reference)["attempts"], 1)
        self.assertEqual(self.store.erasure_log, [])

    def test_the_third_failure_closes_it_failed(self):
        media = FakeMedia(fail_on="customers/a/mine/images/1.jpg")
        for _ in range(erasure.MAX_ATTEMPTS):
            (outcome,) = process(self.store, media, lambda: NOW)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(self.store.erasure_record(self.reference)["status"], "failed")
        self.assertEqual(process(self.store, media, lambda: NOW), [])

    def test_a_cancelled_request_is_left_alone(self):
        self.store.cancel_erasure(ME, "2026-10-01T11:00:00+00:00")
        self.assertEqual(process(self.store, FakeMedia(), lambda: NOW), [])
        self.assertEqual(self.store.conversations_of(ME), ["mine"])

    def test_files_without_a_bucket_are_a_failure_not_a_skip(self):
        (outcome,) = process(self.store, None, lambda: NOW)
        self.assertEqual(outcome.status, "retry")
        self.assertEqual(self.store.conversations_of(ME), ["mine"])

    def test_a_person_with_no_files_needs_no_bucket(self):
        store = InMemoryConversationStore()
        person(store, ME, "mine")
        store.request_erasure(ME, "amiigo_app", None, "2026-10-01T10:00:00+00:00")
        (outcome,) = process(store, None, lambda: NOW)
        self.assertEqual((outcome.status, outcome.s3_objects), ("done", 0))
