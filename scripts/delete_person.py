"""Delete everything the chatbot holds about one person (right to erasure).

    python scripts/delete_person.py --phone 9876543210                          # dry run: counts only
    python scripts/delete_person.py --phone 9876543210 --yes --reason "email from customer, 2026-09-29"
    python scripts/delete_person.py --dealer-id DLR-PUN-014 --yes --reason "..."
    python scripts/delete_person.py --conversation-id web-4f2a --yes --reason "..."

A person's every conversation, whole: working state, every transcript turn
(those from before they signed in too), summaries, idempotency receipts,
media (every photo or video they sent) and where each conversation came from
(conversation_origins). `--conversation-id` removes one
conversation that was never tied to a verified person. Each deletion writes an
audit record to `erasure_log`: who ran it, when, why and what went, with the
person's key only as a SHA-256 hash, so the log itself holds nothing about
them. Reads EMOTORAD_MONGO_URI (never printed). Run by a person, never by a
Claude session.

Erasing anyone who sent media also needs EMOTORAD_AI_MEDIA_BUCKET set and AWS
credentials allowed s3:ListBucketVersions and s3:DeleteObjectVersion on that
bucket, since the instance role has neither, on purpose. Without them the
script refuses rather than delete the database records and orphan the objects.
"""

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.erasure import audit_record  # noqa: E402
from emotorad_ai.identity import PHONE, normalise  # noqa: E402
from emotorad_ai.observability import redact_pii  # noqa: E402
from emotorad_ai.storage.s3 import BUCKET_ENV, StorageError, store_from_env  # noqa: E402
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

    # Keys hold cluster and conversation ids, not personal data, so they are
    # safe to print. The records are found the same way `delete_person` finds
    # its conversations: everything tied to the subject, or the one
    # conversation named on the command line.
    conversation_ids = [args.conversation_id] if args.conversation_id else store.conversations_of(subject)
    media = [record for cid in conversation_ids for record in store.media_of(cid)]

    print("subject: %s" % subject)
    for name, count in erase(dry_run=True).items():
        print("  %-24s %d" % (name, count))
    if media:
        print("  media objects in S3:")
        for record in media:
            print("    %s" % record["key"])
    if not args.yes:
        print("dry run: nothing deleted. Add --yes --reason \"...\" to delete.")
        return 0

    s3_objects = 0
    s3_versions = 0
    if media:
        s3 = store_from_env()
        if s3 is None:
            print("%s is not set: the S3 objects cannot be deleted without the bucket and credentials." % BUCKET_ENV)
            print("nothing was deleted.")
            return 2
        deleted_keys = []
        try:
            for record in media:
                key = record["key"]
                s3_versions += s3.delete_every_version(key)
                s3_objects += 1
                deleted_keys.append(key)
        except StorageError as exc:
            remaining = [r["key"] for r in media if r["key"] not in deleted_keys]
            print("S3 deletion failed, stopping before any database record is touched: %s" % exc)
            print("deleted from S3: %s" % (", ".join(deleted_keys) if deleted_keys else "none"))
            print("not deleted: %s" % ", ".join(remaining))
            print("rerun the same command once the problem is fixed: deleting a version twice is harmless.")
            db[ERASURE_LOG].insert_one(audit_record(
                subject, redact_pii(args.reason.strip()), getpass.getuser(), datetime.now(timezone.utc),
                s3_objects=s3_objects, s3_versions=s3_versions, incomplete=True,
            ))
            print("audit record written to %s (incomplete)" % ERASURE_LOG)
            return 1

    deleted = erase(dry_run=False)
    audit = audit_record(
        subject, redact_pii(args.reason.strip()),  # a reason can quote a number
        getpass.getuser(), datetime.now(timezone.utc), deleted=deleted,
        s3_objects=s3_objects if media else None, s3_versions=s3_versions if media else None,
    )
    db[ERASURE_LOG].insert_one(audit)
    print("deleted: %s" % ", ".join("%s %d" % item for item in deleted.items()))
    if media:
        print("  s3 objects              %d" % s3_objects)
        print("  s3 versions             %d" % s3_versions)
    print("audit record written to %s" % ERASURE_LOG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
