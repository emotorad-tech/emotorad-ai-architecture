"""Scripted stand-ins for the live-eval tests: no network, known costs."""

from datetime import date

from emotorad_ai.config import Settings
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.live_eval.scenarios import Expect, Scenario, Turn, Who
from emotorad_ai.llm import LLMResponse, ScriptedClaude
from emotorad_ai.wiring import Models

BASE = Settings(log_path="", log_to_stdout=False)
TODAY = date(2026, 9, 29)
JEV = "typesafe/jev-1.13"
NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}
UNSURE = {Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose("none", 0.5)}


def priced(text, cost=0.002):
    """A final answer OpenRouter billed at `cost`; `cost=None` means no cost was reported."""
    usage = {"cost": cost} if cost is not None else None
    return LLMResponse(stop_reason="end_turn", text=text, api_content=[{"type": "text", "text": text}], usage=usage)


def decision(answers=None, cost=0.0001):
    return JevDecision(answers=answers or NARROW, model=JEV, cost=cost)


class Raising:
    """A reply model that always fails with `error`."""

    def __init__(self, error, model="anthropic/claude-haiku-4.5"):
        self.error, self.model, self.requests = error, model, []

    def create(self, system, messages, tools):
        self.requests.append(list(messages))
        raise self.error


class Factory:
    """Builds scripted models for each scenario run and records the settings each got."""

    def __init__(self, replies=None, decisions=None, narrow=None):
        self.replies = replies or [priced("Try another socket.")]
        self.decisions = decisions or [[decision()]]  # one list per build; the last is reused
        self.narrow = narrow  # a model object to use as the narrow model, or None for scripted replies
        self.settings = []

    def __call__(self, settings):
        self.settings.append(settings)
        batch = self.decisions[min(len(self.settings), len(self.decisions)) - 1]
        full = ScriptedClaude(list(self.replies))
        full.model = settings.fallback_model
        narrow = self.narrow
        if narrow is None:
            narrow = ScriptedClaude(list(self.replies))
            narrow.model = settings.narrow_model
        return Models(llm=full, narrow_llm=narrow, jev=ScriptedJev(list(batch)))


def scenario(sid="s", family="routing", expect=None, text="my battery won't charge", settings=None, turns=None):
    expect = expect or Expect(path=("narrow",), handled_by=("narrow_support",))
    return Scenario(
        id=sid,
        family=family,
        who=Who(channel="website", session="sess-ananya"),
        turns=tuple(turns or [Turn(text=text, expect=expect)]),
        settings=settings or {},
    )
