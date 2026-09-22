"""The coverage post-check.

Calling the warranty tool proves the tool ran. It does not prove the reply
matches what came back — and a plausible, warm, wrong sentence about coverage is
what a tribunal held Air Canada to. Every case here is a reply that would have
been sent without this check.
"""

import unittest

from emotorad_ai.guardrails import check_coverage_claim

IN_WARRANTY = [{"data": {"bikes": [{"in_warranty": True, "frame_number": "F1"}]}}]
OUT_OF_WARRANTY = [{"data": {"bikes": [{"in_warranty": False, "frame_number": "F1"}]}}]
UNKNOWN_COVERAGE = [{"data": {"bikes": [{"in_warranty": None, "frame_number": "F1"}]}}]
NO_TOOLS: list = []


class ClaimDetectionTests(unittest.TestCase):
    def test_replies_making_no_coverage_claim_pass_untouched(self):
        for reply in (
            "Try a different wall socket and tell me if the light comes on.",
            "I have raised ticket EM-00012 for you.",
            "Could you tell me how old the charger is?",
        ):
            self.assertFalse(check_coverage_claim(reply, NO_TOOLS).blocked, reply)

    def test_positive_claims_are_recognised_in_several_phrasings(self):
        for reply in (
            "Good news, this is covered under warranty.",
            "Your battery is still under warranty.",
            "That repair is covered, so there is no charge.",
            "We'll replace it free of charge.",
        ):
            self.assertTrue(check_coverage_claim(reply, NO_TOOLS).blocked, reply)

    def test_the_electrical_sense_of_charge_is_not_a_coverage_claim(self):
        # Staging, 2026-09-22: a two-bike customer pressed the SOC button, saw no
        # light, and the reply "the pack having no charge to show" was blocked as
        # a promise of free repair. In a battery bot "no charge", "low charge" and
        # "holding no charge" are the symptom vocabulary, not the price.
        disagreeing = [{"data": {"bikes": [
            {"in_warranty": True, "frame_number": "F1"},
            {"in_warranty": False, "frame_number": "F2"},
        ]}}]
        for reply in (
            "That points to the pack having no charge to show, so let's check the charger next.",
            "The battery is holding no charge at all after a full night on the charger.",
            "A pack with no charge left reads exactly like a dead one.",
            "It will not take a charge without the switch on.",
        ):
            self.assertFalse(check_coverage_claim(reply, NO_TOOLS).blocked, reply)
            self.assertFalse(check_coverage_claim(reply, disagreeing).blocked, reply)

    def test_the_money_sense_of_charge_still_is_a_coverage_claim(self):
        for reply in (
            "We will replace the pack at no charge.",
            "The replacement is free of charge.",
            "This will be done without charge to you.",
            "There is no cost to you for this.",
        ):
            self.assertTrue(check_coverage_claim(reply, NO_TOOLS).blocked, reply)

    def test_negative_claims_are_not_mistaken_for_positive_ones(self):
        # "not covered" contains "covered". Reading it as a positive claim would
        # block every correct refusal and pass every wrong promise.
        check = check_coverage_claim("This is not covered under warranty.", OUT_OF_WARRANTY)
        self.assertFalse(check.blocked)


class ContradictionTests(unittest.TestCase):
    def test_claiming_cover_on_an_out_of_warranty_bike_is_blocked(self):
        check = check_coverage_claim("Yes, this is covered under warranty.", OUT_OF_WARRANTY)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "coverage_claim_contradicts_tool_result")
        self.assertEqual(check.claimed, "covered")
        self.assertEqual(check.actual, "not_covered")

    def test_denying_cover_on_an_in_warranty_bike_is_also_blocked(self):
        # The rarer direction, and just as wrong: the customer pays for a repair
        # they were entitled to, and nobody ever finds out.
        check = check_coverage_claim(
            "I'm afraid that is out of warranty, so it would be chargeable.", IN_WARRANTY
        )
        self.assertTrue(check.blocked)
        self.assertEqual(check.claimed, "not_covered")
        self.assertEqual(check.actual, "covered")

    def test_a_correct_claim_in_either_direction_passes(self):
        self.assertFalse(
            check_coverage_claim("Yes, this is covered under warranty.", IN_WARRANTY).blocked
        )
        self.assertFalse(
            check_coverage_claim("That is out of warranty, I'm afraid.", OUT_OF_WARRANTY).blocked
        )


class TwoQuestionCoverageTests(unittest.TestCase):
    """Coverage is two questions, not one, and a correct answer says both.

    In the warranty term with no physical damage is free; physical damage is
    chargeable even inside the term; out of the term is chargeable. That rule
    is confirmed by the business and encoded in battery-warranty-replacement,
    so a correct reply about a bike in warranty with heat damage has to say
    "you are in warranty" and "impact damage would be chargeable" in the same
    breath.

    Blocked live on 20 September, conversation ef679f97. The reply below is
    close to a model answer, and the check read its "chargeable" as a denial of
    cover contradicting in_warranty=True. The customer got a handover instead.
    """

    BLOCKED_LIVE = (
        "On cost \u2014 your bike is in warranty on our record, with about 7 months "
        "left. That's counted from when the bike was registered, 1 April 2025, since "
        "there's no purchase date on file, so your invoice can confirm the exact date. "
        "If the service centre finds nothing from an impact, a covered fault costs you "
        "nothing; if they do find impact damage, it's chargeable even within warranty, "
        "and they'll confirm any cost with you before starting work. I can't quote "
        "figures myself."
    )

    def test_the_reply_blocked_live_passes(self):
        check = check_coverage_claim(self.BLOCKED_LIVE, IN_WARRANTY)
        self.assertFalse(check.blocked, check)

    def test_in_warranty_plus_a_conditional_charge_is_not_a_contradiction(self):
        check = check_coverage_claim(
            "You're in warranty, so a fault is covered; damage from a drop would be chargeable.",
            IN_WARRANTY,
        )
        self.assertFalse(check.blocked, check)

    def test_a_flat_denial_on_an_in_warranty_bike_is_still_blocked(self):
        """Saying only the negative half is still a contradiction. The fix must
        not weaken the check it corrects."""
        check = check_coverage_claim(
            "That would be chargeable, I'm afraid.", IN_WARRANTY
        )
        self.assertTrue(check.blocked)
        self.assertEqual(check.claimed, "not_covered")

    def test_both_halves_on_an_out_of_warranty_bike_are_still_blocked(self):
        """The 'covered' half is unsupported when the tool says the term has
        ended. Conservative in both directions, as before."""
        check = check_coverage_claim(
            "You're in warranty, so a fault is covered; damage would be chargeable.",
            OUT_OF_WARRANTY,
        )
        self.assertTrue(check.blocked)

    def test_a_negation_alone_does_not_count_as_the_positive_half(self):
        """'is not covered' contains 'covered'. It must not be read as saying
        both, or every correct refusal on an out-of-warranty bike would pass
        as a two-question answer."""
        check = check_coverage_claim("This is not covered under warranty.", IN_WARRANTY)
        self.assertTrue(check.blocked)
        self.assertEqual(check.claimed, "not_covered")


class UnsupportedClaimTests(unittest.TestCase):
    def test_a_coverage_claim_with_no_tool_call_at_all_is_blocked(self):
        # The Air Canada shape exactly: a confident policy statement with no
        # system behind it. The model has no other source for this.
        check = check_coverage_claim("Don't worry, this is covered under warranty.", NO_TOOLS)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "coverage_claim_without_tool_result")

    def test_a_claim_is_blocked_when_coverage_was_undeterminable(self):
        # in_warranty is None — the missing purchase_date path. There is no fact
        # to support a claim in either direction.
        check = check_coverage_claim("This is covered under warranty.", UNKNOWN_COVERAGE)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "coverage_claim_without_tool_result")

    def test_a_claim_across_bikes_that_disagree_is_blocked(self):
        # Three bikes, mixed coverage. "It's covered" cannot be verified against
        # "one of them is", and the customer will hear it about whichever bike
        # they had in mind.
        mixed = [{"data": {"bikes": [{"in_warranty": True}, {"in_warranty": False}]}}]
        check = check_coverage_claim("Yes, that's covered under warranty.", mixed)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "coverage_claim_across_disagreeing_bikes")


class ShapeTests(unittest.TestCase):
    def test_a_flat_result_without_a_bikes_list_still_counts(self):
        flat = [{"data": {"in_warranty": False}}]
        self.assertTrue(check_coverage_claim("It is covered under warranty.", flat).blocked)

    def test_error_envelopes_and_junk_do_not_crash_the_check(self):
        for results in (
            [{"error": {"code": "oms_unavailable"}}],
            [{}],
            [{"data": None}],
            [{"data": {"bikes": []}}],
        ):
            check = check_coverage_claim("This is covered under warranty.", results)
            self.assertTrue(check.blocked, results)


if __name__ == "__main__":
    unittest.main()


class EvidencePostCheckTests(unittest.TestCase):
    """A fault concluded with nothing seen.

    The rule existed in the prompt in four forms across three versions and was
    skipped every time: stated at 26% of a 22,000-character prompt, it lost to
    whichever procedure the model was working at 74%. A rule only holds where it
    is written, and a long prompt cannot have it written everywhere. So it moved
    into code, beside the coverage check, for the same reason that one is there.
    """

    def _check(self, reply, seen=False):
        from emotorad_ai.guardrails import check_evidence

        return check_evidence(reply, evidence_seen=seen)

    def test_the_reply_that_started_this_is_blocked(self):
        # Verbatim from chat 20260909-edb42fba, turn 13.
        verdict = self._check("That's pointing toward a dead battery.")
        self.assertTrue(verdict.blocked)
        self.assertEqual(verdict.reason, "fault_concluded_without_evidence")

    def test_raising_a_ticket_with_nothing_seen_is_blocked(self):
        self.assertTrue(self._check("I'll raise a ticket for you now.").blocked)

    def test_moving_to_warranty_with_nothing_seen_is_blocked(self):
        self.assertTrue(self._check("Let me proceed with the warranty replacement.").blocked)

    def test_evidence_anywhere_in_the_conversation_clears_it(self):
        # Not "this turn": a customer who sent the photo three turns ago must not
        # be asked again because the model concluded later.
        self.assertFalse(self._check("That's pointing toward a dead battery.", seen=True).blocked)

    def test_the_honest_undetermined_reply_is_not_blocked(self):
        # The one correct answer when there is no multimeter. Blocking it would
        # make the guardrail worse than the problem.
        for reply in (
            "It could be the battery, or it could be another part of the cycle.",
            "I can't tell for certain over chat whether the battery is the issue.",
            "That's inconclusive on its own, so I need to dig a bit deeper.",
        ):
            self.assertFalse(self._check(reply).blocked, reply)

    def test_asking_for_evidence_is_not_concluding(self):
        self.assertFalse(self._check("Before I raise a case, I need a short video.").blocked)

    def test_a_question_about_the_led_is_not_a_conclusion(self):
        self.assertFalse(self._check("What colour is the charger LED showing?").blocked)

    def test_a_safety_handover_is_never_held_for_a_photo(self):
        # Never ask someone to photograph a battery that may be dangerous.
        verdict = self._check("Stop using and stop charging the battery immediately.")
        self.assertFalse(verdict.blocked)
        self.assertEqual(verdict.reason, "safety_exempt")

    def test_a_hedge_in_an_earlier_sentence_does_not_soften_a_later_conclusion(self):
        reply = "It could be a few things. The battery is dead and needs replacing."
        self.assertTrue(self._check(reply).blocked)
