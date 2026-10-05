"""Shared pieces of the Zoho scripts (spec 2026-10-05, section 10).

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

Every script asks for secrets by hidden input and keeps them in memory. None
prints a token or a secret, except exchange_code.py, whose job is to show the
refresh token once. Zoho's answers are masked (`mask`) before they are printed
or saved, so no phone, email or person's name reaches the screen or the repo.

The scripts reach Desk through the service's own client (emotorad_ai.zoho):
the same hosts, HTTP layer, token source, headers and Desk calls, and the same
look-up (`find_adoptable`) the worker runs after an unknown outcome. What a
script proves against the real Zoho is what the worker will do.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.observability import redact_pii  # noqa: E402
# TOKEN_URL is not used in this file: the scripts take it from here, so none of
# them names the address.
from emotorad_ai.zoho.auth import ACCOUNTS_URL, TOKEN_URL, TokenSource  # noqa: E402,F401
from emotorad_ai.zoho.desk import DESK_URL, DeskClient  # noqa: E402
from emotorad_ai.zoho.errors import ZohoAuthExpired, ZohoError  # noqa: E402
from emotorad_ai.zoho.http import DeskHTTP  # noqa: E402
from emotorad_ai.zoho.settings import ZohoSettings  # noqa: E402

# The India data centre only (spec section 1). Both hosts, and the token
# address, are the service's own: they are imported, never written twice. The
# two below are for the accounts calls only a person makes (consent, revoke).
AUTH_URL = ACCOUNTS_URL + "/oauth/v2/auth"
# Zoho's revoke page: a POST with the token as a form field, never in the address.
REVOKE_URL = ACCOUNTS_URL + "/oauth/v2/revoke/token"

# Every scope the chatbot needs, asked for in one grant (spec section 10). A
# second grant would mean another refresh token on the OMS's client.
# Desk.settings.READ is here in case the layout calls need it. Desk.basic.CREATE
# is not: it is only for /uploads, which this design does not use.
SCOPES = (
    "Desk.tickets.CREATE",
    "Desk.tickets.UPDATE",
    "Desk.tickets.READ",
    "Desk.search.READ",
    "Desk.contacts.READ",
    "Desk.contacts.CREATE",
    "Desk.basic.READ",
    "Desk.settings.READ",
)

# The test contact's fake number (person step 3). Never a real customer's.
TEST_PHONE = "+919999999999"

SHAPES = ROOT / "docs" / "api-shapes"

_ID = re.compile(r"[0-9]{1,30}")


# What the person is told whenever the redirect address may be the problem.
# Zoho compares it with the client's registered one exactly ("Invalid Redirect
# Uri" on the consent page), so a pasted space or a missing slash is enough.
REDIRECT_RULE = "The redirect address must be the one registered on the client, character for character."


def redirect_address(value: Optional[str]) -> Optional[str]:
    """--redirect-uri without the spaces or line end a paste brings. None
    when nothing is left."""
    text = (value or "").strip()
    return text or None


def scope_string() -> str:
    """The scopes as Zoho's consent address takes them: comma-separated."""
    return ",".join(SCOPES)


def consent_url(client_id: str, redirect_uri: str) -> str:
    """The India consent address for a server-based client (person step 4).
    access_type=offline gives a refresh token; prompt=consent makes Zoho issue
    a new one even if this user approved the client before."""
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": scope_string(),
            "redirect_uri": redirect_uri,
            "access_type": "offline",
            "prompt": "consent",
        },
        safe=",",
    )
    return AUTH_URL + "?" + query


def exchange_form(client_id: str, client_secret: str, code: str, redirect_uri: Optional[str] = None) -> Dict[str, str]:
    """The code exchange. A server-based client sends the redirect address it
    was granted with. A Self Client sends none."""
    form = {"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret, "code": code}
    if redirect_uri:
        form["redirect_uri"] = redirect_uri
    return form


def refresh_form(client_id: str, client_secret: str, refresh_token: str) -> Dict[str, str]:
    return {"grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token}


def post_form(http: Any, url: str, form: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """POST a form to the accounts server. Zoho answers an error with HTTP 200
    and an "error" field, and a refused code or token with a 400, so the
    answer is never classified (`classify=False`): callers read the body."""
    body = urllib.parse.urlencode(form).encode("utf-8")
    status, answer = http.call("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, body,
                               write=True, classify=False)
    return status, answer if isinstance(answer, dict) else {}


def says_india(answer: Dict[str, Any]) -> bool:
    """True only when Zoho's answer names the India data centre, by its
    `location` or its `api_domain`, and nothing in it names another one."""
    seen = []
    location = answer.get("location")
    if location is not None:
        seen.append(str(location).strip().lower() == "in")
    domain = answer.get("api_domain")
    if domain is not None:
        host = (urllib.parse.urlsplit(str(domain)).hostname or "").lower()
        seen.append(host == "zohoapis.in" or host.endswith(".zohoapis.in"))
    return bool(seen) and all(seen)


def granted_scopes(answer: Dict[str, Any]) -> List[str]:
    """The scopes Zoho says it granted, when its answer says. Some answers
    separate them with spaces, others with commas."""
    return [scope for scope in re.split(r"[\s,]+", str(answer.get("scope") or "")) if scope]


def revoke(http: Any, token: str) -> Tuple[bool, str]:
    """Revoke one refresh token. Returns (done, what Zoho said): "revoked", or
    the error name. Never the token."""
    try:
        status, answer = post_form(http, REVOKE_URL, {"token": token})
    except ZohoError as exc:
        return False, exc.error
    if status >= 400 or answer.get("error"):
        return False, str(answer.get("error") or "http_%d" % status)
    return True, "revoked"


def ask_secret(prompt: str, ask: Callable[[str], str] = getpass.getpass) -> str:
    """A secret by hidden input, kept in memory. An empty answer stops the
    script: Zoho would refuse it anyway, after spending a request."""
    value = (ask(prompt) or "").strip()
    if not value:
        raise SystemExit("Nothing entered for %r. Stopping." % prompt.strip().rstrip(":"))
    return value


def script_settings(*, client_id: str, client_secret: str, refresh_token: str, org_id: str,
                    department_id: str = "", contact_id: str = "", live: bool = False,
                    environment: str = "stage", layout_id: Optional[str] = None,
                    priority_high: str = "High", priority_medium: str = "Medium",
                    channel: str = "Chat") -> ZohoSettings:
    """Settings for one script run. The department and contact the person
    named stand for both the test and the live ones, so whichever the payload
    picks by the record's mode, it is the one the person chose. test_ticket.py
    checks the payload against it before sending."""
    return ZohoSettings(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token, org_id=org_id,
        test_department_id=department_id, test_contact_id=contact_id,
        department_id=department_id or None, unverified_contact_id=contact_id or None, live=live,
        environment=environment, priority_high=priority_high, priority_medium=priority_medium, channel=channel,
        credits_floor=0, attachment_limit_bytes=20 * 1024 * 1024, layout_id=layout_id or None,
    )


class _KeptAnswers:
    """The scripts' transport. It keeps, in memory, each answer Desk gives,
    by method and path, so a write's own answer can be saved as a shape: the
    Desk client hands back only the new id. Answers from the accounts server
    hold tokens and are never kept."""

    def __init__(self, http: Any) -> None:
        self.http = http
        self.answers: Dict[Tuple[str, str], Any] = {}

    def call(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None,
             **options: Any) -> Tuple[int, Any]:
        status, answer = self.http.call(method, url, headers, body, **options)
        if url.startswith(DESK_URL + "/"):
            self.answers[(method, urllib.parse.urlsplit(url).path)] = answer
        return status, answer

    def __getattr__(self, name: str) -> Any:
        return getattr(self.http, name)


class ScriptClient:
    """The worker's Desk client, plus the reads only the scripts make
    (organisations, departments, layouts, channels, one contact, one ticket)."""

    def __init__(self, settings: ZohoSettings, http: Optional[Any] = None) -> None:
        self.settings = settings
        self.http = _KeptAnswers(http if http is not None else DeskHTTP())
        self.tokens = TokenSource(settings, self.http)
        self.desk = DeskClient(settings, self.tokens, self.http)

    def answer(self, method: str, path: str) -> Any:
        """Desk's last answer to `method` on `path`, or None if it gave none."""
        return self.http.answers.get((method, path))

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, *, org: bool = True) -> Any:
        """A read, with the headers the Desk client sends. An expired access
        token is refreshed once and the read tried once more, as the client
        does (spec section 1). `org=False` leaves out the organisation id,
        which the list of organisations is asked without."""
        # "*" stays as typed: it is the search wildcard (zoho/desk.py).
        url = DESK_URL + path + ("?" + urllib.parse.urlencode(params, safe="*") if params else "")
        retried = False
        while True:
            headers = self.desk.headers()
            if not org:
                headers.pop("orgId", None)
            try:
                return self.http.call("GET", url, headers, write=False)[1]
            except ZohoAuthExpired:
                if retried:
                    raise
                retried = True
                self.tokens.invalidate()


def step(out: Callable[[str], None], label: str, read: Callable[[], Any]) -> Any:
    """Run one read. On a Zoho refusal, print its error name and carry on."""
    try:
        return read()
    except ZohoError as exc:
        out("%s: refused (error=%s)" % (label, exc.error))
        return None


def rows(answer: Any) -> List[Dict[str, Any]]:
    """The "data" list of a Desk answer. Empty for a 204 or a refusal."""
    data = answer.get("data") if isinstance(answer, dict) else None
    return [row for row in data or [] if isinstance(row, dict)]


def zoho_id(value: Any) -> Optional[str]:
    """`value` as a Zoho id (digits), or None. Nothing else goes into a path."""
    text = "" if value is None else str(value)
    return text if _ID.fullmatch(text) else None


# -- masking -----------------------------------------------------------------

# Values worth reading in a shape that name nobody: ids, the names of
# departments, layouts, fields and channels, types, flags, counts and times,
# and Zoho's own error names. Every other value becomes its type.
KEEP_VALUES = frozenset({
    "id", "ticketNumber", "departmentId", "contactId", "layoutId", "productId", "accountId",
    "status", "statusType", "priority", "channel", "classification", "category", "subCategory",
    "module", "type", "apiName", "displayLabel", "name", "layoutName", "layoutDisplayName",
    "isMandatory", "isSystemMandatory", "isCustomField", "isNested", "isEnabled", "isDefault",
    "isDefaultLayout", "isCustomSection", "isPublic", "isSandboxPortal", "maxLength", "defaultValue",
    "contentType", "size", "createdTime", "modifiedTime", "commentedTime", "companyName", "portalName",
    "edition", "count", "errorCode", "fieldName", "errorType", "api_domain", "location", "token_type",
    "expires_in", "scope", "webUrl",
})
# Free text among those, which still goes through the log's phone and email
# filter. Ids are not: a Zoho id is a long digit run the filter would hide.
_TEXT_VALUES = frozenset({"name", "displayLabel", "layoutName", "layoutDisplayName", "companyName",
                          "portalName", "defaultValue"})
# A subject the chatbot wrote (zoho/payload.py subject): our own text, with no
# phone and no name, ending with the chat reference the worker adopts by. It
# is kept, so a captured shape can be adopted from as the drafts are. Any
# other subject is someone's free text, and becomes its type.
_OUR_SUBJECT = re.compile(r"(\[Unverified\] )?\[AI chat\] ")
# Never written, whatever else is kept.
SECRET_KEYS = frozenset({"access_token", "refresh_token", "client_secret", "authorization"})
# Objects that describe a person. Inside one, only its id and type are kept.
PERSON_KEYS = frozenset({"contact", "assignee", "commenter", "author", "creator", "owner", "createdBy",
                         "modifiedBy", "account", "agent", "sharedBy", "approver"})
# A field whose label or API name says its values hold people, dealers or
# accounts (an account is a customer or a dealer). Its pick list is counted,
# never written (the 1 October probe's rule: the OMS's "Dealer Principle
# Name" list names real dealers). "Name" alone is not a person word: B4 needs
# the "Product Name" list, and "Model Name" lists bikes. A person's name is
# caught by the word beside it (first, last, full or contact name), and a
# "Mechanical issue" list is a fault list, not a mechanic's name. A list of
# staff names employees, which the org rule forbids capturing: engineers,
# executives, managers, area, regional and zonal sales managers (ASM, RSM,
# ZSM), salesmen, staff, assignees, users and technicians. The three short
# ones and "user" must stand alone, so "plasma" is not an ASM.
_PERSONAL_FIELD = re.compile(
    r"first[\s_]*name|last[\s_]*name|full[\s_]*name|contact[\s_]*name|person|dealer|principle|customer|"
    r"account|owner|agent|franchise|technician|mechanic(?!al)|rider|employee|phone|mobile|email|address|"
    r"engineer|executive|manager|sales[\s_]*m[ae]n|staff|assignee|"
    r"(?<![a-z])(?:asm|rsm|zsm)(?![a-z])|(?<![a-z])user(?:s|[\s_]*name)?(?![a-z])",
    re.IGNORECASE)


def mask(value: Any, person: bool = False) -> Any:
    """A Zoho answer with nothing personal in it. Every key stays, so the shape
    is whole, but only the values in KEEP_VALUES survive. Every other value
    becomes its type ("<str>", "<int>"). `person` treats the whole answer as
    a person (a contact read or a contact search)."""
    return _mask(value, None, person)


def _mask(value: Any, key: Optional[str], in_person: bool) -> Any:
    if isinstance(value, dict):
        people = _lists_people(value)
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() in SECRET_KEYS:
                out[k] = "[secret]"
            elif people and k == "allowedValues":
                out[k] = {"withheld": "the list may name people or dealers",
                          "count": len(v) if isinstance(v, list) else 0}
            elif people and k == "defaultValue":
                out[k] = None if v is None else "<withheld>"
            elif k == "allowedValues" and not in_person and isinstance(v, list):
                out[k] = [_allowed_value(item) for item in v]
            else:
                out[k] = _mask(v, k, in_person or k in PERSON_KEYS)
        return out
    if isinstance(value, list):
        return [_mask(item, key, in_person) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if key == "subject" and not in_person and isinstance(value, str) and _OUR_SUBJECT.match(value):
        return redact_pii(value)
    if key not in KEEP_VALUES or (in_person and key not in ("id", "type")):
        return "<%s>" % type(value).__name__
    if isinstance(value, str) and key in _TEXT_VALUES:
        return redact_pii(value)
    return value


def _lists_people(field: Dict[str, Any]) -> bool:
    if "apiName" not in field and "displayLabel" not in field:
        return False
    label = "%s %s" % (field.get("apiName") or "", field.get("displayLabel") or "")
    return bool(_PERSONAL_FIELD.search(label))


def _allowed_value(item: Any) -> Any:
    if isinstance(item, str):
        return redact_pii(item)
    if isinstance(item, dict):
        return {k: redact_pii(v) if k == "value" and isinstance(v, str) else _mask(v, k, False)
                for k, v in item.items()}
    return _mask(item, None, False)


def source_note(script: str) -> str:
    return "Captured by scripts/zoho/%s on %s, masked by scripts/zoho/_common.py. Replaces the published-shape draft." % (
        script, date.today().isoformat())


def save_shape(name: str, answer: Any, source: str, shapes_dir: Path = SHAPES, person: bool = False,
               under: Optional[str] = None) -> Path:
    """Write a masked answer to docs/api-shapes/zoho-<name>.json, with a
    "_source" note saying where and when it was captured.

    With `under`, the answer replaces that one key of the file and every other
    key stays: the probe's token answer goes under "refresh", beside the error
    bodies the suite also answers from (tests/fake_zoho.py). The note those
    keys came with is kept as "_source_of_the_rest"."""
    masked = mask(answer, person=person)
    path = Path(shapes_dir) / ("zoho-%s.json" % name)
    if under is not None:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if "_source" in doc and "_source_of_the_rest" not in doc:
            doc["_source_of_the_rest"] = doc["_source"]
        doc["_source"] = '%s Only "%s" is this capture: the other keys were already in the file (see ' \
                         '"_source_of_the_rest").' % (source, under)
        doc[under] = masked
    else:
        doc = {"_source": source}
        if isinstance(masked, dict):
            doc.update(masked)
        else:
            doc["body"] = masked
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def department_names(shapes_dir: Path = SHAPES) -> Dict[str, str]:
    """Department id to name, from the probe's masked departments shape."""
    path = Path(shapes_dir) / "zoho-departments.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {str(row.get("id")): str(row.get("name")) for row in rows(doc)}


def department_refusal(names: Dict[str, str], department_id: Any, test_department_id: Any, real: bool,
                       typed_name: Optional[str]) -> Optional[str]:
    """Why a test ticket may not go to this department, or None when it may.

    The test department is the one whose id the person gives as
    --test-department-id (EMOTORAD_ZOHO_TEST_DEPARTMENT_ID). Its name is
    never written into a script: the person types it, and it has to match the
    probe's record of that department exactly. Without `real`, only the test
    department passes. With it, any other department passes, and the test one
    never does.
    """
    department = str(department_id)
    name = names.get(department)
    if name is None:
        return "Department %s is not in docs/api-shapes/zoho-departments.json. Run probe.py first." % department
    is_test = department == str(test_department_id)
    if not real and not is_test:
        return ("Department %s is not the configured test department (%s). Test tickets go to the test "
                "department only. Nothing was sent." % (department, test_department_id))
    if real and is_test:
        return "--real-department names the test department. Leave the flag off for a test ticket. Nothing was sent."
    if (typed_name or "").strip() != name:
        return "The name typed does not match department %s. Nothing was sent." % department
    return None
