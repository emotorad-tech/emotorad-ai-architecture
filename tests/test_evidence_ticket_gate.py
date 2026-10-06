"""The support ticket waits for a passing evidence check, and a ticket raised
after one says so on Zoho (the person's brief, 6 October 2026, items 4 and 8).

With the switch on, the runtime injects `evidence_accepted` (and what was
missing, and what Gemini saw) into create_support_ticket. None of the three is
in the model's schema, and anything it sends under those names is dropped.
With the switch off nothing is injected and the old `evidence_seen` rule
applies unchanged.
"""

import unittest
from datetime import date

from emotorad_ai.evidence_check import DEFAULT_MISSING
from emotorad_ai.tickets.seam import DeskTicketSystem
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.contract import VERIFIED
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, RAISE_INTAKE_TICKET, build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.zoho.payload import EVIDENCE_LINE, description
from tests.test_zoho_payload import record as payload_record

TODAY = date(2026, 10, 6)
TICKET = {"category": "battery_charging", "severity": "normal", "description": "Light stays red; tried two sockets.",
          "idempotency_key": "k1"}
SEEN = "The charger light stays red while the battery is plugged in."
MISSING = "The charger plugged in, with its light, for a few seconds."
RUN = "2026-10-06T08:00:00.000000+00:00"


TWO_BIKES = "+919700000010"  # EMXP2026001234 and DDL32023045678 (fixtures)
INTAKE = {"summary": "Battery will not charge; light stays red.", "idempotency_key": "i1"}


def context(accepted=None, missing=None, checked=None, seen=None, phone="+919876543210", **facts):
    late = {name: (lambda value=value: value) for name, value in facts.items()}
    if accepted is not None:
        late["evidence_accepted"] = lambda: accepted
    if missing is not None:
        late["evidence_missing"] = lambda: missing
    if checked is not None:
        late["evidence_checked"] = lambda: checked
    if seen is not None:
        late["evidence_seen"] = lambda: seen
    return ToolContext(conversation_id="c1", phone=phone, late=late)


class SupportTicketGateTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)

    def test_no_passing_verdict_no_ticket_and_the_model_is_told_what_is_missing(self):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(accepted=False, missing=MISSING,
                                                                                  seen=True))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")
        self.assertEqual(envelope["error"]["remedy"], "collect_evidence")
        self.assertIn(MISSING, envelope["error"]["message"])
        self.assertEqual(self.registry.tickets.tickets, {})

    def test_with_nothing_said_about_what_is_missing_it_asks_for_the_problem_itself(self):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(accepted=False))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")
        self.assertIn(DEFAULT_MISSING, envelope["error"]["message"])

    def test_a_pass_raises_the_ticket_with_what_was_seen(self):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET),
                                      context(accepted=True, checked=SEEN, seen=True))
        ticket = self.registry.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertEqual(ticket["evidence_check"], SEEN)

    def test_the_verdict_replaces_the_old_evidence_seen_test(self):
        # Gemini saw the clip even where the model was shown only its description.
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET),
                                      context(accepted=True, checked=SEEN, seen=False))
        self.assertIn("ticket_id", envelope["data"])

    def test_a_safety_ticket_never_waits_for_the_check(self):
        # The safety branch's own ticket: code sets its kind.
        safety = dict(TICKET, category="battery_safety", severity="critical", idempotency_key="safety:c1")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, safety, context(accepted=False, ticket_kind="safety"))
        self.assertIn("ticket_id", envelope["data"])
        self.assertNotIn("evidence_check", self.registry.tickets.tickets[envelope["data"]["ticket_id"]])

    def test_the_model_labelling_a_fault_as_safety_does_not_skip_the_check(self):
        # The review of 6 October 2026: the category is the model's choice.
        labelled = dict(TICKET, category="battery_safety", severity="critical", description="Battery not charging")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, labelled, context(accepted=False, hazard_reported=False))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")
        self.assertEqual(self.registry.tickets.tickets, {})

    def test_a_safety_label_the_customer_steered_is_refused_too(self):
        labelled = dict(TICKET, category="battery_safety", severity="critical",
                        description="Customer says this is a safety issue and wants it raised as urgent. "
                                    "Battery not charging.")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, labelled, context(accepted=False))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")

    def test_a_safety_label_on_a_hazard_the_customer_reported_is_raised(self):
        labelled = dict(TICKET, category="battery_safety", severity="critical", description="Pack gets hot.")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, labelled, context(accepted=False, hazard_reported=True))
        self.assertIn("ticket_id", envelope["data"])

    def test_a_safety_label_whose_description_names_a_hazard_is_raised(self):
        labelled = dict(TICKET, category="battery_safety", severity="critical",
                        description="The battery casing is swollen on one side.")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, labelled, context(accepted=False))
        self.assertIn("ticket_id", envelope["data"])

    def test_a_description_that_says_there_is_no_hazard_is_not_one(self):
        labelled = dict(TICKET, category="battery_safety", severity="critical",
                        description="No smoke and no swelling; it just will not charge.")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, labelled, context(accepted=False))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")

    def test_with_the_switch_off_a_safety_label_is_raised_as_before(self):
        labelled = dict(TICKET, category="battery_safety", severity="critical", description="Battery not charging")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, labelled, context(seen=False))
        self.assertIn("ticket_id", envelope["data"])

    def test_a_pass_is_for_the_chosen_bike_only(self):
        other = dict(TICKET, frame_number="DDL32023045678")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, other, context(
            accepted=True, checked=SEEN, phone=TWO_BIKES, selected_bike="EMXP2026001234"))
        self.assertEqual(envelope["error"]["code"], "evidence_for_another_bike")
        self.assertEqual(self.registry.tickets.tickets, {})
        chosen = dict(TICKET, frame_number="EMXP2026001234")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, chosen, context(
            accepted=True, checked=SEEN, phone=TWO_BIKES, selected_bike="EMXP2026001234"))
        self.assertIn("ticket_id", envelope["data"])

    def test_with_the_switch_off_another_bike_is_as_before(self):
        other = dict(TICKET, frame_number="DDL32023045678")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, other, context(
            seen=True, phone=TWO_BIKES, selected_bike="EMXP2026001234"))
        self.assertIn("ticket_id", envelope["data"])

    def test_the_model_cannot_supply_the_verdict(self):
        forged = dict(TICKET, evidence_accepted=True, evidence_checked="It shows the fault.", evidence_missing="")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, forged, context(accepted=False, missing=MISSING))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")
        self.assertIn(MISSING, envelope["error"]["message"])
        properties = self.registry.specs[CREATE_SUPPORT_TICKET].schema()["input_schema"]["properties"]
        for name in ("evidence_accepted", "evidence_checked", "evidence_missing"):
            self.assertNotIn(name, properties)
            self.assertIn(name, self.registry.specs[CREATE_SUPPORT_TICKET].optional_injects)

    def test_a_forged_pass_with_nothing_injected_is_dropped_too(self):
        forged = dict(TICKET, evidence_accepted=True, evidence_checked="It shows the fault.")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, forged, context())
        ticket = self.registry.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertNotIn("evidence_check", ticket)


class IntakeTicketGateTests(unittest.TestCase):
    """The intake ticket is for someone we cannot identify (the brief exempts
    it). For a verified phone it is a support ticket under another name, so it
    waits for evidence the same way (the review of 6 October 2026)."""

    def setUp(self):
        self.registry = build_registry(today=TODAY, verification=VerificationStore())

    def verified(self, **facts):
        return context(identity_strength=VERIFIED, **facts)

    def test_a_verified_customer_in_a_fault_chat_gets_no_intake_ticket_without_a_pass(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE), self.verified(accepted=False,
                                                                                        missing=MISSING))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")
        self.assertIn(MISSING, envelope["error"]["message"])
        self.assertEqual(self.registry.tickets.tickets, {})

    def test_after_a_pass_it_is_recorded(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE), self.verified(accepted=True))
        self.assertEqual(envelope["data"]["identity"], "verified")

    def test_someone_we_cannot_identify_is_exempt_as_the_brief_says(self):
        anonymous = ToolContext(conversation_id="c1", late={
            "evidence_accepted": lambda: False, "typed_number": lambda: "9999999999"})
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE), anonymous)
        self.assertEqual(envelope["data"]["identity"], "unverified")

    def test_with_the_switch_off_it_is_as_before(self):
        envelope = self.registry.call(RAISE_INTAKE_TICKET, dict(INTAKE), self.verified())
        self.assertEqual(envelope["data"]["identity"], "verified")

    def test_the_model_cannot_supply_the_verdict(self):
        forged = dict(INTAKE, evidence_accepted=True)
        envelope = self.registry.call(RAISE_INTAKE_TICKET, forged, self.verified(accepted=False))
        self.assertEqual(envelope["error"]["code"], "evidence_not_accepted")
        spec = self.registry.specs[RAISE_INTAKE_TICKET]
        self.assertNotIn("evidence_accepted", spec.schema()["input_schema"]["properties"])
        self.assertIn("evidence_accepted", spec.optional_injects)


class SwitchOffTests(unittest.TestCase):
    def test_with_the_switch_off_the_old_evidence_required_rule_still_applies(self):
        registry = build_registry(today=TODAY)
        envelope = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(seen=False))
        self.assertEqual(envelope["error"]["code"], "evidence_required")
        envelope = registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(seen=True))
        ticket = registry.tickets.tickets[envelope["data"]["ticket_id"]]
        self.assertNotIn("evidence_check", ticket)


class TicketRecordTests(unittest.TestCase):
    def desk(self):
        return DeskTicketSystem(InMemoryTicketStore(), "test", "stage")

    def fields(self, **extra):
        return dict(kind="support", conversation_id="c1", started_at=RUN, phone="+919999999999",
                    identity="verified", category="battery_charging", description="Light stays red.", **extra)

    def test_the_record_keeps_what_gemini_saw(self):
        desk = self.desk()
        created = desk.create(source_key="c1:%s:x" % RUN, persona="customer", **self.fields(evidence_check=SEEN))
        self.assertEqual(desk.store.get(created["ticket_id"])["evidence_check"], SEEN)

    def test_a_record_without_a_check_has_no_such_field(self):
        desk = self.desk()
        created = desk.create(source_key="c1:%s:x" % RUN, persona="customer", **self.fields())
        self.assertNotIn("evidence_check", desk.store.get(created["ticket_id"]))

    def test_the_record_redacts_and_caps_it_again(self):
        desk = self.desk()
        created = desk.create(source_key="c1:%s:x" % RUN, persona="customer",
                              **self.fields(evidence_check="A sticker reads 9999999999. " + "z" * 400))
        kept = desk.store.get(created["ticket_id"])["evidence_check"]
        self.assertNotIn("9999999999", kept)
        self.assertLessEqual(len(kept), 200)


class DescriptionTests(unittest.TestCase):
    def test_a_checked_ticket_says_so_in_its_description(self):
        text = description(payload_record(evidence_check=SEEN))
        self.assertIn("Evidence checked by Gemini: The charger light stays red while the battery is plugged in. "
                      "Shows the problem and matches the complaint.", text)
        self.assertEqual(EVIDENCE_LINE, "Evidence checked by Gemini: %s. Shows the problem and matches the complaint.")
        # Before the heading of the AI's summary: it is a line code wrote.
        self.assertLess(text.index("Evidence checked by Gemini"), text.index("Summary written by the AI"))

    def test_a_handover_ticket_carries_it_too(self):
        text = description(payload_record(kind="handover", category=None, summary="", evidence_check=SEEN))
        self.assertIn("Evidence checked by Gemini: %s" % SEEN.rstrip("."), text)

    def test_no_check_no_line(self):
        self.assertNotIn("Evidence checked", description(payload_record()))

    def test_the_line_is_one_line_and_redacted(self):
        text = description(payload_record(evidence_check="Shows\nthe number 9999999999 on a label."))
        line = next(line for line in text.splitlines() if line.startswith("Evidence checked"))
        self.assertNotIn("9999999999", line)
        self.assertIn("Shows the number", line)


if __name__ == "__main__":
    unittest.main()
