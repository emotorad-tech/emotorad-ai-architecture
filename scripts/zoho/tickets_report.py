"""Waiting, stuck and held chatbot tickets, for the support lead (spec 2026-10-05, section 10).

    python scripts/zoho/tickets_report.py
    python scripts/zoho/tickets_report.py --mode live

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

Read only, against the deployment's store: EMOTORAD_STORE=mongodb and
EMOTORAD_MONGO_URI (never printed), read as the service reads them. For each
record still waiting, stuck or held, it prints our reference, the Zoho ticket
number, the state, the mode and the last four digits of the number to call
back. Those customers were told someone would be in touch, so run it before
stopping or rolling back the Zoho integration, and give the list to the
support lead.

The mode is the deployment's: live when EMOTORAD_ZOHO_LIVE is exactly "yes",
test otherwise. Records of the other mode are listed as held.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.conversation import StoreUnavailable  # noqa: E402
from emotorad_ai.wiring import build_stores  # noqa: E402
from emotorad_ai.zoho.settings import LIVE  # noqa: E402

_ROW = "%-12s %-10s %-8s %-5s %s"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=("test", "live"), default="live" if os.environ.get(LIVE) == "yes" else "test")
    return p


def lines(rows: List[Dict[str, Any]]) -> List[str]:
    """The listing as text. Only ever the last four digits of a number."""
    out = [_ROW % ("reference", "zoho", "state", "mode", "number")]
    for row in rows:
        last_four = row.get("last_four")
        out.append(_ROW % (row.get("reference"), row.get("zoho_number") or "-", row.get("state"), row.get("mode"),
                           "..." + str(last_four)[-4:] if last_four else "-"))
    out.append("%d record(s)" % len(rows))
    return out


def main(argv: Optional[List[str]] = None, out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    settings = load_settings()
    if settings.store != "mongodb":
        out("EMOTORAD_STORE is %r, not mongodb: there is no deployment store to read." % settings.store)
        return 1
    try:
        rows = build_stores(settings).tickets.listing(args.mode)
    except StoreUnavailable as exc:
        out("The store could not be read (%s). Check EMOTORAD_MONGO_URI and the Atlas access list."
            % type(exc).__name__)
        return 1
    for line in lines(rows):
        out(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
