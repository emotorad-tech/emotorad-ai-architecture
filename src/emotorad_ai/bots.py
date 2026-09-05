"""The bot catalogue — every sub-agent the runtime can route to, in one list.

A bot is configuration: a name, a persona, a topic, the keywords triage matches
on, the tools it may call, and a prompt. The four built-in agents are entries in
the same list as the YAML ones, so runtime, triage, the search_knowledge topic
enum and the "I can help with …" reply all derive from here and nothing about a
bot is written down twice.

Files, not a database: `bots/*.yaml` is published (Git is the audit trail, a PR
is the approval); a drafts directory holds what the playground is trying out.
Both load through the same validation, which raises rather than skips — a spec
that fails to load is a bot that silently stopped existing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .agents import battery_support, dealer_orders, late_warranty, motor_support
from .agents.base import AgentDefinition
from .agents.generic import definition_from_spec
from .tools.mocks import (
    BOOK_SERVICE_SLOT,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    GET_BATTERY_DIAGNOSTICS,
    GET_DEALER_ACCOUNT,
    LOOKUP_WARRANTY_RECORD,
    PLACE_ORDER,
    QUOTE_ORDER,
    SEARCH_KNOWLEDGE,
    SUBMIT_WARRANTY_PROOF,
)

BOTS_DIR = Path(__file__).resolve().parent.parent.parent / "bots"

BUILTIN = "builtin"
PUBLISHED = "published"
DRAFT = "draft"

PERSONAS = ("customer", "dealer")

# What each persona's bots may call, enforced at load. A spec can narrow this;
# it can never widen it. The dealer list has no warranty lookup on purpose:
# dealers register most warranties under their own number, so that tool would
# hand them dozens of unrelated customers' bikes (see agents/dealer_orders.py).
PERSONA_TOOLS: Dict[str, frozenset] = {
    "customer": frozenset(
        {
            LOOKUP_WARRANTY_RECORD,
            GET_BATTERY_DIAGNOSTICS,
            SEARCH_KNOWLEDGE,
            CREATE_SUPPORT_TICKET,
            FIND_SERVICE_SLOTS,
            BOOK_SERVICE_SLOT,
            SUBMIT_WARRANTY_PROOF,
        }
    ),
    "dealer": frozenset(
        {GET_DEALER_ACCOUNT, QUOTE_ORDER, PLACE_ORDER, CREATE_SUPPORT_TICKET, SEARCH_KNOWLEDGE}
    ),
}

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class BotSpecError(ValueError):
    """A spec is malformed or conflicts with another. Raised at load, never later."""


@dataclass(frozen=True)
class BotSpec:
    name: str
    persona: str
    prompt: str
    topic: Optional[str] = None
    keywords: Sequence[str] = ()
    tools: Sequence[str] = ()
    source: str = DRAFT
    path: Optional[Path] = None
    # Built-ins only: the module whose `_BASE_PROMPT` the playground may swap.
    module: Optional[str] = None
    # Built-ins carry their own definition; YAML bots are built by generic.py.
    definition: Optional[AgentDefinition] = field(default=None, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "persona": self.persona,
            "topic": self.topic,
            "keywords": list(self.keywords),
            "tools": list(self.tools),
            "prompt": self.prompt,
        }


def spec_from_dict(raw: Mapping[str, Any], source: str, path: Optional[Path] = None) -> BotSpec:
    where = str(path) if path else "<inline>"
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise BotSpecError("%s: name must be snake_case (got %r)" % (where, name))

    persona = raw.get("persona")
    if persona not in PERSONAS:
        raise BotSpecError("%s: persona must be one of %s (got %r)" % (where, PERSONAS, persona))

    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BotSpecError("%s: prompt must not be empty" % where)

    topic = raw.get("topic")
    if topic is not None and (not isinstance(topic, str) or not _NAME.match(topic)):
        raise BotSpecError("%s: topic must be snake_case (got %r)" % (where, topic))

    keywords = tuple(str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip())
    tools = tuple(str(t) for t in (raw.get("tools") or []))

    allowed = PERSONA_TOOLS[persona]
    forbidden = [t for t in tools if t not in allowed]
    if forbidden:
        raise BotSpecError(
            "%s: a %s bot may not use %s (allowed: %s)"
            % (where, persona, ", ".join(forbidden), ", ".join(sorted(allowed)))
        )

    return BotSpec(
        name=name,
        persona=persona,
        prompt=prompt,
        topic=topic,
        keywords=keywords,
        tools=tools,
        source=source,
        path=path,
    )


def load_specs(directory: Path, source: str) -> List[BotSpec]:
    """Every `*.yaml` directly under `directory`. A missing directory is empty,
    not an error — the drafts directory does not exist until the first draft."""
    root = Path(directory)
    if not root.exists():
        return []
    import yaml

    specs: List[BotSpec] = []
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise BotSpecError("%s: expected a mapping at the top level" % path)
        specs.append(spec_from_dict(raw, source, path))
    return specs


def builtin_specs() -> List[BotSpec]:
    """The four hand-written agents, as catalogue entries.

    Order matters: it is the order the playground lists them and the order
    `supported_summary` names them, and "battery and motor" is what the reply
    said before this file existed.
    """
    return [
        BotSpec(
            name=battery_support.AGENT_NAME,
            persona="customer",
            prompt=battery_support._BASE_PROMPT,
            topic=battery_support.TOPIC,
            keywords=battery_support.KEYWORDS,
            tools=battery_support.TOOL_NAMES,
            source=BUILTIN,
            module=battery_support.__name__,
            definition=battery_support.DEFINITION,
        ),
        BotSpec(
            name=motor_support.AGENT_NAME,
            persona="customer",
            prompt=motor_support._BASE_PROMPT,
            topic=motor_support.TOPIC,
            keywords=motor_support.KEYWORDS,
            tools=motor_support.TOOL_NAMES,
            source=BUILTIN,
            module=motor_support.__name__,
            definition=motor_support.DEFINITION,
        ),
        BotSpec(
            name=late_warranty.AGENT_NAME,
            persona="customer",
            prompt=late_warranty._BASE_PROMPT,
            tools=late_warranty.TOOL_NAMES,
            source=BUILTIN,
            module=late_warranty.__name__,
            definition=late_warranty.DEFINITION,
        ),
        BotSpec(
            name=dealer_orders.AGENT_NAME,
            persona="dealer",
            prompt=dealer_orders._BASE_PROMPT,
            topic="order",
            tools=dealer_orders.TOOL_NAMES,
            source=BUILTIN,
            module=dealer_orders.__name__,
            definition=dealer_orders.DEFINITION,
        ),
    ]


class BotCatalogue:
    def __init__(self, specs: Iterable[BotSpec]) -> None:
        self._specs = list(specs)
        self._check_unique()
        self._check_keywords_disjoint()

    @classmethod
    def load(
        cls,
        published_dir: Optional[Path] = None,
        drafts_dir: Optional[Path] = None,
        extra: Sequence[BotSpec] = (),
    ) -> "BotCatalogue":
        specs = builtin_specs()
        specs += load_specs(Path(published_dir) if published_dir else BOTS_DIR, PUBLISHED)
        if drafts_dir:
            specs += load_specs(Path(drafts_dir) / "bots", DRAFT)
        specs += list(extra)
        return cls(specs)

    # -- validation ------------------------------------------------------------

    def _check_unique(self) -> None:
        seen_names: Dict[str, BotSpec] = {}
        seen_topics: Dict[str, BotSpec] = {}
        for spec in self._specs:
            if spec.name in seen_names:
                raise BotSpecError(
                    "duplicate bot name %r (%s and %s)"
                    % (spec.name, _where(seen_names[spec.name]), _where(spec))
                )
            seen_names[spec.name] = spec
            if spec.topic:
                key = "%s:%s" % (spec.persona, spec.topic)
                if key in seen_topics:
                    raise BotSpecError(
                        "duplicate %s topic %r (%s and %s)"
                        % (spec.persona, spec.topic, _where(seen_topics[key]), _where(spec))
                    )
                seen_topics[key] = spec

    def _check_keywords_disjoint(self) -> None:
        # classify_issue matches by substring (`word in lowered`), not equality
        # — so two bots of one persona whose keywords merely overlap, not just
        # match exactly, both stop being reachable: "charger cable" contains
        # "charge", so a message with either word matches both bots' tables and
        # classify_issue returns None for both. Checked in both directions,
        # since it does not matter which keyword is the longer one. Across
        # personas it is fine: the tables are never consulted together.
        for persona in PERSONAS:
            seen: List[Any] = []  # (lowered, original_word, spec), other personas excluded
            for spec in self._specs:
                if spec.persona != persona:
                    continue
                for word in spec.keywords:
                    lowered = word.lower()
                    for other_lowered, other_word, other_spec in seen:
                        if other_spec.name == spec.name:
                            continue
                        if lowered == other_lowered or lowered in other_lowered or other_lowered in lowered:
                            raise BotSpecError(
                                "keyword %r (%s) overlaps with keyword %r (%s) — classify_issue "
                                "matches by substring, so one would swallow the other"
                                % (word, spec.name, other_word, other_spec.name)
                            )
                    seen.append((lowered, word, spec))

    def validate(self, registry: Any) -> None:
        """Every tool a YAML bot names must exist in this registry.

        Built-ins are exempt: battery_support lists get_battery_diagnostics on
        purpose, and Agent.run drops it when telematics do not exist.
        """
        for spec in self._specs:
            if spec.source == BUILTIN:
                continue
            missing = [t for t in spec.tools if t not in registry.specs]
            if missing:
                raise BotSpecError(
                    "%s: tool(s) not registered: %s" % (_where(spec), ", ".join(missing))
                )

    # -- derived tables --------------------------------------------------------

    @property
    def specs(self) -> List[BotSpec]:
        return list(self._specs)

    def get(self, name: str) -> BotSpec:
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def definitions(self) -> Dict[str, AgentDefinition]:
        return {
            spec.name: spec.definition if spec.definition is not None else definition_from_spec(spec)
            for spec in self._specs
        }

    def topic_agents(self, persona: str) -> Dict[str, str]:
        """topic -> bot name, for the bots triage can reach by keyword."""
        return {
            spec.topic: spec.name
            for spec in self._specs
            if spec.persona == persona and spec.topic and spec.keywords
        }

    def keywords(self, persona: str) -> Dict[str, Sequence[str]]:
        return {
            spec.topic: spec.keywords
            for spec in self._specs
            if spec.persona == persona and spec.topic and spec.keywords
        }

    def topics(self) -> List[str]:
        """Knowledge topics, for the search_knowledge enum: every topic of a bot
        that can actually search. dealer_orders has a topic and no search tool,
        so it is not one."""
        return sorted(
            {spec.topic for spec in self._specs if spec.topic and SEARCH_KNOWLEDGE in spec.tools}
        )

    def supported_summary(self, persona: str) -> str:
        topics = [t for t in self.topic_agents(persona)]
        if not topics:
            return "general questions"
        if len(topics) == 1:
            return "%s problems" % topics[0]
        return "%s and %s problems" % (", ".join(topics[:-1]), topics[-1])


def _where(spec: BotSpec) -> str:
    return str(spec.path) if spec.path else "%s:%s" % (spec.source, spec.name)
