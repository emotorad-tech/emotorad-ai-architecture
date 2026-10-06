"""The ticket store in memory: one process, lost on restart.

What tests and a server on the memory store use. MongoTicketStore
(stores/mongo.py) does the same in MongoDB for every server. Both are held to
tests/ticket_store_contract.py. Every method takes the lock, so the worker
thread and a turn never see half an update.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..conversation import StoreUnavailable
from .clock import parse, plus
from .kinds import FIRST_DESK_NUMBER, STUCK_SECONDS, URGENT_LATE_SECONDS, desk_reference
from .record import GONE, HELD, OUTSTANDING, SENT, STUCK, SUPPORT_CLOSED, WAITING, support_status


def age_seconds(since: str, now: str) -> int:
    """Whole seconds from `since` to `now`."""
    return int((parse(now) - parse(since)).total_seconds())


def listing_row(record: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """One line of the listing for the support lead: no name, no summary, and
    only the last four digits of the number."""
    digits = "".join(ch for ch in record.get("phone") or "" if ch in "0123456789")
    return {
        "reference": record["_id"],
        "zoho_number": (record.get("zoho") or {}).get("ticket_number"),
        "state": record["state"] if record.get("mode") == mode else HELD,
        "mode": record.get("mode"),
        "last_four": digits[-4:] or None,
    }


def _set_path(doc: Dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for part in parents:
        doc = doc.setdefault(part, {})
    doc[last] = value


def _list_at(doc: Dict[str, Any], path: str) -> List[Any]:
    *parents, last = path.split(".")
    for part in parents:
        doc = doc.setdefault(part, {})
    return doc.setdefault(last, [])


class InMemoryTicketStore:
    """The ticket store protocol (the plan's shared interfaces) in one process."""

    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}
        self._next_number = FIRST_DESK_NUMBER
        self._lock = threading.Lock()

    # -- recording ---------------------------------------------------------------

    def next_reference(self) -> str:
        with self._lock:
            number = self._next_number
            self._next_number += 1
        return desk_reference(number)

    def insert(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The record, or the one already recorded under its source key: a
        retried create never makes a second ticket."""
        with self._lock:
            existing = self._with_source_key(record["source_key"])
            if existing is not None:
                return copy.deepcopy(existing)
            if record["_id"] in self._records:
                # A reference handed out twice: refuse rather than overwrite
                # somebody's ticket.
                raise StoreUnavailable("ticket reference %s is already used" % record["_id"])
            self._records[record["_id"]] = copy.deepcopy(record)
            return copy.deepcopy(record)

    def get(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._records.get(reference))

    def by_source_key(self, source_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._with_source_key(source_key))

    def ticket_status(self, reference: str) -> Optional[Dict[str, Any]]:
        """{"reference", "status", "closed_at"} as the rider sees it, or None
        when no record has this reference (the mock's tickets)."""
        with self._lock:
            record = self._records.get(reference)
            return support_status(record) if record is not None else None

    def _with_source_key(self, source_key: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._records.values() if r["source_key"] == source_key), None)

    def close_support(self, zoho_ticket_id: Optional[str], closed_at: str) -> Optional[Tuple[Dict[str, Any], bool]]:
        """Support closed this Zoho Desk ticket: the record sent as it, closed
        once under the lock, `closed_at` kept from the first closure. None
        when no record was sent as this ticket; otherwise the record and
        whether this call closed it."""
        if not zoho_ticket_id:
            return None
        with self._lock:
            record = next((r for r in self._records.values()
                           if (r.get("zoho") or {}).get("ticket_id") == zoho_ticket_id), None)
            if record is None:
                return None
            if record.get("support_status") == SUPPORT_CLOSED:
                return copy.deepcopy(record), False
            record.update(support_status=SUPPORT_CLOSED, closed_at=closed_at)
            return copy.deepcopy(record), True

    # -- new content -------------------------------------------------------------

    def wake(self, reference: str, now: str) -> bool:
        with self._lock:
            return self._wake(reference, now)

    def add_note(self, reference: str, text: str, now: str) -> bool:
        with self._lock:
            record = self._records.get(reference)
            if record is None or record["state"] == GONE:
                return False
            record.setdefault("notes", []).append({"text": text, "at": now})
            return self._wake(reference, now)

    def _wake(self, reference: str, now: str) -> bool:
        """Under the lock. A sent record has work again from now; an
        outstanding one is due now, never later than it already was; a gone
        one takes no work at all."""
        record = self._records.get(reference)
        if record is None or record["state"] == GONE:
            return False
        record["wake"] = record.get("wake", 0) + 1
        if record["state"] == SENT:
            record.update(state=WAITING, due_since=now, next_attempt_at=now)
        else:
            record["next_attempt_at"] = min(record.get("next_attempt_at") or now, now)
        return True

    def close_runs(self, conversation_id: str, new_started_at: str) -> int:
        with self._lock:
            closed = 0
            for record in self._records.values():
                started = record.get("started_at")
                if (record["conversation_id"] == conversation_id and started is not None
                        and started < new_started_at and record.get("ended_at") is None):
                    record["ended_at"] = new_started_at
                    closed += 1
            return closed

    # -- the worker --------------------------------------------------------------

    def take_due(self, now: str, mode: str, lease_seconds: float, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            due = [r for r in self._records.values()
                   if r["state"] in OUTSTANDING and r["mode"] == mode and r["next_attempt_at"] <= now
                   and (r.get("lease_until") is None or r["lease_until"] < now)]
            if not due:
                return None
            record = min(due, key=lambda r: (not r["urgent"], r["next_attempt_at"], r["created_at"], r["_id"]))
            record["lease_until"] = plus(now, lease_seconds)
            record["lease_token"] = token
            return copy.deepcopy(record)

    def renew_lease(self, reference: str, token: str, until: str) -> bool:
        with self._lock:
            record = self._records.get(reference)
            if not token or record is None or record.get("lease_token") != token:
                return False
            record["lease_until"] = until
            return True

    def save(
        self,
        reference: str,
        token: str,
        changes: Dict[str, Any],
        add_to_set: Optional[Dict[str, List[Any]]] = None,
        push: Optional[Dict[str, List[Any]]] = None,
        expect_wake: Optional[int] = None,
    ) -> bool:
        """Only under this lease, and with the wake count unchanged when asked.
        Never creates a record: False when nothing matched."""
        with self._lock:
            record = self._records.get(reference)
            if not token or record is None or record.get("lease_token") != token:
                return False
            if expect_wake is not None and record.get("wake") != expect_wake:
                return False
            for path, value in changes.items():
                _set_path(record, path, copy.deepcopy(value))
            for path, values in (add_to_set or {}).items():
                items = _list_at(record, path)
                for value in values:
                    if value not in items:
                        items.append(copy.deepcopy(value))
            for path, values in (push or {}).items():
                _list_at(record, path).extend(copy.deepcopy(list(values)))
            return True

    def mark_stuck(self, reference: str) -> bool:
        """A waiting record becomes stuck, with or without a lease: the check
        for overdue records does not hold one. Nothing else is changed, and
        any other state is refused."""
        with self._lock:
            record = self._records.get(reference)
            if record is None or record["state"] != WAITING:
                return False
            record["state"] = STUCK
            return True

    # -- reporting ---------------------------------------------------------------

    def overdue(self, now: str, mode: str) -> List[Dict[str, Any]]:
        late, stuck = plus(now, -URGENT_LATE_SECONDS), plus(now, -STUCK_SECONDS)
        with self._lock:
            found = [copy.deepcopy(r) for r in self._records.values()
                     if r["state"] in OUTSTANDING and r["mode"] == mode
                     and r["due_since"] <= (late if r["urgent"] else stuck)]
        return sorted(found, key=lambda r: (r["due_since"], r["_id"]))

    def counts(self, mode: str, now: str) -> Dict[str, Any]:
        with self._lock:
            mine = [r for r in self._records.values() if r["state"] in OUTSTANDING and r["mode"] == mode]
            held = sum(1 for r in self._records.values() if r["state"] in OUTSTANDING and r["mode"] != mode)
        oldest = min((r["due_since"] for r in mine), default=None)
        return {
            "waiting": sum(1 for r in mine if r["state"] == WAITING),
            "stuck": sum(1 for r in mine if r["state"] == STUCK),
            "held": held,
            "oldest_due_seconds": age_seconds(oldest, now) if oldest else None,
        }

    def unverified_since(self, since: str, phone: Optional[str] = None) -> int:
        with self._lock:
            return sum(1 for r in self._records.values()
                       if r["identity"] == "unverified" and not r["urgent"] and r["created_at"] >= since
                       and (phone is None or r.get("phone") == phone))

    def contact_for(self, phone: str) -> Optional[str]:
        """A Zoho contact only from a live record of a proved number: never
        a test or an unverified one's."""
        with self._lock:
            mine = sorted((r for r in self._records.values()
                           if r.get("phone") == phone and r["mode"] == "live" and r["identity"] == "verified"),
                          key=lambda r: (r["created_at"], r["_id"]), reverse=True)
            return next((r["zoho"]["contact_id"] for r in mine if (r.get("zoho") or {}).get("contact_id")), None)

    def listing(self, mode: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = sorted((r for r in self._records.values() if r["state"] in OUTSTANDING),
                          key=lambda r: (r["created_at"], r["_id"]))
            return [listing_row(r, mode) for r in rows]

    def has_unique_source_key(self) -> bool:
        """Always: insert holds the lock while it looks for the key."""
        return True
