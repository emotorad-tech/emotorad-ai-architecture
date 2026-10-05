"""The Zoho worker (spec 2026-10-05 section 4), offline.

Zoho here is mostly a double with DeskClient's methods over dicts, so a test
can make any call fail before its write lands or after it (a timeout after
Zoho made the ticket). RealClientTests runs the real DeskClient over
tests/fake_zoho.py for the case the review focuses on. The ticket store and
the conversation store are the in-memory ones, on one clock the test moves by
hand. No socket is opened, and only the two loop tests at the end start a
thread.

The 401 INVALID_OAUTH refresh and retry belongs to DeskClient
(tests/test_zoho_desk.py). The worker sees ZohoAuthExpired only when the
retry failed too.
"""

import copy
import itertools
import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace

from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.media_records import media_record
from emotorad_ai.observability import EventLog
from emotorad_ai.storage.s3 import StorageError
from emotorad_ai.tickets.clock import plus
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.store import InMemoryTicketStore
from emotorad_ai.zoho import errors
from emotorad_ai.zoho.auth import TokenSource
from emotorad_ai.zoho.desk import DeskClient
from emotorad_ai.zoho.http import DeskHTTP
from emotorad_ai.zoho.settings import ZohoSettings
from emotorad_ai.zoho.worker import THREAD_NAME, ZohoWorker, retry_wait
from tests.fake_zoho import TEST_CONTACT as FAKE_ZOHO_CONTACT
from tests.fake_zoho import Answer, FakeZoho, zoho_settings
from tests.store_contract import inbound, reply

START = "2026-10-05T10:00:00.000000+00:00"
PHONE = "+919999999999"
TEST_DEPARTMENT, REAL_DEPARTMENT = "dept-test", "dept-real"
TEST_CONTACT, UNVERIFIED_CONTACT = "contact-test", "contact-unverified"
MB = 1024 * 1024
# The events an alarm watches carry level="error". The others carry no level.
NOT_ALARMED = ("zoho_ticket_sent", "zoho_retry", "zoho_rejected", "zoho_ticket_gone", "zoho_ticket_adopted",
               "zoho_record_dropped")


def settings(live=False, **changes):
    values = dict(
        client_id="client-test", client_secret="secret-test", refresh_token="refresh-test", org_id="org-test",
        test_department_id=TEST_DEPARTMENT, test_contact_id=TEST_CONTACT, department_id=REAL_DEPARTMENT,
        unverified_contact_id=UNVERIFIED_CONTACT, live=live, environment="stage",
        priority_high="High", priority_medium="Medium", channel="Chat", credits_floor=1000,
        attachment_limit_bytes=20 * MB,
    )
    values.update(changes)
    return ZohoSettings(**values)


def zoho_error(cls, error, **attributes):
    """One of the exceptions in errors.py, as DeskClient raises it."""
    return cls("Zoho answered %s" % error, error=error, **attributes)


class Clock:
    """The time, moved by hand. Shared by the worker and the conversation store."""

    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = plus(self.now, seconds)
        return self.now


class FakeDesk:
    """DeskClient's methods over dicts.

    `fail(name, exc)` makes the next call of that method raise before it does
    anything. With `after=True` the write lands first and the error comes back
    afterwards, which is what a timeout after Zoho made the ticket looks like.
    `hooks[name]` runs at the start of the next call of that method. `down`
    makes every call fail as unreachable."""

    def __init__(self):
        self.http = SimpleNamespace(last_credits_remaining=None)
        # The two contacts from person step 3. They have no numbers here, so a
        # search never finds them by accident.
        self.contacts = {
            TEST_CONTACT: {"id": TEST_CONTACT, "lastName": "AI chatbot test"},
            UNVERIFIED_CONTACT: {"id": UNVERIFIED_CONTACT, "lastName": "Unverified AI chat"},
        }
        self.tickets = {}
        self.comment_log = {}
        self.files = {}
        self.calls = []
        self.hooks = {}
        self.down = False
        self._errors = {}
        self._ids = itertools.count(1)

    def fail(self, name, exc, after=False):
        self._errors.setdefault(name, []).append((exc, after))

    def _enter(self, name, *args):
        self.calls.append((name,) + args)
        hook = self.hooks.pop(name, None)
        if hook is not None:
            hook()
        if self.down:
            raise zoho_error(errors.ZohoUnavailable, "network")
        queued = self._errors.get(name)
        if queued and not queued[0][1]:
            raise queued.pop(0)[0]

    def _leave(self, name, result):
        queued = self._errors.get(name)
        if queued and queued[0][1]:
            raise queued.pop(0)[0]
        return result

    def _known_ticket(self, ticket_id):
        if ticket_id not in self.tickets:
            raise zoho_error(errors.ZohoGone, "RESOURCE_NOT_FOUND")

    def search_contacts(self, field, last_ten):
        self._enter("search_contacts", field, last_ten)
        found = [dict(c) for c in self.contacts.values() if (c.get(field) or "").endswith(last_ten)]
        return self._leave("search_contacts", found)

    def create_contact(self, last_name, mobile):
        self._enter("create_contact", last_name, mobile)
        contact_id = "contact-%d" % next(self._ids)
        self.contacts[contact_id] = {"id": contact_id, "lastName": last_name, "mobile": mobile}
        return self._leave("create_contact", contact_id)

    def contact_tickets(self, contact_id, department_id, limit=50):
        self._enter("contact_tickets", contact_id, department_id)
        if contact_id not in self.contacts:
            raise zoho_error(errors.ZohoGone, "RESOURCE_NOT_FOUND")
        newest_first = [copy.deepcopy(t) for t in reversed(list(self.tickets.values()))
                        if t["contactId"] == contact_id and t["departmentId"] == department_id]
        return self._leave("contact_tickets", newest_first[:limit])

    def create_ticket(self, payload):
        self._enter("create_ticket", payload)
        if payload.get("contactId") not in self.contacts:
            raise zoho_error(errors.ZohoGone, "RESOURCE_NOT_FOUND")
        ticket_id = "ticket-%d" % next(self._ids)
        number = str(1000 + len(self.tickets))
        url = "https://desk.zoho.in/agent/tickets/" + ticket_id
        self.tickets[ticket_id] = dict(copy.deepcopy(payload), id=ticket_id, ticketNumber=number, webUrl=url)
        return self._leave("create_ticket", {"id": ticket_id, "ticketNumber": number, "webUrl": url})

    def add_comment(self, ticket_id, content):
        self._enter("add_comment", ticket_id, content)
        self._known_ticket(ticket_id)
        comment_id = "comment-%d" % next(self._ids)
        self.comment_log.setdefault(ticket_id, []).append(
            {"id": comment_id, "content": content, "isPublic": False, "contentType": "plainText"})
        return self._leave("add_comment", comment_id)

    def comments(self, ticket_id):
        self._enter("comments", ticket_id)
        self._known_ticket(ticket_id)
        return self._leave("comments", copy.deepcopy(self.comment_log.get(ticket_id, [])))

    def upload_attachment(self, ticket_id, filename, data, mime):
        self._enter("upload_attachment", ticket_id, filename, len(data), mime)
        self._known_ticket(ticket_id)
        attachment_id = "file-%d" % next(self._ids)
        self.files.setdefault(ticket_id, []).append({"id": attachment_id, "name": filename, "size": len(data)})
        return self._leave("upload_attachment", attachment_id)

    def attachments(self, ticket_id):
        self._enter("attachments", ticket_id)
        self._known_ticket(ticket_id)
        return self._leave("attachments", copy.deepcopy(self.files.get(ticket_id, [])))


class Bucket:
    """S3Store's get_bytes and head over a dict, recording each read. A key in
    `broken` fails as S3 itself failing; a key not in `objects` is not found."""

    def __init__(self):
        self.objects, self.reads, self.broken = {}, [], set()

    def get_bytes(self, key):
        self.reads.append(key)
        if key in self.broken or key not in self.objects:
            raise StorageError("get failed")
        return self.objects[key]

    def head(self, key):
        if key in self.broken:
            raise StorageError("head failed")
        if key not in self.objects:
            return None
        return {"size": len(self.objects[key]), "mime": "image/jpeg"}


class RefusesIntents:
    """The ticket store, except that saving an intent matches nothing. This is
    what happens when the record is erased or another worker takes it in
    between."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def save(self, reference, token, changes, **kwargs):
        if changes.get("intent"):
            return False
        return self.inner.save(reference, token, changes, **kwargs)


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tickets = InMemoryTicketStore()
        self.conversations = InMemoryConversationStore(clock=self.clock)
        self.desk = FakeDesk()
        self.bucket = Bucket()
        self.log = EventLog(path=None)
        self.worker = self.make_worker()

    def make_worker(self, live=False, **changes):
        return ZohoWorker(self.tickets, self.desk, self.conversations, self.bucket,
                          settings(live=live, **changes), self.log, clock=self.clock)

    def chat(self, turns=(("my battery won't charge", "Try another socket."),), cid="conv-1"):
        """Turns said now, in the current run of `cid`. Returns the run's start."""
        state = self.conversations.get(cid)
        for customer, bot in turns:
            state.turns += 1
            self.conversations.record_turn(state, inbound(customer, cid=cid), reply(bot, cid=cid))
        return state.started_at

    def restart(self, customer, bot, cid="conv-1"):
        """A second person proves a number on the same conversation id (verify
        first's restart_for). This is a new run, and its turns are numbered on
        from the last run's."""
        state = self.conversations.get(cid)
        state.turns += 1
        state.restart_for("PHONE#" + PHONE, self.clock())
        self.conversations.record_turn(state, inbound(customer, cid=cid), reply(bot, cid=cid))
        return state.started_at

    def record(self, started_at, kind="support", category="battery_charging", mode="test", identity="verified",
               customer_name=None, cluster_id="cluster-1", cid="conv-1", phone=PHONE):
        reference = self.tickets.next_reference()
        return self.tickets.insert(new_record(
            reference=reference, chat_reference="stage:" + reference,
            source_key="%s:%s:create_support_ticket:%s" % (cid, started_at, reference),
            mode=mode, kind=kind, conversation_id=cid, started_at=started_at, cluster_id=cluster_id,
            channel="web", phone=phone, identity=identity, category=category, ai_severity="normal",
            summary="Charger LED stays off; tried another socket.", claims={}, bike=None,
            coverage="computed", customer_name=customer_name, created_at=self.clock(),
        ))["_id"]

    def photo(self, upload_id, cluster_id, stored_at, size=200_000, kind="image", cid="conv-1"):
        folder, ext, mime = ("images", "jpg", "image/jpeg") if kind == "image" else ("videos", "mp4", "video/mp4")
        key = "customers/%s/%s/%s/%s.%s" % (cluster_id, cid, folder, upload_id, ext)
        self.bucket.objects[key] = b"fake media bytes"
        self.conversations.record_media(media_record(
            bucket="media-test", key=key, kind=kind, mime_type=mime, size_bytes=size,
            conversation_id=cid, cluster_id=cluster_id, source="inline", stored_at=stored_at))
        return key

    def wake(self, reference):
        self.tickets.wake(reference, self.clock())

    def send(self, reference):
        """The turn ended: wake the record and run the worker once."""
        self.wake(reference)
        return self.worker.run_once()

    def saved(self, reference):
        return self.tickets.get(reference)

    def ticket_of(self, reference):
        return self.saved(reference)["zoho"].get("ticket_id")

    def comments(self, reference):
        return self.desk.comment_log.get(self.ticket_of(reference), [])

    def text_on(self, reference):
        return "\n".join(c["content"] for c in self.comments(reference))

    def files(self, reference):
        return [f["name"] for f in self.desk.files.get(self.ticket_of(reference), [])]

    def events(self, name):
        return [e for e in self.log.events if e["event"] == name]

    def calls(self, name):
        return [c for c in self.desk.calls if c[0] == name]


class WhenTests(WorkerCase):
    def test_nothing_is_sent_before_the_turn_ends(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(10)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.desk.calls, [])
        self.wake(ref)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_turn_that_died_is_sent_two_minutes_after_the_record(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(119)
        self.assertFalse(self.worker.run_once())
        self.clock.advance(2)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_record_of_the_other_mode_is_held_and_never_sent(self):
        ref = self.record(started_at=self.chat(), mode="live")
        self.wake(ref)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.desk.calls, [])
        self.assertEqual(self.tickets.counts("test", self.clock())["held"], 1)


class LeaseTests(WorkerCase):
    def test_a_record_under_another_workers_lease_is_not_taken(self):
        ref = self.record(started_at=self.chat())
        self.wake(ref)
        self.assertIsNotNone(self.tickets.take_due(self.clock(), "test", 300, "another-worker"))
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.desk.calls, [])
        self.clock.advance(301)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_save_that_matches_nothing_drops_the_record_and_sends_nothing(self):
        ref = self.record(started_at=self.chat())
        self.worker.store = RefusesIntents(self.tickets)
        self.assertTrue(self.send(ref))
        self.assertEqual(self.calls("create_ticket"), [])
        self.assertEqual([e["reference"] for e in self.events("zoho_record_dropped")], [ref])
        self.assertNotEqual(self.saved(ref)["state"], "sent")
        # Once the lease runs out, a worker whose saves land sends it.
        self.worker.store = self.tickets
        self.clock.advance(301)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_turn_that_ends_while_the_worker_sends_is_not_lost(self):
        ref = self.record(started_at=self.chat())

        def another_turn():
            self.chat(turns=[("the light is red now", "Thanks, noted.")])
            self.wake(ref)

        self.desk.hooks["add_comment"] = another_turn
        self.assertTrue(self.send(ref))
        self.assertEqual(self.saved(ref)["state"], "waiting")
        self.assertEqual(self.saved(ref)["next_attempt_at"], self.clock())
        self.assertTrue(self.worker.run_once())
        saved = self.saved(ref)
        self.assertEqual(saved["state"], "sent")
        self.assertIn("the light is red now", self.text_on(ref))
        self.assertEqual(sorted(saved["posted_turns"]), [1, 2, 3, 4])

    def test_below_the_credits_floor_only_urgent_records_are_sent(self):
        started = self.chat()
        normal = self.record(started_at=started)
        urgent = self.record(started_at=started, kind="safety", category="battery_safety")
        self.desk.http.last_credits_remaining = 999
        self.wake(normal)
        self.wake(urgent)
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(urgent)["state"], "sent")
        self.assertEqual(self.saved(normal)["state"], "waiting")
        self.assertEqual(self.saved(normal)["next_attempt_at"], plus(self.clock(), 600))
        self.assertEqual(len(self.desk.tickets), 1)
        self.desk.http.last_credits_remaining = 5000
        self.clock.advance(600)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(normal)["state"], "sent")


class FieldTests(WorkerCase):
    def test_a_ticket_carries_what_code_sets_and_nothing_forbidden(self):
        started = self.chat()
        normal = self.record(started_at=started)
        urgent = self.record(started_at=started, kind="safety", category="battery_safety")
        self.wake(normal)
        self.wake(urgent)
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        # The chat reference ends the subject, in square brackets: there are no custom fields.
        by_reference = {t["subject"].rsplit(" [", 1)[1].rstrip("]"): t for t in self.desk.tickets.values()}
        ticket = by_reference["stage:" + normal]
        self.assertEqual(ticket["departmentId"], TEST_DEPARTMENT)
        self.assertEqual(ticket["contactId"], TEST_CONTACT)
        self.assertEqual(ticket["phone"], PHONE)
        self.assertEqual(ticket["priority"], "Medium")
        self.assertEqual(by_reference["stage:" + urgent]["priority"], "High")
        self.assertEqual(ticket["status"], "Open")
        self.assertEqual(ticket["channel"], "Chat")
        self.assertTrue(ticket["subject"].startswith("[AI chat]"))
        self.assertTrue(ticket["subject"].endswith(" [stage:%s]" % normal))
        self.assertLessEqual(len(ticket["subject"]), 255)
        self.assertIn(normal, ticket["description"])
        self.assertIn("Source: AI chatbot", ticket["description"])
        for forbidden in ("cf", "layoutId", "assigneeId", "teamId", "dueDate", "customFields"):
            self.assertNotIn(forbidden, ticket)
        self.assertNotIn("Dealer Principle", json.dumps(ticket))

    def test_a_test_record_always_goes_on_the_test_contact_with_no_search(self):
        started = self.chat()
        for identity in ("verified", "unverified"):
            ref = self.record(started_at=started, identity=identity)
            self.send(ref)
            self.assertEqual(self.saved(ref)["zoho"]["contact_id"], TEST_CONTACT)
        self.assertEqual(self.calls("search_contacts") + self.calls("create_contact"), [])

    def test_a_sent_ticket_is_logged_with_its_number_and_the_credits_left(self):
        self.desk.http.last_credits_remaining = 4321
        ref = self.record(started_at=self.chat())
        self.send(ref)
        saved = self.saved(ref)
        self.assertEqual((saved["state"], saved["attempts"], saved["intent"], saved["lease_token"]),
                         ("sent", 0, None, None))
        self.assertEqual(saved["zoho"]["ticket_number"], "1000")
        self.assertTrue(saved["zoho"]["web_url"])
        [sent] = self.events("zoho_ticket_sent")
        # "#1000", not "1000": the log hides a bare run of digits as a code.
        self.assertEqual((sent["reference"], sent["zoho_number"], sent["attempts"], sent["credits_remaining"]),
                         (ref, "#1000", 1, 4321))
        self.assertNotIn("level", sent)


class ContactTests(WorkerCase):
    """Live mode, step 1: whose contact the ticket goes on."""

    def setUp(self):
        super().setUp()
        self.worker = self.make_worker(live=True)
        self.started = self.chat()

    def live(self, **fields):
        return self.record(started_at=self.started, mode="live", **fields)

    def contact_of(self, ref):
        return self.saved(ref)["zoho"]["contact_id"]

    def test_an_unverified_number_goes_on_the_unverified_contact(self):
        ref = self.live(identity="unverified")
        self.send(ref)
        self.assertEqual(self.contact_of(ref), UNVERIFIED_CONTACT)
        self.assertEqual(self.calls("search_contacts") + self.calls("create_contact"), [])

    def test_a_verified_record_without_an_indian_mobile_goes_on_the_unverified_contact(self):
        for phone in (None, "+447700900123", "12345"):
            ref = self.live(phone=phone)
            self.send(ref)
            self.assertEqual(self.contact_of(ref), UNVERIFIED_CONTACT)
            self.assertEqual(self.saved(ref)["state"], "sent")
        self.assertEqual(self.calls("search_contacts") + self.calls("create_contact"), [])

    def test_one_match_by_phone_is_used(self):
        self.desk.contacts["contact-asha"] = {"id": "contact-asha", "lastName": "Asha", "phone": PHONE}
        ref = self.live()
        self.send(ref)
        self.assertEqual(self.contact_of(ref), "contact-asha")
        self.assertEqual([c[1:] for c in self.calls("search_contacts")], [("phone", "9999999999")])
        self.assertEqual(self.calls("create_contact"), [])

    def test_of_several_matches_the_one_named_as_on_record_is_used(self):
        self.desk.contacts["contact-a"] = {"id": "contact-a", "lastName": "Someone Else", "mobile": PHONE}
        self.desk.contacts["contact-b"] = {"id": "contact-b", "lastName": "Asha Test", "mobile": PHONE}
        ref = self.live(customer_name="Asha Test")
        self.send(ref)
        self.assertEqual(self.contact_of(ref), "contact-b")
        self.assertEqual([c[1] for c in self.calls("search_contacts")], ["phone", "mobile"])
        self.assertEqual(self.calls("create_contact"), [])

    def test_a_name_split_into_first_and_last_still_matches(self):
        self.desk.contacts["contact-a"] = {"id": "contact-a", "lastName": "Else", "mobile": PHONE}
        self.desk.contacts["contact-b"] = {"id": "contact-b", "firstName": "Asha", "lastName": "Test",
                                           "mobile": PHONE}
        ref = self.live(customer_name="asha  TEST")
        self.send(ref)
        self.assertEqual(self.contact_of(ref), "contact-b")

    def test_of_several_matches_with_none_named_a_new_contact_is_made(self):
        self.desk.contacts["contact-a"] = {"id": "contact-a", "lastName": "Someone", "mobile": PHONE}
        self.desk.contacts["contact-b"] = {"id": "contact-b", "lastName": "Someone Else", "mobile": PHONE}
        ref = self.live()
        self.send(ref)
        [call] = self.calls("create_contact")
        self.assertEqual(call[1:], ("AI chat customer", PHONE))
        self.assertNotIn(self.contact_of(ref), ("contact-a", "contact-b"))

    def test_with_no_match_a_contact_is_made_with_the_name_on_record(self):
        ref = self.live(customer_name="Asha Test")
        self.send(ref)
        [call] = self.calls("create_contact")
        self.assertEqual(call[1:], ("Asha Test", PHONE))
        made = self.desk.contacts[self.contact_of(ref)]
        self.assertEqual(made["mobile"], PHONE)
        self.assertNotIn("email", made)

    def test_the_contact_found_once_is_reused_for_the_same_number(self):
        first = self.live()
        self.send(first)
        searches = len(self.calls("search_contacts"))
        second = self.live()
        self.send(second)
        self.assertEqual(self.contact_of(second), self.contact_of(first))
        self.assertEqual(len(self.calls("search_contacts")), searches)
        self.assertEqual(len(self.calls("create_contact")), 1)

    def test_a_contact_from_a_test_or_unverified_record_is_never_reused(self):
        tester = self.make_worker(live=False)
        on_test = self.record(started_at=self.started, mode="test")
        self.wake(on_test)
        tester.run_once()
        self.send(self.live(identity="unverified"))
        verified = self.live()
        self.send(verified)
        self.assertNotIn(self.contact_of(verified), (TEST_CONTACT, UNVERIFIED_CONTACT))
        self.assertEqual(len(self.calls("create_contact")), 1)

    def test_a_stored_contact_deleted_in_desk_is_forgotten_and_found_again(self):
        first = self.live()
        self.send(first)
        deleted = self.contact_of(first)
        del self.desk.contacts[deleted]
        second = self.live()
        self.send(second)
        self.assertNotEqual(self.contact_of(second), deleted)
        self.assertEqual(self.saved(second)["state"], "sent")
        self.assertEqual(self.desk.tickets[self.ticket_of(second)]["contactId"], self.contact_of(second))

    def test_a_contact_create_that_was_never_answered_searches_again_first(self):
        ref = self.live()
        self.desk.fail("create_contact", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.send(ref)
        made = [c for c in self.desk.contacts if c not in (TEST_CONTACT, UNVERIFIED_CONTACT)]
        self.desk.contacts[made[0]]["phone"] = PHONE  # Zoho's search now finds it
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(self.contact_of(ref), made[0])
        self.assertEqual(len(self.calls("create_contact")), 1)
        self.assertIsNone(self.saved(ref)["intent"])


class TicketTests(WorkerCase):
    """Step 2. A timeout after the ticket was made must never become two."""

    def test_a_timeout_after_the_ticket_was_made_finds_it_and_makes_no_second(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.assertTrue(self.send(ref))
        saved = self.saved(ref)
        self.assertEqual((saved["state"], saved["intent"]["step"]), ("waiting", "ticket"))
        self.assertIsNone(saved["zoho"]["ticket_id"])
        self.clock.advance(30)
        self.assertTrue(self.worker.run_once())
        [ticket_id] = self.desk.tickets
        self.assertEqual(self.ticket_of(ref), ticket_id)
        self.assertEqual(self.saved(ref)["state"], "sent")
        self.assertEqual(len(self.calls("create_ticket")), 1)
        self.assertEqual(len(self.calls("contact_tickets")), 1)
        [adopted] = self.events("zoho_ticket_adopted")
        self.assertEqual((adopted["reference"], adopted["zoho_number"]), (ref, "#1000"))

    def test_a_timeout_before_it_was_made_looks_then_makes_it_once(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        self.send(ref)
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.desk.tickets), 1)
        self.assertEqual([c[0] for c in self.desk.calls if c[0] in ("contact_tickets", "create_ticket")],
                         ["create_ticket", "contact_tickets", "create_ticket"])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_crash_after_the_intent_was_saved_looks_before_making_it(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", RuntimeError("process died"))
        with self.assertRaises(RuntimeError):
            self.send(ref)
        self.assertEqual(self.saved(ref)["intent"]["step"], "ticket")
        self.clock.advance(301)  # the dead pass's lease runs out
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.desk.tickets), 1)
        self.assertEqual(len(self.calls("contact_tickets")), 1)

    def test_a_crash_after_the_ticket_was_made_adopts_it(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", RuntimeError("process died"), after=True)
        with self.assertRaises(RuntimeError):
            self.send(ref)
        self.clock.advance(301)
        self.worker.run_once()
        self.assertEqual(len(self.desk.tickets), 1)
        self.assertEqual(self.ticket_of(ref), next(iter(self.desk.tickets)))

    def test_another_records_ticket_on_the_same_contact_is_never_adopted(self):
        started = self.chat()
        first = self.record(started_at=started)
        self.send(first)
        second = self.record(started_at=started)
        self.desk.fail("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        self.send(second)
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.desk.tickets), 2)
        self.assertNotEqual(self.ticket_of(second), self.ticket_of(first))


class RealClientTests(WorkerCase):
    """The real DeskClient over the opener-level fake (tests/fake_zoho.py):
    a timeout after the ticket was made ends in exactly one Zoho ticket,
    found by the contact's ticket list."""

    TICKET_ID = "4000000528005"

    def setUp(self):
        super().setUp()
        self.zoho = FakeZoho()
        transport = DeskHTTP(opener=self.zoho)
        real = zoho_settings()
        client = DeskClient(real, TokenSource(real, transport, clock=lambda: 1000.0), transport)
        self.worker = ZohoWorker(self.tickets, client, self.conversations, self.bucket, real, self.log,
                                 clock=self.clock)

    def posts(self, path):
        return [r for r in self.zoho.desk_requests if r["method"] == "POST" and r["path"] == path]

    def timed_out_create(self):
        """A record whose first create timed out after it was sent. Returns
        the reference and the subject that create sent."""
        ref = self.record(started_at=self.chat())
        self.zoho.queue(socket.timeout("timed out"))
        self.assertTrue(self.send(ref))
        self.assertEqual(self.saved(ref)["intent"]["step"], "ticket")
        [sent] = self.posts("/api/v1/tickets")
        return ref, json.loads(sent["body"])["subject"]

    def test_a_timeout_after_the_ticket_was_made_adopts_it_from_the_contacts_list(self):
        ref, subject = self.timed_out_create()
        made = {"id": self.TICKET_ID, "ticketNumber": "1024", "subject": subject,
                "createdTime": "2026-10-05T10:00:01.000Z", "webUrl": "https://desk.zoho.in/x/" + self.TICKET_ID}
        self.zoho.queue(Answer(200, {"data": [made]}), Answer(200, {"id": "4000000529001"}))
        self.clock.advance(30)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.posts("/api/v1/tickets")), 1)
        listed = [r for r in self.zoho.desk_requests if r["method"] == "GET"]
        self.assertEqual([r["path"] for r in listed], ["/api/v1/contacts/%s/tickets" % FAKE_ZOHO_CONTACT])
        saved = self.saved(ref)
        self.assertEqual((saved["state"], saved["zoho"]["ticket_id"], saved["zoho"]["ticket_number"]),
                         ("sent", self.TICKET_ID, "1024"))
        self.assertEqual([e["zoho_number"] for e in self.events("zoho_ticket_adopted")], ["#1024"])

    def test_a_ticket_with_the_same_reference_made_before_the_record_is_not_adopted(self):
        # A reference comes round again when a database is started afresh.
        ref, subject = self.timed_out_create()
        old = {"id": "4000000400001", "ticketNumber": "17", "subject": subject,
               "createdTime": "2026-10-04T09:00:00.000Z"}
        made = {"id": self.TICKET_ID, "ticketNumber": "1025", "webUrl": "https://desk.zoho.in/x/" + self.TICKET_ID}
        self.zoho.queue(Answer(200, {"data": [old]}), Answer(200, made), Answer(200, {"id": "4000000529001"}))
        self.clock.advance(30)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.posts("/api/v1/tickets")), 2)
        self.assertEqual(self.saved(ref)["zoho"]["ticket_id"], self.TICKET_ID)
        self.assertEqual(self.events("zoho_ticket_adopted"), [])


class TranscriptTests(WorkerCase):
    def test_the_transcript_goes_once_then_only_the_new_turns(self):
        ref = self.record(started_at=self.chat(turns=[("my battery won't charge", "Try another socket."),
                                                      ("still nothing", "Raising a ticket.")]))
        self.send(ref)
        [first] = self.comments(ref)
        self.assertIn("still nothing", first["content"])
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2, 3, 4])
        self.clock.advance(60)
        self.chat(turns=[("thank you", "You're welcome.")])
        self.send(ref)
        later = self.comments(ref)
        self.assertEqual(len(later), 2)
        self.assertIn("thank you", later[1]["content"])
        self.assertNotIn("still nothing", later[1]["content"])
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2, 3, 4, 5, 6])

    def test_a_long_transcript_goes_in_comments_under_the_limit_each_turn_once(self):
        ref = self.record(started_at=self.chat(turns=[("a" * 9000, "b" * 9000)] * 4))
        self.send(ref)
        comments = self.comments(ref)
        self.assertGreater(len(comments), 1)
        self.assertTrue(all(len(c["content"]) <= 30000 for c in comments))
        self.assertEqual(sum(c["content"].count("a" * 9000) for c in comments), 4)
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), list(range(1, 9)))

    def test_a_comment_whose_answer_was_lost_is_not_posted_twice(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("add_comment", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.send(ref)
        self.assertEqual(self.saved(ref)["state"], "waiting")
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.comments(ref)), 1)
        self.assertEqual(len(self.calls("comments")), 1)
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_marker_quoted_inside_another_comment_does_not_count_as_posted(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("add_comment", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        self.send(ref)
        marker = "[stage:%s transcript, turns 1-2]" % ref
        self.desk.comment_log[self.ticket_of(ref)] = [
            {"id": "comment-typed", "content": "A note that quotes %s in its body." % marker}]
        self.clock.advance(30)
        self.worker.run_once()
        self.assertTrue(any(c["content"].startswith(marker) for c in self.comments(ref)))
        self.assertEqual(self.saved(ref)["zoho"]["comment_ids"], [self.comments(ref)[-1]["id"]])

    def test_each_persons_ticket_gets_only_their_own_run(self):
        first_started = self.chat(turns=[("first person's words", "Ok.")])
        self.photo("upl_first", "cluster-1", self.clock())
        first = self.record(started_at=first_started, cluster_id="cluster-1")
        self.clock.advance(60)
        second_started = self.restart("second person's words", "Hello.")
        self.tickets.close_runs("conv-1", second_started)
        self.photo("upl_second", "cluster-2", self.clock())
        second = self.record(started_at=second_started, cluster_id="cluster-2")
        self.clock.advance(10)
        self.chat(turns=[("more from the second person", "Noted.")])
        self.send(first)
        self.send(second)
        self.assertIn("first person's words", self.text_on(first))
        self.assertNotIn("second person", self.text_on(first))
        self.assertIn("second person's words", self.text_on(second))
        self.assertIn("more from the second person", self.text_on(second))
        self.assertNotIn("first person", self.text_on(second))
        self.assertEqual(self.files(first), ["%s-upl_first.jpg" % first])
        self.assertEqual(self.files(second), ["%s-upl_second.jpg" % second])

    def test_a_photo_from_another_cluster_in_the_same_run_is_not_attached(self):
        started = self.chat()
        self.photo("upl_mine", "cluster-1", self.clock())
        self.photo("upl_other", "cluster-9", self.clock())
        ref = self.record(started_at=started, cluster_id="cluster-1")
        self.send(ref)
        self.assertEqual(self.files(ref), ["%s-upl_mine.jpg" % ref])

    def test_a_time_written_without_microseconds_still_counts_inside_the_run(self):
        # conversation.utc_now_iso drops the microseconds when they are zero.
        # Compared as text, "10:00:00+00:00" sorts before the run's start.
        started = self.chat()
        self.photo("upl_whole_second", "cluster-1", "2026-10-05T10:00:00+00:00")
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.files(ref), ["%s-upl_whole_second.jpg" % ref])


class RunEndTests(WorkerCase):
    """The run's end is exclusive, and it is read again just before anything
    is posted: a newcomer's turn can close the run while a pass is under way
    (runtime._end_run_before_this_turn)."""

    def test_a_turn_or_file_at_or_after_the_runs_end_is_never_posted(self):
        started = self.chat(turns=[("before the end", "Ok.")])
        ref = self.record(started_at=started)
        self.clock.advance(5)
        end = self.clock()
        self.photo("upl_just_before", "cluster-1", plus(end, -0.000001))
        self.photo("upl_at_the_end", "cluster-1", end)
        self.chat(turns=[("exactly at the end", "Ok.")])
        self.clock.advance(5)
        self.chat(turns=[("after the end", "Ok.")])
        self.tickets.close_runs("conv-1", end)
        self.send(ref)
        said = self.text_on(ref)
        self.assertIn("before the end", said)
        self.assertNotIn("exactly at the end", said)
        self.assertNotIn("after the end", said)
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2])
        self.assertEqual(self.files(ref), ["%s-upl_just_before.jpg" % ref])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_run_closed_while_the_worker_sends_takes_none_of_the_newcomers_turns(self):
        ref = self.record(started_at=self.chat())

        def newcomer_writes():
            # Their turn is recorded, then their turn's end closes the run.
            self.clock.advance(10)
            self.chat(turns=[("someone else's words", "Hello.")])
            self.tickets.close_runs("conv-1", self.clock())

        self.desk.hooks["create_ticket"] = newcomer_writes
        self.assertTrue(self.send(ref))
        self.assertNotIn("someone else", self.text_on(ref))
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_run_closed_between_two_comments_posts_none_of_the_rest(self):
        ref = self.record(started_at=self.chat(turns=[("a" * 14000, "b" * 14000)]))
        self.clock.advance(10)
        newcomer_at = self.clock()
        # The newcomer's turn is recorded before their turn's end closes the run.
        self.chat(turns=[("c" * 14000, "d" * 14000)])
        self.desk.hooks["add_comment"] = lambda: self.tickets.close_runs("conv-1", newcomer_at)
        self.send(ref)
        self.assertEqual(len(self.comments(ref)), 1)
        self.assertNotIn("c" * 100, self.text_on(ref))
        self.assertEqual(sorted(self.saved(ref)["posted_turns"]), [1, 2])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_run_closed_between_two_files_attaches_none_of_the_newcomers(self):
        started = self.chat()
        self.photo("upl_mine", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.clock.advance(10)
        newcomer_at = self.clock()
        # The same browser, so the same cluster.
        self.photo("upl_theirs", "cluster-1", newcomer_at)
        self.desk.hooks["upload_attachment"] = lambda: self.tickets.close_runs("conv-1", newcomer_at)
        self.send(ref)
        self.assertEqual(self.files(ref), ["%s-upl_mine.jpg" % ref])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_record_with_no_run_start_takes_nothing_from_the_conversation(self):
        self.chat()
        self.photo("upl_any", "cluster-1", self.clock())
        ref = self.record(started_at=None)
        self.send(ref)
        self.assertEqual((self.comments(ref), self.files(ref)), ([], []))
        self.assertEqual(self.saved(ref)["state"], "sent")


class NoteTests(WorkerCase):
    def test_each_note_is_posted_once_with_its_marker(self):
        ref = self.record(started_at=self.chat())
        self.tickets.add_note(ref, "Customer asked for a person at 10:00", self.clock())
        self.assertTrue(self.worker.run_once())
        marker = "[stage:%s note 1]" % ref
        [note] = [c["content"] for c in self.comments(ref) if marker in c["content"]]
        self.assertIn("Customer asked for a person", note)
        self.assertEqual(self.saved(ref)["posted_notes"], [0])
        self.clock.advance(60)
        self.tickets.add_note(ref, "Customer sent another safety report", self.clock())
        self.assertTrue(self.worker.run_once())
        notes = [c["content"] for c in self.comments(ref) if "[stage:%s note " % ref in c["content"]]
        self.assertEqual(len(notes), 2)
        self.assertIn("another safety report", notes[1])
        self.assertEqual(sorted(self.saved(ref)["posted_notes"]), [0, 1])


class AttachmentTests(WorkerCase):
    def test_each_photo_is_attached_once_named_by_the_reference(self):
        started = self.chat()
        self.photo("upl_one", "cluster-1", self.clock())
        self.photo("upl_two", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(sorted(self.files(ref)), ["%s-upl_one.jpg" % ref, "%s-upl_two.jpg" % ref])
        self.clock.advance(60)
        self.chat(turns=[("thanks", "You're welcome.")])
        self.send(ref)
        self.assertEqual(len(self.calls("upload_attachment")), 2)
        self.assertEqual(len(self.saved(ref)["zoho"]["attachment_ids"]), 2)

    def test_a_file_over_the_limit_is_never_read_and_a_comment_says_so(self):
        started = self.chat()
        key = self.photo("upl_big", "cluster-1", self.clock(), size=25 * MB, kind="video")
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.bucket.reads, [])
        self.assertEqual(self.calls("upload_attachment"), [])
        self.assertIn(key, self.saved(ref)["posted_media"])
        [said] = [c["content"] for c in self.comments(ref) if "too large" in c["content"]]
        self.assertIn("A video", said)
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_file_zoho_refuses_as_too_large_gets_the_same_comment(self):
        started = self.chat()
        key = self.photo("upl_refused", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.desk.fail("upload_attachment", zoho_error(errors.ZohoTooLarge, "RESOURCE_SIZE_EXCEEDED"))
        self.send(ref)
        self.assertEqual(self.files(ref), [])
        self.assertIn(key, self.saved(ref)["posted_media"])
        self.assertEqual(len([c for c in self.comments(ref) if "too large" in c["content"]]), 1)
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_file_whose_answer_was_lost_is_not_uploaded_twice(self):
        started = self.chat()
        self.photo("upl_once", "cluster-1", self.clock())
        ref = self.record(started_at=started)
        self.desk.fail("upload_attachment", zoho_error(errors.ZohoUnknownOutcome, "timeout"), after=True)
        self.send(ref)
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(len(self.calls("upload_attachment")), 1)
        self.assertEqual(len(self.calls("attachments")), 1)
        self.assertEqual(self.files(ref), ["%s-upl_once.jpg" % ref])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_file_gone_from_storage_gets_a_comment_and_is_never_retried(self):
        started = self.chat()
        key = self.photo("upl_deleted", "cluster-1", self.clock())
        del self.bucket.objects[key]
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.calls("upload_attachment"), [])
        self.assertIn(key, self.saved(ref)["posted_media"])
        [said] = [c["content"] for c in self.comments(ref) if "could not be read" in c["content"]]
        self.assertIn("A photo", said)
        self.assertNotIn("customers/", said)
        self.assertEqual(self.saved(ref)["state"], "sent")
        self.clock.advance(60)
        self.chat(turns=[("thanks", "You're welcome.")])
        self.send(ref)
        self.assertEqual(self.bucket.reads, [key])

    def test_storage_that_fails_goes_on_the_schedule_and_the_file_waits(self):
        started = self.chat()
        key = self.photo("upl_later", "cluster-1", self.clock())
        self.bucket.broken.add(key)
        ref = self.record(started_at=started)
        self.send(ref)
        saved = self.saved(ref)
        self.assertNotIn(key, saved["posted_media"])
        self.assertEqual((saved["state"], saved["last_error"], saved["next_attempt_at"]),
                         ("waiting", "StorageError", plus(self.clock(), 30)))
        self.assertEqual([c for c in self.comments(ref) if "could not be read" in c["content"]], [])
        self.bucket.broken.clear()
        self.clock.advance(30)
        self.worker.run_once()
        self.assertEqual(self.files(ref), ["%s-upl_later.jpg" % ref])
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_without_a_media_bucket_the_ticket_goes_without_files(self):
        started = self.chat()
        self.photo("upl_unreadable", "cluster-1", self.clock())
        self.worker = ZohoWorker(self.tickets, self.desk, self.conversations, None, settings(), self.log,
                                 clock=self.clock)
        ref = self.record(started_at=started)
        self.send(ref)
        self.assertEqual(self.files(ref), [])
        self.assertEqual(self.saved(ref)["state"], "sent")


class AnswerTests(WorkerCase):
    """Zoho's answers, sorted by error code (the table in spec section 4)."""

    def fail_once(self, method, exc):
        ref = self.record(started_at=self.chat())
        self.desk.fail(method, exc)
        self.assertTrue(self.send(ref))
        return ref

    def test_too_many_requests_waits_thirty_seconds_and_counts_no_attempt(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoBusy, "TOO_MANY_REQUESTS"))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["attempts"]), (plus(self.clock(), 30), 0))

    def test_an_access_token_refused_twice_goes_on_the_schedule(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoAuthExpired, "INVALID_OAUTH"))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["attempts"], saved["last_error"]),
                         (plus(self.clock(), 30), 1, "ZohoAuthExpired:INVALID_OAUTH"))

    def test_our_payload_refused_retries_hourly_and_names_the_fields(self):
        ref = self.fail_once("create_ticket",
                             zoho_error(errors.ZohoRejected, "INVALID_DATA", fields=["contactId"]))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["last_error"]),
                         (plus(self.clock(), 3600), "ZohoRejected:INVALID_DATA"))
        [rejected] = self.events("zoho_rejected")
        self.assertEqual((rejected["reference"], rejected["fields"]), (ref, ["contactId"]))

    def test_a_configuration_fault_retries_hourly_and_shows_on_health(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoConfigError, "SCOPE_MISMATCH"))
        self.assertEqual(self.saved(ref)["next_attempt_at"], plus(self.clock(), 3600))
        self.assertEqual(self.worker.status["failing"], "sending failing: SCOPE_MISMATCH")
        self.assertEqual([e["error"] for e in self.events("zoho_rejected")], ["ZohoConfigError:SCOPE_MISMATCH"])
        self.clock.advance(3600)
        self.worker.run_once()
        self.assertEqual(self.saved(ref)["state"], "sent")
        self.assertIsNone(self.worker.status["failing"])

    def test_a_refused_refresh_token_retries_hourly_and_is_alarmed(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoTokenRefused, "invalid_client_secret"))
        self.assertEqual(self.saved(ref)["next_attempt_at"], plus(self.clock(), 3600))
        self.assertEqual(self.worker.status["failing"], "token refused: invalid_client_secret")
        [refused] = self.events("zoho_token_refused")
        self.assertEqual((refused["level"], refused["error"]), ("error", "ZohoTokenRefused:invalid_client_secret"))

    def test_a_throttled_token_endpoint_waits_ten_minutes(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoTokenThrottled, "Access Denied"))
        self.assertEqual(self.saved(ref)["next_attempt_at"], plus(self.clock(), 600))

    def test_an_unknown_outcome_goes_on_the_schedule_with_its_intent_kept(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoUnknownOutcome, "timeout"))
        saved = self.saved(ref)
        self.assertEqual((saved["next_attempt_at"], saved["intent"]["step"]), (plus(self.clock(), 30), "ticket"))

    def test_the_days_credits_gone_pause_every_record_until_zoho_says(self):
        started = self.chat()
        first, second = self.record(started_at=started), self.record(started_at=started)
        self.desk.fail("create_ticket", zoho_error(errors.ZohoCreditsExhausted, "THRESHOLD_EXCEEDED",
                                                   retry_after_seconds=1200))
        self.wake(first)
        self.wake(second)
        self.assertTrue(self.worker.run_once())
        self.assertFalse(self.worker.run_once())
        self.clock.advance(1199)
        self.assertFalse(self.worker.run_once())
        self.clock.advance(1)
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        self.assertEqual({self.saved(first)["state"], self.saved(second)["state"]}, {"sent"})
        self.assertEqual(len(self.desk.tickets), 2)

    def test_a_ticket_deleted_in_desk_is_gone_and_never_retried(self):
        ref = self.record(started_at=self.chat())
        self.send(ref)
        del self.desk.tickets[self.ticket_of(ref)]
        self.clock.advance(60)
        self.chat(turns=[("hello?", "I'm here.")])
        self.send(ref)
        self.assertEqual(self.saved(ref)["state"], "gone")
        [gone] = self.events("zoho_ticket_gone")
        self.assertEqual((gone["reference"], gone["zoho_number"]), (ref, "#1000"))
        self.clock.advance(3600)
        self.wake(ref)
        self.assertFalse(self.worker.run_once())

    def test_only_the_alarmed_events_carry_an_error_level(self):
        ref = self.fail_once("create_ticket", zoho_error(errors.ZohoRejected, "INVALID_DATA", fields=["subject"]))
        self.clock.advance(3600)
        self.worker.run_once()
        names = {e["event"] for e in self.log.events}
        self.assertTrue({"zoho_rejected", "zoho_retry", "zoho_ticket_sent"} <= names)
        for event in self.log.events:
            if event["event"] in NOT_ALARMED:
                self.assertNotIn("level", event)
        self.assertEqual(self.saved(ref)["state"], "sent")


class RetryTests(WorkerCase):
    def test_the_schedule(self):
        self.assertEqual([retry_wait(n) for n in range(1, 9)], [30, 60, 120, 300, 600, 1800, 3600, 3600])

    def test_the_waits_grow_then_settle_hourly_and_nothing_is_dropped(self):
        ref = self.record(started_at=self.chat())
        self.desk.down = True
        self.clock.advance(120)
        for _ in range(8):
            self.assertTrue(self.worker.run_once())
            self.clock.now = self.saved(ref)["next_attempt_at"]
        self.assertEqual([e["wait_seconds"] for e in self.events("zoho_retry")],
                         [30, 60, 120, 300, 600, 1800, 3600, 3600])
        self.assertEqual(self.saved(ref)["attempts"], 8)
        self.desk.down = False
        self.assertTrue(self.worker.run_once())
        self.assertEqual((self.saved(ref)["state"], self.saved(ref)["attempts"]), ("sent", 0))
        self.assertEqual(len(self.desk.tickets), 1)


class OverdueTests(WorkerCase):
    def test_a_safety_ticket_is_late_at_ten_minutes_even_with_a_turn_every_minute(self):
        ref = self.record(started_at=self.chat(), kind="safety", category="battery_safety")
        self.desk.down = True
        for _ in range(9):
            self.clock.advance(60)
            self.chat(turns=[("it is still hot", "Please keep away from it.")])
            self.wake(ref)
            self.worker.run_once()
            self.worker.check_overdue()
        self.assertEqual(self.events("safety_ticket_late"), [])
        self.clock.advance(61)
        self.worker.check_overdue()
        [late] = self.events("safety_ticket_late")
        self.assertEqual((late["reference"], late["level"]), (ref, "error"))
        self.assertGreaterEqual(late["age_seconds"], 600)
        self.assertEqual(self.saved(ref)["state"], "stuck")
        self.clock.advance(60)
        self.worker.check_overdue()
        self.assertEqual(len(self.events("safety_ticket_late")), 1)
        self.clock.advance(3600)
        self.worker.check_overdue()
        self.assertEqual(len(self.events("safety_ticket_late")), 2)

    def test_any_other_ticket_is_stuck_after_a_day_and_is_still_retried(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(86_399)
        self.worker.check_overdue()
        self.assertEqual(self.events("zoho_ticket_stuck"), [])
        self.clock.advance(2)
        self.worker.check_overdue()
        [stuck] = self.events("zoho_ticket_stuck")
        self.assertEqual((stuck["reference"], stuck["level"]), (ref, "error"))
        self.assertEqual(self.saved(ref)["state"], "stuck")
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.saved(ref)["state"], "sent")

    def test_a_record_under_lease_is_still_marked_stuck(self):
        ref = self.record(started_at=self.chat())
        self.clock.advance(86_401)
        self.assertIsNotNone(self.tickets.take_due(self.clock(), "test", 300, "another-worker"))
        self.worker.check_overdue()
        self.assertEqual(self.saved(ref)["state"], "stuck")
        self.assertEqual(self.saved(ref)["lease_token"], "another-worker")


class SecretTests(WorkerCase):
    def test_no_secret_or_phone_reaches_the_log_and_no_secret_the_record(self):
        ref = self.record(started_at=self.chat())
        self.desk.fail("create_ticket", zoho_error(errors.ZohoTokenRefused, "invalid_client_secret"))
        self.send(ref)
        self.clock.advance(3600)
        self.worker.run_once()
        logged = json.dumps(self.log.events)
        stored = json.dumps(self.saved(ref), default=str)
        for secret in ("secret-test", "refresh-test"):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, stored)
        self.assertNotIn("9999999999", logged)


class LoopTests(WorkerCase):
    def test_the_thread_sends_what_is_due_and_stops(self):
        ref = self.record(started_at=self.chat())
        self.wake(ref)
        self.worker.pass_seconds = 0.01
        self.worker.start()
        try:
            self.assertTrue(wait_for(lambda: self.saved(ref)["state"] == "sent"))
            self.assertTrue(self.worker.status["running"])
        finally:
            self.worker.stop()
        self.assertFalse(self.worker.status["running"])
        self.assertIsNotNone(self.worker.status["last_pass_at"])
        self.assertFalse([t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()])

    def test_a_pass_that_fails_is_logged_by_class_and_the_loop_carries_on(self):
        class Broken(Exception):
            pass

        def take_due(*args, **kwargs):
            raise Broken("boom with details")

        self.worker.store = SimpleNamespace(take_due=take_due, overdue=lambda now, mode: [])
        self.worker.pass_seconds = 0.01
        self.worker.start()
        try:
            self.assertTrue(wait_for(lambda: len(self.events("zoho_worker_error")) >= 2))
        finally:
            self.worker.stop()
        failures = self.events("zoho_worker_error")
        self.assertEqual({(e["error"], e["level"]) for e in failures}, {("Broken", "error")})
        self.assertNotIn("boom", json.dumps(failures))


if __name__ == "__main__":
    unittest.main()
