#!/usr/bin/env python3
"""Publish a prompt version into the playground's store, from a file.

    python scripts/publish_prompt.py battery_support draft.txt
    python scripts/publish_prompt.py battery_support draft.txt --dry-run

Writes a new version the same way the page's Save button does: appended, never
overwriting, so every earlier version stays loadable and a bad publish is undone
by loading the one before it.

This exists so that editing the prompt from outside the browser is one repeatable
command rather than a hand-written script each time — and so the diff against the
current version is printed before anything is written, because a prompt edit is
the highest-leverage change in this repo and the easiest to make carelessly.

The open editor does not update on its own; Streamlit's session state holds
whatever the tester has typed. The page notices a newer version and offers to
load it, which is deliberate: silently replacing someone's unsaved draft with a
version published underneath them would be worse than making them click.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emotorad_ai.playground import (  # noqa: E402
    AGENT_MODULES,
    _latest_prompt_version,
    _load_prompt_versions,
    _save_prompt_version,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("agent", choices=sorted(AGENT_MODULES), help="which agent's prompt")
    parser.add_argument("file", type=Path, help="file holding the new prompt text")
    parser.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    args = parser.parse_args()

    try:
        new_text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print("cannot read %s: %s" % (args.file, exc), file=sys.stderr)
        return 1
    if not new_text.strip():
        print("refusing to publish an empty prompt", file=sys.stderr)
        return 1

    current = _latest_prompt_version(args.agent)
    old_text = current["text"] if current else ""
    label = "v%d" % current["version"] if current else "(no saved version)"

    if current and old_text == new_text:
        print("identical to %s — nothing to publish" % label)
        return 0

    diff = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=label,
            tofile="(new)",
            n=2,
        )
    )
    sys.stdout.writelines(diff or ["(no textual difference)\n"])
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    print("\n%s -> new: +%d/-%d lines, %d -> %d chars" % (label, added, removed, len(old_text), len(new_text)))

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    record = _save_prompt_version(args.agent, new_text)
    print(
        "published v%d (%d versions stored). Reload the playground, or use the "
        "'load v%d' prompt on the page." % (record["version"], len(_load_prompt_versions(args.agent)), record["version"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
