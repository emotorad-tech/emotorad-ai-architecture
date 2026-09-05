"""The identity graph, and resolution from a channel to a person (build plan §3.2).

Deterministic code, never an LLM guess. Two layers, and keeping them apart is the
whole design:

  1. The identity graph answers **who is this** — a `cluster_id` covering every
     browser, phone number and channel one person uses.
  2. The OMS warranty API answers **what do they own**. That is a *tool call*
     made mid-conversation, not part of identity resolution.

`IdentityGraph` below is an in-memory reference implementation. Production owns
this in the Nest service (`identities` / `cluster_merges` tables) — but the merge
semantics are the easiest thing in the system to get subtly wrong, and a wrong
merge is a privacy incident that cannot be unpicked. So the rules live here as
executable, tested behaviour that the Nest implementation must reproduce, and
`docs/Website_Anonymous_Identity_Approach.md` §5 is the same logic in prose.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .contract import ANONYMOUS, ASSERTED, VERIFIED, Identity, InboundMessage
from .tools import fixtures
from .tools.mocks import LOOKUP_WARRANTY_RECORD
from .tools.registry import ToolContext, ToolRegistry, is_error

ANON_ID = "anon_id"
PHONE = "phone"
EMAIL = "email"
WA_ID = "wa_id"
GA_CLIENT_ID = "ga_client_id"
USER_ID = "user_id"
EMPLOYEE_EMAIL = "employee_email"

IDENTITY_TYPES = (ANON_ID, PHONE, EMAIL, WA_ID, GA_CLIENT_ID, USER_ID, EMPLOYEE_EMAIL)

# Numbers arrive bare from web forms and fully qualified from WhatsApp. Both must
# normalise to one string or the unique constraint stops deduplicating and one
# person silently becomes two — with no error raised anywhere.
DEFAULT_COUNTRY_CODE = "+91"

_DIGITS = re.compile(r"\D")


def canonical_type(identity_type: str) -> str:
    """The type an identifier is *stored* as.

    A WhatsApp ID is a phone number — Meta hands us the subscriber's number in
    international form, not a separate handle. Storing it under its own type
    would mean `wa_id:+919876543210` and `phone:+919876543210` never collide,
    because the unique key is (type, value): the same human, the same digits,
    two clusters, and no error anywhere. Callers still pass `wa_id` because that
    is what the channel gives them; storage collapses it here.
    """
    return PHONE if identity_type == WA_ID else identity_type


def normalise(identity_type: str, value: str) -> str:
    """Canonical form for an identifier. Called inside `link`, never at call sites.

    Doing this at call sites is how one of them gets forgotten. There is no error
    when that happens — just a duplicate person nobody notices for months.
    """
    if identity_type not in IDENTITY_TYPES:
        raise ValueError("unknown identity type: %r" % (identity_type,))
    value = value.strip()
    if not value:
        raise ValueError("identity value cannot be empty")

    if identity_type in (PHONE, WA_ID):
        # WhatsApp sends digits with no "+"; forms send ten digits with no country
        # code; humans type the trunk prefix. All have to land on one E.164 string.
        digits = _DIGITS.sub("", value)
        if value.startswith("+"):
            return "+" + digits
        if digits.startswith("00"):
            # International access code — "00919876543210" is the same number as
            # "+919876543210". Left alone it produces a corrupt "+0091…" that can
            # never match, which is a duplicate person nobody notices.
            return "+" + digits[2:]
        if digits.startswith("0") and len(digits) == 11:
            # Indian national trunk prefix: "09876543210". Only stripped when what
            # remains is a full ten-digit subscriber number, so we never mangle a
            # foreign number that happens to begin with a zero.
            return DEFAULT_COUNTRY_CODE + digits[1:]
        if len(digits) == 10:
            return DEFAULT_COUNTRY_CODE + digits
        # Anything else is assumed already international. NOTE: a *national-format*
        # foreign number (a German "0151…") genuinely cannot be normalised without
        # knowing the country, and would be wrong here. Every channel we accept
        # supplies E.164 — WhatsApp guarantees it — so this is reachable only from
        # hand-entered data. See the edge case register.
        return "+" + digits
    if identity_type in (EMAIL, EMPLOYEE_EMAIL):
        return value.lower()
    return value


@dataclass
class _Row:
    """One row of the `identities` table."""

    cluster_id: str
    identity_type: str
    identity_value: str
    verified: bool
    seq: int  # stands in for first_seen_at; monotonic and deterministic in tests


@dataclass(frozen=True)
class Merge:
    """One row of `cluster_merges`. Never deleted — it is how merges are debugged
    and how an erasure request finds every cluster a person ever had."""

    from_cluster: str
    to_cluster: str
    reason: str


class IdentityGraph:
    """Identifiers in, one `cluster_id` per person out."""

    def __init__(self, new_id: Optional[Callable[[], str]] = None) -> None:
        self._rows: Dict[Tuple[str, str], _Row] = {}
        self._merges: List[Merge] = []
        self._counter = itertools.count()
        self._new_id = new_id or (lambda: str(uuid.uuid4()))

    # -- reads ---------------------------------------------------------------

    def resolve(self, identity_type: str, identity_value: str) -> Optional[str]:
        """The cluster this identifier belongs to, or None if never seen."""
        row = self._rows.get(self._key(identity_type, identity_value))
        return row.cluster_id if row else None

    @staticmethod
    def _key(identity_type: str, identity_value: str) -> Tuple[str, str]:
        return canonical_type(identity_type), normalise(identity_type, identity_value)

    def identifiers(self, cluster_id: str) -> List[Dict[str, Any]]:
        return [
            {"type": row.identity_type, "value": row.identity_value, "verified": row.verified}
            for row in sorted(self._rows.values(), key=lambda r: r.seq)
            if row.cluster_id == cluster_id
        ]

    def verified_phone(self, cluster_id: str) -> Optional[str]:
        """The proven phone for this person, or None. Drives what may be disclosed."""
        for row in sorted(self._rows.values(), key=lambda r: r.seq):
            if row.cluster_id == cluster_id and row.identity_type == PHONE and row.verified:
                return row.identity_value
        return None

    @property
    def merges(self) -> List[Merge]:
        return list(self._merges)

    # -- writes --------------------------------------------------------------

    def cluster_for(self, em_aid: str) -> str:
        """Cluster for a browser, creating one on first sight.

        The real implementation needs `on conflict do nothing` plus a re-select
        here: two concurrent first requests from the same new browser would
        otherwise create two clusters, or crash on the unique constraint.
        """
        existing = self.resolve(ANON_ID, em_aid)
        if existing:
            return existing
        cluster_id = self._new_id()
        self._insert(cluster_id, ANON_ID, em_aid, verified=False)
        return cluster_id

    def link(
        self,
        em_aid: Optional[str],
        identity_type: str,
        identity_value: str,
        verified: bool,
    ) -> str:
        """Attach an identifier to a person, merging two clusters if that is what
        it proves. Returns the cluster the identifier now belongs to.

        `em_aid` may be None — a WhatsApp message or IVR call with no ref code has
        no browser behind it, so the identifier stands alone.
        """
        key = self._key(identity_type, identity_value)          # always first
        identity_type, identity_value = key
        existing = self._rows.get(key)

        if em_aid is None:
            if existing:
                return existing.cluster_id
            cluster_id = self._new_id()
            self._insert(cluster_id, identity_type, identity_value, verified)
            return cluster_id

        my_cluster = self.cluster_for(em_aid)

        # 1. Never seen this identifier — attach it to this browser's person.
        if existing is None:
            self._insert(my_cluster, identity_type, identity_value, verified)
            return my_cluster

        # 2. Known, and already the same person. Cheap path.
        if existing.cluster_id == my_cluster:
            existing.verified = existing.verified or verified
            return my_cluster

        # 3. Known, but on a different cluster — two clusters are one human.
        if verified:
            return self._merge(existing.cluster_id, my_cluster, "%s verified" % identity_type)

        # An unverified identifier claiming an existing person proves nothing.
        # This is the typo'd-email protection: merging here would fuse two
        # customers permanently, and no later evidence can separate them again.
        return my_cluster

    # -- internals -----------------------------------------------------------

    def _insert(self, cluster_id: str, identity_type: str, identity_value: str, verified: bool) -> None:
        self._rows[(identity_type, identity_value)] = _Row(
            cluster_id=cluster_id,
            identity_type=identity_type,
            identity_value=identity_value,
            verified=verified,
            seq=next(self._counter),
        )

    def _merge(self, cluster_a: str, cluster_b: str, reason: str) -> str:
        survivor, loser = self._older_of(cluster_a, cluster_b)
        for row in self._rows.values():
            if row.cluster_id == loser:
                row.cluster_id = survivor
        self._merges.append(Merge(from_cluster=loser, to_cluster=survivor, reason=reason))
        return survivor

    def _older_of(self, cluster_a: str, cluster_b: str) -> Tuple[str, str]:
        """(survivor, loser). The older cluster wins, so that replaying the same
        events always produces the same result — rather than depending on which
        request happened to arrive first."""
        born = {
            cluster: min(r.seq for r in self._rows.values() if r.cluster_id == cluster)
            for cluster in (cluster_a, cluster_b)
        }
        if born[cluster_a] <= born[cluster_b]:
            return cluster_a, cluster_b
        return cluster_b, cluster_a


@dataclass(frozen=True)
class ResolvedIdentity:
    """The identity layer's answer, before any ownership lookup."""

    persona: str
    method: str
    identity: Identity = field(default_factory=Identity)
    profile: Optional[Dict[str, Any]] = None
    # Every bike on this person's number, each with coverage already computed.
    # A list because multi-bike ownership is confirmed in production, so the
    # single-bike case is just a list of one — never a special shape.
    bikes: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def single_bike(self) -> Optional[Dict[str, Any]]:
        """The bike, when there is exactly one. None when there are none or
        several — because with several, the agent must ask which."""
        return self.bikes[0] if len(self.bikes) == 1 else None

    @property
    def cluster_id(self) -> Optional[str]:
        return self.identity.cluster_id

    @property
    def customer_id(self) -> Optional[str]:
        return self.identity.customer_id

    @property
    def may_disclose(self) -> bool:
        return self.identity.may_disclose

    @property
    def is_known_customer(self) -> bool:
        """A customer we can speak to personally: verified *and* owning a bike we
        can name. Someone verified with no warranty record is a real customer too,
        but there is nothing to state about their bike, so the agent stays generic
        and hands to Late Warranty Registration."""
        return self.persona == "customer" and bool(self.bikes)


class SessionDirectory:
    """Session token -> verified phone number.

    Stands in for the website/Amiigo session store. Replace with a call to the
    real session service; the interface is the one method.
    """

    def __init__(self, sessions: Optional[Mapping[str, str]] = None) -> None:
        self._sessions = dict(sessions if sessions is not None else fixtures.SESSIONS)

    def phone_for_session(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        return self._sessions.get(token)


class IdentityResolver:
    """Channel-specific facts in, a persona and a cluster out."""

    def __init__(
        self,
        registry: ToolRegistry,
        graph: Optional[IdentityGraph] = None,
        sessions: Optional[SessionDirectory] = None,
    ) -> None:
        self._registry = registry
        self._graph = graph or IdentityGraph()
        self._sessions = sessions or SessionDirectory()

    @property
    def graph(self) -> IdentityGraph:
        return self._graph

    def resolve_website(self, em_aid: Optional[str], session_token: Optional[str]) -> Tuple[str, Identity]:
        """Website chat: a cookie always, a logged-in session sometimes.

        An anonymous visitor is a *valid* identity, not a failure — they get a
        cluster and generic help. What they do not get is anything personal.
        """
        phone = self._sessions.phone_for_session(session_token)
        if phone:
            cluster_id = self._graph.link(em_aid, PHONE, phone, verified=True)
            return "customer", Identity(
                cluster_id=cluster_id,
                strength=VERIFIED,
                em_aid=em_aid,
                phone=normalise(PHONE, phone),
                channel_user_id=session_token,
            )

        if em_aid:
            return "customer", Identity(
                cluster_id=self._graph.cluster_for(em_aid),
                strength=ANONYMOUS,
                em_aid=em_aid,
                channel_user_id=session_token,
            )
        return "unknown", Identity(strength=ANONYMOUS, channel_user_id=session_token)

    def resolve_whatsapp(self, sender: str, ref_code_em_aid: Optional[str] = None) -> Tuple[str, Identity]:
        """WhatsApp hands us the sender's phone natively, so it is verified.

        `ref_code_em_aid` is the browser resolved from a `ref:` code in the first
        message — the stitch that carries a whole browsing history into the
        conversation. Absent when they messaged us directly.
        """
        cluster_id = self._graph.link(ref_code_em_aid, WA_ID, sender, verified=True)
        return "customer", Identity(
            cluster_id=cluster_id,
            strength=VERIFIED,
            em_aid=ref_code_em_aid,
            phone=normalise(WA_ID, sender),
            channel_user_id=sender,
        )

    def resolve_voice(self, caller_id: Optional[str]) -> Tuple[str, Identity]:
        """IVR. Caller ID is spoofable, so it resolves a cluster but never
        authorises disclosure — the agent must verify before saying anything
        personal, and `strength` is what stops it."""
        if not caller_id:
            return "unknown", Identity(strength=ANONYMOUS)
        cluster_id = self._graph.link(None, PHONE, caller_id, verified=False)
        return "customer", Identity(
            cluster_id=cluster_id,
            strength=ASSERTED,
            phone=normalise(PHONE, caller_id),
            channel_user_id=caller_id,
        )

    def resolve_dealer(self, sender: str) -> Tuple[str, Identity]:
        """Dealer WhatsApp. Phone plus dealer ID, resolved against the dealer table.

        **Deliberately a separate lookup from the customer path.** Dealers perform
        most warranty registrations and routinely enter their own number, so the
        same phone can appear across dozens of customers' warranty rows. Resolving
        a dealer through `resolve_whatsapp` would hand them every one of those
        bikes as though they owned them — a data breach dressed up as a feature.

        A number that is not a known dealer is *not* silently downgraded to a
        customer either: the dealer number is a separate WhatsApp line, so an
        unknown sender there is an error worth surfacing, not a shopper.
        """
        phone = normalise(PHONE, sender)
        dealer = fixtures.DEALERS.get(phone)
        if dealer is None:
            return "unknown", Identity(strength=ASSERTED, phone=phone, channel_user_id=sender)

        cluster_id = self._graph.link(None, PHONE, phone, verified=True)
        return "dealer", Identity(
            cluster_id=cluster_id,
            strength=VERIFIED,
            phone=phone,
            dealer_id=dealer["dealer_id"],
            channel_user_id=sender,
        )

    def resolve_internal(self, employee_email: str) -> Tuple[str, Identity]:
        """Google Workspace SSO. The employee is the *actor*; whichever customer
        they ask about is the subject, carried separately on the message."""
        cluster_id = self._graph.link(None, EMPLOYEE_EMAIL, employee_email, verified=True)
        return "internal", Identity(
            cluster_id=cluster_id,
            strength=VERIFIED,
            employee_email=normalise(EMPLOYEE_EMAIL, employee_email),
            channel_user_id=employee_email,
        )

    def hydrate(self, message: InboundMessage) -> ResolvedIdentity:
        """Attach the OMS profile for whoever this turn is *about*.

        Reads `message.about`, not `message.identity`: for an internal user those
        differ, and scoping to the actor would hand an employee their own record
        instead of the customer's.
        """
        identity = message.about

        if message.persona == "dealer":
            # Dealers get their own hydration path and never touch the customer
            # warranty lookup. Two personas sharing one enrichment function is
            # exactly how a dealer ends up holding customer records.
            dealer = fixtures.DEALERS.get(identity.phone or "")
            if dealer is None:
                return ResolvedIdentity(
                    persona="unknown", method="unknown_dealer", identity=identity
                )
            return ResolvedIdentity(
                persona="dealer", method="verified", identity=identity, profile=dict(dealer)
            )

        if not identity.may_disclose:
            # An unverified person still gets a conversation — just not a personal
            # one. No OMS call is made at all, so there is nothing to leak.
            return ResolvedIdentity(
                persona=message.persona, method="unverified", identity=identity
            )

        envelope = self._registry.call(
            LOOKUP_WARRANTY_RECORD,
            {},
            ToolContext(
                conversation_id=message.conversation_id,
                phone=identity.phone,
                cluster_id=identity.cluster_id,
            ),
        )

        if is_error(envelope):
            code = envelope["error"]["code"]
            # "No record" is not an error state for the person — registration is
            # routinely skipped, and a real owner is standing in front of us. It
            # routes to Late Warranty Registration. An OMS outage is a different
            # thing entirely and must not produce the same reply.
            method = "no_warranty_record" if code == "no_warranty_record" else "oms_error"
            return ResolvedIdentity(
                persona=message.persona, method=method, identity=identity, error=code
            )

        data = envelope["data"]
        return ResolvedIdentity(
            persona=message.persona,
            method="verified",
            identity=identity,
            profile={"name": data.get("customer_name")},
            bikes=data["bikes"],
        )


class StaticResolver:
    """Returns one pre-built ResolvedIdentity for every message.

    For the playground: the rider is chosen in the sidebar, not resolved from a
    session token, so hydration is a lookup of what was chosen. Presets still go
    through the real IdentityResolver *once* to build that value, so they stay
    honest as the mocks evolve; this only pins the answer for the session.
    """

    def __init__(self, resolved: ResolvedIdentity) -> None:
        self._resolved = resolved

    def hydrate(self, message: InboundMessage) -> ResolvedIdentity:
        return self._resolved


def replace_customer_id(identity: Identity, customer_id: str) -> Identity:
    """Attach the OMS record key, preserving everything else.

    `dataclasses.replace` rather than rebuilding field by field: a hand-written
    copy silently drops any field added to `Identity` later, and the field most
    likely to be added is another one that gates disclosure.
    """
    return dataclasses.replace(identity, customer_id=customer_id)
