"""The playground: add a bot, tune its prompt, and test it through the real
runtime ("AIRO v0" — the deferred agent-builder, at the size the build plan
said to start with).

Run locally:

    streamlit run src/emotorad_ai/playground.py

Two modes:

  Chat     — pick any bot (built-in, published, or draft), a rider and a model,
             and talk to it. Every turn runs production's `Runtime`: identity,
             enrichment, the safety and handoff guardrails, triage with bike
             selection, the sub-agent tool loop against the mocked tools, the
             coverage post-check and the AI disclosure. The prompt editor
             changes the bot's base prompt from the next turn on.
  New bot  — a form that writes a draft spec (and optional knowledge records)
             into the drafts directory. The draft is immediately selectable in
             Chat. "Export for review" copies it into `.playground/export/` for
             an engineer to move into `bots/` and `knowledge/` via PR.

What this is not: a way to add tools, guardrails or ticket categories, or to
change production behaviour. Nothing here writes to a tracked file.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import importlib
import mimetypes
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import yaml

# `streamlit run` executes this file as a script, so `src/` is never on sys.path
# the way an installed package would be — same bootstrap tests/__init__.py uses.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.bots import BUILTIN, DRAFT, PERSONA_TOOLS, BotSpecError
from emotorad_ai.config import Settings
from emotorad_ai.contract import Attachment
from emotorad_ai.knowledge import KnowledgeError
from emotorad_ai.llm import AnthropicClaude, OfflinePlanner
from emotorad_ai.playground_bots import (
    DRAFTS_DIR,
    EXPORT_DIR,
    delete_draft,
    export_for_review,
    load_catalogue,
    prompt_template,
    save_draft,
    save_draft_knowledge,
)
from emotorad_ai.playground_riders import (
    custom_customer_form,
    custom_dealer_form,
    resolved_for_preset,
    scenarios_for,
)
from emotorad_ai.playground_runtime import PlaygroundSession

# Both support adaptive thinking and effort, which the runtime always sends.
MODELS = {
    "Opus 5 (production model)": "claude-opus-5",
    "Sonnet 5 (faster iteration)": "claude-sonnet-5",
    "Offline planner (no model, no key)": None,
}

PLAYGROUND_DIR = Path(__file__).resolve().parent.parent.parent / ".playground"


# --- helpers -------------------------------------------------------------------


def _save_prompt_diff(agent_name: str, original_prompt: str, edited_prompt: str) -> Path:
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


def _attachment_from_upload(uploaded: Any) -> Attachment:
    name = uploaded.name or "upload"
    mime_type = uploaded.type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    data = base64.b64encode(uploaded.getvalue()).decode("utf-8")
    return Attachment(
        kind="image" if mime_type.startswith("image/") else "document",
        url="data:%s;base64,%s" % (mime_type, data),
        mime_type=mime_type,
    )


def _make_llm(model_id: Optional[str], api_key: str) -> Any:
    if model_id is None:
        return OfflinePlanner()
    return AnthropicClaude(Settings(), api_key=api_key, model=model_id)


def _new_session(bot: str, resolved: Any, channel: str, oms_available: bool, llm: Any) -> PlaygroundSession:
    return PlaygroundSession(
        bot=bot,
        resolved=resolved,
        channel=channel,
        catalogue=load_catalogue(DRAFTS_DIR),
        llm=llm,
        oms_available=oms_available,
        drafts_dir=DRAFTS_DIR,
    )


def _source_tag(source: str) -> str:
    return {"builtin": "built-in", "published": "published", "draft": "draft"}[source]


# --- Chat mode -------------------------------------------------------------------


def _chat_mode(catalogue: Any) -> None:
    with st.sidebar:
        labels = {"%s  [%s]" % (s.name, _source_tag(s.source)): s.name for s in catalogue.specs}
        bot_name = labels[st.selectbox("Bot", list(labels.keys()))]
        spec = catalogue.get(bot_name)

        model_label = st.selectbox("Model", list(MODELS.keys()))
        model_id = MODELS[model_label]
        api_key = ""
        if model_id is not None:
            api_key = st.text_input(
                "Anthropic API key",
                value=os.environ.get("ANTHROPIC_API_KEY", ""),
                type="password",
                help="Session-only — never written to disk. Falls back to ANTHROPIC_API_KEY if set.",
            )

        st.divider()
        st.subheader("Rider")
        rider_mode = st.radio("Rider source", ["Preset rider", "Custom rider"], horizontal=True)
        oms_available = True
        if rider_mode == "Preset rider":
            scenarios = scenarios_for(spec.persona)
            scenario_label = st.selectbox("Scenario", [s.label for s in scenarios])
            scenario = next(s for s in scenarios if s.label == scenario_label)
            resolved = resolved_for_preset(scenario)
            channel = scenario.channel
            oms_available = scenario.oms_available
            rider_display = scenario.label
        else:
            if spec.persona == "dealer":
                resolved = custom_dealer_form()
                channel = "dealer_app"
            else:
                resolved = custom_customer_form()
                channel = "website_chat"
            rider_display = "Custom: %s" % (resolved.profile or {}).get("name", "unnamed")

        pill: Optional[str] = None
        if spec.topic and spec.persona == "customer":
            if st.checkbox("Arrive via the '%s' pill" % spec.topic, value=False):
                pill = spec.topic

        st.divider()
        st.caption("Tools this bot may call: " + ", ".join(spec.tools))

    # One Runtime per (bot, rider, model). Changing any of them starts a fresh
    # conversation; editing the prompt does not. A short digest of the key
    # (not bool(api_key)) so correcting a typo'd key also starts a fresh
    # session — a wrong-then-right key would otherwise hash to the same
    # `True` and the client would never be rebuilt.
    api_key_digest = hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else ""
    session_key = (bot_name, rider_display, repr(resolved), model_id, api_key_digest)
    if st.session_state.get("session_key") != session_key or "session" not in st.session_state:
        if model_id is not None and not api_key:
            st.session_state["session"] = None
        else:
            st.session_state["session"] = _new_session(
                bot_name, resolved, channel, oms_available, _make_llm(model_id, api_key)
            )
        st.session_state["session_key"] = session_key
        st.session_state["transcript"] = []
    session: Optional[PlaygroundSession] = st.session_state["session"]

    prompt_key = "prompt_%s" % bot_name
    if prompt_key not in st.session_state:
        st.session_state[prompt_key] = spec.prompt

    col_prompt, col_chat = st.columns([1, 1])

    with col_prompt:
        st.subheader("System prompt (%s)" % bot_name)
        edited_prompt = st.text_area(
            "Applies from the next turn. Nothing is saved until you click a Save button.",
            value=st.session_state[prompt_key],
            height=480,
            key="textarea_%s" % bot_name,
        )
        st.session_state[prompt_key] = edited_prompt
        if session is not None and edited_prompt != spec.prompt and session.prompt_override != edited_prompt:
            session.set_prompt(edited_prompt)

        if spec.source == DRAFT:
            if st.button("Save prompt to draft"):
                try:
                    save_draft(dict(spec.to_dict(), prompt=edited_prompt), DRAFTS_DIR)
                    st.success("Draft updated.")
                    st.rerun()
                except BotSpecError as exc:
                    st.error(str(exc))
        else:
            if st.button("Save diff for review"):
                path = _save_prompt_diff(bot_name, spec.prompt, edited_prompt)
                st.success("Diff written to %s — review and apply it as a normal PR." % path)
                st.code(path.read_text(), language="diff")

        if session is not None:
            with st.expander("System prompt as the model will see it"):
                st.text(session.system_prompt_preview(pill))

    with col_chat:
        st.subheader("Test conversation")
        st.caption("Rider: %s · Channel: %s" % (rider_display, channel))
        if st.button("Reset conversation"):
            st.session_state.pop("session_key", None)
            st.rerun()

        if session is None:
            st.info("Enter an API key in the sidebar, or pick the offline planner, to start.")
            return

        for turn in st.session_state["transcript"]:
            with st.chat_message(turn["role"]):
                for name in turn.get("attachments", []):
                    st.caption("📎 %s" % name)
                st.write(turn["content"])
                if turn["role"] == "assistant":
                    st.caption(turn["summary"])
                    if turn["events"]:
                        with st.expander("Trace"):
                            st.json(turn["events"])

        uploaded_files = st.file_uploader(
            "📎 Attach image or PDF",
            type=["png", "jpg", "jpeg", "pdf"],
            accept_multiple_files=True,
            key="uploader_%s" % bot_name,
        )

        user_text = st.chat_input("Type a test message…")
        if user_text or uploaded_files:
            attachments = [_attachment_from_upload(f) for f in (uploaded_files or [])]
            text = (user_text or "").strip()
            st.session_state["transcript"].append(
                {"role": "user", "content": text or "(uploaded file)", "attachments": [f.name for f in uploaded_files or []]}
            )
            try:
                result = session.send(text, pill=pill, attachments=attachments)
                reply = result.reply
                summary = "handled_by=%s · tools=%s%s%s" % (
                    reply.handled_by,
                    ", ".join(reply.metadata.get("tool_calls", [])) or "none",
                    " · ESCALATED" if reply.escalated else "",
                    " · ticket %s" % reply.ticket_id if reply.ticket_id else "",
                )
                st.session_state["transcript"].append(
                    {"role": "assistant", "content": reply.text, "summary": summary, "events": result.events}
                )
            except Exception as exc:  # surfaced, not swallowed: this is a test bench
                st.session_state["transcript"].append(
                    {"role": "assistant", "content": "Error: %s" % exc, "summary": "error", "events": []}
                )
            st.rerun()


# --- New bot mode -------------------------------------------------------------


def _new_bot_mode(catalogue: Any) -> None:
    st.subheader("New bot")
    st.caption(
        "Saves a draft spec into %s. Drafts are testable in Chat immediately and are never "
        "loaded by the deployed API." % DRAFTS_DIR
    )

    persona = st.radio("Persona", ["customer", "dealer"], horizontal=True)
    name = st.text_input("Name (snake_case)", "brakes_support")
    topic = st.text_input("Topic (snake_case; the knowledge topic and the triage topic)", "brakes")
    keywords_text = st.text_area(
        "Keywords, one per line (English plus Hindi/Hinglish as customers actually type them)",
        "brake\nbraking\nब्रेक\nbrek",
        height=120,
    )
    tools = st.multiselect(
        "Tools", sorted(PERSONA_TOOLS[persona]),
        default=[t for t in ("lookup_warranty_record", "search_knowledge", "create_support_ticket") if t in PERSONA_TOOLS[persona]],
    )
    prompt = st.text_area("Prompt", prompt_template(persona, topic or "<topic>"), height=420)

    with st.expander("Knowledge records for this topic (optional)"):
        st.caption("One record per sub-issue, in the knowledge/README.md format. Symptoms and steps one per line.")
        record_count = st.number_input("Records", min_value=0, max_value=10, value=0, step=1)
        records: List[Dict[str, Any]] = []
        for i in range(int(record_count)):
            st.markdown("**Record %d**" % (i + 1))
            records.append(
                {
                    "id": st.text_input("id", "%s-%d" % (topic or "topic", i + 1), key="kb_id_%d" % i),
                    "title": st.text_input("title", "", key="kb_title_%d" % i),
                    "symptoms": [s.strip() for s in st.text_area("symptoms", "", key="kb_sym_%d" % i).splitlines() if s.strip()],
                    "steps": [s.strip() for s in st.text_area("steps", "", key="kb_steps_%d" % i).splitlines() if s.strip()],
                    "escalate_when": st.text_input("escalate_when", "", key="kb_esc_%d" % i),
                }
            )

    col_save, col_export, col_delete = st.columns(3)
    with col_save:
        if st.button("Save draft"):
            raw = {
                "name": name.strip(),
                "persona": persona,
                "topic": topic.strip() or None,
                "keywords": [k.strip() for k in keywords_text.splitlines() if k.strip()],
                "tools": tools,
                "prompt": prompt,
            }
            try:
                path = save_draft(raw, DRAFTS_DIR)
                written = save_draft_knowledge(topic.strip(), records, DRAFTS_DIR) if records and topic.strip() else []
                st.success("Draft saved to %s%s. Switch to Chat to test it." % (path, " (+%d knowledge records)" % len(written) if written else ""))
                st.rerun()
            except (BotSpecError, KnowledgeError) as exc:
                st.error(str(exc))
    with col_export:
        if st.button("Export for review"):
            try:
                paths = export_for_review(name.strip(), DRAFTS_DIR, EXPORT_DIR)
                st.success("Exported:\n" + "\n".join("- %s" % p for p in paths))
                st.caption("Move these into bots/ and knowledge/ in the repo and open a PR.")
            except (BotSpecError, KeyError) as exc:
                st.error("Save the draft first: %s" % exc)
    with col_delete:
        if st.button("Delete draft"):
            try:
                delete_draft(name.strip(), DRAFTS_DIR)
            except (BotSpecError, KnowledgeError, OSError, yaml.YAMLError) as exc:
                st.error(str(exc))
            else:
                # st.rerun() raises to unwind the script, so it must stay out
                # of the try — otherwise it would be caught as a failure.
                st.session_state.pop("session_key", None)
                st.success("Deleted.")
                st.rerun()

    st.divider()
    st.subheader("Bots in the catalogue")
    rows = [
        {"name": s.name, "persona": s.persona, "topic": s.topic or "", "source": _source_tag(s.source),
         "keywords": ", ".join(s.keywords), "tools": ", ".join(s.tools)}
        for s in catalogue.specs
    ]
    st.dataframe(rows, use_container_width=True)


# --- main ---------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Emotorad AI — playground", layout="wide")
    st.title("Bot playground")

    try:
        catalogue = load_catalogue(DRAFTS_DIR)
    except BotSpecError as exc:
        st.error("A draft failed to load: %s\n\nFix or delete it under %s." % (exc, DRAFTS_DIR))
        return

    with st.sidebar:
        mode = st.radio("Mode", ["Chat", "New bot"], horizontal=True)
        st.divider()

    if mode == "Chat":
        _chat_mode(catalogue)
    else:
        _new_bot_mode(catalogue)


if __name__ == "__main__":
    main()
