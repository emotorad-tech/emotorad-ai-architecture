"""Create the chatbot's collections and indexes in MongoDB. Safe to rerun.

    python scripts/mongo_setup.py

Reads EMOTORAD_MONGO_URI (never printed) and EMOTORAD_MONGO_DB (default
emotorad_ai). Run by a person with a database user that has readWrite on that
database only. A Claude session never runs this: org rules forbid writing to a
shared database from one.

Prints every collection and index so the output can be checked, and fails if
the permanent record (transcripts, summaries, media) has picked up an expiry
index.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.stores.mongo import (  # noqa: E402
    CONVERSATION_SUMMARIES, MEDIA, MONGO_URI_ENV, TRANSCRIPT_TURNS, connect, ensure_indexes,
)

PERMANENT = (TRANSCRIPT_TURNS, CONVERSATION_SUMMARIES, MEDIA)


def main() -> int:
    uri = os.environ.get(MONGO_URI_ENV, "")
    db_name = os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai")
    host = urlsplit(uri).hostname if uri else None
    print("cluster: %s | database: %s | user: %s" % (host, db_name, urlsplit(uri).username if uri else None))
    db = connect(db_name=db_name)
    report = ensure_indexes(db)

    ok = True
    for collection, names in report.items():
        info = db[collection].index_information()
        cells = []
        for name in names:
            ttl = info[name].get("expireAfterSeconds")
            cells.append("%s%s" % (name, " (TTL %ss)" % ttl if ttl is not None else ""))
            if collection in PERMANENT and ttl is not None:
                ok = False
        kept = "permanent" if collection in PERMANENT else "expires"
        print("  %-24s %-10s %s" % (collection, kept, ", ".join(cells)))

    print("setup OK" if ok else "SETUP PROBLEM: a permanent collection has a TTL index")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
