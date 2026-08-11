"""Quality metrics, computed from the event log (risk register §1).

**Volume metrics are not quality metrics.** Klarna hit every deflection target
they set and were rehiring humans eighteen months later, because "the bot handled
it" and "the customer's problem was solved" are different measurements and only
one of them was being taken.

So the numbers here are deliberately awkward ones:

* **Deflection is reported, never celebrated.** It is an input to cost, not a
  measure of success, and it is trivially gamed by a bot that refuses to escalate.
* **Repeat contact within 48 hours** is the honest counter-metric. A conversation
  that "resolved" and came back two days later did not resolve.
* **Escalation rate is a health signal, not a failure.** Driving it to zero means
  a bot that will not hand over — which is how Klarna's ended up worse than the
  humans it replaced.
* **Cost per *resolved* conversation**, not per conversation. Ten cheap turns
  that fix nothing are not cheaper than one that works.
* **Everything is reported per language.** An aggregate can hide 95% English and
  40% Hindi, and the aggregate is what ends up on a slide.

Computed from the JSONL event log so nothing extra has to be instrumented, and so
the same function runs over shadow-mode output and production.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Anything at or above this share of conversations escalating means the bot is
# not carrying its half — but the floor matters as much as the ceiling.
ESCALATION_HEALTHY = (0.05, 0.35)
REPEAT_CONTACT_WINDOW_HOURS = 48


@dataclass
class ConversationSummary:
    conversation_id: str
    cluster_id: Optional[str] = None
    channel: str = ""
    language: str = "en"
    turns: int = 0
    escalated: bool = False
    handled_by: str = ""
    guardrails: List[str] = field(default_factory=list)
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    @property
    def resolved(self) -> bool:
        """Handled by an agent, without escalation and without a guardrail stop.

        Deliberately strict: a conversation that ended in a coverage block or a
        safety escalation is a *correct* outcome but not a resolved issue, and
        counting it as one is exactly how deflection numbers start lying.
        """
        return not self.escalated and bool(self.handled_by) and not self.handled_by.startswith(
            ("guardrail:", "triage", "router")
        )


@dataclass
class Report:
    conversations: int = 0
    resolved: int = 0
    escalated: int = 0
    repeat_contacts: int = 0
    by_language: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_channel: Dict[str, int] = field(default_factory=dict)
    guardrail_hits: Dict[str, int] = field(default_factory=dict)
    edge_cases: Dict[str, int] = field(default_factory=dict)
    total_tokens: int = 0

    @property
    def deflection_rate(self) -> float:
        """Reported, not celebrated. Trivially gamed by refusing to escalate."""
        return self.resolved / self.conversations if self.conversations else 0.0

    @property
    def escalation_rate(self) -> float:
        return self.escalated / self.conversations if self.conversations else 0.0

    @property
    def repeat_contact_rate(self) -> float:
        """The counter-metric. A conversation that came back did not resolve."""
        return self.repeat_contacts / self.resolved if self.resolved else 0.0

    @property
    def escalation_health(self) -> str:
        low, high = ESCALATION_HEALTHY
        if self.escalation_rate < low:
            # The Klarna shape: a bot that will not hand over looks excellent on
            # a deflection dashboard and is worse than the humans it replaced.
            return "suspiciously_low"
        if self.escalation_rate > high:
            return "too_high"
        return "healthy"

    def tokens_per_resolved(self) -> float:
        """Cost per *resolved* conversation. Ten cheap turns that fix nothing are
        not cheaper than one that works."""
        return self.total_tokens / self.resolved if self.resolved else float("inf")


def detect_language(text: str) -> str:
    """Crude, and honest about it — enough to bucket a report, not to route on.

    The point is that quality is never reported as a single average. Replace with
    a real detector when one is available; the shape of the report does not change.
    """
    if not text:
        return "unknown"
    if any("ऀ" <= character <= "ॿ" for character in text):
        return "hi-deva"
    lowered = text.lower()
    hinglish = ("nahi", "hai", "kya", "kar", "raha", "rahi", "gaya", "hua", "mera", "meri", "aa rahi")
    if any(" %s " % marker in " %s " % lowered for marker in hinglish):
        return "hinglish"
    return "en"


def summarise(events: Sequence[Dict[str, Any]]) -> List[ConversationSummary]:
    """Fold a flat event log into one row per conversation."""
    by_id: Dict[str, ConversationSummary] = {}

    for event in events:
        conversation_id = event.get("conversation_id")
        if not conversation_id:
            continue
        summary = by_id.setdefault(
            conversation_id, ConversationSummary(conversation_id=conversation_id)
        )
        kind = event.get("event")

        if summary.started_at is None:
            summary.started_at = event.get("timestamp")
        summary.ended_at = event.get("timestamp") or summary.ended_at

        if kind == "inbound":
            summary.turns += 1
            summary.channel = event.get("channel") or summary.channel
            text = event.get("text") or ""
            if summary.language in ("en", "unknown"):
                # First non-English turn wins: a conversation with any Hindi in it
                # is a Hindi conversation for reporting purposes, because that is
                # the experience being measured.
                detected = detect_language(text)
                if detected != "en":
                    summary.language = detected
                elif summary.language == "unknown":
                    summary.language = detected
        elif kind == "identity_resolved":
            summary.cluster_id = event.get("cluster_id") or summary.cluster_id
        elif kind == "guardrail_triggered":
            summary.guardrails.append(event.get("guardrail", "unknown"))
        elif kind == "tool_call":
            summary.tool_calls += 1
        elif kind == "llm_turn":
            usage = event.get("usage") or {}
            summary.input_tokens += usage.get("input_tokens", 0) or 0
            summary.output_tokens += usage.get("output_tokens", 0) or 0
        elif kind == "outcome":
            summary.handled_by = event.get("handled_by") or summary.handled_by
            summary.escalated = bool(event.get("escalated")) or summary.escalated

    return list(by_id.values())


def _repeat_contacts(summaries: Sequence[ConversationSummary]) -> int:
    """Resolved conversations whose cluster came back inside the window.

    Timestamps are ISO strings from the log; comparing them lexicographically is
    valid for UTC ISO-8601 and avoids a parsing dependency here.
    """
    by_cluster: Dict[str, List[ConversationSummary]] = defaultdict(list)
    for summary in summaries:
        if summary.cluster_id:
            by_cluster[summary.cluster_id].append(summary)

    repeats = 0
    for conversations in by_cluster.values():
        ordered = sorted(conversations, key=lambda s: s.started_at or "")
        for earlier, later in zip(ordered, ordered[1:]):
            if not earlier.resolved:
                continue
            if _within_hours(earlier.ended_at, later.started_at, REPEAT_CONTACT_WINDOW_HOURS):
                repeats += 1
    return repeats


def _within_hours(earlier: Optional[str], later: Optional[str], hours: int) -> bool:
    from datetime import datetime

    if not earlier or not later:
        return False
    try:
        start = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
        end = datetime.fromisoformat(later.replace("Z", "+00:00"))
    except ValueError:
        return False
    return 0 <= (end - start).total_seconds() <= hours * 3600


def build_report(
    events: Iterable[Dict[str, Any]], edge_case_signals: Optional[Dict[str, int]] = None
) -> Report:
    summaries = summarise(list(events))
    report = Report(conversations=len(summaries))

    report.resolved = sum(1 for s in summaries if s.resolved)
    report.escalated = sum(1 for s in summaries if s.escalated)
    report.repeat_contacts = _repeat_contacts(summaries)
    report.total_tokens = sum(s.input_tokens + s.output_tokens for s in summaries)
    report.by_channel = dict(Counter(s.channel for s in summaries if s.channel))
    report.guardrail_hits = dict(Counter(g for s in summaries for g in s.guardrails))
    report.edge_cases = dict(edge_case_signals or {})

    # Per language, never aggregated. An average hides the language that is worst.
    for language in sorted({s.language for s in summaries}):
        rows = [s for s in summaries if s.language == language]
        resolved = sum(1 for s in rows if s.resolved)
        report.by_language[language] = {
            "conversations": len(rows),
            "resolved": resolved,
            "escalated": sum(1 for s in rows if s.escalated),
            "deflection_rate": resolved / len(rows) if rows else 0.0,
            "avg_turns": sum(s.turns for s in rows) / len(rows) if rows else 0.0,
        }
    return report


def render(report: Report) -> str:
    """A plain-text scorecard, ordered so the honest numbers come first."""
    lines = [
        "conversations        %d" % report.conversations,
        "escalation rate      %.1f%%  (%s)" % (report.escalation_rate * 100, report.escalation_health),
        "repeat contact <48h  %.1f%% of resolved" % (report.repeat_contact_rate * 100),
        "deflection           %.1f%%  (reported, not a target)" % (report.deflection_rate * 100),
        "tokens per resolved  %.0f" % report.tokens_per_resolved(),
        "",
        "per language (never averaged):",
    ]
    for language, row in report.by_language.items():
        lines.append(
            "  %-8s n=%-4d deflection %5.1f%%  escalated %-3d  avg turns %.1f"
            % (language, row["conversations"], row["deflection_rate"] * 100,
               row["escalated"], row["avg_turns"])
        )
    if report.guardrail_hits:
        lines.append("")
        lines.append("guardrails fired:")
        for name, count in sorted(report.guardrail_hits.items(), key=lambda kv: -kv[1]):
            lines.append("  %-28s %d" % (name, count))
    if report.edge_cases:
        lines.append("")
        lines.append("edge cases seen (promotes items out of CAPTURE):")
        for name, count in sorted(report.edge_cases.items(), key=lambda kv: -kv[1]):
            rate = count / report.conversations if report.conversations else 0
            lines.append("  %-28s %d  (%.1f%%)" % (name, count, rate * 100))
    return "\n".join(lines)
