"""Merge semantics for the identity graph.

These are the behaviours the Nest implementation must reproduce. A wrong merge
fuses two customers permanently and cannot be unpicked afterwards, so this file
is the specification — treat a failure here as a privacy bug, not a test bug.
"""

import itertools
import unittest

from emotorad_ai.contract import ANONYMOUS, ASSERTED, VERIFIED
from emotorad_ai.identity import (
    ANON_ID,
    EMAIL,
    PHONE,
    WA_ID,
    IdentityGraph,
    IdentityResolver,
    normalise,
)
from emotorad_ai.tools.mocks import build_registry


def make_graph():
    """Deterministic cluster IDs so failures name the cluster that went wrong."""
    counter = itertools.count(1)
    return IdentityGraph(new_id=lambda: "clu-%d" % next(counter))


class NormalisationTests(unittest.TestCase):
    def test_every_phone_shape_lands_on_one_string(self):
        for raw in ("9876543210", "+919876543210", "+91 98765 43210", "+91-98765-43210"):
            self.assertEqual(normalise(PHONE, raw), "+919876543210", raw)

    def test_whatsapp_senders_match_web_form_phones(self):
        # WhatsApp sends digits with no "+". If these two diverge, the WhatsApp
        # stitch silently stops working and every chat starts from zero.
        self.assertEqual(normalise(WA_ID, "919876543210"), normalise(PHONE, "9876543210"))

    def test_trunk_and_international_prefixes_do_not_create_a_second_person(self):
        # Regression. Both shapes are ordinary in Indian data entry, and both
        # previously produced corrupt E.164 ("+09876543210", "+00919876543210")
        # that could never match the same person's other rows — a duplicate
        # customer, created silently, with no error raised anywhere.
        for raw in ("09876543210", "0 98765 43210", "00919876543210", "0091 98765 43210"):
            self.assertEqual(normalise(PHONE, raw), "+919876543210", raw)

    def test_the_trunk_strip_only_fires_at_eleven_digits(self):
        # A ten-digit string keeps all ten digits — the leading 0 is not treated
        # as a trunk prefix, because stripping it would leave nine and quietly
        # corrupt the number. It still gets the +91 default, which is the
        # documented assumption and wrong for a foreign national-format number;
        # that limitation is case 3.11 in the edge case register.
        self.assertEqual(normalise(PHONE, "0151234567"), "+910151234567")

    def test_emails_are_case_folded(self):
        self.assertEqual(normalise(EMAIL, "  Ananya@GMail.com "), "ananya@gmail.com")

    def test_unknown_type_and_empty_value_are_rejected(self):
        with self.assertRaises(ValueError):
            normalise("passport", "x")
        with self.assertRaises(ValueError):
            normalise(PHONE, "   ")


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = make_graph()

    def test_a_browser_gets_one_cluster_and_keeps_it(self):
        first = self.graph.cluster_for("cookie-laptop")
        self.assertEqual(first, self.graph.cluster_for("cookie-laptop"))

    def test_two_devices_stay_separate_until_something_proves_otherwise(self):
        laptop = self.graph.cluster_for("cookie-laptop")
        phone = self.graph.cluster_for("cookie-phone")
        self.assertNotEqual(laptop, phone)
        self.assertEqual(self.graph.merges, [])

    def test_a_verified_phone_on_a_second_device_merges_the_two(self):
        laptop = self.graph.cluster_for("cookie-laptop")          # 10 Aug
        phone = self.graph.cluster_for("cookie-phone")            # 15 Aug
        self.graph.link("cookie-phone", PHONE, "9876543210", verified=True)

        survivor = self.graph.link("cookie-laptop", PHONE, "+919876543210", verified=True)

        self.assertEqual(survivor, laptop, "the older cluster must survive")
        self.assertEqual(self.graph.resolve(ANON_ID, "cookie-phone"), laptop)
        self.assertEqual(len(self.graph.merges), 1)
        self.assertEqual(self.graph.merges[0].from_cluster, phone)
        self.assertEqual(self.graph.merges[0].to_cluster, laptop)

    def test_the_older_cluster_survives_regardless_of_arrival_order(self):
        # Same facts, opposite order. Replaying events must give the same answer,
        # or two environments diverge on which cluster is canonical.
        graph = make_graph()
        laptop = graph.cluster_for("cookie-laptop")
        graph.cluster_for("cookie-phone")
        graph.link("cookie-laptop", PHONE, "9876543210", verified=True)
        self.assertEqual(graph.link("cookie-phone", PHONE, "9876543210", verified=True), laptop)

    def test_an_unverified_identifier_never_merges(self):
        laptop = self.graph.cluster_for("cookie-laptop")
        other = self.graph.cluster_for("cookie-other")
        self.graph.link("cookie-other", EMAIL, "ananya@gmail.com", verified=False)

        # Someone typos a stranger's address into a form. Merging here would fuse
        # two customers' histories permanently.
        result = self.graph.link("cookie-laptop", EMAIL, "ananya@gmail.com", verified=False)

        self.assertEqual(result, laptop)
        self.assertNotEqual(laptop, other)
        self.assertEqual(self.graph.merges, [])

    def test_an_unverified_identifier_is_still_recorded(self):
        self.graph.link("cookie-laptop", EMAIL, "ananya@gmail.com", verified=False)
        types = {row["type"] for row in self.graph.identifiers(self.graph.cluster_for("cookie-laptop"))}
        self.assertEqual(types, {ANON_ID, EMAIL})

    def test_verification_upgrades_an_existing_unverified_row(self):
        self.graph.link("cookie-laptop", PHONE, "9876543210", verified=False)
        cluster = self.graph.link("cookie-laptop", PHONE, "9876543210", verified=True)
        self.assertEqual(self.graph.verified_phone(cluster), "+919876543210")

    def test_a_verified_row_is_never_downgraded(self):
        self.graph.link("cookie-laptop", PHONE, "9876543210", verified=True)
        cluster = self.graph.link("cookie-laptop", PHONE, "9876543210", verified=False)
        self.assertEqual(self.graph.verified_phone(cluster), "+919876543210")

    def test_whatsapp_with_no_ref_code_stands_alone(self):
        # No browser behind the message. It must not raise, and it must not
        # attach itself to some arbitrary existing cluster.
        cluster = self.graph.link(None, WA_ID, "919999888877", verified=True)
        self.assertTrue(cluster)
        self.assertEqual(self.graph.identifiers(cluster)[0]["value"], "+919999888877")

    def test_a_whatsapp_sender_is_the_same_identifier_as_a_web_form_phone(self):
        # Regression. wa_id and phone were once separate identity types, so the
        # same number stored from two channels produced two clusters — the same
        # human, the same digits, and no error anywhere. A WhatsApp ID *is* a
        # phone number, so it is stored as one.
        web = self.graph.link("cookie-laptop", PHONE, "9876543210", verified=True)
        whatsapp = self.graph.link(None, WA_ID, "919876543210", verified=True)

        self.assertEqual(whatsapp, web)
        self.assertEqual(self.graph.identifiers(web), self.graph.identifiers(whatsapp))
        self.assertEqual(
            len([r for r in self.graph.identifiers(web) if r["type"] == PHONE]), 1
        )

    def test_whatsapp_with_a_ref_code_inherits_the_browsing_history(self):
        laptop = self.graph.cluster_for("cookie-laptop")
        cluster = self.graph.link("cookie-laptop", WA_ID, "919876543210", verified=True)
        self.assertEqual(cluster, laptop, "the ref: stitch is the whole point")

    def test_resolving_an_unseen_identifier_returns_none_rather_than_raising(self):
        self.assertIsNone(self.graph.resolve(PHONE, "9000000000"))

    def test_merging_carries_every_row_across(self):
        self.graph.cluster_for("cookie-laptop")
        self.graph.link("cookie-phone", PHONE, "9876543210", verified=True)
        self.graph.link("cookie-phone", EMAIL, "ananya@gmail.com", verified=False)
        survivor = self.graph.link("cookie-laptop", PHONE, "9876543210", verified=True)

        types = {row["type"] for row in self.graph.identifiers(survivor)}
        self.assertEqual(types, {ANON_ID, PHONE, EMAIL})
        self.assertEqual(len(self.graph.identifiers(survivor)), 4)  # two cookies


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = IdentityResolver(build_registry(), graph=make_graph())

    def test_voice_resolves_a_person_but_authorises_nothing(self):
        persona, identity = self.resolver.resolve_voice("+919876543210")
        self.assertEqual(persona, "customer")
        self.assertEqual(identity.strength, ASSERTED)
        self.assertFalse(identity.may_disclose, "caller ID is spoofable")
        self.assertTrue(identity.cluster_id)

    def test_a_spoofed_caller_id_never_reaches_the_oms(self):
        from emotorad_ai.contract import InboundMessage

        _, identity = self.resolver.resolve_voice("+919876543210")
        message = InboundMessage("c1", "customer", identity, "voice", "am I in warranty?")
        resolved = self.resolver.hydrate(message)

        self.assertEqual(resolved.method, "unverified")
        self.assertIsNone(resolved.profile, "no profile means nothing to leak")

    def test_whatsapp_is_verified_because_the_platform_proves_the_number(self):
        persona, identity = self.resolver.resolve_whatsapp("919876543210")
        self.assertEqual(persona, "customer")
        self.assertEqual(identity.strength, VERIFIED)
        self.assertEqual(identity.phone, "+919876543210")

    def test_an_anonymous_website_visitor_gets_a_cluster_but_no_disclosure(self):
        persona, identity = self.resolver.resolve_website("cookie-new", session_token=None)
        self.assertEqual(persona, "customer")
        self.assertEqual(identity.strength, ANONYMOUS)
        self.assertTrue(identity.cluster_id)
        self.assertFalse(identity.may_disclose)

    def test_internal_sso_separates_the_actor_from_the_subject(self):
        from emotorad_ai.contract import InboundMessage

        _, employee = self.resolver.resolve_internal("Ops@emotorad.com")
        self.assertEqual(employee.employee_email, "ops@emotorad.com")

        _, customer = self.resolver.resolve_whatsapp("919876543210")
        message = InboundMessage(
            "c1", "internal", employee, "internal_portal", "her coverage?", subject=customer
        )

        # Scoping to the actor would hand the employee their own record.
        resolved = self.resolver.hydrate(message)
        self.assertEqual(resolved.profile["name"], "Ananya Rao")

    def test_a_verified_customer_with_no_warranty_record_is_not_a_stranger(self):
        from emotorad_ai.contract import InboundMessage

        _, identity = self.resolver.resolve_whatsapp("919000000000")
        message = InboundMessage("c1", "customer", identity, "whatsapp", "hi")
        resolved = self.resolver.hydrate(message)

        # Registration is routinely skipped. This routes to Late Warranty
        # Registration, not to "we don't know you".
        self.assertEqual(resolved.method, "no_warranty_record")
        self.assertTrue(resolved.identity.cluster_id)

    def test_a_verified_customer_gets_profile_and_coverage_preloaded(self):
        from emotorad_ai.contract import InboundMessage

        _, identity = self.resolver.resolve_whatsapp("919876543210")
        message = InboundMessage("c1", "customer", identity, "whatsapp", "hi")
        resolved = self.resolver.hydrate(message)

        self.assertEqual(resolved.method, "verified")
        self.assertEqual(resolved.profile["name"], "Ananya Rao")
        self.assertEqual(len(resolved.bikes), 1)
        self.assertEqual(resolved.single_bike["term_source"], "provisional")

    def test_multi_bike_gives_no_single_bike_so_the_agent_has_to_ask(self):
        from emotorad_ai.contract import InboundMessage

        _, identity = self.resolver.resolve_whatsapp("919700000001")
        resolved = self.resolver.hydrate(
            InboundMessage("c1", "customer", identity, "whatsapp", "battery issue")
        )
        self.assertEqual(len(resolved.bikes), 3)
        self.assertIsNone(resolved.single_bike, "three bikes must not collapse to one")

    def test_an_oms_outage_is_not_the_no_record_path(self):
        from emotorad_ai.contract import InboundMessage
        from emotorad_ai.identity import IdentityResolver

        resolver = IdentityResolver(build_registry(oms_available=False), graph=make_graph())
        _, identity = resolver.resolve_whatsapp("919876543210")
        resolved = resolver.hydrate(
            InboundMessage("c1", "customer", identity, "whatsapp", "hi")
        )
        self.assertEqual(resolved.method, "oms_error")
        self.assertEqual(resolved.error, "oms_unavailable")
        self.assertFalse(resolved.is_known_customer)


class PromptDisclosureTests(unittest.TestCase):
    """What the model is *told* is the last gate before it speaks."""

    def setUp(self):
        from emotorad_ai.identity import IdentityResolver

        self.resolver = IdentityResolver(build_registry(), graph=make_graph())

    def _prompt(self, resolver, phone, channel="whatsapp"):
        from emotorad_ai.agents.battery_support import DEFINITION
        from emotorad_ai.contract import InboundMessage

        _, identity = resolver.resolve_whatsapp(phone)
        message = InboundMessage("c1", "customer", identity, channel, "battery issue")
        return DEFINITION.build_system_prompt(message, resolver.hydrate(message))

    def test_an_unregistered_customer_is_offered_registration_not_a_dead_end(self):
        from emotorad_ai.tools import fixtures

        prompt = self._prompt(self.resolver, fixtures.PHONE_WITH_NO_RECORD.lstrip("+"))
        self.assertIn("register their bike", prompt)
        self.assertNotIn("EMX Plus", prompt)

    def test_an_outage_is_not_reported_to_the_customer_as_being_unregistered(self):
        from emotorad_ai.identity import IdentityResolver

        resolver = IdentityResolver(build_registry(oms_available=False), graph=make_graph())
        prompt = self._prompt(resolver, "919876543210")
        self.assertIn("not responding", prompt)
        self.assertNotIn("unregistered", prompt.split("Customer context")[-1].split(".")[0])

    def test_a_three_bike_owner_prompt_forbids_assuming_which_bike(self):
        prompt = self._prompt(self.resolver, "919700000001")
        self.assertIn("owns 3 bikes", prompt)
        self.assertIn("Do NOT assume", prompt)

    def test_a_bike_with_no_purchase_date_never_gets_a_coverage_claim(self):
        prompt = self._prompt(self.resolver, "919700000002")
        self.assertIn("Coverage: UNKNOWN", prompt)
        self.assertIn("invoice", prompt)
        self.assertNotIn("in warranty", prompt)


if __name__ == "__main__":
    unittest.main()
