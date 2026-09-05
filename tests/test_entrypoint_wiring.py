"""Pins the construction order api.py and cli.py must use: build the catalogue
first, pass its topics into build_registry, then hand both the registry and the
catalogue to Runtime. Getting this order wrong is how a published YAML bot's
topic silently never reaches the search_knowledge enum in production."""

import os
import unittest

from emotorad_ai.bots import BotCatalogue, BotSpec, builtin_specs, spec_from_dict
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import OfflinePlanner
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

BRAKES = {
    "name": "brakes_support",
    "persona": "customer",
    "topic": "brakes",
    "keywords": ["brake", "braking", "ब्रेक"],
    "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
    "prompt": "You are the brake support assistant for EMotorad.\n",
}


class EntrypointWiringTests(unittest.TestCase):
    def test_a_published_yaml_topic_reaches_the_search_knowledge_enum(self):
        # Mirrors api.py's construction order exactly: catalogue, then a
        # registry built from that catalogue's topics, then Runtime given both.
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(BRAKES, "published")])
        registry = build_registry(topics=catalogue.topics())
        runtime = Runtime(
            settings=Settings(log_to_stdout=False, log_path=None),
            registry=registry,
            llm=OfflinePlanner(),
            log=EventLog(path=None, to_stdout=False),
            resolver=IdentityResolver(registry),
            catalogue=catalogue,
        )

        schema = runtime.registry.schemas_for([SEARCH_KNOWLEDGE])[0]
        enum = schema["input_schema"]["properties"]["topic"]["enum"]
        self.assertIn("brakes", enum)

    def test_the_deployed_api_module_wires_its_own_registry_and_catalogue_together(self):
        os.environ.setdefault("EMOTORAD_AI_MODE", "offline")
        from emotorad_ai import api

        schema = api.registry.schemas_for([SEARCH_KNOWLEDGE])[0]
        enum = schema["input_schema"]["properties"]["topic"]["enum"]
        self.assertEqual(enum, api.runtime.catalogue.topics())


if __name__ == "__main__":
    unittest.main()
