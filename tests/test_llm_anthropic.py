"""The playground's model client: the first-party API instead of Bedrock, with
the identical request shape, so a bot tuned in the playground behaves the same
once it is served from Bedrock."""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.llm import DEFAULT_ANTHROPIC_MODEL, AnthropicClaude, BedrockClaude


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


TEXT = _Response([_Block(type="text", text="Try another socket.")])
TOOL = _Response(
    [_Block(type="text", text="Checking."), _Block(type="tool_use", id="tu_1", name="search_knowledge", input={"query": "x"})],
    stop_reason="tool_use",
)


class AnthropicClaudeTests(unittest.TestCase):
    def test_it_sends_the_same_request_shape_as_bedrock(self):
        client = _Client(TEXT)
        llm = AnthropicClaude(Settings(effort="low"), api_key="k", model="claude-sonnet-5", client=client)
        llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}])
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-5")
        self.assertEqual(call["system"], "sys")
        self.assertEqual(call["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(call["tools"], [{"name": "t"}])
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(call["output_config"], {"effort": "low"})

    def test_the_default_model_is_opus_5(self):
        llm = AnthropicClaude(Settings(), api_key="k", client=_Client(TEXT))
        llm.create(system="s", messages=[], tools=[])
        self.assertEqual(DEFAULT_ANTHROPIC_MODEL, "claude-opus-5")
        self.assertEqual(llm.model, "claude-opus-5")

    def test_text_and_tool_calls_map_like_bedrock(self):
        anthropic_llm = AnthropicClaude(Settings(), api_key="k", client=_Client(TOOL))
        bedrock_llm = BedrockClaude(Settings(), client=_Client(TOOL))
        a = anthropic_llm.create(system="s", messages=[], tools=[])
        b = bedrock_llm.create(system="s", messages=[], tools=[])
        self.assertEqual(a, b)
        self.assertTrue(a.wants_tools)
        self.assertEqual(a.tool_uses[0].name, "search_knowledge")
        self.assertEqual(a.tool_uses[0].arguments, {"query": "x"})
        self.assertEqual(a.text, "Checking.")
        self.assertEqual(a.usage, {"input_tokens": 12, "output_tokens": 7})


if __name__ == "__main__":
    unittest.main()
