"""What goes on a Zoho Desk ticket (spec 2026-10-05, section 5).

Code chooses every field Zoho routes on. Model text reaches only the
description, under a heading that says who wrote it. The Desk has no room for
custom fields, so the payload carries no `cf` at all: the chat reference ends
the subject instead, and the adoption look-up reads it from there. Fake data
only: the test number and a fixture frame number.
"""

import re
import unittest

from emotorad_ai.conversation import TranscriptTurn, render_transcript
from emotorad_ai.tickets.kinds import subject_label
from emotorad_ai.tickets.record import new_record
from emotorad_ai.zoho.desk import find_adoptable
from emotorad_ai.zoho.errors import ZohoConfigError
from emotorad_ai.zoho.payload import (
    DESCRIPTION_LIMIT,
    SUBJECT_LIMIT,
    TAKEOVER_LINE,
    TRANSCRIPT_LIMIT,
    UTC_LINE,
    description,
    subject,
    ticket_payload,
    transcript_chunks,
)
from tests.fake_zoho import LIVE_DEPARTMENT, TEST_CONTACT, TEST_DEPARTMENT, zoho_settings

PHONE = "+919999999999"
FRAME = "EMXP2025004417"
MODEL = "EMX Plus"
LAYOUT = "4000000000099"
CHAT_REFERENCE = "stage:EM-1000001"
SUFFIX = " [%s]" % CHAT_REFERENCE
ALLOWED = {"subject", "departmentId", "contactId", "phone", "priority", "status", "channel", "description"}
FORBIDDEN = ("cf", "assigneeId", "teamId", "dueDate", "customFields", "productId", "email", "contact",
             "classification", "category", "subCategory", "uploads", "language")
MARKER = re.compile(r"\[stage:EM-1000001 transcript, turns (\d+)-(\d+)(?:, part (\d+))?\]")
UTC = UTC_LINE % "EM-1000001"


def record(**changes):
    fields = dict(
        reference="EM-1000001", chat_reference=CHAT_REFERENCE,
        source_key="c-1:2026-10-05T08:00:00.000000+00:00:create_support_ticket:k-1", mode="test", kind="support",
        conversation_id="c-1", started_at="2026-10-05T08:00:00.000000+00:00", cluster_id="cl-1", channel="whatsapp",
        phone=PHONE, identity="verified", category="battery_charging", ai_severity="high",
        summary="Battery stops charging at 40 percent. Tried another socket, same result.", claims={},
        bike={"model": MODEL, "frame_number": FRAME, "frame_number_source": None}, coverage="computed",
        customer_name="Test Rider", created_at="2026-10-05T08:05:00.000000+00:00",
    )
    fields.update(changes)
    return new_record(**fields)


def turns(count, start=1, text="My battery stops charging at 40 percent"):
    return [TranscriptTurn(n=n, role="customer" if n % 2 else "bot", text="%s, turn %d" % (text, n),
                           at="2026-10-05T08:%02d:00+00:00" % (n % 60)) for n in range(start, start + count)]


def body_of(text, opening):
    lines = text.split("\n")
    return "\n".join(lines[2:] if opening else lines[1:])


class SubjectTests(unittest.TestCase):
    def test_a_verified_ticket_reads_ai_chat_label_bike_and_the_chat_reference(self):
        self.assertEqual(subject(record()),
                         "[AI chat] %s - EMX Plus%s" % (subject_label("support", "battery_charging"), SUFFIX))
        self.assertEqual(subject(record()), "[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]")

    def test_unverified_comes_first(self):
        self.assertEqual(subject(record(identity="unverified", kind="intake", category=None, bike=None)),
                         "[Unverified] [AI chat] %s - bike not given%s" % (subject_label("intake", None), SUFFIX))

    def test_the_label_is_set_by_kind_in_code(self):
        self.assertEqual(subject(record(kind="safety", category="battery_safety")),
                         "[AI chat] SAFETY - EMX Plus" + SUFFIX)
        for kind, label in (("handover", "Asked for a person"), ("lockout", "Could not verify"),
                            ("intake", "Unverified customer"), ("warranty_proof", "Late warranty registration")):
            with self.subTest(kind=kind):
                self.assertIn(label, subject(record(kind=kind, category=None)))

    def test_no_phone_and_no_name(self):
        text = subject(record(identity="unverified"))
        self.assertNotIn("9999999999", text)
        self.assertNotIn("Test Rider", text)

    def test_at_most_255_characters_on_one_line_and_the_reference_is_never_cut(self):
        text = subject(record(bike={"model": "EMX\nPlus " + "x" * 400, "frame_number": FRAME,
                                    "frame_number_source": None}))
        self.assertEqual(SUBJECT_LIMIT, 255)
        self.assertEqual(len(text), 255)
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("[AI chat] "))
        self.assertTrue(text.endswith(SUFFIX))

    def test_a_four_hundred_character_label_is_cut_and_the_suffix_and_prefix_stay_whole(self):
        for identity in ("verified", "unverified"):
            with self.subTest(identity=identity):
                text = subject(record(category="a" * 400, identity=identity))
                self.assertEqual(len(text), 255)
                self.assertTrue(text.endswith(SUFFIX))
                self.assertEqual(text.count(SUFFIX), 1)
                prefix = "[Unverified] [AI chat] " if identity == "unverified" else "[AI chat] "
                self.assertTrue(text.startswith(prefix + "Aaaa"))
                # The label is cut from the end, so none of the bike part is left.
                self.assertNotIn("EMX Plus", text)

    def test_a_cut_just_after_a_space_does_not_leave_two_before_the_suffix(self):
        room = SUBJECT_LIMIT - len("[AI chat] ") - len(SUFFIX)
        lead = len("Battery: charging - ")
        # The last kept character of the label and bike part is the space.
        model = "a" * (room - 1 - lead) + " " + "b" * 100
        text = subject(record(bike={"model": model, "frame_number": FRAME, "frame_number_source": None}))
        self.assertTrue(text.endswith("a" + SUFFIX))
        self.assertNotIn("  ", text)
        self.assertEqual(len(text), 255 - 1)

    def test_the_adoption_look_up_finds_a_ticket_by_the_subject_it_was_made_with(self):
        for changes in ({}, {"category": "a" * 400}, {"identity": "unverified", "bike": None}):
            with self.subTest(changes=sorted(changes)):
                made = subject(record(**changes))
                other = {"id": "9", "subject": "[AI chat] Battery: charging - EMX Plus [stage:EM-1000002]"}
                found = find_adoptable([other, {"id": "1", "subject": made}], CHAT_REFERENCE)
                self.assertEqual(found["id"], "1")

    def test_a_reference_too_long_for_any_subject_is_refused_not_cut(self):
        with self.assertRaises(ZohoConfigError) as raised:
            subject(record(chat_reference="stage:" + "E" * 400))
        self.assertNotIn("EEEE", str(raised.exception))


class DescriptionTests(unittest.TestCase):
    def test_the_lines_come_in_the_spec_order(self):
        text = description(record(kind="warranty_proof", category=None,
                                  claims={"claimed_purchase_date": "2025-01-10", "purchase_channel": "dealer"}))
        order = ["Reference: EM-1000001", "Source: AI chatbot", "Channel: WhatsApp", "Identity: verified",
                 "Kind: warranty_proof", "Bike: EMX Plus, frame number EMXP2025004417",
                 "Warranty, from our systems, not the AI:", "AI's view of severity: high",
                 "Customer's claims, not checked:",
                 "Summary written by the AI from the customer's words, not checked:"]
        positions = [text.index(line) for line in order]
        self.assertEqual(positions, sorted(positions))

    def test_the_source_stays_in_the_description_now_the_custom_field_is_gone(self):
        self.assertEqual(description(record()).split("\n")[1], "Source: AI chatbot")

    def test_an_unverified_number_says_so(self):
        self.assertIn("Identity: number given in chat, not verified", description(record(identity="unverified")))
        self.assertNotIn("not verified", description(record()))

    def test_the_bike_line_carries_model_frame_and_source_and_is_left_out_when_unknown(self):
        text = description(record(bike={"model": MODEL, "frame_number": FRAME,
                                        "frame_number_source": "read by the rider"}))
        self.assertIn("Bike: EMX Plus, frame number EMXP2025004417 (read by the rider)", text)
        self.assertNotIn("Bike:", description(record(bike=None)))

    def test_the_coverage_outcome_comes_from_code(self):
        self.assertIn("(computed)", description(record()))
        self.assertIn("no warranty record for this number (no_warranty_record)",
                      description(record(coverage="no_warranty_record")))
        self.assertIn("no result recorded in this chat", description(record(coverage=None)))

    def test_every_coverage_outcome_the_code_can_set_reads_in_words(self):
        for code in ("computed", "computed_from_registration", "purchase_date_missing", "not_registered",
                     "warranty_unknown", "warranty_unavailable", "no_warranty_record", "oms_unavailable"):
            with self.subTest(code=code):
                line = [l for l in description(record(coverage=code)).split("\n") if l.startswith("Warranty, from")][0]
                self.assertTrue(line.endswith("(%s)" % code))
                self.assertGreater(len(line), len("Warranty, from our systems, not the AI: (%s)" % code) + 10)

    def test_ai_labels_only_for_model_raised_kinds(self):
        for kind in ("support", "intake", "warranty_proof"):
            with self.subTest(kind=kind):
                text = description(record(kind=kind))
                self.assertIn("AI's view of severity: high", text)
                self.assertIn("Summary written by the AI from the customer's words, not checked", text)
        for kind in ("safety", "handover", "lockout"):
            with self.subTest(kind=kind):
                text = description(record(kind=kind, ai_severity="critical"))
                self.assertNotIn("AI's view", text)
                self.assertNotIn("written by the AI", text)
                self.assertIn("by code", text)

    def test_a_safety_ticket_carries_what_the_safety_check_found(self):
        detail = ("Automatic safety escalation. Customer reported: battery is swelling and smoking. "
                  "Matched safety indicators: swelling, smoke. Seen in the customer's photo or video: "
                  "the pack is bulging.")
        text = description(record(kind="safety", category="battery_safety", summary=detail))
        self.assertIn("Safety report", text)
        self.assertIn(detail, text)

    def test_a_handover_with_no_summary_says_the_customer_asked_for_a_person(self):
        self.assertIn("Customer asked for a person.", description(record(kind="handover", category=None, summary="")))

    def test_a_lockout_warns_of_a_takeover_and_nothing_else_does(self):
        self.assertEqual(TAKEOVER_LINE, "Possible takeover attempt: verify only through the number on record, "
                                        "never a number from this chat.")
        locked = description(record(kind="lockout", identity="unverified", bike=None, category=None, summary=""))
        self.assertTrue(locked.endswith(TAKEOVER_LINE))
        for kind in ("support", "safety", "handover", "intake", "warranty_proof"):
            with self.subTest(kind=kind):
                self.assertNotIn("takeover", description(record(kind=kind)))

    def test_claims_are_labelled_as_the_customers(self):
        text = description(record(kind="intake", identity="unverified", category=None, bike=None, claims={
            "stated_name": "Test Rider", "stated_contact": "rider at example dot com", "evidence": "invoice INV-1",
            "claimed_purchase_date": "2025-01-10", "purchase_channel": "website"}))
        self.assertIn("Customer's claims, not checked:", text)
        for line in ("- Name they gave: Test Rider", "- Contact they gave: rider at example dot com",
                     "- Evidence they offered: invoice INV-1", "- Purchase date they gave: 2025-01-10",
                     "- Where they say they bought it: website"):
            self.assertIn(line, text)
        self.assertNotIn("Customer's claims", description(record()))

    def test_a_number_or_email_typed_into_a_claim_never_reaches_the_description(self):
        text = description(record(kind="intake", identity="unverified", category=None, bike=None, claims={
            "stated_name": "Test Rider 9876543210",
            "stated_contact": "ring +91 98765 43210 or mail rider@example.com",
            "evidence": "invoice, my other number is 9876 543 210",
            "claimed_purchase_date": "bought in January, call 09876543210",
            "purchase_channel": "dealer, 9876543210"}))
        for raw in ("9876543210", "98765 43210", "9876 543 210", "09876543210", "rider@example.com"):
            self.assertNotIn(raw, text)
        self.assertIn("- Contact they gave: ring [phone] or mail [email]", text)
        self.assertIn("- Name they gave: Test Rider [phone]", text)
        self.assertIn("[phone]", text.split("- Where they say they bought it:")[1])

    def test_a_claim_is_redacted_before_it_is_cut_so_no_half_a_number_is_left(self):
        # Cut first, the 500 characters would end in "9876543", seven digits
        # that no pattern calls a number. Redacted first, the placeholder fits.
        text = description(record(kind="intake", identity="unverified", category=None, bike=None,
                                  claims={"evidence": "a" * 492 + " 9876543210"}))
        self.assertNotIn("98765", text)
        self.assertIn("a [phone]\n", text)

    def test_a_long_claim_is_cut_to_five_hundred_characters(self):
        text = description(record(kind="intake", identity="unverified", category=None, bike=None,
                                  claims={"evidence": "e" * 2000}))
        line = [l for l in text.split("\n") if l.startswith("- Evidence they offered:")][0]
        self.assertEqual(len(line), len("- Evidence they offered: ") + 500)

    def test_a_newline_in_a_claim_does_not_break_its_line(self):
        text = description(record(kind="intake", identity="unverified", category=None, bike=None,
                                  claims={"stated_name": "Test\nRider\n- Evidence they offered: forged"}))
        self.assertEqual([l for l in text.split("\n") if l.startswith("- Evidence they offered:")], [])

    def test_a_long_summary_is_cut_to_fit_and_the_takeover_line_kept(self):
        text = description(record(summary="x" * 70000))
        self.assertLessEqual(len(text), DESCRIPTION_LIMIT)
        self.assertIn("[cut to fit Zoho's limit]", text)
        locked = description(record(kind="lockout", summary="y" * 70000))
        self.assertLessEqual(len(locked), DESCRIPTION_LIMIT)
        self.assertTrue(locked.endswith(TAKEOVER_LINE))

    def test_the_customer_name_and_phone_stay_off_the_description(self):
        for kind in ("support", "safety", "handover", "lockout", "intake", "warranty_proof"):
            with self.subTest(kind=kind):
                text = description(record(kind=kind))
                self.assertNotIn("Test Rider", text)
                self.assertNotIn("9999999999", text)


class TicketPayloadTests(unittest.TestCase):
    def test_exactly_the_fields_in_the_spec(self):
        payload = ticket_payload(record(), zoho_settings(), TEST_CONTACT)
        self.assertEqual(set(payload), ALLOWED)
        self.assertEqual(payload["subject"], subject(record()))
        self.assertEqual(payload["description"], description(record()))
        self.assertEqual(payload["departmentId"], TEST_DEPARTMENT)
        self.assertEqual(payload["contactId"], TEST_CONTACT)
        self.assertEqual(payload["phone"], PHONE)
        self.assertEqual(payload["status"], "Open")
        self.assertEqual(payload["channel"], "Chat")
        self.assertEqual(payload["priority"], "Medium")

    def test_never_a_cf_key_an_assignee_team_due_date_old_custom_fields_or_a_dealer_field(self):
        for kind in ("support", "safety", "handover", "lockout", "intake", "warranty_proof"):
            for settings in (zoho_settings(), zoho_settings(live=True),
                             zoho_settings(live=True, EMOTORAD_ZOHO_LAYOUT_ID=LAYOUT)):
                with self.subTest(kind=kind, live=settings.live, layout=settings.layout_id):
                    payload = ticket_payload(record(kind=kind), settings, TEST_CONTACT)
                    for key in FORBIDDEN:
                        self.assertNotIn(key, payload)
                    self.assertEqual([key for key in payload if "dealer" in key.lower()], [])
                    self.assertNotIn("Dealer Principle", payload["description"])

    def test_the_layout_is_sent_only_when_one_is_set(self):
        self.assertNotIn("layoutId", ticket_payload(record(), zoho_settings(), TEST_CONTACT))
        payload = ticket_payload(record(), zoho_settings(EMOTORAD_ZOHO_LAYOUT_ID=LAYOUT), TEST_CONTACT)
        self.assertEqual(payload["layoutId"], LAYOUT)
        self.assertEqual(set(payload), ALLOWED | {"layoutId"})
        # A blank setting is no setting.
        self.assertNotIn("layoutId", ticket_payload(record(), zoho_settings(EMOTORAD_ZOHO_LAYOUT_ID="  "),
                                                    TEST_CONTACT))

    def test_the_department_follows_the_records_mode(self):
        live = zoho_settings(live=True)
        self.assertEqual(ticket_payload(record(mode="test"), live, TEST_CONTACT)["departmentId"], TEST_DEPARTMENT)
        self.assertEqual(ticket_payload(record(mode="live"), live, TEST_CONTACT)["departmentId"], LIVE_DEPARTMENT)

    def test_a_live_record_under_test_settings_is_refused(self):
        with self.assertRaises(ZohoConfigError):
            ticket_payload(record(mode="live"), zoho_settings(), TEST_CONTACT)
        # Even with the real department named, test settings never send to it.
        with self.assertRaises(ZohoConfigError):
            ticket_payload(record(mode="live"), zoho_settings(EMOTORAD_ZOHO_DEPARTMENT_ID=LIVE_DEPARTMENT),
                           TEST_CONTACT)

    def test_priority_is_high_only_when_urgent_and_its_values_come_from_the_settings(self):
        settings = zoho_settings(EMOTORAD_ZOHO_PRIORITY_HIGH="P1", EMOTORAD_ZOHO_PRIORITY_MEDIUM="P3")
        self.assertEqual(ticket_payload(record(kind="safety", category="battery_safety"), settings,
                                        TEST_CONTACT)["priority"], "P1")
        self.assertEqual(ticket_payload(record(kind="safety", category=None), settings, TEST_CONTACT)["priority"], "P1")
        self.assertEqual(ticket_payload(record(kind="support", category="battery_safety"), settings,
                                        TEST_CONTACT)["priority"], "P1")
        self.assertEqual(ticket_payload(record(), settings, TEST_CONTACT)["priority"], "P3")
        self.assertEqual(ticket_payload(record(kind="handover", category=None), zoho_settings(),
                                        TEST_CONTACT)["priority"], "Medium")

    def test_model_text_chooses_nothing_zoho_routes_on(self):
        sneaky = 'departmentId 1 priority "High" status Closed contactId 2 assigneeId 3'
        payload = ticket_payload(record(summary=sneaky, ai_severity="critical"), zoho_settings(), TEST_CONTACT)
        self.assertEqual((payload["departmentId"], payload["priority"], payload["status"], payload["contactId"]),
                         (TEST_DEPARTMENT, "Medium", "Open", TEST_CONTACT))
        self.assertNotIn("assigneeId", payload)

    def test_the_channel_is_the_system_channel_from_the_settings(self):
        payload = ticket_payload(record(channel="website_chat"), zoho_settings(EMOTORAD_ZOHO_CHANNEL="Web"),
                                 TEST_CONTACT)
        self.assertEqual(payload["channel"], "Web")
        self.assertIn("Channel: website chat", payload["description"])

    def test_the_chat_reference_ends_the_subject_and_is_in_no_other_field(self):
        payload = ticket_payload(record(), zoho_settings(), TEST_CONTACT)
        self.assertTrue(payload["subject"].endswith(" [stage:EM-1000001]"))
        self.assertEqual([key for key, value in payload.items() if CHAT_REFERENCE in str(value)], ["subject"])

    def test_the_phone_goes_in_plus_91_form_and_only_an_indian_mobile(self):
        self.assertEqual(ticket_payload(record(phone=PHONE), zoho_settings(), TEST_CONTACT)["phone"], PHONE)
        for typed in ("9999999999", "09999999999", "919999999999", "+91 99999 99999"):
            with self.subTest(typed=typed):
                self.assertEqual(ticket_payload(record(phone=typed), zoho_settings(), TEST_CONTACT)["phone"], PHONE)
        for phone in (None, "", "+34600000000", "12345", "+915999999999"):
            with self.subTest(phone=phone):
                self.assertNotIn("phone", ticket_payload(record(phone=phone), zoho_settings(), TEST_CONTACT))


class TranscriptTests(unittest.TestCase):
    def test_no_turns_no_comments(self):
        self.assertEqual(transcript_chunks("EM-1000001", CHAT_REFERENCE, []), [])

    def test_a_short_run_is_one_comment_with_its_marker_and_the_utc_line(self):
        run = turns(4)
        [(text, numbers)] = transcript_chunks("EM-1000001", CHAT_REFERENCE, run)
        lines = text.split("\n")
        self.assertEqual(lines[0], "[stage:EM-1000001 transcript, turns 1-4]")
        self.assertEqual(lines[1], UTC)
        self.assertEqual(UTC, "Chat with the AI chatbot for EM-1000001. Times are in UTC.")
        self.assertEqual(body_of(text, True), render_transcript(run))
        self.assertEqual(numbers, [1, 2, 3, 4])

    def test_a_long_run_splits_on_turn_boundaries_under_the_limit(self):
        run = turns(40)
        chunks = transcript_chunks("EM-1000001", CHAT_REFERENCE, run, limit=400)
        self.assertGreater(len(chunks), 3)
        seen = []
        for index, (text, numbers) in enumerate(chunks):
            self.assertLessEqual(len(text), 400)
            first, last, part = MARKER.fullmatch(text.split("\n")[0]).groups()
            self.assertIsNone(part)
            self.assertEqual((int(first), int(last)), (numbers[0], numbers[-1]))
            self.assertEqual(numbers, list(range(numbers[0], numbers[-1] + 1)))
            self.assertEqual(body_of(text, index == 0), render_transcript([t for t in run if t.n in numbers]))
            self.assertEqual(UTC in text, index == 0)
            seen += numbers
        self.assertEqual(seen, list(range(1, 41)))

    def test_the_default_limit_is_thirty_thousand(self):
        self.assertEqual(TRANSCRIPT_LIMIT, 30000)
        chunks = transcript_chunks("EM-1000001", CHAT_REFERENCE, turns(200, text="x" * 400))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(text) <= 30000 for text, _ in chunks))
        self.assertEqual([n for _, numbers in chunks for n in numbers], list(range(1, 201)))

    def test_a_turn_too_long_for_one_comment_goes_in_parts_and_only_the_last_part_counts_it(self):
        long_turn = TranscriptTurn(n=3, role="customer", text="z" * 1000, at="2026-10-05T08:03:00+00:00")
        run = turns(2) + [long_turn] + turns(1, start=4)
        chunks = transcript_chunks("EM-1000001", CHAT_REFERENCE, run, limit=300)
        self.assertTrue(all(len(text) <= 300 for text, _ in chunks))
        self.assertEqual([n for _, numbers in chunks for n in numbers], [1, 2, 3, 4])
        parts = [(text, numbers) for text, numbers in chunks if MARKER.match(text).group(3)]
        self.assertGreater(len(parts), 2)
        self.assertEqual([numbers for _, numbers in parts], [[]] * (len(parts) - 1) + [[3]])
        self.assertEqual("".join(text.split("\n", 1)[1] for text, _ in parts), render_transcript([long_turn]))

    def test_a_first_turn_too_long_for_one_comment_still_opens_with_the_utc_line_once(self):
        long_turn = TranscriptTurn(n=1, role="customer", text="z" * 1000, at="2026-10-05T08:01:00+00:00")
        chunks = transcript_chunks("EM-1000001", CHAT_REFERENCE, [long_turn], limit=300)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(text) <= 300 for text, _ in chunks))
        self.assertEqual([UTC in text for text, _ in chunks], [True] + [False] * (len(chunks) - 1))
        self.assertEqual([numbers for _, numbers in chunks], [[]] * (len(chunks) - 1) + [[1]])

    def test_a_limit_with_no_room_after_the_marker_is_refused(self):
        with self.assertRaises(ValueError):
            transcript_chunks("EM-1000001", CHAT_REFERENCE, turns(1), limit=20)

    def test_turns_are_put_in_order_and_the_result_is_stable(self):
        run = turns(10)
        shuffled = run[5:] + run[:5]
        self.assertEqual(transcript_chunks("EM-1000001", CHAT_REFERENCE, shuffled, limit=300),
                         transcript_chunks("EM-1000001", CHAT_REFERENCE, run, limit=300))

    def test_devanagari_is_counted_in_characters_and_kept_whole(self):
        run = turns(30, text="मेरी बैटरी चार्ज नहीं हो रही है")
        chunks = transcript_chunks("EM-1000001", CHAT_REFERENCE, run, limit=500)
        self.assertTrue(all(len(text) <= 500 for text, _ in chunks))
        joined = "\n".join(body_of(text, index == 0) for index, (text, _) in enumerate(chunks))
        self.assertEqual(joined, render_transcript(run))

    def test_a_later_posting_is_marked_from_its_first_new_turn(self):
        [(text, numbers)] = transcript_chunks("EM-1000001", CHAT_REFERENCE, turns(6, start=7))
        self.assertTrue(text.startswith("[stage:EM-1000001 transcript, turns 7-12]\n"))
        self.assertEqual(numbers, [7, 8, 9, 10, 11, 12])


if __name__ == "__main__":
    unittest.main()
