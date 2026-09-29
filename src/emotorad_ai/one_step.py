"""One step per reply: the rule every customer agent is given, and the backstop in code.

The person's decision, 2026-09-29: a customer gets one thing at a time (one
step, or one question, in a few short sentences) and the next once they have
answered, not everything at once.

The rule is appended to the system prompt by the agent loop (agents/base.py),
last, where it is read after the procedures, and where a promoted prompt
cannot lose it. A rule held only in a prompt has been skipped here before (see
guardrails.py, evidence post-check), so the runtime also checks every agent
reply against the limits below and, when one runs over, asks the same model
once to cut it to its first step or question (Runtime._one_step). The cut is
used only when it is within the limits and keeps every reference; otherwise
the original is sent and the reason logged.

The dealer agent is exempt (AgentDefinition.one_step): an order summary has to
list every line for the dealer to confirm it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .conversation import _is_customer_turn

ONE_STEP_RULE = """

Reply format, for every reply: one thing at a time. Give one step, or ask one \
question, in at most three short sentences, then stop and wait for the customer's \
answer before the next step. Never list several steps or several questions in one \
reply. When you raise a ticket or book a visit, give its reference and what happens \
next, and nothing more."""

# The backstop's limits: a little above what the rule asks for, so it catches a
# reply that ignored the rule, not one that ran a word over.
MAX_WORDS = 80
MAX_SENTENCES = 4

# The ids the tools mint (tickets, bookings, replacement orders). A cut that
# loses one would leave the customer without the reference to quote.
_REFERENCE = re.compile(r"\b(?:EM|BK|RO)-\d{5}\b")

# A sentence ends at . ! or ? followed by the end, or by a space and something
# that does not start in lower case: so "1.8.2026", "10.30" and "a.m. is" are not
# breaks, and "1. Check" in a numbered list is (a list is several steps). The
# danda ends a Hindi sentence.
_END = re.compile(r"[.!?]+(?=\s+[^a-z\s]|\s*$)|।")
_ENDS_WITH_STOP = re.compile(r"(?:[.!?]+|।)\s*$")

SHORTEN_SYSTEM = """\
You edit replies from EMotorad's customer support assistant before the customer sees \
them. Rewrite the reply you are given so that it does one thing: its first step, or \
its single most important question, in at most three short sentences. Keep its \
language and tone. Keep every reference number (such as EM-00001, BK-00001 or \
RO-00001) exactly as written. Add nothing that is not in the reply. Answer with the \
rewritten reply only."""


def words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def sentences(text: str) -> int:
    """Sentences, counted line by line, so a bulleted list counts every line."""
    total = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        count = len(_END.findall(line))
        if not _ENDS_WITH_STOP.search(line):
            count += 1
        total += max(count, 1)
    return total


def is_too_long(text: str) -> bool:
    return words(text) > MAX_WORDS or sentences(text) > MAX_SENTENCES


def references(text: str) -> Set[str]:
    return set(_REFERENCE.findall(text or ""))


def shorten(llm: Any, text: str) -> Tuple[Optional[str], str, Any]:
    """Ask `llm` once to cut `text` to its first step.

    Returns (cut, reason, response): the cut, or None with why it cannot be
    used ("no_text", "still_too_long", "reference_lost"). A failed call raises
    to the caller, which logs it and sends the original.
    """
    response = llm.create(SHORTEN_SYSTEM, [{"role": "user", "content": text}], [])
    cut = (getattr(response, "text", "") or "").strip()
    if not cut or getattr(response, "wants_tools", False):
        return None, "no_text", response
    if references(text) - references(cut):
        return None, "reference_lost", response
    if is_too_long(cut):
        return None, "still_too_long", response
    return cut, "shortened", response


def replace_turn_text(history: List[Dict[str, Any]], text: str) -> None:
    """Make this turn's record say what the customer was sent.

    The reply the customer gets is every text the model wrote in the turn, so
    each of this turn's assistant entries loses its text blocks (tool calls and
    anything else stay, paired with their results) and the last one carries the
    cut. Otherwise the next turn's model believes the customer saw every step
    that was cut, and carries on from the last of them.
    """
    start = max((i for i, entry in enumerate(history) if _is_customer_turn(entry)), default=-1)
    assistant = [i for i in range(start + 1, len(history)) if history[i].get("role") == "assistant"]
    if not assistant:
        return
    for i in assistant:
        content = history[i].get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        history[i] = dict(history[i], content=[b for b in blocks if b.get("type") != "text"])
    last = history[assistant[-1]]
    last["content"] = list(last["content"]) + [{"type": "text", "text": text}]
