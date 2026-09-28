import unittest

from emotorad_ai.decisions import (
    EMPTY_MESSAGE,
    NONE,
    NONE_OF_THESE,
    Q_CATEGORY,
    Q_ERROR_CODE,
    Q_LANGUAGE,
    Q_STANDARD,
    Q_SUB_CATEGORY,
    Q_WARRANTY,
    PrefetchCall,
    Thresholds,
    build_catalogue,
    route,
)
from emotorad_ai.jev import JevDecision, choose, yes
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.standard_responses import StandardResponse

T = Thresholds()
THANKS = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome.", "hinglish": "Swagat hai."})
EMX = {"product_name": "EMX Plus"}
DOODLE = {"product_name": "Doodle V3"}


def decide(**answers):
    return JevDecision(answers=answers)


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogue = build_catalogue(KnowledgeBase(), standard=[THANKS])

    def go(self, decision, error=None, current=None, bike=EMX):
        return route(decision, error, T, self.catalogue, current_sub_category=current, bike=bike)

    # rule 1 ------------------------------------------------------------------
    def test_no_decision_is_the_full_agent_with_the_reason(self):
        self.assertEqual(self.go(None).path, "full")
        self.assertEqual(self.go(None).reasons, ("jev_disabled",))
        self.assertEqual(self.go(None, error="unavailable").reasons, ("jev_error:unavailable",))

    def test_an_empty_message_mid_flow_stays_on_the_current_record(self):
        result = self.go(None, error=EMPTY_MESSAGE, current="battery-wont-charge")
        self.assertEqual((result.path, result.sub_category, result.category), ("narrow", "battery-wont-charge", "battery"))
        self.assertEqual(self.go(None, error=EMPTY_MESSAGE).path, "full")

    # rule 2 ------------------------------------------------------------------
    def test_a_confident_standard_response_in_a_language_we_have(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.95), Q_LANGUAGE: choose("english", 0.9)}))
        self.assertEqual((result.path, result.standard_response_id, result.language), ("standard", "std-thanks", "english"))

    def test_a_standard_response_without_a_reply_in_that_language_goes_to_the_full_agent(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.99), Q_LANGUAGE: choose("hindi", 0.99)}))
        self.assertEqual(result.path, "full")
        self.assertIn("standard_no_reply_for:hindi", result.reasons)

    def test_a_standard_response_needs_a_confident_language(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-thanks", 0.99), Q_LANGUAGE: choose("english", 0.5)}))
        self.assertEqual(result.path, "full")
        self.assertIn("standard_language_unsure", result.reasons)

    def test_standard_threshold_boundary(self):
        at = self.go(decide(**{Q_STANDARD: choose("std-thanks", T.standard_response), Q_LANGUAGE: choose("english", 0.9)}))
        below = self.go(decide(**{Q_STANDARD: choose("std-thanks", T.standard_response - 0.0001), Q_LANGUAGE: choose("english", 0.9)}))
        self.assertEqual((at.path, below.path), ("standard", "full"))

    def test_none_as_the_standard_choice_is_not_a_standard_reply(self):
        result = self.go(decide(**{Q_STANDARD: choose(NONE, 0.99), Q_LANGUAGE: choose("english", 0.99)}))
        self.assertEqual(result.path, "full")

    def test_a_standard_id_we_do_not_know_is_ignored(self):
        result = self.go(decide(**{Q_STANDARD: choose("std-gone", 0.99), Q_LANGUAGE: choose("english", 0.99)}))
        self.assertIn("standard_unknown:std-gone", result.reasons)

    # rule 3 ------------------------------------------------------------------
    def test_confident_category_and_sub_category_pick_the_narrow_agent(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.9), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.8)}))
        self.assertEqual((result.path, result.category, result.sub_category), ("narrow", "battery", "battery-wont-charge"))
        self.assertEqual(result.prefetch, ())

    def test_category_and_sub_category_boundaries(self):
        def at(category_p, sub_p):
            return self.go(decide(**{Q_CATEGORY: choose("battery", category_p), Q_SUB_CATEGORY: choose("battery-wont-charge", sub_p)})).path
        self.assertEqual(at(T.category, T.sub_category), "narrow")
        self.assertEqual(at(T.category - 0.0001, 0.99), "full")
        self.assertEqual(at(0.99, T.sub_category - 0.0001), "full")

    def test_a_sub_category_from_the_other_topic_is_a_mismatch(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("motor-noise", 0.95)}))
        self.assertEqual(result.path, "full")
        self.assertIn("topic_mismatch:battery/motor-noise", result.reasons)

    def test_a_record_that_does_not_apply_to_this_bike_is_refused(self):
        # battery-wont-power-on excludes the Doodle; its Doodle twin applies only to it.
        refused = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-power-on", 0.95)}), bike=DOODLE)
        self.assertEqual(refused.path, "full")
        self.assertIn("not_applicable:battery-wont-power-on", refused.reasons)
        allowed = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-doodle-wont-power-on", 0.95)}), bike=DOODLE)
        self.assertEqual(allowed.path, "narrow")

    def test_none_of_these_is_never_narrow(self):
        result = self.go(decide(**{Q_CATEGORY: choose(NONE_OF_THESE, 0.99), Q_SUB_CATEGORY: choose(NONE, 0.99)}))
        self.assertEqual(result.path, "full")
        self.assertIn("category_none_of_these", result.reasons)

    # rule 4 ------------------------------------------------------------------
    def test_an_unsure_follow_up_continues_the_current_record(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose(NONE, 0.6)}), current="battery-wont-charge")
        self.assertEqual((result.path, result.sub_category), ("narrow", "battery-wont-charge"))
        self.assertIn("narrow_continue:battery-wont-charge", result.reasons)

    def test_a_confident_switch_of_topic_does_not_continue(self):
        result = self.go(decide(**{Q_CATEGORY: choose("motor", 0.95), Q_SUB_CATEGORY: choose(NONE, 0.9)}), current="battery-wont-charge")
        self.assertEqual((result.path, result.category), ("full", "motor"))

    def test_a_confident_new_record_replaces_the_current_one(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-range-dropped", 0.9)}), current="battery-wont-charge")
        self.assertEqual(result.sub_category, "battery-range-dropped")

    # prefetch ----------------------------------------------------------------
    def test_prefetch_follows_the_warranty_noul_and_the_error_code(self):
        result = self.go(decide(**{
            Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9),
            Q_WARRANTY: yes(T.tools["lookup_warranty_record"]), Q_ERROR_CODE: choose("E07", 0.9),
        }))
        self.assertEqual(result.prefetch, (PrefetchCall("lookup_warranty_record", {}), PrefetchCall("lookup_error_code", {"code": "E07"})))
        quiet = self.go(decide(**{
            Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9),
            Q_WARRANTY: yes(T.tools["lookup_warranty_record"] - 0.0001), Q_ERROR_CODE: choose(NONE, 0.99),
        }))
        self.assertEqual(quiet.prefetch, ())

    def test_scores_travel_with_the_route_for_the_log(self):
        result = self.go(decide(**{Q_CATEGORY: choose("battery", 0.9), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.8)}))
        self.assertEqual(result.scores[Q_CATEGORY], {"choice": "battery", "p": 0.9})


if __name__ == "__main__":
    unittest.main()
