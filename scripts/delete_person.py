"""Delete everything the chatbot holds about one person (right to erasure).

    python scripts/delete_person.py --phone 9876543210                          # dry run: counts only
    python scripts/delete_person.py --phone 9876543210 --yes --reason "email from customer, 2026-09-29"
    python scripts/delete_person.py --dealer-id DLR-PUN-014 --yes --reason "..."
    python scripts/delete_person.py --conversation-id web-4f2a --yes --reason "..."

A person's every conversation, whole: working state, every transcript turn
(those from before they signed in too), summaries and idempotency receipts.
`--conversation-id` removes one conversation that was never tied to a verified
person. Each deletion writes an audit record to `erasure_log`: who ran it,
when, why and what went, with the person's key only as a SHA-256 hash, so the
log itself holds nothing about them. Reads EMOTORAD_MONGO_URI (never printed).
Run by a person, never by a Claude session.
"""

import argparse
import getpass
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.identity import PHONE, normalise  # noqa: E402
from emotorad_ai.observability import redact_pii  # noqa: E402
from emotorad_ai.stores.mongo import MongoConversationStore, connect  # noqa: E402

ERASURE_LOG = "erasure_log"


def main() -> int:
    parser = argparse.ArgumentParser()
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--phone", help="the customer's phone, in any format")
    who.add_argument("--dealer-id", help="the dealer's id, e.g. DLR-PUN-014")
    who.add_argument("--conversation-id", help="one conversation not tied to a verified person")
    parser.add_argument("--yes", action="store_true", help="actually delete; without it, only count")
    parser.add_argument("--reason", help="who asked and when; required with --yes, kept in the audit record")
    args = parser.parse_args()
    if args.yes and not (args.reason or "").strip():
        parser.error("--yes needs --reason: say who asked for the deletion and when")

    if args.conversation_id:
        subject = "CONVERSATION#" + args.conversation_id
    elif args.phone:
        subject = "PHONE#" + normalise(PHONE, args.phone)
    else:
        subject = "DEALER#" + args.dealer_id
    db = connect(db_name=os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai"))
    store = MongoConversationStore(db)

    def erase(dry_run):
        if args.conversation_id:
            return store.delete_conversation(args.conversation_id, dry_run=dry_run)
        return store.delete_person(subject, dry_run=dry_run)

    print("subject: %s" % subject)
    for name, count in erase(dry_run=True).items():
        print("  %-24s %d" % (name, count))
    if not args.yes:
        print("dry run: nothing deleted. Add --yes --reason \"...\" to delete.")
        return 0
    deleted = erase(dry_run=False)
    db[ERASURE_LOG].insert_one({
        "key_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest(),
        "kind": subject.split("#", 1)[0],
        "reason": redact_pii(args.reason.strip()),  # a reason can quote a number
        "run_by": getpass.getuser(),
        "at": datetime.now(timezone.utc),
        "deleted": deleted,
    })
    print("deleted: %s" % ", ".join("%s %d" % item for item in deleted.items()))
    print("audit record written to %s" % ERASURE_LOG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
