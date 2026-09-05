# src/emotorad_ai/playground_runtime.py
"""The playground's runtime: production's `Runtime`, driven per session.

What differs from `api.py` is exactly three inputs — which model client, which
rider, and which catalogue (drafts included) — and nothing in between. Triage,
tools, the safety and coverage guardrails and the disclosure all run, because a
prompt-only preview that skips them is a plausible-looking wrong answer.

Streamlit-free on purpose, so all of this is unit-testable.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .agents.base import Agent
from .agents.generic import definition_from_spec
from .bots import BUILTIN, BotCatalogue, BotSpec
from .config import Settings
from .contract import Attachment, InboundMessage, Reply, new_conversation_id
from .identity import ResolvedIdentity, StaticResolver
from .knowledge import KnowledgeBase, KnowledgeError, load_records
from .observability import EventLog
from .runtime import Runtime
from .tools.mocks import build_registry

# What the trace panel shows per turn: what the platform decided, not the prose.
TRACE_EVENTS = ("routed", "classification", "guardrail_triggered", "tool_call", "escalation", "llm_turn")


def knowledge_base_with_drafts(drafts_dir: Optional[Path]) -> KnowledgeBase:
    """Published records plus whatever the drafts directory holds.

    A draft may not reuse a published id: it would silently double the record,
    and the copy that retrieves is whichever sorts first.
    """
    records = load_records()
    extra = Path(drafts_dir) / "knowledge" if drafts_dir else None
    if extra is not None and extra.exists():
        drafts = load_records(extra)
        published = {record.id for record in records}
        clash = [record.id for record in drafts if record.id in published]
        if clash:
            raise KnowledgeError("draft knowledge id(s) already published: %s" % ", ".join(clash))
        records = records + drafts
    return KnowledgeBase(records=records)


@dataclass
class TurnResult:
    reply: Reply
    events: List[Dict[str, Any]]


class PlaygroundSession:
    def __init__(
        self,
        bot: str,
        resolved: ResolvedIdentity,
        channel: str,
        catalogue: BotCatalogue,
        llm: Any,
        oms_available: bool = True,
        today: Optional[date] = None,
        drafts_dir: Optional[Path] = None,
    ) -> None:
        self.spec: BotSpec = catalogue.get(bot)
        self.resolved = resolved
        self.channel = channel
        self.catalogue = catalogue
        self.settings = Settings(log_path=None, log_to_stdout=False)
        self.log = EventLog(path=None, to_stdout=False)
        registry = build_registry(
            oms_available=oms_available,
            today=today or date.today(),
            topics=catalogue.topics(),
            knowledge_base=knowledge_base_with_drafts(drafts_dir),
            knowledge_bike=resolved.single_bike,
        )
        self.runtime = Runtime(
            settings=self.settings,
            registry=registry,
            llm=llm,
            log=self.log,
            resolver=StaticResolver(resolved),
            catalogue=catalogue,
        )
        self.conversation_id = new_conversation_id()
        self.prompt_override: Optional[str] = None

    # -- prompt editing ----------------------------------------------------------

    def set_prompt(self, text: str) -> None:
        """Use `text` as the bot's base prompt from the next turn on.

        A YAML bot's prompt is captured in its definition, so the agent is
        rebuilt in place — the conversation is kept. A built-in reads its
        module's `_BASE_PROMPT` at call time, so that is patched around each
        call instead (and restored, so nothing leaks into the process).
        """
        self.prompt_override = text
        if self.spec.source != BUILTIN:
            definition = definition_from_spec(replace(self.spec, prompt=text))
            self.runtime.agents[self.spec.name] = Agent(
                definition, self.runtime.registry, self.runtime.llm, self.log, self.settings
            )

    @contextmanager
    def _patched_builtin_prompt(self) -> Iterator[None]:
        if self.prompt_override is None or self.spec.source != BUILTIN or not self.spec.module:
            yield
            return
        module = importlib.import_module(self.spec.module)
        original = module._BASE_PROMPT
        module._BASE_PROMPT = self.prompt_override
        try:
            yield
        finally:
            module._BASE_PROMPT = original

    def system_prompt_preview(self, pill: Optional[str] = None) -> str:
        message = self._message("", pill, ())
        state = self.runtime.conversations.get(self.conversation_id)
        if state.context_block is None:
            # Same block, built the same way and cached on the same state, so
            # the preview is what the first turn will actually send.
            state.context_block = self.runtime.enricher.build(self.resolved).render()
        context = state.context_block
        with self._patched_builtin_prompt():
            definition = self.runtime.agents[self.spec.name].definition
            return definition.build_system_prompt(message, self.resolved, context)

    # -- turns ---------------------------------------------------------------------

    def send(
        self, text: str, pill: Optional[str] = None, attachments: Sequence[Attachment] = ()
    ) -> TurnResult:
        message = self._message(text, pill, attachments)
        before = len(self.log.events)
        with self._patched_builtin_prompt():
            reply = self.runtime.handle(message)
        events = [event for event in self.log.events[before:] if event["event"] in TRACE_EVENTS]
        return TurnResult(reply=reply, events=events)

    def _message(
        self, text: str, pill: Optional[str], attachments: Sequence[Attachment]
    ) -> InboundMessage:
        return InboundMessage(
            conversation_id=self.conversation_id,
            persona=self.resolved.persona,
            identity=self.resolved.identity,
            channel=self.channel,
            message_text=text.strip(),
            entry_metadata={"pill_clicked": pill} if pill else {},
            attachments=list(attachments),
        )
