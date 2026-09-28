"""Jev, TypeSafe's System One decision model, through OpenRouter's Decisions API.

Jev does not write text. It takes a block of state and a set of typed
questions and returns, for each, a structured answer with probabilities:

* **choice**: one label from the criteria we define, the full distribution over
  every label, and a confidence (how concentrated that distribution is);
* **noul**: one probability that the statement is true.

Our code reads those numbers and decides what happens next (decisions.py). The
parser here is strict on purpose: the Decisions API is alpha, and an answer we
cannot fully trust is reported as an error so the turn falls back to the full
agent, rather than half-read and acted on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from .openrouter import DECISIONS_PATH, OpenRouterError


class JevError(Exception):
    """Anything that stops a turn using Jev. `code` is stable and loggable:
    auth, rate_limited, unavailable, bad_response, bad_request,
    payment_required, config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Question:
    type: str
    instructions: str
    criteria: Any = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria:
            payload["criteria"] = dict(self.criteria)
        return payload


def choice(instructions: str, criteria: Mapping[str, str]) -> Question:
    if not 2 <= len(criteria) <= 255:
        raise ValueError("a choice needs between 2 and 255 options, got %d" % len(criteria))
    return Question("choice", instructions, dict(criteria))


def noul(instructions: str, true: str = "", false: str = "") -> Question:
    criteria = {key: text for key, text in (("true", true), ("false", false)) if text}
    return Question("noul", instructions, criteria or None)


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float

    @property
    def p(self) -> float:
        """The probability of the option Jev chose, which is what thresholds compare."""
        return float(self.probabilities.get(self.choice, 0.0))


@dataclass(frozen=True)
class NoulAnswer:
    p: float


Answer = Union[ChoiceAnswer, NoulAnswer]


@dataclass(frozen=True)
class JevDecision:
    answers: Mapping[str, Answer]
    model: str = ""
    cost: Optional[float] = None
    latency_ms: int = 0

    def scores(self) -> Dict[str, Any]:
        """What gets logged: the numbers, never the state they were computed from."""
        out: Dict[str, Any] = {}
        for question_id, answer in self.answers.items():
            if isinstance(answer, ChoiceAnswer):
                out[question_id] = {"choice": answer.choice, "p": round(answer.p, 4)}
            else:
                out[question_id] = {"p": round(answer.p, 4)}
        return out


def _probability(value: Any, question_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError("bad_response", "%s: probability is not a number" % question_id)
    if not 0.0 <= float(value) <= 1.0:
        raise JevError("bad_response", "%s: probability %r is outside 0..1" % (question_id, value))
    return float(value)


def parse_decision(payload: Mapping[str, Any], questions: Mapping[str, Question]) -> JevDecision:
    raw_answers = payload.get("answers") if isinstance(payload, Mapping) else None
    if not isinstance(raw_answers, Mapping):
        raise JevError("bad_response", "the response has no answers object")

    answers: Dict[str, Answer] = {}
    for question_id, question in questions.items():
        raw = raw_answers.get(question_id)
        if not isinstance(raw, Mapping):
            raise JevError("bad_response", "no answer for %s" % question_id)
        if raw.get("type") != question.type:
            raise JevError("bad_response", "%s: expected a %s answer" % (question_id, question.type))

        if question.type == "choice":
            chosen = raw.get("choice")
            if chosen not in question.criteria:
                raise JevError("bad_response", "%s: %r is not one of the options" % (question_id, chosen))
            probabilities = raw.get("probabilities")
            if not isinstance(probabilities, Mapping) or not probabilities:
                raise JevError("bad_response", "%s: no probabilities" % question_id)
            clean: Dict[str, float] = {}
            for option, value in probabilities.items():
                if option not in question.criteria:
                    raise JevError("bad_response", "%s: probability for unknown option %r" % (question_id, option))
                clean[option] = _probability(value, question_id)
            answers[question_id] = ChoiceAnswer(
                choice=chosen,
                probabilities=clean,
                confidence=_probability(raw.get("confidence", 0.0), question_id),
            )
        elif question.type == "noul":
            answers[question_id] = NoulAnswer(p=_probability(raw.get("noul"), question_id))
        else:
            raise JevError("bad_request", "%s: unsupported question type %r" % (question_id, question.type))

    usage = payload.get("usage")
    cost = usage.get("cost") if isinstance(usage, Mapping) else None
    return JevDecision(
        answers=answers,
        model=str(payload.get("model") or ""),
        cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


class JevClient:
    def __init__(
        self,
        transport: Any,
        model: str = "typesafe/jev-1.13",
        timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self.model = model
        self.timeout = timeout
        self._clock = clock

    def decide(self, state: Any, questions: Mapping[str, Question]) -> JevDecision:
        body = {
            "model": self.model,
            "state": state,
            "questions": {question_id: q.to_dict() for question_id, q in questions.items()},
        }
        started = self._clock()
        try:
            payload = self._transport.post(DECISIONS_PATH, body, timeout=self.timeout)
        except OpenRouterError as exc:
            raise JevError(exc.code, str(exc)) from None
        decision = parse_decision(payload, questions)
        return replace(decision, latency_ms=int(round((self._clock() - started) * 1000)))


class ScriptedJev:
    """Returns queued decisions (or raises queued JevErrors) in order.

    Records every call, so a test can assert Jev was never consulted, which is
    how the safety and handoff paths prove they still run first.
    """

    def __init__(self, decisions: Sequence[Union[JevDecision, JevError]] = ()) -> None:
        self._queue = list(decisions)
        self.calls: List[Dict[str, Any]] = []

    def decide(self, state: Any, questions: Mapping[str, Question]) -> JevDecision:
        self.calls.append({"state": state, "questions": dict(questions)})
        if not self._queue:
            raise AssertionError("ScriptedJev ran out of queued decisions")
        item = self._queue.pop(0)
        if isinstance(item, JevError):
            raise item
        return item


def choose(chosen: str, p: float) -> ChoiceAnswer:
    """Scripted choice answer."""
    return ChoiceAnswer(choice=chosen, probabilities={chosen: p}, confidence=p)


def yes(p: float) -> NoulAnswer:
    """Scripted noul answer."""
    return NoulAnswer(p=p)
