"""Where conversations came from: runs counted by country, state or city.

    python scripts/origin_report.py --from 2026-10-01 --to 2026-10-31 --by region
    python scripts/origin_report.py --from 2026-10-01 --to 2026-10-31 --by country --channel amiigo_app

Read-only. Reads EMOTORAD_MONGO_URI (never printed). Run by a person.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.origin import CREDIT  # noqa: E402

LEVELS = {"country": ("country",), "region": ("country", "region"), "city": ("country", "region", "city")}


def counts(collection: Any, by: str, start: str, end: str, channel: Optional[str] = None) -> List[Tuple[str, int]]:
    """Runs per place, most first. `end` is inclusive (a date)."""
    stop = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    match = {"started_at": {"$gte": start, "$lt": stop}}
    if channel:
        match["channel"] = channel
    fields = LEVELS[by]
    rows = collection.aggregate([
        {"$match": match},
        {"$group": {"_id": {f: "$" + f for f in fields}, "n": {"$sum": 1}}},
    ])
    labelled = [(" / ".join(str(row["_id"].get(f) or "unknown") for f in fields), row["n"]) for row in rows]
    return sorted(labelled, key=lambda item: (-item[1], item[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--by", choices=sorted(LEVELS), default="region")
    parser.add_argument("--channel")
    args = parser.parse_args()

    from emotorad_ai.stores.mongo import CONVERSATION_ORIGINS, connect

    rows = counts(connect()[CONVERSATION_ORIGINS], args.by, args.start, args.end, args.channel)
    width = max([len(label) for label, _ in rows] + [5])
    for label, n in rows:
        print("%-*s %6d" % (width, label, n))
    print("%-*s %6d" % (width, "total", sum(n for _, n in rows)))
    print(CREDIT)


if __name__ == "__main__":
    main()
