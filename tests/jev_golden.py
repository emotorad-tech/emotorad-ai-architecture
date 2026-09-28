"""The labelled set Jev is calibrated against.

Three sources, none copied: the retrieval goldens (already labelled with topic
and record), the extras in tests/data/jev_golden.yaml, and the examples and
counter-examples authored on each standard response.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from emotorad_ai.decisions import NONE, NONE_OF_THESE
from emotorad_ai.metrics import detect_language
from emotorad_ai.standard_responses import load_standard_responses

EXTRAS = Path(__file__).parent / "data" / "jev_golden.yaml"
_LANGUAGE_NAMES = {"en": "english", "hinglish": "hinglish", "hi-deva": "hindi"}


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


def _language(text: str) -> str:
    return _LANGUAGE_NAMES.get(detect_language(text), "english")


def load_golden() -> List[GoldenCase]:
    from tests.test_retrieval_evals import GOLDEN

    cases = [
        GoldenCase(text=query, language=_language(query), category=topic, sub_category=record_id,
                   standard_response=NONE, needs_warranty_lookup=False, bike=dict(bike))
        for query, record_id, topic, bike in GOLDEN
    ]
    raw = yaml.safe_load(EXTRAS.read_text(encoding="utf-8")) or {}
    for item in raw.get("cases", []):
        cases.append(GoldenCase(
            text=item["text"], language=item["language"], category=item.get("category"),
            sub_category=item.get("sub_category", NONE), standard_response=item.get("standard_response", NONE),
            needs_warranty_lookup=bool(item.get("needs_warranty_lookup", False)), error_code=item.get("error_code", NONE),
        ))
    for response in load_standard_responses():
        for example in response.examples:
            cases.append(GoldenCase(text=example, language=_language(example), category=NONE_OF_THESE, standard_response=response.id))
        for example in response.counter_examples:
            cases.append(GoldenCase(text=example, language=_language(example), standard_response=NONE))
    return cases
