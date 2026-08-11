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
