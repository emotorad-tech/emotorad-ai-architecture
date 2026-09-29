"""The permanent media record (spec 2026-09-29-customer-media-in-s3-design.md
§2) and the two stores that keep it: never a presigned URL, never pruned,
gone the moment the conversation it belongs to is erased.
"""

import unittest

import mongomock

from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.media_records import media_record
from emotorad_ai.stores.mongo import INDEXES, MEDIA, MongoConversationStore, ensure_indexes


def fresh_mongo_db():
    return mongomock.MongoClient()["emotorad_ai"]


# -- the builder --------------------------------------------------------------


class MediaRecordTests(unittest.TestCase):
    def test_the_builder_shapes_every_field_and_the_uri(self):
        record = media_record(
            bucket="emotorad-ai-stage-media",
            key="customers/cl_ab12/c1/images/upl_9f.jpg",
            kind="image",
            mime_type="image/jpeg",
            size_bytes=183422,
            conversation_id="c1",
            cluster_id="cl_ab12",
            source="upload",
            stored_at="2026-09-29T10:00:00+00:00",
        )
        self.assertEqual(record, {
            "_id": "customers/cl_ab12/c1/images/upl_9f.jpg",
            "bucket": "emotorad-ai-stage-media",
            "key": "customers/cl_ab12/c1/images/upl_9f.jpg",
            "uri": "s3://emotorad-ai-stage-media/customers/cl_ab12/c1/images/upl_9f.jpg",
            "kind": "image",
            "mime_type": "image/jpeg",
            "size_bytes": 183422,
            "conversation_id": "c1",
            "cluster_id": "cl_ab12",
            "source": "upload",
            "stored_at": "2026-09-29T10:00:00+00:00",
        })

    def test_size_bytes_is_stored_as_an_int(self):
        record = media_record("b", "k", "image", "image/jpeg", "12345", "c1", "cl1", "inline", "2026-09-29T10:00:00+00:00")
        self.assertEqual(record["size_bytes"], 12345)
        self.assertIsInstance(record["size_bytes"], int)

    def test_a_query_string_in_the_key_is_refused(self):
        """Kills: a caller passing a presigned URL's path straight through,
        which would put a credential in the permanent record."""
        with self.assertRaises(ValueError):
            media_record("bkt", "customers/cl1/c1/images/a.jpg?X-Amz-Signature=abc", "image", "image/jpeg", 1,
                         "c1", "cl1", "upload", "2026-09-29T10:00:00+00:00")

    def test_x_amz_anywhere_in_bucket_or_key_is_refused_case_insensitively(self):
        for bucket, key in (
            ("bkt", "customers/cl1/c1/images/a.jpg?x-amz-signature=abc"),
            ("bkt", "customers/cl1/c1/images/a.jpg?X-AMZ-Credential=abc"),
            ("emotorad-x-amz-media", "customers/cl1/c1/images/a.jpg"),
        ):
            with self.assertRaises(ValueError):
                media_record(bucket, key, "image", "image/jpeg", 1, "c1", "cl1", "upload", "2026-09-29T10:00:00+00:00")

    def test_an_empty_bucket_or_key_is_refused(self):
        with self.assertRaises(ValueError):
            media_record("", "customers/cl1/c1/images/a.jpg", "image", "image/jpeg", 1, "c1", "cl1", "upload", "2026-09-29T10:00:00+00:00")
        with self.assertRaises(ValueError):
            media_record("bkt", "", "image", "image/jpeg", 1, "c1", "cl1", "upload", "2026-09-29T10:00:00+00:00")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            media_record("bkt", "k", "audio", "audio/mpeg", 1, "c1", "cl1", "upload", "2026-09-29T10:00:00+00:00")

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            media_record("bkt", "k", "image", "image/jpeg", 1, "c1", "cl1", "webhook", "2026-09-29T10:00:00+00:00")

    def test_video_and_document_and_inline_are_accepted(self):
        video = media_record("bkt", "k1", "video", "video/mp4", 1, "c1", "cl1", "upload", "2026-09-29T10:00:00+00:00")
        document = media_record("bkt", "k2", "document", "application/pdf", 1, "c1", "cl1", "inline", "2026-09-29T10:00:00+00:00")
        self.assertEqual((video["kind"], document["kind"], document["source"]), ("video", "document", "inline"))


# -- both conversation stores: record, list, erase -----------------------------


def a_record(key, conversation_id="c1", cluster_id="cl1", stored_at="2026-09-29T10:00:00+00:00", size_bytes=100):
    return media_record("bkt", key, "image", "image/jpeg", size_bytes, conversation_id, cluster_id, "inline", stored_at)


class MediaStoreContract:
    """Both `InMemoryConversationStore` and `MongoConversationStore` keep media
    the same way: mixed into a TestCase per store, as tests/store_contract.py
    does for the rest of the conversation record."""

    def make_store(self):
        raise NotImplementedError

    def test_record_media_is_found_by_conversation(self):
        store = self.make_store()
        store.record_media(a_record("customers/cl1/c1/images/a.jpg"))
        self.assertEqual(store.media_of("c2"), [])
        [only] = store.media_of("c1")
        self.assertEqual(only["uri"], "s3://bkt/customers/cl1/c1/images/a.jpg")

    def test_media_of_lists_oldest_stored_at_first(self):
        store = self.make_store()
        newer = a_record("customers/cl1/c1/images/b.jpg", stored_at="2026-09-29T11:00:00+00:00")
        older = a_record("customers/cl1/c1/images/a.jpg", stored_at="2026-09-29T10:00:00+00:00")
        store.record_media(newer)
        store.record_media(older)
        self.assertEqual([r["key"] for r in store.media_of("c1")],
                         ["customers/cl1/c1/images/a.jpg", "customers/cl1/c1/images/b.jpg"])

    def test_record_media_upserts_by_id_not_by_duplicating(self):
        store = self.make_store()
        key = "customers/cl1/c1/images/a.jpg"
        store.record_media(a_record(key, size_bytes=100))
        store.record_media(a_record(key, size_bytes=999))  # the same object, claimed twice
        [only] = store.media_of("c1")
        self.assertEqual(only["size_bytes"], 999)

    def test_delete_conversation_removes_its_media_and_reports_the_count(self):
        store = self.make_store()
        store.record_media(a_record("customers/cl1/c1/images/a.jpg"))
        store.record_media(a_record("customers/cl1/c1/images/b.jpg"))
        store.record_media(a_record("customers/cl1/c2/images/c.jpg", conversation_id="c2"))
        counts = store.delete_conversation("c1")
        self.assertEqual(counts["media"], 2)
        self.assertEqual(store.media_of("c1"), [])
        self.assertEqual(len(store.media_of("c2")), 1)  # someone else's conversation, untouched


class InMemoryMediaStoreTests(MediaStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryConversationStore()


class MongoMediaStoreTests(MediaStoreContract, unittest.TestCase):
    def make_store(self):
        db = fresh_mongo_db()
        ensure_indexes(db)
        return MongoConversationStore(db)

    def test_delete_conversation_honours_dry_run(self):
        store = self.make_store()
        store.record_media(a_record("customers/cl1/c1/images/a.jpg"))
        counts = store.delete_conversation("c1", dry_run=True)
        self.assertEqual(counts["media"], 1)
        self.assertEqual(len(store.media_of("c1")), 1)  # nothing actually removed


# -- indexes: permanent, no TTL -------------------------------------------------


class MediaIndexTests(unittest.TestCase):
    def test_the_media_collection_has_a_conversation_index_and_no_ttl(self):
        self.assertIn(MEDIA, INDEXES)
        for _, options in INDEXES[MEDIA]:
            self.assertNotIn("expireAfterSeconds", options)

    def test_ensure_indexes_creates_the_media_collection(self):
        db = fresh_mongo_db()
        report = ensure_indexes(db)
        self.assertIn(MEDIA, report)
        self.assertIn("conversation", db[MEDIA].index_information())


if __name__ == "__main__":
    unittest.main()
