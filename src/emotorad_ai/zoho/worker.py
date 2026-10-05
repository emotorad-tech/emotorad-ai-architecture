"""The Zoho worker (spec 2026-10-05 section 4). It sends recorded tickets to
Zoho Desk after the customer has their reply.

A daemon thread in the API process. api.py's lifespan starts it, and nothing
starts it at import. It takes one due record at a time under a five-minute
lease and runs the steps in order: the contact, the ticket, this run's
transcript, the notes, then this run's photos and videos. Each step is saved
before the next, so a retry starts where the last one stopped.

Before every write to Zoho it saves what it is about to do (`intent`), and
sends nothing if that save fails. A later pass that finds an intent with no
result checks Zoho before it writes again: the contact's tickets for a
ticket, the ticket's comments for a comment, its attachments for a file. This
is how a timeout after Zoho made the ticket still ends in one ticket, not two.

Only the run's own turns and files go on its ticket: at or after the run's
start, before its end. A file a customer's turn carried is judged by that
turn alone: the API stores a file before its turn runs, so one sent with a
run's first message is older than the run, and belongs to the run its turn
is in. Only a file no turn names is judged by when it was stored. The end can
be set while a pass is under way, when someone else writes on the same browser
(runtime._end_run_before_this_turn), so it is read again just before anything
is posted.

Failures are sorted by Zoho's error code, not by the HTTP status alone (the
answers table in section 4). Nothing is ever dropped. A record with work
outstanding is retried on the schedule below for as long as it takes. Once a
record is late (10 minutes for an urgent one, a day for any other), it is
marked stuck and logged at error level once an hour.

The log carries references, Zoho's ticket number (as "#1024", since a bare
number is hidden as a code), error classes and Zoho's error codes. It never
carries a phone, a token, a request or a response body.
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from ..conversation import StoreUnavailable
from ..storage.s3 import StorageError
from ..tickets.clock import now_iso, parse, plus
from ..tickets.record import GONE, SENT, STUCK, WAITING
from ..tickets.store import age_seconds
from .desk import find_adoptable, safe_filename
from .errors import (
    ZohoBusy,
    ZohoConfigError,
    ZohoCreditsExhausted,
    ZohoError,
    ZohoGone,
    ZohoRejected,
    ZohoTokenRefused,
    ZohoTokenThrottled,
    ZohoTooLarge,
    ZohoUnknownOutcome,
)
from .payload import plus91, ticket_payload, transcript_chunks

THREAD_NAME = "zoho-worker"
# How often the loop looks for due records when nothing wakes it.
PASS_SECONDS = 30.0
# How long a taken record belongs to this worker. The lease is renewed before
# every Zoho call, so it runs out only when a worker has died.
LEASE_SECONDS = 300.0
# The wait after the first, second, ... failure in a row. Hourly after the last.
RETRY_WAITS = (30, 60, 120, 300, 600, 1800)
HOURLY = 3600
# The least wait after a ticket create of unknown outcome, before the look at
# the contact's ticket list.
TICKET_LOOKUP_WAIT = 120
# Zoho's 429 TOO_MANY_REQUESTS is about calls in flight, not the day's credits.
BUSY_WAIT = 30
# The token endpoint throttles for ten minutes, and auth.py sends no request
# until then.
TOKEN_THROTTLED_WAIT = 600
# How long a record that is not urgent waits while credits are below the floor.
# It is also how long a reading below the floor counts. Only Zoho's answers
# carry the reading, so after this one record that is not urgent goes out, and
# its calls read the credits again (Zoho's daily reset is seen this way).
CREDITS_FLOOR_WAIT = 600
# A late or stuck record is logged at most this often.
OVERDUE_LOG_SECONDS = 3600
# How long stop() waits for a pass in progress. The thread is a daemon either way.
STOP_JOIN_SECONDS = 10.0
# The contact's last name when the OMS record gave no name (step 1).
UNNAMED_CONTACT = "AI chat customer"
_WHAT = {"image": "A photo", "video": "A video", "document": "A document"}
# How a turn names a file the API stored: "s3://" and the key (api.py).
_S3 = "s3://"
_COMMENT_STEPS = ("transcript", "note", "media_note")


def retry_wait(attempts: int) -> int:
    """Seconds before the next try after `attempts` failures in a row."""
    if 1 <= attempts <= len(RETRY_WAITS):
        return RETRY_WAITS[attempts - 1]
    return HOURLY


def _moment(text: Any) -> Optional[datetime]:
    """An ISO time as an aware datetime, or None. Times are parsed, never
    compared as text: conversation.utc_now_iso drops the microseconds when they
    are zero, and "10:00:00+00:00" sorts before "10:00:00.000000+00:00"."""
    if not text:
        return None
    try:
        return parse(str(text))
    except ValueError:
        return None


def _folded(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def _names(contact: Dict[str, Any]) -> Set[str]:
    """A Zoho contact's name, as the last name alone (how this worker makes
    contacts) and as first and last (how a person might)."""
    first, last = contact.get("firstName") or "", contact.get("lastName") or ""
    return {name for name in (_folded(last), _folded("%s %s" % (first, last))) if name}


def _file_name(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _error_name(exc: BaseException) -> str:
    """The error class and Zoho's code. This is all that is logged or kept."""
    code = getattr(exc, "error", None)
    return "%s:%s" % (type(exc).__name__, code) if code else type(exc).__name__


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


class _Dropped(Exception):
    """A save matched nothing: the record was erased, or another worker holds it."""


class _Job:
    """One taken record, as this pass sees it."""

    def __init__(self, record: Dict[str, Any], token: str) -> None:
        # The pass's own copy, kept in step with each save that lands.
        self.record = copy.deepcopy(record)
        self.token = token
        self.ref: str = record["_id"]
        self.wake = record.get("wake", 0)
        self.attempts = int(record.get("attempts") or 0)
        self.zoho: Dict[str, Any] = copy.deepcopy(record.get("zoho") or {})

    @property
    def intent(self) -> Dict[str, Any]:
        return self.record.get("intent") or {}

    @property
    def conversation_id(self) -> str:
        return self.record.get("conversation_id") or "zoho"

    @property
    def zoho_number(self) -> Optional[str]:
        # "#1024": a bare run of digits is hidden in the log as a one-time code.
        number = self.zoho.get("ticket_number")
        return "#%s" % number if number else None


class ZohoWorker:
    """Sends due ticket records to Zoho Desk, one at a time, under a lease."""

    def __init__(
        self,
        store: Any,
        client: Any,
        conversations: Any,
        media_reader: Any,
        settings: Any,
        log: Any,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self.store = store
        self.client = client
        self.conversations = conversations
        # S3Store, or None on a deployment with no media bucket. With none, no
        # photo or video was kept, so there is nothing to attach.
        self.media_reader = media_reader
        self.settings = settings
        self.log = log
        self._clock = clock
        # Read by the loop on every wait. The tests shorten it.
        self.pass_seconds = PASS_SECONDS
        # For /health. `failing` is set while Zoho refuses our token or our
        # configuration, and cleared when the next record is sent.
        self.status: Dict[str, Any] = {"running": False, "last_pass_at": None, "failing": None}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Set by Zoho's THRESHOLD_EXCEEDED. No record is taken before this time.
        self._paused_until: Optional[str] = None
        # When this worker last called Zoho: the last time the credits
        # reading (DeskHTTP.last_credits_remaining) can have been refreshed.
        self._credits_read_at: Optional[str] = None
        # Reference -> when it was last logged as late or stuck.
        self._overdue_logged: Dict[str, str] = {}

    # -- life -------------------------------------------------------------

    def start(self) -> None:
        """Start the loop. Called by api.py's lifespan and nowhere else. Safe to call twice."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self.status["running"] = True
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to end, and wait briefly for a pass in progress."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(STOP_JOIN_SECONDS)
        self.status["running"] = False

    def wake(self) -> None:
        """A turn ended or a note came in: look now, not at the next pass."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Cleared before the pass, so a wake during the pass brings the
            # next pass forward.
            self._wake.clear()
            try:
                self.check_overdue()
            except StoreUnavailable:
                self._store_unavailable()
            except Exception as exc:
                self._worker_error(exc)
            try:
                while not self._stop.is_set() and self.run_once():
                    pass
                self.status["last_pass_at"] = self._clock()
            except Exception as exc:
                self._worker_error(exc)
            # stop() sets the flag before the wake. A stop that came just
            # before the clear above lost its wake, but not its flag.
            if self._stop.is_set():
                break
            self._wake.wait(self.pass_seconds)

    def _worker_error(self, exc: BaseException) -> None:
        # The class only, never the message: a driver's text can include what
        # it failed on.
        self.log.emit("zoho_worker_error", "zoho", level="error", error=type(exc).__name__)

    def _store_unavailable(self) -> None:
        # An Atlas blip is not the worker's fault, so it is not the alarmed
        # zoho_worker_error. The store's own alarms cover a long outage, and
        # the next pass tries again.
        self.log.emit("zoho_worker_store_unavailable", "zoho", level="warning")

    # -- one record ---------------------------------------------------------

    def run_once(self) -> bool:
        """Take one due record and send what it has outstanding. True if one was taken.

        A store that cannot be reached (the tickets or the conversations) ends
        the pass with False. A record already taken keeps its lease, which
        lapses in five minutes, and is then taken again."""
        try:
            return self._take_and_send()
        except StoreUnavailable:
            self._store_unavailable()
            return False

    def _take_and_send(self) -> bool:
        now = self._clock()
        if self._paused_until is not None:
            if now < self._paused_until:
                return False
            self._paused_until = None
        token = uuid.uuid4().hex
        record = self.store.take_due(now, self.settings.mode, LEASE_SECONDS, token)
        if record is None:
            return False
        job = _Job(record, token)
        try:
            if not job.record.get("urgent") and self._credits_low():
                self._defer_for_credits(job)
            else:
                self._send(job)
        except _Dropped:
            self.log.emit("zoho_record_dropped", job.conversation_id, reference=job.ref)
        except (ZohoError, StorageError) as exc:
            self._failed(job, exc)
        return True

    def _send(self, job: _Job) -> None:
        """The steps in order, each saved before the next."""
        if not job.zoho.get("contact_id"):
            self._contact(job)
        if not job.zoho.get("ticket_id"):
            self._ticket(job)
        self._settle_intent(job)
        self._transcript(job)
        self._notes(job)
        self._attachments(job)
        self._finish(job)

    # -- step 1: the contact --------------------------------------------------

    def _contact(self, job: _Job, use_stored: bool = True) -> None:
        record = job.record
        if record.get("mode") != "live":
            # Contacts belong to the whole organisation, not to a department,
            # so a test never searches for a contact or makes one.
            contact_id = self.settings.test_contact_id
        elif not self._searches_contacts(job):
            # A number nobody proved never goes on a real customer's contact.
            contact_id = self.settings.unverified_contact_id
        else:
            # Our own first: the store returns a contact only from a live,
            # verified record of this number, never a test or unverified one.
            stored = self.store.contact_for(record["phone"]) if use_stored else None
            contact_id = stored or self._find_or_create_contact(job)
        job.zoho["contact_id"] = contact_id
        changes: Dict[str, Any] = {"zoho": copy.deepcopy(job.zoho)}
        if job.intent.get("step") == "contact":
            changes["intent"] = None
        self._save(job, changes)

    def _find_or_create_contact(self, job: _Job) -> str:
        """Search by phone, then by mobile, in separate calls. One match is
        used. Of several, the one with the name on the OMS record is used.
        Otherwise a new contact is made. A create that never got an answer
        (a crash or a timeout) comes back here, so the search runs again
        before any second create."""
        record = job.record
        mobile = plus91(record.get("phone")) or ""
        last_ten = mobile[-10:]
        matches = list(self._call(job, self.client.search_contacts, "phone", last_ten) or [])
        if not matches:
            matches = list(self._call(job, self.client.search_contacts, "mobile", last_ten) or [])
        if len(matches) == 1:
            return str(matches[0]["id"])
        name = _folded(record.get("customer_name"))
        if name:
            for match in matches:
                if name in _names(match):
                    return str(match["id"])
        self._intent(job, {"step": "contact"})
        return str(self._call(job, self.client.create_contact,
                              record.get("customer_name") or UNNAMED_CONTACT, mobile))

    def _searches_contacts(self, job: _Job) -> bool:
        """A live record of a proved Indian mobile. Anything less goes on a
        fixed contact: a verified record whose number cannot be searched for
        goes on the unverified one."""
        record = job.record
        return (record.get("mode") == "live" and record.get("identity") == "verified"
                and plus91(record.get("phone")) is not None)

    # -- step 2: the ticket ---------------------------------------------------

    def _ticket(self, job: _Job) -> None:
        try:
            self._create_or_adopt(job)
        except ZohoGone:
            if not self._searches_contacts(job):
                raise
            # The stored contact was deleted or merged in Desk. Forget it and
            # find the person again, once. The ticket intent stays: a create
            # that was never answered may have been made on the old contact
            # and moved with a merge, so the new contact's ticket list is
            # read before any create.
            job.zoho["contact_id"] = None
            self._save(job, {"zoho": copy.deepcopy(job.zoho)})
            self._contact(job, use_stored=False)
            self._create_or_adopt(job)

    def _create_or_adopt(self, job: _Job) -> None:
        contact_id = job.zoho["contact_id"]
        if job.intent.get("step") == "ticket":
            # A create was started and its answer never came back. Look
            # before making a second one. The contact's own ticket list is
            # used, not Zoho's search, which can lag by minutes.
            found = self._adoptable(job, contact_id)
            if found is not None:
                self._ticket_done(job, found, adopted=True)
                return
        payload = ticket_payload(job.record, self.settings, contact_id)
        self._intent(job, {"step": "ticket"})
        self._ticket_done(job, self._call(job, self.client.create_ticket, payload))

    def _adoptable(self, job: _Job, contact_id: str) -> Optional[Dict[str, Any]]:
        """Our ticket, by the chat reference that ends its subject. A ticket
        made before this record (a reference that came round again after a
        database was started afresh) is not ours."""
        tickets = self._call(job, self.client.contact_tickets, contact_id, self._department(job)) or []
        return find_adoptable(tickets, job.record["chat_reference"], not_before=job.record.get("created_at"))

    def _ticket_done(self, job: _Job, ticket: Dict[str, Any], adopted: bool = False) -> None:
        job.zoho.update(ticket_id=str(ticket["id"]), ticket_number=_text(ticket.get("ticketNumber")),
                        web_url=_text(ticket.get("webUrl")))
        self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None})
        if adopted:
            self.log.emit("zoho_ticket_adopted", job.conversation_id, reference=job.ref,
                          zoho_number=job.zoho_number)

    def _department(self, job: _Job) -> Optional[str]:
        # The department ticket_payload sends to, so the look-up searches
        # where the create went.
        if job.record.get("mode") == "live":
            return self.settings.department_id
        return self.settings.test_department_id

    # -- an intent left by an earlier pass --------------------------------------

    def _settle_intent(self, job: _Job) -> None:
        """Deal with an intent left by a pass that crashed or never heard
        back: check the ticket before writing again. If the write is found,
        it is recorded as posted. If not, it never landed, and its step runs
        again below."""
        intent = dict(job.intent)
        step = intent.get("step")
        if not step:
            return
        ticket_id = job.zoho["ticket_id"]
        if step in _COMMENT_STEPS:
            marker = intent.get("marker") or ""
            for comment in self._call(job, self.client.comments, ticket_id) or []:
                # The marker starts our comment. One quoted inside another
                # comment's text is not ours.
                if marker and str(comment.get("content") or "").lstrip().startswith(marker):
                    self._comment_done(job, str(comment["id"]), intent)
                    return
        elif step == "attachment":
            for attachment in self._call(job, self.client.attachments, ticket_id) or []:
                if attachment.get("name") == intent.get("filename"):
                    self._attachment_done(job, str(attachment["id"]), intent["key"])
                    return
        self._save(job, {"intent": None})

    # -- step 3: the transcript -------------------------------------------------

    def _transcript(self, job: _Job) -> None:
        """This run's turns that are not on Zoho yet, as private comments of
        at most 30,000 characters. Each comment starts with its marker.

        The run's end is read again before each comment. If it moved, the
        turns are worked out again against it, so a newcomer's turn recorded
        just before their run closed this one never goes out. It moves once
        at most: close_runs sets an end only where there is none."""
        while True:
            end = job.record.get("ended_at")
            posted = set(job.record.get("posted_turns") or [])
            turns = [turn for turn in self.conversations.transcript(job.record["conversation_id"])
                     if turn.n not in posted and self._in_run(job, turn.at)]
            for text, numbers in transcript_chunks(job.ref, job.record["chat_reference"], turns):
                self._refresh_end(job)
                if job.record.get("ended_at") != end:
                    break
                marker = text.split("\n", 1)[0]
                self._post_comment(job, {"step": "transcript", "marker": marker, "turns": list(numbers)}, text)
            else:
                return

    # -- step 4: the notes --------------------------------------------------

    def _notes(self, job: _Job) -> None:
        posted = set(job.record.get("posted_notes") or [])
        for index, note in enumerate(job.record.get("notes") or []):
            if index in posted:
                continue
            marker = "[%s note %d]" % (job.record["chat_reference"], index + 1)
            at = _moment(note.get("at"))
            when = at.strftime("%d %b %H:%M UTC") if at is not None else "at a time not recorded"
            text = "%s\n%s\nAdded %s." % (marker, note.get("text") or "", when)
            self._post_comment(job, {"step": "note", "marker": marker, "index": index}, text)

    # -- step 5: the photos and videos ---------------------------------------------

    def _attachments(self, job: _Job) -> None:
        """This run's media from the same cluster, each attached once, with a
        file name that starts with our reference. The run's end is read again
        just before each file or comment goes out."""
        if self.media_reader is None:
            return
        record = job.record
        posted = set(record.get("posted_media") or [])
        cluster = record.get("cluster_id")
        media = self.conversations.media_of(record["conversation_id"])
        carried = self._carried(job) if media else {}
        for item in media:
            key = item["_id"]
            if key in posted or (cluster and item.get("cluster_id") != cluster):
                continue
            if not self._file_in_run(job, item, carried):
                continue
            if int(item.get("size_bytes") or 0) > self.settings.attachment_limit_bytes:
                # Never read: a comment says so, and the file stays with us.
                if self._still_in_run(job, item, carried):
                    self._too_large(job, item)
                continue
            try:
                data = self.media_reader.get_bytes(key)
            except StorageError:
                if not self._missing(key):
                    raise
                # Deleted from the bucket: no retry will bring it back.
                if self._still_in_run(job, item, carried):
                    self._unreadable(job, item)
                continue
            if not self._still_in_run(job, item, carried):
                continue
            filename = safe_filename("%s-%s" % (job.ref, _file_name(key)))
            self._intent(job, {"step": "attachment", "key": key, "filename": filename})
            try:
                attachment_id = self._call(job, self.client.upload_attachment, job.zoho["ticket_id"], filename,
                                           data, item.get("mime_type") or "application/octet-stream")
            except ZohoTooLarge:
                self._too_large(job, item)
                continue
            self._attachment_done(job, str(attachment_id), key)

    def _missing(self, key: str) -> bool:
        """Whether the bucket no longer has the object. get_bytes says only
        that it failed; a head tells not found from S3 failing."""
        head = getattr(self.media_reader, "head", None)
        if head is None:
            return False
        try:
            return head(key) is None
        except StorageError:
            return False

    def _too_large(self, job: _Job, item: Dict[str, Any]) -> None:
        size_mb = int(item.get("size_bytes") or 0) / (1024 * 1024)
        self._media_note(job, item, "%s sent in this chat (%.0f MB) was too large to attach here. "
                                    "The AI team keeps it." % (_WHAT.get(item.get("kind"), "A file"), size_mb))

    def _unreadable(self, job: _Job, item: Dict[str, Any]) -> None:
        self._media_note(job, item, "%s sent in this chat could not be read from our storage, so it is not "
                                    "attached here." % _WHAT.get(item.get("kind"), "A file"))

    def _media_note(self, job: _Job, item: Dict[str, Any], sentence: str) -> None:
        """A comment in place of a file. The key is then marked posted."""
        key = item["_id"]
        marker = "[%s file %s]" % (job.record["chat_reference"], _file_name(key))
        self._post_comment(job, {"step": "media_note", "marker": marker, "key": key}, "%s\n%s" % (marker, sentence))

    # -- writes and their records ---------------------------------------------

    def _post_comment(self, job: _Job, intent: Dict[str, Any], text: str) -> None:
        self._intent(job, intent)
        comment_id = self._call(job, self.client.add_comment, job.zoho["ticket_id"], text)
        self._comment_done(job, str(comment_id), intent)

    def _comment_done(self, job: _Job, comment_id: str, intent: Dict[str, Any]) -> None:
        job.zoho.setdefault("comment_ids", []).append(comment_id)
        field, values = {
            "transcript": ("posted_turns", list(intent.get("turns") or ())),
            "note": ("posted_notes", [intent.get("index")]),
            "media_note": ("posted_media", [intent.get("key")]),
        }[intent["step"]]
        self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None}, add_to_set={field: values})

    def _attachment_done(self, job: _Job, attachment_id: str, key: str) -> None:
        job.zoho.setdefault("attachment_ids", []).append(attachment_id)
        self._save(job, {"zoho": copy.deepcopy(job.zoho), "intent": None}, add_to_set={"posted_media": [key]})

    def _finish(self, job: _Job) -> None:
        """Nothing is outstanding: the record is sent. This save also checks
        that `wake` has not changed. If a turn ended or a note came in while
        this pass was working, what it added is not on Zoho yet, so the record
        goes back to waiting, due now."""
        self.status["failing"] = None
        sent = {"state": SENT, "attempts": 0, "last_error": None, "intent": None,
                "lease_until": None, "lease_token": None}
        if self.store.save(job.ref, job.token, sent, expect_wake=job.wake):
            self.log.emit("zoho_ticket_sent", job.conversation_id, reference=job.ref, zoho_number=job.zoho_number,
                          attempts=job.attempts + 1, credits_remaining=self._credits())
            return
        again = {"state": WAITING, "attempts": 0, "last_error": None, "next_attempt_at": self._clock(),
                 "lease_until": None, "lease_token": None}
        if not self.store.save(job.ref, job.token, again):
            raise _Dropped()

    # -- failures ---------------------------------------------------------------

    def _failed(self, job: _Job, exc: BaseException) -> None:
        """Sort a failure by Zoho's answer (the table in spec section 4) and
        put the record back with its next try. The intent stays, so the next
        pass checks Zoho before it writes again."""
        now = self._clock()
        error = _error_name(exc)
        code = getattr(exc, "error", None) or type(exc).__name__
        cid = job.conversation_id
        if isinstance(exc, ZohoGone) and job.zoho.get("ticket_id"):
            # Deleted or merged in Desk. Nothing more can be added to it, and a
            # new ticket would reopen what someone closed. Logged, not retried.
            gone = {"state": GONE, "last_error": error, "intent": None, "lease_until": None, "lease_token": None}
            if self.store.save(job.ref, job.token, gone):
                self.log.emit("zoho_ticket_gone", cid, reference=job.ref, zoho_number=job.zoho_number)
            else:
                self.log.emit("zoho_record_dropped", cid, reference=job.ref)
            return
        attempts = job.attempts + 1
        if isinstance(exc, ZohoBusy):
            wait, attempts = BUSY_WAIT, job.attempts
        elif isinstance(exc, ZohoCreditsExhausted):
            # The organisation's credits for the day are used up. No record
            # is sent until Zoho's Retry-After time. This is not the record's
            # fault, so it counts no attempt.
            wait, attempts = int(getattr(exc, "retry_after_seconds", None) or HOURLY), job.attempts
            self._paused_until = plus(now, wait)
        elif isinstance(exc, ZohoTokenRefused):
            wait = HOURLY
            self.status["failing"] = "token refused: %s" % code
            self.log.emit("zoho_token_refused", cid, level="error", reference=job.ref, error=error)
        elif isinstance(exc, (ZohoConfigError, ZohoGone)):
            # Scope, organisation or licence, or the fixed test or unverified
            # contact deleted in Desk. A person has to fix it.
            wait = HOURLY
            self.status["failing"] = "sending failing: %s" % code
            self.log.emit("zoho_rejected", cid, reference=job.ref, error=error)
        elif isinstance(exc, (ZohoRejected, ZohoTooLarge)):
            wait = HOURLY
            if isinstance(exc, ZohoRejected):
                # Probably the department's required fields: every record is
                # refused until a person looks, so /health says so.
                self.status["failing"] = "sending failing: %s" % code
            self.log.emit("zoho_rejected", cid, reference=job.ref, error=error,
                          fields=[str(name) for name in getattr(exc, "fields", None) or ()])
        elif isinstance(exc, ZohoTokenThrottled):
            wait = TOKEN_THROTTLED_WAIT
        elif isinstance(exc, ZohoUnknownOutcome) and job.intent.get("step") == "ticket":
            # A create that may have been made. The contact's ticket list can
            # lag by minutes, so the look before a second create waits at
            # least two minutes (spec section 4).
            wait = max(retry_wait(attempts), TICKET_LOOKUP_WAIT)
        else:
            # Zoho unreachable, an unknown outcome, an access token refused
            # twice, or the bucket failing: the schedule.
            wait = retry_wait(attempts)
        retry = {"attempts": attempts, "next_attempt_at": plus(now, wait), "last_error": error,
                 "lease_until": None, "lease_token": None}
        if not self.store.save(job.ref, job.token, retry):
            self.log.emit("zoho_record_dropped", cid, reference=job.ref)
            return
        self.log.emit("zoho_retry", cid, reference=job.ref, error=error, attempts=attempts, wait_seconds=wait)

    def _credits(self) -> Optional[int]:
        return getattr(getattr(self.client, "http", None), "last_credits_remaining", None)

    def _credits_low(self) -> bool:
        """A reading below the floor, taken less than CREDITS_FLOOR_WAIT ago.
        Deferring makes no call, so with only deferred records due the
        reading would never change. Once it is that old it no longer counts:
        the record goes out, and its calls bring a new reading."""
        remaining = self._credits()
        if remaining is None or remaining >= self.settings.credits_floor:
            return False
        read_at = self._credits_read_at
        return read_at is not None and age_seconds(read_at, self._clock()) < CREDITS_FLOOR_WAIT

    def _defer_for_credits(self, job: _Job) -> None:
        """Credits are shared with the OMS, whose calls do not retry. Below
        the floor only urgent records are sent, and the others wait. One of
        them goes out each CREDITS_FLOOR_WAIT to read the credits again."""
        later = {"next_attempt_at": plus(self._clock(), CREDITS_FLOOR_WAIT), "lease_until": None, "lease_token": None}
        if not self.store.save(job.ref, job.token, later):
            raise _Dropped()
        self.log.emit("zoho_retry", job.conversation_id, reference=job.ref, error="credits_floor",
                      attempts=job.attempts, wait_seconds=CREDITS_FLOOR_WAIT, credits_remaining=self._credits())

    # -- late and stuck -------------------------------------------------------------

    def check_overdue(self) -> None:
        """Mark records still waiting past their limit as stuck, and log them
        by reference and age only, at most once an hour each. Stuck records
        are still retried. mark_stuck needs no lease, so a record another
        worker is sending is marked too."""
        now = self._clock()
        seen: Set[str] = set()
        for record in self.store.overdue(now, self.settings.mode):
            reference = record["_id"]
            seen.add(reference)
            last = self._overdue_logged.get(reference)
            if last is None or age_seconds(last, now) >= OVERDUE_LOG_SECONDS:
                self._overdue_logged[reference] = now
                cid = record.get("conversation_id") or "zoho"
                age = age_seconds(record.get("due_since") or now, now)
                # Each event is named in its own call, so a scan of the code
                # for the events it emits (the alarm stack's check) finds both.
                if record.get("urgent"):
                    self.log.emit("safety_ticket_late", cid, level="error", reference=reference, age_seconds=age)
                else:
                    self.log.emit("zoho_ticket_stuck", cid, level="error", reference=reference, age_seconds=age)
            if record.get("state") != STUCK:
                self.store.mark_stuck(reference)
        for reference in [r for r in self._overdue_logged if r not in seen]:
            del self._overdue_logged[reference]

    # -- the run's bounds ---------------------------------------------------------------

    def _in_run(self, job: _Job, at: Any) -> bool:
        """At or after the run's start, and before its end. close_runs sets the
        end when a new run begins on the conversation, or someone else writes
        in it. A record with no start, or a time that cannot be read, takes
        nothing: an unbounded window would take in every earlier run and
        person of the conversation."""
        moment, start = _moment(at), _moment(job.record.get("started_at"))
        if moment is None or start is None or moment < start:
            return False
        ended = job.record.get("ended_at")
        if not ended:
            return True
        end = _moment(ended)
        return end is not None and moment < end

    def _refresh_end(self, job: _Job) -> None:
        """The run's end as the store has it now: another server's turn may
        have set it since this record was taken."""
        current = self.store.get(job.ref)
        if current is None:
            raise _Dropped()
        job.record["ended_at"] = current.get("ended_at")

    def _carried(self, job: _Job) -> Dict[str, Any]:
        """The files the customer's messages in this conversation carried, by
        S3 key, with the time of the turn that carried each. The API stores a
        file before its turn runs (api._persist_media), and a new run's start
        is set inside that turn (a new or expired state, or restart_for), so
        a file sent with a run's first message is older than the run. Its
        turn is not. A file whose turn was never recorded is named by none."""
        carried: Dict[str, Any] = {}
        for turn in self.conversations.transcript(job.record["conversation_id"]):
            if turn.role != "customer":
                continue
            for attachment in turn.attachments or ():
                url = str((attachment or {}).get("url") or "")
                if url.startswith(_S3):
                    carried.setdefault(url[len(_S3):], turn.at)
        return carried

    def _file_in_run(self, job: _Job, item: Dict[str, Any], carried: Dict[str, Any]) -> bool:
        """A file a customer's turn carried belongs to the run that turn is in,
        whatever time it was stored: one sent with a run's first message is
        older than the run, and would otherwise look like the earlier run's
        when that run is closed at this one's start. Only a file no turn names
        is judged by the time it was stored."""
        key = item["_id"]
        if key in carried:
            return self._in_run(job, carried[key])
        return self._in_run(job, item.get("stored_at"))

    def _still_in_run(self, job: _Job, item: Dict[str, Any], carried: Dict[str, Any]) -> bool:
        self._refresh_end(job)
        return self._file_in_run(job, item, carried)

    # -- the store, under the lease ---------------------------------------------------

    def _save(self, job: _Job, changes: Dict[str, Any], add_to_set: Optional[Dict[str, List[Any]]] = None) -> None:
        if not self.store.save(job.ref, job.token, changes, add_to_set=add_to_set):
            raise _Dropped()
        for name, value in changes.items():
            job.record[name] = copy.deepcopy(value)
        for name, values in (add_to_set or {}).items():
            have = list(job.record.get(name) or [])
            for value in values:
                if value not in have:
                    have.append(value)
            job.record[name] = have

    def _intent(self, job: _Job, intent: Dict[str, Any]) -> None:
        """Saved before the write it names. If this save fails, nothing is sent."""
        self._save(job, {"intent": dict(intent, at=self._clock())})

    def _call(self, job: _Job, method: Callable[..., Any], *args: Any) -> Any:
        """One Zoho call, after the lease is renewed for it. The credits
        reading changes only in a call, so its age is counted from the last
        one. A call that never reached Zoho makes a low reading count for
        longer, which only delays a record that is not urgent."""
        if not self.store.renew_lease(job.ref, job.token, plus(self._clock(), LEASE_SECONDS)):
            raise _Dropped()
        try:
            return method(*args)
        finally:
            self._credits_read_at = self._clock()
