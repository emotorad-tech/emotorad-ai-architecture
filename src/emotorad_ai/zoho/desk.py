"""The Zoho Desk calls the worker makes, and nothing else (spec 2026-10-05,
sections 4 and 5).

These go to the India data centre only. Every call carries the access token
and the organisation id. An expired token (401 INVALID_OAUTH) is refreshed
once and the same call made once more. A 401 means nothing was done, so
that is safe even for a write.

Any id that goes into a path is checked first to be a Zoho id (digits), so
nothing read from a record or a setting can change which URL is called.
"""

from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..tickets.clock import parse
from .auth import TokenSource
from .errors import ZohoAuthExpired, ZohoConfigError, ZohoRejected, ZohoUnavailable, ZohoUnknownOutcome
from .http import DeskHTTP
from .settings import ZohoSettings

# The India data centre. The person's scripts import this too.
DESK_URL = "https://desk.zoho.in"
UPLOAD_TIMEOUT = 60.0
# Zoho's limit on a comment (https://desk.zoho.com/DeskAPIDocument#TicketsComments).
# Transcript chunks stay under 30,000 (zoho/payload.py).
COMMENT_LIMIT = 32000
# Zoho's limit on an attachment's name (v1.0/TicketAttachment.json).
NAME_LIMIT = 100
PAGE = 100
MAX_PAGES = 20
SEARCH_FIELDS = ("phone", "mobile")
# Our clock and Zoho's may differ by a little, so a ticket counts as made
# before a record only when it is older by more than this.
ADOPT_SLACK_SECONDS = 300

_ID = re.compile(r"[0-9]{1,30}")
_TEN_DIGITS = re.compile(r"[0-9]{10}")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MIME = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+")


def _path_id(value: Any) -> str:
    text = "" if value is None else str(value)
    if not _ID.fullmatch(text):
        raise ZohoConfigError("an id for a Zoho call is not a Zoho id; nothing was sent", error="bad_id")
    return text


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _items(status: int, payload: Any) -> List[Dict[str, Any]]:
    """A list answer's `data`. A 204, or no body, is an empty list."""
    if status == 204 or payload is None:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ZohoUnavailable("Zoho's list answer had no data list", error="bad_shape")
    return [item for item in data if isinstance(item, dict)]


def _made_id(payload: Any, what: str) -> str:
    made = payload.get("id") if isinstance(payload, dict) else None
    if made is None or str(made) == "":
        # The write went through, and we cannot tell what it made. A resume
        # looks before it writes again.
        raise ZohoUnknownOutcome("Zoho answered a %s create with no id" % what, error="no_id")
    return str(made)


def safe_filename(name: str) -> str:
    """A file name that cannot break the multipart header and fits Zoho's limit."""
    cleaned = _UNSAFE_NAME.sub("_", name or "").strip(".") or "attachment"
    if len(cleaned) <= NAME_LIMIT:
        return cleaned
    stem, dot, extension = cleaned.rpartition(".")
    if dot and stem and 0 < len(extension) <= 10:
        return stem[: NAME_LIMIT - len(extension) - 1] + "." + extension
    return cleaned[:NAME_LIMIT]


def find_adoptable(tickets: List[Dict[str, Any]], chat_reference: Optional[str],
                   *, not_before: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The ticket an earlier attempt made, if it is in this list.

    The chat reference is the last thing in a ticket's subject, in square
    brackets, and a ticket is taken only when its subject (less trailing
    white space) ends with " [<chat_reference>]" exactly. Nothing looser:
    chat references are unique per record and deployment, and a near match
    would put this customer's transcript on someone else's ticket. There are
    no custom fields to carry it.

    `not_before` is the record's `created_at` in the ticket store's format.
    A ticket whose `createdTime` is earlier than that, by more than
    ADOPT_SLACK_SECONDS, is not this record's: a reference can come round
    again when a database is started afresh. A ticket with no readable
    `createdTime` is judged by its subject alone, and so is every ticket when
    `not_before` is missing or unreadable.
    """
    if not chat_reference:
        return None
    ending = " [%s]" % chat_reference
    floor = _floor(not_before)
    for ticket in tickets:
        subject = ticket.get("subject") if isinstance(ticket, dict) else None
        if not isinstance(subject, str) or not subject.rstrip().endswith(ending):
            continue
        if floor is not None and _made_before(ticket.get("createdTime"), floor):
            continue
        return ticket
    return None


def _floor(not_before: Optional[str]) -> Any:
    if not not_before:
        return None
    try:
        return parse(not_before) - timedelta(seconds=ADOPT_SLACK_SECONDS)
    except ValueError:
        return None


def _made_before(created: Any, floor: Any) -> bool:
    if not isinstance(created, str):
        return False
    try:
        return parse(created) < floor
    except ValueError:
        return False


class DeskClient:
    def __init__(self, settings: ZohoSettings, tokens: TokenSource, http: DeskHTTP) -> None:
        self._settings = settings
        # Public: the worker reads the credit count from `http`, and the
        # person's scripts reuse both for the calls they make themselves.
        self.tokens = tokens
        self.http = http

    def __repr__(self) -> str:
        return "DeskClient(org_id=%r)" % self._settings.org_id

    def headers(self, content_type: Optional[str] = None) -> Dict[str, str]:
        """The headers every Desk call carries, with a live token."""
        headers = {
            "Authorization": "Zoho-oauthtoken " + self.tokens.token(),
            "orgId": self._settings.org_id,
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _call(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None,
              payload: Optional[Dict[str, Any]] = None, body: Optional[bytes] = None,
              content_type: Optional[str] = None, write: bool, timeout: Optional[float] = None) -> Tuple[int, Any]:
        url = DESK_URL + path
        if query:
            # "*" stays as typed: it is the search wildcard.
            url += "?" + urllib.parse.urlencode(query, safe="*")
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        for attempt in (1, 2):
            try:
                return self.http.call(method, url, self.headers(content_type), body, write=write, timeout=timeout)
            except ZohoAuthExpired:
                if attempt == 2:
                    raise
                # The token died early: Zoho keeps at most ten per refresh
                # token, and the person's scripts share ours. A 401 means
                # nothing was done, so the same step is sent once more.
                self.tokens.invalidate()
        raise AssertionError("unreachable")

    def _paged(self, path: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for page in range(MAX_PAGES):
            status, payload = self._call("GET", path, query={"from": page * PAGE, "limit": PAGE}, write=False)
            batch = _items(status, payload)
            items.extend(batch)
            if len(batch) < PAGE:
                break
        return items

    def search_contacts(self, field: str, last_ten: str) -> List[Dict[str, Any]]:
        """Contacts whose phone (or mobile) ends with these ten digits."""
        if field not in SEARCH_FIELDS:
            raise ValueError("contacts are searched by phone or mobile only")
        if not isinstance(last_ten, str) or not _TEN_DIGITS.fullmatch(last_ten):
            # Never the number itself in the message.
            raise ValueError("a contact search needs the last ten digits of a number")
        status, payload = self._call("GET", "/api/v1/contacts/search", query={field: "*" + last_ten}, write=False)
        return _items(status, payload)

    def create_contact(self, last_name: str, mobile: str) -> str:
        """A new contact with a last name and a mobile, and no email."""
        _, payload = self._call("POST", "/api/v1/contacts", payload={"lastName": last_name, "mobile": mobile},
                                write=True)
        return _made_id(payload, "contact")

    def contact_tickets(self, contact_id: str, department_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """The contact's tickets in one department, newest first. This is used
        instead of Zoho's search, which can lag by a few minutes."""
        path = "/api/v1/contacts/%s/tickets" % _path_id(contact_id)
        query = {"departmentId": _path_id(department_id), "sortBy": "-createdTime", "limit": int(limit)}
        status, payload = self._call("GET", path, query=query, write=False)
        return _items(status, payload)

    def create_ticket(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """{"id", "ticketNumber", "webUrl"} of the ticket made, as text."""
        _, made = self._call("POST", "/api/v1/tickets", payload=payload, write=True)
        ticket_id = _made_id(made, "ticket")
        return {"id": ticket_id, "ticketNumber": _text(made.get("ticketNumber")), "webUrl": _text(made.get("webUrl"))}

    def add_comment(self, ticket_id: str, content: str) -> str:
        """A private, plain-text comment. Its id comes back."""
        if len(content) > COMMENT_LIMIT:
            raise ZohoRejected("a comment over Zoho's 32,000 characters was not sent", error="too_long",
                               fields=("content",))
        path = "/api/v1/tickets/%s/comments" % _path_id(ticket_id)
        _, payload = self._call("POST", path, payload={"isPublic": False, "contentType": "plainText",
                                                       "content": content}, write=True)
        return _made_id(payload, "comment")

    def comments(self, ticket_id: str) -> List[Dict[str, Any]]:
        """Every comment on a ticket, so a resume can find the markers already posted."""
        return self._paged("/api/v1/tickets/%s/comments" % _path_id(ticket_id))

    def upload_attachment(self, ticket_id: str, filename: str, data: bytes, mime: str) -> str:
        """One file, as the multipart field "file", private. Its id comes back."""
        path = "/api/v1/tickets/%s/attachments" % _path_id(ticket_id)
        boundary = "EMotoradAI" + secrets.token_hex(16)
        while boundary.encode("ascii") in data:
            boundary = "EMotoradAI" + secrets.token_hex(16)
        kind = mime if isinstance(mime, str) and _MIME.fullmatch(mime) else "application/octet-stream"
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                % (boundary, safe_filename(filename), kind)).encode("ascii")
        body = head + data + ("\r\n--%s--\r\n" % boundary).encode("ascii")
        _, payload = self._call("POST", path, query={"isPublic": "false"}, body=body,
                                content_type="multipart/form-data; boundary=" + boundary,
                                write=True, timeout=UPLOAD_TIMEOUT)
        return _made_id(payload, "attachment")

    def attachments(self, ticket_id: str) -> List[Dict[str, Any]]:
        """Every attachment on a ticket, so a resume can see what is already there."""
        return self._paged("/api/v1/tickets/%s/attachments" % _path_id(ticket_id))
