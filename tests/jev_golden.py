"""The labelled set Jev is calibrated against.

Three sources, none copied: the retrieval goldens (already labelled with topic
and record), the extras in tests/data/jev_golden.yaml, and the examples and
counter-examples authored on each standard response.

A case with `current_sub_category` is a follow-up: a reply in the middle of a
conversation already on that record, scored with the customer's earlier
messages (`history`) exactly as the runtime would send them to Jev.
"""

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from emotorad_ai.decisions import NONE, NONE_OF_THESE, build_state
from emotorad_ai.metrics import detect_language
from emotorad_ai.standard_responses import load_standard_responses

EXTRAS = Path(__file__).parent / "data" / "jev_golden.yaml"
_LANGUAGE_NAMES = {"en": "english", "hinglish": "hinglish", "hi-deva": "hindi"}

# Hindi words written in Latin letters, for labelling only. metrics.detect_language
# is deliberately crude (it buckets reports); a calibration label has to be right,
# because a wrong one counts Jev wrong for a correct answer. Whole words, so
# English text is not caught by accident.
_HINGLISH_WORDS = frozenset(
    """
    aap aapka aaya abhi aur ab bahut band batao batata batayiye bhi chahiye chal chalti
    chalu dekhta dhanyavaad diya ek gaya gayi hai hain haan ho hoga hona hoon hota hua
    insaan jal jaldi ka kab kaam kar karke karna karo karta ke kharab ki kitna ko koi
    kya laga lagega mein mera meri mujhe nahi nahin par paisa pe raha rahi rahe ruko se
    shukriya theek thik toh wo ya
    """.split()
)
_LATIN_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class GoldenCase:
    text: str
    language: str
    category: Optional[str] = None  # None: not scored on this question
    sub_category: Optional[str] = None
    standard_response: Optional[str] = None
    needs_warranty_lookup: Optional[bool] = None
    error_code: Optional[str] = None
    bike: Dict[str, Any] = field(default_factory=dict)
    # Follow-ups only: the record the conversation is on, and the customer's
    # earlier messages, oldest first.
    current_sub_category: Optional[str] = None
    history: Tuple[str, ...] = ()


def state_for(case: GoldenCase, channel: str = "whatsapp") -> Dict[str, Any]:
    """The state Jev is sent for this case, built by the runtime's own function."""
    history = [{"role": "user", "content": text} for text in case.history]
    return build_state(
        case.text, history, channel, bike=case.bike or None, current_sub_category=case.current_sub_category,
    )


def _language(text: str) -> str:
    detected = _LANGUAGE_NAMES.get(detect_language(text), "english")
    if detected == "english" and set(_LATIN_WORD.findall(text.lower())) & _HINGLISH_WORDS:
        return "hinglish"
    return detected


def load_golden() -> List[GoldenCase]:
    from tests.test_retrieval_evals import GOLDEN

    raw = yaml.safe_load(EXTRAS.read_text(encoding="utf-8")) or {}
    # Corrections to the imported retrieval phrases, keyed by their exact text:
    # the crude language detector misses Hinglish without a marker word, and the
    # retrieval set never labelled warranty questions as such.
    overrides = raw.get("imported") or {}

    cases = []
    for query, record_id, topic, bike in GOLDEN:
        case = GoldenCase(text=query, language=_language(query), category=topic, sub_category=record_id,
                          standard_response=NONE, needs_warranty_lookup=False, error_code=NONE, bike=dict(bike))
        cases.append(replace(case, **overrides.get(query, {})))

    for item in raw.get("cases", []):
        # An explicit null means "not scored on this question" (the label is
        # genuinely ambiguous); an omitted field takes the default.
        cases.append(GoldenCase(
            text=item["text"], language=item["language"], category=item.get("category"),
            sub_category=item.get("sub_category", NONE), standard_response=item.get("standard_response", NONE),
            needs_warranty_lookup=bool(item.get("needs_warranty_lookup", False)), error_code=item.get("error_code", NONE),
            bike=dict(item.get("bike") or {}),
            current_sub_category=item.get("current_sub_category"),
            history=tuple(item.get("history") or ()),
        ))
    for response in load_standard_responses():
        for example in response.examples:
            cases.append(GoldenCase(text=example, language=_language(example), category=NONE_OF_THESE, standard_response=response.id))
        for example in response.counter_examples:
            cases.append(GoldenCase(text=example, language=_language(example), standard_response=NONE))
    return cases
