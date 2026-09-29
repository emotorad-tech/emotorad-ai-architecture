"""Delete everything the chatbot holds about one person (right to erasure).

    python scripts/delete_person.py --phone 9876543210            # dry run: counts only
    python scripts/delete_person.py --phone 9876543210 --yes      # delete
    python scripts/delete_person.py --dealer-id DLR-PUN-014 --yes

Working state, transcript turns and conversation summaries, found by the
person's key. Idempotency receipts carry no person and expire within seven
days on their own. Reads EMOTORAD_MONGO_URI (never printed). Run by a person,
never by a Claude session, and record who asked and when.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.identity import PHONE, normalise  # noqa: E402
from emotorad_ai.stores.mongo import (  # noqa: E402
    CONVERSATION_SUMMARIES, CONVERSATIONS, TRANSCRIPT_TURNS, MongoConversationStore, connect,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--phone", help="the customer's phone, in any format")
    who.add_argument("--dealer-id", help="the dealer's id, e.g. DLR-PUN-014")
    parser.add_argument("--yes", action="store_true", help="actually delete; without it, only count")
    args = parser.parse_args()

    user_key = "PHONE#" + normalise(PHONE, args.phone) if args.phone else "DEALER#" + args.dealer_id
    db = connect(db_name=os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai"))
    counts = {name: db[name].count_documents({"user_key": user_key})
              for name in (CONVERSATIONS, TRANSCRIPT_TURNS, CONVERSATION_SUMMARIES)}
    print("person: %s" % user_key)
    for name, count in counts.items():
        print("  %-24s %d" % (name, count))
    if not args.yes:
        print("dry run: nothing deleted. Add --yes to delete.")
        return 0
    deleted = MongoConversationStore(db).delete_person(user_key)
    print("deleted: %s" % ", ".join("%s %d" % item for item in deleted.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
