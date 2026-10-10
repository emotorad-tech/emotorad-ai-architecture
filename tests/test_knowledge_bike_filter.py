"""The full agent's knowledge search is filtered to the bike the customer chose.

Found on 6 October 2026 while checking the staging test script. The router and
the narrow path pass the chosen bike to the knowledge filter
(Runtime._selected_bike), but the full agent's search_knowledge never received
it: the runtime's facts did not carry it, the tool did not ask for it, and the
web chat's API built its registry without a knowledge_bike. The filter saw an
empty bike, so a record scoped to one model (the Doodle power-on flow) could
never be found, and a Doodle owner was handed the standard flow, which
disagrees with it about what to check.
"""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.registry import ToolContext

# fixtures.WARRANTY_RECORDS: a T-Rex Air, an EMX Plus and a Doodle V3 on one number.
PHONE = "+919700000001"
DOODLE = "DDL32021100455"
EMX_PLUS = "EMXP2025004990"
QUERY = {"query": "bike will not turn on, no display", "topic": "battery"}
DOODLE_RECORD = "battery-doodle-wont-power-on"
STANDARD_RECORD = "battery-wont-power-on"


def _searched(selected_frame):
    """The record ids the full battery agent's search returned, for a customer
    who chose `selected_frame` (None: several bikes, none chosen)."""
    registry = build_registry()
    llm = ScriptedClaude([call_tool("search_knowledge", dict(QUERY)), say("Here is what to check.")])
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=llm,
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    state = runtime.conversations.get("c1")
    if selected_frame:
        state.select_bike(selected_frame)
    state.route_to("battery_support")
    runtime.handle(InboundMessage(
        conversation_id="c1",
        persona="customer",
        identity=Identity(strength=VERIFIED, phone=PHONE),
        channel="website_chat",
        message_text="my bike will not turn on",
    ))
    calls = [e for e in runtime.log.events if e["event"] == "tool_call" and e["tool"] == "search_knowledge"]
    assert len(calls) == 1, "the scripted search did not run: %r" % [e["event"] for e in runtime.log.events]
    return [passage["id"] for passage in calls[0]["result"]["data"]["passages"]]


class ChosenBikeReachesTheSearchTests(unittest.TestCase):
    def test_a_doodle_owner_gets_the_doodle_flow(self):
        ids = _searched(DOODLE)
        self.assertIn(DOODLE_RECORD, ids)
        # The standard record excludes the Doodle: it disagrees about what to check.
        self.assertNotIn(STANDARD_RECORD, ids)

    def test_an_emx_owner_never_gets_the_doodle_flow(self):
        ids = _searched(EMX_PLUS)
        self.assertIn(STANDARD_RECORD, ids)
        self.assertNotIn(DOODLE_RECORD, ids)

    def test_with_no_bike_chosen_the_search_stays_general(self):
        # Several bikes and none chosen: no model is assumed, so a record that
        # needs one stays out, as before.
        self.assertNotIn(DOODLE_RECORD, _searched(None))


class TheModelCannotChooseTheBikeTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()

    def test_the_bike_is_not_in_the_schema_the_model_sees(self):
        schema = next(s for s in self.registry.schemas_for(["search_knowledge"]) if s["name"] == "search_knowledge")
        self.assertNotIn("knowledge_bike", schema["input_schema"]["properties"])

    def test_a_bike_the_model_supplies_is_ignored(self):
        context = ToolContext(conversation_id="c1")
        envelope = self.registry.call(
            "search_knowledge", dict(QUERY, knowledge_bike={"product_name": "Doodle V3"}), context,
        )
        ids = [passage["id"] for passage in envelope["data"]["passages"]]
        self.assertNotIn(DOODLE_RECORD, ids)

    def test_the_injected_bike_filters_the_search(self):
        context = ToolContext(conversation_id="c1", late={"knowledge_bike": lambda: {"product_name": "Doodle V3"}})
        envelope = self.registry.call("search_knowledge", dict(QUERY), context)
        ids = [passage["id"] for passage in envelope["data"]["passages"]]
        self.assertIn(DOODLE_RECORD, ids)


if __name__ == "__main__":
    unittest.main()
