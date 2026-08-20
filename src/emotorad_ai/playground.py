"""Prompt-tuning playground for the sub-agents ("AIRO v0" — build plan context in
the repo README/CLAUDE.md; this is a deliberately small slice of that idea).

Run locally:

    streamlit run src/emotorad_ai/playground.py

What this is: a page where someone edits a sub-agent's system prompt, sends test
customer messages, and sees a real Claude response — so tone/behaviour can be
tuned without touching code. Reuses each agent's real ``build_system_prompt``
(via a temporary monkey-patch of its module-level ``_BASE_PROMPT``), so the
playground never re-implements — and cannot drift from — the prompt-assembly
logic those functions already encode.

What this is not: a way to create new agents, edit tool registries, or change
production behaviour. "Save" never touches ``agents/*.py`` — it only produces a
diff for a human to review and apply the normal way (a PR), matching the
knowledge-base content convention already used elsewhere in this repo. There is
also no tool-execution loop here (see the module docstring on `Agent.run` in
`agents/base.py` for what that looks like in production) — this is for
tone/behaviour tuning, not full conversational-flow testing.
"""

from __future__ import annotations

import difflib
import importlib
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

# `streamlit run` executes this file as a standalone script, so `src/` is never
# put on sys.path automatically the way an installed package would be. Same
# bootstrap tests/__init__.py already uses to import emotorad_ai without an
# install step — this file just needs its own copy, since Streamlit never
# imports the `tests` package.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry

MODELS = {
    "Haiku 4.5 (cheap, fast — bulk iteration)": "claude-haiku-4-5",
    "Sonnet 5": "claude-sonnet-5",
    "Opus 5 (production model — final validation pass)": "claude-opus-5",
}

AGENT_MODULES = {
    "battery_support": "emotorad_ai.agents.battery_support",
    "motor_support": "emotorad_ai.agents.motor_support",
    "late_warranty_registration": "emotorad_ai.agents.late_warranty",
    "dealer_orders": "emotorad_ai.agents.dealer_orders",
}

PLAYGROUND_DIR = Path(__file__).resolve().parent.parent.parent / ".playground"


@dataclass(frozen=True)
class Scenario:
    label: str
    persona: str
    channel: str
    phone: Optional[str]
    strength: str
    oms_available: bool = True


CUSTOMER_SCENARIOS = [
    Scenario("Single bike, in warranty (Ananya)", "customer", "website_chat", "+919876543210", VERIFIED),
    Scenario("Single bike, out of warranty (Rohit)", "customer", "website_chat", "+919812345678", VERIFIED),
    Scenario("Multi-bike customer (Priya, 3 bikes)", "customer", "whatsapp", "+919700000001", VERIFIED),
    Scenario(
        "Unregistered — no warranty record",
        "customer",
        "website_chat",
        fixtures.PHONE_WITH_NO_RECORD,
        VERIFIED,
    ),
    Scenario("OMS system down (outage)", "customer", "website_chat", "+919876543210", VERIFIED, oms_available=False),
    Scenario("Anonymous / not signed in", "customer", "website_chat", None, ANONYMOUS),
]

DEALER_SCENARIOS = [
    Scenario("Dealer, healthy credit (Royal Cycle Stores)", "dealer", "dealer_app", "+919000000001", VERIFIED),
]


def _scenarios_for(agent_name: str) -> List[Scenario]:
    return DEALER_SCENARIOS if agent_name == "dealer_orders" else CUSTOMER_SCENARIOS


def _build_resolved_identity(scenario: Scenario, message: InboundMessage) -> ResolvedIdentity:
    registry = build_registry(oms_available=scenario.oms_available, today=date.today())
    resolver = IdentityResolver(registry)
    return resolver.hydrate(message)


def _tuned_system_prompt(module: Any, message: InboundMessage, resolved: ResolvedIdentity, edited_prompt: str) -> str:
    """Call the agent's real ``build_system_prompt`` with the edited base text.

    Monkey-patches the module-level ``_BASE_PROMPT`` for the duration of the
    call so the agent's own ``_facts_block``/``_context_block``/``_entry_block``
    helpers still run — this is the whole point: the playground never
    re-implements prompt assembly, it just substitutes the editable part.
    """
    original = module._BASE_PROMPT
    module._BASE_PROMPT = edited_prompt
    try:
        return module.DEFINITION.build_system_prompt(message, resolved, "")
    finally:
        module._BASE_PROMPT = original


def _save_diff(agent_name: str, original_prompt: str, edited_prompt: str) -> Path:
    diff_lines = difflib.unified_diff(
        original_prompt.splitlines(keepends=True),
        edited_prompt.splitlines(keepends=True),
        fromfile="%s/_BASE_PROMPT (current)" % agent_name,
        tofile="%s/_BASE_PROMPT (edited in playground)" % agent_name,
    )
    diff_text = "".join(diff_lines) or "# no changes\n"
    PLAYGROUND_DIR.mkdir(exist_ok=True)
    path = PLAYGROUND_DIR / ("%s_%s.patch" % (agent_name, date.today().isoformat()))
    path.write_text(diff_text)
    return path


def main() -> None:
    st.set_page_config(page_title="Emotorad AI — prompt playground", layout="wide")
    st.title("Prompt-tuning playground")
    st.caption(
        "Edit a sub-agent's system prompt, chat-test it against a real Claude model, "
        "and save a diff for review. Nothing here writes to production code."
    )

    with st.sidebar:
        st.subheader("Setup")
        agent_name = st.selectbox("Agent", list(AGENT_MODULES.keys()))
        model_label = st.selectbox("Model", list(MODELS.keys()))
        model_id = MODELS[model_label]

        api_key = st.text_input(
            "Anthropic API key",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            type="password",
            help="Session-only — never written to disk. Falls back to ANTHROPIC_API_KEY if set.",
        )

        scenarios = _scenarios_for(agent_name)
        scenario_label = st.selectbox("Test customer/dealer scenario", [s.label for s in scenarios])
        scenario = next(s for s in scenarios if s.label == scenario_label)

        module = importlib.import_module(AGENT_MODULES[agent_name])
        st.caption("Tools this agent has (not called in this playground): " + ", ".join(module.TOOL_NAMES))

    session_key = "chat_%s" % agent_name
    prompt_key = "prompt_%s" % agent_name
    if prompt_key not in st.session_state:
        st.session_state[prompt_key] = module._BASE_PROMPT
    if session_key not in st.session_state:
        st.session_state[session_key] = []  # type: ignore[assignment]

    col_prompt, col_chat = st.columns([1, 1])

    with col_prompt:
        st.subheader("System prompt (%s)" % agent_name)
        edited_prompt = st.text_area(
            "Edit and test — nothing is saved until you click Save below",
            value=st.session_state[prompt_key],
            height=500,
            key="textarea_%s" % agent_name,
        )
        st.session_state[prompt_key] = edited_prompt

        if st.button("Save diff for review"):
            path = _save_diff(agent_name, module._BASE_PROMPT, edited_prompt)
            st.success("Diff written to %s — review and apply it as a normal reviewed change." % path)
            st.code(path.read_text(), language="diff")

    with col_chat:
        st.subheader("Test conversation")
        st.caption("Scenario: %s" % scenario.label)

        for turn in st.session_state[session_key]:
            with st.chat_message(turn["role"]):
                st.write(turn["content"])

        user_text = st.chat_input("Type a test customer message…")
        if user_text:
            st.session_state[session_key].append({"role": "user", "content": user_text})

            identity = Identity(strength=scenario.strength, phone=scenario.phone)
            message = InboundMessage(
                conversation_id=str(uuid.uuid4()),
                persona=scenario.persona,
                identity=identity,
                channel=scenario.channel,
                message_text=user_text,
            )
            resolved = _build_resolved_identity(scenario, message)
            system_prompt = _tuned_system_prompt(module, message, resolved, edited_prompt)

            if not api_key:
                st.session_state[session_key].append(
                    {
                        "role": "assistant",
                        "content": "(No API key entered — add one in the sidebar to get a real response.)",
                    }
                )
            else:
                import anthropic

                client = anthropic.Anthropic(api_key=api_key)
                anthropic_messages: List[Dict[str, str]] = [
                    {"role": t["role"], "content": t["content"]}
                    for t in st.session_state[session_key]
                    if t["role"] in ("user", "assistant")
                ]
                try:
                    response = client.messages.create(
                        model=model_id,
                        max_tokens=1024,
                        system=system_prompt,
                        messages=anthropic_messages,
                    )
                    text = "\n".join(block.text for block in response.content if block.type == "text")
                except anthropic.APIStatusError as exc:
                    text = "API error: %s" % exc.message
                st.session_state[session_key].append({"role": "assistant", "content": text})

            st.rerun()

        with st.expander("System prompt sent to the model (this turn)"):
            st.text(
                _tuned_system_prompt(
                    module,
                    InboundMessage(
                        conversation_id="preview",
                        persona=scenario.persona,
                        identity=Identity(strength=scenario.strength, phone=scenario.phone),
                        channel=scenario.channel,
                        message_text="",
                    ),
                    _build_resolved_identity(
                        scenario,
                        InboundMessage(
                            conversation_id="preview",
                            persona=scenario.persona,
                            identity=Identity(strength=scenario.strength, phone=scenario.phone),
                            channel=scenario.channel,
                            message_text="",
                        ),
                    ),
                    edited_prompt,
                )
            )


if __name__ == "__main__":
    main()
