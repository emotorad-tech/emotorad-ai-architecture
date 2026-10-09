"""Reading an invoice for its purchase date (spec 2026-10-08, section 3).

When a bike's purchase date is missing, its invoice is read: the copy OMS
holds, or one the customer uploads. Gemini Flash (through OpenRouter, zero
data retention) only reads: the date as printed, the seller, every frame
number printed, the product, whether it is legible and whether it is an
invoice at all. Code decides everything else:

- **Confident** only when the date reads day-first as one real date, not in
  the future and not before 1 January 2019, a frame number on the invoice
  equals the bike's (ignoring case, spaces and dashes), and it is legible.
- **What the customer hears** is written here, never by the model: a
  confident date labelled as read from their invoice, with the battery's
  cover end, and "a support executive will confirm"; otherwise only that the
  invoice has gone to the support team. Sachin signs off before real
  customers hear the date.

Gemini never sees what the customer typed. Values are never logged.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import warranty_terms
from .invoice_dates import parse_printed_date

SWITCH_ENV = "EMOTORAD_INVOICE_OCR"
OPENROUTER_INVOICE_MODEL = "google/gemini-3.8-flash"
TIMEOUT_SECONDS = 30
INLINE_LIMIT = 12 * 1024 * 1024
EARLIEST = date(2019, 1, 1)

PROMPT = (
    "You are reading a document a customer of an electric cycle company sent, or that the company holds, to "
    "find when their bike was bought. Answer with JSON only, with exactly these keys: is_invoice, "
    "invoice_date, seller, frame_numbers, product, legible. is_invoice is true only when the document is a "
    "purchase invoice or bill for a bicycle or e-bike; false for anything else (a photo of the bike, a "
    "part, a screen, a warranty card). invoice_date is the invoice's date exactly as printed (do not "
    "reformat it), or null. seller is the selling shop's or company's name as printed, or null. "
    "frame_numbers is a list of every frame, chassis or serial number printed on the invoice, exactly as "
    "printed, whether or not it matches the frame number given below; an empty list if none. product is "
    "the bicycle model as printed, or null. legible is true only when the date and the frame number can "
    "be read with certainty. When unsure, answer false or null."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_FRAME_JUNK = re.compile(r"[^A-Z0-9]")
_KEYS = ("is_invoice", "invoice_date", "seller", "frame_numbers", "product", "legible")

CONFIDENT_EN = ("Going by your invoice dated {date}, your battery is covered until {end}. "
                "A support executive will confirm this.")
# When the battery's cover has already ended (final review, finding 7).
ENDED_EN = ("Going by your invoice dated {date}, your battery's cover ended on {end}. "
            "A support executive will confirm this.")
NOT_CONFIDENT_EN = ("Thanks, I have passed your invoice to our support team. A support executive will confirm "
                    "your warranty.")
# DRAFT: for a Hindi speaker to check before real traffic.
CONFIDENT_HI = "आपके इनवॉइस की तारीख {date} के हिसाब से, आपकी बैटरी {end} तक कवर है। सपोर्ट टीम इसकी पुष्टि करेगी।"
ENDED_HI = "आपके इनवॉइस की तारीख {date} के हिसाब से, आपकी बैटरी का कवर {end} को समाप्त हो गया। सपोर्ट टीम इसकी पुष्टि करेगी।"
NOT_CONFIDENT_HI = "धन्यवाद, मैंने आपका इनवॉइस हमारी सपोर्ट टीम को भेज दिया है। सपोर्ट टीम आपकी वारंटी की पुष्टि करेगी।"


class InvoiceReadError(Exception):
    """A stable code only: a provider message can echo the request."""


class OpenRouterInvoiceReader:
    provider = "openrouter"

    def __init__(self, transport: Any, model: str = OPENROUTER_INVOICE_MODEL, zdr: bool = True) -> None:
        self.model = model
        self.zdr = zdr
        self._transport = transport

    def read(self, data: bytes, mime: str, frame_number: str) -> Dict[str, Any]:
        from .llm import from_openai_response
        from .openrouter import CHAT_PATH, OpenRouterError

        if len(data) > INLINE_LIMIT:
            raise InvoiceReadError("too_large")
        url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        document = ({"type": "file", "file": {"filename": "invoice.pdf", "file_data": url}}
                    if mime == "application/pdf" else {"type": "image_url", "image_url": {"url": url}})
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "text", "text": "The bike's frame number: %s. Report every frame number printed on the "
                                         "invoice, whether or not it matches." % frame_number},
                document,
            ]}],
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        }
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        try:
            response = from_openai_response(self._transport.post(CHAT_PATH, body, timeout=TIMEOUT_SECONDS))
        except OpenRouterError as exc:
            raise InvoiceReadError(exc.code) from None
        return parse(response.text or "")


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse(text: str) -> Dict[str, Any]:
    """The model's answer, made safe: only the known keys, strings or None,
    a list of strings for the frame numbers, and only a real true."""
    try:
        answer = json.loads(_FENCE.sub("", (text or "").strip()))
    except ValueError:
        raise InvoiceReadError("bad_json") from None
    if not isinstance(answer, dict):
        raise InvoiceReadError("bad_json")
    frames = answer.get("frame_numbers")
    if isinstance(frames, str):
        frames = [frames]
    return {
        "is_invoice": answer.get("is_invoice") is True,
        "invoice_date": _text(answer.get("invoice_date")),
        "seller": _text(answer.get("seller")),
        "frame_numbers": [f.strip() for f in frames if isinstance(f, str) and f.strip()]
        if isinstance(frames, list) else [],
        "product": _text(answer.get("product")),
        "legible": answer.get("legible") is True,
    }


def _frame(value: str) -> str:
    return _FRAME_JUNK.sub("", value.upper())


def assess(found: Mapping[str, Any], frame_number: str, today: date) -> Dict[str, Any]:
    """{"confident", "purchase_date" (the date read, when there is one), "reason"}."""
    purchase: Optional[date] = None
    if found.get("invoice_date"):
        parsed = parse_printed_date(found["invoice_date"])
        if parsed.reason is None:
            purchase = parsed.date
    if not found.get("is_invoice"):
        reason = "not an invoice"
    elif purchase is None:
        reason = "no readable date"
    elif purchase > today:
        reason = "date in the future"
    elif purchase < EARLIEST:
        reason = "date before 2019"
    elif not any(_frame(f) == _frame(frame_number) for f in found.get("frame_numbers") or []):
        reason = "frame number not on the invoice"
    elif not found.get("legible"):
        reason = "not legible"
    else:
        reason = None
    return {"confident": reason is None, "purchase_date": purchase, "reason": reason}


def _spoken(day: date) -> str:
    return "%d %s" % (day.day, day.strftime("%B %Y"))


def customer_line(assessed: Mapping[str, Any], today: date, hindi: bool) -> str:
    """What the customer hears, written by code."""
    if not assessed.get("confident"):
        return NOT_CONFIDENT_HI if hindi else NOT_CONFIDENT_EN
    bought = assessed["purchase_date"]
    end = warranty_terms.valid_until(bought, warranty_terms.TERMS[warranty_terms.LEAD_PART])
    if end < today:
        template = ENDED_HI if hindi else ENDED_EN
    else:
        template = CONFIDENT_HI if hindi else CONFIDENT_EN
    return template.format(date=_spoken(bought), end=_spoken(end))


def findings_text(found: Mapping[str, Any], assessed: Mapping[str, Any], source: str) -> str:
    """The ticket's account of what the invoice showed."""
    confident = "yes" if assessed.get("confident") else "no (%s)" % assessed.get("reason")
    return ("Invoice read by AI (source: %s): date %s, seller %s, frame numbers %s, product %s; confident: %s. "
            "Warranty check: not confirmed yet." % (
                "OMS" if source == "oms" else "customer upload",
                found.get("invoice_date") or "not read", found.get("seller") or "not read",
                ", ".join(found.get("frame_numbers") or []) or "none read", found.get("product") or "not read",
                confident))


def reader_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OpenRouterInvoiceReader]:
    """On with EMOTORAD_INVOICE_OCR exactly "on" and the OpenRouter key."""
    from .openrouter import API_KEY_ENV, OpenRouterTransport

    env = os.environ if environ is None else environ
    key = (env.get(API_KEY_ENV) or "").strip()
    if env.get(SWITCH_ENV) != "on" or not key:
        return None
    transport = OpenRouterTransport(
        api_key=key, base_url=env.get("EMOTORAD_OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
        timeout=TIMEOUT_SECONDS,
    )
    return OpenRouterInvoiceReader(transport, zdr=env.get("EMOTORAD_OPENROUTER_ZDR", "1") == "1")


# --- The service: reading, keeping, waiting ---------------------------------


class InvoiceService:
    """Reads a bike's invoice for its purchase date and keeps the reading
    (`invoice_readings`). The API starts the reads before a turn and waits up
    to `wait_seconds`; one that takes longer finishes on its own and is told
    on a later turn. The runtime raises the ticket and tells the customer
    (Runtime._with_invoice_result). Never raises; failures are logged by code.

    An invoice is read once (final review, 8 October 2026): a read already
    running is not started again, a reading is never overwritten, and an OMS
    file that is not an invoice or cannot be read is not tried again in this
    process (`invoice_state` then says "unreadable", so the agent asks the
    customer for theirs). The copy of OMS's file is kept only once it has been
    read as an invoice."""

    READABLE = "readable"
    UNREADABLE = "unreadable"
    WITH_SUPPORT = "with_support"

    def __init__(self, reader: Any, oms_db: Any, oms_client: Any, media_store: Any, conversations: Any,
                 emit: Callable[..., None], persist: Optional[Callable[..., None]] = None, pool: Any = None,
                 wait_seconds: float = 15.0, today: Callable[[], date] = date.today) -> None:
        self.reader = reader
        self.oms_db = oms_db
        self.oms_client = oms_client
        self.media_store = media_store
        self.conversations = conversations
        self.emit = emit
        self.persist = persist
        self.pool = pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="invoice-read")
        self.wait_seconds = wait_seconds
        self.today = today
        self._lock = threading.Lock()
        self._inflight: set = set()
        self._unreadable: set = set()

    @property
    def provider(self) -> str:
        return getattr(self.reader, "provider", "openrouter")

    def bike(self, phone: str, frame_number: str) -> Optional[Dict[str, Any]]:
        """The frame's OMS registration for this phone, or None (also when
        OMS cannot be read: then nothing is read this turn)."""
        try:
            return self.oms_db.row(phone, frame_number)
        except Exception:
            return None

    def _reading(self, reading_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.conversations.invoice_reading(reading_id)
        except Exception:
            return None

    def invoice_state(self, file_id: Optional[str]) -> str:
        """What can be done with OMS's invoice file: "readable" (read it, or
        it is being read), "with_support" (read and passed on, in any chat),
        or "unreadable" (no usable file, no way to fetch it, or it was not an
        invoice): then the customer is asked for theirs."""
        from .tools.oms import _FILE_ID

        if not file_id or not _FILE_ID.fullmatch(str(file_id)) or self.oms_client is None:
            return self.UNREADABLE
        with self._lock:
            if file_id in self._unreadable:
                return self.UNREADABLE
        existing = self._reading("oms:" + file_id)
        if existing is not None and existing.get("told"):
            return self.WITH_SUPPORT
        return self.READABLE

    def _claim(self, reading_id: str) -> bool:
        with self._lock:
            if reading_id in self._inflight:
                return False
            self._inflight.add(reading_id)
        if self._reading(reading_id) is not None:
            self._release(reading_id)
            return False
        return True

    def _release(self, reading_id: str) -> None:
        with self._lock:
            self._inflight.discard(reading_id)

    def _give_up(self, file_id: str) -> None:
        with self._lock:
            self._unreadable.add(file_id)

    def read_from_oms(self, conversation_id: str, user_key: Optional[str], cluster_id: Optional[str], phone: str,
                      frame_number: str) -> None:
        from .tools.oms import OMSNoRecord

        try:
            file_id = self.oms_db.invoice_file(phone, frame_number)
        except Exception as exc:
            self.emit("invoice_read_failed", conversation_id, error=type(exc).__name__, source="oms")
            return
        if self.invoice_state(file_id) != self.READABLE:
            return
        reading_id = "oms:" + file_id
        if not self._claim(reading_id):
            return
        try:
            try:
                data, mime = self.oms_client.download_file(file_id)
            except (OMSNoRecord, ValueError) as exc:
                self._give_up(file_id)
                self.emit("invoice_read_failed", conversation_id, error=type(exc).__name__, source="oms")
                return
            except Exception as exc:
                # An outage: tried again on a later turn.
                self.emit("invoice_read_failed", conversation_id, error=type(exc).__name__, source="oms")
                return
            found = self._read(conversation_id, frame_number, "oms", data, mime)
            if found is None or not found.get("is_invoice"):
                self._give_up(file_id)
                return
            copy_key = self._keep_copy(conversation_id, cluster_id, data, mime)
            self._store(conversation_id, user_key, frame_number, reading_id, "oms", found, copy_key)
        finally:
            self._release(reading_id)

    def read_upload(self, conversation_id: str, user_key: Optional[str], frame_number: str, key: str,
                    mime: str) -> None:
        if not self._claim(key):
            return
        try:
            try:
                data = self.media_store.get_bytes(key)
            except Exception as exc:
                self.emit("invoice_read_failed", conversation_id, error=type(exc).__name__, source="customer")
                return
            found = self._read(conversation_id, frame_number, "customer", data, mime)
            if found is not None and found.get("is_invoice"):
                self._store(conversation_id, user_key, frame_number, key, "customer", found, key)
        finally:
            self._release(key)

    def start(self, jobs: List[Callable[[], None]]) -> None:
        """Runs the jobs at once, waiting up to `wait_seconds` for them."""
        if not jobs:
            return
        futures = [self.pool.submit(job) for job in jobs]
        done, pending = wait(futures, timeout=self.wait_seconds)
        if pending:
            self.emit("invoice_read_late", "invoice", reads=len(pending))

    def start_later(self, jobs: List[Callable[[], None]]) -> None:
        """Starts the jobs and returns at once; what they find is told on a
        later turn (`Runtime._with_invoice_result`)."""
        for job in jobs:
            self.pool.submit(job)

    def _keep_copy(self, conversation_id: str, cluster_id: Optional[str], data: bytes, mime: str) -> Optional[str]:
        """OMS's invoice copied into the customer's tree, so the ticket can
        point at it and erasure reaches it. None when it cannot be kept."""
        from .storage import keys

        if self.media_store is None or not cluster_id:
            return None
        try:
            kind = keys.customer_kind_for(mime)
            key = keys.customer_key(cluster_id, conversation_id, kind, keys.new_upload_id(), mime)
            self.media_store.put_bytes(key, data, mime)
        except Exception as exc:
            self.emit("invoice_copy_not_kept", conversation_id, error=type(exc).__name__)
            return None
        if self.persist is not None:
            self.persist(conversation_id=conversation_id, cluster_id=cluster_id, key=key,
                         kind="image" if kind == "images" else "document", mime_type=mime, size_bytes=len(data),
                         source="oms_invoice")
        return key

    def _read(self, conversation_id: str, frame_number: str, source: str, data: bytes,
              mime: str) -> Optional[Dict[str, Any]]:
        """What the reader found, or None when it could not read. Logs the
        outcome only, never a value."""
        try:
            found = self.reader.read(data, mime, frame_number)
        except InvoiceReadError as exc:
            self.emit("invoice_read_failed", conversation_id, error=str(exc), source=source)
            return None
        except Exception as exc:
            self.emit("invoice_read_failed", conversation_id, error=type(exc).__name__, source=source)
            return None
        assessed = assess(found, frame_number, self.today())
        self.emit("invoice_read", conversation_id, source=source, is_invoice=bool(found.get("is_invoice")),
                  confident=assessed["confident"])
        return found

    def _store(self, conversation_id: str, user_key: Optional[str], frame_number: str, reading_id: str,
               source: str, found: Dict[str, Any], copy_key: Optional[str]) -> None:
        assessed = assess(found, frame_number, self.today())
        try:
            self.conversations.add_invoice_reading({
                "_id": reading_id, "conversation_id": conversation_id, "user_key": user_key,
                "frame_number": frame_number, "source": source, "found": dict(found),
                "confident": assessed["confident"], "reason": assessed["reason"],
                "purchase_date": assessed["purchase_date"].isoformat() if assessed["purchase_date"] else None,
                "copy_key": copy_key, "read_at": datetime.now(timezone.utc).isoformat(), "ticket_id": None,
                "told": False,
            })
        except Exception as exc:
            self.emit("invoice_read_failed", conversation_id, error="store:" + type(exc).__name__, source=source)
