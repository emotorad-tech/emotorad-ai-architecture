"""The live-eval scenario file: load it, and refuse anything it does not understand.

Spec: docs/superpowers/specs/2026-09-29-live-openrouter-eval-design.md, section 4.1.
A typo in an expectation must fail loudly when the file is read, never pass
quietly during a paid run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "tests" / "data" / "live_scenarios.yaml"

FAMILIES = ("routing", "languages", "guardrails", "identity", "tools", "adversarial", "channels", "failures")
PATHS = ("standard", "narrow", "full", "guardrail", "triage", "direct")
CHANNELS = ("website", "amiigo", "whatsapp", "dealer_whatsapp", "voice")
SCRIPTS = ("latin", "devanagari", "tamil")
GUARDRAILS = ("battery_safety", "human_handoff", "coverage_post_check", "evidence_post_check", "standard_response_blocked")
# handled_by values that are neither an agent, a standard reply nor a guardrail.
FIXED_HANDLERS = ("triage", "router", "llm_error", "store_unavailable", "conversation_busy")
# What a scenario may override, for the forced failures: the models and the time limits.
SETTING_OVERRIDES: Dict[str, type] = {
    "jev_model": str,
    "narrow_model": str,
    "fallback_model": str,
    "openrouter_timeout": float,
    "jev_timeout": float,
}
MIX_SIZE = 10


class ScenarioError(ValueError):
    """The scenario file is wrong. The message says where."""


@dataclass(frozen=True)
class Known:
    """What exists in the code, so the file can be checked against it."""

    tools: FrozenSet[str]
    records: FrozenSet[str]
    standard: FrozenSet[str]
    agents: FrozenSet[str]

    def handlers(self) -> FrozenSet[str]:
        return (
            frozenset(self.agents)
            | frozenset(FIXED_HANDLERS)
            | frozenset("standard:" + s for s in self.standard)
            | frozenset("guardrail:" + g for g in GUARDRAILS)
        )


@dataclass(frozen=True)
class Who:
    channel: str
    session: Optional[str] = None
    em_aid: Optional[str] = None
    phone: Optional[str] = None


@dataclass(frozen=True)
class Expect:
    """The intended behaviour for one turn. Empty means only the checks that always run."""

    path: Tuple[str, ...] = ()  # any of these
    handled_by: Tuple[str, ...] = ()  # any of these
    sub_category: Optional[str] = None  # a record id, or "none"
    tools: Tuple[str, ...] = ()  # each must be called this turn
    no_tools: Tuple[str, ...] = ()  # none may be called this turn
    ticket: Optional[bool] = None
    quotes_ticket: Optional[bool] = None  # the reply quotes the ticket id exactly
    escalated: Optional[bool] = None
    guardrail: Optional[str] = None  # a guardrail name, or "none"
    media: Optional[bool] = None  # the reply carries a picture or clip
    script: Optional[str] = None
    mentions_any: Tuple[str, ...] = ()
    never_mentions: Tuple[str, ...] = ()
    reply_model: Optional[bool] = None  # a reply model was called
    jev: Optional[bool] = None  # Jev was consulted


@dataclass(frozen=True)
class Turn:
    text: str
    repeat: int = 1
    attachments: Tuple[Mapping[str, str], ...] = ()
    expect: Expect = field(default_factory=Expect)

    @property
    def message(self) -> str:
        """What is sent: the text, `repeat` times."""
        return " ".join([self.text] * self.repeat)


@dataclass(frozen=True)
class Scenario:
    id: str
    family: str
    who: Who
    turns: Tuple[Turn, ...]
    note: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Suite:
    scenarios: Tuple[Scenario, ...]
    mixes: Mapping[str, Mapping[str, int]]

    def select(self, only: Optional[Sequence[str]] = None) -> List[Scenario]:
        """Everything, or the scenarios whose id or family is named."""
        if not only:
            return list(self.scenarios)
        names = {s.id for s in self.scenarios} | set(FAMILIES)
        unknown = [name for name in only if name not in names]
        if unknown:
            raise ScenarioError("--only names no scenario or family: %s" % ", ".join(unknown))
        return [s for s in self.scenarios if s.id in only or s.family in only]


def _mapping(where: str, raw: Any, allowed: Sequence[str], required: Sequence[str] = ()) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScenarioError("%s: expected a mapping" % where)
    unknown = sorted(str(name) for name in set(raw) - set(allowed))
    if unknown:
        raise ScenarioError("%s: unknown field(s) %s" % (where, ", ".join(unknown)))
    missing = [name for name in required if name not in raw]
    if missing:
        raise ScenarioError("%s: missing %s" % (where, ", ".join(missing)))
    return raw


def _choice(where: str, value: Any, allowed: Any) -> Any:
    if value is not None and value not in allowed:
        raise ScenarioError("%s: %r is not one of %s" % (where, value, ", ".join(sorted(allowed))))
    return value


def _names(where: str, value: Any, allowed: Any = None) -> Tuple[str, ...]:
    """A name or a list of names."""
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ScenarioError("%s: expected a name or a list of names" % where)
    if allowed is not None:
        for v in values:
            _choice(where, v, allowed)
    return tuple(values)


def _flag(where: str, value: Any) -> Optional[bool]:
    if value is not None and not isinstance(value, bool):
        raise ScenarioError("%s: expected true or false" % where)
    return value


def _expect(where: str, raw: Any, known: Known) -> Expect:
    raw = _mapping(where, raw if raw is not None else {}, tuple(Expect.__dataclass_fields__))
    return Expect(
        path=_names(where + ".path", raw.get("path"), PATHS),
        handled_by=_names(where + ".handled_by", raw.get("handled_by"), known.handlers()),
        sub_category=_choice(where + ".sub_category", raw.get("sub_category"), known.records | {"none"}),
        tools=_names(where + ".tools", raw.get("tools"), known.tools),
        no_tools=_names(where + ".no_tools", raw.get("no_tools"), known.tools),
        ticket=_flag(where + ".ticket", raw.get("ticket")),
        quotes_ticket=_flag(where + ".quotes_ticket", raw.get("quotes_ticket")),
        escalated=_flag(where + ".escalated", raw.get("escalated")),
        guardrail=_choice(where + ".guardrail", raw.get("guardrail"), GUARDRAILS + ("none",)),
        media=_flag(where + ".media", raw.get("media")),
        script=_choice(where + ".script", raw.get("script"), SCRIPTS),
        mentions_any=_names(where + ".mentions_any", raw.get("mentions_any")),
        never_mentions=_names(where + ".never_mentions", raw.get("never_mentions")),
        reply_model=_flag(where + ".reply_model", raw.get("reply_model")),
        jev=_flag(where + ".jev", raw.get("jev")),
    )


def _who(where: str, raw: Any) -> Who:
    raw = _mapping(where, raw, ("channel", "session", "em_aid", "phone"), required=("channel",))
    channel = _choice(where + ".channel", raw["channel"], CHANNELS)
    who = Who(channel=channel, session=raw.get("session"), em_aid=raw.get("em_aid"), phone=raw.get("phone"))
    if channel in ("website", "amiigo") and who.phone:
        raise ScenarioError("%s: %s identifies by session or em_aid, not phone" % (where, channel))
    if channel == "amiigo" and not who.session:
        raise ScenarioError("%s: amiigo needs a session" % where)
    if channel in ("whatsapp", "dealer_whatsapp") and not who.phone:
        raise ScenarioError("%s: %s needs a phone" % (where, channel))
    if channel == "voice" and (who.session or who.em_aid):
        raise ScenarioError("%s: voice identifies by caller id (phone) only" % where)
    return who


def _turn(where: str, raw: Any, known: Known) -> Turn:
    raw = _mapping(where, raw, ("text", "repeat", "attachments", "expect"), required=("text",))
    if not isinstance(raw["text"], str):
        raise ScenarioError("%s.text: expected text" % where)
    repeat = raw.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        raise ScenarioError("%s.repeat: expected a whole number of 1 or more" % where)
    attachments = raw.get("attachments") or []
    if not isinstance(attachments, list):
        raise ScenarioError("%s.attachments: expected a list" % where)
    parsed = tuple(
        dict(_mapping("%s.attachments[%d]" % (where, i), a, ("kind", "url"), required=("url",)))
        for i, a in enumerate(attachments)
    )
    return Turn(text=raw["text"], repeat=repeat, attachments=parsed, expect=_expect(where + ".expect", raw.get("expect"), known))


def _scenario(where: str, raw: Any, known: Known) -> Scenario:
    raw = _mapping(where, raw, ("id", "family", "who", "turns", "note", "settings"), required=("id", "family", "who", "turns"))
    if not isinstance(raw["id"], str) or not raw["id"].strip():
        raise ScenarioError("%s.id: expected a name" % where)
    where = "scenario %s" % raw["id"]
    family = _choice(where + ".family", raw["family"], FAMILIES)
    if not isinstance(raw["turns"], list) or not raw["turns"]:
        raise ScenarioError("%s: needs at least one turn" % where)
    settings = _mapping(where + ".settings", raw.get("settings") or {}, tuple(SETTING_OVERRIDES))
    for name, value in settings.items():
        if SETTING_OVERRIDES[name] is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ScenarioError("%s.settings.%s: expected a number above 0" % (where, name))
        elif not isinstance(value, str) or not value:
            raise ScenarioError("%s.settings.%s: expected a model name" % (where, name))
    return Scenario(
        id=raw["id"],
        family=family,
        who=_who(where + ".who", raw["who"]),
        turns=tuple(_turn("%s.turns[%d]" % (where, i), t, known) for i, t in enumerate(raw["turns"])),
        note=str(raw.get("note") or ""),
        settings=dict(settings),
    )


def load_suite(path: Path, known: Known) -> Suite:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw = _mapping(str(path), raw, ("scenarios", "cost_mixes"), required=("scenarios", "cost_mixes"))
    if not isinstance(raw["scenarios"], list):
        raise ScenarioError("scenarios: expected a list")
    scenarios = tuple(_scenario("scenarios[%d]" % i, s, known) for i, s in enumerate(raw["scenarios"]))
    ids = [s.id for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ScenarioError("duplicate scenario id(s): %s" % ", ".join(duplicates))
    mixes = _mapping("cost_mixes", raw["cost_mixes"], ("typical", "worst"), required=("typical", "worst"))
    for name, mix in mixes.items():
        where = "cost_mixes." + name
        if not isinstance(mix, dict):
            raise ScenarioError("%s: expected scenario id: count" % where)
        unknown = sorted(set(mix) - set(ids))
        if unknown:
            raise ScenarioError("%s: unknown scenario(s) %s" % (where, ", ".join(unknown)))
        if any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in mix.values()):
            raise ScenarioError("%s: every count must be a whole number of 1 or more" % where)
        if sum(mix.values()) != MIX_SIZE:
            raise ScenarioError("%s: counts add up to %d, not %d" % (where, sum(mix.values()), MIX_SIZE))
    return Suite(scenarios=scenarios, mixes={name: dict(mix) for name, mix in mixes.items()})


def known_from_code() -> Known:
    """The tools, records, standard replies and agents that exist right now."""
    from ..agents.battery_support import AGENT_NAME as battery
    from ..agents.dealer_orders import AGENT_NAME as dealer
    from ..agents.late_warranty import AGENT_NAME as late
    from ..agents.motor_support import AGENT_NAME as motor
    from ..agents.narrow_support import AGENT_NAME as narrow
    from ..decisions import build_catalogue
    from ..errorcodes import load_table
    from ..knowledge import KnowledgeBase
    from ..media import load_catalogue
    from ..standard_responses import load_standard_responses
    from ..tools.mocks import build_registry

    catalogue = build_catalogue(KnowledgeBase(), standard=load_standard_responses(), error_table=load_table())
    # The whole tool set, as the runner wires it (runner.scenario_registry).
    tools = build_registry(guide_media=load_catalogue(), error_codes=load_table()).specs
    return Known(
        tools=frozenset(tools),
        records=frozenset(catalogue.records),
        standard=frozenset(catalogue.standard),
        agents=frozenset({battery, dealer, late, motor, narrow}),
    )
