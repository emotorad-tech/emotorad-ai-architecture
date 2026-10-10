"""The order worker (spec 2026-10-10 replacement orders, section 4). It sends
queued replacement orders to OMS after the rider has their reply.

A daemon thread in the API process, started by the lifespan only when the
switch is on. It takes one due order at a time under a five-minute lease. OMS
has no protection against a repeat and can answer an error after it has made
the order, so before every send, and after every failed one, it looks the
order up by our reference (afs_order_list matches ticket_id), and an order it
finds is never sent again. It records its intent before sending and sends
nothing if that record fails. Retries after 1, 5, 15 and 60 minutes, then
hourly; after 8 passes or 24 hours the order is `failed` and left to a person,
still holding its bike and part, so the bot never orders it again by itself.

An order is `failed` only when the most recent look answered "not found". When
the give-up limit is reached but the last look errored, OMS may hold the order,
so it stays `queued` as `look_only`: it is never sent again, and each pass
(hourly) only looks: found is `sent`, not found is `failed`, an error waits.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from .oms_afs import SALE_TYPE, OMSAuthError, OMSCallError, OMSRefused, afs_request

RETRY_MINUTES = (1, 5, 15, 60)
MAX_PASSES = 8
GIVE_UP_HOURS = 24
LEASE_SECONDS = 300
_OMS_ERRORS = (OMSAuthError, OMSCallError, OMSRefused)


class OrderWorker:
    def __init__(self, ledger: Any, client: Any, pin_codes: Callable[[str], Optional[str]],
                 log: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 clock: Optional[Callable[[], datetime]] = None, interval: float = 30.0) -> None:
        self._ledger = ledger
        self._client = client
        self._pin_codes = pin_codes
        self._log = log or (lambda event, fields: None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status: Dict[str, Any] = {"running": False, "last_pass": None}

    # -- the loop ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="order-worker", daemon=True)
        self._thread.start()
        self.status["running"] = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self.status["running"] = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # the loop never dies; the order stays queued
                self._log("order_worker_pass_failed", {"error": type(exc).__name__})
            self._stop.wait(self._interval)

    def run_once(self) -> int:
        now = self._clock()
        self.status["last_pass"] = now.isoformat()
        done = 0
        for order in self._ledger.due(now.isoformat()):
            claimed_at = self._clock()  # a lease per claim: a long pass never hands out an expired one
            claimed = self._ledger.claim(order["_id"], claimed_at.isoformat(),
                                         (claimed_at + timedelta(seconds=LEASE_SECONDS)).isoformat())
            if claimed is None:
                continue
            try:
                self.process(claimed)
            except Exception as exc:  # one bad order never stops the pass
                self._log("replacement_order_pass_failed", {"reference": order["_id"], "error": type(exc).__name__})
                self._count_failed_pass(claimed, type(exc).__name__)
            done += 1
        return done

    def _count_failed_pass(self, order: Dict[str, Any], error: str) -> None:
        """A pass that raised still counts toward the give-up limit. If this save fails too,
        the lease runs out and a later pass tries again."""
        try:
            if order.get("status") == "queued":
                self._retry(order, error, looked_not_found=False)
            else:
                self._finish(order)
        except Exception:
            return None

    # -- one order --------------------------------------------------------------

    def process(self, order: Dict[str, Any]) -> None:
        reference = order["_id"]
        order["oms"]["passes"] = int(order["oms"].get("passes") or 0) + 1
        frame = order.get("frame_number")
        try:
            found = self._client.find(reference, frame_number=frame)
        except _OMS_ERRORS as exc:
            return self._retry(order, type(exc).__name__, looked_not_found=False)
        if found:
            return self._sent(order, found)
        if order["oms"].get("look_only"):
            return self._fail(order, order["oms"].get("last_error") or "not_found")
        pin_code_id = self._pin_codes(((order.get("customer") or {}).get("address") or {}).get("pincode") or "")
        if not pin_code_id:
            return self._retry(order, "pin_code_unknown", looked_not_found=True)
        try:
            sale_type_id = self._client.sale_type_id(SALE_TYPE)
        except _OMS_ERRORS as exc:
            return self._retry(order, type(exc).__name__, looked_not_found=True)
        order["oms"]["intent_at"] = self._clock().isoformat()
        try:
            self._ledger.save(order)
        except Exception as exc:
            # No record of the intent: send nothing (the lease runs out and a later pass tries again).
            self._log("replacement_order_intent_unsaved", {"reference": reference, "error": type(exc).__name__})
            return None
        try:
            placed = self._client.place(afs_request(order, pin_code_id, sale_type_id,
                                                    env=getattr(self._client, "env", None)),
                                        "%s:%d" % (reference, order["oms"]["passes"]))
            return self._sent(order, placed)
        except _OMS_ERRORS as exc:
            error = type(exc).__name__
        try:
            found = self._client.find(reference, frame_number=frame)
        except _OMS_ERRORS:
            return self._retry(order, error, looked_not_found=False)  # OMS may hold it: not "no order"
        if found:
            return self._sent(order, found)
        return self._retry(order, error, looked_not_found=True)

    def _sent(self, order: Dict[str, Any], found: Dict[str, str]) -> None:
        order["status"] = "sent"
        order["oms"].update({"order_code": found.get("order_code"), "order_id": found.get("order_id"),
                             "last_error": None})
        self._finish(order)
        self._log("replacement_order_sent", {"reference": order["_id"]})

    def _fail(self, order: Dict[str, Any], error: str) -> None:
        order["status"] = "failed"
        self._finish(order)
        self._log("replacement_order_failed", {"reference": order["_id"], "error": error})

    def _retry(self, order: Dict[str, Any], error: str, looked_not_found: bool) -> None:
        """`looked_not_found`: the most recent look answered "not found". Only then may the
        order be failed; otherwise OMS may hold it, and it waits an hour as look-only."""
        now = self._clock()
        order["oms"]["last_error"] = error
        passes = int(order["oms"]["passes"])
        created = datetime.fromisoformat(order["created_at"])
        look_only = bool(order["oms"].get("look_only"))
        if passes >= MAX_PASSES or now - created >= timedelta(hours=GIVE_UP_HOURS) or look_only:
            if looked_not_found:
                return self._fail(order, error)
            order["oms"]["look_only"] = True
            minutes = 60
        else:
            minutes = RETRY_MINUTES[passes - 1] if passes - 1 < len(RETRY_MINUTES) else 60
        order["next_attempt_at"] = (now + timedelta(minutes=minutes)).isoformat()
        self._finish(order)

    def _finish(self, order: Dict[str, Any]) -> None:
        order["lease_until"] = None
        order["updated_at"] = self._clock().isoformat()
        self._ledger.save(order)
