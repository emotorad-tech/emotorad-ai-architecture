"""Context enrichment (build plan §3.2.1).

Assembles what the agent should already know before it speaks: who the person is,
what they own, what they were browsing, what we last talked about. Four rules
shape the whole file:

* **Deterministic.** No LLM. Assembling context is selection and formatting, and
  paying a model to do it would add latency, cost and non-determinism to
  something a dictionary lookup answers.
* **Lazy, not precomputed.** Most visitors never open a chat, so a nightly job
  building profiles for everyone is ~97% wasted — and would still miss the
  browsing someone did five minutes before typing, which is the most relevant
  browsing there is.
* **Summarised, not dumped.** Forty page views become "looked at three bikes in
  the ₹30-40k band". A transcript of everything is expensive, and buries the two
  facts that matter.
* **Disclosure-gated.** A cookie identifies a browser; a shared family laptop is
  one cookie and several people. So browsing may personalise, but only a verified
  identity may unlock name, bikes and coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .identity import ResolvedIdentity

# Roughly four characters per token for English. Good enough to keep a budget
# honest; nothing here needs a real tokeniser.
CHARS_PER_TOKEN = 4
DEFAULT_TOKEN_BUDGET = 800

# Ordered most to least valuable. When the budget runs out the tail is dropped,
# so this ordering decides what survives — ownership before browsing, always,
# because it is what the agent can actually act on.
SECTION_PRIORITY = ("identity", "bikes", "recent_conversation", "browsing", "signals")


@dataclass
class EnrichedContext:
    sections: Dict[str, str] = field(default_factory=dict)
    dropped: List[str] = field(default_factory=list)
    tokens: int = 0

    def render(self) -> str:
        ordered = [self.sections[name] for name in SECTION_PRIORITY if name in self.sections]
        return "\n".join(ordered)


def _tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def summarise_browsing(events: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Turn a list of page views into one sentence.

    Deliberately lossy. "Viewed EMX Plus four times and Doodle V3 twice" is what
    changes a reply; the individual timestamps never do.
    """
    if not events:
        return None
    counts: Dict[str, int] = {}
    for event in events:
        model = (event.get("properties") or {}).get("model")
        if model:
            counts[model] = counts.get(model, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    parts = [
        "%s (%d view%s)" % (model, count, "" if count == 1 else "s") for model, count in ranked
    ]
    return "Recently viewed: " + ", ".join(parts)


def summarise_signals(signals: Sequence[str]) -> Optional[str]:
    """Buying-intent markers, in words the agent can act on."""
    if not signals:
        return None
    readable = {
        "emi_page_viewed": "looked at EMI options",
        "test_ride_page_viewed": "looked at booking a test ride",
        "dealer_locator_used": "searched for a dealer",
        "add_to_cart": "added a bike to cart",
        "checkout_started": "started checkout",
    }
    named = [readable.get(signal, signal) for signal in signals]
    return "Intent signals: " + ", ".join(named)


class ContextEnricher:
    def __init__(self, token_budget: int = DEFAULT_TOKEN_BUDGET) -> None:
        self.token_budget = token_budget

    def build(
        self,
        resolved: ResolvedIdentity,
        events: Sequence[Dict[str, Any]] = (),
        signals: Sequence[str] = (),
        last_conversation: Optional[str] = None,
    ) -> EnrichedContext:
        candidates: Dict[str, str] = {}

        if resolved.may_disclose:
            name = (resolved.profile or {}).get("name")
            if name:
                candidates["identity"] = "Customer: %s (identity verified)." % name
            if resolved.bikes:
                candidates["bikes"] = self._bikes_block(resolved.bikes)
            if last_conversation:
                candidates["recent_conversation"] = "Last contact: %s" % last_conversation
        else:
            # Anonymous or merely asserted. A cookie tells us what this *browser*
            # looked at, which is safe to use, and nothing about who is holding it.
            candidates["identity"] = (
                "Customer: not verified. Do not state any name, bike, frame number or warranty "
                "status. Browsing interest below may be referenced generally."
            )

        browsing = summarise_browsing(events)
        if browsing:
            candidates["browsing"] = browsing
        signal_line = summarise_signals(signals)
        if signal_line:
            candidates["signals"] = signal_line

        return self._fit(candidates)

    def _bikes_block(self, bikes: Sequence[Dict[str, Any]]) -> str:
        lines = ["Owns %d bike%s:" % (len(bikes), "" if len(bikes) == 1 else "s")]
        for bike in bikes:
            descriptor = bike.get("product_name") or "unknown model"
            if bike.get("product_color"):
                descriptor += " (%s)" % bike["product_color"]
            if bike.get("coverage_status") == "purchase_date_missing":
                coverage = "coverage unknown — no purchase date on record"
            elif bike.get("in_warranty") is True:
                coverage = "in warranty, %d month(s) left" % bike.get("months_remaining", 0)
            elif bike.get("in_warranty") is False:
                coverage = "out of warranty"
            else:
                coverage = "coverage unknown"
            lines.append("- %s, frame %s, %s" % (descriptor, bike.get("frame_number"), coverage))
        return "\n".join(lines)

    def _fit(self, candidates: Dict[str, str]) -> EnrichedContext:
        """Keep the highest-value sections that fit the budget, drop the rest.

        Two deliberate choices:

        * Sections are kept **whole or not at all**. Truncating mid-sentence
          leaves a fragment the model reads as a complete fact.
        * Packing is **best-fit, not a strict cut-off**: a cheap low-priority
          section may survive when an expensive higher-priority one did not fit,
          rather than wasting the remaining budget. Priority still decides who
          gets first claim on it, and `dropped` records what went.
        """
        context = EnrichedContext()
        for name in SECTION_PRIORITY:
            block = candidates.get(name)
            if block is None:
                continue
            cost = _tokens(block)
            if context.tokens + cost > self.token_budget:
                context.dropped.append(name)
                continue
            context.sections[name] = block
            context.tokens += cost
        return context
