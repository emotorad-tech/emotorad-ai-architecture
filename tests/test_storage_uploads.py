"""Presign, then claim. A claim verifies the object S3 actually holds against
what was presigned, so a client cannot upload something other than what it
declared and have the runtime trust it."""

import unittest

from emotorad_ai.storage.uploads import Claimed, UploadError, UploadRegistry


class _Store:
    def __init__(self):
        self.objects = {}
        self.presigned = []

    def presign_put(self, key, mime, size):
        self.presigned.append((key, mime, size))
        return {"url": "https://signed/" + key, "headers": {"Content-Type": mime, "Content-Length": str(size)}, "expires_in": 300}

    def head(self, key):
        return self.objects.get(key)


class BeginTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.now = [1_000.0]
        self.reg = UploadRegistry(self.store, clock=lambda: self.now[0])

    def test_customer_presign_derives_the_key_and_pins_type_and_size(self):
        pending, presign = self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)
        self.assertTrue(pending.key.startswith("customers/clu_1/conv_1/images/upl_"))
        self.assertTrue(pending.key.endswith(".jpg"))
        self.assertEqual(pending.kind, "images")
        self.assertEqual(self.store.presigned, [(pending.key, "image/jpeg", 1234)])
        self.assertEqual(presign["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(pending.expires_at, 1_300.0)

    def test_an_unclaimed_presign_is_swept_by_the_next_begin(self):
        """A presign nobody claims must not sit in memory forever: the next
        begin_* sweeps anything past PUT_EXPIRY + CLAIM_WINDOW."""
        stale, _ = self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)
        self.now[0] = 1_000.0 + 300 + 3600 + 1
        fresh, _ = self.reg.begin_asset("afs", "battery", "photos", "soc-button", "image/png", 10)
        self.assertNotIn(stale.upload_id, self.reg._pending)
        self.assertIn(fresh.upload_id, self.reg._pending)

    def test_a_live_presign_survives_the_sweep(self):
        live, _ = self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)
        self.now[0] = 1_000.0 + 300 + 3600
        self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)
        self.assertIn(live.upload_id, self.reg._pending)

    def test_asset_presign_uses_the_assets_tree(self):
        pending, _ = self.reg.begin_asset("afs", "battery", "photos", "soc-button", "image/png", 10)
        self.assertEqual(pending.key, "assets/afs/battery/photos/soc-button.png")
        self.assertEqual(pending.tree, "assets")

    def test_unsupported_type_is_415(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_customer("clu_1", "conv_1", "image/gif", 10)
        self.assertEqual(caught.exception.status, 415)

    def test_over_cap_is_413_and_zero_is_400(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 10 * 1024 * 1024 + 1)
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 0)
        self.assertEqual(caught.exception.status, 400)

    def test_bad_path_segments_are_400(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_asset("afs", "Battery", "photos", "x", "image/png", 10)
        self.assertEqual(caught.exception.status, 400)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.now = [1_000.0]
        self.reg = UploadRegistry(self.store, clock=lambda: self.now[0])
        self.pending, _ = self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)

    def test_a_matching_object_is_claimed_once(self):
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/jpeg"}
        claimed = self.reg.claim(self.pending.upload_id)
        self.assertEqual(claimed, Claimed(self.pending.upload_id, self.pending.key, "image/jpeg", 1234, "images"))
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 404, "an id is single-use")

    def test_a_missing_object_is_409(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 409)

    def test_a_mismatched_size_or_type_is_409_and_the_id_stays(self):
        self.store.objects[self.pending.key] = {"size": 99, "mime": "image/jpeg"}
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 409)
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/png"}
        with self.assertRaises(UploadError):
            self.reg.claim(self.pending.upload_id)

    def test_an_unknown_id_is_404(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.claim("upl_nope")
        self.assertEqual(caught.exception.status, 404)

    def test_an_expired_presign_cannot_be_claimed(self):
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/jpeg"}
        self.now[0] = 1_000.0 + 300 + 3600 + 1
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 404)

    def test_peek_returns_the_pending_entry_and_leaves_it_claimable(self):
        peeked = self.reg.peek(self.pending.upload_id)
        self.assertEqual(peeked, self.pending)
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/jpeg"}
        claimed = self.reg.claim(self.pending.upload_id)
        self.assertEqual(claimed, Claimed(self.pending.upload_id, self.pending.key, "image/jpeg", 1234, "images"))

    def test_peek_on_an_unknown_id_is_none(self):
        self.assertIsNone(self.reg.peek("upl_nope"))

    def test_peek_on_an_expired_id_is_none(self):
        self.now[0] = 1_000.0 + 300 + 3600 + 1
        self.assertIsNone(self.reg.peek(self.pending.upload_id))

    def test_concurrent_claims_of_one_id_succeed_exactly_once(self):
        import threading

        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/jpeg"}
        gate = threading.Barrier(4)
        original_head = self.store.head

        def slow_head(key):
            gate.wait(timeout=5)  # every thread has read `pending` before any pops
            return original_head(key)

        self.store.head = slow_head
        results = []

        def attempt():
            try:
                results.append(("ok", self.reg.claim(self.pending.upload_id)))
            except UploadError as exc:
                results.append(("err", exc.status))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sorted(r[0] for r in results), ["err", "err", "err", "ok"])
        self.assertTrue(all(r[1] == 404 for r in results if r[0] == "err"))


if __name__ == "__main__":
    unittest.main()
