"""Test the chat system end to end on the real MongoDB database, then clean up.

    python scripts/mongo_smoke.py

Run `scripts/mongo_setup.py` first. Reads EMOTORAD_MONGO_URI (never printed).
Runs the real runtime (offline model, mocked business tools: no OpenRouter, no
OMS, no Zoho) as the Amiigo test rider (sess-amiigo-test, fixture phone
+919700000010), and checks every part of the store: persistence across a
restart, transcripts, summaries, memory, tickets carrying the transcript,
conflict protection, duplicate-ticket protection, expiry and deletion.

Everything it writes is under a unique smoke-... id, and it deletes it all at
the end, whether the checks pass or fail. Deleting the test rider's data also
removes any other conversations held for that fixture phone, which is test
data only. Run by a person, never by a Claude session.
"""

import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.adapters import AmiigoAdapter  # noqa: E402
from emotorad_ai.config import Settings  # noqa: E402
from emotorad_ai.conversation import ConversationConflict  # noqa: E402
from emotorad_ai.identity import IdentityResolver  # noqa: E402
from emotorad_ai.llm import OfflinePlanner  # noqa: E402
from emotorad_ai.observability import EventLog  # noqa: E402
from emotorad_ai.runtime import Runtime  # noqa: E402
from emotorad_ai.stores.mongo import INDEXES, connect  # noqa: E402
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, MockTicketSystem, build_registry  # noqa: E402
from emotorad_ai.tools.registry import ToolContext  # noqa: E402
from emotorad_ai.wiring import build_stores  # noqa: E402

SESSION = "sess-amiigo-test"
USER_KEY = "PHONE#+919700000010"


def run_smoke(client: Any = None, db_name: Optional[str] = None, out: Callable[[str], None] = print) -> bool:
    db_name = db_name or os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai")
    tag = "smoke-" + uuid.uuid4().hex[:10]
    settings = Settings(store="mongodb", mode="offline", mongo_db=db_name, log_path="")
    results: List[Tuple[str, bool]] = []

    def check(name: str, passed: Any, detail: str = "") -> None:
        results.append((name, bool(passed)))
        out("%s  %s%s" % ("PASS" if passed else "FAIL", name, "  (%s)" % detail if detail and not passed else ""))

    def new_runtime():
        # A fresh connection and fresh stores each time: a restart, or a second server.
        stores = build_stores(settings, client=client)
        registry = build_registry(idempotency=stores.idempotency)
        runtime = Runtime(settings=settings, registry=registry, llm=OfflinePlanner(), log=EventLog(path=None),
                          resolver=IdentityResolver(registry), conversations=stores.conversations)
        return runtime, AmiigoAdapter(runtime.resolver), stores

    def send(runtime, adapter, cid, text):
        return runtime.handle(adapter.to_message({"conversation_id": cid, "session_token": SESSION, "text": text}))

    db = connect(db_name=db_name, client=client)
    missing = {c: sorted(o["name"] for _, o in idx if o["name"] not in db[c].index_information()) for c, idx in INDEXES.items()}
    missing = {c: names for c, names in missing.items() if names}
    check("0. setup has been run: every collection has its indexes", not missing, "missing %s; run mongo_setup.py" % missing)
    if missing:
        return False

    a, b = tag + "-a", tag + "-b"
    stores_for_cleanup = None
    try:
        rt, ad, stores = new_runtime()
        stores_for_cleanup = stores
        first = send(rt, ad, a, "hi")
        check("1. a new chat is answered (the rider has two bikes, so it asks which)", "Which one" in first.text, first.text[:80])
        send(rt, ad, a, "1")
        routed = send(rt, ad, a, "my battery won't charge")
        check("2. the chat reaches battery support", routed.handled_by == "battery_support", routed.handled_by)

        rt2, ad2, stores2 = new_runtime()
        after = send(rt2, ad2, a, "still not charging, my number is 9812345678")
        check("3. after a restart, the chat continues where it was", after.handled_by == "battery_support", after.handled_by)

        turns = stores2.conversations.transcript(a)
        check("4. the transcript holds every message, in order", [t.role for t in turns] == ["customer", "bot"] * 4,
              str([t.role for t in turns]))
        check("5. the transcript blanks out phone numbers", turns and "9812345678" not in turns[6].text)

        summary = [s for s in stores2.conversations.recent_summaries(USER_KEY) if s.conversation_id == a]
        check("6. a summary is kept for the person", summary and summary[0].title == "Battery issue"
              and summary[0].product_name == "EMX Plus", str(summary[:1]))

        send(rt2, ad2, b, "hi")
        memory = stores2.conversations.get(b).context_block or ""
        check("7. a new chat remembers the last one", "Last contact:" in memory and "Battery issue" in memory)

        # The bike is chosen first: a two-bike rider's safety ticket needs one
        # named (a separate, known gap in the safety branch, tracked on its own).
        send(rt2, ad2, b, "1")
        safety = send(rt2, ad2, b, "my battery is swollen")
        check("8. a safety report raises a ticket", bool(safety.ticket_id), safety.handled_by)
        ticket = rt2.registry.tickets.tickets.get(safety.ticket_id or "", {})
        check("9. the ticket carries the conversation transcript", "my battery is swollen" in ticket.get("transcript", ""))

        one, other = stores2.conversations.get(a), stores2.conversations.get(a)
        stores2.conversations.save(one)
        try:
            stores2.conversations.save(other)
            conflicted = False
        except ConversationConflict:
            conflicted = True
        check("10. two servers cannot overwrite each other's save", conflicted)

        tickets = MockTicketSystem()
        server_one = build_registry(ticket_system=tickets, idempotency=stores.idempotency)
        server_two = build_registry(ticket_system=tickets, idempotency=stores2.idempotency)
        call = {"category": "battery_charging", "severity": "normal", "description": "Smoke test.",
                "frame_number": "EMXP2026001234", "idempotency_key": "k"}
        context = ToolContext(conversation_id=tag + "-idem", phone="+919700000010")
        first_try = server_one.call(CREATE_SUPPORT_TICKET, dict(call), context)
        retry = server_two.call(CREATE_SUPPORT_TICKET, dict(call), context)
        check("11. a retried ticket is raised once, even across servers", first_try == retry and len(tickets.tickets) == 1)

        working = db["conversations"].find_one({"_id": a}) or {}
        record = db["transcript_turns"].find_one({"conversation_id": a}) or {"expires_at": "?"}
        check("12. working state expires; the transcript is permanent", working.get("expires_at") and "expires_at" not in record)
    finally:
        if stores_for_cleanup is not None:
            deleted = stores_for_cleanup.conversations.delete_person(USER_KEY)
            pattern = {"$regex": "^" + re.escape(tag)}
            receipts = db["idempotency_keys"].delete_many({"_id": pattern}).deleted_count
            leftovers = (db["conversations"].count_documents({"_id": pattern})
                         + db["transcript_turns"].count_documents({"conversation_id": pattern})
                         + db["conversation_summaries"].count_documents({"_id": pattern}))
            check("13. deletion on request removes everything for the person", leftovers == 0, "%d left" % leftovers)
            out("cleaned up: %s, idempotency_keys %d" % (", ".join("%s %d" % i for i in deleted.items()), receipts))

    passed = all(ok for _, ok in results)
    out("SMOKE OK: %d checks passed" % len(results) if passed else "SMOKE FAILED: %d of %d checks failed"
        % (sum(1 for _, ok in results if not ok), len(results)))
    return passed


if __name__ == "__main__":
    raise SystemExit(0 if run_smoke() else 1)
