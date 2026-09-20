"""What may and may not be written to the event log.

`redact_pii` was applied to the inbound message text and nowhere else, so a
phone number typed by a customer was redacted on the way in and then written out
in full a few lines later as an argument to `request_identity_verification`.
Observed 2026-09-20 in logs/conversations.jsonl while testing with real numbers.

The log is gitignored and untracked, so none of this reached the public repo. It
still sat unencrypted on a laptop.
"""

import unittest

from emotorad_ai.observability import EventLog, redact_pii


class RedactPiiTests(unittest.TestCase):
    def test_a_bare_mobile_number_goes(self):
        self.assertEqual(redact_pii("call me on 9876500000"), "call me on [phone]")

    def test_an_international_form_goes(self):
        self.assertEqual(redact_pii("+919876500000"), "[phone]")

    def test_an_email_goes(self):
        self.assertIn("[email]", redact_pii("write to a@b.com"))

    def test_ordinary_words_are_left_alone(self):
        self.assertEqual(redact_pii("my battery is dead"), "my battery is dead")


class ToolArgumentsAreRedactedTests(unittest.TestCase):
    def setUp(self):
        self.log = EventLog(path=None)

    def test_a_phone_in_tool_arguments_is_redacted(self):
        self.log.tool_call("c1", "request_identity_verification", {"phone": "9876500000"}, {"data": {}})
        self.assertNotIn("9876500000", str(self.log.events[-1]))

    def test_a_one_time_code_is_redacted(self):
        """Six digits are too short for the number patterns to catch, so the code
        is removed by the name of the field it arrives in."""
        self.log.tool_call("c1", "verify_identity", {"code": "039760"}, {"data": {}})
        self.assertNotIn("039760", str(self.log.events[-1]))

    def test_a_code_typed_as_a_message_is_redacted(self):
        """The customer types the code into the chat, so it arrives as message
        text before it ever reaches a tool."""
        self.assertNotIn("039760", redact_pii("039760"))

    def test_an_already_masked_number_survives(self):
        """The masked form is what the agent is meant to read back to the
        customer. Redacting it would make the log useless for checking that."""
        self.log.tool_call("c1", "request_identity_verification", {}, {"data": {"phone_masked": "•••••••000"}})
        self.assertIn("•••••••000", str(self.log.events[-1]))

    def test_the_tool_name_and_shape_survive(self):
        """Redaction must not cost us the audit trail. Which tool ran, and
        whether it failed, is the whole point of the log."""
        self.log.tool_call("c1", "verify_identity", {"code": "039760"}, {"error": {"code": "verification_failed"}})
        event = self.log.events[-1]
        self.assertEqual(event["tool"], "verify_identity")
        self.assertFalse(event["ok"])

    def test_nested_values_are_reached(self):
        self.log.tool_call("c1", "lookup_warranty_record", {}, {"data": {"bikes": [{"mobile": "9876500000"}]}})
        self.assertNotIn("9876500000", str(self.log.events[-1]))


class AttachmentsNeverReachTheLogTests(unittest.TestCase):
    """A customer's photo of their own bike, and their garage, and whoever is
    standing in it, must not be written to a file on somebody's laptop.

    It would also be useless there: a megabyte of base64 on one line makes the
    log unreadable for the thing it exists for.
    """

    def setUp(self):
        self.log = EventLog(path=None)

    def test_an_image_data_uri_is_dropped(self):
        payload = "data:image/jpeg;base64," + ("A" * 5000)
        self.log.emit("inbound", "c1", attachments=[{"kind": "image", "url": payload}])
        written = str(self.log.events[-1])
        self.assertNotIn("AAAA", written)
        self.assertLess(len(written), 500)

    def test_the_fact_of_an_attachment_survives(self):
        """That a photo was sent, and what kind, is exactly what the log is for."""
        self.log.emit(
            "inbound", "c1",
            attachments=[{"kind": "image", "url": "data:image/jpeg;base64," + ("A" * 5000)}],
        )
        written = str(self.log.events[-1])
        self.assertIn("image", written)

    def test_an_ordinary_url_is_left_alone(self):
        """Guide media is a public catalogue URL and is meant to be readable."""
        url = "https://res.cloudinary.com/cjdq4bv8/image/upload/SOC_Button"
        self.log.emit("outcome", "c1", attachments=[{"kind": "image", "url": url}])
        self.assertIn(url, str(self.log.events[-1]))


class ErrorCodesStayReadableTests(unittest.TestCase):
    """Redacting `code` to hide one-time codes also hid every error code.

    On 2026-09-20 a refused order was logged as {"error": {"code":
    "[redacted]", ...}} and the refusal could only be read from its message
    text. A log that cannot say which error fired is not doing its one job.
    """

    def setUp(self):
        self.log = EventLog(path=None)

    def test_an_error_envelopes_code_is_kept(self):
        self.log.tool_call("c1", "place_replacement_order", {}, {"error": {"code": "address_unconfirmed", "message": "m"}})
        self.assertIn("address_unconfirmed", str(self.log.events[-1]))

    def test_a_one_time_code_argument_is_still_hidden(self):
        self.log.tool_call("c1", "verify_identity", {"code": "039760"}, {"data": {}})
        self.assertNotIn("039760", str(self.log.events[-1]))

    def test_a_code_nested_under_data_is_still_hidden(self):
        """Only the error envelope is exempt. A tool that echoed a code back
        in its data would still be redacted."""
        self.log.tool_call("c1", "some_tool", {}, {"data": {"code": "039760"}})
        self.assertNotIn("039760", str(self.log.events[-1]))


if __name__ == "__main__":
    unittest.main()
