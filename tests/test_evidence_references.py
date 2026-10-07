"""EMotorad's reference pictures in the evidence check (7 October 2026).

A battery chat's evidence goes to Gemini with the library's melted and normal
comparisons in front of it, labelled as ours and never as evidence. They are
read from the bucket once (the 900 px WebP copy), and a picture that cannot be
read is left out and named, never a reason to stop the check.
"""

import base64
import unittest

from fastapi.testclient import TestClient

from emotorad_ai.contract import Reply
from emotorad_ai.evidence_check import (CUSTOMER_MEDIA_LINE, REFERENCE_KEYS, REFERENCES_INTRO, EvidenceCheckError,
                                        References)
from emotorad_ai.media import load_catalogue
from tests.test_api_evidence_check import FakeEvidenceChecker, photo
from tests.test_api_media_persistence import _Store, fresh_api
from tests.test_evidence_check import COMPLAINT, PHOTO, FakeTransport, checker
from unittest import mock

TERMINALS = "assets/library/battery/photos/terminals-melted-vs-normal.w900.webp"
CONNECTOR = "assets/library/controller/photos/connector-melted-vs-normal.w900.webp"
NOT_MELTED = "assets/library/controller/photos/connector-not-melted.w900.webp"
REF = (b"RIFFwebp-ref", "image/webp", "Reference: a melted battery terminal next to a normal one")


class Bucket:
    def __init__(self, objects=None):
        self.objects = dict(objects if objects is not None else
                            {TERMINALS: b"terminals", CONNECTOR: b"connector", NOT_MELTED: b"not-melted"})
        self.fetched = []

    def get_bytes(self, key):
        self.fetched.append(key)
        return self.objects[key]


class RequestTests(unittest.TestCase):
    def test_references_go_first_labelled_as_ours_then_the_customers_media(self):
        transport = FakeTransport()
        checker(transport).check([PHOTO], COMPLAINT, "battery", references=[REF])
        content = transport.posts[0]["body"]["messages"][0]["content"]
        self.assertEqual(content[2], {"type": "text", "text": REFERENCES_INTRO})
        self.assertEqual(content[3], {"type": "text", "text": REF[2]})
        self.assertEqual(content[4], {"type": "image_url", "image_url": {
            "url": "data:image/webp;base64," + base64.b64encode(REF[0]).decode()}})
        self.assertEqual(content[5], {"type": "text", "text": CUSTOMER_MEDIA_LINE})
        self.assertEqual(content[6]["image_url"]["url"],
                         "data:image/jpeg;base64," + base64.b64encode(PHOTO[0]).decode())
        self.assertEqual(len(content), 7)

    def test_the_intro_says_they_are_never_evidence(self):
        for words in ("not the customer's", "never evidence", "customer's photos and videos alone"):
            self.assertIn(words, REFERENCES_INTRO)

    def test_references_take_their_size_off_the_customers_share(self):
        transport = FakeTransport()
        big = (b"x" * 95, "image/webp", "ref")
        with self.assertRaises(EvidenceCheckError) as caught:
            checker(transport, inline_limit=100).check([PHOTO], COMPLAINT, "battery", references=[big])
        self.assertEqual(str(caught.exception), "too_large")
        self.assertEqual(transport.posts, [])


class ReferencesTests(unittest.TestCase):
    def test_battery_gets_the_three_comparisons_from_their_webp_copies(self):
        bucket = Bucket()
        found, missing = References(load_catalogue(), bucket).for_component("battery")
        self.assertEqual(missing, [])
        self.assertEqual(bucket.fetched, [TERMINALS, CONNECTOR, NOT_MELTED])
        self.assertEqual([(data, mime) for data, mime, _ in found],
                         [(b"terminals", "image/webp"), (b"connector", "image/webp"), (b"not-melted", "image/webp")])
        self.assertEqual(found[0][2], "Reference: a melted battery terminal next to a normal one")

    def test_each_is_read_once(self):
        bucket = Bucket()
        references = References(load_catalogue(), bucket)
        references.for_component("battery")
        references.for_component("battery")
        self.assertEqual(len(bucket.fetched), 3)

    def test_a_picture_the_bucket_lacks_is_left_out_and_named_and_tried_again(self):
        bucket = Bucket({TERMINALS: b"terminals", NOT_MELTED: b"not-melted"})
        references = References(load_catalogue(), bucket)
        found, missing = references.for_component("battery")
        self.assertEqual((len(found), missing), (2, ["controller_connector_melted_vs_normal"]))
        bucket.objects[CONNECTOR] = b"connector"
        found, missing = references.for_component("battery")
        self.assertEqual((len(found), missing), (3, []))

    def test_a_picture_missing_from_the_catalogue_is_named(self):
        catalogue = load_catalogue()
        del catalogue["battery_terminals_melted_vs_normal"]
        _, missing = References(catalogue, Bucket()).for_component("battery")
        self.assertEqual(missing, ["battery_terminals_melted_vs_normal"])

    def test_the_motor_has_none_yet(self):
        self.assertEqual(References(load_catalogue(), Bucket()).for_component("motor"), ([], []))
        self.assertNotIn("motor", REFERENCE_KEYS)

    def test_every_reference_is_a_code_only_checker_reference(self):
        catalogue = load_catalogue()
        for keys in REFERENCE_KEYS.values():
            for key in keys:
                self.assertTrue(catalogue[key].get("code_only"), key)
                self.assertEqual(catalogue[key].get("use"), "checker_reference", key)


class TakesReferences(FakeEvidenceChecker):
    def check(self, media, complaint, component, deadline_at=None, cancel=None, references=None):
        verdict = super().check(media, complaint, component, deadline_at=deadline_at, cancel=cancel)
        self.calls[-1]["references"] = references
        return verdict


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.addCleanup(lambda: fresh_api(None))
        self.client = TestClient(self.api.app)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.checker = TakesReferences()
        self.api.EVIDENCE_CHECKER = self.checker
        self.api.runtime.evidence_check = True
        patch = mock.patch.object(self.api.runtime, "handle",
                                  side_effect=lambda m: Reply(conversation_id="c1", text="ok", handled_by="test"))
        patch.start()
        self.addCleanup(patch.stop)

    def post(self, agent, bucket):
        self.api.stores.conversations.get("c1").route_to(agent)
        with mock.patch.object(self.api, "EVIDENCE_REFERENCES", References(self.api.CATALOGUE, bucket)):
            r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                                   "text": "here is the battery terminal", "attachments": [photo()]})
        self.assertEqual(r.status_code, 200, r.text)
        (call,) = self.checker.calls
        return call

    def test_a_battery_chats_evidence_is_checked_with_the_references(self):
        call = self.post("battery_support", Bucket())
        self.assertEqual([data for data, _, _ in call["references"]], [b"terminals", b"connector", b"not-melted"])

    def test_a_motor_chats_is_checked_without(self):
        call = self.post("motor_support", Bucket())
        self.assertIsNone(call["references"])

    def test_with_none_readable_the_check_still_runs_and_the_gap_is_logged(self):
        call = self.post("battery_support", Bucket({}))
        self.assertIsNone(call["references"])
        (event,) = [e for e in self.api.log.events if e["event"] == "evidence_references_missing"]
        self.assertEqual(len(event["keys"]), 3)


if __name__ == "__main__":
    unittest.main()
