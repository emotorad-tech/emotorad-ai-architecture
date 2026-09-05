"""Draft bots: where the playground writes, and how they get to a PR.

Drafts live outside the tree (`EMOTORAD_AI_BOT_DRAFTS`, default
`.playground/drafts`, gitignored and bind-mounted on staging). "Export for
review" copies a draft and its knowledge into `.playground/export/` laid out
exactly as `bots/` and `knowledge/` are, so an engineer moves the files into
place and opens a PR. Nothing here touches a tracked file — same convention as
the prompt diff the playground already produced.

Streamlit-free on purpose, so all of this is unit-testable.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from .bots import BOTS_DIR, DRAFT, BotCatalogue, BotSpecError, spec_from_dict
from .knowledge import ID_PATTERN, KnowledgeError, load_records

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DRAFTS_DIR = Path(os.environ.get("EMOTORAD_AI_BOT_DRAFTS", str(REPO_ROOT / ".playground" / "drafts")))
EXPORT_DIR = REPO_ROOT / ".playground" / "export"

_CUSTOMER_TEMPLATE = """\
You are the {topic} support assistant for EMotorad, an Indian e-cycle company. You are \
talking to a signed-in customer about their bike.

Your job: understand the symptom, walk the customer through safe checks, and either \
resolve the issue or raise a support ticket. Be warm, plain-spoken and brief.

How to work:
- Ask at most one or two clarifying questions, and only when the answer changes what you \
would suggest.
- Never ask the customer for their bike model, purchase date or warranty status. Those are \
given to you below.
- Every factual claim about how the bike behaves must come from search_knowledge, called \
with topic "{topic}". If it returns nothing useful, say you are not certain and raise a \
ticket rather than guessing.
- Suggest at most two or three checks at a time, and only ones that are safe for a customer \
to do themselves.
- Ownership and warranty coverage come only from lookup_warranty_record. Never estimate or \
infer either.
- If you cannot resolve it, call create_support_ticket with a clean summary of the symptom, \
what was already tried, and the result. Tell the customer the ticket number. Do not promise \
a specific outcome, refund, replacement or repair cost.

Style: reply in short plain sentences suited to a chat widget. No headings, no bullet \
symbols, no markdown, no emoji. Indian English. If you do not know something, say so.
"""

_DEALER_TEMPLATE = """\
You are EMotorad's dealer assistant for {topic}. You are talking to a verified dealer on \
their WhatsApp line.

How to work:
- Be brief and specific. Dealers are working.
- Never state a price, discount, total, credit figure or stock count that did not come from \
a tool. You have no authority to set or negotiate any of them.
- If the dealer asks for an exception, say that is not something you can approve and offer \
to raise it with their account manager.
- If you cannot help, call create_support_ticket with a clean summary.

Style: short plain sentences suited to WhatsApp. Indian English. No headings, no markdown, \
no emoji.
"""


def prompt_template(persona: str, topic: str = "<topic>") -> str:
    template = _DEALER_TEMPLATE if persona == "dealer" else _CUSTOMER_TEMPLATE
    return template.replace("{topic}", topic)


class _Dumper(yaml.SafeDumper):
    """Block style for multi-line strings, so a prompt diff reads as prose."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _dump(data: Mapping[str, Any]) -> str:
    return yaml.dump(dict(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True)


def load_catalogue(drafts_dir: Path) -> BotCatalogue:
    return BotCatalogue.load(published_dir=BOTS_DIR, drafts_dir=drafts_dir)


def save_draft(raw: Mapping[str, Any], drafts_dir: Path) -> Path:
    """Validate against everything else that exists, then write. Nothing is
    written on failure, so a bad save cannot leave a draft that will not load."""
    spec = spec_from_dict(raw, DRAFT)
    others = [s for s in load_catalogue(drafts_dir).specs if s.name != spec.name]
    BotCatalogue(others + [spec])  # raises BotSpecError on any conflict

    directory = Path(drafts_dir) / "bots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("%s.yaml" % spec.name)
    path.write_text(_dump(spec.to_dict()), encoding="utf-8")
    return path


def delete_draft(name: str, drafts_dir: Path) -> None:
    path = Path(drafts_dir) / "bots" / ("%s.yaml" % name)
    if path.exists():
        path.unlink()


def save_draft_knowledge(topic: str, records: Sequence[Mapping[str, Any]], drafts_dir: Path) -> List[Path]:
    """Write records in the `knowledge/README.md` format, then load them back
    through the real loader so a malformed record fails here, not at retrieval."""
    directory = Path(drafts_dir) / "knowledge" / topic
    directory.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    # Pre-call bytes for paths this call overwrites (None means the path was new),
    # so a mid-call failure can restore the directory exactly, not just delete what we wrote.
    snapshots: Dict[Path, Optional[bytes]] = {}
    try:
        for record in records:
            payload: Dict[str, Any] = {
                "id": record.get("id"),
                "title": record.get("title"),
                "topic": topic,
                "symptoms": list(record.get("symptoms") or []),
                "applies_to": dict(record.get("applies_to") or {}),
                "source": record.get("source", "Authored in the playground"),
                "media": list(record.get("media") or []),
                "steps": list(record.get("steps") or []),
                "escalate_when": record.get("escalate_when", ""),
            }
            if not payload["id"]:
                raise KnowledgeError("a knowledge record needs an id")
            if not isinstance(payload["id"], str) or not ID_PATTERN.match(payload["id"]):
                raise KnowledgeError(
                    "id %r must match %s (lowercase letters, digits, '-' and '_', starting "
                    "with a letter or digit)" % (payload["id"], ID_PATTERN.pattern)
                )
            path = directory / ("%s.yaml" % payload["id"])
            if not path.resolve().is_relative_to(directory.resolve()):
                raise KnowledgeError("id %r escapes the knowledge directory" % payload["id"])
            if path not in snapshots:
                snapshots[path] = path.read_bytes() if path.exists() else None
            path.write_text(_dump(payload), encoding="utf-8")
            written.append(path)
        load_records(Path(drafts_dir) / "knowledge")
    except KnowledgeError:
        for path, previous in snapshots.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise
    return written


def export_for_review(name: str, drafts_dir: Path, export_dir: Path) -> List[Path]:
    """Copy a draft and its knowledge into `export_dir` laid out like the repo."""
    spec = load_catalogue(drafts_dir).get(name)
    if spec.source != DRAFT or spec.path is None:
        raise BotSpecError("%s is not a draft" % name)

    copied: List[Path] = []
    target = Path(export_dir) / "bots" / spec.path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec.path, target)
    copied.append(target)

    if spec.topic:
        source_dir = Path(drafts_dir) / "knowledge" / spec.topic
        if source_dir.exists():
            for path in sorted(source_dir.glob("*.yaml")):
                destination = Path(export_dir) / "knowledge" / spec.topic / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                copied.append(destination)
    return copied
