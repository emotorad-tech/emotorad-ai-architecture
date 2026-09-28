"""Standard responses: reviewed replies Jev can pick without any LLM call.

Authored as files under knowledge/_standard/, like the knowledge base, so Git
is the audit trail and a PR is the approval. Only `status: approved` records
ever reach Jev; drafts load (so calibration can measure them) and are
otherwise inert.

A standard reply is identical for every customer, so it must never be
personal and never assert anything the turn's tools did not establish. Those
two rules are checked here, at load time, with the same post-checks the
runtime applies to model replies: a canned "it's covered" is the Air Canada
failure with no model to blame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .guardrails import check_coverage_claim, check_evidence
from .knowledge import KNOWLEDGE_DIR

STANDARD_DIR = KNOWLEDGE_DIR / "_standard"
LANGUAGES = ("english", "hindi", "hinglish", "marathi", "tamil")
STATUSES = ("draft", "approved")
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


class StandardResponseError(Exception):
    """A standard response is malformed or unsafe. Raised at load, never at reply time."""


@dataclass(frozen=True)
class StandardResponse:
    id: str
    status: str
    approved_by: str
    criteria: str
    replies: Mapping[str, str]
    examples: Sequence[str] = field(default_factory=tuple)
    counter_examples: Sequence[str] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def reply_for(self, language: Optional[str]) -> Optional[str]:
        return self.replies.get(language or "")


def _validate(raw: Mapping[str, Any], where: str) -> None:
    for name in ("id", "status", "criteria", "replies"):
        if not raw.get(name):
            raise StandardResponseError("%s is missing %s" % (where, name))
    if raw["status"] not in STATUSES:
        raise StandardResponseError("%s: status must be one of %s" % (where, ", ".join(STATUSES)))
    if raw["status"] == "approved" and not str(raw.get("approved_by") or "").strip():
        raise StandardResponseError("%s: an approved response needs approved_by" % where)
    replies = raw["replies"]
    if not isinstance(replies, Mapping):
        raise StandardResponseError("%s: replies must map a language to text" % where)
    for language, text in replies.items():
        if language not in LANGUAGES:
            raise StandardResponseError("%s: unknown language %r" % (where, language))
        if not isinstance(text, str) or not text.strip():
            raise StandardResponseError("%s: the %s reply is empty" % (where, language))
        if _PLACEHOLDER.search(text):
            raise StandardResponseError(
                "%s: the %s reply has a placeholder; standard replies are never personal" % (where, language)
            )
        if check_coverage_claim(text, []).blocked:
            raise StandardResponseError("%s: the %s reply makes a coverage claim" % (where, language))
        if check_evidence(text, False).blocked:
            raise StandardResponseError("%s: the %s reply concludes a fault" % (where, language))


def load_standard_responses(directory: Optional[Path] = None) -> List[StandardResponse]:
    import yaml

    root = Path(directory) if directory else STANDARD_DIR
    if not root.exists():
        return []
    responses: List[StandardResponse] = []
    seen: Dict[str, Path] = {}
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _validate(raw, str(path))
        if raw["id"] in seen:
            raise StandardResponseError(
                "duplicate standard response id %r in %s and %s" % (raw["id"], seen[raw["id"]], path)
            )
        seen[raw["id"]] = path
        responses.append(
            StandardResponse(
                id=raw["id"],
                status=raw["status"],
                approved_by=str(raw.get("approved_by") or ""),
                criteria=str(raw["criteria"]).strip(),
                replies={language: text.strip() for language, text in raw["replies"].items()},
                examples=tuple(raw.get("examples") or ()),
                counter_examples=tuple(raw.get("counter_examples") or ()),
            )
        )
    return responses
