"""EMotorad's staff are "support executives", never "colleagues" (the person,
6 October 2026).

The customer agents were told to say "a colleague will confirm", in their own
prompts and in the knowledge records they read, so customers were told about
"a colleague". Every customer agent now gets one rule naming the words to use,
and no prompt, knowledge record, tool description or fixed text says
"colleague" again.
"""

import re
import unittest
from datetime import date
from pathlib import Path

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents import late_warranty
from emotorad_ai.agents.base import STAFF_WORDS_RULE
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry

ROOT = Path(__file__).resolve().parents[1]
# Everything a customer can read, or a model is told to say.
CUSTOMER_FACING = ("src", "prompts", "knowledge", "web")
COLLEAGUE = re.compile(r"colleague", re.IGNORECASE)


def _system_for(agent):
    registry = build_registry(today=date(2026, 10, 6), guide_media={}, sent_media={})
    runtime = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([say("Thanks.")]),
                      log=EventLog(path=None), resolver=IdentityResolver(registry),
                      conversations=InMemoryConversationStore())
    runtime.conversations.get("c1").route_to(agent)
    runtime.handle(WebsiteChatAdapter(runtime.resolver).to_message(
        {"conversation_id": "c1", "session_token": "sess-ananya", "text": "here is my bike"}))
    return runtime.llm.requests[0]["system"]


class NoColleagueAnywhereTests(unittest.TestCase):
    def test_no_customer_facing_file_says_colleague(self):
        found = []
        for folder in CUSTOMER_FACING:
            for path in sorted((ROOT / folder).rglob("*")):
                if not path.is_file() or path.suffix not in (".py", ".md", ".yaml", ".yml", ".html", ".js", ".txt"):
                    continue
                in_rule = False
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    # The rule itself has to name the word it forbids.
                    if line.startswith('STAFF_WORDS_RULE = """'):
                        in_rule = True
                    if not in_rule and COLLEAGUE.search(line):
                        found.append("%s:%d" % (path.relative_to(ROOT), number))
                    if in_rule and line.rstrip().endswith('"""') and not line.startswith("STAFF_WORDS_RULE"):
                        in_rule = False
        self.assertEqual(found, [])

    def test_the_scan_would_catch_it(self):
        self.assertTrue(COLLEAGUE.search('say a Colleague will confirm'))


class EveryCustomerAgentIsToldTheWordsTests(unittest.TestCase):
    def test_the_rule_names_both_words_and_the_one_to_avoid(self):
        self.assertIn("a support executive", STAFF_WORDS_RULE)
        self.assertIn("our support team", STAFF_WORDS_RULE)
        self.assertIn("Never call them a colleague", STAFF_WORDS_RULE)

    def test_the_battery_agent_gets_it(self):
        self.assertIn(STAFF_WORDS_RULE, _system_for("battery_support"))

    def test_the_motor_agent_gets_it(self):
        self.assertIn(STAFF_WORDS_RULE, _system_for("motor_support"))

    def test_the_registration_agent_gets_it(self):
        self.assertIn(STAFF_WORDS_RULE, _system_for(late_warranty.AGENT_NAME))


if __name__ == "__main__":
    unittest.main()
