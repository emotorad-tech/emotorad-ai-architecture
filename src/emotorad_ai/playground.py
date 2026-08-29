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

Work survives a reload. Streamlit's ``session_state`` is scoped to the browser
tab's session, so a refresh — or the auto-rerun after a code edit — used to wipe
everything typed in. Three things are now mirrored to ``.playground/``
(gitignored): the custom rider, the open chat, and saved prompt versions.

Prompt versioning: "Save prompt version" appends to
``.playground/prompts/<agent>.json`` and the newest version becomes what a fresh
editor — and so every new chat — starts from. Old versions stay loadable.

What this is not: a way to create new agents, edit tool registries, or change
production behaviour. Neither save button touches ``agents/*.py``: versions are
tester-local, and "Save diff for review" only produces a diff for a human to
review and apply the normal way (a PR), matching the knowledge-base content
convention already used elsewhere in this repo. There is
also no tool-execution loop here (see the module docstring on `Agent.run` in
`agents/base.py` for what that looks like in production) — this is for
tone/behaviour tuning, not full conversational-flow testing.
"""

from __future__ import annotations

import base64
import difflib
import importlib
import json
import mimetypes
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime
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

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
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


CUSTOM_RIDER_PATH = PLAYGROUND_DIR / "custom_rider.json"
_MAX_CUSTOM_BIKES = 5

# Every custom-rider widget, with the value it starts at on a clean slate.
# Widgets below pass only `key=` and read their default from here via
# session_state, so there is exactly one place a default is written down.
_CUSTOM_RIDER_DEFAULTS: Dict[str, Any] = {
    "custom_name": "Test Customer",
    "custom_phone": "+919999999999",
    "custom_verified": True,
    "custom_bike_count": 1,
    "custom_dealer_name": "Test Cycle Stores",
    "custom_dealer_phone": "+919999999999",
    "custom_dealer_city": "Pune",
    "custom_dealer_credit_limit": 500000,
    "custom_dealer_credit_used": 100000,
    "custom_dealer_overdue": 0,
    "custom_dealer_terms": 30,
}
for _i in range(_MAX_CUSTOM_BIKES):
    _CUSTOM_RIDER_DEFAULTS["custom_bike_model_%d" % _i] = "EMX Plus"
    _CUSTOM_RIDER_DEFAULTS["custom_bike_date_%d" % _i] = "2025-06-01"
    _CUSTOM_RIDER_DEFAULTS["custom_bike_frame_%d" % _i] = "CUSTOM%03d" % _i
    _CUSTOM_RIDER_DEFAULTS["custom_bike_batt_%d" % _i] = ""

# Stored as ISO strings in JSON, handed to st.date_input as `date` objects.
_CUSTOM_RIDER_DATE_KEYS = {"custom_bike_date_%d" % i for i in range(_MAX_CUSTOM_BIKES)}


def _load_custom_rider() -> Dict[str, Any]:
    """The last custom rider typed in, so a reload does not wipe it.

    Streamlit's session_state lives only as long as the browser tab's session:
    a refresh, or the auto-rerun after a code edit, starts a fresh one and the
    form snaps back to defaults. Testing against real chats means re-entering
    the same rider across many turns and many restarts, so the form is mirrored
    to .playground/ — already gitignored, and where _save_diff writes too.

    Never holds anything but the tester's own typed fixture: the API key is
    deliberately not part of this (it stays session-only, as the sidebar says).
    """
    try:
        loaded = json.loads(CUSTOM_RIDER_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _seed_custom_rider_state() -> None:
    """Prime session_state before the widgets are built, once per session.

    Assigning to a widget's key *before* that widget is instantiated is the
    supported way to give it a starting value; doing it after would raise.
    """
    if st.session_state.get("_custom_rider_seeded"):
        return
    saved = _load_custom_rider()
    for key, default in _CUSTOM_RIDER_DEFAULTS.items():
        value = saved.get(key, default)
        if key in _CUSTOM_RIDER_DATE_KEYS:
            try:
                value = date.fromisoformat(str(value))
            except ValueError:
                value = date.fromisoformat(str(default))
        elif not isinstance(value, type(default)):
            value = default  # a hand-edited/stale file must not break the form
        st.session_state.setdefault(key, value)
    st.session_state["_custom_rider_seeded"] = True


def _persist_custom_rider() -> None:
    values: Dict[str, Any] = {}
    for key in _CUSTOM_RIDER_DEFAULTS:
        if key not in st.session_state:
            continue
        value = st.session_state[key]
        values[key] = value.isoformat() if isinstance(value, date) else value
    try:
        PLAYGROUND_DIR.mkdir(exist_ok=True)
        CUSTOM_RIDER_PATH.write_text(json.dumps(values, indent=2, sort_keys=True))
    except OSError:
        pass  # a convenience, never worth breaking the page over


def _clear_custom_rider() -> None:
    try:
        CUSTOM_RIDER_PATH.unlink()
    except OSError:
        pass
    for key in list(_CUSTOM_RIDER_DEFAULTS) + ["_custom_rider_seeded"]:
        st.session_state.pop(key, None)


CHAT_DIR = PLAYGROUND_DIR / "chats"
PROMPT_DIR = PLAYGROUND_DIR / "prompts"


def _new_chat_id() -> str:
    """Short enough to read off the screen and quote in a bug report."""
    return "%s-%s" % (date.today().strftime("%Y%m%d"), uuid.uuid4().hex[:8])


def _blank_chat() -> Dict[str, Any]:
    return {
        "chat_id": _new_chat_id(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "turns": [],
    }


def _chat_path(agent_name: str) -> Path:
    return CHAT_DIR / ("%s.json" % agent_name)


def _load_chat(agent_name: str) -> Dict[str, Any]:
    """The open chat for this agent, so a refresh does not end the conversation.

    One live chat per agent: "Start new chat" is the only thing that retires it.
    Kept under .playground/ (gitignored) — note this does put the test transcript,
    and any attachment bytes, on local disk.
    """
    try:
        loaded = json.loads(_chat_path(agent_name).read_text())
    except (OSError, ValueError):
        return _blank_chat()
    if not isinstance(loaded, dict) or not isinstance(loaded.get("turns"), list):
        return _blank_chat()
    loaded.setdefault("chat_id", _new_chat_id())
    loaded.setdefault("started_at", "")
    return loaded


def _save_chat(agent_name: str, chat: Dict[str, Any]) -> None:
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        _chat_path(agent_name).write_text(json.dumps(chat, indent=2))
    except OSError:
        pass  # a convenience, never worth breaking the page over


def _prompt_path(agent_name: str) -> Path:
    return PROMPT_DIR / ("%s.json" % agent_name)


def _load_prompt_versions(agent_name: str) -> List[Dict[str, Any]]:
    try:
        loaded = json.loads(_prompt_path(agent_name).read_text())
    except (OSError, ValueError):
        return []
    versions = loaded.get("versions") if isinstance(loaded, dict) else None
    if not isinstance(versions, list):
        return []
    return [v for v in versions if isinstance(v, dict) and "text" in v and "version" in v]


def _latest_prompt_version(agent_name: str) -> Optional[Dict[str, Any]]:
    versions = _load_prompt_versions(agent_name)
    return versions[-1] if versions else None


def _save_prompt_version(agent_name: str, text: str) -> Dict[str, Any]:
    versions = _load_prompt_versions(agent_name)
    record = {
        "version": versions[-1]["version"] + 1 if versions else 1,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "text": text,
    }
    versions.append(record)
    try:
        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        _prompt_path(agent_name).write_text(json.dumps({"versions": versions}, indent=2))
    except OSError:
        pass
    return record


def _default_prompt(agent_name: str, module: Any) -> str:
    """What a fresh editor starts from: newest saved version, else the module's."""
    latest = _latest_prompt_version(agent_name)
    return latest["text"] if latest else module._BASE_PROMPT


def _prompt_version_label(agent_name: str, edited_prompt: str) -> str:
    """How the prompt in the editor relates to what has been saved."""
    latest = _latest_prompt_version(agent_name)
    if latest is None:
        return "base"
    if latest["text"] == edited_prompt:
        return "v%d" % latest["version"]
    return "v%d+draft" % latest["version"]


def _custom_customer_form() -> ResolvedIdentity:
    name = st.text_input("Name", key="custom_name")
    phone = st.text_input("Phone", key="custom_phone")
    verified = st.checkbox("Verified (signed in)", key="custom_verified")
    bike_rows: List[Dict[str, Any]] = []
    if verified:
        bike_count = st.number_input(
            "Bikes owned", min_value=0, max_value=_MAX_CUSTOM_BIKES, step=1, key="custom_bike_count"
        )
        for i in range(int(bike_count)):
            with st.expander("Bike %d" % (i + 1), expanded=(bike_count == 1)):
                product_name = st.text_input("Model", key="custom_bike_model_%d" % i)
                purchase_date = st.date_input("Purchase date", key="custom_bike_date_%d" % i)
                frame_number = st.text_input("Frame number", key="custom_bike_frame_%d" % i)
                battery_variant = st.text_input(
                    "Battery variant (optional)", key="custom_bike_batt_%d" % i
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
    name = st.text_input("Dealer name", key="custom_dealer_name")
    phone = st.text_input("Phone", key="custom_dealer_phone")
    city = st.text_input("City", key="custom_dealer_city")
    credit_limit = st.number_input("Credit limit (₹)", min_value=0, step=10000, key="custom_dealer_credit_limit")
    credit_used = st.number_input("Credit used (₹)", min_value=0, step=10000, key="custom_dealer_credit_used")
    overdue = st.number_input("Overdue amount (₹)", min_value=0, step=1000, key="custom_dealer_overdue")
    terms_days = st.number_input("Payment terms (days)", min_value=0, step=5, key="custom_dealer_terms")
    return _resolved_for_custom_dealer(name, phone, city, int(credit_limit), int(credit_used), int(overdue), int(terms_days))


def _serialise_uploaded_file(uploaded: Any) -> Dict[str, Any]:
    name = uploaded.name or "upload"
    content = uploaded.getvalue()
    mime_type = uploaded.type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {
        "name": name,
        "kind": "image" if mime_type.startswith("image/") else "document",
        "mime_type": mime_type,
        "data": base64.b64encode(content).decode("utf-8"),
    }


def _attachment_blocks(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for attachment in attachments:
        mime_type = attachment.get("mime_type") or "application/octet-stream"
        data = attachment.get("data")
        if not data:
            continue
        if mime_type.startswith("image/"):
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": data},
                }
            )
        elif mime_type.lower() == "application/pdf" or attachment.get("name", "").lower().endswith(".pdf"):
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                    "title": attachment.get("name", "document.pdf"),
                }
            )
    return blocks


def _should_submit_chat(user_text: Optional[str], pending_files: List[Any]) -> bool:
    """Only send after the user explicitly submits a chat turn.

    Uploading a file should not trigger a side-effect by itself; the file can stay
    queued until the user types a message or otherwise confirms the send action.
    """
    return user_text is not None and (bool(user_text.strip()) or bool(pending_files))


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
            _seed_custom_rider_state()
            if agent_name == "dealer_orders":
                resolved = _custom_dealer_form()
                persona, channel = "dealer", "dealer_app"
            else:
                resolved = _custom_customer_form()
                persona, channel = "customer", "website_chat"
            _persist_custom_rider()
            if st.button(
                "Reset custom rider",
                help="Forget the saved rider and start again from the defaults.",
            ):
                _clear_custom_rider()
                st.rerun()
            rider_display = "Custom: %s" % (resolved.profile or {}).get("name", "unnamed")

        st.divider()
        st.caption("Tools this agent has (not called in this playground): " + ", ".join(module.TOOL_NAMES))

    session_key = "chat_%s" % agent_name
    textarea_key = "textarea_%s" % agent_name
    nonce_key = "uploader_nonce_%s" % agent_name
    pending_load_key = "pending_prompt_load_%s" % agent_name

    # Restoring an old version has to land here, before the textarea exists:
    # Streamlit refuses writes to a widget's key once that widget is built.
    if pending_load_key in st.session_state:
        st.session_state[textarea_key] = st.session_state.pop(pending_load_key)
    if textarea_key not in st.session_state:
        st.session_state[textarea_key] = _default_prompt(agent_name, module)
    if session_key not in st.session_state:
        st.session_state[session_key] = _load_chat(agent_name)

    chat = st.session_state[session_key]

    col_prompt, col_chat = st.columns([1, 1])

    with col_prompt:
        st.subheader("System prompt (%s)" % agent_name)
        edited_prompt = st.text_area(
            "Edit, then Save version — the newest version is what new chats start from",
            height=500,
            key=textarea_key,
        )

        save_col, diff_col = st.columns([1, 1])
        with save_col:
            if st.button("💾 Save prompt version", type="primary", use_container_width=True):
                current = _latest_prompt_version(agent_name)
                if current is not None and current["text"] == edited_prompt:
                    st.info("No changes since v%d." % current["version"])
                else:
                    record = _save_prompt_version(agent_name, edited_prompt)
                    st.success("Saved v%d — new chats now start from this." % record["version"])
        with diff_col:
            if st.button("Save diff for review", use_container_width=True):
                path = _save_diff(agent_name, module._BASE_PROMPT, edited_prompt)
                st.success("Diff written to %s — review and apply it as a normal reviewed change." % path)
                st.code(path.read_text(), language="diff")

        # Read back *after* the save button so the status reflects this click.
        latest = _latest_prompt_version(agent_name)
        if latest is None:
            st.caption("Unsaved — still `_BASE_PROMPT` from the agent module.")
        elif latest["text"] == edited_prompt:
            st.caption("Saved as **v%d** · %s" % (latest["version"], latest["saved_at"]))
        else:
            st.caption("**Unsaved edits** on top of v%d · %s" % (latest["version"], latest["saved_at"]))

        versions = _load_prompt_versions(agent_name)
        if versions:
            with st.expander("Version history — %d saved" % len(versions)):
                newest_first = list(reversed(versions))
                labels = ["v%d — %s" % (v["version"], v["saved_at"]) for v in newest_first]
                picked = st.selectbox("Version", labels, key="version_pick_%s" % agent_name)
                chosen = newest_first[labels.index(picked)]
                if st.button("Load v%d into the editor" % chosen["version"]):
                    st.session_state[pending_load_key] = chosen["text"]
                    st.rerun()
                st.code(chosen["text"])

    with col_chat:
        st.subheader("Test conversation")
        head_left, head_right = st.columns([3, 1])
        with head_left:
            st.caption("Rider: %s" % rider_display)
            st.caption(
                "Chat `%s` · %d turns · prompt %s"
                % (chat["chat_id"], len(chat["turns"]), _prompt_version_label(agent_name, edited_prompt))
            )
        with head_right:
            if st.button("➕ Start new chat", use_container_width=True):
                st.session_state[session_key] = _blank_chat()
                _save_chat(agent_name, st.session_state[session_key])
                st.session_state[nonce_key] = st.session_state.get(nonce_key, 0) + 1
                st.rerun()

        for turn in chat["turns"]:
            with st.chat_message(turn["role"]):
                attachments = turn.get("attachments") or []
                if attachments:
                    for attachment in attachments:
                        st.caption("📎 %s" % attachment["name"])
                st.write(turn["content"])

        # The uploader keeps its files until the user clears it, so a sent file
        # would otherwise re-attach itself to every later message. Its key carries
        # a counter that is bumped on send: a new key means a fresh, empty widget.
        # (Assigning [] to a file_uploader's own key raises instead of clearing it.)
        nonce = st.session_state.setdefault(nonce_key, 0)
        uploaded_files = st.file_uploader(
            "📎 Attach image or PDF",
            type=["png", "jpg", "jpeg", "pdf"],
            accept_multiple_files=True,
            key="uploader_%s_%d" % (agent_name, nonce),
            help="Upload JPG, JPEG, PNG, or PDF files to test multimodal prompts.",
        )

        pending_files = list(uploaded_files or [])
        if pending_files:
            st.caption("Attached: %s" % ", ".join(file.name for file in pending_files))

        user_text = st.chat_input("Type a test customer message…")
        if _should_submit_chat(user_text, pending_files):
            attachments = [_serialise_uploaded_file(file) for file in pending_files]
            chat["turns"].append({
                "role": "user",
                "content": user_text.strip() if user_text else "(Uploaded file(s) for review)",
                "attachments": attachments,
                "prompt_version": _prompt_version_label(agent_name, edited_prompt),
            })

            message = InboundMessage(
                # The whole chat shares one id now, which is what a real
                # conversation looks like to the agent — and it is the id shown
                # on screen, so a bad turn can be traced back to its transcript.
                conversation_id=chat["chat_id"],
                persona=persona,
                identity=resolved.identity,
                channel=channel,
                message_text=user_text.strip() if user_text else "",
                attachments=[
                    Attachment(kind=item["kind"], url="data:%s;base64,%s" % (item["mime_type"], item["data"]), mime_type=item["mime_type"])
                    for item in attachments
                ],
            )
            system_prompt = _tuned_system_prompt(module, message, resolved, edited_prompt)

            if not api_key:
                chat["turns"].append(
                    {
                        "role": "assistant",
                        "content": "(No API key entered — add one in the sidebar to get a real response.)",
                    }
                )
            else:
                import anthropic

                client = anthropic.Anthropic(api_key=api_key)
                anthropic_messages: List[Dict[str, Any]] = []
                for turn in chat["turns"]:
                    if turn["role"] not in ("user", "assistant"):
                        continue
                    blocks: List[Dict[str, Any]] = []
                    content = turn.get("content") or ""
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for attachment in turn.get("attachments") or []:
                        blocks.extend(_attachment_blocks([attachment]))
                    anthropic_messages.append({"role": turn["role"], "content": blocks})

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
                chat["turns"].append({"role": "assistant", "content": text})

            _save_chat(agent_name, chat)
            st.session_state[nonce_key] = nonce + 1
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
