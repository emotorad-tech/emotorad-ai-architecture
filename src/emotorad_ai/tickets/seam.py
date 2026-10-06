"""The ticket seam (spec 2026-10-05, section 2): where a ticket tool or a
runtime gate records a ticket.

`DeskTicketSystem` writes the ticket record (record.py) to the ticket store
and never calls Zoho: the worker (zoho/worker.py) sends it after the reply.
`TicketRouter` is what the registry holds when Zoho is on. A new ticket goes
to Desk for the customer persona, and to the mock for anyone else or a caller
that names nobody, so a dealer's report never becomes a customer ticket.
Every later call goes by the id, to the system that issued it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ..evidence_check import clean_sentence
from ..observability import redact_pii
from .clock import now_iso
from .kinds import KINDS, is_desk_reference
from .record import MODES, new_record

if TYPE_CHECKING:
    from ..tools.mocks import MockTicketSystem

# What the customer told us, kept on the record as their claims and never as
# facts. The intake and warranty-proof tools pass these.
CLAIM_FIELDS = ("stated_name", "stated_contact", "evidence", "claimed_purchase_date", "purchase_channel")


class DeskTicketSystem:
    """Records customer tickets for Zoho Desk. Nothing here calls Zoho."""

    def __init__(
        self,
        store: Any,
        mode: str,
        environment: str,
        clock: Callable[[], str] = now_iso,
        wake: Callable[[], None] = lambda: None,
    ) -> None:
        if mode not in MODES:
            raise ValueError("unknown mode: %r" % (mode,))
        if not environment:
            raise ValueError("the environment names the chat reference and is required")
        self.store = store
        # Stamped on every record: the worker sends only its own mode's.
        self.mode = mode
        self.environment = environment
        self._clock = clock
        # Tells the worker there is work now, so it need not wait for its pass.
        self._wake = wake

    def create(self, source_key: Optional[str] = None, persona: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """One record per source key: the same key always returns the same ticket.

        Checked before a reference is taken, so a refusal spends no number.
        Not woken here: the turn's end does that (attach_transcript), so Zoho
        is first called after the reply, or two minutes on if the turn died.
        """
        if persona != "customer":
            raise ValueError("Zoho Desk records customer tickets only, not %r" % (persona,))
        if not source_key:
            raise ValueError("a Desk ticket needs a source_key")
        if fields.get("kind") not in KINDS:
            raise ValueError("unknown ticket kind: %r" % (fields.get("kind"),))
        if not fields.get("conversation_id"):
            raise ValueError("a Desk ticket needs a conversation_id")
        if not fields.get("started_at"):
            # The worker posts a run's own turns by this time. Without it the
            # record would take in every earlier run and person of the conversation.
            raise ValueError("a Desk ticket needs started_at")
        record = self.store.by_source_key(source_key)
        if record is None:
            record = self.store.insert(self._new_record(source_key, fields))
        return {"ticket_id": record["_id"], "status": "open"}

    def _new_record(self, source_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        identity = "verified" if fields.get("identity") == "verified" else "unverified"
        bike = None
        if identity == "verified":
            # A bike only for a proved number. A typed one could name a
            # stranger's bike, because the look-up would find its owner's.
            bike = {"model": fields.get("bike_model"), "frame_number": fields.get("frame_number"),
                    "frame_number_source": fields.get("frame_number_source")}
            if not any(bike.values()):
                bike = None
        reference = self.store.next_reference()
        return new_record(
            reference=reference,
            # Unique across deployments that share the Zoho organisation.
            chat_reference="%s:%s" % (self.environment, reference),
            source_key=source_key,
            mode=self.mode,
            kind=fields["kind"],
            conversation_id=fields["conversation_id"],
            started_at=fields["started_at"],
            cluster_id=fields.get("cluster_id"),
            channel=fields.get("channel"),
            phone=fields.get("phone"),
            identity=identity,
            category=fields.get("category"),
            ai_severity=fields.get("severity"),
            # From the customer's words: a number or an email typed into it
            # does not go to a third party.
            summary=redact_pii(fields.get("description") or ""),
            claims={name: fields[name] for name in CLAIM_FIELDS if fields.get(name) not in (None, "")},
            bike=bike,
            coverage=fields.get("coverage"),
            customer_name=fields.get("customer_name"),
            created_at=self._clock(),
            # What Gemini saw in media that passed the evidence check, set by
            # code only (evidence_check.py); cleaned again before it is kept.
            evidence_check=clean_sentence(fields.get("evidence_check")) or None,
        )

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        """Marks the record as having new content. The text is not used: the
        worker reads this run's turns from the conversation store itself, so
        an earlier person's run never reaches this ticket."""
        if self.store.wake(ticket_id, self._clock()):
            self._wake()
            return
        self._known(ticket_id)

    def add_note(self, ticket_id: str, text: str) -> None:
        """A short line for the person working the ticket."""
        if self.store.add_note(ticket_id, text, self._clock()):
            self._wake()
            return
        self._known(ticket_id)

    def close_runs(self, conversation_id: str, new_started_at: str) -> None:
        """Marks where the conversation's earlier runs ended, so the worker
        posts only each run's own turns and media."""
        self.store.close_runs(conversation_id, new_started_at)

    def _known(self, ticket_id: str) -> None:
        # Not woken. A gone record (deleted or merged in Desk) takes no more
        # work, which is not a failure. An id nobody issued is one.
        if self.store.get(ticket_id) is None:
            raise KeyError("no ticket %s" % ticket_id)


class TicketRouter:
    """What the registry holds when Zoho is on."""

    # The registry's ticket system now records tickets a person will work.
    records_real_tickets = True

    def __init__(self, desk: DeskTicketSystem, mock: "MockTicketSystem") -> None:
        self.desk = desk
        self.mock = mock

    @property
    def tickets(self) -> Dict[str, Dict[str, Any]]:
        """The mock's tickets, so code and tests that read
        registry.tickets.tickets keep working."""
        return self.mock.tickets

    @property
    def store(self) -> Any:
        return self.desk.store

    def create(self, source_key: Optional[str] = None, persona: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """Desk for a customer. The mock for anyone else, or for a call that
        names nobody, so a ticket of unknown origin never becomes a customer's."""
        system: Any = self.desk if persona == "customer" else self.mock
        return system.create(source_key=source_key, persona=persona, **fields)

    def attach_transcript(self, ticket_id: str, transcript: str) -> None:
        self._issuer(ticket_id).attach_transcript(ticket_id, transcript)

    def add_note(self, ticket_id: str, text: str) -> None:
        self._issuer(ticket_id).add_note(ticket_id, text)

    def close_runs(self, conversation_id: str, new_started_at: str) -> None:
        self.desk.close_runs(conversation_id, new_started_at)
        self.mock.close_runs(conversation_id, new_started_at)

    def _issuer(self, ticket_id: str) -> Any:
        # Seven digits (EM-1000001 up) are Desk's; five are the mock's.
        return self.desk if is_desk_reference(ticket_id) else self.mock
