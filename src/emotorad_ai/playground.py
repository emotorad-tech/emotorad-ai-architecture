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

Two ways to pick who the "customer" is for a turn:
  - Preset riders — named fixtures from tools/fixtures.py, the same data the
    automated test suite runs against.
  - Custom rider — typed in on the spot. Bike coverage is still computed by the
    real ``_coverage()`` helper from tools/mocks.py (never re-implemented here),
    so a hand-typed purchase date produces the same in/out-of-warranty math a
    real fixture would.

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
from emotorad_ai.tools.mocks import _coverage, build_registry

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


def _resolved_for_preset(scenario: Scenario) -> ResolvedIdentity:
    """Preset riders go through the real hydration path — registry + IdentityResolver
    — exactly like a live conversation would, so they stay honest as the mocks evolve."""
    identity = Identity(strength=scenario.strength, phone=scenario.phone)
    message = InboundMessage(
        conversation_id="preview",
        persona=scenario.persona,
        identity=identity,
        channel=scenario.channel,
        message_text="",
    )
    registry = build_registry(oms_available=scenario.oms_available, today=date.today())
    resolver = IdentityResolver(registry)
    return resolver.hydrate(message)


def _resolved_for_custom_customer(
    name: str, phone: Optional[str], verified: bool, bike_rows: List[Dict[str, Any]]
) -> ResolvedIdentity:
    """A hand-typed customer. Coverage is still computed by the real `_coverage()`
    helper (tools/mocks.py) — never re-implemented here — so a typed purchase date
    produces the same in/out-of-warranty math a real fixture would."""
    identity = Identity(strength=VERIFIED if verified else ANONYMOUS, phone=phone if verified else None)
    if not verified:
        return ResolvedIdentity(persona="customer", method="unverified", identity=identity)
    if not bike_rows:
        return ResolvedIdentity(
            persona="customer", method="no_warranty_record", identity=identity, error="no_warranty_record"
        )
    bikes = [_coverage(row, date.today()) for row in bike_rows]
    return ResolvedIdentity(
        persona="customer", method="verified", identity=identity, profile={"name": name}, bikes=bikes
    )


def _resolved_for_custom_dealer(
    name: str, phone: str, city: str, credit_limit: int, credit_used: int, overdue: int, terms_days: int
) -> ResolvedIdentity:
    identity = Identity(strength=VERIFIED, phone=phone)
    profile = {
        "dealer_id": "CUSTOM-%s" % phone[-4:],
        "name": name,
        "city": city,
        "credit_limit": credit_limit,
        "credit_used": credit_used,
        "payment_terms_days": terms_days,
        "overdue_amount": overdue,
        "status": "active",
    }
    return ResolvedIdentity(persona="dealer", method="verified", identity=identity, profile=profile)


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


def _custom_customer_form() -> ResolvedIdentity:
    name = st.text_input("Name", "Test Customer")
    phone = st.text_input("Phone", "+919999999999")
    verified = st.checkbox("Verified (signed in)", value=True)
    bike_rows: List[Dict[str, Any]] = []
    if verified:
        bike_count = st.number_input("Bikes owned", min_value=0, max_value=5, value=1, step=1)
        for i in range(int(bike_count)):
            with st.expander("Bike %d" % (i + 1), expanded=(bike_count == 1)):
                product_name = st.text_input("Model", "EMX Plus", key="custom_bike_model_%d" % i)
                purchase_date = st.date_input(
                    "Purchase date", value=date(2025, 6, 1), key="custom_bike_date_%d" % i
                )
                frame_number = st.text_input(
                    "Frame number", "CUSTOM%03d" % i, key="custom_bike_frame_%d" % i
                )
                battery_variant = st.text_input(
                    "Battery variant (optional)", "", key="custom_bike_batt_%d" % i
                )
                bike_rows.append(
                    {
                        "frame_number": frame_number,
                        "product_name": product_name,
                        "purchase_date": purchase_date.isoformat(),
                        "battery_variant": battery_variant,
                        "product_color": "",
                    }
                )
    return _resolved_for_custom_customer(name, phone, verified, bike_rows)


def _custom_dealer_form() -> ResolvedIdentity:
    name = st.text_input("Dealer name", "Test Cycle Stores")
    phone = st.text_input("Phone", "+919999999999")
    city = st.text_input("City", "Pune")
    credit_limit = st.number_input("Credit limit (₹)", min_value=0, value=500000, step=10000)
    credit_used = st.number_input("Credit used (₹)", min_value=0, value=100000, step=10000)
    overdue = st.number_input("Overdue amount (₹)", min_value=0, value=0, step=1000)
    terms_days = st.number_input("Payment terms (days)", min_value=0, value=30, step=5)
    return _resolved_for_custom_dealer(name, phone, city, int(credit_limit), int(credit_used), int(overdue), int(terms_days))


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

        module = importlib.import_module(AGENT_MODULES[agent_name])

        st.divider()
        st.subheader("Rider")
        rider_mode = st.radio("Rider source", ["Preset rider", "Custom rider"], horizontal=True)

        if rider_mode == "Preset rider":
            scenarios = _scenarios_for(agent_name)
            scenario_label = st.selectbox("Test customer/dealer scenario", [s.label for s in scenarios])
            scenario = next(s for s in scenarios if s.label == scenario_label)
            resolved = _resolved_for_preset(scenario)
            persona, channel = scenario.persona, scenario.channel
            rider_display = scenario.label
        else:
            st.caption(
                "Type in your own rider. Bike coverage still runs through the same "
                "coverage math the real tools use — just fed a typed purchase date "
                "instead of a fixture."
            )
            if agent_name == "dealer_orders":
                resolved = _custom_dealer_form()
                persona, channel = "dealer", "dealer_app"
            else:
                resolved = _custom_customer_form()
                persona, channel = "customer", "website_chat"
            rider_display = "Custom: %s" % (resolved.profile or {}).get("name", "unnamed")

        st.divider()
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
        st.caption("Rider: %s" % rider_display)

        for turn in st.session_state[session_key]:
            with st.chat_message(turn["role"]):
                st.write(turn["content"])

        user_text = st.chat_input("Type a test customer message…")
        if user_text:
            st.session_state[session_key].append({"role": "user", "content": user_text})

            message = InboundMessage(
                conversation_id=str(uuid.uuid4()),
                persona=persona,
                identity=resolved.identity,
                channel=channel,
                message_text=user_text,
            )
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
            preview_message = InboundMessage(
                conversation_id="preview",
                persona=persona,
                identity=resolved.identity,
                channel=channel,
                message_text="",
            )
            st.text(_tuned_system_prompt(module, preview_message, resolved, edited_prompt))


if __name__ == "__main__":
    main()
