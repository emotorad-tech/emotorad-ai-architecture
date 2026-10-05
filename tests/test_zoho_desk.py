"""The Desk calls the worker makes (spec 2026-10-05, sections 4 and 5), and the
recorded shapes the fake answers from.

All run against tests/fake_zoho.py. The shapes in docs/api-shapes/zoho-*.json
are built from Zoho's published OpenAPI files and the OMS's production code
until part 1's probe replaces them. ShapeTests is the contract between them
and the client.
"""

import json
import os
import socket
import unittest
from unittest import mock

from emotorad_ai.zoho.auth import TokenSource
from emotorad_ai.zoho.desk import (
    ADOPT_SLACK_SECONDS,
    COMMENT_LIMIT,
    DESK_URL,
    NAME_LIMIT,
    UPLOAD_TIMEOUT,
    DeskClient,
    find_adoptable,
    safe_filename,
)
from emotorad_ai.zoho.errors import ZohoAuthExpired, ZohoConfigError, ZohoRejected, ZohoUnavailable, ZohoUnknownOutcome
from emotorad_ai.zoho.http import DeskHTTP
from tests.fake_zoho import (
    ORG_ID,
    SHAPES,
    TEST_CONTACT,
    TEST_DEPARTMENT,
    Answer,
    FakeZoho,
    shape,
    zoho_settings,
)

TICKET_ID = "4000000528005"
ZOHO_SHAPES = ("zoho-ticket.json", "zoho-contact-search.json", "zoho-contact-tickets.json", "zoho-comment.json",
               "zoho-attachment.json", "zoho-token.json", "zoho-errors.json")


def desk(*answers):
    fake = FakeZoho().queue(*answers)
    settings = zoho_settings()
    transport = DeskHTTP(opener=fake)
    return DeskClient(settings, TokenSource(settings, transport, clock=lambda: 1000.0), transport), fake


def zoho_error(name):
    return shape("zoho-errors.json")[name]


def listing(*items):
    return {"data": list(items)}


class HeaderTests(unittest.TestCase):
    def test_every_call_carries_the_token_and_the_org_id(self):
        client, fake = desk(Answer(204))
        client.search_contacts("phone", "9999999999")
        sent = fake.desk_requests[0]
        self.assertEqual(sent["headers"]["authorization"], "Zoho-oauthtoken tok-1")
        self.assertEqual(sent["headers"]["orgid"], ORG_ID)
        self.assertTrue(sent["url"].startswith("https://desk.zoho.in/api/v1/"))
        self.assertEqual(sent["timeout"], 8.0)

    def test_the_desk_host_is_the_india_data_centre(self):
        self.assertEqual(DESK_URL, "https://desk.zoho.in")

    def test_the_headers_are_public_for_the_scripts(self):
        client, _ = desk()
        self.assertEqual(client.headers(),
                         {"Authorization": "Zoho-oauthtoken tok-1", "orgId": ORG_ID, "Accept": "application/json"})
        self.assertEqual(client.headers("application/json")["Content-Type"], "application/json")
        self.assertNotIn("Content-Type", client.headers())

    def test_the_http_and_the_token_source_are_public(self):
        settings = zoho_settings()
        transport = DeskHTTP(opener=FakeZoho())
        tokens = TokenSource(settings, transport)
        client = DeskClient(settings, tokens, transport)
        self.assertIs(client.http, transport)
        self.assertIs(client.tokens, tokens)

    def test_repr_holds_no_token(self):
        client, _ = desk()
        client.headers()
        self.assertNotIn("tok-1", repr(client))


class SearchTests(unittest.TestCase):
    def test_contacts_are_searched_by_the_last_ten_digits_with_a_wildcard(self):
        found = shape("zoho-contact-search.json")
        for field in ("phone", "mobile"):
            with self.subTest(field=field):
                client, fake = desk(Answer(200, found))
                self.assertEqual(client.search_contacts(field, "9999999999"), found["data"])
                sent = fake.desk_requests[0]
                self.assertEqual((sent["method"], sent["path"]), ("GET", "/api/v1/contacts/search"))
                self.assertIn("%s=*9999999999" % field, sent["url"])
                self.assertEqual(sent["query"], {field: ["*9999999999"]})

    def test_204_is_no_match(self):
        client, _ = desk(Answer(204))
        self.assertEqual(client.search_contacts("mobile", "9999999999"), [])

    def test_only_phone_or_mobile_and_only_ten_digits_reach_zoho(self):
        client, fake = desk()
        for field, digits in (("email", "9999999999"), ("phone", "+919999999999"), ("phone", "999999999"),
                              ("phone", "९९९९९९९९९९"), ("mobile", "")):
            with self.subTest(field=field, digits=digits):
                with self.assertRaises(ValueError) as caught:
                    client.search_contacts(field, digits)
                if digits:
                    self.assertNotIn(digits, str(caught.exception))
        self.assertEqual(fake.requests, [])


class ContactTests(unittest.TestCase):
    def test_a_contact_is_made_with_a_last_name_and_a_mobile_and_nothing_else(self):
        made = shape("zoho-contact-search.json")["data"][0]
        client, fake = desk(Answer(200, made))
        self.assertEqual(client.create_contact("AI chat customer", "+919999999999"), made["id"])
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/contacts"))
        self.assertEqual(json.loads(sent["body"]), {"lastName": "AI chat customer", "mobile": "+919999999999"})
        self.assertEqual(sent["headers"]["content-type"], "application/json")

    def test_a_create_answer_with_no_id_is_an_unknown_outcome(self):
        client, _ = desk(Answer(200, {}))
        with self.assertRaises(ZohoUnknownOutcome):
            client.create_contact("AI chat customer", "+919999999999")


class ContactTicketsTests(unittest.TestCase):
    def test_the_contacts_tickets_newest_first_in_one_department(self):
        recorded = shape("zoho-contact-tickets.json")
        client, fake = desk(Answer(200, recorded))
        self.assertEqual(client.contact_tickets(TEST_CONTACT, TEST_DEPARTMENT), recorded["data"])
        sent = fake.desk_requests[0]
        self.assertEqual(sent["path"], "/api/v1/contacts/%s/tickets" % TEST_CONTACT)
        self.assertEqual(sent["query"], {"departmentId": [TEST_DEPARTMENT], "sortBy": ["-createdTime"], "limit": ["50"]})

    def test_no_tickets_is_204(self):
        client, _ = desk(Answer(204))
        self.assertEqual(client.contact_tickets(TEST_CONTACT, TEST_DEPARTMENT, limit=10), [])

    def test_a_list_answer_without_data_is_unavailable(self):
        client, _ = desk(Answer(200, {"tickets": []}))
        with self.assertRaises(ZohoUnavailable):
            client.contact_tickets(TEST_CONTACT, TEST_DEPARTMENT)


class TicketCreateTests(unittest.TestCase):
    def test_a_ticket_is_created_and_its_id_number_and_link_come_back(self):
        made = shape("zoho-ticket.json")
        client, fake = desk(Answer(200, made))
        payload = {"subject": made["subject"], "departmentId": TEST_DEPARTMENT}
        self.assertEqual(client.create_ticket(payload),
                         {"id": made["id"], "ticketNumber": made["ticketNumber"], "webUrl": made["webUrl"]})
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/tickets"))
        self.assertEqual(json.loads(sent["body"]), payload)

    def test_numeric_ids_come_back_as_text(self):
        client, _ = desk(Answer(200, {"id": 4000000528005, "ticketNumber": 1024}))
        self.assertEqual(client.create_ticket({}), {"id": "4000000528005", "ticketNumber": "1024", "webUrl": None})

    def test_an_answer_without_an_id_is_an_unknown_outcome(self):
        client, _ = desk(Answer(200, {"ticketNumber": "1024"}))
        with self.assertRaises(ZohoUnknownOutcome):
            client.create_ticket({"subject": "x"})

    def test_a_create_that_times_out_after_sending_is_not_sent_again(self):
        client, fake = desk(socket.timeout("timed out"))
        with self.assertRaises(ZohoUnknownOutcome):
            client.create_ticket({"subject": "x"})
        self.assertEqual(len(fake.desk_requests), 1)


class CommentTests(unittest.TestCase):
    def test_a_comment_is_private_plain_text(self):
        made = shape("zoho-comment.json")
        client, fake = desk(Answer(200, made))
        text = "[stage:EM-1000001 transcript, turns 1-2]"
        self.assertEqual(client.add_comment(TICKET_ID, text), made["id"])
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/tickets/%s/comments" % TICKET_ID))
        self.assertEqual(json.loads(sent["body"]), {"isPublic": False, "contentType": "plainText", "content": text})

    def test_a_comment_over_zohos_limit_is_never_sent(self):
        client, fake = desk()
        self.assertEqual(COMMENT_LIMIT, 32000)
        with self.assertRaises(ZohoRejected) as caught:
            client.add_comment(TICKET_ID, "x" * (COMMENT_LIMIT + 1))
        self.assertEqual(caught.exception.fields, ("content",))
        self.assertEqual(fake.requests, [])

    def test_a_comment_at_zohos_limit_is_sent(self):
        client, fake = desk(Answer(200, shape("zoho-comment.json")))
        client.add_comment(TICKET_ID, "x" * COMMENT_LIMIT)
        self.assertEqual(len(fake.desk_requests), 1)

    def test_comments_are_read_page_by_page(self):
        one = shape("zoho-comment.json")
        full = listing(*[dict(one, id=str(5000000000000 + i)) for i in range(100)])
        rest = listing(*[dict(one, id=str(6000000000000 + i)) for i in range(3)])
        client, fake = desk(Answer(200, full), Answer(200, rest))
        self.assertEqual(len(client.comments(TICKET_ID)), 103)
        pages = [request["query"] for request in fake.desk_requests]
        self.assertEqual([page["from"] for page in pages], [["0"], ["100"]])
        self.assertEqual([page["limit"] for page in pages], [["100"], ["100"]])

    def test_a_ticket_with_no_comments(self):
        client, _ = desk(Answer(204))
        self.assertEqual(client.comments(TICKET_ID), [])


class AttachmentTests(unittest.TestCase):
    DATA = b"\x89PNG\r\n\x1a\n fake photo bytes"

    def test_a_file_goes_as_the_multipart_field_file_privately_with_a_long_timeout(self):
        made = shape("zoho-attachment.json")
        client, fake = desk(Answer(200, made))
        self.assertEqual(client.upload_attachment(TICKET_ID, "EM-1000001-photo-1.png", self.DATA, "image/png"),
                         made["id"])
        sent = fake.desk_requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/tickets/%s/attachments" % TICKET_ID))
        self.assertEqual(sent["query"], {"isPublic": ["false"]})
        self.assertEqual(UPLOAD_TIMEOUT, 60.0)
        self.assertEqual(sent["timeout"], UPLOAD_TIMEOUT)
        content_type = sent["headers"]["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        self.assertEqual(sent["body"], b"".join([
            b"--", boundary, b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="EM-1000001-photo-1.png"\r\n',
            b"Content-Type: image/png\r\n\r\n",
            self.DATA, b"\r\n--", boundary, b"--\r\n",
        ]))

    def test_each_upload_has_its_own_boundary(self):
        client, fake = desk(Answer(200, {"id": "1"}), Answer(200, {"id": "2"}))
        client.upload_attachment(TICKET_ID, "a.png", self.DATA, "image/png")
        client.upload_attachment(TICKET_ID, "b.png", self.DATA, "image/png")
        first, second = (request["headers"]["content-type"] for request in fake.desk_requests)
        self.assertNotEqual(first, second)

    def test_a_file_name_is_made_safe_for_the_header_and_for_zohos_length(self):
        self.assertEqual(safe_filename('EM-1000001 "photo"\r\n.jpg'), "EM-1000001_photo_.jpg")
        self.assertEqual(safe_filename("फोटो.jpg"), "_.jpg")
        traversal = safe_filename("../../etc/passwd")
        self.assertNotIn("/", traversal)
        self.assertFalse(traversal.startswith("."))
        long_name = "EM-1000001-" + "x" * 200 + ".jpeg"
        self.assertEqual(NAME_LIMIT, 100)
        self.assertEqual(len(safe_filename(long_name)), NAME_LIMIT)
        self.assertTrue(safe_filename(long_name).endswith(".jpeg"))
        self.assertEqual(safe_filename(""), "attachment")

    def test_the_uploaded_name_is_the_safe_one(self):
        client, fake = desk(Answer(200, {"id": "1"}))
        client.upload_attachment(TICKET_ID, 'a"b.png', self.DATA, "image/png")
        self.assertIn(b'filename="a_b.png"', fake.desk_requests[0]["body"])

    def test_an_odd_mime_type_goes_as_octet_stream(self):
        client, fake = desk(Answer(200, {"id": "1"}))
        client.upload_attachment(TICKET_ID, "a.bin", self.DATA, "image/png\r\nX-Evil: 1")
        self.assertIn(b"Content-Type: application/octet-stream\r\n", fake.desk_requests[0]["body"])
        self.assertNotIn(b"X-Evil", fake.desk_requests[0]["body"])

    def test_a_boundary_found_in_the_file_is_not_used(self):
        client, fake = desk(Answer(200, {"id": "1"}))
        data = b"before EMotoradAI" + b"a" * 32 + b" after"
        with mock.patch("emotorad_ai.zoho.desk.secrets.token_hex", side_effect=["a" * 32, "b" * 32]):
            client.upload_attachment(TICKET_ID, "a.png", data, "image/png")
        content_type = fake.desk_requests[0]["headers"]["content-type"]
        self.assertEqual(content_type, "multipart/form-data; boundary=EMotoradAI" + "b" * 32)
        self.assertIn(data, fake.desk_requests[0]["body"])

    def test_attachments_are_listed(self):
        one = shape("zoho-attachment.json")
        client, fake = desk(Answer(200, listing(one)))
        self.assertEqual(client.attachments(TICKET_ID), [one])
        self.assertEqual(fake.desk_requests[0]["path"], "/api/v1/tickets/%s/attachments" % TICKET_ID)


class ExpiredTokenTests(unittest.TestCase):
    def test_an_expired_token_is_refreshed_once_and_the_step_sent_once_more(self):
        made = shape("zoho-ticket.json")
        client, fake = desk(Answer(401, zoho_error("invalid_oauth")), Answer(200, made))
        self.assertEqual(client.create_ticket({"subject": "x"})["id"], made["id"])
        self.assertEqual(len(fake.token_requests), 2)
        self.assertEqual([request["headers"]["authorization"] for request in fake.desk_requests],
                         ["Zoho-oauthtoken tok-1", "Zoho-oauthtoken tok-2"])

    def test_a_second_expiry_is_raised_not_retried_again(self):
        client, fake = desk(Answer(401, zoho_error("invalid_oauth")), Answer(401, zoho_error("invalid_oauth")))
        with self.assertRaises(ZohoAuthExpired):
            client.add_comment(TICKET_ID, "note")
        self.assertEqual(len(fake.desk_requests), 2)
        self.assertEqual(len(fake.token_requests), 2)

    def test_a_read_is_retried_the_same_way(self):
        client, fake = desk(Answer(401, zoho_error("invalid_oauth")), Answer(204))
        self.assertEqual(client.search_contacts("phone", "9999999999"), [])
        self.assertEqual(len(fake.desk_requests), 2)


class IdTests(unittest.TestCase):
    def test_an_id_that_is_not_a_zoho_id_never_reaches_a_path(self):
        client, fake = desk()
        for bad in ("../contacts", TICKET_ID + "/comments", "", None, "12a"):
            with self.subTest(bad=bad):
                with self.assertRaises(ZohoConfigError) as caught:
                    client.add_comment(bad, "note")
                self.assertEqual(caught.exception.error, "bad_id")
        with self.assertRaises(ZohoConfigError):
            client.contact_tickets(TEST_CONTACT, "dept")
        with self.assertRaises(ZohoConfigError):
            client.comments("12a")
        with self.assertRaises(ZohoConfigError):
            client.attachments("12a")
        with self.assertRaises(ZohoConfigError):
            client.upload_attachment("12a", "a.png", b"x", "image/png")
        self.assertEqual(fake.requests, [])


class AdoptTests(unittest.TestCase):
    WANTED = "stage:EM-1000001"

    def ticket(self, subject, **more):
        return dict({"id": "1", "subject": subject}, **more)

    def test_only_a_subject_ending_in_the_exact_chat_reference_is_adopted(self):
        near = [
            self.ticket("[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]x"),
            self.ticket("[AI chat] Battery: charging - EMX Plus [STAGE:EM-1000001]"),
            self.ticket("[AI chat] Battery: charging - EMX Plus [stage:EM-10000011]"),
            self.ticket("[AI chat] Battery: charging - EMX Plus [stage:EM-100000]"),
            self.ticket("[AI chat] Battery: charging - EMX Plus [prod:EM-1000001]"),
            self.ticket("[AI chat] Battery: charging - EMX Plus [prod:stage:EM-1000001]"),
            self.ticket("[AI chat] Battery: charging - EMX Plus stage:EM-1000001"),
            self.ticket("[AI chat] Battery: charging - EMX Plus [stage:EM-1000001] and more"),
            self.ticket("[stage:EM-1000001]"),
            self.ticket("[AI chat] Battery: charging - EMX Plus (stage:EM-1000001)"),
            {"id": "2"}, {"id": "3", "subject": None}, {"id": "4", "subject": 7}, "not a ticket", None,
        ]
        self.assertIsNone(find_adoptable(near, self.WANTED))
        match = self.ticket("[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]", id="5")
        self.assertIs(find_adoptable(near + [match], self.WANTED), match)

    def test_trailing_white_space_in_the_subject_is_ignored(self):
        match = self.ticket("[AI chat] Battery: charging - EMX Plus [stage:EM-1000001] \r\n")
        self.assertIs(find_adoptable([match], self.WANTED), match)

    def test_an_empty_reference_adopts_nothing(self):
        self.assertIsNone(find_adoptable([self.ticket("[AI chat] x []"), self.ticket("[AI chat] x [ ]")], ""))
        self.assertIsNone(find_adoptable([self.ticket("[AI chat] x []")], None))

    def test_the_recorded_list_can_be_adopted_from(self):
        # Read from the shape, so a capture (other ids, EM-TEST references)
        # is held to the same rule as the draft: each subject's reference
        # adopts the newest ticket carrying it, and a near miss adopts none.
        recorded = shape("zoho-contact-tickets.json")["data"]
        for item in recorded:
            reference = item["subject"].rstrip().rsplit("[", 1)[1].rstrip("]")
            with self.subTest(reference=reference):
                newest = next(t for t in recorded if t["subject"].rstrip().endswith("[%s]" % reference))
                self.assertIs(find_adoptable(recorded, reference), newest)
                self.assertIsNone(find_adoptable(recorded, reference + "0"))

    def test_the_newest_match_is_the_first_in_the_list(self):
        # The list is newest first (contact_tickets), so a repeat is never preferred to the latest.
        newer = self.ticket("[AI chat] a [stage:EM-1000001]", id="9")
        older = self.ticket("[AI chat] a [stage:EM-1000001]", id="8")
        self.assertIs(find_adoptable([newer, older], self.WANTED), newer)


class AdoptAgeTests(unittest.TestCase):
    # A reference can come round again (a database started afresh), and an old
    # ticket with the same chat reference must not be taken for this record's.
    WANTED = "stage:EM-1000001"
    CREATED = "2026-10-05T10:00:00.000000+00:00"

    def ticket(self, created):
        made = {"id": "1", "subject": "[AI chat] a [stage:EM-1000001]"}
        if created is not None:
            made["createdTime"] = created
        return made

    def test_a_ticket_made_before_the_record_is_not_adopted(self):
        old = self.ticket("2026-10-03T08:30:00.000Z")
        self.assertIsNone(find_adoptable([old], self.WANTED, not_before=self.CREATED))

    def test_a_ticket_made_after_the_record_is_adopted(self):
        later = self.ticket("2026-10-05T10:02:00.000Z")
        self.assertIs(find_adoptable([later], self.WANTED, not_before=self.CREATED), later)

    def test_a_little_clock_difference_does_not_hide_the_ticket(self):
        self.assertEqual(ADOPT_SLACK_SECONDS, 300)
        just_inside = self.ticket("2026-10-05T09:55:01.000Z")
        self.assertIs(find_adoptable([just_inside], self.WANTED, not_before=self.CREATED), just_inside)
        just_outside = self.ticket("2026-10-05T09:54:59.000Z")
        self.assertIsNone(find_adoptable([just_outside], self.WANTED, not_before=self.CREATED))

    def test_a_ticket_with_no_readable_time_is_judged_by_its_subject_alone(self):
        for created in (None, "", "yesterday", 5):
            with self.subTest(created=created):
                made = self.ticket(created)
                self.assertIs(find_adoptable([made], self.WANTED, not_before=self.CREATED), made)

    def test_the_record_time_is_given_by_keyword_only(self):
        with self.assertRaises(TypeError):
            find_adoptable([self.ticket(None)], self.WANTED, self.CREATED)

    def test_with_no_record_time_nothing_is_skipped_for_its_age(self):
        old = self.ticket("2020-01-01T00:00:00.000Z")
        self.assertIs(find_adoptable([old], self.WANTED), old)

    def test_a_record_time_that_cannot_be_read_skips_nothing(self):
        old = self.ticket("2020-01-01T00:00:00.000Z")
        self.assertIs(find_adoptable([old], self.WANTED, not_before="not a time"), old)

    def test_an_old_ticket_does_not_hide_a_new_one_behind_it(self):
        old = dict(self.ticket("2026-10-03T08:30:00.000Z"), id="1")
        new = dict(self.ticket("2026-10-05T10:02:00.000Z"), id="2")
        self.assertIs(find_adoptable([old, new], self.WANTED, not_before=self.CREATED), new)


class ShapeChecks:
    """What the client needs from a folder of recorded shapes: the drafts in
    docs/api-shapes (ShapeTests), or what part 1's scripts write over them
    (tests/test_zoho_scripts.py CapturedShapeTests). Not a TestCase on its
    own, so it runs once per folder."""

    shapes_dir = SHAPES

    def raw(self, name):
        with open(os.path.join(self.shapes_dir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def recorded(self, name):
        return shape(name, self.shapes_dir)

    def captured(self, name):
        return str(self.raw(name).get("_source", "")).startswith("Captured by scripts/zoho/")

    def test_every_shape_says_where_it_came_from_and_what_replaces_it(self):
        for name in ZOHO_SHAPES:
            with self.subTest(name=name):
                source = self.raw(name)["_source"]
                if self.captured(name):
                    self.assertIn("masked by scripts/zoho/_common.py", source)
                else:
                    # A draft says what it was built from and which script's capture replaces it.
                    self.assertIn("scripts/zoho/", source)
                    self.assertTrue("zohodesk-oas" in source or "em-biz-backend" in source)
                self.assertNotIn("_source", self.recorded(name))

    def test_no_shape_has_a_custom_field(self):
        # The Desk's text custom fields are at their limit, so none is sent or
        # read. A capture keeps the "cf" object Zoho answers with, its values
        # masked, as it keeps every key; the client reads none of it.
        for name in ZOHO_SHAPES:
            with self.subTest(name=name):
                with open(os.path.join(self.shapes_dir, name), encoding="utf-8") as handle:
                    text = handle.read()
                banned = ("cf_chat_reference", "cf_source") + (() if self.captured(name) else ('"cf"',))
                for word in banned:
                    self.assertNotIn(word, text)

    def test_the_client_reads_only_keys_the_shapes_carry(self):
        ticket = self.recorded("zoho-ticket.json")
        for key in ("id", "ticketNumber", "webUrl", "subject", "createdTime", "departmentId", "contactId"):
            self.assertIn(key, ticket)
        for name in ("zoho-contact-search.json", "zoho-contact-tickets.json"):
            self.assertIsInstance(self.recorded(name)["data"], list)
            self.assertIn("id", self.recorded(name)["data"][0])
        for item in self.recorded("zoho-contact-tickets.json")["data"]:
            self.assertIn("subject", item)
            self.assertIn("createdTime", item)
        self.assertIn("id", self.recorded("zoho-comment.json"))
        self.assertIn("id", self.recorded("zoho-attachment.json"))
        token = self.recorded("zoho-token.json")
        self.assertIn("access_token", token["refresh"])
        self.assertIn("expires_in", token["refresh"])
        self.assertEqual(token["access_denied"]["error"], "Access Denied")
        for name in ("invalid_code", "invalid_client", "invalid_client_secret"):
            self.assertEqual(token[name]["error"], name)

    def test_the_chat_reference_ends_every_recorded_subject_in_square_brackets(self):
        # test_ticket.py's tickets carry EM-TEST-<n>, which no real record can have.
        subjects = [self.recorded("zoho-ticket.json")["subject"]]
        subjects += [item["subject"] for item in self.recorded("zoho-contact-tickets.json")["data"]]
        for subject in subjects:
            with self.subTest(subject=subject):
                self.assertRegex(subject, r"^(\[Unverified\] )?\[AI chat\] .+ \[(stage|prod):EM-([0-9]{7}|TEST-[0-9]+)\]$")

    def test_every_error_code_the_client_classifies_is_recorded(self):
        errors = self.recorded("zoho-errors.json")
        codes = {body["errorCode"] for body in errors.values()}
        for code in ("INVALID_OAUTH", "SCOPE_MISMATCH", "OAUTH_ORG_MISMATCH", "FORBIDDEN", "LICENSE_ACCESS_LIMITED",
                     "INVALID_DATA", "URL_NOT_FOUND", "RESOURCE_SIZE_EXCEEDED", "TOO_MANY_REQUESTS",
                     "THRESHOLD_EXCEEDED"):
            self.assertIn(code, codes)
        self.assertEqual([item["fieldName"] for item in errors["invalid_data"]["errors"]],
                         ["/contactId", "/departmentId"])


class ShapeTests(ShapeChecks, unittest.TestCase):
    """The drafts committed in docs/api-shapes."""


if __name__ == "__main__":
    unittest.main()
