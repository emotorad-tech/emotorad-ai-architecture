"""The nightly erasure job (spec 2026-10-01).

    python -m emotorad_ai.erasure_job

Every pending self-service request, oldest first: the person's files hidden in
S3 (the lifecycle erases them within 30 days), then their records deleted, an
erasure_log audit record written and the request closed. A request whose files
could not all be hidden keeps its records, counts an attempt and stays pending:
it is tried again every night until the cause is fixed, never given up on (the
person's decision, 2026-10-01). Exits 1 when any request did not finish, so
the GitHub run turns red and GitHub emails the workflow's owner.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import erasure
from .storage.s3 import StorageError

RUN_BY = "nightly erasure job"


@dataclass(frozen=True)
class Outcome:
    reference: str
    status: str  # "done" | "retry"
    counts: Optional[Dict[str, int]]
    s3_objects: int
    error: Optional[str]


def process(store: Any, media_store: Any, now: Callable[[], datetime]) -> List[Outcome]:
    outcomes: List[Outcome] = []
    for request in store.pending_erasures():
        reference, user_key = request["_id"], request["user_key"]
        try:
            keys = [record["key"] for cid in store.conversations_of(user_key) for record in store.media_of(cid)]
            if keys and media_store is None:
                raise StorageError("no media bucket configured")
            for key in keys:
                media_store.hide(key)
            counts = store.delete_person(user_key)
            store.log_erasure(erasure.audit_record(user_key, "self-service request %s" % reference, RUN_BY, now(),
                                                   deleted=counts, s3_objects=len(keys)))
            store.close_erasure(reference, "done", counts, None, now().isoformat())
            outcomes.append(Outcome(reference, "done", counts, len(keys), None))
        except Exception as exc:
            error = type(exc).__name__
            store.record_erasure_failure(reference, error)
            outcomes.append(Outcome(reference, "retry", None, 0, error))
    return outcomes


def main() -> int:
    from .config_store import load_into_environ
    from .storage.s3 import store_from_env
    from .stores.mongo import MongoConversationStore, connect

    load_into_environ()
    store = MongoConversationStore(connect(db_name=os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai")))
    outcomes = process(store, store_from_env(), lambda: datetime.now(timezone.utc))
    for outcome in outcomes:
        detail = outcome.error or json.dumps(outcome.counts, sort_keys=True)
        print("%s %s files=%d %s" % (outcome.reference, outcome.status, outcome.s3_objects, detail))
    print("erasure requests processed: %d" % len(outcomes))
    return 1 if any(outcome.status != "done" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
