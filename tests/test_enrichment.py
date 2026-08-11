import unittest

from emotorad_ai.contract import ANONYMOUS, ASSERTED, VERIFIED, Identity
from emotorad_ai.enrichment import ContextEnricher, summarise_browsing, summarise_signals
from emotorad_ai.identity import ResolvedIdentity

BIKES = [
    {
        "frame_number": "EMXP2025004417",
        "product_name": "EMX Plus",
        "product_color": "Grey",
        "in_warranty": True,
        "months_remaining": 8,
    }
]

EVENTS = [
    {"properties": {"model": "EMX Plus"}},
    {"properties": {"model": "EMX Plus"}},
    {"properties": {"model": "Doodle V3"}},
]


def verified(bikes=BIKES):
    return ResolvedIdentity(
        persona="customer",
        method="verified",
        identity=Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919876543210"),
        profile={"name": "Ananya Rao"},
        bikes=list(bikes),
    )


def anonymous():
    return ResolvedIdentity(
        persona="customer",
        method="unverified",
        identity=Identity(cluster_id="clu-2", strength=ANONYMOUS, em_aid="cookie-1"),
    )


class SummaryTests(unittest.TestCase):
    def test_page_views_collapse_into_one_sentence(self):
        self.assertEqual(
            summarise_browsing(EVENTS), "Recently viewed: EMX Plus (2 views), Doodle V3 (1 view)"
        )

    def test_empty_or_unusable_events_produce_nothing(self):
        self.assertIsNone(summarise_browsing([]))
        self.assertIsNone(summarise_browsing([{"properties": {}}]))

    def test_only_the_top_three_models_survive(self):
        many = [{"properties": {"model": "M%d" % i}} for i in range(10)]
        self.assertEqual(summarise_browsing(many).count(","), 2)

    def test_signals_are_rendered_as_actions_not_event_names(self):
        line = summarise_signals(["emi_page_viewed", "dealer_locator_used"])
        self.assertIn("looked at EMI options", line)
        self.assertNotIn("emi_page_viewed", line)


class DisclosureGateTests(unittest.TestCase):
    """A cookie identifies a browser. A shared laptop is one cookie, several people."""

    def test_a_verified_customer_gets_name_bikes_and_coverage(self):
        context = ContextEnricher().build(verified(), events=EVENTS).render()
        self.assertIn("Ananya Rao", context)
        self.assertIn("EMXP2025004417", context)
        self.assertIn("in warranty", context)

    def test_an_anonymous_visitor_gets_browsing_but_no_personal_facts(self):
        context = ContextEnricher().build(anonymous(), events=EVENTS).render()
        self.assertIn("Recently viewed", context)
        self.assertIn("not verified", context)
        for leak in ("Ananya", "EMXP2025004417", "in warranty"):
            self.assertNotIn(leak, context, leak)

    def test_an_asserted_caller_is_treated_as_unverified(self):
        # Caller ID is spoofable, so it personalises but never discloses.
        spoofable = ResolvedIdentity(
            persona="customer",
            method="unverified",
            identity=Identity(cluster_id="c", strength=ASSERTED, phone="+919876543210"),
            profile={"name": "Ananya Rao"},
            bikes=BIKES,
        )
        context = ContextEnricher().build(spoofable).render()
        self.assertNotIn("Ananya", context)

    def test_no_events_and_no_verification_still_renders_safely(self):
        context = ContextEnricher().build(anonymous()).render()
        self.assertIn("not verified", context)


class BudgetTests(unittest.TestCase):
    def test_the_budget_is_respected(self):
        enricher = ContextEnricher(token_budget=12)
        context = enricher.build(verified(), events=EVENTS, signals=["emi_page_viewed"])
        self.assertLessEqual(context.tokens, 12)
        self.assertTrue(context.dropped)

    def test_ownership_survives_when_lower_value_context_is_dropped(self):
        # Ordering decides what the agent still knows under pressure. Ownership is
        # what it can act on; browsing and signals are colour.
        enricher = ContextEnricher(token_budget=40)
        context = enricher.build(verified(), events=EVENTS, signals=["checkout_started"])
        self.assertIn("identity", context.sections)
        self.assertIn("bikes", context.sections)
        self.assertTrue(context.dropped, "something had to give at this budget")
        self.assertTrue(
            set(context.dropped) <= {"browsing", "signals", "recent_conversation"},
            "only low-priority sections may be dropped: %s" % context.dropped,
        )

    def test_a_cheap_low_priority_section_may_survive_a_dropped_expensive_one(self):
        # Best-fit, not a strict cut-off: skipping an 8-token section only because
        # a 14-token higher-priority one did not fit would waste the budget for no
        # gain. Priority still decides who gets first claim on it.
        enricher = ContextEnricher(token_budget=40)
        context = enricher.build(verified(), events=EVENTS, signals=["checkout_started"])
        self.assertIn("signals", context.sections)
        self.assertIn("browsing", context.dropped)

    def test_sections_are_kept_whole_or_not_at_all(self):
        # A truncated half-sentence reads to the model as a complete fact.
        enricher = ContextEnricher(token_budget=10)
        context = enricher.build(verified(), events=EVENTS)
        for block in context.sections.values():
            self.assertFalse(block.endswith("…"))
            self.assertTrue(block.strip())

    def test_a_generous_budget_drops_nothing(self):
        context = ContextEnricher().build(verified(), events=EVENTS, signals=["add_to_cart"])
        self.assertEqual(context.dropped, [])


class MultiBikeTests(unittest.TestCase):
    def test_every_owned_bike_appears_with_its_own_coverage(self):
        bikes = [
            dict(BIKES[0]),
            {
                "frame_number": "DDL32021100455",
                "product_name": "Doodle V3",
                "in_warranty": False,
            },
            {
                "frame_number": "EMXP2024773311",
                "product_name": "EMX Plus",
                "coverage_status": "purchase_date_missing",
                "in_warranty": None,
            },
        ]
        context = ContextEnricher().build(verified(bikes)).render()
        self.assertIn("Owns 3 bikes", context)
        self.assertIn("out of warranty", context)
        self.assertIn("no purchase date on record", context)


if __name__ == "__main__":
    unittest.main()
