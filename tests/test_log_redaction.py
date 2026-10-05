"""What may and may not be written to the event log.

`redact_pii` was applied to the inbound message text and nowhere else, so a
phone number typed by a customer was redacted on the way in and then written out
in full a few lines later as an argument to `request_identity_verification`.
Observed 2026-09-20 in logs/conversations.jsonl while testing with real numbers.

The log is gitignored and untracked, so none of this reached the public repo. It
still sat unencrypted on a laptop.
"""

import json
import unittest

from emotorad_ai.observability import EventLog, redact_fields, redact_pii


class RedactPiiTests(unittest.TestCase):
    def test_a_bare_mobile_number_goes(self):
        self.assertEqual(redact_pii("call me on 9876500000"), "call me on [phone]")

    def test_an_international_form_goes(self):
        self.assertEqual(redact_pii("+919876500000"), "[phone]")

    def test_an_email_goes(self):
        self.assertIn("[email]", redact_pii("write to a@b.com"))

    def test_ordinary_words_are_left_alone(self):
        self.assertEqual(redact_pii("my battery is dead"), "my battery is dead")


class IdentifiersAreNotPhonesTests(unittest.TestCase):
    """A random id can hold a run of digits that reads as a mobile. The CI
    deploy gate failed once on this: an S3 key logged with part of its
    customer id replaced by [phone], so the log named an object that does
    not exist."""

    def test_digits_inside_a_uuid_are_left_alone(self):
        key = "customers/b0c90584-4584-4cd2-8a28-2be5db526ec4/c1/images/upl_00muo4g98q7c3ywwn2.jpg"
        self.assertEqual(redact_pii(key), key)

    def test_digits_glued_to_hex_letters_are_left_alone(self):
        self.assertEqual(redact_pii("run-a9876543210f"), "run-a9876543210f")

    def test_a_number_after_a_label_still_goes(self):
        self.assertEqual(redact_pii("mobile: 9876543210, call me"), "mobile: [phone], call me")
        self.assertEqual(redact_pii("reach me on +91 98765-43210."), "reach me on [phone].")

    def test_an_id_that_starts_with_ten_digits_and_letters_is_left_alone(self):
        """Spec 2026-10-05, section 8. The glued-number rule must not reach
        into an identifier. In a key the digits follow a slash or an
        underscore. In a hex id the letters are a to f only, or they run on
        into more digits, a dash or a file extension."""
        for text in (
            "customers/c1/images/9876543210abcd.jpg",
            "upl_9876543210qx",
            "9876543210abcdef",
            "9876543210ab12cd",
            "trace 9876543210abc-4cd2-8a28 saved",
        ):
            self.assertEqual(redact_pii(text), text)

    def test_a_hex_id_in_a_logged_field_is_left_alone(self):
        event = EventLog(path=None).emit("tool_call", "c1", result={"data": {"id": "9876543210abcdef"}})
        self.assertEqual(event["result"]["data"]["id"], "9876543210abcdef")


class ToolArgumentsAreRedactedTests(unittest.TestCase):
    def setUp(self):
        self.log = EventLog(path=None)

    def test_a_phone_in_tool_arguments_is_redacted(self):
        self.log.tool_call("c1", "request_identity_verification", {"phone": "9876500000"}, {"data": {}})
        self.assertNotIn("9876500000", str(self.log.events[-1]))
        # By name, not by the phone pattern (which would say "[phone]").
        self.assertEqual(self.log.events[-1]["arguments"]["phone"], "[redacted]")

    def test_a_one_time_code_is_redacted(self):
        """Six digits are too short for the number patterns to catch, so the code
        is removed by the name of the field it arrives in."""
        self.log.tool_call("c1", "verify_identity", {"code": "039760"}, {"data": {}})
        self.assertNotIn("039760", str(self.log.events[-1]))
        # By name: the bare-digits rule alone would say "[6 digits]".
        self.assertEqual(self.log.events[-1]["arguments"]["code"], "[redacted]")

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


class BareDigitsTests(unittest.TestCase):
    """A pincode and a one-time code are both six digits typed alone. The log
    cannot tell them apart, so it must not claim to. Conversation b186a5dd
    logged the customer's pincode as [code]."""

    def test_six_bare_digits_are_hidden_without_being_called_a_code(self):
        out = redact_pii("122018")
        self.assertNotIn("122018", out)
        self.assertEqual(out, "[6 digits]")

    def test_the_count_is_honest(self):
        self.assertEqual(redact_pii("1234"), "[4 digits]")


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
        self.assertEqual(self.log.events[-1]["result"]["data"]["code"], "[redacted]")

    def test_a_code_nested_deeper_under_error_is_still_hidden(self):
        """The exemption is by immediate parent, not by anything above it. A
        code inside a sub-object of the error envelope is not itself the
        error's own code, so it is not exempt."""
        self.log.tool_call(
            "c1", "some_tool", {},
            {"error": {"code": "keep", "details": {"code": "039760"}}},
        )
        logged = str(self.log.events[-1])
        self.assertIn("keep", logged)
        self.assertNotIn("039760", logged)
        self.assertEqual(self.log.events[-1]["result"]["error"]["details"]["code"], "[redacted]")

    def test_a_list_named_errors_is_not_exempt(self):
        """The exemption is keyed on the literal parent name "error", singular.
        A list of error dicts under "errors" is not that shape."""
        self.log.tool_call("c1", "some_tool", {}, {"errors": [{"code": "039760"}]})
        self.assertNotIn("039760", str(self.log.events[-1]))
        self.assertEqual(self.log.events[-1]["result"]["errors"][0]["code"], "[redacted]")


class GluedNumbersTests(unittest.TestCase):
    """A number typed straight against a word (spec 2026-10-05, section 8).
    Transcripts now go to Zoho, so whatever the log misses, a third party keeps."""

    def test_english_letters_after_the_number(self):
        self.assertEqual(redact_pii("9876543210pls"), "[phone]pls")
        self.assertEqual(redact_pii("call me on 9876543210asap."), "call me on [phone]asap.")
        self.assertEqual(redact_pii("ring 98765 43210please"), "ring [phone]please")
        self.assertEqual(redact_pii("+919876543210pls"), "[phone]pls")

    def test_hinglish_letters_after_the_number(self):
        self.assertEqual(redact_pii("mera number 9876543210hai"), "mera number [phone]hai")
        self.assertEqual(redact_pii("isi 9876543210pe call karo"), "isi [phone]pe call karo")

    def test_devanagari_after_the_number(self):
        self.assertEqual(redact_pii("नंबर 9876543210पर कॉल करें"), "नंबर [phone]पर कॉल करें")

    def test_devanagari_before_the_number(self):
        self.assertEqual(redact_pii("नंबर9876543210 है"), "नंबर[phone] है")

    def test_letters_then_a_full_stop_then_devanagari_still_goes(self):
        """A full stop with no space after it, then Hindi. `\\w` would read the
        next Devanagari letter as a run-on and the number would stay."""
        self.assertEqual(redact_pii("call 9876543210pls.धन्यवाद"), "call [phone]pls.धन्यवाद")


class DevanagariDigitsTests(unittest.TestCase):
    """Digits typed on a Hindi keyboard are the same digits (digits.ascii_digits)."""

    def test_a_number_in_devanagari_digits_goes(self):
        self.assertEqual(redact_pii("मेरा नंबर ९८७६५४३२१० है"), "मेरा नंबर [phone] है")

    def test_hinglish_with_devanagari_digits_in_groups(self):
        self.assertEqual(redact_pii("mera number ९८७६५ ४३२१० hai"), "mera number [phone] hai")

    def test_devanagari_digits_glued_to_devanagari(self):
        self.assertEqual(redact_pii("मेरा नंबर ९८७६५४३२१०पर"), "मेरा नंबर [phone]पर")

    def test_a_code_in_devanagari_digits_is_still_hidden(self):
        self.assertEqual(redact_pii("०३९७६०"), "[6 digits]")


class OtherCountriesNumbersTests(unittest.TestCase):
    """A +<country code> number (spec 2026-10-05, section 8): customers in Spain
    write to the same chat."""

    def test_a_spanish_number_goes(self):
        self.assertEqual(redact_pii("llámame al +34 612 345 678"), "llámame al [phone]")
        self.assertEqual(redact_pii("+34612345678"), "[phone]")

    def test_a_foreign_number_typed_straight_after_a_devanagari_word_goes(self):
        self.assertEqual(redact_pii("नंबर+34612345678"), "नंबर[phone]")

    def test_a_plus_sign_inside_a_longer_token_is_left_alone(self):
        self.assertEqual(redact_pii("ref ab+34612345678"), "ref ab+34612345678")

    def test_other_groupings_go(self):
        self.assertEqual(redact_pii("UK office +44 20 7946 0958."), "UK office [phone].")
        self.assertEqual(redact_pii("call +1 (415) 555-0100 now"), "call [phone] now")

    def test_an_indian_number_is_still_one_phone(self):
        self.assertEqual(redact_pii("reach me on +91 98765-43210."), "reach me on [phone].")

    def test_a_plus_sign_on_a_short_figure_is_left_alone(self):
        self.assertEqual(redact_pii("range +15 km after the update"), "range +15 km after the update")


class SecretFieldsTests(unittest.TestCase):
    """Zoho's OAuth values are removed by name wherever they appear in an event
    (spec 2026-10-05, section 8). They are secret because of what they are,
    not what they look like."""

    def test_each_oauth_field_is_redacted(self):
        event = EventLog(path=None).emit(
            "zoho_token_refused", "-",
            access_token="1000.aaaa.bbbb", refresh_token="1000.cccc.dddd",
            client_secret="test-client-secret", error="invalid_client_secret",
        )
        for name in ("access_token", "refresh_token", "client_secret"):
            self.assertEqual(event[name], "[redacted]")
        self.assertEqual(event["error"], "invalid_client_secret")
        self.assertNotIn("1000.", json.dumps(event))
        self.assertNotIn("test-client-secret", json.dumps(event))

    def test_an_authorization_header_is_redacted_whatever_its_case(self):
        self.assertEqual(
            redact_fields({"headers": {"Authorization": "Zoho-oauthtoken 1000.aaaa.bbbb", "orgId": "60001234567"}}),
            {"headers": {"Authorization": "[redacted]", "orgId": "60001234567"}},
        )


if __name__ == "__main__":
    unittest.main()
