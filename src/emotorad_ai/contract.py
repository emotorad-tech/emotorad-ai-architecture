"""The internal message contract (build plan §3.1).

Every channel adapter emits exactly this shape. Everything downstream — router,
sub-agents, tool registry, observability — depends on it being stable, so treat
changes here as breaking changes for every adapter and agent at once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PERSONAS = ("customer", "dealer", "internal", "unknown")

# How well we know who we are talking to. This gates disclosure, so it is a
# first-class field rather than a boolean: "we have a phone number" and "we have
# a *proven* phone number" authorise completely different replies.
VERIFIED = "verified"    # OTP, WhatsApp-native sender, Google SSO
ASSERTED = "asserted"    # caller ID on an IVR call — trivially spoofable
ANONYMOUS = "anonymous"  # a cookie, and nothing else
STRENGTHS = (VERIFIED, ASSERTED, ANONYMOUS)

# Only the channels use case #1 exercises are listed as "built". The rest are
# declared so the contract does not change when their adapters land.
CHANNELS = (
    "website_chat",
    "amiigo_app",
    "whatsapp",
    "voice",
    "dealer_app",
    "internal_portal",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Identity:
    """Who the message is from, as resolved by deterministic code — never by the model.

    ``cluster_id`` is the primary key of the whole system: it identifies a *person*
    across every browser, phone number and channel they use. ``em_aid`` identifies
    only a *browser*, which is why it can never authorise disclosure on its own —
    a shared family laptop is one cookie and several people.
    """

    cluster_id: Optional[str] = None
    strength: str = ANONYMOUS
    em_aid: Optional[str] = None
    phone: Optional[str] = None
    channel_user_id: Optional[str] = None
    dealer_id: Optional[str] = None
    employee_email: Optional[str] = None
    # The OMS/warranty record for this person, once looked up by phone. Absent
    # until that call happens, and absent forever for someone who never registered.
    customer_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.strength not in STRENGTHS:
            raise ValueError("unknown identity strength: %r" % (self.strength,))

    @property
    def may_disclose(self) -> bool:
        """Whether the agent may state personal facts — name, bikes, coverage.

        The disclosure gate lives here, in code, so that no prompt wording and no
        model decision can widen it. Anything short of `verified` gets generic help.
        """
        return self.strength == VERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "strength": self.strength,
            "em_aid": self.em_aid,
            "phone": self.phone,
            "channel_user_id": self.channel_user_id,
            "dealer_id": self.dealer_id,
            "employee_email": self.employee_email,
            "customer_id": self.customer_id,
        }


@dataclass(frozen=True)
class Attachment:
    kind: str  # "image" | "document"
    url: str
    mime_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "mime_type": self.mime_type}


@dataclass(frozen=True)
class InboundMessage:
    """One inbound turn, normalised out of whatever the channel sent."""

    conversation_id: str
    persona: str
    identity: Identity
    channel: str
    message_text: str
    # Who the conversation is *about*, when that is not who is typing. An employee
    # asking about a customer is the actor; the customer is the subject. Collapsing
    # the two loses either the audit trail (who asked) or the data scoping (whose
    # records may be read). Always None outside the internal persona.
    subject: Optional[Identity] = None
    entry_metadata: Dict[str, Any] = field(default_factory=dict)
    attachments: List[Attachment] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.persona not in PERSONAS:
            raise ValueError("unknown persona: %r" % (self.persona,))
        if self.channel not in CHANNELS:
            raise ValueError("unknown channel: %r" % (self.channel,))
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        if self.subject is not None and self.persona != "internal":
            raise ValueError(
                "subject is only meaningful for the internal persona; %r speaks for itself"
                % (self.persona,)
            )

    @property
    def about(self) -> Identity:
        """The identity whose data this turn may read. Actor and subject collapse
        into one for customers and dealers, who only ever ask about themselves."""
        return self.subject if self.subject is not None else self.identity

    @property
    def pill_clicked(self) -> Optional[str]:
        """Entry-point intent, when the channel already told us (metadata over inference)."""
        value = self.entry_metadata.get("pill_clicked")
        return value if isinstance(value, str) else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "persona": self.persona,
            "identity": self.identity.to_dict(),
            "subject": self.subject.to_dict() if self.subject is not None else None,
            "channel": self.channel,
            "entry_metadata": dict(self.entry_metadata),
            "message": {
                "text": self.message_text,
                "attachments": [a.to_dict() for a in self.attachments],
            },
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class Reply:
    """What the platform sends back for one inbound turn."""

    conversation_id: str
    text: str
    handled_by: str  # agent name, or the guardrail that short-circuited the turn
    escalated: bool = False
    ticket_id: Optional[str] = None
    # Outbound media — the workflow diagrams and clips attached to knowledge
    # records. Separate from the inbound attachments on InboundMessage: showing
    # someone where the charger seats beats describing it, and without this field
    # the media authored into the knowledge base has nowhere to go.
    attachments: List[Attachment] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "text": self.text,
            "handled_by": self.handled_by,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
            "attachments": [a.to_dict() for a in self.attachments],
            "metadata": dict(self.metadata),
        }


def new_conversation_id() -> str:
    return str(uuid.uuid4())
