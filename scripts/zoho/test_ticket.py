"""Write one test ticket in Zoho Desk and read it back (spec 2026-10-05, section 10, person step 6).

    python scripts/zoho/test_ticket.py --org-id <org id> --contact-id <test contact id> \
        --test-department-id <test department id> --department-id <test department id>

    # Person step 11 only, with Sachin watching: one ticket in the real department.
    python scripts/zoho/test_ticket.py ... --department-id <real department id> --real-department

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards. Run probe.py first, and pass --layout-id when the
service will pin tickets to one (EMOTORAD_ZOHO_LAYOUT_ID).

The test department is the existing one the person names with
--test-department-id (EMOTORAD_ZOHO_TEST_DEPARTMENT_ID). The script asks for
the department's name every time, and refuses unless the department given is
that one, the probe has seen it (docs/api-shapes/zoho-departments.json) and
the name typed matches it exactly. With --real-department it writes once to
another department instead, after the person types that department's name,
and it never writes to the test department then.

On the test contact, with fake data only (the test number +919999999999 and a
fixture frame number), it:
  1. searches contacts by the test number (read only, to record the shape);
  2. creates a ticket built exactly as the worker builds one, with no custom
     field and the chat reference stage:EM-TEST-<n> at the end of its subject.
     If Zoho refuses it and names fields the chatbot does not send, the layout
     enforces its required fields: the script prints their names, saves the
     refusal and stops, and the support lead chooses a value for each;
  3. reads it back, and checks the subject, the frame number and that the
     description's lines survived;
  4. runs the worker's look-up (the contact's tickets, matched on the chat
     reference at the end of the subject by find_adoptable) at once and after
     two minutes, and makes a second ticket only if both find nothing;
  5. adds a private comment, then uploads a small image and files of 19 MB and
     26 MB, to find Zoho's attachment limit;
  6. saves the masked shapes to docs/api-shapes/zoho-*.json.
The real-department run stops after step 4 and never makes a second ticket.
Close the ticket (or tickets) in Desk afterwards.
"""

from __future__ import annotations

import argparse
import getpass
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from _common import (
    SHAPES, TEST_PHONE, ScriptClient, ask_secret, department_names, department_refusal, rows, save_shape,
    script_settings, source_note, zoho_id,
)
from emotorad_ai.tickets.clock import now_iso
from emotorad_ai.tickets.seam import DeskTicketSystem
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.tools.fixtures import WARRANTY_RECORDS
from emotorad_ai.zoho.desk import find_adoptable
from emotorad_ai.zoho.errors import ZohoError, ZohoRejected, ZohoTooLarge
from emotorad_ai.zoho.payload import ticket_payload

# A fixture bike: invented data, so the test ticket names no real frame.
_FIXTURE_BIKE = WARRANTY_RECORDS["+919876543210"][0]
FRAME_NUMBER = _FIXTURE_BIKE["frame_number"]
BIKE_MODEL = _FIXTURE_BIKE["product_name"]
MB = 1024 * 1024

# What the person is told when Zoho names fields the chatbot does not send.
ENFORCED = ("Zoho enforces the layout's required fields: the support lead must choose a value for each "
            "before part C continues.")
# Zoho may keep the description's newlines, or turn them into HTML. Either is
# readable. Lines run together into one are not.
_LINE_BREAKS = ("\n", "<br", "</p>", "</div>", "<li")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--org-id", required=True)
    p.add_argument("--test-department-id", required=True,
                   help="the existing test department, by id (EMOTORAD_ZOHO_TEST_DEPARTMENT_ID)")
    p.add_argument("--department-id", required=True,
                   help="where the ticket goes: the test department, or with --real-department another one")
    p.add_argument("--contact-id", required=True, help="the test contact, also with --real-department")
    p.add_argument("--layout-id", help="the layout the service will pin tickets to, from the probe")
    p.add_argument("--priority-high", default="High")
    p.add_argument("--priority-medium", default="Medium")
    p.add_argument("--channel", default="Chat")
    p.add_argument("--env", default="stage", help="the chat reference's prefix")
    p.add_argument("--n", type=int, help="the EM-TEST number. Defaults to the clock's seconds")
    p.add_argument("--real-department", action="store_true")
    return p


def fake_record(n: int, environment: str, mode: str) -> Dict[str, Any]:
    """A ticket record made by the service's own seam, so the payload built
    from it is the one the worker would send. Fake data only. The reference
    becomes EM-TEST-<n>, which no real record can have."""
    store = InMemoryTicketStore()
    desk = DeskTicketSystem(store, mode, environment)
    now = now_iso()
    made = desk.create(
        source_key="test_ticket:%d" % n, persona="customer", kind="support",
        conversation_id="test-ticket-%d" % n, started_at=now, cluster_id=None, channel="website_chat",
        phone=TEST_PHONE, identity="verified", category="battery_charging", severity="normal",
        description="Test ticket from scripts/zoho/test_ticket.py, fake data only. Close it in Desk.",
        frame_number=FRAME_NUMBER, frame_number_source=None, bike_model=BIKE_MODEL, coverage="computed",
        customer_name=None,
    )
    record = dict(store.get(made["ticket_id"]))
    record["_id"] = "EM-TEST-%d" % n
    record["chat_reference"] = "%s:EM-TEST-%d" % (environment, n)
    return record


def look_up(desk: Any, contact_id: str, department_id: str, chat_reference: str,
            waits: Sequence[float] = (0, 120), sleep: Callable[[float], None] = time.sleep,
            out: Callable[[str], None] = print) -> List[Optional[Dict[str, Any]]]:
    """The worker's look-up after an unknown outcome (spec section 4, step 2):
    the contact's tickets in this department, matched exactly on the chat
    reference at the end of the subject by the shared find_adoptable. Run at
    once and again after two minutes, to learn whether Zoho's list lags."""
    found: List[Optional[Dict[str, Any]]] = []
    elapsed = 0.0
    for wait in waits:
        if wait:
            out("Looking again in %d seconds..." % wait)
            sleep(wait)
            elapsed += wait
        match = find_adoptable(desk.contact_tickets(contact_id, department_id), chat_reference)
        out("look-up at %d s: %s" % (elapsed, "found #%s" % match.get("ticketNumber") if match else "not found"))
        found.append(match)
    return found


def needs_second_create(found: Sequence[Optional[Dict[str, Any]]]) -> bool:
    return not any(found)


def lines_kept(description: str) -> bool:
    """Whether a description read back still has its lines."""
    lowered = description.lower()
    return any(marker in lowered for marker in _LINE_BREAKS)


def tiny_png(size: int = 8) -> bytes:
    """A small grey square, made here so no file is read from disk."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    pixels = b"".join(b"\x00" + b"\x80" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def filler(megabytes: int) -> bytes:
    return bytes(megabytes * MB)


def uploads(reference: str) -> List[Tuple[str, bytes, str]]:
    """A small image, then files either side of 20 MB, to find Zoho's limit
    (spec "Open until part 1"). Names start with the reference, as the worker's do."""
    return [
        ("%s-small.png" % reference, tiny_png(), "image/png"),
        ("%s-19mb.bin" % reference, filler(19), "application/octet-stream"),
        ("%s-26mb.bin" % reference, filler(26), "application/octet-stream"),
    ]


def upload(desk: Any, ticket_id: str, name: str, data: bytes, mime: str) -> str:
    """What happened to one upload, in words. A refusal is the finding, not a failure."""
    try:
        return "attached (id %s)" % desk.upload_attachment(ticket_id, name, data, mime)
    except ZohoTooLarge as exc:
        return "refused as too large (error=%s)" % exc.error
    except ZohoError as exc:
        return "failed (error=%s)" % exc.error


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         typed: Callable[[str], str] = input, http: Any = None, out: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, shapes_dir: Path = SHAPES) -> int:
    args = parser().parse_args(argv)
    # Always asked: the person confirms, in their own words, which department
    # this is, whichever one it turns out to be.
    typed_name = typed("Type the name of department %s exactly as Desk shows it: " % args.department_id)
    refusal = department_refusal(department_names(shapes_dir), args.department_id, args.test_department_id,
                                 args.real_department, typed_name)
    if refusal:
        out(refusal)
        return 1
    if zoho_id(args.contact_id) is None:
        out("--contact-id is not a Zoho id (digits only). Nothing was sent.")
        return 1
    settings = script_settings(
        client_id=ask_secret("Zoho client id: ", ask), client_secret=ask_secret("Zoho client secret: ", ask),
        refresh_token=ask_secret("Zoho refresh token: ", ask), org_id=args.org_id,
        department_id=args.department_id, contact_id=args.contact_id, live=args.real_department,
        environment=args.env, layout_id=args.layout_id, priority_high=args.priority_high,
        priority_medium=args.priority_medium, channel=args.channel)
    client = ScriptClient(settings, http)
    n = args.n if args.n is not None else int(time.time())
    record = fake_record(n, args.env, "live" if args.real_department else "test")
    payload = ticket_payload(record, settings, args.contact_id)
    if str(payload.get("departmentId")) != str(args.department_id):
        out("The payload names another department. Nothing was sent.")
        return 1
    created: Dict[str, Any] = {}
    try:
        return _run(client, args, record, payload, created, source_note("test_ticket.py"), shapes_dir, sleep, out)
    except ZohoError as exc:
        where = " Close ticket #%s in Desk." % created.get("ticketNumber") if created else ""
        out("Stopped: error=%s.%s" % (exc.error, where))
        return 1


def _rejected(exc: ZohoRejected, payload: Dict[str, Any], source: str, shapes_dir: Path,
              out: Callable[[str], None]) -> int:
    """Zoho refused the first ticket. Its field names are what the support
    lead needs, and they are printed without any value."""
    names = list(exc.fields)
    out("Zoho rejected the ticket (error=%s)." % exc.error)
    if names:
        out("Fields Zoho named: %s" % ", ".join(names))
    # The client keeps no body (an error never carries one), so what is saved
    # is the code and the names it classified.
    save_shape("ticket-rejected", {"errorCode": exc.error, "errors": [{"fieldName": name} for name in names]},
               source + " The error code and field names only: the client keeps no body.", shapes_dir)
    unsent = [name for name in names if name not in payload]
    if unsent:
        # A field the chatbot never sends can only be named because the layout wants it.
        out(ENFORCED)
    elif names:
        out("Nothing was created. Zoho refused a field the chatbot does send: fix the payload before going on.")
    else:
        out("Nothing was created. Zoho named no field.")
    return 1


def _run(client: ScriptClient, args: argparse.Namespace, record: Dict[str, Any], payload: Dict[str, Any],
         created: Dict[str, Any], source: str, shapes_dir: Path, sleep: Callable[[float], None],
         out: Callable[[str], None]) -> int:
    last_ten = TEST_PHONE[-10:]
    save_shape("contact-search", client.get("/api/v1/contacts/search", {"phone": "*" + last_ten, "limit": 10}),
               source, shapes_dir, person=True)
    for field in ("phone", "mobile"):
        out("contacts whose %s ends %s: %d" % (field, last_ten[-4:], len(client.desk.search_contacts(field, last_ten))))

    # No custom field, as the worker sends none. If the layout requires some,
    # Zoho says so here.
    try:
        created.update(client.desk.create_ticket(payload))
    except ZohoRejected as exc:
        return _rejected(exc, payload, source, shapes_dir, out)
    out("Created ticket #%s with chat reference %s." % (created.get("ticketNumber"), record["chat_reference"]))
    ticket_id = zoho_id(created.get("id"))
    if ticket_id is None:
        out("Zoho's ticket id is not a Zoho id, so nothing more was read. Close ticket #%s in Desk."
            % created.get("ticketNumber"))
        return 1

    ticket = client.get("/api/v1/tickets/%s" % ticket_id)
    save_shape("ticket", ticket, source, shapes_dir)
    subject = str((ticket or {}).get("subject") or "")
    description = str((ticket or {}).get("description") or "")
    out("chat reference ends the subject: %s"
        % ("yes" if subject.rstrip().endswith(" [%s]" % record["chat_reference"]) else "NO"))
    out("frame number in the description: %s" % ("yes" if FRAME_NUMBER in description else "NO"))
    out("lines kept in the description: %s" % ("yes" if lines_kept(description) else "NO"))

    # The worker's own call (zoho/desk.py contact_tickets), read raw for its shape.
    listing = client.get("/api/v1/contacts/%s/tickets" % args.contact_id,
                         {"departmentId": args.department_id, "sortBy": "-createdTime", "limit": 50})
    save_shape("contact-tickets", listing, source, shapes_dir)
    listed = rows(listing)
    out("subjects in the contact's ticket list: %s"
        % ("not known, the list is empty" if not listed else "yes" if all("subject" in t for t in listed) else "NO"))
    found = look_up(client.desk, args.contact_id, args.department_id, record["chat_reference"],
                    sleep=sleep, out=out)
    if not needs_second_create(found):
        out("The look-up found the first ticket, so no second ticket was made.")
    elif args.real_department:
        out("The look-up found nothing. No second ticket in the real department. Tell Claude the list lags.")
    else:
        second = client.desk.create_ticket(payload)
        out("The look-up found nothing, so a second ticket was made (#%s): the list lags. Close both."
            % second.get("ticketNumber"))

    if args.real_department:
        out("Real department: one ticket only. Close #%s in Desk once it has been checked." % created.get("ticketNumber"))
        return 0

    comment_id = zoho_id(client.desk.add_comment(
        ticket_id, "[%s test comment] Private, plain text. Nothing to do." % record["chat_reference"]))
    if comment_id:
        save_shape("comment", client.get("/api/v1/tickets/%s/comments/%s" % (ticket_id, comment_id)),
                   source, shapes_dir)
    for name, data, mime in uploads(record["_id"]):
        out("upload %-26s %s" % (name, upload(client.desk, ticket_id, name, data, mime)))
    save_shape("attachment", client.get("/api/v1/tickets/%s/attachments" % ticket_id), source, shapes_dir)
    out("Done. Close ticket #%s in Desk now." % created.get("ticketNumber"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
