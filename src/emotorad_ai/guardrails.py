"""Deterministic gates that run before the model sees the turn (build plan §4).

Two things must never be a model judgement call:

* the battery-safety branch — swelling, smoke, heat, fire or physical damage is
  a safety issue, not a support issue, and the agent must not be given the
  chance to keep troubleshooting through it;
* the request for a human — a customer asking to talk to someone exits
  immediately, at any point in the flow, with no friction.

Both are matched here with plain patterns and enforced by `runtime`, so a prompt
change can never silently disable either one. False positives are the acceptable
direction of error: a safety escalation on an ordinary complaint costs one
ticket, a missed one costs a customer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Pattern, Sequence, Tuple

# (label, pattern). Labels land in the log so ops can see which phrasing fired
# and tune the list against real transcripts.
_SAFETY_TERMS: Sequence[Tuple[str, str]] = (
    ("swelling", r"swell(?:ing|ed|s)?|bulg(?:e|ed|ing)|puff(?:ed|y|ing)|expand(?:ed|ing)"),
    ("smoke", r"smok(?:e|ing|y)|fumes?|\bdhuan\b"),
    ("fire", r"\bfire\b|flames?|caught fire|burn(?:ing|t|ed)?\b|\baag\b|jal gaya"),
    ("burning_smell", r"burning smell|smell(?:s|ed|ing)? (?:of )?burn|acrid|chemical smell"),
    ("overheating", r"too hot to touch|very hot|extremely hot|overheat(?:ing|ed|s)?|scalding"),
    ("leak", r"leak(?:ing|ed|s|age)?|fluid coming|liquid coming"),
    ("physical_damage",
     # \b on `dent`: unbounded, it matched inside "accident" and "incident",
     # which is a lot of ordinary sentences escalated for nothing.
     r"crack(?:ed|s|ing)?|\bdent(?:ed|s)?\b|melted|deformed|punctured|damaged casing"),
    ("sparks", r"spark(?:s|ing|ed)?|short circuit|shock(?:ed|ing)?\b"),
)

# Drive-system safety. A different risk from the battery: not a pack that may
# catch fire in a hallway, but a bike that may fail *while someone is riding it*.
# So "try it and see" is never acceptable advice for these.
#
# Deliberately narrow. Routine "power cuts out" is an ordinary support case with
# its own knowledge record — making that a safety stop would break the happy path
# for every intermittent-connector complaint. What is here is loss of control.
_MOTOR_SAFETY_TERMS: Sequence[Tuple[str, str]] = (
    ("brake_failure", r"brakes? (?:are |is |not |stopped |aren'?t |isn'?t )*(?:not )?work\w*|no brakes|brake fail\w*"),
    ("wheel_lock", r"wheel (?:locked|locking|seized|jammed)|rear wheel stopped"),
    (
        "uncommanded_power",
        r"(?:motor|bike|it) (?:starts?|started|runs?|ran|took off|takes off|moves?|moved)"
        r"(?: \w+){0,2} (?:on its own|by itself|without)|accelerat\w+ (?:on its own|by itself)",
    ),
    # Any report of injury escalates immediately, whatever the component.
    (
        "injury",
        # Word boundaries matter here: without them "dent" matches inside
        # "accident" and "incident", escalating ordinary conversations.
        r"\bfell off\b|\bcrashed?\b|\baccident\b|\binjur\w*|"
        r"\bhurt (?:myself|my|me)\b|\bwounded\b",
    ),
)

_HANDOFF_TERMS: Sequence[Tuple[str, str]] = (
    (
        "asks_for_human",
        # "talk to a human", "connect me to an agent", "put me through to someone"
        r"(?:talk|speak|connect|transfer|chat|put)\s+(?:me|us)?\s*(?:to|with|through)\s+(?:to\s+)?"
        r"(?:a|an|the|my|our)?\s*(?:human|person|agent|someone|somebody|executive|"
        r"representative|rep)\b",
    ),
    ("asks_for_support_team", r"customer (?:care|support|service)\b|call me\b|real person\b|human being\b"),
    ("rejects_bot", r"(?:not|don'?t want)\s+(?:a\s+)?(?:bot|robot|ai)\b|stop the bot\b"),
    (
        # Dealers do not ask for "an agent" — they ask for their account manager
        # by name of role. Same intent, entirely different vocabulary, and the
        # customer-shaped pattern above misses every one of them.
        "asks_for_account_manager",
        # An explicit request verb is required. Without it, "my account manager
        # said I get 5% discount" reads as a transfer request when it is really
        # an argument for a discount — which the money guardrails already refuse
        # in code, so the agent can handle it without a human.
        r"(?:talk|speak|connect|transfer|chat|put|call)\b[^.?!]{0,30}?"
        r"\b(?:(?:account|area|sales|regional|relationship)\s+manager|asm|rsm)\b",
    ),
)


def _compile(terms: Sequence[Tuple[str, str]]) -> List[Tuple[str, Pattern[str]]]:
    return [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in terms]


_SAFETY_PATTERNS = _compile(_SAFETY_TERMS)
_MOTOR_SAFETY_PATTERNS = _compile(_MOTOR_SAFETY_TERMS)
_HANDOFF_PATTERNS = _compile(_HANDOFF_TERMS)
# One gate for the whole conversation. Which sub-agent would have handled the
# message is irrelevant: safety runs *before* triage, so the topic is not yet
# known, and a customer describing brake failure must not depend on having been
# routed to the right agent first.
_ALL_SAFETY_PATTERNS = _SAFETY_PATTERNS + _MOTOR_SAFETY_PATTERNS

SAFETY_MESSAGE = (
    "Please stop using and stop charging the battery right now, and move it away from "
    "anything flammable and away from people. Do not try to open, repair or charge it "
    "again, and do not put it in water.\n\n"
    "What you have described is a safety issue rather than a normal support question, so "
    "I am handing this to our safety team immediately rather than troubleshooting it here. "
    "They will call you on the number linked to your account.\n\n"
    "If you can see smoke or flames right now, move away from the bike and call emergency "
    "services on 112."
)

HANDOFF_MESSAGE = (
    "Of course — I am connecting you to a member of our support team now. I have passed "
    "on this conversation so you will not need to repeat yourself."
)


@dataclass(frozen=True)
class GuardrailVerdict:
    triggered: bool
    matched: List[str]

    def __bool__(self) -> bool:
        return self.triggered


def _scan(text: str, patterns: Sequence[Tuple[str, Pattern[str]]]) -> GuardrailVerdict:
    matched = [label for label, pattern in patterns if pattern.search(text or "")]
    return GuardrailVerdict(triggered=bool(matched), matched=matched)


def check_safety(text: str) -> GuardrailVerdict:
    """Hard stop: physical danger, battery or drive system.

    Runs ahead of the agent turn, always, and before triage — so it fires whether
    or not we have worked out which component the customer means. Over-triggering
    is the acceptable direction of error: a needless escalation costs one ticket,
    a missed one costs a customer.
    """
    return _scan(text, _ALL_SAFETY_PATTERNS)


# Battery-only, kept for the tests and callers written before motor support.
def check_battery_safety(text: str) -> GuardrailVerdict:
    return _scan(text, _SAFETY_PATTERNS)


def check_human_handoff(text: str) -> GuardrailVerdict:
    """Customer asked for a person. Exits the flow wherever it happens."""
    return _scan(text, _HANDOFF_PATTERNS)


# --- coverage post-check -----------------------------------------------------
#
# The highest-value control in the system, and the one that closes the Air Canada
# precedent: a tribunal held the airline to a policy its chatbot invented.
#
# Calling the warranty tool guarantees the tool *ran*. It does not guarantee the
# reply matches what the tool returned — and a model that has just been told a
# bike is out of warranty can still produce a warm, plausible "yes, that's
# covered under warranty". Nothing upstream catches that: the tool call
# succeeded, the prompt was correct, and the sentence reads well.
#
# So the reply is checked against the turn's tool results before it is sent.

_COVERED_CLAIM = re.compile(
    r"(?:is|are|it'?s|this is|that'?s|you'?re|fully|still)\s+(?:\w+\s+){0,2}"
    r"(?:covered|under (?:the )?warranty|in warranty|within warranty)"
    r"|covered under (?:the )?warranty"
    r"|(?:no|free of|without)\s+(?:charge|cost)"
    r"|(?:at )?no cost to you"
    r"|we(?:'| wi)ll (?:repair|replace) (?:it|this) (?:free|at no)",
    re.IGNORECASE,
)

_NOT_COVERED_CLAIM = re.compile(
    r"(?:not|no longer|isn'?t|aren'?t|won'?t be)\s+(?:\w+\s+){0,2}"
    r"(?:covered|under warranty|in warranty)"
    r"|out of (?:the )?warranty"
    r"|warranty (?:has )?(?:expired|lapsed|ended)"
    r"|(?:will be |is )?chargeable|you would be charged|at your (?:own )?cost",
    re.IGNORECASE,
)

COVERAGE_BLOCKED_MESSAGE = (
    "Let me get this confirmed for you properly. I am passing this to our support team so "
    "they can check your warranty and come back to you."
)


@dataclass(frozen=True)
class CoverageCheck:
    blocked: bool
    reason: str = ""
    claimed: str = ""
    actual: str = ""


def _coverage_facts(tool_results: Sequence[dict]) -> List[bool]:
    """Every in/out-of-warranty fact this turn's tools actually returned."""
    facts: List[bool] = []
    for result in tool_results:
        data = (result or {}).get("data") or {}
        for bike in data.get("bikes", []):
            if isinstance(bike.get("in_warranty"), bool):
                facts.append(bike["in_warranty"])
        if isinstance(data.get("in_warranty"), bool):
            facts.append(data["in_warranty"])
    return facts


def check_coverage_claim(reply: str, tool_results: Sequence[dict]) -> CoverageCheck:
    """Block a reply whose coverage claim contradicts this turn's tool results.

    Deliberately conservative in both directions:

    * A coverage claim with **no** supporting tool result is blocked. An
      unsupported "yes, that's covered" is exactly the Air Canada failure, and
      the model has no other source for it.
    * A claim that contradicts the result is blocked, both ways round. Telling a
      covered customer they must pay is a different harm from the reverse, but it
      is equally wrong and equally invisible.
    * Where a customer owns several bikes and the results disagree, any claim is
      blocked — the reply cannot be verified against "one of them is covered".
    """
    says_covered = bool(_COVERED_CLAIM.search(reply))
    says_not_covered = bool(_NOT_COVERED_CLAIM.search(reply))

    # "not covered" also matches the "covered" pattern's tail, so a negative
    # phrasing must win — otherwise every correct refusal reads as a claim.
    if says_not_covered:
        says_covered = False
    if not (says_covered or says_not_covered):
        return CoverageCheck(blocked=False)

    facts = _coverage_facts(tool_results)
    claimed = "covered" if says_covered else "not_covered"

    if not facts:
        return CoverageCheck(
            blocked=True, reason="coverage_claim_without_tool_result", claimed=claimed
        )
    if len(set(facts)) > 1:
        return CoverageCheck(
            blocked=True, reason="coverage_claim_across_disagreeing_bikes", claimed=claimed
        )

    actual = facts[0]
    if says_covered != actual:
        return CoverageCheck(
            blocked=True,
            reason="coverage_claim_contradicts_tool_result",
            claimed=claimed,
            actual="covered" if actual else "not_covered",
        )
    return CoverageCheck(blocked=False)
