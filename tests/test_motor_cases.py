"""AFS's motor diagnosis (§5f, 8 October 2026), as code carries it.

The steps live in the knowledge records under knowledge/motor/; these tests
hold what code adds around them: the motor agent can send the reference clips,
a motor ticket says on Zoho who acts next (the Approval team or the Service
team), a motor jam is raised with no photo or video, E-07 and the T-Rex Pro's
E-24 lead to the E-07 record, the motor cases route to the motor agent, and
the evidence check is told what the reference clips show.
"""

import base64
import json
import unittest
from datetime import date

from emotorad_ai.agents.motor_support import DEFINITION as MOTOR
from emotorad_ai.errorcodes import load_table
from emotorad_ai.evidence_check import REFERENCE_NOTES_INTRO, OpenRouterEvidenceChecker, reference_notes
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.media import load_catalogue
from emotorad_ai.tickets.kinds import subject_label
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, SEND_GUIDE_MEDIA, TICKET_CATEGORIES, build_registry
from emotorad_ai.triage import classify_issue
from emotorad_ai.zoho.payload import NEXT_ACTIONS, description
from tests.test_evidence_check import PASS, PHOTO, FakeTransport
from tests.test_evidence_ticket_gate import context
from tests.test_zoho_payload import record as payload_record

TODAY = date(2026, 10, 8)
MOTOR_CASES = ("motor_fault", "motor_damage", "motor_jam", "motor_under_load")


def reading(part, serial="X12345", confirmed=False, source="ocr"):
    return {"part": part, "serial": serial, "confirmed": confirmed, "source": source}


def ticket(category, key="k1"):
    return {"category": category, "severity": "normal", "idempotency_key": key,
            "description": "Wheel does not turn at PAS 1 with the throttle applied; no error code."}


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)

    def test_the_motor_categories_exist(self):
        self.assertTrue(set(MOTOR_CASES) <= set(TICKET_CATEGORIES))

    def test_every_motor_case_waits_for_evidence_the_jam_too(self):
        for category in MOTOR_CASES:
            with self.subTest(category=category):
                refused = self.registry.call(CREATE_SUPPORT_TICKET, ticket(category), context(accepted=False))
                self.assertEqual(refused["error"]["code"], "evidence_not_accepted")
                refused = self.registry.call(CREATE_SUPPORT_TICKET, ticket(category), context(seen=False))
                self.assertEqual(refused["error"]["code"], "evidence_required")

    def test_zoho_says_who_acts_next(self):
        expected = {"motor_fault": "Approval team", "motor_damage": "Approval team",
                    "motor_jam": "Approval team", "motor_under_load": "Service team"}
        for category, team in expected.items():
            with self.subTest(category=category):
                text = description(payload_record(category=category))
                self.assertIn("Category: %s. Next action: %s" % (category, NEXT_ACTIONS[category]), text)
                self.assertIn(team, NEXT_ACTIONS[category])
                self.assertTrue(subject_label("support", category).startswith("Motor: "))

    def test_a_motor_ticket_waits_for_the_motor_the_controller_and_the_frame(self):
        for readings, missing in (([], ("rear hub motor", "controller's label", "frame number sticker")),
                                  ([reading("motor"), reading("frame")], ("controller's label",))):
            with self.subTest(parts=[r["part"] for r in readings]):
                refused = self.registry.call(CREATE_SUPPORT_TICKET, ticket("motor_fault"),
                                             context(accepted=True, seen=True, serial_readings=readings))
                self.assertEqual(refused["error"]["code"], "serial_photos_required")
                self.assertEqual(refused["error"]["remedy"], "collect_serial_photos")
                for words in missing:
                    self.assertIn(words, refused["error"]["message"])
        self.assertEqual(self.registry.tickets.tickets, {})

    def test_with_all_three_the_ticket_carries_what_was_read(self):
        readings = [reading("motor", "220516001234", confirmed=True), reading("controller", None),
                    reading("frame", "EMXP2025004417", source="typed", confirmed=True)]
        made = self.registry.call(CREATE_SUPPORT_TICKET, ticket("motor_fault"),
                                  context(accepted=True, seen=True, serial_readings=readings))
        stored = self.registry.tickets.tickets[made["data"]["ticket_id"]]
        self.assertIn("From the customer's photos: Motor serial: 220516001234 (read by AI, confirmed by the customer); "
                      "Controller S/N: photo received, not readable; Frame number: EMXP2025004417 (typed by the "
                      "customer).", stored["description"])

    def test_with_the_serial_ask_off_nothing_is_asked_for(self):
        made = self.registry.call(CREATE_SUPPORT_TICKET, ticket("motor_fault"), context(accepted=True, seen=True))
        self.assertIn("ticket_id", made["data"])

    def test_a_battery_ticket_never_waits_for_motor_photos(self):
        battery = dict(ticket("battery_charging"), description="Light stays red.")
        made = self.registry.call(CREATE_SUPPORT_TICKET, battery,
                                  context(accepted=True, seen=True, serial_readings=[]))
        self.assertIn("ticket_id", made["data"])

    def test_a_battery_ticket_has_no_next_action(self):
        self.assertNotIn("Next action", description(payload_record(category="battery_charging")))


class AgentTests(unittest.TestCase):
    def test_the_motor_agent_can_send_the_reference_clips(self):
        self.assertIn(SEND_GUIDE_MEDIA, MOTOR.tool_names)

    def test_its_prompt_says_approvals_are_never_its_own(self):
        from emotorad_ai.agents.motor_support import _BASE_PROMPT

        for words in ("Approvals are never yours", "Approval team", "never say a claim is approved or rejected",
                      "Comparing evidence is yours", "Never send a battery picture"):
            self.assertIn(words, " ".join(_BASE_PROMPT.split()), words)


class ErrorCodeTests(unittest.TestCase):
    def test_e07_and_the_t_rex_pros_e24_lead_to_the_e07_record(self):
        table = load_table()
        for code, bike in (("E-07", "EMX Plus"), ("E07", "T-Rex Air"), ("E24", "T-Rex Pro"), ("E-24", "T-REX PRO")):
            with self.subTest(code=code, bike=bike):
                answer = table.lookup(code, bike)
                self.assertEqual(answer["status"], "found")
                self.assertIn("motor-e07-malfunction", answer["entry"]["verification"])

    def test_e24_on_a_t_rex_plus_v3_is_unchanged(self):
        answer = load_table().lookup("E24", "T-REX + V3")
        self.assertIn("throttle or the motor", answer["entry"]["customer_message"])

    def test_the_e07_record_is_found_by_the_code(self):
        hits = [p.id for p in KnowledgeBase().search("E-07", topic="motor", bike={"throttle": "yes"})]
        self.assertEqual(hits[0], "motor-e07-malfunction")


class TriageTests(unittest.TestCase):
    def test_the_motor_cases_route_to_the_motor(self):
        for text in ("display shows E07", "E-24 on my bike", "the disc rotor is loose", "wheel not rotating",
                     "works on throttle, freewheel issue", "speedometer shows nothing"):
            with self.subTest(text=text):
                self.assertEqual(classify_issue(text), "motor")

    def test_discharge_is_still_the_battery(self):
        self.assertEqual(classify_issue("battery discharges fast"), "battery")


class EvidenceNotesTests(unittest.TestCase):
    def test_the_motor_gets_one_line_per_clip_and_the_battery_none(self):
        notes = reference_notes(load_catalogue(), "motor")
        self.assertEqual(len(notes), 6)
        self.assertTrue(all(":" in note for note in notes))
        self.assertEqual(reference_notes(load_catalogue(), "battery"), [])

    def test_the_notes_go_before_the_customers_media_labelled_as_ours(self):
        transport = FakeTransport(PASS)
        OpenRouterEvidenceChecker(transport=transport).check([PHOTO], "motor noise", "motor",
                                                             reference_notes=["Clip: what it shows"])
        content = transport.posts[0]["body"]["messages"][0]["content"]
        self.assertEqual(content[2], {"type": "text", "text": REFERENCE_NOTES_INTRO + "\n- Clip: what it shows"})
        self.assertEqual(content[3]["image_url"]["url"],
                         "data:image/jpeg;base64," + base64.b64encode(PHOTO[0]).decode())
        self.assertIn("never evidence", REFERENCE_NOTES_INTRO)


if __name__ == "__main__":
    unittest.main()
