"""Retrieval over the authored knowledge base (build plan §3.5.1, §5 step 4).

This is the one genuinely-RAG piece in the customer journey — everything else is
a tool call against structured data.

Three properties the design depends on:

* **Structured authoring makes chunking free.** Each file is one sub-issue, so it
  is already a retrieval unit with a natural boundary. Nothing is split by token
  count, and no troubleshooting step is ever cut in half — which is the usual way
  a RAG pipeline starts confidently giving people half an instruction.
* **`applies_to` is a hard filter, not a ranking signal.** A step that is wrong
  for a 36V bike must be *unretrievable* for one, not merely ranked lower.
* **Retrieval fails silently.** A wrong passage produces a fluent, plausible,
  wrong answer that reads exactly like a right one. So retrieval is evaluated on
  its own (`tests/test_retrieval_evals.py`), separately from conversations —
  a conversational review would pass all of it.

Swapping the keyword scorer below for pgvector is a change to `_score` and
nothing else; the record shape and the filter stay.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge"

# Devanagari (U+0900–U+097F) is listed explicitly because Python's `\w` matches
# Indic *consonants* but not the combining vowel marks — so a plain `\w+` splits
# "बैटरी" into ["ब", "टर"] and Hindi retrieval silently returns nothing at all.
# Same root cause as the word-boundary bug in triage.py: Mn-category characters
# are not word characters. Add other Indic ranges here as those languages land.
_WORD = re.compile(r"[\w\u0900-\u097f]+", re.UNICODE)

# Words that match everything and rank nothing. "battery" and "motor" are in here
# deliberately: nearly every message contains one, so they separate no records
# within a topic — the topic filter has already done that work.
#
# "no" and "not" are deliberately NOT stopwords. Negation is the whole difference
# between "not charging" and "charging slowly", and dropping it makes those two
# records score identically on the query a customer is most likely to type.
_STOPWORDS = frozenset(
    # "does"/"do"/"did" sit here with is/are/was, which were always listed: they
    # are auxiliaries and carry no meaning on their own. Their absence was an
    # oversight, and it showed — a record whose symptoms read "charger does not
    # fit" was matching a query about the charger LED not coming on, on the
    # strength of "does" plus "charger".
    """a an and are as at be but by for from get getting go has have how i in is it
    do does did
    me my of on or so the this to was what when why will with you your
    bike battery motor cycle ebike e problem issue""".split()
)

REQUIRED_FIELDS = ("id", "title", "topic", "symptoms", "steps")


class KnowledgeError(Exception):
    """A record is malformed. Raised at load time, never at retrieval time.

    Loudly is the point: a record that fails to load is a gap in coverage, and a
    gap that announces itself at startup is far cheaper than one found by a
    customer getting no answer.
    """


@dataclass(frozen=True)
class KnowledgeRecord:
    id: str
    title: str
    topic: str
    symptoms: Sequence[str]
    steps: Sequence[str]
    source: str = ""
    applies_to: Mapping[str, str] = field(default_factory=dict)
    # The negative of applies_to, and it needs to exist separately: a record
    # for every model *except* one cannot be written as a positive match
    # without listing the whole catalogue and revising it at each launch.
    # Without it a Doodle owner is handed both the Doodle flow and the
    # standard one, which disagree about what a red charger LED means.
    excludes: Mapping[str, str] = field(default_factory=dict)
    media: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    escalate_when: str = ""
    superseded_by: Optional[str] = None

    @property
    def text(self) -> str:
        return " ".join(self.steps)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "steps": list(self.steps),
            "source": self.source,
        }
        if self.media:
            # Sent to the customer, not just described. Captions are what the
            # model reads; URLs are what the customer sees.
            payload["media"] = [dict(item) for item in self.media]
        if self.escalate_when:
            payload["escalate_when"] = self.escalate_when
        return payload


@dataclass(frozen=True)
class Passage:
    record: KnowledgeRecord
    score: float

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def title(self) -> str:
        return self.record.title

    @property
    def source(self) -> str:
        return self.record.source

    def to_dict(self) -> Dict[str, Any]:
        return self.record.to_dict()


def _tokens(text: str) -> List[str]:
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def _validate(raw: Mapping[str, Any], where: str) -> None:
    missing = [name for name in REQUIRED_FIELDS if not raw.get(name)]
    if missing:
        raise KnowledgeError("%s is missing required field(s): %s" % (where, ", ".join(missing)))
    if not isinstance(raw.get("steps"), list) or not all(
        isinstance(step, str) for step in raw["steps"]
    ):
        raise KnowledgeError("%s: steps must be a list of strings" % where)
    if not isinstance(raw.get("symptoms"), list):
        raise KnowledgeError("%s: symptoms must be a list" % where)
    for item in raw.get("media") or []:
        if not isinstance(item, Mapping):
            raise KnowledgeError("%s: each media item must be a mapping" % where)
        # `id` is a CDN public id resolved by media.py; `url` is an absolute
        # address for anything hosted elsewhere. One or the other, never neither.
        if not (item.get("id") or item.get("url")):
            raise KnowledgeError(
                "%s: every media item needs an id (a CDN public id) or a url" % where
            )
        if not item.get("caption"):
            raise KnowledgeError(
                "%s: every media item needs a caption — a photo with no caption is "
                "invisible to retrieval, and it is the caption the model reasons about "
                "while the customer sees the picture" % where
            )
        kind = item.get("kind", "image")
        if kind not in ("image", "video"):
            raise KnowledgeError(
                "%s: media kind must be 'image' or 'video', not %r — the channel has to "
                "know whether to render a picture or a player" % (where, kind)
            )


def load_records(directory: Optional[Path] = None) -> List[KnowledgeRecord]:
    """Read and validate every authored record.

    Raises rather than skipping a bad file. A silently dropped record is a topic
    the bot has quietly stopped knowing about, with nothing anywhere to say so.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment, not logic
        raise KnowledgeError(
            "PyYAML is required to load the knowledge base (pip install -r requirements.txt)"
        ) from exc

    root = Path(directory) if directory else KNOWLEDGE_DIR
    if not root.exists():
        raise KnowledgeError("knowledge directory not found: %s" % root)

    records: List[KnowledgeRecord] = []
    seen: Dict[str, Path] = {}
    for path in sorted(root.rglob("*.yaml")):
        # An underscore-prefixed directory is authored content that is not a
        # record — the guide-media catalogue lives in one. Skipping by an
        # explicit namespace is not the same as skipping a malformed record,
        # which still raises: that would be a topic the bot has quietly stopped
        # knowing about, with nothing anywhere to say so.
        if any(part.startswith("_") for part in path.relative_to(root).parts):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _validate(raw, str(path))

        record_id = raw["id"]
        if record_id in seen:
            raise KnowledgeError(
                "duplicate record id %r in %s and %s — ids are referenced by evals, so a "
                "collision silently changes what a test asserts"
                % (record_id, seen[record_id], path)
            )
        seen[record_id] = path

        records.append(
            KnowledgeRecord(
                id=record_id,
                title=raw["title"],
                topic=raw["topic"],
                symptoms=tuple(raw["symptoms"]),
                steps=tuple(raw["steps"]),
                source=raw.get("source", ""),
                applies_to=dict(raw.get("applies_to") or {}),
                excludes=dict(raw.get("excludes") or {}),
                media=tuple(dict(item) for item in (raw.get("media") or [])),
                escalate_when=raw.get("escalate_when", ""),
                superseded_by=raw.get("superseded_by"),
            )
        )
    return records


class KnowledgeBase:
    """Keyword-scored stand-in for the vector index.

    Deliberately simple, and honest about it: it exists so the conversational flow
    can be built and evaluated against real authored content before anything is
    embedded. Replace `_score` with a pgvector similarity query over the same
    record shape and nothing upstream changes.
    """

    def __init__(
        self,
        records: Optional[Sequence[KnowledgeRecord]] = None,
        directory: Optional[Path] = None,
    ) -> None:
        loaded = list(records) if records is not None else load_records(directory)
        # A superseded record is deleted from the index, not down-ranked. One that
        # still retrieves is worse than a missing one, because it answers with the
        # same confidence as a current one.
        self.records = [record for record in loaded if not record.superseded_by]
        self.retired = [record for record in loaded if record.superseded_by]
        self._idf = self._build_idf(self.records)

    @staticmethod
    def _build_idf(records: Sequence[KnowledgeRecord]) -> Dict[str, float]:
        """How much one matched word is worth, by how rare it is in the corpus.

        Without this every matched word counts the same, and a word that says
        almost nothing outranks one that says everything. Measured on the real
        corpus: "nothing" appears in 7 of 13 records and "happens" in 4, while
        "laga" and "diya" appear in exactly one — the Hinglish for *I plugged the
        charger in*, which is the whole meaning of the question. Flat weighting
        let the two vague words outscore the two precise ones, and the records
        tied at identical scores, decided by whichever id sorted first.

        That tie is not a curiosity: several golden queries were passing on it,
        so any honest edit to an unrelated record could silently flip them. This
        is the same seam pgvector replaces later (see the module docstring);
        until then, frequency is the signal available.
        """
        total = len(records) or 1
        frequency: Dict[str, int] = {}
        for record in records:
            seen = set(_tokens(record.title)) | set(_tokens(record.text))
            for symptom in record.symptoms:
                seen.update(_tokens(symptom.replace("_", " ")))
            for token in seen:
                frequency[token] = frequency.get(token, 0) + 1
        # Smoothed, and floored at 1.0 so a word in every record still counts for
        # something — being unhelpful is not the same as being evidence against.
        return {
            token: 1.0 + math.log(total / count)
            for token, count in frequency.items()
        }

    def _weight(self, tokens: set) -> float:
        # An unknown word (in the query, in no record) gets the weight of the
        # rarest thing we have seen rather than zero.
        return sum(self._idf.get(token, 1.0 + math.log(len(self.records) or 1)) for token in tokens)

    def _score(self, record: KnowledgeRecord, query_tokens: set) -> float:
        """Score a record, or 0 when the only overlap is incidental prose.

        **Body text alone is never sufficient evidence.** Symptoms and titles are
        what an author curated *for retrieval*; the steps are prose, and prose
        contains ordinary words. Without this rule "where is my order" retrieves
        a battery-storage passage, because that passage happens to contain the
        word "where" — a fluent, confident, completely irrelevant answer, which
        is precisely how retrieval fails silently.
        """
        symptom_tokens: set = set()
        for symptom in record.symptoms:
            symptom_tokens.update(_tokens(symptom.replace("_", " ")))

        title_tokens = set(_tokens(record.title))
        body_tokens = set(_tokens(record.text))

        symptom_hits = query_tokens & symptom_tokens
        title_hits = query_tokens & title_tokens
        if not symptom_hits and not title_hits:
            return 0.0

        # Each matched word now counts for what it is worth rather than one
        # apiece (see `_build_idf`), and the field weights are spread to match:
        # symptoms are curated for retrieval, titles nearly so, prose is
        # incidental. Body was 1.0 against 3.0/2.0 when every word counted the
        # same. Weighting words exposed that as too generous — a record can
        # repeat an ordinary word across its title and a dozen steps, and under
        # IDF that out-scored a record whose curated symptom list matched the
        # query outright. Prose can now break a tie and not much more, which is
        # what the docstring above always claimed it was for.
        return (
            4.0 * self._weight(symptom_hits)
            + 2.0 * self._weight(title_hits)
            + 0.25 * self._weight(query_tokens & body_tokens)
        )

    def _applicable(self, record: KnowledgeRecord, bike: Mapping[str, Any]) -> bool:
        """Hard filter. Wrong-for-this-bike must be unretrievable, not ranked low."""
        for key, expected in record.applies_to.items():
            actual = bike.get(key)
            if actual is None:
                # We cannot confirm the record applies. Excluding it costs a
                # possible answer; including it risks telling someone to check a
                # part their bike does not have.
                return False
            if str(expected).lower() not in str(actual).lower():
                return False
        for key, unwanted in record.excludes.items():
            actual = bike.get(key)
            if actual is None:
                # Unknown bike: the exclusion does not fire. This is the opposite
                # of applies_to on purpose. A positive filter names a part the
                # record depends on, so it must be confirmed present. An
                # exclusion carves one model out of the default flow — applying
                # it unconfirmed would leave an anonymous customer with no
                # record at all, which is a worse answer than the general one.
                continue
            if str(unwanted).lower() in str(actual).lower():
                return False
        return True

    def search(
        self,
        query: str,
        top_k: int = 2,
        topic: Optional[str] = None,
        bike: Optional[Mapping[str, Any]] = None,
    ) -> List[Passage]:
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []

        bike = bike or {}
        scored: List[Passage] = []
        for record in self.records:
            if topic and record.topic != topic:
                continue
            if not self._applicable(record, bike):
                continue
            score = self._score(record, query_tokens)
            if score > 0:
                scored.append(Passage(record=record, score=score))

        # Ties break on id so results are stable. An eval that passes and fails
        # alternately teaches everyone to ignore it.
        scored.sort(key=lambda passage: (-passage.score, passage.id))
        return scored[:top_k]


# The battery agent and its tool were written against this name before the corpus
# covered more than one topic.
BatteryKnowledgeBase = KnowledgeBase
