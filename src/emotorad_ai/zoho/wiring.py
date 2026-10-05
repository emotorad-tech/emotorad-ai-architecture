"""Zoho Desk on or off, decided once at start-up (spec 2026-10-05, sections 1
and 9).

Only api.py calls `build_zoho`. The CLI, the playground and the live
evaluation build their registries with the mock and never come here. So a
laptop with every Zoho setting in its environment still sends nothing.

Zoho is off while EMOTORAD_ZOHO_REFRESH_TOKEN is unset. With it set, the
start-up checks run in order (settings.startup_problem). If any check fails,
the mock stays, exactly as when Zoho is off: nothing is recorded in `tickets`,
/health says why, and `zoho_misconfigured` is logged at error level for the
alarm. There is no state in which tickets are recorded but cannot be sent
safely.

The worker is built here. It is started only by api.py's lifespan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from ..conversation import StoreUnavailable
from ..tickets.seam import DeskTicketSystem, TicketRouter
from ..tools.mocks import MockTicketSystem
from .auth import TokenSource
from .desk import DeskClient
from .http import DeskHTTP
from .settings import NOT_CONFIGURED, STORE_UNREACHABLE, load_zoho_settings, startup_problem
from .worker import ZohoWorker

__all__ = ["NOT_CONFIGURED", "STORE_UNREACHABLE", "ZohoWiring", "build_zoho", "ticket_health", "zoho_status"]


@dataclass
class ZohoWiring:
    # The /health "zoho" value as start-up left it. zoho_status adds what the
    # worker has met since.
    status: str
    # What the registry holds when Zoho is on. None means the mock.
    router: Optional[TicketRouter] = None
    worker: Optional[ZohoWorker] = None
    # The tickets store, whether Zoho is on or off, so /health still counts
    # what a rollback left waiting.
    store: Optional[Any] = None


def build_zoho(
    env: Mapping[str, str],
    *,
    ticket_store: Any,
    store_kind: str,
    conversations: Any,
    media_reader: Any,
    log: Any,
    otp_is_mock: bool,
    opener: Optional[Callable[..., Any]] = None,
) -> ZohoWiring:
    """The ticket system api.py's registry holds, and the worker that sends
    its records. Never calls Zoho and never starts a thread."""
    settings, status = load_zoho_settings(env)
    if settings is None:
        if status != NOT_CONFIGURED:
            _misconfigured(log, status)
        return ZohoWiring(status=status, store=ticket_store)
    try:
        # A ticket_store of None (Stores built without one) is answered as
        # "store is not mongodb": there is nowhere to keep a record.
        problem = startup_problem(
            settings,
            region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "",
            store_kind=store_kind,
            ticket_store=ticket_store,
            dev_codes=env.get("EMOTORAD_AI_DEV_CODES") == "1",
            otp_is_mock=otp_is_mock,
        )
    except StoreUnavailable:
        # The index list could not be read. Starting on the mock shows on
        # /health and raises the alarm. Failing the import would take the
        # chat down.
        problem = STORE_UNREACHABLE
    if problem is not None:
        _misconfigured(log, problem)
        return ZohoWiring(status=problem, store=ticket_store)
    http = DeskHTTP(opener) if opener is not None else DeskHTTP()
    client = DeskClient(settings, TokenSource(settings, http), http)
    worker = ZohoWorker(ticket_store, client, conversations, media_reader, settings, log)
    # The end of the turn wakes the worker, so Zoho is first called after the reply.
    desk = DeskTicketSystem(ticket_store, settings.mode, settings.environment, wake=worker.wake)
    return ZohoWiring(
        status="live" if settings.live else "test department",
        router=TicketRouter(desk, MockTicketSystem()),
        worker=worker,
        store=ticket_store,
    )


def _misconfigured(log: Any, reason: str) -> None:
    # A reason holds setting names, never their values.
    if log is not None:
        log.emit("zoho_misconfigured", "zoho", level="error", reason=reason)


def zoho_status(wiring: ZohoWiring) -> str:
    """The /health "zoho" value: the start-up status, or what the worker has
    met since ("token refused: ...", "sending failing: ...")."""
    failing = wiring.worker.status.get("failing") if wiring.worker is not None else None
    return failing or wiring.status


def ticket_health(wiring: ZohoWiring, now: str) -> Dict[str, Any]:
    """The /health ticket fields. Shown while Zoho is on, and whenever
    records are waiting, stuck or held, so that after a rollback the
    customers who were told someone would be in touch can still be counted."""
    if wiring.store is None:
        return {}
    # With Zoho off there is no mode. Records are counted as for test, so a
    # live one shows as held: counted either way.
    mode = wiring.worker.settings.mode if wiring.worker is not None else "test"
    try:
        counts = wiring.store.counts(mode, now)
    except StoreUnavailable:
        return {"tickets": "store unavailable"}
    outstanding = sum(int(counts.get(name) or 0) for name in ("waiting", "stuck", "held"))
    if wiring.worker is None and not outstanding:
        return {}
    return {
        "tickets_waiting": counts.get("waiting", 0),
        "tickets_stuck": counts.get("stuck", 0),
        "tickets_held": counts.get("held", 0),
        "zoho_worker": dict(wiring.worker.status) if wiring.worker is not None else "off",
        "oldest_due_seconds": counts.get("oldest_due_seconds"),
    }
