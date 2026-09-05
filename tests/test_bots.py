"""A bot is configuration. These tests pin what the configuration may say and
what the catalogue derives from it, so runtime, triage and the tool enum never
need a second list."""

import tempfile
import unittest
from pathlib import Path

from emotorad_ai.agents import battery_support, blocks
from emotorad_ai.agents.generic import definition_from_spec
from emotorad_ai.bots import (
    BUILTIN,
    DRAFT,
    PUBLISHED,
    BotCatalogue,
    BotSpec,
    BotSpecError,
    builtin_specs,
    load_specs,
    spec_from_dict,
)
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.tools.mocks import build_registry

BRAKES = {
    "name": "brakes_support",
    "persona": "customer",
    "topic": "brakes",
    "keywords": ["brake", "braking", "ब्रेक"],
    "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
    "prompt": "You are the brake support assistant for EMotorad.\n",
}

STOCK = {
    "name": "dealer_stock",
    "persona": "dealer",
    "topic": "stock",
    "keywords": ["stock", "availability"],
    "tools": ["get_dealer_account", "create_support_ticket"],
    "prompt": "You answer dealer stock questions.\n",
}


class SpecValidationTests(unittest.TestCase):
    def test_a_well_formed_spec_loads(self):
        spec = spec_from_dict(BRAKES, DRAFT)
        self.assertEqual(spec.name, "brakes_support")
        self.assertEqual(spec.keywords, ("brake", "braking", "ब्रेक"))
        self.assertEqual(spec.source, DRAFT)

    def test_name_must_be_snake_case(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, name="Brakes Support"), DRAFT)

    def test_persona_must_be_customer_or_dealer(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, persona="internal"), DRAFT)

    def test_prompt_must_not_be_empty(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, prompt="  "), DRAFT)

    def test_a_dealer_bot_cannot_reach_the_customer_warranty_lookup(self):
        # Dealers register most warranties under their own number; that tool
        # would hand them dozens of other people's bikes. Withheld here, not
        # merely discouraged in a prompt.
        with self.assertRaises(BotSpecError) as caught:
            spec_from_dict(dict(STOCK, tools=["lookup_warranty_record"]), DRAFT)
        self.assertIn("lookup_warranty_record", str(caught.exception))

    def test_a_customer_bot_cannot_place_dealer_orders(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, tools=["place_order"]), DRAFT)

    def test_to_dict_round_trips(self):
        spec = spec_from_dict(BRAKES, DRAFT)
        self.assertEqual(spec_from_dict(spec.to_dict(), DRAFT), spec)


class CatalogueTests(unittest.TestCase):
    def test_built_ins_are_present_with_their_definitions(self):
        catalogue = BotCatalogue(builtin_specs())
        names = [s.name for s in catalogue.specs]
        self.assertEqual(
            names, ["battery_support", "motor_support", "late_warranty_registration", "dealer_orders"]
        )
        self.assertIs(catalogue.definitions()["battery_support"], battery_support.DEFINITION)
        self.assertTrue(all(s.source == BUILTIN for s in catalogue.specs))

    def test_derived_tables_for_the_built_ins_match_today(self):
        catalogue = BotCatalogue(builtin_specs())
        self.assertEqual(catalogue.topic_agents("customer"), {"battery": "battery_support", "motor": "motor_support"})
        self.assertIs(catalogue.keywords("customer")["battery"], battery_support.KEYWORDS)
        self.assertEqual(catalogue.topics(), ["battery", "motor"])
        self.assertEqual(catalogue.supported_summary("customer"), "battery and motor problems")
        self.assertEqual(catalogue.topic_agents("dealer"), {})

    def test_a_yaml_bot_is_derived_into_every_table(self):
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(BRAKES, DRAFT), spec_from_dict(STOCK, DRAFT)])
        self.assertEqual(catalogue.topic_agents("customer")["brakes"], "brakes_support")
        self.assertEqual(catalogue.keywords("customer")["brakes"], ("brake", "braking", "ब्रेक"))
        self.assertEqual(catalogue.topics(), ["battery", "brakes", "motor"])
        self.assertEqual(catalogue.supported_summary("customer"), "battery, motor and brakes problems")
        self.assertEqual(catalogue.topic_agents("dealer"), {"stock": "dealer_stock"})
        self.assertIn("brakes_support", catalogue.definitions())

    def test_topics_only_include_bots_that_search_knowledge(self):
        # dealer_orders has a topic but no search tool, so it is not an enum value.
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(STOCK, DRAFT)])
        self.assertNotIn("stock", catalogue.topics())
        self.assertNotIn("order", catalogue.topics())

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(BotSpecError):
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, name="battery_support"), DRAFT)])

    def test_duplicate_topics_are_rejected(self):
        with self.assertRaises(BotSpecError):
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, topic="battery"), DRAFT)])

    def test_overlapping_keywords_within_a_persona_are_rejected(self):
        # An overlap makes classify_issue return None for both — neither bot is
        # ever reached, and nothing says why.
        with self.assertRaises(BotSpecError) as caught:
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, keywords=["brake", "battery"]), DRAFT)])
        self.assertIn("battery", str(caught.exception))

    def test_the_same_keyword_across_personas_is_fine(self):
        BotCatalogue(builtin_specs() + [spec_from_dict(dict(STOCK, keywords=["battery"]), DRAFT)])

    def test_a_keyword_that_contains_a_built_in_keyword_is_rejected(self):
        # "charger cable" contains battery_support's "charge", so
        # classify_issue("my charger cable is frayed") would match both
        # bots' tables and return None for both — silently unreachable.
        with self.assertRaises(BotSpecError) as caught:
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, keywords=["charger cable"]), DRAFT)])
        self.assertIn("charge", str(caught.exception))

    def test_a_keyword_contained_by_a_built_in_keyword_is_also_rejected(self):
        # The reverse direction: "pedal" is a substring of motor_support's
        # "pedal assist", so it is rejected too, not just the longer-in-shorter
        # case above.
        with self.assertRaises(BotSpecError) as caught:
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, keywords=["pedal"]), DRAFT)])
        self.assertIn("pedal assist", str(caught.exception))

    def test_validate_rejects_a_tool_the_registry_does_not_have(self):
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, tools=["get_battery_diagnostics"]), DRAFT)])
        with self.assertRaises(BotSpecError):
            catalogue.validate(build_registry(diagnostics_available=False))
        catalogue.validate(build_registry(diagnostics_available=True))

    def test_validate_does_not_police_built_ins(self):
        # battery_support lists get_battery_diagnostics on purpose; Agent.run
        # filters it out when telematics do not exist.
        BotCatalogue(builtin_specs()).validate(build_registry(diagnostics_available=False))

    def test_get_raises_for_an_unknown_bot(self):
        with self.assertRaises(KeyError):
            BotCatalogue(builtin_specs()).get("nope")


class LoadingTests(unittest.TestCase):
    def test_specs_load_from_yaml_files_and_a_missing_directory_is_empty(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(load_specs(root / "missing", PUBLISHED), [])
            (root / "brakes_support.yaml").write_text(yaml.safe_dump(BRAKES), encoding="utf-8")
            specs = load_specs(root, PUBLISHED)
            self.assertEqual([s.name for s in specs], ["brakes_support"])
            self.assertEqual(specs[0].source, PUBLISHED)
            self.assertEqual(specs[0].path, root / "brakes_support.yaml")

    def test_catalogue_load_combines_published_and_drafts(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            published = Path(tmp) / "bots"
            drafts = Path(tmp) / "drafts"
            (drafts / "bots").mkdir(parents=True)
            published.mkdir()
            (published / "brakes_support.yaml").write_text(yaml.safe_dump(BRAKES), encoding="utf-8")
            (drafts / "bots" / "dealer_stock.yaml").write_text(yaml.safe_dump(STOCK), encoding="utf-8")
            catalogue = BotCatalogue.load(published_dir=published, drafts_dir=drafts)
            sources = {s.name: s.source for s in catalogue.specs}
            self.assertEqual(sources["brakes_support"], PUBLISHED)
            self.assertEqual(sources["dealer_stock"], DRAFT)
            self.assertEqual(sources["battery_support"], BUILTIN)


class GenericDefinitionTests(unittest.TestCase):
    def _message(self, pill=None):
        return InboundMessage(
            "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", "hi",
            entry_metadata={"pill_clicked": pill} if pill else {},
        )

    def test_a_customer_spec_gets_the_shared_customer_blocks(self):
        definition = definition_from_spec(spec_from_dict(BRAKES, DRAFT))
        self.assertEqual(definition.name, "brakes_support")
        self.assertEqual(definition.tool_names, tuple(BRAKES["tools"]))
        resolved = ResolvedIdentity(
            persona="customer", method="verified",
            identity=Identity(strength=VERIFIED, phone="+919876543210"),
            profile={"name": "Ananya Rao"},
            bikes=[{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": True,
                    "months_remaining": 10, "term_months": 24}],
        )
        prompt = definition.build_system_prompt(self._message("brake_issue"), resolved, "Visited /brakes")
        self.assertTrue(prompt.startswith(BRAKES["prompt"]))
        self.assertIn("Ananya Rao", prompt)
        self.assertIn("EMXP2025004417", prompt)
        self.assertIn("Visited /brakes", prompt)
        self.assertIn("'brake_issue'", prompt)

    def test_a_dealer_spec_gets_the_account_block(self):
        definition = definition_from_spec(spec_from_dict(STOCK, DRAFT))
        resolved = ResolvedIdentity(
            persona="dealer", method="verified",
            identity=Identity(strength=VERIFIED, phone="+919000000001"),
            profile={"dealer_id": "DLR-PUN-014", "name": "Royal Cycle Stores", "city": "Pune",
                     "credit_limit": 500000, "credit_used": 380000, "payment_terms_days": 30,
                     "overdue_amount": 0, "status": "active"},
        )
        prompt = definition.build_system_prompt(self._message(), resolved, "")
        self.assertIn("Royal Cycle Stores", prompt)
        self.assertIn("Credit available: 120000", prompt)

    def test_the_blocks_are_shared_not_copied(self):
        definition = definition_from_spec(spec_from_dict(BRAKES, DRAFT))
        self.assertIs(definition.build_system_prompt.__globals__["_facts_block"], blocks._facts_block)


if __name__ == "__main__":
    unittest.main()
