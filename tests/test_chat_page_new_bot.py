"""The /chat page on the new chatbot: Jev routing, the OpenRouter models and a
photo the customer attaches.

Before 2026-09-29 the page pinned every message to `battery_support`. A pin is
followed with no Jev decision and no narrow path (test_turn_bookkeeping), so a
tester on the page only ever reached the full agent, never the bot that staging
now runs. The page now pins only when its own URL asks.
"""

import base64
import importlib.util
import io
import pathlib
import re
import unittest
from datetime import date

from PIL import Image

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import OpenRouterChat, ScriptedClaude
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from tests.fake_http import FakeTransport

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAT = ROOT / "web" / "emotorad-support-chat-dev.html"
TODAY = date(2026, 7, 28)
NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}


def jpeg_data_url(width=64, height=48):
    """What the page sends: a JPEG re-encoded in the browser, inline. A real
    one, because a malformed data URL is dropped before the model sees it."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 40, 40)).save(out, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


PHOTO = jpeg_data_url()


def openrouter_reply(content="", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "gen-1", "model": "anthropic/claude-haiku-4.5",
            "choices": [{"finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 40, "cost": 0.003}}


TICKET_CALL = openrouter_reply(tool_calls=[{
    "id": "call_1", "type": "function",
    "function": {"name": "create_support_ticket", "arguments": (
        '{"category": "battery_charging", "severity": "normal", '
        '"description": "Charger LED stays off; photo sent.", "idempotency_key": "k1"}')},
}])


def runtime_with_openrouter_narrow(transport):
    registry = build_registry(today=TODAY)
    return Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=ScriptedClaude([]),
        narrow_llm=OpenRouterChat("anthropic/claude-haiku-4.5", transport),
        jev=ScriptedJev([JevDecision(answers=NARROW)]),
        standard_responses=[],
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
        conversations=InMemoryConversationStore(),
    )


def from_the_page(rt, text, attachments=()):
    """The body post_message builds from what the page sends, with no pin."""
    return rt.handle(WebsiteChatAdapter(rt.resolver).to_message({
        "conversation_id": "c1", "session_token": "sess-ananya", "text": text,
        "attachments": list(attachments),
    }))


def user_parts(call):
    """The content parts of the customer's message in one OpenRouter request."""
    users = [m for m in call["body"]["messages"] if m["role"] == "user"]
    content = users[-1]["content"]
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


class PhotoToOpenRouterTests(unittest.TestCase):
    def test_the_photo_reaches_the_narrow_model_as_an_image_beside_the_text(self):
        transport = FakeTransport([openrouter_reply("Thanks, I can see the charger light is off.")])
        rt = runtime_with_openrouter_narrow(transport)
        answer = from_the_page(rt, "my battery won't charge, here is the charger",
                               [{"kind": "image", "url": PHOTO}])
        self.assertEqual(answer.handled_by, "narrow_support")
        parts = user_parts(transport.calls[0])
        self.assertIn({"type": "image_url", "image_url": {"url": PHOTO}}, parts)
        self.assertTrue(any(p.get("type") == "text" and "won't charge" in p.get("text", "") for p in parts))

    def test_with_the_photo_the_ticket_is_raised_and_its_reference_reaches_the_customer(self):
        transport = FakeTransport([TICKET_CALL, openrouter_reply("I've raised ticket EM-00001 for you.")])
        rt = runtime_with_openrouter_narrow(transport)
        answer = from_the_page(rt, "still dead after all the steps, photo attached",
                               [{"kind": "image", "url": PHOTO}])
        self.assertEqual(list(rt.registry.tickets.tickets), ["EM-00001"])
        self.assertEqual(answer.ticket_id, "EM-00001")
        self.assertIn("EM-00001", answer.text)

    def test_the_same_turn_without_the_photo_raises_no_ticket(self):
        # The pair to the test above: the only difference is the photo.
        transport = FakeTransport([TICKET_CALL, openrouter_reply("I've raised ticket EM-00001 for you.")])
        rt = runtime_with_openrouter_narrow(transport)
        answer = from_the_page(rt, "still dead after all the steps")
        self.assertEqual(rt.registry.tickets.tickets, {})
        self.assertIsNone(answer.ticket_id)
        self.assertNotIn("EM-00001", answer.text)


class PageRoutingTests(unittest.TestCase):
    def setUp(self):
        self.html = CHAT.read_text(encoding="utf-8")

    def test_no_agent_is_pinned_unless_the_url_asks(self):
        self.assertNotIn('agent: "battery_support"', self.html)
        self.assertRegex(self.html, r"URLSearchParams\(\s*(window\.)?location\.search\s*\)")
        self.assertRegex(self.html, r'\.get\(\s*"agent"\s*\)')

    def test_the_pin_is_sent_only_when_there_is_one(self):
        # An empty or absent `agent` must not reach the server as a pin.
        self.assertRegex(self.html, r"agent:\s*PINNED_AGENT\s*\|\|\s*null")

    def test_a_server_without_video_storage_asks_for_a_photo_instead(self):
        # /uploads answers 503 when EMOTORAD_AI_MEDIA_BUCKET is unset, as on a
        # laptop. "Please try again" there sends the tester round in a loop.
        handler = self.html[self.html.index("function failed(err)"):]
        handler = handler[:handler.index("\n  }\n")]
        self.assertRegex(handler, r"err\.status\s*===\s*503")
        self.assertIn("send a photo instead", handler)

    def test_the_route_that_answered_is_shown_only_in_debug(self):
        self.assertRegex(self.html, r'\.get\(\s*"debug"\s*\)\s*===\s*"1"')
        self.assertIn("reply.handled_by", self.html)
        self.assertRegex(self.html, r"if\s*\(\s*DEBUG\b")


def load_launcher():
    spec = importlib.util.spec_from_file_location("chat_local", ROOT / "scripts" / "chat_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.launcher = load_launcher()

    def test_it_defaults_to_the_staging_configuration(self):
        env = self.launcher.server_env({}, mode="openrouter", store="mongodb")
        self.assertEqual(env["EMOTORAD_AI_MODE"], "openrouter")
        self.assertEqual(env["EMOTORAD_STORE"], "mongodb")
        self.assertEqual(env["EMOTORAD_AI_DEV_CODES"], "1")
        self.assertIn("src", env["PYTHONPATH"])

    def test_a_playground_login_already_set_is_kept(self):
        env = self.launcher.server_env({"EMOTORAD_AI_PLAYGROUND_USER": "u1", "EMOTORAD_AI_PLAYGROUND_PASSWORD": "p1"},
                                       mode="openrouter", store="mongodb")
        self.assertEqual((env["EMOTORAD_AI_PLAYGROUND_USER"], env["EMOTORAD_AI_PLAYGROUND_PASSWORD"]), ("u1", "p1"))

    def test_without_one_a_local_login_is_set_so_the_code_panel_works(self):
        env = self.launcher.server_env({}, mode="offline", store="memory")
        self.assertTrue(env["EMOTORAD_AI_PLAYGROUND_USER"])
        self.assertTrue(env["EMOTORAD_AI_PLAYGROUND_PASSWORD"])

    def test_the_environment_it_was_given_is_not_changed(self):
        given = {"OPENROUTER_API_KEY": "x"}
        self.launcher.server_env(given, mode="openrouter", store="mongodb")
        self.assertEqual(given, {"OPENROUTER_API_KEY": "x"})

    def test_it_names_the_secrets_each_choice_needs_and_nothing_more(self):
        missing = self.launcher.missing_secrets
        self.assertEqual(missing({}, "openrouter", "mongodb"), ["OPENROUTER_API_KEY", "EMOTORAD_MONGO_URI"])
        self.assertEqual(missing({"OPENROUTER_API_KEY": "x"}, "openrouter", "memory"), [])
        self.assertEqual(missing({"OPENROUTER_API_KEY": " "}, "openrouter", "memory"), ["OPENROUTER_API_KEY"])
        self.assertEqual(missing({}, "anthropic", "memory"), ["ANTHROPIC_API_KEY"])
        self.assertEqual(missing({}, "offline", "memory"), [])

    def test_the_status_lines_never_carry_a_value(self):
        env = {"OPENROUTER_API_KEY": "sk-or-v1-secretvalue", "EMOTORAD_MONGO_URI": "mongodb+srv://u:pw@h/"}
        text = "\n".join(self.launcher.status_lines(env, "openrouter", "mongodb"))
        self.assertNotIn("secretvalue", text)
        self.assertNotIn("pw@", text)
        self.assertIn("OPENROUTER_API_KEY: set", text)
        self.assertIn("EMOTORAD_MONGO_URI: set", text)

    def test_the_url_routes_through_jev_on_openrouter_and_pins_without_it(self):
        self.assertEqual(self.launcher.chat_url("openrouter", 8000), "http://localhost:8000/chat?debug=1")
        # No Jev in the single-model modes: pin the agent the prompts were tuned
        # on, as the page always did, rather than the keyword triage.
        self.assertEqual(self.launcher.chat_url("offline", 8001),
                         "http://localhost:8001/chat?agent=battery_support&debug=1")

    def test_the_page_is_opened_on_the_origin_the_bucket_allows(self):
        # The media bucket's CORS allows http://localhost:8000 for local
        # testing (docs/runbooks/media.md section 1). A page opened on
        # 127.0.0.1 is another origin, and its video PUT to S3 is refused.
        # Both addresses, since a browser signed in on one origin is not
        # signed in on the other.
        for url in (self.launcher.chat_url("openrouter", 8000), self.launcher.sign_in_url(8000)):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("http://localhost:8000/"), url)

    def test_the_business_tools_are_always_the_fixtures(self):
        # With the OMS key the server reads the live purchase table, and in
        # openrouter mode real customers' details would leave AWS.
        env = self.launcher.server_env({"EMOTORAD_OMS_API_KEY": "k"}, mode="openrouter", store="mongodb")
        self.assertNotIn("EMOTORAD_OMS_API_KEY", env)
        lines = self.launcher.status_lines({"EMOTORAD_OMS_API_KEY": "k"}, "openrouter", "memory")
        self.assertTrue(any("fixtures" in line for line in lines))

    def test_it_says_where_to_sign_in_for_the_code_panel(self):
        # The page's code fetch cannot ask for the login itself; signing in
        # once under /dev/verification/ lets the browser send it from then on.
        self.assertEqual(self.launcher.sign_in_url(8000), "http://localhost:8000/dev/verification/sign-in")

    def test_it_only_ever_listens_on_this_machine(self):
        # Dev codes on means the code panel hands out a login for any phone.
        # The address printed is localhost; the address bound stays 127.0.0.1.
        command = self.launcher.server_command(8000)
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertNotIn("0.0.0.0", command)
        self.assertNotIn("localhost", command)

    def test_an_unknown_mode_or_store_is_refused(self):
        with self.assertRaises(SystemExit):
            self.launcher.parse_args(["--mode", "gpt"])
        with self.assertRaises(SystemExit):
            self.launcher.parse_args(["--store", "dynamodb"])


if __name__ == "__main__":
    unittest.main()
