"""The health check is what the deploy workflow reads. It has to say which model
path is live and whether the config store was used, so a container that started
without its secret is visible in the workflow log rather than in a customer chat."""

import importlib
import os
import unittest
from unittest import mock


def fresh_api(env):
    with mock.patch.dict(os.environ, env, clear=False):
        import emotorad_ai.api as api

        return importlib.reload(api)


class HealthTests(unittest.TestCase):
    def test_offline_reports_no_secret(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""})
        self.assertEqual(
            api.health(),
            {"status": "ok", "mode": "offline", "secrets": "not configured", "media": "not configured"},
        )

    def test_registry_offers_the_guide_media_tool(self):
        # The playground wires guide_media into build_registry so the model can
        # send pictures. Production has to do the same, or a deployed agent
        # can be asked for a picture and simply has no tool to send one with.
        api = fresh_api({"EMOTORAD_AI_MODE": "offline"})
        self.assertIn("send_guide_media", api.registry.specs)

    def test_a_secret_id_reports_loaded(self):
        api = fresh_api(
            {
                "EMOTORAD_AI_MODE": "offline",
                "EMOTORAD_AI_SECRET_ID": "/emotorad/stage/ai/app",
                "EMOTORAD_AI_CONFIG_EXPORTED": "5",
            }
        )
        self.assertEqual(api.health()["secrets"], "loaded")

    def test_a_secret_that_exported_nothing_reports_empty(self):
        api = fresh_api(
            {
                "EMOTORAD_AI_MODE": "offline",
                "EMOTORAD_AI_SECRET_ID": "/emotorad/stage/ai/app",
                "EMOTORAD_AI_CONFIG_EXPORTED": "0",
            }
        )
        self.assertEqual(api.health()["secrets"], "empty")

    def test_no_secret_id_with_export_count_zero_reports_not_configured(self):
        api = fresh_api(
            {
                "EMOTORAD_AI_MODE": "offline",
                "EMOTORAD_AI_SECRET_ID": "",
                "EMOTORAD_AI_CONFIG_EXPORTED": "0",
            }
        )
        self.assertEqual(api.health()["secrets"], "not configured")

    def test_anthropic_mode_without_a_key_fails_at_import(self):
        from emotorad_ai.llm import LLMConfigError

        with self.assertRaises(LLMConfigError):
            fresh_api({"EMOTORAD_AI_MODE": "anthropic", "ANTHROPIC_API_KEY": ""})

    @classmethod
    def tearDownClass(cls):
        fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""})


if __name__ == "__main__":
    unittest.main()
