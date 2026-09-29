"""Run live scenarios, cost every call, and stop at the budget.

Spec: docs/superpowers/specs/2026-09-29-live-openrouter-eval-design.md, sections 4.2, 6 and 7.
Each scenario gets a fresh runtime on OpenRouter with conversations in memory
and the business tools mocked, whatever the environment says: nothing is
written to MongoDB and only fixture people leave this machine.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..adapters.amiigo import AmiigoAdapter
from ..adapters.dealer_whatsapp import DealerWhatsAppAdapter
from ..adapters.voice import VoiceAdapter
from ..adapters.website_chat import WebsiteChatAdapter
from ..adapters.whatsapp import WhatsAppAdapter
from ..config import Settings
from ..conversation import InMemoryConversationStore
from ..errorcodes import load_table
from ..identity import IdentityResolver
from ..media import load_catalogue
from ..observability import EventLog
from ..runtime import Runtime
from ..tools.mocks import build_registry
from ..tools.registry import IdempotencyStore, ToolRegistry
from ..wiring import Models, build_models
from .checks import TurnRecord, check_turn, path_of, sub_category_of, tools_called
from .scenarios import Scenario, Turn, Who

# The provider, not the bot: worth one retry after a pause.
PROVIDER_CODES = frozenset({"rate_limited", "unavailable", "bad_response"})
# Every further call would fail the same way: stop the run.
FATAL_CODES = {"auth": "OpenRouter rejected the key", "payment_required": "the OpenRouter account is out of credit"}
ADAPTERS = {
    "website": WebsiteChatAdapter,
    "amiigo": AmiigoAdapter,
    "whatsapp": WhatsAppAdapter,
    "dealer_whatsapp": DealerWhatsAppAdapter,
    "voice": VoiceAdapter,
}
ModelsFactory = Callable[[Settings], Models]


class RunAborted(Exception):
    """OpenRouter refused the key or the account: nothing further can succeed."""


@dataclass
class Cost:
    total: float = 0.0
    by_model: Dict[str, float] = field(default_factory=dict)
    calls: int = 0
    unknown: int = 0  # calls that came back without a billed cost

    def charge(self, model: str, amount: Optional[float]) -> None:
        self.calls += 1
        if amount is None:
            self.unknown += 1
            return
        self.total += amount
        self.by_model[model] = self.by_model.get(model, 0.0) + amount

    def add(self, other: "Cost") -> None:
        self.total += other.total
        self.calls += other.calls
        self.unknown += other.unknown
        for model, amount in other.by_model.items():
            self.by_model[model] = self.by_model.get(model, 0.0) + amount

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": round(self.total, 6),
            "by_model": {m: round(a, 6) for m, a in sorted(self.by_model.items())},
            "calls": self.calls,
            "unknown": self.unknown,
        }


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def cost_of(events: Sequence[Mapping[str, Any]], jev_model: str) -> Cost:
    """What one turn's model calls were billed, from the event log."""
    cost = Cost()
    for event in events:
        if event.get("event") == "llm_turn":
            usage = event.get("usage")
            cost.charge(str(event.get("model") or "unknown model"), _number(usage.get("cost")) if isinstance(usage, dict) else None)
        elif event.get("event") == "jev_decision" and not event.get("error"):
            cost.charge(str(event.get("model") or jev_model), _number(event.get("cost")))
    return cost


@dataclass
class TurnResult:
    index: int
    text: str
    reply: str
    handled_by: str
    path: str
    sub_category: Optional[str]
    tools: List[str]
    ticket_id: Optional[str]
    escalated: bool
    media: int
    cost: Cost
    seconds: float
    failures: List[str]
    provider_codes: List[str]
    # What the model wrote when a post-check replaced it, so the reader can
    # judge whether the check or the model was wrong.
    blocked_reason: Optional[str] = None
    suppressed: Optional[str] = None


@dataclass
class Attempt:
    status: str  # pass | fail | provider
    turns: List[TurnResult]
    cost: Cost
    error: Optional[str] = None


@dataclass
class Outcome:
    scenario: Scenario
    attempts: List[Attempt]  # one, or two when a provider error was retried

    @property
    def final(self) -> Attempt:
        return self.attempts[-1]

    @property
    def spend(self) -> Cost:
        total = Cost()
        for attempt in self.attempts:
            total.add(attempt.cost)
        return total


@dataclass
class SuiteRun:
    started_at: str
    models: Dict[str, str]
    budget: float
    repeat: int
    outcomes: List[Outcome] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    aborted: Optional[str] = None

    @property
    def spend(self) -> Cost:
        total = Cost()
        for outcome in self.outcomes:
            total.add(outcome.spend)
        return total


@dataclass(frozen=True)
class Summary:
    scenario: Scenario
    runs: int
    passes: int
    status: str  # pass | flaky | fail | provider
    cost: Optional[float]  # mean cost of one conversation; None when not fully measured


def scenario_settings(base: Settings, scenario: Scenario) -> Settings:
    """OpenRouter models, memory store and zero data retention, whatever the
    environment says, plus the scenario's own overrides."""
    return replace(
        base, mode="openrouter", store="memory", openrouter_zdr=True, log_path="", log_to_stdout=False,
        **dict(scenario.settings),
    )


def _event(who: Who, conversation_id: str, turn: Turn) -> Dict[str, Any]:
    """The payload each channel's adapter expects."""
    if who.channel == "voice":
        return {"conversation_id": conversation_id, "caller_id": who.phone, "transcript": turn.message}
    event: Dict[str, Any] = {
        "conversation_id": conversation_id,
        "text": turn.message,
        "attachments": [dict(a) for a in turn.attachments],
    }
    if who.channel in ("whatsapp", "dealer_whatsapp"):
        event["from"] = who.phone
    if who.session:
        event["session_token"] = who.session
    if who.em_aid:
        event["em_aid"] = who.em_aid
    return event


def bikes_for(scenario: Scenario, today: date) -> List[Dict[str, Any]]:
    """The bikes the scenario's person owns, as identity resolution sees them.
    Empty for anyone unverified: a cookie or a caller ID owns nothing."""
    resolver = IdentityResolver(build_registry(today=today))
    message = ADAPTERS[scenario.who.channel](resolver).to_message(
        _event(scenario.who, "probe-" + scenario.id, Turn(text="hi"))
    )
    return list(resolver.hydrate(message).bikes)


def scenario_registry(scenario: Scenario, today: date) -> ToolRegistry:
    """Every tool, wired as the playground's offline branch wires them: the
    error-code table, the guide pictures and the person's bikes. The CLI and
    the API do not pass these yet (a known gap); the live test covers the set."""
    bikes = bikes_for(scenario, today)
    return build_registry(
        today=today, idempotency=IdempotencyStore(), guide_media=load_catalogue(), sent_media={},
        error_codes=load_table(), owned_bikes=bikes, knowledge_bike=(bikes or [{}])[0],
    )


def run_scenario(
    scenario: Scenario, base: Settings, models_factory: ModelsFactory = build_models, today: Optional[date] = None,
) -> Attempt:
    settings = scenario_settings(base, scenario)
    today = today or date.today()
    models = models_factory(settings)
    registry = scenario_registry(scenario, today)
    log = EventLog(path=None)
    conversations = InMemoryConversationStore()
    runtime = Runtime(
        settings=settings, registry=registry, llm=models.llm, log=log, resolver=IdentityResolver(registry),
        jev=models.jev, narrow_llm=models.narrow_llm, conversations=conversations,
    )
    adapter = ADAPTERS[scenario.who.channel](runtime.resolver)
    conversation_id = "live-" + scenario.id
    turns: List[TurnResult] = []
    total = Cost()
    customer_texts: List[str] = []
    known_data: List[str] = []
    error: Optional[str] = None

    for index, turn in enumerate(scenario.turns):
        customer_texts.append(turn.message)
        mark, started = len(log.events), time.monotonic()
        try:
            reply = runtime.handle(adapter.to_message(_event(scenario.who, conversation_id, turn)))
        except Exception as exc:  # a crash is a finding; the run goes on
            error = "turn %d crashed: %s: %s" % (index + 1, type(exc).__name__, exc)
            break
        seconds = time.monotonic() - started
        events = log.events[mark:]
        # The runtime logs a provider's code as `error`, which the log's
        # redaction keeps (a field named `code` is hidden as a possible OTP).
        codes = [str(e.get("error") or e.get("code")) for e in events if e.get("event") == "llm_error"]
        codes += [str(e.get("error")) for e in events if e.get("event") == "jev_decision" and e.get("error")]
        for code in codes:
            if code in FATAL_CODES:
                raise RunAborted(FATAL_CODES[code])
        known_data.extend(json.dumps(e.get("result"), default=str) for e in events if e.get("event") == "tool_call")
        context = conversations.get(conversation_id).context_block
        if context and context not in known_data:
            known_data.append(context)
        record = TurnRecord(
            index=index, channel=scenario.who.channel, reply=reply, events=events,
            customer_texts=tuple(customer_texts), known_data=tuple(known_data), today=today,
        )
        cost = cost_of(events, settings.jev_model)
        total.add(cost)
        turns.append(TurnResult(
            index=index, text=turn.message, reply=reply.text, handled_by=reply.handled_by,
            path=path_of(reply, events), sub_category=sub_category_of(events), tools=tools_called(events),
            ticket_id=reply.ticket_id, escalated=reply.escalated, media=len(reply.attachments),
            cost=cost, seconds=round(seconds, 2), failures=check_turn(record, turn.expect, scenario.who),
            provider_codes=[c for c in codes if c in PROVIDER_CODES],
            blocked_reason=reply.metadata.get("blocked_reason"), suppressed=reply.metadata.get("suppressed_text"),
        ))

    if error:
        status = "fail"
    elif scenario.family != "failures" and any(t.provider_codes for t in turns):
        status = "provider"
    elif any(t.failures for t in turns):
        status = "fail"
    else:
        status = "pass"
    return Attempt(status=status, turns=turns, cost=total, error=error)


def run_suite(
    scenarios: Sequence[Scenario],
    base: Settings,
    budget: float,
    repeat: int = 1,
    models_factory: ModelsFactory = build_models,
    pause: float = 20.0,
    sleep: Callable[[float], Any] = time.sleep,
    today: Optional[date] = None,
    progress: Optional[Callable[[Outcome], Any]] = None,
) -> SuiteRun:
    run = SuiteRun(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        models={"jev": base.jev_model, "narrow": base.narrow_model, "full": base.fallback_model},
        budget=budget, repeat=repeat,
    )
    for scenario in scenarios:
        for _ in range(repeat):
            if run.spend.total >= budget:
                if scenario.id not in run.skipped:
                    run.skipped.append(scenario.id)
                continue
            try:
                attempts = [run_scenario(scenario, base, models_factory, today)]
                if attempts[0].status == "provider":
                    sleep(pause)
                    attempts.append(run_scenario(scenario, base, models_factory, today))
            except RunAborted as exc:
                run.aborted = str(exc)
                return run
            outcome = Outcome(scenario=scenario, attempts=attempts)
            run.outcomes.append(outcome)
            if progress is not None:
                progress(outcome)
    return run


def summarise(run: SuiteRun) -> List[Summary]:
    """One line per scenario, in the order they ran."""
    order: List[Scenario] = []
    finals: Dict[str, List[Attempt]] = {}
    for outcome in run.outcomes:
        if outcome.scenario.id not in finals:
            order.append(outcome.scenario)
            finals[outcome.scenario.id] = []
        finals[outcome.scenario.id].append(outcome.final)
    summaries: List[Summary] = []
    for scenario in order:
        attempts = finals[scenario.id]
        passes = sum(1 for a in attempts if a.status == "pass")
        if all(a.status == "provider" for a in attempts):
            status = "provider"
        elif passes == len(attempts):
            status = "pass"
        elif passes:
            status = "flaky"
        else:
            status = "fail"
        measured = [a for a in attempts if a.status != "provider"]
        complete = bool(measured) and all(a.cost.unknown == 0 for a in measured)
        cost = sum(a.cost.total for a in measured) / len(measured) if complete else None
        summaries.append(Summary(scenario=scenario, runs=len(attempts), passes=passes, status=status, cost=cost))
    return summaries


def project(summaries: Sequence[Summary], mixes: Mapping[str, Mapping[str, int]]) -> Dict[str, Dict[str, Any]]:
    """The cost of each mix of 10, and per 1,000. A scenario not measured is
    named, never counted as free."""
    costs = {s.scenario.id: s.cost for s in summaries}
    projected: Dict[str, Dict[str, Any]] = {}
    for name, mix in mixes.items():
        missing = sorted(sid for sid in mix if costs.get(sid) is None)
        if missing:
            projected[name] = {"ten": None, "per_1000": None, "missing": missing}
            continue
        ten = sum(costs[sid] * count for sid, count in mix.items())
        projected[name] = {"ten": round(ten, 6), "per_1000": round(ten * 100, 4), "missing": []}
    return projected
