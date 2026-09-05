"""Pins the construction order api.py and cli.py must use: build the catalogue
first, pass its topics into build_registry, then hand both the registry and the
catalogue to Runtime. Getting this order wrong is how a published YAML bot's
topic silently never reaches the search_knowledge enum in production."""

import importlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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

# A real *.yaml on disk, for the tests below that need bots.BOTS_DIR to
# actually resolve to a directory holding a published bot — the module-level
# BRAKES dict above is only usable with the in-memory BotCatalogue(...)
# constructor, not with BotCatalogue.load()/BOTS_DIR.
BRAKES_YAML = """\
name: brakes_support
persona: customer
topic: brakes
keywords:
  - brake
  - braking
tools:
  - lookup_warranty_record
  - search_knowledge
  - create_support_ticket
prompt: |
  You are the brake support assistant for EMotorad.
"""


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

    def test_a_real_published_bot_directory_reaches_the_deployed_api_module(self):
        # The two tests above never actually exercise BOTS_DIR/BotCatalogue.load(),
        # so neither would fail if api.py's module-level wiring regressed back
        # to building the registry before the catalogue: this repo's real
        # bots/ directory holds only a README, so catalogue.topics() is just
        # the two built-ins ["battery", "motor"] either way. This test points
        # bots.BOTS_DIR at a temp directory with one real brakes_support.yaml
        # and reloads api.py while that patch is active, so it fails against
        # the pre-fix construction order.
        os.environ.setdefault("EMOTORAD_AI_MODE", "offline")
        import emotorad_ai.api as api
        import emotorad_ai.bots as bots

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "brakes_support.yaml").write_text(BRAKES_YAML, encoding="utf-8")
            try:
                with patch.object(bots, "BOTS_DIR", Path(tmpdir)):
                    importlib.reload(api)

                    schema = api.registry.schemas_for([SEARCH_KNOWLEDGE])[0]
                    enum = schema["input_schema"]["properties"]["topic"]["enum"]
                    self.assertIn("brakes", enum)
                    self.assertIn("brakes_support", api.runtime.agents)
            finally:
                # Reload again outside the patch so every other test in the
                # suite sees api.py wired against the real bots/ directory.
                importlib.reload(api)

    def test_the_cli_entrypoint_wires_a_real_published_bot_into_its_own_runtime(self):
        # cli.py builds its runtime freshly inside main() on every call (unlike
        # api.py's module-level construction), so this needs no reload — just
        # BOTS_DIR patched for the duration of the call.
        import emotorad_ai.bots as bots
        from emotorad_ai import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "brakes_support.yaml").write_text(BRAKES_YAML, encoding="utf-8")
            log_path = str(Path(tmpdir) / "conversations.jsonl")
            with patch.object(bots, "BOTS_DIR", Path(tmpdir)), patch.dict(
                os.environ, {"EMOTORAD_AI_LOG_PATH": log_path}
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    cli.main(["--offline", "brake", "squeaks"])

        self.assertIn("handled_by=brakes_support", out.getvalue())


if __name__ == "__main__":
    unittest.main()
