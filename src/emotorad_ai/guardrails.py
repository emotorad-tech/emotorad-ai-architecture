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
    # "swollen" is the irregular past participle and the way people actually say
    # it — "my battery is swollen" was passing straight through, and swelling is
    # the canonical pre-fire symptom on a lithium pack.
    ("swelling", r"swell(?:ing|ed|s)?|swollen|bulg(?:e|ed|ing|y)|puff(?:ed|y|ing)|expand(?:ed|ing)"),
    ("smoke", r"smok(?:e|ing|y)|fumes?|\bdhuan\b"),
    ("fire", r"\bfire\b|flames?|caught fire|burn(?:ing|t|ed)?\b|\baag\b|jal gaya"),
    ("burning_smell", r"burning smell|smell(?:s|ed|ing)? (?:of )?burn|acrid|chemical smell"),
    ("overheating", r"too hot to touch|very hot|extremely hot|overheat(?:ing|ed|s)?|scalding"),
    ("leak", r"leak(?:ing|ed|s|age)?|fluid coming|liquid coming"),
    ("physical_damage",
     # \b on `dent`: unbounded, it matched inside "accident" and "incident",
     # which is a lot of ordinary sentences escalated for nothing.
     #
     # `melted` is deliberately absent. A melted terminal or connector is a
     # thermal event that has already finished, and the case then turns on how
     # far the heat travelled — which needs the battery *and* controller photos
     # the agent collects (prompt §5b1, and E-06's verification rule). Handing it
     # straight over meant that assessment never happened and the comparison
     # photos were never sent. Swelling is the opposite and stays above: a pack
     # that is swollen is venting gas now, not showing damage from before.
     r"crack(?:ed|s|ing)?|\bdent(?:ed|s)?\b|deformed|punctured|damaged casing"),
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


# Words that, within a few tokens before a hazard term, mean the writer is
# saying it was *absent*. The video analyser is a careful observer and a
# careful observer writes "no smoke visible"; without this, every benign clip
# would hard-stop the conversation and open a critical ticket.
_NEGATIONS = frozenset({"no", "not", "without", "none", "absent", "never", "nor"})
# How many tokens back a negation still governs the term. Four covers "no
# signs of visible swelling"; further back it is more likely a different clause.
_NEGATION_WINDOW = 4
_PUNCTUATION = str.maketrans("", "", ".,;:!?()[]\"'")


def _has_negation(tokens: Sequence[str]) -> bool:
    return any(token.lower().translate(_PUNCTUATION) in _NEGATIONS for token in tokens)


def _negated(text: str, start: int, end: int) -> bool:
    if _has_negation(text[:start].split()[-_NEGATION_WINDOW:]):
        return True
    # The label form, "Sparks: not visible": the negation follows the term.
    # Only a colon straight after the match opens that window, so "the pack
    # is swollen, not cracked" still counts the swelling.
    after = text[end:].lstrip()
    return after.startswith(":") and _has_negation(after[1:].split()[:_NEGATION_WINDOW])


def check_safety_in_description(text: str) -> GuardrailVerdict:
    """The safety gate for machine-written descriptions of a clip.

    Same patterns as `check_safety`, but a match preceded within the last
    four words by a negation is dropped, as is the label form "Term: not
    visible". Typed customer text keeps the plain
    scan: a customer who writes "not swelling but very hot" is still in
    trouble, and over-triggering on their words costs one ticket. A
    description that lists what it did not see is the normal case, and
    over-triggering on it would make the video path unusable.
    """
    matched = []
    for label, pattern in _ALL_SAFETY_PATTERNS:
        for found in pattern.finditer(text or ""):
            if not _negated(text, found.start(), found.end()):
                matched.append(label)
                break
    return GuardrailVerdict(triggered=bool(matched), matched=matched)


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
    negative = [m.span() for m in _NOT_COVERED_CLAIM.finditer(reply)]
    # A "covered" match that overlaps a negative one is that negative phrase's
    # own tail — "is not covered", "chargeable even within warranty" — not a
    # separate claim. Only a match standing on its own counts as the positive
    # half. Without this, every correct refusal would read as saying both.
    positive = [
        m.span()
        for m in _COVERED_CLAIM.finditer(reply)
        if not any(m.start() < n_end and n_start < m.end() for n_start, n_end in negative)
    ]
    says_covered = bool(positive)
    says_not_covered = bool(negative)
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

    # Coverage is two questions, and a correct answer to a customer in the
    # term with physical damage says both: "you are in warranty" and "impact
    # damage would be chargeable". That is the rule the business confirmed and
    # battery-warranty-replacement encodes. A reply saying both is a
    # conditional explanation, not a contradiction — provided the positive
    # half is actually true. When the tool says the term has ended, the
    # "in warranty" half is unsupported and the reply is blocked as before.
    if says_covered and says_not_covered:
        if actual:
            return CoverageCheck(blocked=False)
        return CoverageCheck(
            blocked=True,
            reason="coverage_claim_contradicts_tool_result",
            claimed="covered",
            actual="not_covered",
        )

    if says_covered != actual:
        return CoverageCheck(
            blocked=True,
            reason="coverage_claim_contradicts_tool_result",
            claimed=claimed,
            actual="covered" if actual else "not_covered",
        )
    return CoverageCheck(blocked=False)


# --- evidence post-check -----------------------------------------------------
# A fault conclusion, a warranty path or a raised ticket, asserted in a reply
# when no photo or video has ever arrived in the conversation.
#
# This is a code check for the same reason the coverage one is. The rule existed
# in the prompt in four different forms across three versions and was skipped
# every time: stated generally at 26% of a 22,000-character prompt, it lost to
# whichever procedure the model was working through at 74%. A rule only holds
# where it is written, and a long prompt cannot have it written everywhere.
#
# Deliberately narrow. It does not ask whether the evidence was *good* — a human
# judges that — only whether any arrived. "The bot concluded a fault having seen
# nothing" is a question with an answer; "was the photo convincing" is not.

EVIDENCE_BLOCKED_MESSAGE = (
    "Before I can take this further I need to see it — please send a photo or a short video "
    "of what you are describing, and I will pick it straight up from there."
)

# Conclusions that must rest on something seen. Phrased for what a support bot
# actually writes, not for what a spec would say.
_FAULT_CONCLUSION = re.compile(
    r"\b(?:"
    r"battery (?:is|appears|seems|looks) (?:dead|faulty|failed)"
    r"|(?:is|appears|seems|looks) (?:to be )?(?:dead|faulty|defective)"
    r"|needs? (?:to be )?(?:replaced|replacement)"
    r"|(?:raise|raising|raised|open|opening|opened) (?:a |the )?(?:support |warranty |zoho )?ticket"
    r"|(?:start|starting|begin|beginning) the (?:warranty|replacement)"
    r"|(?:under|covered by) warranty[, ]+so we(?:'| a)?ll (?:replace|repair)"
    r"|proceed(?:ing)? (?:with|to) (?:the )?(?:warranty|replacement)"
    r"|arrange (?:a )?(?:replacement|repair)"
    # The bare noun phrase, which is how a model most often states it —
    # "that's pointing toward a dead battery" was the real reply that started
    # this. Hedges are stripped below rather than enumerated here.
    r"|(?:dead|faulty|failed) battery"
    r")\b",
    re.IGNORECASE,
)

# A conclusion is not a conclusion when it is being ruled out, weighed against
# something else, or named as a possibility. Without this the check fires on the
# correct "it could be the battery or another part — go to a dealer" reply, which
# is the one honest answer available when there is no multimeter.
_HEDGE_BEFORE = re.compile(
    r"(?:"
    r"could be|might be|may be|maybe|possibly|perhaps"
    r"|can(?:'|no)?t (?:tell|say|be sure|confirm)|cannot (?:tell|say|confirm)"
    r"|not (?:sure|certain|clear)|unclear|unsure"
    r"|whether|if it(?:'| i)?s|in case|rule out|ruling out"
    r"|either|or another part|or something else"
    r"|do not conclude|don'?t conclude"
    r")",
    re.IGNORECASE,
)

# Safety short-circuits everything: never hold a dangerous battery behind a
# request for a photograph of it.
_SAFETY_HANDOVER = re.compile(
    r"stop (?:using|charging)|safety team|do not (?:use|charge)|unplug it now", re.IGNORECASE
)


@dataclass(frozen=True)
class EvidenceCheck:
    blocked: bool
    reason: str = ""
    matched: str = ""


def check_evidence(reply: str, evidence_seen: bool, safety_triggered: bool = False) -> EvidenceCheck:
    """Block a fault conclusion reached without any photo or video.

    ``evidence_seen`` is whether *any* attachment has arrived in this
    conversation, not this turn: a customer who sent the picture three turns ago
    should not be asked again because the model concluded later.
    """
    if safety_triggered or _SAFETY_HANDOVER.search(reply or ""):
        # A hazard is handled on the customer's word, immediately, every time.
        return EvidenceCheck(blocked=False, reason="safety_exempt")
    if evidence_seen:
        return EvidenceCheck(blocked=False)
    found = _FAULT_CONCLUSION.search(reply or "")
    if not found:
        return EvidenceCheck(blocked=False)
    # Look at the sentence the match sits in, not the whole reply: a hedge three
    # paragraphs earlier does not soften a conclusion stated flatly here.
    text = reply or ""
    sentence_start = max(text.rfind(".", 0, found.start()), text.rfind("\n", 0, found.start())) + 1
    if _HEDGE_BEFORE.search(text[sentence_start:found.end()]):
        return EvidenceCheck(blocked=False, reason="hedged")
    return EvidenceCheck(
        blocked=True, reason="fault_concluded_without_evidence", matched=found.group(0)
    )


# --- order post-check -------------------------------------------------------
# Replacement order ids as place_replacement_order issues them. Tickets are
# EM- and dealer orders SO-; only RO- is a claim this check owns.
_ORDER_ID = re.compile(r"\bRO-\d{5}\b")

ORDER_BLOCKED_MESSAGE = (
    "Let me get this confirmed for you properly. I am passing this to our support team so "
    "they can check the order and come back to you."
)

# Words that tell the customer the order they are hearing about existed
# before this turn. On 2026-09-21 the tool returned the morning's order and
# the model read "That's done, your battery is ordered" over it, to an address
# the customer had not confirmed in that conversation. A reply about an
# in-flight order must carry one of these or it is reporting the old order as
# a new one. Hindi and Hinglish included: "pehle se" is how it is said.
_ALREADY = re.compile(
    r"\b(already|earlier|before|existing|previous(ly)?|in flight|pehle|pahle)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class OrderCheck:
    blocked: bool
    reason: str = ""
    claimed: str = ""


def check_order_claim(reply: str, tool_results: Sequence[dict]) -> OrderCheck:
    """Block a reply that names an order no tool in the conversation placed.

    The same control as the coverage post-check, pointed at shipments: calling
    the order tool proves it ran, not that the reply names the order it
    returned. An order id the model made up would send a customer to wait for
    a battery nobody is sending.
    """
    placed = set()
    in_flight = None
    for result in tool_results:
        data = (result or {}).get("data") or {}
        order_id = data.get("order_id")
        if isinstance(order_id, str):
            placed.add(order_id)
            if data.get("already_placed") is True:
                in_flight = order_id
    for claimed in _ORDER_ID.findall(reply):
        if claimed not in placed:
            return OrderCheck(blocked=True, reason="order_claim_without_tool_result", claimed=claimed)
    # The tool refused to place a second order and handed back the first. The
    # reply must say so; a reply that does not is telling the customer a new
    # order went to the address they just gave, when nothing did.
    if in_flight is not None and not _ALREADY.search(reply or ""):
        return OrderCheck(blocked=True, reason="existing_order_reported_as_new", claimed=in_flight)
    return OrderCheck(blocked=False)
