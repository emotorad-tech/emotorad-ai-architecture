"""Read-only probe of EMotorad's Zoho Desk for the chatbot (spec 2026-10-05, section 10, person step 5).

    python scripts/zoho/probe.py --org-id <org id> --test-department-id <id> \
        --test-contact-id <id> [--department-id <id>] [--unverified-contact-id <id>]

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards. It asks for the client id, the client secret and the
chatbot's refresh token by hidden input, and keeps them in memory.

It only reads. It checks that the token sees the organisation given. Then it
lists the departments, every active ticket layout of the test and real
departments, the contact layout, the channels, and the test and unverified
contacts.

With the OMS's token (no Desk.basic.READ or Desk.settings.READ), Zoho refuses
the organisations, departments and layouts reads for scope, and that is
expected. The probe carries on. The organisation id is confirmed when the
test contact read, sent with it, succeeds. Each department given is confirmed
through one ticket in it (GET /api/v1/tickets with include=departments, which
needs only Desk.tickets.READ): its id and name are kept, never the ticket,
which holds a customer's details. zoho-departments.json is then written in
the list's own shape, so test_ticket.py's guard works unchanged. A
department with no ticket cannot be confirmed: create one in it by hand in
Desk and run the probe again. A layout id is optional, and the first ticket
is sent without one. Any other refusal of the organisations read stops it.

For each layout it records the layout id, whether it is the default,
and each field's API name, label, type, whether it is mandatory and, for a
pick list, the allowed values (a pick list whose label names people, dealers
or accounts is counted, never listed). It sums up each layout's required
custom fields: whether Zoho enforces them on a ticket made through the API is
what test_ticket.py settles. It prints the scopes Zoho says it granted and the
API credits left today. Every answer is masked (scripts/zoho/_common.py) and
written to docs/api-shapes/zoho-*.json. The token answer goes under "refresh"
in zoho-token.json, beside the error bodies already there. Claude reviews
those files and fills in the settings.

Two token requests per run (one to read the scopes, one by the client). Zoho
allows ten in ten minutes per refresh token. Replaces the 1 October spike in
reports/zoho-probe/.
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from _common import (
    SCOPES, SHAPES, TOKEN_URL, ScriptClient, ask_secret, attempt, granted_scopes, mask, post_form, refresh_form,
    refused_for_scope, rows, save_shape, says_india, script_settings, source_note, step, zoho_id,
)
from emotorad_ai.zoho.errors import ZohoError
from emotorad_ai.zoho.http import DeskHTTP

# Added to the departments shape's note when the departments were confirmed
# one ticket at a time.
BY_TICKET_NOTE = (" The token cannot list departments (no Desk.basic.READ, as with the OMS's token), so this"
                  " list was confirmed through a ticket in each department: GET /api/v1/tickets with the"
                  " department's id, include=departments and limit=1. Only each department's id and name were"
                  " kept, never the ticket.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--org-id", required=True)
    p.add_argument("--test-department-id", required=True, help="the existing test department, by id")
    p.add_argument("--test-contact-id", required=True)
    p.add_argument("--department-id", help="the real department, once the support lead has named it")
    p.add_argument("--unverified-contact-id", help='the "Unverified AI chat" contact')
    return p


def describe_contact(answer: Any) -> str:
    """Whether a contact exists and has a number. Never the number."""
    if not isinstance(answer, dict) or not answer.get("id"):
        return "not found"
    return "found, %s" % ("has a number" if answer.get("phone") or answer.get("mobile") else "no number")


def active(listed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The layouts that are in use. A layout whose list entry names no status
    counts as active: the probe reads one too many, not one too few."""
    return [row for row in listed if str(row.get("status") or "ACTIVE").strip().upper() == "ACTIVE"]


def _fields(value: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(value, dict):
        if "apiName" in value:
            yield value
            return
        for item in value.values():
            yield from _fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from _fields(item)


def _mandatory(field: Dict[str, Any]) -> bool:
    return bool(field.get("isMandatory") or field.get("isSystemMandatory"))


def field_lines(masked: Any) -> List[str]:
    """One line per field in a masked layout: API name, label, type, whether
    it is mandatory or custom, and its pick-list values when they were kept."""
    lines = []
    for field in _fields(masked):
        values = field.get("allowedValues")
        if isinstance(values, dict):
            shown = "%s values withheld" % values.get("count", 0)
        elif isinstance(values, list):
            shown = ", ".join(str(v.get("value") if isinstance(v, dict) else v) for v in values)
        else:
            shown = ""
        flags = ("mandatory " if _mandatory(field) else "") + ("custom " if field.get("isCustomField") else "")
        lines.append("  %-30s %-30s %-10s %s%s" % (field.get("apiName"), field.get("displayLabel"),
                                                  field.get("type") or "", flags,
                                                  ("values: " + shown) if shown else ""))
    return lines


def layout_lines(masked: Dict[str, Any]) -> List[str]:
    """One masked layout as text: its id, name and default flag, each field,
    and the custom fields it makes required. The required ones are what
    decides whether Zoho will take a ticket the chatbot makes without them."""
    default = masked.get("isDefaultLayout")
    flag = "default flag not given" if default is None else ("the default layout" if default
                                                            else "not the default layout")
    lines = ['layout %s "%s": %s' % (masked.get("id"), masked.get("layoutName") or masked.get("layoutDisplayName"),
                                     flag)]
    lines += field_lines(masked)
    required = [str(field.get("apiName")) for field in _fields(masked)
                if field.get("isMandatory") and field.get("isCustomField")]
    lines.append("  %d required custom field(s)%s" % (len(required), ": " + ", ".join(required) if required else ""))
    return lines


def layout_details(client: ScriptClient, out: Callable[[str], None], label: str, params: Dict[str, Any],
                   refused: Optional[Callable[[ZohoError], None]] = None) -> Dict[str, Any]:
    """A module's active layouts, and each one's sections and fields. A
    refusal of the list is printed and handed to `refused`."""
    listed, refusal = attempt(lambda: client.get("/api/v1/layouts", params))
    if refusal is not None:
        out("%s layouts: refused (error=%s)" % (label, refusal.error))
        if refused is not None:
            refused(refusal)
    every = rows(listed)
    in_use = active(every)
    out("%s layouts: %d active, %d not read (not active)" % (label, len(in_use), len(every) - len(in_use)))
    details = []
    for row in in_use:
        layout_id = zoho_id(row.get("id"))
        if layout_id is None:
            out("%s layout: an entry has no usable id, skipped" % label)
            continue
        detail = step(out, "%s layout %s" % (label, layout_id),
                      lambda layout_id=layout_id: client.get("/api/v1/layouts/%s" % layout_id))
        if detail is not None:
            details.append(detail)
    return {"layouts": listed, "details": details}


def department_by_ticket(client: ScriptClient, department_id: str) -> Optional[str]:
    """A department's name, read from one ticket in it, or None when no ticket
    can be read or the ticket names another department. Only the department's
    id and name are taken. The ticket holds a customer's details: it is never
    printed or saved, and the client's in-memory copy of the answer is dropped."""
    if zoho_id(department_id) is None:
        return None
    answer, _ = attempt(lambda: client.get(
        "/api/v1/tickets", {"departmentId": department_id, "include": "departments", "limit": 1}))
    client.http.answers.pop(("GET", "/api/v1/tickets"), None)
    for ticket in rows(answer):
        department = ticket.get("department")
        if not isinstance(department, dict) or str(department.get("id")) != str(department_id):
            continue
        name = department.get("name")
        if isinstance(name, str) and name.strip():
            return name
    return None


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass, http: Any = None,
         out: Callable[[str], None] = print, shapes_dir: Path = SHAPES) -> int:
    args = parser().parse_args(argv)
    client_id = ask_secret("Zoho client id: ", ask)
    client_secret = ask_secret("Zoho client secret: ", ask)
    refresh_token = ask_secret("Zoho refresh token: ", ask)
    http = http if http is not None else DeskHTTP()
    source = source_note("probe.py")

    # One token request of our own, only to read what Zoho says it granted.
    try:
        _, answer = post_form(http, TOKEN_URL, refresh_form(client_id, client_secret, refresh_token))
    except ZohoError as exc:
        out("token: refused (error=%s)" % exc.error)
        return 1
    if answer.get("error"):
        out("token: refused (error=%s)" % answer["error"])
        return 1
    if not says_india(answer):
        out("token: Zoho's answer does not name the India data centre. Stopping.")
        return 1
    # Under "refresh": the error bodies beside it in that file are what the suite
    # answers a refused token with, and no probe run meets them.
    save_shape("token", answer, source, shapes_dir, under="refresh")
    granted = granted_scopes(answer)
    out("scopes granted: %s" % (", ".join(granted) if granted else "not stated in Zoho's answer"))
    missing = [scope for scope in SCOPES if scope not in granted]
    if granted and missing:
        out("scopes MISSING: %s" % ", ".join(missing))

    client = ScriptClient(script_settings(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token, org_id=args.org_id,
        department_id=args.test_department_id, contact_id=args.test_contact_id), http)

    orgs, refusal = attempt(lambda: client.get("/api/v1/organizations", org=False))
    # True while the organisation id rests on a later read sent with it.
    org_unconfirmed = False
    if refusal is not None:
        out("organisations: refused (error=%s)" % refusal.error)
        if not refused_for_scope(refusal):
            out("The token cannot see organisation %s. Stopping." % args.org_id)
            return 1
        org_unconfirmed = True
        out("  The token cannot list organisations, which is expected with the OMS's token. Carrying on:"
            " organisation %s is confirmed when a read sent with it succeeds (the test contact, below)." % args.org_id)
    else:
        mine = [row for row in rows(orgs) if str(row.get("id")) == str(args.org_id)]
        if not mine:
            out("The token cannot see organisation %s. Stopping." % args.org_id)
            return 1
        out("organisation %s: found, edition %s" % (args.org_id, mine[0].get("edition")))
        save_shape("organizations", orgs, source, shapes_dir)

    wanted = [(label, department_id) for label, department_id in (("test", args.test_department_id),
                                                                  ("real", args.department_id)) if department_id]
    departments, refusal = attempt(lambda: client.get("/api/v1/departments", {"limit": 100}))
    by_ticket = refused_for_scope(refusal)
    if refusal is not None:
        out("departments: refused (error=%s)" % refusal.error)
    if by_ticket:
        out("  The token cannot list departments, which is expected with the OMS's token. Each department given"
            " is confirmed through one ticket in it instead.")
        names = {}
        for _, department_id in wanted:
            name = department_by_ticket(client, department_id)
            if name is not None:
                names[str(department_id)] = name
        save_shape("departments", {"data": [{"id": key, "name": value} for key, value in names.items()]},
                   source + BY_TICKET_NOTE, shapes_dir)
        out("departments confirmed through a ticket: %d of %d" % (len(names), len(wanted)))
    else:
        if departments is not None:
            save_shape("departments", departments, source, shapes_dir)
        names = {str(row.get("id")): str(row.get("name")) for row in rows(departments)}
        out("departments: %d" % len(names))

    said_layouts: List[bool] = []

    def layouts_refused(exc: ZohoError) -> None:
        if refused_for_scope(exc) and not said_layouts:
            said_layouts.append(True)
            out("  The token cannot read layouts, which is expected with the OMS's token. The layout id is optional:"
                " leave EMOTORAD_ZOHO_LAYOUT_ID out, and the first ticket is sent without one.")

    for label, department_id in wanted:
        name = names.get(str(department_id))
        if by_ticket:
            out("%s department %s: %s" % (label, department_id,
                                          "%s (confirmed through a ticket in it)" % name if name else "NOT CONFIRMED"))
            if name is None:
                out("  No ticket in it could be read, so it cannot be confirmed, and test_ticket.py refuses it."
                    " Create one ticket in that department by hand in Desk, then run the probe again.")
        else:
            out("%s department %s: %s" % (label, department_id, name or "NOT FOUND"))
            if name is None:
                out("  Not in the departments listed. Check the id; test_ticket.py refuses a department it has not"
                    " seen.")
        if name is not None and label == "test":
            out("  test_ticket.py asks for this name, typed exactly. Check the department is quiet first.")
        found = layout_details(client, out, "%s ticket" % label, {"module": "tickets", "departmentId": department_id},
                               layouts_refused)
        save_shape("ticket-layouts-%s" % label, found, source, shapes_dir)
        for detail in found["details"]:
            for line in layout_lines(mask(detail)):
                out(line)

    found = layout_details(client, out, "contact", {"module": "contacts"}, layouts_refused)
    save_shape("contact-layout", found, source, shapes_dir)
    for detail in found["details"]:
        for line in layout_lines(mask(detail)):
            out(line)

    channels = step(out, "channels", lambda: client.get("/api/v1/channels"))
    if channels is not None:
        save_shape("channels", channels, source, shapes_dir)
    out("channels: %s" % (", ".join(str(row.get("name")) for row in rows(channels)) or "none listed"))

    contacts: Dict[str, Any] = {}
    for label, contact_id in (("test", args.test_contact_id), ("unverified", args.unverified_contact_id)):
        if not contact_id:
            continue
        if zoho_id(contact_id) is None:
            out("%s contact id is not a Zoho id (digits only). Skipped." % label)
            continue
        contacts[label] = step(out, "%s contact" % label,
                               lambda contact_id=contact_id: client.get("/api/v1/contacts/%s" % contact_id))
        out("%s contact %s: %s" % (label, contact_id, describe_contact(contacts[label])))
    save_shape("contacts", contacts, source, shapes_dir, person=True)
    if org_unconfirmed:
        test_contact = contacts.get("test")
        if isinstance(test_contact, dict) and test_contact.get("id"):
            org_unconfirmed = False
            out("organisation %s: confirmed, the test contact read sent with it succeeded." % args.org_id)
        else:
            out("organisation %s: NOT confirmed, no read sent with it succeeded. Check the organisation id and the"
                " test contact id, then run the probe again." % args.org_id)

    credits = getattr(http, "last_credits_remaining", None)
    out("API credits left today: %s" % (credits if credits is not None else "not reported"))
    out("Masked shapes written to %s. Ask Claude to review them." % shapes_dir)
    return 1 if org_unconfirmed else 0


if __name__ == "__main__":
    raise SystemExit(main())
