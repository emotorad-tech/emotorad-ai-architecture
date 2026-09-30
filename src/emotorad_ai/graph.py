"""The turn, as a LangGraph graph.

The graph is the order, and the order is the design (runtime.py's docstring):
identity and context, then the safety and handoff gates, then verification for
an anonymous customer, then persona routing and triage, then Jev's path, then
an agent. Each node's work lives in
runtime.py; this file only says what may follow what, so the order is visible
in one place and cannot be rearranged by an edit to a step.

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
    handoff_gate: Node
    verify_gate: Node
    persona_route: Node
    jev_classify: Node
    standard_reply: Node
    narrow_agent: Node
    full_agent: Node


NODE_NAMES = (
    "prepare", "safety_gate", "handoff_gate", "verify_gate", "persona_route",
    "jev_classify", "standard_reply", "narrow_agent", "full_agent",
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
    graph.add_conditional_edges("safety_gate", _replied_or("handoff_gate"), ["handoff_gate", END])
    graph.add_conditional_edges("handoff_gate", _replied_or("verify_gate"), ["verify_gate", END])
    graph.add_conditional_edges("verify_gate", _replied_or("persona_route"), ["persona_route", END])
    graph.add_conditional_edges("persona_route", _replied_or("jev_classify"), ["jev_classify", END])
    graph.add_conditional_edges("jev_classify", _by_path, list(_PATH_NODES.values()))
    # A standard reply that fails its backstop, or a narrow agent whose model
    # failed, falls through to the full agent rather than ending the turn.
    graph.add_conditional_edges("standard_reply", _replied_or("full_agent"), ["full_agent", END])
    graph.add_conditional_edges("narrow_agent", _replied_or("full_agent"), ["full_agent", END])
    graph.add_edge("full_agent", END)
    return graph.compile()
