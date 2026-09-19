#!/usr/bin/env python3
"""Promote a tuned prompt version into the one the service actually runs.

    python3 scripts/promote_prompt.py battery_support            # latest
    python3 scripts/promote_prompt.py battery_support --version 28
    python3 scripts/promote_prompt.py battery_support --dry-run

`publish_prompt.py` appends a version to the playground's tuning history. This
takes one of those versions and writes it to `prompts/<agent>.md`, which is what
the agent module loads at import. Two commands rather than one, on purpose:
publishing is a sandbox action and should stay cheap, while changing the prompt
the live bot runs should be a file in a pull request that somebody reads.

The gap this closes was invisible. v28 was 22,162 characters of tuned rules and
the service was running an untouched 2,641-character literal, because the step
between them was a human copying text and nobody had done it since v24.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emotorad_ai.playground import AGENT_MODULES, _load_prompt_versions  # noqa: E402
from emotorad_ai.prompts import prompt_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("agent", choices=sorted(AGENT_MODULES))
    parser.add_argument("--version", type=int, help="which published version (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    args = parser.parse_args()

    versions = _load_prompt_versions(args.agent)
    if not versions:
        print("no published versions for %s — publish one first" % args.agent, file=sys.stderr)
        return 1

    if args.version is None:
        chosen = versions[-1]
    else:
        matches = [v for v in versions if v["version"] == args.version]
        if not matches:
            print(
                "v%d not found. Published: %s"
                % (args.version, ", ".join("v%d" % v["version"] for v in versions[-8:])),
                file=sys.stderr,
            )
            return 1
        chosen = matches[0]

    new_text = chosen["text"]
    if not new_text.strip():
        print("refusing to promote an empty prompt", file=sys.stderr)
        return 1

    target = prompt_path(args.agent)
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        current = ""

    if current == new_text:
        print("prompts/%s.md is already v%d — nothing to promote" % (args.agent, chosen["version"]))
        return 0

    diff = list(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="prompts/%s.md (live)" % args.agent,
            tofile="v%d (published %s)" % (chosen["version"], chosen.get("saved_at", "?")),
            n=2,
        )
    )
    sys.stdout.writelines(diff or ["(no textual difference)\n"])
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    print(
        "\nlive -> v%d: +%d/-%d lines, %d -> %d chars"
        % (chosen["version"], added, removed, len(current), len(new_text))
    )

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    print(
        "wrote prompts/%s.md from v%d. Commit it: this is the prompt the service runs, "
        "and the diff above is what a reviewer needs to see." % (args.agent, chosen["version"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
