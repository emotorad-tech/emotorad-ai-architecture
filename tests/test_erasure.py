"""Self-service erasure: the phrases, the confirmation, the reference and the
audit record (spec 2026-10-01)."""

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from emotorad_ai import erasure


class PhraseTests(unittest.TestCase):
    def test_deletion_phrases(self):
        for text in ("delete my data", "Please DELETE my account", "erase my data", "remove my data",
                     "delete my details", "Delete my conversation data", "forget me",
                     "मेरा डेटा हटाओ", "मेरा डेटा डिलीट कर दो", "quiero borrar mis datos",
                     "eliminar mis datos", "eliminar mi cuenta", "delete   my    data",
                     # Staging, 2026-10-01: "delete my chat" reached the model, which
                     # said it could not delete anything.
                     "delete my chat", "delete my chats", "please delete this chat", "delete our conversation",
                     "delete my conversations", "delete my chat history", "clear my chat", "wipe my history",
                     "delete all my data", "delete all of my chats", "remove my messages",
                     "मेरी चैट डिलीट करो", "borrar mi chat", "eliminar mis conversaciones", "borrar mi historial"):
            self.assertTrue(erasure.wants_deletion(text), text)

    def test_cancel_phrases_win_over_deletion(self):
        for text in ("cancel my deletion", "please cancel the deletion", "don't delete my data",
                     "do not delete my data", "cancel deletion"):
            self.assertTrue(erasure.wants_cancel(text), text)
            self.assertFalse(erasure.wants_deletion(text), text)

    def test_ordinary_sentences_are_not_requests(self):
        for text in ("how do I delete a ride?", "my battery data looks wrong", "remove the battery",
                     "I deleted the app", "delete the photo", "clear the error on the display",
                     "the chat history shows my order", "", None):
            self.assertFalse(erasure.wants_deletion(text), text)
            self.assertFalse(erasure.wants_cancel(text), text)


class ConfirmationTests(unittest.TestCase):
    def test_only_the_word_delete_confirms(self):
        for text in ("DELETE", "delete", "  Delete  "):
            self.assertTrue(erasure.is_confirmation(text), text)
        for text in ("yes", "delete it", "Delete.", "", None):
            self.assertFalse(erasure.is_confirmation(text), text)


class ReferenceTests(unittest.TestCase):
    def test_the_reference_shape(self):
        self.assertRegex(erasure.new_reference(), r"^DEL-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{6}$")
        self.assertEqual(erasure.new_reference(choice=lambda alphabet: alphabet[0]), "DEL-222222")
        self.assertFalse(set("01OILU") & set(erasure.REFERENCE_ALPHABET))


class AuditTests(unittest.TestCase):
    def test_the_record_names_no_one(self):
        at = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
        record = erasure.audit_record("PHONE#+919700000031", "self-service request DEL-222222",
                                      "nightly erasure job", at, deleted={"conversations": 1}, s3_objects=2)
        self.assertEqual(record, {
            "key_sha256": erasure.key_sha256("PHONE#+919700000031"), "kind": "PHONE",
            "reason": "self-service request DEL-222222", "run_by": "nightly erasure job", "at": at,
            "deleted": {"conversations": 1}, "s3_objects": 2,
        })
        self.assertRegex(record["key_sha256"], r"^[0-9a-f]{64}$")

    def test_an_incomplete_record(self):
        at = datetime(2026, 10, 2, tzinfo=timezone.utc)
        record = erasure.audit_record("DEALER#DLR-1", "r", "me", at, s3_objects=1, s3_versions=3, incomplete=True)
        self.assertEqual((record["kind"], record["incomplete"], record["s3_versions"]), ("DEALER", True, 3))
        self.assertNotIn("deleted", record)


class TextTests(unittest.TestCase):
    def test_no_text_has_an_em_dash_and_the_formats_take_a_reference(self):
        for name in ("ERASURE_CONFIRM", "ERASURE_DIALOG", "ERASURE_REQUESTED", "ERASURE_KEPT", "ERASURE_EXISTING",
                     "ERASURE_CANCELLED", "ERASURE_NOTHING_TO_CANCEL", "ERASURE_FAILED", "ERASURE_SIGN_IN"):
            self.assertNotIn("\u2014", getattr(erasure, name), name)
        for name in ("ERASURE_REQUESTED", "ERASURE_EXISTING", "ERASURE_CANCELLED"):
            self.assertIn("DEL-222222", getattr(erasure, name).format(reference="DEL-222222"))
        self.assertTrue(erasure.ERASURE_CONFIRM.startswith(erasure.ERASURE_DIALOG))
        self.assertTrue(re.search(r"Reply DELETE to confirm", erasure.ERASURE_CONFIRM))


class BareDeleteTests(unittest.TestCase):
    """Staging, 2026-10-01: "delete" on its own went to the model, which said
    it had deleted the chat. Nothing was deleted, and nothing should be without
    the confirmation."""

    def test_delete_on_its_own_is_a_request(self):
        for text in ("delete", "Delete it", "delete this", "please delete everything", "erase it all", "DELETE!"):
            self.assertTrue(erasure.wants_deletion(text), text)

    def test_other_deletes_still_are_not(self):
        for text in ("delete the photo", "how do I delete a ride?", "can I delete the app"):
            self.assertFalse(erasure.wants_deletion(text), text)


ROOT = Path(__file__).resolve().parents[1]


class ManualPromiseTests(unittest.TestCase):
    """Manual erasure spec, section 3: a person deletes, within 30 days."""

    def test_no_text_promises_a_nightly_run(self):
        for name in ("ERASURE_CONFIRM", "ERASURE_DIALOG", "ERASURE_REQUESTED", "ERASURE_EXISTING"):
            text = getattr(erasure, name)
            self.assertNotIn("tonight", text.lower(), name)
            self.assertIn("within 30 days", text, name)

    def test_the_two_new_texts(self):
        self.assertEqual(erasure.ERASURE_REQUESTED.format(reference="DEL-7K3P9Q"), (
            "Your deletion request is DEL-7K3P9Q. Our team will check it and delete everything this chat "
            "holds about you within 30 days. If you change your mind before then, say 'cancel my deletion'."))
        self.assertEqual(erasure.ERASURE_EXISTING.format(reference="DEL-7K3P9Q"), (
            "You've already asked for this. Your request is DEL-7K3P9Q, and our team will complete it "
            "within 30 days."))

    def test_the_contract_promises_the_team_not_a_job(self):
        contract = (ROOT / "docs" / "contracts" / "amiigo-support-chat.md").read_text(encoding="utf-8")
        self.assertNotIn("nightly", contract)
        self.assertNotIn("tonight", contract)
        self.assertIn("Our team checks each request and deletes the data within 30 days.", contract)
