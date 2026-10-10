"""Zoho Desk settings and the start-up checks (spec 2026-10-05, section 1).

Zoho is off unless EMOTORAD_ZOHO_REFRESH_TOKEN is set. When it is set, every
name the mode needs must be present and every start-up check must pass, or
the mock is used exactly as when Zoho is off: there is no state in which
tickets are recorded but cannot be sent safely. zoho/wiring.py logs
`zoho_misconfigured`, and /health shows the reason.

The client id, the client secret and the refresh token are credentials. They
are kept out of repr, and no string built here ever holds a value, only
names.

There are no custom field settings: the Desk's text custom fields are at
their limit, so the chat reference rides in the ticket's subject instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

REFRESH_TOKEN = "EMOTORAD_ZOHO_REFRESH_TOKEN"
CLIENT_ID = "EMOTORAD_ZOHO_CLIENT_ID"
CLIENT_SECRET = "EMOTORAD_ZOHO_CLIENT_SECRET"
ORG_ID = "EMOTORAD_ZOHO_ORG_ID"
TEST_DEPARTMENT_ID = "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID"
TEST_CONTACT_ID = "EMOTORAD_ZOHO_TEST_CONTACT_ID"
DEPARTMENT_ID = "EMOTORAD_ZOHO_DEPARTMENT_ID"
UNVERIFIED_CONTACT_ID = "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID"
LIVE = "EMOTORAD_ZOHO_LIVE"
# The ticket layout, when the department has more than one. Not secret.
LAYOUT_ID = "EMOTORAD_ZOHO_LAYOUT_ID"
PRIORITY_HIGH = "EMOTORAD_ZOHO_PRIORITY_HIGH"
PRIORITY_MEDIUM = "EMOTORAD_ZOHO_PRIORITY_MEDIUM"
CHANNEL = "EMOTORAD_ZOHO_CHANNEL"
CREDITS_FLOOR = "EMOTORAD_ZOHO_CREDITS_FLOOR"
ATTACHMENT_LIMIT_MB = "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB"
# The Zoho Desk webhook's secret, the last segment of the path Zoho Desk calls
# when support closes a ticket (amiigo/webhooks.py). Secret. Not needed for
# tickets to be sent: without it the webhook answers 503.
WEBHOOK_SECRET = "EMOTORAD_ZOHO_WEBHOOK_SECRET"
# Set by the deploy (deploy-staging.yml). It prefixes every chat reference, so
# two deployments sharing one Zoho organisation never adopt each other's
# tickets. Not a Zoho name, so it is not in ENV_NAMES.
AI_ENV = "EMOTORAD_AI_ENV"

# Needed in both modes, in the order a missing list names them.
ALWAYS = (CLIENT_ID, CLIENT_SECRET, ORG_ID, TEST_DEPARTMENT_ID, TEST_CONTACT_ID, AI_ENV)
# Needed only to send to the real department.
LIVE_ONLY = (DEPARTMENT_ID, UNVERIFIED_CONTACT_ID)
# Every EMOTORAD_ZOHO_* name the code reads, for the places that blank or
# withhold them all (the health test, scripts/chat_local.py).
ENV_NAMES = (
    REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET, ORG_ID, TEST_DEPARTMENT_ID, TEST_CONTACT_ID,
    DEPARTMENT_ID, UNVERIFIED_CONTACT_ID, LIVE, LAYOUT_ID,
    PRIORITY_HIGH, PRIORITY_MEDIUM, CHANNEL, CREDITS_FLOOR, ATTACHMENT_LIMIT_MB, WEBHOOK_SECRET,
)

# The values only part 1's probe can give. These defaults hold until it runs.
DEFAULT_PRIORITY_HIGH = "High"
DEFAULT_PRIORITY_MEDIUM = "Medium"
DEFAULT_CHANNEL = "Chat"
DEFAULT_CREDITS_FLOOR = 1000
DEFAULT_ATTACHMENT_LIMIT_MB = 20

# What load_zoho_settings and startup_problem answer, in /health's words.
NOT_CONFIGURED = "not configured"
OK = "ok"
NOT_ALLOWED_IN_REGION = "not allowed in this region"
STORE_NOT_MONGODB = "misconfigured: store is not mongodb"
INDEX_MISSING = "misconfigured: tickets index missing"
# Not returned here: startup_problem lets StoreUnavailable propagate, and
# build_zoho (zoho/wiring.py) maps it to this.
STORE_UNREACHABLE = "misconfigured: store unreachable"
LIVE_REFUSED = "misconfigured: live refused: test verification in use"

# ASCII digits only: int() would also take "२०", "1_000" and "+5".
_WHOLE = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True)
class ZohoSettings:
    """What the client and the worker need. Built by load_zoho_settings."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    org_id: str
    test_department_id: str
    test_contact_id: str
    department_id: Optional[str]
    unverified_contact_id: Optional[str]
    live: bool
    environment: str
    priority_high: str
    priority_medium: str
    channel: str
    credits_floor: int
    attachment_limit_bytes: int
    layout_id: Optional[str] = None

    @property
    def mode(self) -> str:
        """The stamp on every record made under these settings."""
        return "live" if self.live else "test"

    @property
    def active_department_id(self) -> str:
        # Live always has the real department: load_zoho_settings refuses it
        # otherwise. Anything short of that is the test department.
        if self.live and self.department_id:
            return self.department_id
        return self.test_department_id


def _value(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _whole(env: Mapping[str, str], name: str, default: int, least: int) -> Optional[int]:
    """A whole-number setting, or its default when unset. None when it is set
    to anything else: a typo must not quietly become the default."""
    text = _value(env, name)
    if not text:
        return default
    if not _WHOLE.fullmatch(text):
        return None
    number = int(text)
    return number if number >= least else None


def load_zoho_settings(env: Mapping[str, str]) -> Tuple[Optional[ZohoSettings], str]:
    """(settings, "ok"), or (None, why not) in the words /health uses."""
    refresh_token = _value(env, REFRESH_TOKEN)
    if not refresh_token:
        return None, NOT_CONFIGURED
    # Exactly "yes", as typed. "Yes", "true" or "1" means the test department:
    # the safe way to read a mistake, and /health shows which one it is.
    live = env.get(LIVE) == "yes"
    needed = ALWAYS + (LIVE_ONLY if live else ())
    missing = [name for name in needed if not _value(env, name)]
    if missing:
        return None, "misconfigured: missing %s" % ", ".join(missing)
    floor = _whole(env, CREDITS_FLOOR, DEFAULT_CREDITS_FLOOR, least=0)
    limit_mb = _whole(env, ATTACHMENT_LIMIT_MB, DEFAULT_ATTACHMENT_LIMIT_MB, least=1)
    bad = [name for name, number in ((CREDITS_FLOOR, floor), (ATTACHMENT_LIMIT_MB, limit_mb)) if number is None]
    if bad:
        return None, "misconfigured: bad number: %s" % ", ".join(bad)
    settings = ZohoSettings(
        client_id=_value(env, CLIENT_ID),
        client_secret=_value(env, CLIENT_SECRET),
        refresh_token=refresh_token,
        org_id=_value(env, ORG_ID),
        test_department_id=_value(env, TEST_DEPARTMENT_ID),
        test_contact_id=_value(env, TEST_CONTACT_ID),
        department_id=_value(env, DEPARTMENT_ID) or None,
        unverified_contact_id=_value(env, UNVERIFIED_CONTACT_ID) or None,
        live=live,
        environment=_value(env, AI_ENV),
        priority_high=_value(env, PRIORITY_HIGH) or DEFAULT_PRIORITY_HIGH,
        priority_medium=_value(env, PRIORITY_MEDIUM) or DEFAULT_PRIORITY_MEDIUM,
        channel=_value(env, CHANNEL) or DEFAULT_CHANNEL,
        credits_floor=floor,
        attachment_limit_bytes=limit_mb * 1024 * 1024,
        layout_id=_value(env, LAYOUT_ID) or None,
    )
    return settings, OK


def startup_problem(settings: ZohoSettings, *, region: str, store_kind: str, ticket_store: Any,
                    dev_codes: bool, otp_is_mock: bool) -> Optional[str]:
    """The first start-up check that fails, in /health's words, or None.

    These run in the spec's order, after load_zoho_settings has checked the
    names: the region, then the store and its unique index, then real
    verification for live mode. Any answer other than None means the mock is
    used and nothing is recorded in `tickets`.

    A store that cannot be reached raises StoreUnavailable from the index
    read. It is not caught here, so a database that is down is not reported
    as a missing index: the caller answers STORE_UNREACHABLE.
    """
    # EU data stays in eu-central-1 (CLAUDE.md; Risk Register sections 16-20),
    # and there is no EU Zoho organisation.
    if (region or "").strip().lower().startswith("eu-"):
        return NOT_ALLOWED_IN_REGION
    if store_kind != "mongodb" or ticket_store is None:
        # A record must survive a restart to be sent after the reply. A
        # Stores built without tickets has nowhere to keep one.
        return STORE_NOT_MONGODB
    if not ticket_store.has_unique_source_key():
        return INDEX_MISSING
    if settings.live and (dev_codes or otp_is_mock):
        # Live tickets carry real customers' numbers. Dev codes or the mock
        # OTP sender would let anyone pass as any number.
        return LIVE_REFUSED
    return None
