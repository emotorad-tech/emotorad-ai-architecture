"""The turn, as a LangGraph graph.

The graph is the order, and the order is the design (runtime.py's docstring):
identity and context, then the safety gate, the call-back number, going back
and the handoff gate, then verification for an anonymous customer, then
persona routing and triage, then the melt ask, then Jev's path, then an agent. Each node's work
lives in runtime.py; this file only says what may follow what, so the order
is visible in one place and cannot be rearranged by an edit to a step.

Conversation memory stays in ConversationStore. The graph state is one turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .contract import InboundMessage, Reply
from .conversation import ConversationState
from .decisions import Route
from .identity import ResolvedIdentity


class TurnState(TypedDict, total=False):
    message: InboundMessage
    conversation: ConversationState
    resolved: ResolvedIdentity
    route: Route
    reply: Reply


Node = Callable[[TurnState], Dict[str, Any]]


@dataclass(frozen=True)
class TurnNodes:
    prepare: Node
    safety_gate: Node
    callback_gate: Node
    navigation_gate: Node
    handoff_gate: Node
    erasure_gate: Node
    verify_gate: Node
    persona_route: Node
    serial_confirm: Node
    melt_ask: Node
    jev_classify: Node
    standard_reply: Node
    narrow_agent: Node
    full_agent: Node


NODE_NAMES = (
    "prepare", "safety_gate", "callback_gate", "navigation_gate", "handoff_gate", "erasure_gate", "verify_gate",
    "persona_route", "serial_confirm", "melt_ask", "jev_classify", "standard_reply", "narrow_agent", "full_agent",
)

_PATH_NODES = {"standard": "standard_reply", "narrow": "narrow_agent", "full": "full_agent"}


def _replied_or(next_node: str) -> Callable[[TurnState], str]:
    def decide(state: TurnState) -> str:
        return END if state.get("reply") is not None else next_node

    return decide


def _by_path(state: TurnState) -> str:
    route = state.get("route")
    return _PATH_NODES.get(route.path if route else "full", "full_agent")


def build_turn_graph(nodes: TurnNodes):
    graph = StateGraph(TurnState)
    for name in NODE_NAMES:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "safety_gate")
    graph.add_conditional_edges("safety_gate", _replied_or("callback_gate"), ["callback_gate", END])
    # The call-back number (runtime._node_callback). While a handover or a
    # safety report waits for a number, the next message is read for one
    # here, straight after safety and before going back and the verify step,
    # so a number typed for a call is never taken as a number to verify.
    graph.add_conditional_edges("callback_gate", _replied_or("navigation_gate"), ["navigation_gate", END])
    # Going back (navigation.py): another number, another bike, the list
    # again, a fresh start. After safety, which always comes first.
    graph.add_conditional_edges("navigation_gate", _replied_or("handoff_gate"), ["handoff_gate", END])
    graph.add_conditional_edges("handoff_gate", _replied_or("erasure_gate"), ["erasure_gate", END])
    graph.add_conditional_edges("erasure_gate", _replied_or("verify_gate"), ["verify_gate", END])
    graph.add_conditional_edges("verify_gate", _replied_or("persona_route"), ["persona_route", END])
    graph.add_conditional_edges("persona_route", _replied_or("serial_confirm"), ["serial_confirm", END])
    # The customer's answer to a serial confirmation (serial_confirm.py): a
    # reply by code, or on to the melt ask when the message is not an answer.
    graph.add_conditional_edges("serial_confirm", _replied_or("melt_ask"), ["melt_ask", END])
    # The melt ask (melt_ask.py): a fixed reply by code once the bike is
    # chosen and routed, so it comes after triage and before Jev or any model.
    graph.add_conditional_edges("melt_ask", _replied_or("jev_classify"), ["jev_classify", END])
    graph.add_conditional_edges("jev_classify", _by_path, list(_PATH_NODES.values()))
    # A standard reply that fails its backstop, or a narrow agent whose model
    # failed, falls through to the full agent rather than ending the turn.
    graph.add_conditional_edges("standard_reply", _replied_or("full_agent"), ["full_agent", END])
    graph.add_conditional_edges("narrow_agent", _replied_or("full_agent"), ["full_agent", END])
    graph.add_edge("full_agent", END)
    return graph.compile()
