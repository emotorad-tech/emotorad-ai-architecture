import json
import unittest

from emotorad_ai.llm import OpenRouterChat, from_openai_response, to_openai_messages, to_openai_tools
from emotorad_ai.openrouter import CHAT_PATH, OpenRouterBadResponse
from tests.fake_http import FakeTransport

HISTORY = [
    {"role": "user", "content": "my battery won't charge"},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "private", "signature": "sig"},
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_1", "name": "search_knowledge", "input": {"query": "won't charge"}},
            {"type": "tool_use", "id": "toolu_2", "name": "lookup_warranty_record", "input": {}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"data\": {}}", "is_error": False},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "{\"error\": {}}", "is_error": True},
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "Try another socket."}]},
]


def reply(content="", tool_calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "gen-1", "choices": [{"finish_reason": finish, "message": message}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.00001}}


class ToOpenAIMessagesTests(unittest.TestCase):
    def test_history_translates_block_for_block(self):
        messages = to_openai_messages("SYS", HISTORY)
        self.assertEqual(messages[0], {"role": "system", "content": "SYS"})
        self.assertEqual(messages[1], {"role": "user", "content": "my battery won't charge"})
        assistant = messages[2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"], "Let me check.")
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["toolu_1", "toolu_2"])
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"query": "won't charge"})
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "search_knowledge")
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "toolu_1", "content": "{\"data\": {}}"})
        self.assertEqual(messages[4], {"role": "tool", "tool_call_id": "toolu_2", "content": "{\"error\": {}}"})
        self.assertEqual(messages[5], {"role": "assistant", "content": "Try another socket."})

    def test_thinking_blocks_never_leave(self):
        self.assertNotIn("private", json.dumps(to_openai_messages("SYS", HISTORY)))

    def test_an_assistant_turn_with_only_tool_calls_has_null_content(self):
        history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}]
        self.assertIsNone(to_openai_messages("S", history)[1]["content"])

    def test_the_system_prompt_is_marked_for_caching_when_asked(self):
        system = to_openai_messages("SYS", [], cache_system=True)[0]
        self.assertEqual(system["content"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])


class ToOpenAIToolsTests(unittest.TestCase):
    def test_input_schema_becomes_function_parameters(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        tools = to_openai_tools([{"name": "search", "description": "d", "input_schema": schema}])
        self.assertEqual(tools, [{"type": "function", "function": {"name": "search", "description": "d", "parameters": schema}}])


class FromOpenAIResponseTests(unittest.TestCase):
    def test_parallel_tool_calls_become_tool_use_blocks(self):
        body = reply(content="", finish="tool_calls", tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "lookup_warranty_record", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "search_knowledge", "arguments": "{\"query\":\"x\"}"}},
        ])
        response = from_openai_response(body)
        self.assertEqual(response.stop_reason, "tool_use")
        self.assertTrue(response.wants_tools)
        self.assertEqual([t.name for t in response.tool_uses], ["lookup_warranty_record", "search_knowledge"])
        self.assertEqual(response.tool_uses[1].arguments, {"query": "x"})
        self.assertEqual([b["type"] for b in response.api_content], ["tool_use", "tool_use"])

    def test_tool_calls_decide_the_stop_reason_even_when_finish_says_stop(self):
        body = reply(finish="stop", tool_calls=[{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{}"}}])
        self.assertEqual(from_openai_response(body).stop_reason, "tool_use")

    def test_arguments_that_are_not_json_become_an_empty_dict(self):
        body = reply(finish="tool_calls", tool_calls=[{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{not json"}}])
        self.assertEqual(from_openai_response(body).tool_uses[0].arguments, {})

    def test_text_and_finish_reasons_map(self):
        self.assertEqual(from_openai_response(reply("Hi")).stop_reason, "end_turn")
        self.assertEqual(from_openai_response(reply("Hi")).text, "Hi")
        self.assertEqual(from_openai_response(reply("Hi", finish="length")).stop_reason, "max_tokens")
        self.assertEqual(from_openai_response(reply("Hi")).api_content, [{"type": "text", "text": "Hi"}])

    def test_usage_carries_tokens_and_cost(self):
        usage = from_openai_response(reply("Hi")).usage
        self.assertEqual(usage, {"input_tokens": 100, "output_tokens": 20, "cost": 0.00001})

    def test_no_choices_is_a_bad_response(self):
        with self.assertRaises(OpenRouterBadResponse):
            from_openai_response({"id": "x", "choices": []})


class OpenRouterChatTests(unittest.TestCase):
    def test_create_posts_the_translated_request(self):
        transport = FakeTransport([reply("Hello")])
        chat = OpenRouterChat("deepseek/deepseek-v4-flash-0731", transport, max_tokens=512)
        response = chat.create("SYS", [{"role": "user", "content": "hi"}], [
            {"name": "search", "description": "d", "input_schema": {"type": "object", "properties": {}}}
        ])
        self.assertEqual(response.text, "Hello")
        call = transport.calls[0]
        self.assertEqual(call["path"], CHAT_PATH)
        body = call["body"]
        self.assertEqual(body["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["usage"], {"include": True})
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["tools"][0]["function"]["name"], "search")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})

    def test_no_tools_key_when_there_are_no_tools(self):
        transport = FakeTransport([reply("Hello")])
        OpenRouterChat("m", transport).create("S", [], [])
        self.assertNotIn("tools", transport.calls[0]["body"])

    def test_anthropic_models_cache_the_system_prompt_and_others_do_not(self):
        transport = FakeTransport([reply("a"), reply("b")])
        OpenRouterChat("anthropic/claude-haiku-4.5", transport).create("S", [], [])
        OpenRouterChat("deepseek/deepseek-v4-flash-0731", transport).create("S", [], [])
        self.assertIsInstance(transport.calls[0]["body"]["messages"][0]["content"], list)
        self.assertEqual(transport.calls[1]["body"]["messages"][0]["content"], "S")

    def test_zdr_off_sends_no_provider_preferences(self):
        transport = FakeTransport([reply("a")])
        OpenRouterChat("m", transport, zdr=False).create("S", [], [])
        self.assertNotIn("provider", transport.calls[0]["body"])


if __name__ == "__main__":
    unittest.main()
