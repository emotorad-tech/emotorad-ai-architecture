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
from datetime import date
from typing import Any, Dict, List, Mapping, Optional

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
NOT_CONFIDENT_EN = ("Thanks, I have passed your invoice to our support team. A support executive will confirm "
                    "your warranty.")
# DRAFT: for a Hindi speaker to check before real traffic.
CONFIDENT_HI = "आपके इनवॉइस की तारीख {date} के हिसाब से, आपकी बैटरी {end} तक कवर है। सपोर्ट टीम इसकी पुष्टि करेगी।"
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
    return (CONFIDENT_HI if hindi else CONFIDENT_EN).format(date=_spoken(bought), end=_spoken(end))


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
