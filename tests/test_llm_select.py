"""Two clients, one request shape, one response mapping — and a factory that
turns EMOTORAD_AI_MODE into a client or a named startup error."""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.llm import (
    MODES,
    AnthropicClaude,
    BedrockClaude,
    LLMConfigError,
    OfflinePlanner,
    select_llm,
)


class _Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if not (exclude_none and v is None)}


class _Usage:
    def model_dump(self):
        return {"input_tokens": 12, "output_tokens": 7}


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _Client:
    def __init__(self, response):
        self.messages = _Messages(response)


TOOL = _Response(
    [_Block(type="text", text="Checking."), _Block(type="tool_use", id="tu_1", name="search_knowledge", input={"query": "x"})],
    stop_reason="tool_use",
)


class AnthropicClaudeTests(unittest.TestCase):
    def test_it_sends_the_same_request_shape_as_bedrock(self):
        client = _Client(TOOL)
        llm = AnthropicClaude(Settings(effort="low", max_tokens=4096), api_key="k", model="claude-sonnet-5", client=client)
        llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}])
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-5")
        self.assertEqual(call["max_tokens"], 4096)
        self.assertEqual(call["system"], "sys")
        self.assertEqual(call["tools"], [{"name": "t"}])
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(call["output_config"], {"effort": "low"})

    def test_the_model_defaults_to_settings(self):
        llm = AnthropicClaude(Settings(model="claude-opus-5"), api_key="k", client=_Client(TOOL))
        self.assertEqual(llm.model, "claude-opus-5")

    def test_responses_map_identically_to_bedrock(self):
        a = AnthropicClaude(Settings(), api_key="k", client=_Client(TOOL)).create(system="s", messages=[], tools=[])
        b = BedrockClaude(Settings(), client=_Client(TOOL)).create(system="s", messages=[], tools=[])
        self.assertEqual(a, b)
        self.assertTrue(a.wants_tools)
        self.assertEqual(a.tool_uses[0].arguments, {"query": "x"})
        self.assertEqual(a.text, "Checking.")
        self.assertEqual(a.usage, {"input_tokens": 12, "output_tokens": 7})


class SelectTests(unittest.TestCase):
    def test_modes_are_the_three_the_deploy_can_name(self):
        self.assertEqual(MODES, ("offline", "anthropic", "bedrock"))

    def test_offline_needs_nothing(self):
        self.assertIsInstance(select_llm("offline", Settings(), environ={}), OfflinePlanner)

    def test_anthropic_needs_a_key_in_the_environment(self):
        llm = select_llm("anthropic", Settings(), environ={"ANTHROPIC_API_KEY": "k"}, client=_Client(TOOL))
        self.assertIsInstance(llm, AnthropicClaude)

    def test_anthropic_without_a_key_fails_with_a_named_error(self):
        with self.assertRaises(LLMConfigError) as caught:
            select_llm("anthropic", Settings(), environ={})
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))

    def test_a_whitespace_key_is_treated_as_missing(self):
        # A secret set to " " must fail here, at startup, not on the first customer request.
        with self.assertRaises(LLMConfigError):
            select_llm("anthropic", Settings(), environ={"ANTHROPIC_API_KEY": "   "})

    def test_bedrock_uses_the_role_and_takes_no_key(self):
        self.assertIsInstance(select_llm("bedrock", Settings(), environ={}, client=_Client(TOOL)), BedrockClaude)

    def test_an_unknown_mode_is_a_named_error(self):
        with self.assertRaises(LLMConfigError) as caught:
            select_llm("cloud", Settings(), environ={})
        self.assertIn("cloud", str(caught.exception))

    def test_each_mode_gets_its_own_default_model_id(self):
        a = select_llm("anthropic", Settings(), environ={"ANTHROPIC_API_KEY": "k"}, client=_Client(TOOL))
        b = select_llm("bedrock", Settings(), environ={}, client=_Client(TOOL))
        self.assertEqual(a.model, "claude-opus-5")
        self.assertEqual(b.settings.model, "anthropic.claude-opus-5")

    def test_emotorad_ai_model_overrides_the_default_for_the_active_mode(self):
        a = select_llm("anthropic", Settings(), environ={"ANTHROPIC_API_KEY": "k", "EMOTORAD_AI_MODEL": "claude-sonnet-5"}, client=_Client(TOOL))
        self.assertEqual(a.model, "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
