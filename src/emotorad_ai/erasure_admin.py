"""Manual erasure (spec 2026-10-01-manual-erasure-design.md).

A person reviews each self-service deletion request and deletes it by hand,
in the live container through an SSM session:

    aws ssm start-session --target i-02e7dc2874e0fdacb
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin list
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin show DEL-XXXXXX
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin hold DEL-XXXXXX
    sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin delete DEL-XXXXXX

`check` is the daily workflow's (.github/workflows/erasure-check.yml): open
requests by reference and age only.

`show` prints the customer's phone number, chats and photo links: it is for
the person deciding, never for a Claude session or a CI log. The review it
records in the request is the log of who read what.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import erasure
from .storage.s3 import StorageError

REVIEW_VALID = timedelta(hours=24)
RECENT = timedelta(hours=48)
REMIND_AFTER_DAYS = 25
FILES = "s3_objects"


class Refused(Exception):
    """A step that will not go ahead. The message is for the person."""


def _when(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _proof(proof: Optional[Dict[str, Any]]) -> str:
    if not proof:
        return "not recorded"
    if proof.get("method") == "otp":
        return "OTP, verified at %s" % (proof.get("verified_at") or "an unrecorded time")
    if proof.get("method") == "app_sign_in":
        return "app sign-in"
    return str(proof.get("method"))


def _latest_review(record: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    mine = [r for r in record.get("reviews") or [] if r["by"].strip().casefold() == name.casefold()]
    return max(mine, key=lambda r: _when(r["at"])) if mine else None


def _counts(counts: Dict[str, int]) -> str:
    return ", ".join("%s %d" % (key, counts[key]) for key in sorted(counts))


class Admin:
    def __init__(self, store: Any, media_store: Any, now: Callable[[], datetime],
                 ask: Callable[[str], str], out: Callable[[str], None]) -> None:
        self.store, self.media_store, self.now, self.ask, self.out = store, media_store, now, ask, out

    # -- shared ---------------------------------------------------------------

    def _request(self, reference: str) -> Dict[str, Any]:
        record = self.store.erasure_record(reference)
        if record is None:
            raise Refused("No erasure request %s." % reference)
        if record["status"] != "pending":
            raise Refused("%s is %s, not pending. Nothing was changed." % (reference, record["status"]))
        return record

    def _answer(self, prompt: str, what: str) -> str:
        answer = (self.ask(prompt) or "").strip()
        if not answer:
            raise Refused("%s is needed. Nothing was changed." % what)
        return answer

    def _files(self, user_key: str) -> List[Dict[str, Any]]:
        return [record for cid in self.store.conversations_of(user_key) for record in self.store.media_of(cid)]

    def totals(self, user_key: str) -> Dict[str, int]:
        """Exactly what delete would remove: the records per collection and the files."""
        counts = dict(self.store.delete_person(user_key, dry_run=True))
        counts[FILES] = len(self._files(user_key))
        return counts

    def _age(self, record: Dict[str, Any]) -> int:
        return (self.now() - _when(record["requested_at"])).days

    def _print_totals(self, totals: Dict[str, int]) -> None:
        self.out("Totals (what delete removes)")
        for key in sorted(totals):
            self.out("  %s: %d" % (key, totals[key]))

    # -- the steps ------------------------------------------------------------

    def list_open(self) -> int:
        """Every open request, oldest first. No phone number, chat text or note."""
        requests = self.store.pending_erasures()
        for record in requests:
            user_key = record["user_key"]
            totals = self.totals(user_key)
            self.out("%s  %3d days  %-12s  %-7s  chats=%d turns=%d files=%d" % (
                record["_id"], self._age(record), record.get("channel") or "-",
                "held" if record.get("held") else "pending", len(self.store.conversations_of(user_key)),
                totals.get("transcript_turns", 0), totals[FILES]))
        self.out("open erasure requests: %d" % len(requests))
        return 0

    def show(self, reference: str) -> int:
        record = self._request(reference)
        name = self._answer("Your name: ", "Your name")
        user_key = record["user_key"]
        now = self.now()
        totals = self.totals(user_key)
        conversations = self.store.conversations_of(user_key)
        transcripts = {cid: self.store.transcript(cid) for cid in conversations}
        summaries: Dict[str, List[Any]] = {}
        for item in self.store.recent_summaries(user_key, limit=1000):
            summaries.setdefault(item.conversation_id, []).append(item)
        self.store.record_erasure_review(reference, name, now.isoformat(), totals)

        out = self.out
        out("%s  %s  asked %s (%d days ago)" % (
            reference, "held" if record.get("held") else "pending", record["requested_at"], self._age(record)))
        out("")
        out("Flags")
        for flag in self._flags(conversations, summaries, transcripts, now) or ["No flags"]:
            out("  " + flag)
        out("")
        out("Is it genuine")
        out("  phone: %s" % user_key.split("#", 1)[-1])
        out("  channel: %s" % (record.get("channel") or "-"))
        out("  proof: %s" % _proof(record.get("proof")))
        out("  asked from conversation: %s" % (record.get("conversation_id") or "-"))
        held = record.get("held")
        if held:
            out("  held by %s at %s: %s" % (held["by"], held["at"], held["note"]))
        earlier = [r for r in self.store.erasure_history(user_key) if r["_id"] != reference]
        if not earlier:
            out("  earlier requests: none")
        for r in earlier:
            out("  earlier request %s: %s, asked %s, closed %s" % (
                r["_id"], r["status"], r["requested_at"], r.get("processed_at") or "-"))
        for cid in conversations:
            self._chat(cid, summaries.get(cid, []), transcripts[cid])
        out("")
        out("Files")
        files = self._files(user_key)
        if not files:
            out("  No files")
        for f in files:
            out("  %s  %s  %s bytes  stored %s  %s" % (
                f.get("kind") or "-", f.get("mime_type") or "-", f.get("size_bytes", "-"),
                f.get("stored_at") or "-", f["key"]))
            out("    open for 15 minutes: %s" % self._link(f["key"]))
        out("")
        self._print_totals(totals)
        out("")
        out("Review recorded: %s at %s." % (name, now.isoformat()))
        return 0

    def hold(self, reference: str) -> int:
        self._request(reference)
        name = self._answer("Your name: ", "Your name")
        note = self._answer("Note (why it is held, no customer details): ", "A note")
        self.store.hold_erasure(reference, name, self.now().isoformat(), note)
        self.out("%s is held: %s" % (reference, note))
        return 0

    def delete(self, reference: str) -> int:
        record = self._request(reference)
        name = self._answer("Your name: ", "Your name")
        reason = self._answer("Reason (who asked, and why it can go): ", "A reason")
        user_key = record["user_key"]
        review = _latest_review(record, name)
        if review is None:
            raise Refused("No review of %s by %s. Run show %s first. Nothing was deleted." % (reference, name, reference))
        if self.now() - _when(review["at"]) > REVIEW_VALID:
            raise Refused("Your review of %s is more than 24 hours old. Run show %s again. Nothing was deleted."
                          % (reference, reference))
        totals = self.totals(user_key)
        if totals != review["totals"]:
            raise Refused("What %s holds has changed since your review. Run show %s again. Nothing was deleted."
                          % (reference, reference))
        self._print_totals(totals)
        typed = (self.ask("Type %s to delete it, or anything else to stop: " % reference) or "").strip().upper()
        if typed != reference:
            raise Refused("Stopped. Nothing was deleted.")
        # The customer may have cancelled while the person read and typed.
        self._request(reference)
        keys = [f["key"] for f in self._files(user_key)]
        try:
            if keys and self.media_store is None:
                raise StorageError("no media bucket configured")
            for key in keys:
                self.media_store.hide(key)
        except Exception as exc:
            error = type(exc).__name__
            self.store.record_erasure_failure(reference, error)
            self.out("Could not hide the files (%s). Nothing in the database was deleted. "
                     "Fix the cause and run delete %s again." % (error, reference))
            return 1
        now = self.now()
        counts = self.store.delete_person(user_key)
        self.store.log_erasure(erasure.audit_record(
            user_key, "self-service request %s: %s" % (reference, reason), name, now,
            deleted=counts, s3_objects=len(keys)))
        self.store.close_erasure(reference, "done", counts, None, now.isoformat(), by=name)
        self.out("Deleted %s: %d files hidden; records: %s. The audit record is written."
                 % (reference, len(keys), _counts(counts)))
        return 0

    def check(self) -> int:
        """References and ages only: red at 25 days, held ones included."""
        requests = self.store.pending_erasures()
        late = 0
        for record in requests:
            age = self._age(record)
            late += age >= REMIND_AFTER_DAYS
            self.out("%s  %3d days  %s" % (record["_id"], age, "held" if record.get("held") else "pending"))
        self.out("open erasure requests: %d" % len(requests))
        if late:
            self.out("%d request(s) are %d days old or more: deal with them before 30 days."
                     % (late, REMIND_AFTER_DAYS))
        return 1 if late else 0

    # -- show's parts ---------------------------------------------------------

    @staticmethod
    def _flags(conversations: Sequence[str], summaries: Dict[str, List[Any]],
               transcripts: Dict[str, List[Any]], now: datetime) -> List[str]:
        """What may still be needed: a ticket, a safety hand-over, a chat in use."""
        flags: List[str] = []
        for cid in conversations:
            for item in summaries.get(cid, []):
                if item.ticket_id:
                    flags.append("ticket %s raised in chat %s (%s)" % (item.ticket_id, cid, item.started_at))
            turns = transcripts[cid]
            for turn in turns:
                if turn.role == "bot" and turn.handled_by.startswith("guardrail:") and "safety" in turn.handled_by:
                    flags.append("safety hand-over in chat %s at %s (%s)" % (cid, turn.at, turn.handled_by))
            if turns and now - _when(turns[-1].at) < RECENT:
                flags.append("chat %s was used in the last 48 hours (last turn %s)" % (cid, turns[-1].at))
        return flags

    def _chat(self, cid: str, summaries: List[Any], turns: List[Any]) -> None:
        out = self.out
        out("")
        out("Chat %s  (first %s, last %s)" % (cid, turns[0].at if turns else "-", turns[-1].at if turns else "-"))
        origins = self.store.origins_of(cid)
        if not origins:
            out("  came from: not recorded")
        for o in origins:
            out("  came from: %s, %s, %s, %s (%s)" % (
                o.get("source") or "-", o.get("country") or "-", o.get("region") or "-",
                o.get("city") or "-", o.get("channel") or "-"))
        for s in summaries:
            out("  summary: %s | %s | outcome %s | ticket %s" % (
                s.title or "-", s.product_name or "-", s.outcome, s.ticket_id or "-"))
        if not turns:
            out("  No transcript turns")
        for t in turns:
            who = "customer" if t.role == "customer" else "bot (%s)" % (t.handled_by or "-")
            out("  [%s] %s: %s" % (t.at, who, t.text))
            for a in t.attachments:
                out("      attachment: %s %s" % (a.get("kind") or "attachment", a.get("url") or ""))

    def _link(self, key: str) -> str:
        if self.media_store is None:
            return "unavailable (no media bucket configured)"
        try:
            return self.media_store.presign_get(key)
        except Exception as exc:
            return "unavailable (%s)" % type(exc).__name__


# step -> (Admin method, how many arguments: a reference, or none)
STEPS: Dict[str, Tuple[str, int]] = {"list": ("list_open", 0), "show": ("show", 1), "hold": ("hold", 1),
                                     "delete": ("delete", 1), "check": ("check", 0)}
USAGE = "usage: python -m emotorad_ai.erasure_admin %s" % " | ".join(
    name + (" DEL-XXXXXX" if nargs else "") for name, (_, nargs) in STEPS.items())


def run(argv: Sequence[str], admin: Admin) -> int:
    if not argv or argv[0] not in STEPS or len(argv) != 1 + STEPS[argv[0]][1]:
        admin.out(USAGE)
        return 2
    method, _ = STEPS[argv[0]]
    try:
        return getattr(admin, method)(*[arg.strip().upper() for arg in argv[1:]])
    except Refused as exc:
        admin.out(str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        admin.out("Stopped.")
        return 1
    except Exception as exc:
        # The class only: a driver's message can carry a host or a value.
        admin.out("Stopped on an error (%s)." % type(exc).__name__)
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    import os

    from .config_store import load_into_environ
    from .storage.s3 import store_from_env
    from .stores.mongo import MongoConversationStore, connect

    try:
        load_into_environ()
        store = MongoConversationStore(connect(db_name=os.environ.get("EMOTORAD_MONGO_DB", "emotorad_ai")))
        media_store = store_from_env()
    except Exception as exc:
        print("Could not reach the store (%s). Nothing was changed." % type(exc).__name__)
        return 1
    admin = Admin(store, media_store, lambda: datetime.now(timezone.utc), input, print)
    return run(sys.argv[1:] if argv is None else list(argv), admin)


if __name__ == "__main__":
    raise SystemExit(main())
