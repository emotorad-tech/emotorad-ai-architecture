"""Jev routing: the questions we ask, the state we send, and the path we pick.

Everything here is deterministic. Jev supplies probabilities; this module
turns them into one of three paths, and nothing else in the system makes that
call. Keeping it pure (no I/O, no model) is what lets every rule and every
threshold boundary be table-tested.

The questions are built from files (knowledge records, error codes, approved
standard responses), never hand-written, so adding a knowledge record makes
it routable without touching this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errorcodes import ANY_CODE, ErrorCodeTable
from .jev import ChoiceAnswer, JevDecision, NoulAnswer, Question, choice, noul
from .knowledge import KNOWLEDGE_DIR, KnowledgeBase, KnowledgeRecord
from .observability import redact_pii
from .standard_responses import LANGUAGES, StandardResponse
from .tools.mocks import LOOKUP_ERROR_CODE, LOOKUP_WARRANTY_RECORD

THRESHOLDS_PATH = KNOWLEDGE_DIR / "_routing" / "thresholds.yaml"

Q_STANDARD = "standard_response"
Q_CATEGORY = "category"
Q_SUB_CATEGORY = "sub_category"
Q_ERROR_CODE = "error_code"
Q_LANGUAGE = "language"
Q_WARRANTY = "needs_warranty_lookup"

NONE = "none"
NONE_OF_THESE = "none_of_these"
# Not a Jev error: the reason a turn skipped Jev because there was no text to
# score (a photo on its own).
EMPTY_MESSAGE = "empty_message"

RECENT_TURNS = 3

# An Indian mobile however it is typed: "98765 43210", "+91-98765-43210",
# "919876543210". observability.redact_pii catches only the unbroken form, and
# Jev's state leaves AWS, so it gets the wider net.
_ANY_INDIAN_MOBILE = re.compile(r"(?<!\d)(?:\+?91[\s-]*)?[6-9](?:[\s-]*\d){9}(?!\d)")
# Word characters including Devanagari, for whole-word matching. `\w` alone
# misses Devanagari vowel signs (see CLAUDE.md), so the range is explicit.
_WORD_CHAR = r"[\wऀ-ॿ]"

CATEGORY_INSTRUCTIONS = "Which area of the bike is the customer's message about?"
CATEGORY_CRITERIA = {
    "battery": (
        "The battery or charging: will not charge, charges slowly, range or backup has dropped, "
        "the bike will not power on, the battery indicator or on/off switch, damage to the battery "
        "or its charging port, storing the battery, or a battery replacement."
    ),
    "motor": (
        "The drive while riding: motor noise, no pedal assist, the throttle does not respond, "
        "power cutting out while riding, or jerking."
    ),
    NONE_OF_THESE: (
        "Anything else: buying a new bike, prices, orders and delivery, refunds, accessories, "
        "general questions, or a message that describes no problem at all."
    ),
}
SUB_CATEGORY_INSTRUCTIONS = (
    "Which documented issue matches what the customer describes? Pick none unless one matches closely."
)
STANDARD_INSTRUCTIONS = (
    "Is the whole message fully answered by one of these standard replies? "
    "Pick none unless one clearly fits the entire message."
)
STANDARD_NONE = (
    "None fits: the message needs more than a standard reply, or asks about this customer's "
    "bike, order, warranty or a fault."
)
ERROR_CODE_INSTRUCTIONS = "Does the customer mention an error code shown on the bike's display?"
LANGUAGE_INSTRUCTIONS = "Which language is the customer's message written in?"
LANGUAGE_CRITERIA = {
    "english": "English.",
    "hindi": "Hindi written in Devanagari script.",
    "hinglish": "Hindi written in Latin letters, often mixed with English words.",
    "marathi": "Marathi, in any script.",
    "tamil": "Tamil, in any script.",
    "other": "Any other language, or too short to tell.",
}
WARRANTY_INSTRUCTIONS = (
    "The customer asks whether something is covered by warranty, or asks about warranty, "
    "a free replacement, or what a repair will cost."
)

_SCALAR_KEYS = ("standard_response", "language", "category", "sub_category", "error_code")
_TOOL_KEYS = ("lookup_warranty_record",)
_META_KEYS = ("calibrated_at", "calibration_set_size")


class RoutingConfigError(Exception):
    """thresholds.yaml is malformed. Raised at startup, never mid-conversation."""


@dataclass(frozen=True)
class Thresholds:
    standard_response: float = 0.90
    language: float = 0.80
    category: float = 0.85
    sub_category: float = 0.75
    error_code: float = 0.85
    tools: Mapping[str, float] = field(default_factory=lambda: {"lookup_warranty_record": 0.60})


def _threshold(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
        raise RoutingConfigError("%s must be a number in (0, 1], got %r" % (where, value))
    return float(value)


def load_thresholds(path: Optional[Path] = None) -> Thresholds:
    import yaml

    target = Path(path) if path else THRESHOLDS_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise RoutingConfigError("%s must be a mapping" % target)
    unknown = set(raw) - set(_SCALAR_KEYS) - {"tools"} - set(_META_KEYS)
    if unknown:
        raise RoutingConfigError("%s has unknown keys: %s" % (target, ", ".join(sorted(unknown))))
    defaults = Thresholds()
    values = {key: _threshold(raw[key], key) if key in raw else getattr(defaults, key) for key in _SCALAR_KEYS}
    tools_raw = raw.get("tools") or {}
    if not isinstance(tools_raw, Mapping):
        raise RoutingConfigError("tools must map a tool name to a threshold")
    unknown_tools = set(tools_raw) - set(_TOOL_KEYS)
    if unknown_tools:
        raise RoutingConfigError("unknown prefetch tools: %s" % ", ".join(sorted(unknown_tools)))
    tools = dict(defaults.tools)
    tools.update({name: _threshold(value, "tools." + name) for name, value in tools_raw.items()})
    return Thresholds(tools=tools, **values)


@dataclass(frozen=True)
class RoutingCatalogue:
    records: Mapping[str, KnowledgeRecord]
    error_codes: Sequence[str]
    standard: Mapping[str, StandardResponse]
    applicable: Callable[[KnowledgeRecord, Mapping[str, Any]], bool]


def build_catalogue(
    knowledge_base: KnowledgeBase,
    standard: Sequence[StandardResponse] = (),
    error_table: Optional[ErrorCodeTable] = None,
    include_drafts: bool = False,
) -> RoutingCatalogue:
    codes = sorted({entry.code for entry in error_table.entries if entry.code != ANY_CODE}) if error_table else []
    return RoutingCatalogue(
        records={record.id: record for record in knowledge_base.records},
        error_codes=tuple(codes),
        standard={s.id: s for s in standard if s.approved or include_drafts},
        applicable=knowledge_base.applicable,
    )


def _describe_record(record: KnowledgeRecord) -> str:
    symptoms = "; ".join(s.replace("_", " ") for s in list(record.symptoms)[:10])
    return "%s. Customers say things like: %s" % (record.title, symptoms)


def _spellings(code: str) -> str:
    number = int(code[1:]) if code[1:].isdigit() else None
    if number is None:
        return code
    return "E-%02d, E%d or E %02d" % (number, number, number)


def build_questions(catalogue: RoutingCatalogue) -> Dict[str, Question]:
    questions: Dict[str, Question] = {}
    if catalogue.standard:
        criteria = {s.id: s.criteria for s in catalogue.standard.values()}
        criteria[NONE] = STANDARD_NONE
        questions[Q_STANDARD] = choice(STANDARD_INSTRUCTIONS, criteria)
    questions[Q_CATEGORY] = choice(CATEGORY_INSTRUCTIONS, CATEGORY_CRITERIA)
    sub_categories = {record.id: _describe_record(record) for record in catalogue.records.values()}
    sub_categories[NONE] = "None of these issues matches what the customer describes."
    questions[Q_SUB_CATEGORY] = choice(SUB_CATEGORY_INSTRUCTIONS, sub_categories)
    if catalogue.error_codes:
        codes = {
            code: "The customer mentions display error %s (it may be written %s)." % (code, _spellings(code))
            for code in catalogue.error_codes
        }
        codes[NONE] = "The customer does not mention a display error code."
        questions[Q_ERROR_CODE] = choice(ERROR_CODE_INSTRUCTIONS, codes)
    # The language labels are the keys standard replies are written under; a
    # label Jev can return that no reply can be written for would never route.
    assert set(LANGUAGE_CRITERIA) - {"other"} == set(LANGUAGES)
    questions[Q_LANGUAGE] = choice(LANGUAGE_INSTRUCTIONS, LANGUAGE_CRITERIA)
    questions[Q_WARRANTY] = noul(
        WARRANTY_INSTRUCTIONS,
        true="The customer asks about warranty, cover or the cost of a repair.",
        false="The customer does not ask about warranty, cover or cost.",
    )
    return questions


def _text_of(entry: Mapping[str, Any]) -> str:
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def _recent_texts(history: Sequence[Mapping[str, Any]], limit: int) -> List[str]:
    """The customer's own recent words. Our replies are left out: they carry the
    facts we looked up (names, frame numbers, warranty status), and the
    customer's words are what the decision is about."""
    texts: List[str] = []
    for entry in reversed(history):
        if entry.get("role") != "user":
            continue
        text = _text_of(entry).strip()
        if text:
            texts.append("%s: %s" % (entry.get("role", "?"), text))
        if len(texts) >= limit:
            break
    return list(reversed(texts))


def build_state(
    message_text: str,
    history: Sequence[Mapping[str, Any]],
    channel: str,
    bike: Optional[Mapping[str, Any]] = None,
    current_sub_category: Optional[str] = None,
    redact: Sequence[str] = (),
) -> Dict[str, Any]:
    """The minimum Jev needs. No name, phone, frame number or warranty status.

    Jev's own docs: accuracy falls as the state grows with content unrelated to
    the decision. Sending less is also sending less customer data outside AWS.
    """
    terms = sorted({term for term in redact if term and len(term) > 2}, key=len, reverse=True)
    # Whole words only: a name part like "Ram" must not rewrite "program".
    patterns = [
        re.compile(r"(?<!%s)%s(?!%s)" % (_WORD_CHAR, re.escape(term), _WORD_CHAR), re.IGNORECASE)
        for term in terms
    ]

    def clean(text: str) -> str:
        text = _ANY_INDIAN_MOBILE.sub("[phone]", redact_pii(text or ""))
        for pattern in patterns:
            text = pattern.sub("[redacted]", text)
        return text

    return {
        "message": clean(message_text),
        "recent_turns": [clean(text) for text in _recent_texts(history, RECENT_TURNS)],
        "channel": channel,
        "bike_model": (bike or {}).get("product_name") or None,
        "current_sub_category": current_sub_category,
    }


@dataclass(frozen=True)
class PrefetchCall:
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Route:
    path: str  # "standard" | "narrow" | "full"
    reasons: Tuple[str, ...] = ()
    scores: Mapping[str, Any] = field(default_factory=dict)
    standard_response_id: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    error_code: Optional[str] = None
    language: Optional[str] = None
    prefetch: Tuple[PrefetchCall, ...] = ()


def _confident(decision: JevDecision, question_id: str, threshold: float) -> Optional[str]:
    answer = decision.answers.get(question_id)
    if isinstance(answer, ChoiceAnswer) and answer.p >= threshold:
        return answer.choice
    return None


def _noul(decision: JevDecision, question_id: str) -> float:
    answer = decision.answers.get(question_id)
    return answer.p if isinstance(answer, NoulAnswer) else 0.0


def route(
    decision: Optional[JevDecision],
    error: Optional[str],
    thresholds: Thresholds,
    catalogue: RoutingCatalogue,
    current_sub_category: Optional[str] = None,
    bike: Optional[Mapping[str, Any]] = None,
) -> Route:
    """Pick the path. Rules in order, first match wins (spec §3.3).

    The safe direction is always `full`: today's agent at today's cost. Every
    other path has to be earned by a score above its own threshold.
    """
    bike = bike or {}
    current = catalogue.records.get(current_sub_category) if current_sub_category else None

    # 1. No decision.
    if decision is None:
        if error == EMPTY_MESSAGE and current is not None:
            return Route(path="narrow", reasons=("empty_message_continue",), category=current.topic, sub_category=current.id)
        return Route(path="full", reasons=("jev_error:%s" % error if error else "jev_disabled",))

    scores = decision.scores()
    reasons: List[str] = []
    language = _confident(decision, Q_LANGUAGE, thresholds.language)

    # 2. Standard response.
    standard_id = _confident(decision, Q_STANDARD, thresholds.standard_response)
    if standard_id and standard_id != NONE:
        response = catalogue.standard.get(standard_id)
        if response is None:
            reasons.append("standard_unknown:%s" % standard_id)
        elif language is None:
            reasons.append("standard_language_unsure")
        elif response.reply_for(language) is None:
            reasons.append("standard_no_reply_for:%s" % language)
        else:
            return Route(
                path="standard", reasons=tuple(reasons + ["standard:%s" % standard_id]), scores=scores,
                standard_response_id=standard_id, language=language,
            )

    category = _confident(decision, Q_CATEGORY, thresholds.category)
    # What Jev confidently named, before none_of_these is mapped to "no category".
    # Rule 4 needs it: a confident "none of these" mid-flow is a change of
    # subject, not an unsure follow-up.
    named_category = category
    if category == NONE_OF_THESE:
        reasons.append("category_none_of_these")
        category = None
    sub_id = _confident(decision, Q_SUB_CATEGORY, thresholds.sub_category)
    record = catalogue.records.get(sub_id) if sub_id and sub_id != NONE else None
    error_code = _confident(decision, Q_ERROR_CODE, thresholds.error_code)
    if error_code == NONE:
        error_code = None

    # 3. A new record, earned by both scores and allowed for this bike.
    chosen: Optional[KnowledgeRecord] = None
    if category and record is not None:
        if record.topic != category:
            reasons.append("topic_mismatch:%s/%s" % (category, record.id))
        elif not catalogue.applicable(record, bike):
            reasons.append("not_applicable:%s" % record.id)
        else:
            chosen = record
            reasons.append("narrow_new:%s" % record.id)

    # 4. Mid-flow: "yes, it's red now" scores low on everything. Stay on the
    #    record unless Jev confidently named something else: another category
    #    (none_of_these included) or another record, even one refused above for
    #    this bike or topic. Answering those from the current record's steps
    #    would be confidently wrong.
    named_other_record = sub_id is not None and sub_id != NONE and (current is None or sub_id != current.id)
    if (
        chosen is None
        and current is not None
        and named_category in (None, current.topic)
        and not named_other_record
    ):
        chosen = current
        reasons.append("narrow_continue:%s" % current.id)

    # 5. Otherwise the full agent.
    if chosen is None:
        if category is None:
            reasons.append("category_unsure")
        elif record is None:
            reasons.append("sub_category_unsure")
        return Route(path="full", reasons=tuple(reasons), scores=scores, category=category, error_code=error_code, language=language)

    prefetch: List[PrefetchCall] = []
    if _noul(decision, Q_WARRANTY) >= thresholds.tools.get(LOOKUP_WARRANTY_RECORD, 1.01):
        prefetch.append(PrefetchCall(LOOKUP_WARRANTY_RECORD, {}))
    if error_code:
        prefetch.append(PrefetchCall(LOOKUP_ERROR_CODE, {"code": error_code}))
    return Route(
        path="narrow", reasons=tuple(reasons), scores=scores, category=chosen.topic, sub_category=chosen.id,
        error_code=error_code, language=language, prefetch=tuple(prefetch),
    )
