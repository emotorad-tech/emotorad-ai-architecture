"""Thresholds from evidence: how Jev scores on the labelled set, per language.

Jev's probabilities are calibrated in aggregate, not per question format, so a
threshold is chosen per question by measuring, never by feel. Every number is
reported per language and never averaged; an average hides the language that
is worst, and Hinglish is the one most likely to be.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .decisions import RoutingCatalogue, Thresholds, route
from .jev import ChoiceAnswer, JevDecision, NoulAnswer

CANDIDATES = [round(0.50 + 0.05 * step, 2) for step in range(10)]  # 0.50 .. 0.95

Pair = Tuple[Any, JevDecision]


def evaluate_choice(
    pairs: Sequence[Pair], question_id: str, expected: Callable[[Any], Optional[str]], candidates: Iterable[float]
) -> Dict[float, Dict[str, Dict[str, int]]]:
    """For each threshold: per language, how many cases, how many Jev was
    confident on (covered), and how many of those it got right."""
    rows: Dict[float, Dict[str, Dict[str, int]]] = {}
    for threshold in candidates:
        by_language: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "covered": 0, "correct": 0})
        for case, decision in pairs:
            want = expected(case)
            answer = decision.answers.get(question_id)
            if want is None or not isinstance(answer, ChoiceAnswer):
                continue
            row = by_language[case.language]
            row["n"] += 1
            if answer.p >= threshold:
                row["covered"] += 1
                row["correct"] += int(answer.choice == want)
        rows[threshold] = dict(by_language)
    return rows


def suggest_choice(rows: Mapping[float, Mapping[str, Mapping[str, int]]], target: float) -> Optional[float]:
    """The lowest threshold at which every language is at least `target`
    accurate on what it covers. Lowest, because coverage is the saving."""
    for threshold in sorted(rows):
        languages = rows[threshold]
        if languages and all(
            (row["correct"] / row["covered"] if row["covered"] else 1.0) >= target for row in languages.values()
        ):
            return threshold
    return None


def evaluate_noul(
    pairs: Sequence[Pair], question_id: str, expected: Callable[[Any], Optional[bool]], candidates: Iterable[float]
) -> Dict[float, Dict[str, Dict[str, int]]]:
    rows: Dict[float, Dict[str, Dict[str, int]]] = {}
    for threshold in candidates:
        by_language: Dict[str, Dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
        for case, decision in pairs:
            want = expected(case)
            answer = decision.answers.get(question_id)
            if want is None or not isinstance(answer, NoulAnswer):
                continue
            predicted = answer.p >= threshold
            key = ("tp" if want else "fp") if predicted else ("fn" if want else "tn")
            by_language[case.language][key] += 1
        rows[threshold] = dict(by_language)
    return rows


def evaluate_followups(
    pairs: Sequence[Pair], thresholds: Thresholds, catalogue: RoutingCatalogue
) -> Dict[str, Dict[str, int]]:
    """Mid-conversation replies, judged by what route() would actually do.

    Per-question accuracy cannot see this. Rule 4 keeps a conversation on its
    record when Jev is unsure, so a follow-up is handled well only if Jev is
    unsure on "yes, it's red now" and sure on "now the motor is noisy". This
    replays the real routing with these thresholds and counts, per language:
    stay_ok / leave_ok (right), stay_wrong (kept on the record when the customer
    had moved on) and leave_wrong (dropped the record mid-flow).
    """
    rows: Dict[str, Dict[str, int]] = defaultdict(lambda: {"stay_ok": 0, "leave_ok": 0, "stay_wrong": 0, "leave_wrong": 0})
    for case, decision in pairs:
        current = getattr(case, "current_sub_category", None)
        if not current or case.sub_category is None:
            continue
        should_stay = case.sub_category == current
        chosen = route(decision, None, thresholds, catalogue, current, getattr(case, "bike", None) or {})
        stayed = chosen.path == "narrow" and chosen.sub_category == current
        if should_stay:
            rows[case.language]["stay_ok" if stayed else "leave_wrong"] += 1
        else:
            rows[case.language]["stay_wrong" if stayed else "leave_ok"] += 1
    return dict(rows)


def suggest_noul(rows: Mapping[float, Mapping[str, Mapping[str, int]]], target_recall: float) -> Optional[float]:
    """The highest threshold that still catches `target_recall` of the real
    cases in every language. A missed warranty lookup blocks a correct reply;
    an extra one costs a read, so recall is what matters."""
    for threshold in sorted(rows, reverse=True):
        languages = rows[threshold]
        recalls = [
            row["tp"] / (row["tp"] + row["fn"]) if (row["tp"] + row["fn"]) else 1.0 for row in languages.values()
        ]
        if languages and all(recall >= target_recall for recall in recalls):
            return threshold
    return None
