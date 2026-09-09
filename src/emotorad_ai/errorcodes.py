"""Display error codes, looked up by code and bike model.

An exact lookup rather than retrieval, on purpose. Semantic search over a code
table would happily return E-08 for a customer who said E-07, and a confidently
wrong diagnosis is the worst thing this bot can produce. A code that is not in
the table comes back as *not in the table*, never as its nearest neighbour.

The model matters as much as the code. E-30 carries a real diagnosis on a
T-REX + V3 and is not supposed to be possible on any other TREX+ — which is a
third answer again: a code that cannot occur on that bike suggests the customer
is misreading the display, and that wants a person rather than troubleshooting a
fault that does not exist.

Content lives in ``knowledge/_errors/codes.yaml``. Adding a code, or a model, is
an edit there — no code change. Underscore-prefixed so the knowledge loader does
not mistake it for a retrieval record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

CODES_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "_errors" / "codes.yaml"

# Stands for "every code on this model", used where a whole model routes to a
# person regardless of what the display says.
ANY_CODE = "*"


class ErrorCodeError(Exception):
    """A malformed table. Raised at load, never at lookup time."""


@dataclass(frozen=True)
class ErrorCodeEntry:
    code: str
    group_id: str
    group_label: str
    disposition: str  # "diagnose" | "human_support"
    customer_message: str
    technician_chain: str = ""
    verification: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "code": self.code,
            "model_group": self.group_label,
            "disposition": self.disposition,
            "customer_message": self.customer_message,
        }
        if self.technician_chain:
            payload["technician_chain"] = self.technician_chain
        if self.verification:
            payload["verification"] = self.verification
        return payload


@dataclass
class ErrorCodeTable:
    groups: List[Dict[str, Any]] = field(default_factory=list)
    entries: List[ErrorCodeEntry] = field(default_factory=list)

    def group_for(self, product_name: Optional[str]) -> Optional[Dict[str, Any]]:
        """Which model group a bike belongs to, or None.

        Matched on substrings of the OMS product name, which is not tidy —
        "X2 Furious Red V2 - EM02BV01C23", "Doodle V4 Indicator Edition". Groups
        are tried in file order so the most specific wins: a T-REX + V3 must not
        fall into the plain TREX+ group and be told E-30 is impossible.
        """
        name = (product_name or "").lower()
        if not name:
            return None
        for group in self.groups:
            if any(pattern in name for pattern in group["match"]):
                return group
        return None

    def normalise(self, code: str) -> str:
        """`e7`, `E 07`, `e-07` all mean E-07 to the person reading the display."""
        raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
        if raw.startswith("E") and raw[1:].isdigit():
            return "E%02d" % int(raw[1:])
        return raw

    def lookup(self, code: str, product_name: Optional[str]) -> Dict[str, Any]:
        """One of four answers, each of which the agent handles differently."""
        group = self.group_for(product_name)
        if group is None:
            return {
                "status": "unknown_model",
                "detail": "No error-code table is published for %r." % (product_name or "unknown"),
            }

        wanted = self.normalise(code)
        if not wanted:
            return {"status": "unreadable_code", "detail": "That does not look like an error code."}

        for entry in self.entries:
            if entry.group_id != group["id"]:
                continue
            if entry.code == ANY_CODE or entry.code == wanted:
                return {"status": "found", "entry": entry.to_dict()}

        # The code is real somewhere, just not on this bike. Worth saying so:
        # it usually means the display was misread.
        on_other_models = sorted({e.group_label for e in self.entries if e.code == wanted})
        if on_other_models:
            return {
                "status": "not_possible_on_this_model",
                "detail": "%s is documented on %s, not on %s."
                % (wanted, "; ".join(on_other_models), group["label"]),
            }
        return {"status": "unknown_code", "detail": "%s is not in the published table." % wanted}


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(v) for v in value]
    raise ErrorCodeError("expected a string or a list, got %r" % (value,))


def load_table(path: Optional[Path] = None) -> ErrorCodeTable:
    """Read and validate the table. Raises rather than skipping a bad row.

    A silently dropped row is a code the bot has quietly stopped recognising,
    with nothing anywhere to say so — the same reason the knowledge loader
    refuses to skip a malformed record.
    """
    import yaml

    source = path or CODES_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except OSError:
        return ErrorCodeTable()
    if not isinstance(raw, dict):
        raise ErrorCodeError("%s: expected a mapping at the top level" % source)

    groups: List[Dict[str, Any]] = []
    for group in raw.get("model_groups") or []:
        if not all(group.get(k) for k in ("id", "label", "match")):
            raise ErrorCodeError("%s: every model group needs an id, a label and match patterns" % source)
        groups.append(
            {"id": str(group["id"]), "label": str(group["label"]),
             "match": [str(m).lower() for m in _as_list(group["match"])]}
        )
    known = {g["id"] for g in groups}

    entries: List[ErrorCodeEntry] = []
    table = ErrorCodeTable(groups=groups)
    for row in raw.get("codes") or []:
        where = "%s: %s" % (source.name, row.get("code"))
        disposition = row.get("disposition")
        if disposition not in ("diagnose", "human_support"):
            raise ErrorCodeError("%s: disposition must be 'diagnose' or 'human_support'" % where)
        if not row.get("customer_message"):
            raise ErrorCodeError(
                "%s: needs a customer_message — without one the bot has to improvise "
                "customer-facing words out of engineer shorthand" % where
            )
        if disposition == "diagnose" and not row.get("technician_chain"):
            raise ErrorCodeError("%s: a diagnosed code needs a technician_chain" % where)
        for group_id in _as_list(row.get("groups") or []):
            if group_id not in known:
                raise ErrorCodeError("%s: unknown model group %r" % (where, group_id))
            label = next(g["label"] for g in groups if g["id"] == group_id)
            for code in _as_list(row["code"]):
                entries.append(
                    ErrorCodeEntry(
                        code=ANY_CODE if code == ANY_CODE else table.normalise(code),
                        group_id=group_id,
                        group_label=label,
                        disposition=disposition,
                        customer_message=" ".join(str(row["customer_message"]).split()),
                        technician_chain=" ".join(str(row.get("technician_chain") or "").split()),
                        verification=" ".join(str(row.get("verification") or "").split()),
                    )
                )
    table.entries = entries
    return table
