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
convention already used elsewhere in this repo.

Tools do run. The loop mirrors `Agent.run` (agents/base.py) — same iteration cap,
same repeat-call detection — and slices tools by the selected agent's own
`TOOL_NAMES`, so a dealer agent is never offered the customer warranty table.
Every call and its result is shown inline, because "why did it say that" is the
question this page exists to answer. Reads go to the mocks; writes land in the
in-memory mock ticket/booking systems and never touch a real one. What is still
absent is the rest of `runtime.handle()` — the safety gate, the coverage
post-check and the disclosure wrapper — so this tests an agent, not the pipeline
that wraps it.
"""

from __future__ import annotations

import base64
import contextlib
import difflib
import hashlib
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
from emotorad_ai.errorcodes import load_table as load_error_codes
from emotorad_ai.guardrails import EVIDENCE_BLOCKED_MESSAGE, check_evidence
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.playground_version import CHANGELOG, PLAYGROUND_VERSION
from emotorad_ai.media import load_catalogue
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import (
    LOOKUP_ERROR_CODE,
    RAISE_INTAKE_TICKET,
    SEARCH_KNOWLEDGE,
    SEND_GUIDE_MEDIA,
    _coverage,
    build_registry,
)
from emotorad_ai.tools.oms import OMSClient, OMSConfigError, OMSNoRecord, OMSUnavailable
from emotorad_ai.tools.registry import ToolContext, ToolError, ToolRegistry, is_error
from emotorad_ai.tools.verification import (
    FIND_ACCOUNT_BY_CODE,
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    VerificationStore,
)

from emotorad_ai import video
from emotorad_ai.video import FRAME_MAX_EDGE, VIDEO_FRAMES, VIDEO_TYPES, WHISPER_MODEL  # noqa: F401  (kept for callers)

_ffmpeg_exe = video.ffmpeg_exe
_transcribe_audio = video.transcribe_audio


def _extract_audio(data_b64: str, suffix: str) -> Optional[bytes]:
    return video.extract_audio(base64.b64decode(data_b64), suffix)


def _extract_frames(data_b64: str, suffix: str, count: int = VIDEO_FRAMES) -> List[str]:
    return video.extract_frames(base64.b64decode(data_b64), suffix, count)


def _is_video(attachment: Dict[str, Any]) -> bool:
    return video.is_video(attachment.get("mime_type") or "", attachment.get("name") or "")


# FRAME_MAX_EDGE and WHISPER_MODEL are re-exported for backward compatibility
# only — nothing below this line reads them directly (VIDEO_FRAMES and
# VIDEO_TYPES do have direct uses further down). Listing them in __all__ is
# the standard way to tell pyflakes an import is a deliberate public
# re-export, not dead code.
__all__ = ["FRAME_MAX_EDGE", "VIDEO_FRAMES", "VIDEO_TYPES", "WHISPER_MODEL"]


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

# Overridable so a test can drive the page without writing into a real tester's
# chats and prompt history — which is why no test had ever submitted a message,
# and why a NameError on the submit path survived a green boot matrix.
PLAYGROUND_DIR = Path(
    os.environ.get("EMOTORAD_PLAYGROUND_DIR")
    or Path(__file__).resolve().parent.parent.parent / ".playground"
)


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
BLOB_DIR = PLAYGROUND_DIR / "attachments"


def _new_chat_id() -> str:
    """Short enough to read off the screen and quote in a bug report."""
    return "%s-%s" % (date.today().strftime("%Y%m%d"), uuid.uuid4().hex[:8])


def _blank_chat() -> Dict[str, Any]:
    return {
        "chat_id": _new_chat_id(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "turns": [],
    }


# --- attachment blobs -------------------------------------------------------
# Attachment bytes live once, in a file named for their own hash, and turns
# reference them by id. They used to be inlined as base64 in the chat JSON,
# which meant a three-screenshot turn rewrote ~4.7MB of base64 to disk on every
# subsequent turn of that conversation. Content-addressed, so the same photo
# uploaded twice is stored once.


def _blob_path(blob_id: str) -> Path:
    return BLOB_DIR / ("%s.b64" % blob_id)


def _put_blob(data_b64: str) -> str:
    blob_id = hashlib.sha256(data_b64.encode("utf-8")).hexdigest()[:32]
    path = _blob_path(blob_id)
    if not path.exists():
        BLOB_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(data_b64)
    return blob_id


def _get_blob(attachment: Dict[str, Any]) -> str:
    """Base64 for an attachment, whether it is stored inline or by reference.

    Inline `data` is still read so chats written before blobs existed keep
    working — a saved transcript is test material, not something to invalidate.
    """
    inline = attachment.get("data")
    if inline:
        return inline
    blob_id = attachment.get("blob_id")
    if not blob_id:
        return ""
    try:
        return _blob_path(blob_id).read_text()
    except OSError:
        return ""


def _externalise_attachments(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for attachment in attachments:
        record = {k: v for k, v in attachment.items() if k != "data"}
        data = attachment.get("data")
        if data:
            record["blob_id"] = _put_blob(data)
            if _is_video(attachment):
                # Sampled once, here, and stored by reference. Decoding is slow
                # and deterministic, and the alternative is re-running ffmpeg on
                # every rerun of every later turn in the conversation.
                name = attachment.get("name") or "video.mp4"
                suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ".mp4"
                record["frame_blob_ids"] = [_put_blob(f) for f in _extract_frames(data, suffix)]
                wav = _extract_audio(data, suffix)
                record["transcript"] = _transcribe_audio(wav) if wav else None
        out.append(record)
    return out


# --- chats ------------------------------------------------------------------
# One file per chat, under chats/<agent>/, plus a pointer to the open one.
# Previously this was a single chats/<agent>.json that "Start new chat"
# overwrote, which destroyed the transcript it replaced. Those transcripts are
# the test corpus: comparing how v1 and v2 of a prompt answer the same question
# is the whole point, and that is impossible if starting the v2 run deletes the
# v1 run.


def _agent_chat_dir(agent_name: str) -> Path:
    return CHAT_DIR / agent_name


def _chat_path(agent_name: str, chat_id: str) -> Path:
    return _agent_chat_dir(agent_name) / ("%s.json" % chat_id)


def _pointer_path(agent_name: str) -> Path:
    return _agent_chat_dir(agent_name) / "current.txt"


def _list_chats(agent_name: str) -> List[Dict[str, Any]]:
    """Every saved chat for this agent, newest first."""
    summaries: List[Dict[str, Any]] = []
    try:
        paths = sorted(_agent_chat_dir(agent_name).glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(loaded, dict):
            continue
        summaries.append(
            {
                "chat_id": loaded.get("chat_id") or path.stem,
                "started_at": loaded.get("started_at") or "",
                "turns": len(loaded.get("turns") or []),
            }
        )
    return sorted(summaries, key=lambda s: s["started_at"], reverse=True)


def _read_chat(agent_name: str, chat_id: str) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(_chat_path(agent_name, chat_id).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("turns"), list):
        return None
    loaded.setdefault("chat_id", chat_id)
    loaded.setdefault("started_at", "")
    return loaded


def _migrate_legacy_chat(agent_name: str) -> None:
    """Move a pre-archive chats/<agent>.json into the per-chat layout."""
    legacy = CHAT_DIR / ("%s.json" % agent_name)
    if not legacy.is_file():
        return
    try:
        loaded = json.loads(legacy.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(loaded, dict) or not isinstance(loaded.get("turns"), list):
        return
    loaded.setdefault("chat_id", _new_chat_id())
    loaded.setdefault("started_at", "")
    loaded["turns"] = [
        dict(turn, attachments=_externalise_attachments(turn.get("attachments") or []))
        for turn in loaded["turns"]
    ]
    _save_chat(agent_name, loaded)
    try:
        legacy.unlink()
    except OSError:
        pass


def _load_chat(agent_name: str) -> Dict[str, Any]:
    """The open chat for this agent, so a refresh does not end the conversation."""
    _migrate_legacy_chat(agent_name)
    try:
        chat_id = _pointer_path(agent_name).read_text().strip()
    except OSError:
        chat_id = ""
    if chat_id:
        existing = _read_chat(agent_name, chat_id)
        if existing is not None:
            return existing
    recent = _list_chats(agent_name)
    if recent:
        restored = _read_chat(agent_name, recent[0]["chat_id"])
        if restored is not None:
            return restored
    return _blank_chat()


def _save_chat(agent_name: str, chat: Dict[str, Any]) -> None:
    try:
        _agent_chat_dir(agent_name).mkdir(parents=True, exist_ok=True)
        _chat_path(agent_name, chat["chat_id"]).write_text(json.dumps(chat, indent=2))
        _pointer_path(agent_name).write_text(chat["chat_id"])
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
        "kind": (
            "image"
            if mime_type.startswith("image/")
            else "video"
            if mime_type.startswith("video/")
            else "document"
        ),
        "mime_type": mime_type,
        "data": base64.b64encode(content).decode("utf-8"),
    }


def _attachment_blocks(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for attachment in attachments:
        mime_type = attachment.get("mime_type") or "application/octet-stream"
        data = _get_blob(attachment)
        if not data:
            continue

        if _is_video(attachment):
            name = attachment.get("name") or "video"
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ".mp4"
            # The transcript is emitted first and independently of the frames.
            # Audio and video fail separately — a stream we cannot decode often
            # still carries usable sound — and on a motor fault the customer's own
            # sentence is worth more than the pictures. Emitting it inside the
            # frames branch discarded it exactly when it mattered most.
            transcript = attachment.get("transcript")
            if transcript and transcript.get("text"):
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            "[What the customer says in the video '%s' (transcribed speech, "
                            "detected language %s): \"%s\"  — this is their narration only. "
                            "It is not a description of any sound the bike makes; you cannot "
                            "hear the bike. If the fault is something they are asking you to "
                            "listen to, say you cannot hear it and ask them to describe it.]"
                            % (name, transcript.get("language", "unknown"), transcript["text"])
                        ),
                    }
                )

            cached = attachment.get("frame_blob_ids")
            if cached is not None:
                frames = [_blob_path(b).read_text() for b in cached if _blob_path(b).exists()]
            else:
                frames = _extract_frames(data, suffix)
            if not frames:
                # Never let an unreadable video look like one that was watched.
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            "[The customer sent a video (%s) that could not be read here. Do "
                            "not describe or assess it. Say you could not open it and ask for "
                            "a photo of the same thing instead.]" % name
                        ),
                    }
                )
                continue
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        "[%d still frames sampled evenly from the customer's video '%s', in "
                        "order. You are seeing stills, not the video: judge only what is "
                        "visible in them, and say so if the moment that matters falls between "
                        "frames.]" % (len(frames), name)
                    ),
                }
            )
            for frame in frames:
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": frame},
                    }
                )
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


# Matches config.Settings.max_agent_iterations. A cap exists because a model that
# keeps asking for tools without answering will otherwise loop on a customer's time.
MAX_TOOL_ITERATIONS = 6

# The playground used to hardcode 1024, which is small enough to be a bug: a
# knowledge passage plus a few screenshots can exhaust it before the model writes
# a single word, and the turn then renders as an empty bubble. Production's
# default (config.Settings.max_tokens) is 16000; match it and let it be tuned.
DEFAULT_MAX_TOKENS = 16000

# Writes are backstopped with a derived idempotency key, exactly as Agent.run does:
# the registry refuses a write without one, and a model that forgot should not
# surface to the tester as a broken ticket tool.
_WRITE_KEY_FIELD = "idempotency_key"


def _live_warranty_source(client: Any) -> Any:
    """Registered bikes from the real OMS, mapped onto the tool's own outcomes.

    The mapping is the point. `no rows` is a customer who never registered and
    routes to Late Warranty Registration; everything else — a rejected key, a
    timeout, a 500 — is our outage and must say so. Reporting an outage as "no
    record" would tell a registered owner to re-register.
    """

    def source(phone: str) -> Optional[List[Dict[str, Any]]]:
        try:
            return client.get_warranties_by_mobile(phone)
        except OMSNoRecord:
            return None
        except (OMSUnavailable, OMSConfigError) as exc:
            raise ToolError(
                "oms_unavailable",
                "The warranty system is not responding (%s)." % type(exc).__name__,
                retryable=True,
            )

    return source


def _live_account_finder(client: Any) -> Any:
    """Order or invoice code -> the phone the warranty is registered on, or None.

    Returns the number to the *registry*, never to the model: the tool that uses
    this hands back only a masked form. An order code is printed on paper, so
    treating it as identification would make an invoice enough to read someone's
    contact details.
    """

    def finder(code: str) -> Optional[str]:
        try:
            rows = client.get_orders_by_code(code)
        except (OMSNoRecord, OMSConfigError):
            return None
        except OMSUnavailable:
            raise ToolError(
                "oms_unavailable", "The order system is not responding.", retryable=True
            )
        for row in rows:
            phone = (row.get("mobile") or "").strip()
            if phone and phone not in ("None", "null"):
                return phone if phone.startswith("+") else "+91%s" % phone.lstrip("0")
        return None

    return finder


# Tools withheld from an agent in the playground only, never in production.
#
# battery_support: `search_knowledge` is suppressed while the battery flow is
# being authored in the prompt rather than in knowledge records. The nine shipped
# records are the R1 skeleton and now hold *worse* content than the prompt does —
# SOC check, charger-LED branch, revival process — so a search would return
# thinner answers than the model already has, from a second source of truth that
# is quietly diverging. One source while the content moves.
#
# This comes out when the settled sections are lifted into records; the guide
# photos on Cloudinary reach customers only through a retrieved record, so
# nothing renders until it does. Deliberate, not a regression.
# Empty again as of the first knowledge migration. search_knowledge was withheld
# from battery while its flows lived in the prompt and the records were the R1
# skeleton — searching would have returned thinner content than the model already
# had, from a second source of truth quietly diverging from the first. The Doodle
# flow now lives *only* in a record, so the tool has to be reachable.
PLAYGROUND_SUPPRESSED_TOOLS: Dict[str, tuple] = {}


def _playground_tool_names(
    agent_name: str, module: Any, registry: ToolRegistry, live: bool
) -> List[str]:
    """What the model is offered on this page — the agent's slice, adjusted."""
    names = _live_tool_names(module, registry) if live else list(module.TOOL_NAMES)
    # Guide pictures are offered in every rider mode: they are how the bot points
    # at a button, and that is not a live-identity concern.
    for extra in (SEND_GUIDE_MEDIA, LOOKUP_ERROR_CODE):
        if extra in registry.specs and extra not in names:
            names.append(extra)
    withheld = PLAYGROUND_SUPPRESSED_TOOLS.get(agent_name, ())
    return [n for n in names if n not in withheld]


def _live_tool_names(module: Any, registry: ToolRegistry) -> List[str]:
    """The agent's own tools, plus the identity tools the live channel needs.

    Production never needs these: identity is resolved upstream by the channel,
    so `battery_support.TOOL_NAMES` is right to omit them. Live mode is the one
    place the agent has to establish identity itself, and slicing strictly by
    TOOL_NAMES left the verification tools registered but unreachable — the model
    was told to ask for a phone number and then had no way to do anything with
    it. Added here rather than in the agent so production stays as designed.
    """
    names = list(module.TOOL_NAMES)
    for extra in (
        REQUEST_IDENTITY_VERIFICATION,
        VERIFY_IDENTITY,
        FIND_ACCOUNT_BY_CODE,
        RAISE_INTAKE_TICKET,
        SEND_GUIDE_MEDIA,
    ):
        if extra in registry.specs and extra not in names:
            names.append(extra)
    return names


def _live_bikes(agent_name: str, verification: "VerificationStore", chat_id: str) -> List[Dict[str, Any]]:
    """The bikes on this conversation, read now rather than when the tool was wired.

    A customer who verifies and then asks about an error code does both inside one
    assistant turn. Bikes captured at wiring time are still empty at that point,
    and the lookup fails with no_bike_resolved on the very turn the identity
    arrived — the same mistake the tool context made before it took a factory.
    """
    client = OMSClient()
    lookup = build_registry(
        today=date.today(),
        warranty_source=_live_warranty_source(client),
        account_finder=_live_account_finder(client),
    )
    return _resolved_for_live(agent_name, verification.verified_phone(chat_id), lookup).bikes


def _resolved_for_live(agent_name: str, verified_phone: Optional[str], registry: ToolRegistry) -> ResolvedIdentity:
    """Anonymous until the customer proves a number, then hydrated from the OMS.

    This is the whole point of the live mode: nothing is known at "hi". The
    disclosure gate (`Identity.may_disclose`) stays shut on its own, in code,
    until `verify_identity` succeeds — no prompt wording can open it early.
    """
    if not verified_phone:
        return ResolvedIdentity(
            persona="customer", method="unverified", identity=Identity(strength=ANONYMOUS)
        )
    identity = Identity(strength=VERIFIED, phone=verified_phone)
    message = InboundMessage(
        conversation_id="hydrate",
        persona="customer",
        identity=identity,
        channel="website_chat",
        message_text="",
    )
    return IdentityResolver(registry).hydrate(message)


def _tool_context(conversation_id: str, resolved: ResolvedIdentity) -> ToolContext:
    """The trusted facts tools are given behind the model's back.

    Same construction as `Agent.run`. `dealer_id` comes off the resolved profile
    so the dealer tools scope to the right account — the model never supplies it.
    """
    profile = resolved.profile or {}
    return ToolContext(
        conversation_id=conversation_id,
        phone=resolved.identity.phone,
        cluster_id=resolved.cluster_id,
        customer_id=resolved.customer_id,
        dealer_id=profile.get("dealer_id"),
    )


def _with_idempotency_key(
    registry: ToolRegistry, name: str, arguments: Dict[str, Any], conversation_id: str, iteration: int
) -> Dict[str, Any]:
    arguments = dict(arguments or {})
    spec = registry.specs.get(name)
    if spec is None or not spec.write or arguments.get(_WRITE_KEY_FIELD):
        return arguments
    payload = json.dumps(arguments, sort_keys=True, default=str)
    arguments[_WRITE_KEY_FIELD] = hashlib.sha256(
        ("%s|%s|%s|%d" % (conversation_id, name, payload, iteration)).encode()
    ).hexdigest()[:32]
    return arguments


def _run_agent_turn(
    client: Any,
    model_id: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    registry: ToolRegistry,
    tool_names: Any,
    context_factory: Any,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Dict[str, Any]:
    """Ask the model, run whatever tools it asks for, ask again. Return the reply.

    This mirrors `Agent.run` (agents/base.py) rather than inventing a second
    conversational loop: same iteration cap, same repeat-call detection, same
    "all results for one assistant turn go back in a single user message" rule —
    splitting them teaches the model to stop batching its calls.

    What it deliberately does *not* mirror: the production loop's observability,
    the coverage post-check and the disclosure wrapper, which live in
    `runtime.handle()`. This is a prompt-tuning harness, so what it adds instead
    is a trace of every call and result for the tester to read.

    Tools are sliced by the agent's own `TOOL_NAMES`, which is what keeps persona
    isolation honest here: `lookup_warranty_record` is simply absent from the
    dealer slice, so a dealer agent is never even offered the customer's
    warranty table.
    """
    # Re-derived before every tool call, not once per turn. A model that verifies
    # an identity and then immediately reads the warranty record does both inside
    # one assistant turn, and a context captured up front still says "anonymous" —
    # so the lookup failed with missing_identity on the very turn the customer
    # had just proved who they were.
    context = context_factory() if callable(context_factory) else context_factory

    tools = registry.schemas_for([name for name in tool_names if name in registry.specs])
    trace: List[Dict[str, Any]] = []
    outbound_media: List[Dict[str, Any]] = []
    # signature -> did it succeed. A model repeating a call that *worked* has
    # nothing new to learn and is stuck. A model repeating one that *errored* is
    # retrying, which is what it should do — the first attempt may have failed
    # for a reason that has since changed, and killing the turn there throws away
    # the successful retry along with the whole reply.
    call_outcomes: Dict[str, bool] = {}
    retried: set = set()

    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
            tools=tools,
        )
        api_content = [block.model_dump(exclude_none=True) for block in response.content]
        messages.append({"role": "assistant", "content": api_content})

        stop_reason = getattr(response, "stop_reason", None)
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            text = "\n".join(block.text for block in response.content if block.type == "text")
            if not text.strip():
                # A blank bubble tells the tester nothing. This happens for real,
                # and for two unrelated reasons, so say which one — the advice for
                # the first is useless for the second and sends people to the
                # wrong knob.
                if stop_reason == "max_tokens":
                    text = (
                        "(No text returned — the output cap was hit before the model wrote "
                        "anything. Raise it in the sidebar; a long knowledge passage plus "
                        "images can exhaust a small one.)"
                    )
                else:
                    text = (
                        "(No text returned — stop_reason=%s, nothing truncated. The model "
                        "ended its turn having written nothing, usually after its tool calls "
                        "came back. In production this hands over to a human rather than "
                        "sending an empty message.)" % stop_reason
                    )
            elif stop_reason == "max_tokens":
                text += "\n\n(Truncated — hit the output token cap. Raise it in the sidebar.)"
            return {
                "text": text,
                "trace": trace,
                "media": outbound_media,
                "iterations": iteration,
                "stop_reason": stop_reason,
            }

        results: List[Dict[str, Any]] = []
        for tool_use in tool_uses:
            arguments = _with_idempotency_key(
                registry, tool_use.name, dict(tool_use.input or {}), context.conversation_id, iteration
            )
            signature = "%s|%s" % (tool_use.name, json.dumps(arguments, sort_keys=True, default=str))
            previously_succeeded = call_outcomes.get(signature)
            stuck = previously_succeeded is True or (
                previously_succeeded is False and signature in retried
            )
            if previously_succeeded is False:
                retried.add(signature)
            if stuck:
                # Either it already worked, or it has now failed the same way twice.
                trace.append({"tool": tool_use.name, "arguments": arguments, "result": None, "stuck": True})
                return {
                    "text": (
                        "(Stopped: %s was called with identical arguments twice with no new "
                        "result. In production this hands over to a human.)" % tool_use.name
                    ),
                    "trace": trace,
                    "media": outbound_media,
                    "iterations": iteration,
                }
            if callable(context_factory):
                context = context_factory()
            envelope = registry.call(tool_use.name, arguments, context)
            call_outcomes[signature] = not is_error(envelope)
            trace.append({"tool": tool_use.name, "arguments": arguments, "result": envelope})

            # One source, and it is the model: `send_guide_media` returns what it
            # asked to show. Media cited by a retrieved knowledge record used to be
            # attached here too, which predates that tool by a month and is what
            # produced the transcripts where a customer was sent the revival clip
            # while still on the SOC-button step. Mirrors agents/base.py.
            if not is_error(envelope):
                for found in (envelope.get("data") or {}).get("media", []) or []:
                    if found not in outbound_media:
                        outbound_media.append(found)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(envelope, default=str),
                    "is_error": is_error(envelope),
                }
            )
        messages.append({"role": "user", "content": results})

    return {
        "text": (
            "(Stopped after %d tool rounds without an answer. In production this hands over "
            "to a human.)" % MAX_TOOL_ITERATIONS
        ),
        "trace": trace,
        "media": outbound_media,
        "iterations": MAX_TOOL_ITERATIONS,
    }


# --- test evidence -----------------------------------------------------------
# Typing `/proof` stands in for uploading a photo or video, so a flow that gates
# on evidence can be walked end to end without producing a real file at every
# step.
#
# It lives here, in the harness, and deliberately not in the prompt. A bypass
# phrase in the prompt would be one prompt-extraction away from being a way to
# open a warranty claim with no evidence; it would have to be stripped from every
# future version before shipping; and — worst for its actual purpose — it would
# mean testing a bot that has a bypass when the production one will not.
#
# So nothing is bypassed. The turn genuinely carries evidence, the model receives
# it by the same path a real upload takes, and the gate is satisfied because it
# has been met.
PROOF_COMMAND = "/proof"


def _proof_note(description: str) -> str:
    return (
        "[Evidence attached by the customer: %s. This is a test fixture standing in for a "
        "real photo or video — treat it as evidence received for the step you asked about, "
        "and carry on.]" % (description.strip() or "a photo or video of the step just asked about")
    )


def _split_proof(user_text: Optional[str]):
    """Pull a `/proof …` marker off a message. Returns (text, note or None).

    Anything after the command describes what the evidence shows, so a tester can
    steer the conversation — "/proof green light, no red" — without opening a
    camera. Only a message that *starts* with the command counts, so a customer
    saying "the /proof is in the pudding" is left alone.
    """
    if not user_text:
        return user_text, None
    stripped = user_text.strip()
    if not stripped.lower().startswith(PROOF_COMMAND):
        return user_text, None
    remainder = stripped[len(PROOF_COMMAND):].strip()
    return (remainder or "Here you go."), _proof_note(remainder)


def _should_submit_chat(user_text: Optional[str], pending_files: List[Any]) -> bool:
    """Only send after the user explicitly submits a chat turn.

    Uploading a file should not trigger a side-effect by itself; the file can stay
    queued until the user types a message or otherwise confirms the send action.
    """
    return user_text is not None and (bool(user_text.strip()) or bool(pending_files))


@contextlib.contextmanager
def _hidden():
    """Swallow a whole column when the view does not include it.

    Streamlit has no "do not draw this"; the alternative is duplicating the
    body of each column behind an `if`, which is how these two get out of step.
    """
    placeholder = st.empty()
    with placeholder.container():
        yield
    placeholder.empty()


# Streamlit's defaults are built for dashboards: ~6rem of top padding, generous
# gaps, an h1 sized for a landing page. On a page whose whole job is reading a
# conversation, that is several hundred pixels of nothing before the first
# message. This trims the frame; it changes no behaviour.
_PAGE_CSS = """
<style>
  .block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1400px; }
  [data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }
  /* Tighter vertical rhythm between elements. */
  [data-testid="stVerticalBlock"] { gap: 0.55rem; }
  /* Chat bubbles: less padding, and a visible edge between speakers. */
  [data-testid="stChatMessage"] { padding: 0.55rem 0.85rem; border-radius: 10px; }
  /* Tool traces are reference, not content — quieter until opened. */
  [data-testid="stExpander"] summary { font-size: 0.86rem; }
  /* The status line under the heading. */
  .pg-status { color: #5f6368; font-size: 0.86rem; }
  .pg-status code { background: #f1f3f4; padding: 0.05rem 0.35rem; border-radius: 4px; }
</style>
"""


def main() -> None:
    st.set_page_config(page_title="Emotorad AI — prompt playground", layout="wide")
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)

    header, control = st.columns([2, 1.6])
    with header:
        st.markdown(
            "#### Prompt-tuning playground &nbsp;"
            "<span style='font-size:0.72rem;color:#9aa0a6;font-weight:400'>build v%s</span>"
            % PLAYGROUND_VERSION,
            unsafe_allow_html=True,
        )

    with st.sidebar:
        st.subheader("Setup")
        agent_name = st.selectbox("Agent", list(AGENT_MODULES.keys()))
        model_label = st.selectbox("Model", list(MODELS.keys()))
        model_id = MODELS[model_label]

        settings = st.expander("Model settings", expanded=not st.session_state.get("_key_set"))
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            # On staging the config store exports the key into the environment,
            # so nobody pastes a shared key into a browser. The field stays for
            # local runs with no key set.
            settings.caption("Anthropic API key: from environment")
            api_key = env_key
        else:
            api_key = settings.text_input(
                "Anthropic API key",
                value="",
                type="password",
                help="Session-only — never written to disk. Or set ANTHROPIC_API_KEY before starting.",
            )
        st.session_state["_key_set"] = bool(api_key)
        max_tokens = settings.number_input(
            "Max output tokens",
            min_value=256,
            max_value=32000,
            value=DEFAULT_MAX_TOKENS,
            step=1000,
            help=(
                "Per model reply. Too low and a turn can end with no text at all — a long "
                "knowledge passage plus screenshots eats the budget before the model writes."
            ),
        )

        module = importlib.import_module(AGENT_MODULES[agent_name])

        st.divider()
        st.subheader("Rider")
        rider_mode = st.radio(
            "Rider source", ["Preset rider", "Custom rider", "Live customer"], horizontal=True
        )

        if rider_mode == "Live customer":
            st.caption(
                "Nothing is known at 'hi'. The bot has to ask for a number, send a code and "
                "verify it before it may name a bike — exactly as a real website chat would. "
                "Bikes come from the live OMS."
            )
            # Resolution needs the chat id (verification is per conversation), and
            # that is not known until the chat loads below.
            resolved = None  # type: ignore[assignment]
            persona, channel = "customer", "website_chat"
            rider_display = "Live customer"
        elif rider_mode == "Preset rider":
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

        st.divider()
        with st.expander("Playground build v%s — what changed" % PLAYGROUND_VERSION):
            # A saved transcript is the product of a prompt version *and* the
            # build that ran it. "It did not send the photo" means something
            # different on 0.4 than on 0.7.
            for version, released, summary in CHANGELOG:
                st.markdown("**v%s** · %s  \n%s" % (version, released, summary))

    session_key = "chat_%s" % agent_name
    textarea_key = "textarea_%s" % agent_name
    pending_load_key = "pending_prompt_load_%s" % agent_name

    # Restoring an old version has to land here, before the textarea exists:
    # Streamlit refuses writes to a widget's key once that widget is built.
    if pending_load_key in st.session_state:
        st.session_state[textarea_key] = st.session_state.pop(pending_load_key)
    base_version_key = "editor_base_version_%s" % agent_name
    if textarea_key not in st.session_state:
        st.session_state[textarea_key] = _default_prompt(agent_name, module)
        seeded = _latest_prompt_version(agent_name)
        st.session_state[base_version_key] = seeded["version"] if seeded else 0
    if session_key not in st.session_state:
        st.session_state[session_key] = _load_chat(agent_name)

    chat = st.session_state[session_key]

    # One registry per run, shared by identity hydration and the tool loop, so the
    # verification a tool records is the verification the next turn reads.
    verification: VerificationStore = st.session_state.setdefault("verification_store", VerificationStore())
    # Which guide pictures each conversation has already received. Session-scoped
    # so it survives across turns; a reload starts it empty, which only means a
    # picture may be sent once more than strictly needed.
    sent_media: Dict[str, set] = st.session_state.setdefault("sent_guide_media", {})
    if rider_mode == "Live customer":
        client = OMSClient()
        registry = build_registry(
            today=date.today(),
            verification=verification,
            warranty_source=_live_warranty_source(client),
            account_finder=_live_account_finder(client),
            guide_media=load_catalogue(),
            sent_media=sent_media,
            error_codes=load_error_codes(),
            owned_bikes=lambda: _live_bikes(agent_name, verification, chat["chat_id"]),
            knowledge_bike=lambda: (_live_bikes(agent_name, verification, chat["chat_id"]) or [{}])[0],
        )
        verified_phone = verification.verified_phone(chat["chat_id"])
        resolved = _resolved_for_live(agent_name, verified_phone, registry)
        rider_display = (
            "Live — verified %s" % verified_phone if verified_phone else "Live — not yet verified"
        )
    else:
        registry = build_registry(
            today=date.today(),
            guide_media=load_catalogue(),
            sent_media=sent_media,
            error_codes=load_error_codes(),
            owned_bikes=resolved.bikes,
            knowledge_bike=(resolved.bikes or [{}])[0],
        )

    # A radio rather than st.segmented_control, which looks tidier but is not
    # readable by Streamlit's own AppTest in 1.50 — it iterates the label's
    # characters and raises. Losing the boot matrix, which has caught real bugs,
    # to decorate a control would be a bad trade.
    with control:
        view = st.radio(
            "View",
            ["Chat", "Side by side", "Prompt"],
            horizontal=True,
            label_visibility="collapsed",
            key="view_mode",
        )

    if view == "Side by side":
        col_prompt, col_chat = st.columns([1, 1])
    else:
        col_prompt, col_chat = st.container(), st.container()
    show_prompt = view in ("Side by side", "Prompt")
    show_chat = view in ("Side by side", "Chat")

    with col_prompt if show_prompt else _hidden():
        st.subheader("System prompt (%s)" % agent_name)
        edited_prompt = st.text_area(
            "Edit, then Save version — the newest version is what new chats start from",
            height=500,
            key=textarea_key,
        )

        # A version can arrive from outside this browser tab —
        # scripts/publish_prompt.py, or a second window. Streamlit will not
        # notice on its own, and silently replacing an unsaved draft with one
        # published underneath the tester would be worse than making them click.
        newest = _latest_prompt_version(agent_name)
        base_version = st.session_state.get(base_version_key, 0)
        if newest and newest["version"] > base_version and newest["text"] != edited_prompt:
            st.warning(
                "**v%d was published** (%s) while this editor was open, and it is not what "
                "you have here. Your unsaved text is untouched until you load it."
                % (newest["version"], newest["saved_at"])
            )
            if st.button("Load v%d into the editor" % newest["version"], type="primary"):
                st.session_state[pending_load_key] = newest["text"]
                st.session_state[base_version_key] = newest["version"]
                st.rerun()

        save_col, diff_col = st.columns([1, 1])
        with save_col:
            if st.button("💾 Save prompt version", type="primary", width="stretch"):
                current = _latest_prompt_version(agent_name)
                if current is not None and current["text"] == edited_prompt:
                    st.info("No changes since v%d." % current["version"])
                else:
                    record = _save_prompt_version(agent_name, edited_prompt)
                    st.session_state[base_version_key] = record["version"]
                    st.success("Saved v%d — new chats now start from this." % record["version"])
        with diff_col:
            if st.button("Save diff for review", width="stretch"):
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
                    st.session_state[base_version_key] = chosen["version"]
                    st.rerun()
                st.code(chosen["text"])

    with col_chat if show_chat else _hidden():
        head_left, head_right = st.columns([3, 1])
        with head_left:
            # One line, not four. Everything a tester needs to know about *which*
            # conversation this is, in the order they ask it.
            st.markdown(
                "<div class='pg-status'><b>%s</b> &nbsp;·&nbsp; prompt <code>%s</code>"
                " &nbsp;·&nbsp; <code>%s</code> &nbsp;·&nbsp; %d turns</div>"
                % (
                    rider_display,
                    _prompt_version_label(agent_name, edited_prompt),
                    chat["chat_id"],
                    len(chat["turns"]),
                ),
                unsafe_allow_html=True,
            )
        with head_right:
            if st.button("➕ Start new chat", width="stretch"):
                # The chat being replaced stays on disk under its own id — it is
                # test material, and the whole point of prompt versions is being
                # able to compare a v1 run against a v2 run of the same questions.
                st.session_state[session_key] = _blank_chat()
                _save_chat(agent_name, st.session_state[session_key])
                st.rerun()

        if rider_mode == "Live customer":
            # Stands in for the SMS. Shown to the tester only — the model never
            # receives it, which is the property test_verification asserts.
            # In the sidebar with the other controls: it is something the tester
            # acts on, not part of the conversation being read.
            pending_code = verification.pending_code(chat["chat_id"])
            if verified_phone:
                st.sidebar.success("Verified as %s — the bot may now name bikes and coverage." % verified_phone)
            elif pending_code:
                st.sidebar.info(
                    "📱 SMS code for this conversation: **%s** — type it into the chat as the "
                    "customer would. %d attempt(s) left."
                    % (pending_code, verification.attempts_left(chat["chat_id"]))
                )
            else:
                st.sidebar.caption("Anonymous — no bike or coverage may be named until verified.")
            if (verified_phone or pending_code) and st.sidebar.button("Reset verification"):
                verification.reset(chat["chat_id"])
                st.rerun()

        saved_chats = _list_chats(agent_name)
        if len(saved_chats) > 1:
            with st.sidebar.expander("Past chats — %d saved" % len(saved_chats)):
                labels = [
                    "%s · %d turns%s"
                    % (c["chat_id"], c["turns"], "  ← open" if c["chat_id"] == chat["chat_id"] else "")
                    for c in saved_chats
                ]
                picked = st.selectbox("Reopen a chat", labels, key="chat_pick_%s" % agent_name)
                chosen = saved_chats[labels.index(picked)]
                if chosen["chat_id"] != chat["chat_id"] and st.button(
                    "Reopen %s" % chosen["chat_id"], key="chat_open_%s" % agent_name
                ):
                    reopened = _read_chat(agent_name, chosen["chat_id"])
                    if reopened is not None:
                        st.session_state[session_key] = reopened
                        _save_chat(agent_name, reopened)
                        st.rerun()

        if not chat["turns"]:
            # A blank page under an input tells a tester nothing about where to
            # start, and the useful openers differ by rider mode.
            st.info(
                "**Nothing sent yet.** Open as a customer would — "
                + (
                    "`hi`, then a symptom. The bot knows nothing until it verifies a number."
                    if rider_mode == "Live customer"
                    else "`my cycle is not turning on`, or name a display code like `E-07`."
                )
                + "  \nAttach with 📎, or type `/proof` to stand in for a photo."
            )

        for turn_number, turn in enumerate(chat["turns"]):
            with st.chat_message(turn["role"]):
                # Numbered so a turn can be pointed at — "look at 15" — instead of
                # described, but small: it is a reference, not content.
                st.markdown(
                    "<span style='color:#9aa0a6;font-size:0.72rem'>#%d</span>" % turn_number,
                    unsafe_allow_html=True,
                )
                attachments = turn.get("attachments") or []
                if attachments:
                    for attachment in attachments:
                        if _is_video(attachment):
                            n = len(attachment.get("frame_blob_ids") or [])
                            said = (attachment.get("transcript") or {}).get("text")
                            st.caption(
                                "🎬 %s — %s%s"
                                % (
                                    attachment["name"],
                                    "%d frames sent as stills" % n if n else "could not be read",
                                    " · speech transcribed" if said else " · no speech found",
                                )
                            )
                            if said:
                                st.caption("🗣️ “%s”" % said)
                        else:
                            st.caption("📎 %s" % attachment["name"])
                # Shown before the reply, in the order they happened: the reply
                # only makes sense against what the tools actually returned, and
                # "why did it say that" is the whole question when tuning.
                for call in turn.get("tool_calls") or []:
                    if call.get("stuck"):
                        st.caption("🔁 %s — asked twice with the same arguments" % call["tool"])
                        continue
                    envelope = call.get("result") or {}
                    failed = is_error(envelope)
                    label = "%s %s" % ("🔴" if failed else "🔧", call["tool"])
                    with st.expander(label, expanded=failed):
                        if call.get("arguments"):
                            st.caption("arguments")
                            st.json(call["arguments"])
                        st.caption("result")
                        st.json(envelope)

                for call in turn.get("tool_calls") or []:
                    data = (call.get("result") or {}).get("data") or {}
                    if call["tool"] == LOOKUP_ERROR_CODE and data.get("technician_chain"):
                        # Behind a control, not in the reply. The chain is written
                        # for someone holding a spare display; a customer who wants
                        # the detail can open it, and one who does not is not handed
                        # a parts list they cannot act on.
                        with st.expander("🔧 Technical breakdown"):
                            st.write(data["technician_chain"])
                            if data.get("verification"):
                                st.caption("How it is confirmed: %s" % data["verification"])

                if turn.get("proof_note"):
                    # Marked as a fixture so it is never mistaken, in a saved
                    # transcript, for something the customer actually sent.
                    st.caption("🧪 test evidence (not a real upload)")
                # Guide media comes BEFORE the reply. The model writes as though
                # the customer can already see it — "press that button" — and a
                # picture underneath makes the sentence refer to nothing. On
                # WhatsApp the two arrive as separate messages, so the order here
                # is the order the customer meets them in.
                for item in turn.get("media") or []:
                    if item.get("unresolved"):
                        st.warning(
                            "🖼️ Guide media for this step could not be shown — %s"
                            % item.get("reason", "unresolved")
                        )
                        continue
                    if item["kind"] == "video":
                        st.video(item["url"])
                    else:
                        st.image(item["url"], width="stretch")
                    if item.get("caption"):
                        st.caption(item["caption"])

                st.write(turn["content"])

        st.caption(
            "📎 attaches a photo, video or PDF. `%s` — or `%s green light, no red` — stands in "
            "for one without making a file." % (PROOF_COMMAND, PROOF_COMMAND)
        )
        submission = st.chat_input(
            "Message as the customer…",
            accept_file="multiple",
            file_type=["png", "jpg", "jpeg", "pdf"] + list(VIDEO_TYPES),
        )
        # Files ride in the chat input now rather than a dropzone above it. That
        # is closer to what a customer's chat looks like, and it retires the
        # uploader-nonce workaround: this widget clears its own files on submit,
        # so a sent photo cannot re-attach itself to the next message.
        user_text = submission.text if submission is not None else None
        pending_files = list(submission.files) if submission is not None else []
        if _should_submit_chat(user_text, pending_files):
            user_text, proof_note = _split_proof(user_text)
            attachments = [_serialise_uploaded_file(file) for file in pending_files]
            chat["turns"].append({
                "role": "user",
                "content": user_text.strip() if user_text else "(Uploaded file(s) for review)",
                "proof_note": proof_note,
                # Bytes go to the blob store; the turn keeps only a reference.
                "attachments": _externalise_attachments(attachments),
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
                    if turn.get("proof_note"):
                        blocks.append({"type": "text", "text": turn["proof_note"]})
                    for attachment in turn.get("attachments") or []:
                        blocks.extend(_attachment_blocks([attachment]))
                    if not blocks:
                        # The API rejects an empty content list on any message but
                        # the last, so a turn that produced no text — the blank
                        # replies the old 1024-token cap caused — would make every
                        # later message in that chat fail. Drop it instead of
                        # letting one bad turn condemn the whole transcript.
                        continue
                    anthropic_messages.append({"role": turn["role"], "content": blocks})

                # Reads may be live; writes always land in the in-memory mocks,
                # never a real system — tuning a prompt must not create real Zoho
                # tickets or book real service slots.
                trace: List[Dict[str, Any]] = []
                guide_media: List[Dict[str, Any]] = []
                try:
                    with st.spinner("Thinking, and calling tools…"):
                        outcome = _run_agent_turn(
                            client,
                            model_id,
                            system_prompt,
                            anthropic_messages,
                            registry,
                            _playground_tool_names(
                                agent_name, module, registry, rider_mode == "Live customer"
                            ),
                            (
                                (lambda: _tool_context(chat["chat_id"], _resolved_for_live(
                                    agent_name, verification.verified_phone(chat["chat_id"]), registry)))
                                if rider_mode == "Live customer"
                                else (lambda: _tool_context(chat["chat_id"], resolved))
                            ),
                            max_tokens=int(max_tokens),
                        )
                    text, trace = outcome["text"], outcome["trace"]
                    guide_media = outcome.get("media") or []

                    # The same post-check runtime.handle() runs, so the guardrail
                    # is testable here rather than only in production. Evidence
                    # counts for the whole conversation, including a /proof turn.
                    evidence_seen = any(
                        (t.get("attachments") or t.get("proof_note"))
                        for t in chat["turns"]
                        if t["role"] == "user"
                    )
                    verdict = check_evidence(text, evidence_seen)
                    if verdict.blocked:
                        blocked_text = text
                        text = EVIDENCE_BLOCKED_MESSAGE
                        trace = list(trace) + [{
                            "tool": "guardrail:evidence_post_check",
                            "arguments": {"matched": verdict.matched},
                            "result": {"error": {
                                "code": verdict.reason,
                                "message": "Blocked in code, not by the prompt. The reply concluded "
                                           "a fault with no photo or video anywhere in this "
                                           "conversation. Suppressed text: %s" % blocked_text,
                                "retryable": False,
                            }},
                        }]
                except anthropic.APIStatusError as exc:
                    text = "API error: %s" % exc.message
                chat["turns"].append(
                    {"role": "assistant", "content": text, "tool_calls": trace, "media": guide_media}
                )

            _save_chat(agent_name, chat)
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
