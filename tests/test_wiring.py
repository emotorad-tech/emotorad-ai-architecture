import os
import unittest
from unittest import mock

from emotorad_ai.cli import resolve_mode
from emotorad_ai.config import Settings
from emotorad_ai.jev import JevClient
from emotorad_ai.llm import OfflinePlanner, OpenRouterChat
from emotorad_ai.openrouter import API_KEY_ENV, OpenRouterConfigError
from emotorad_ai.wiring import build_models
from tests.fake_http import FakeTransport


class BuildModelsTests(unittest.TestCase):
    def test_offline_has_no_jev_and_no_narrow_model(self):
        models = build_models(Settings(mode="offline"))
        self.assertIsInstance(models.llm, OfflinePlanner)
        self.assertIsNone(models.jev)
        self.assertIsNone(models.narrow_llm)

    def test_openrouter_builds_jev_and_both_reply_models_on_one_transport(self):
        # Two different models: the defaults name the same one, so a swap passed.
        settings = Settings(mode="openrouter", fallback_model="anthropic/claude-haiku-4.5",
                            narrow_model="deepseek/deepseek-v4-flash-0731")
        models = build_models(settings, transport=FakeTransport())
        self.assertIsInstance(models.jev, JevClient)
        self.assertEqual(models.jev.model, settings.jev_model)
        self.assertEqual(models.jev.timeout, settings.jev_timeout)
        self.assertIsInstance(models.llm, OpenRouterChat)
        self.assertEqual(models.llm.model, settings.fallback_model)
        self.assertEqual(models.narrow_llm.model, settings.narrow_model)

    def test_openrouter_without_a_key_fails_loudly(self):
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}):
            with self.assertRaises(OpenRouterConfigError):
                build_models(Settings(mode="openrouter"))


class CliModeTests(unittest.TestCase):
    def test_offline_flag_wins(self):
        self.assertEqual(resolve_mode(True, "openrouter", {"EMOTORAD_AI_MODE": "bedrock"}), "offline")

    def test_mode_flag_beats_the_environment(self):
        self.assertEqual(resolve_mode(False, "openrouter", {"EMOTORAD_AI_MODE": "bedrock"}), "openrouter")

    def test_the_environment_is_used_when_no_flag_is_given(self):
        self.assertEqual(resolve_mode(False, None, {"EMOTORAD_AI_MODE": "openrouter"}), "openrouter")

    def test_with_nothing_set_the_cli_still_talks_to_bedrock_as_before(self):
        self.assertEqual(resolve_mode(False, None, {}), "bedrock")


if __name__ == "__main__":
    unittest.main()
