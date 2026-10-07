"""Reading the serial off a customer's photo (the person's rule, 7 October 2026).

The serial ask (serial_ask.py) and the melt ask ask a battery customer for
two photos: the battery's serial sticker and the controller's label. Each
photo the customer sends on that bike, from the turn the ask goes out, is
read here by Gemini
and the reading kept in the conversation store's `serial_readings`, one per
photo, with the bike's frame number and the photo's S3 key (never a link).

A reading is a claim, not a fact: `confirmed` is False until the customer
confirms it, which is later work, and nothing in the chat quotes it back yet.
The model answers in JSON only: which part the photo shows, the serial and
whether it could read it. A photo of neither part is not kept. The serial is
kept only when it looks like one (letters, digits, dashes and slashes); for
the battery that is the number under the barcode, never the "Patent No" one
the sticker also carries. For the controller it is the S/N, which is the one
that matters (the person, 7 October 2026), never the model or part code.

It runs after the reply, never inside the turn: the customer does not wait
for it, and a reading that fails is logged by its code and lost, as a photo
check's is. Switched on with the serial ask (EMOTORAD_SERIAL_ASK, exactly
"on") and the OpenRouter key; api.py decides once, at startup.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .serial_ask import SWITCH_ENV

OPENROUTER_SERIAL_MODEL = "google/gemini-3.8-flash"
TIMEOUT_SECONDS = 20
# Uploaded photos are capped at 10 MB (storage/keys.py); a backstop.
INLINE_LIMIT = 12 * 1024 * 1024

PARTS = ("battery", "controller")

PROMPT = (
    "You are reading a photo a customer sent to an electric cycle company's support chat. It should show "
    "either the battery's serial number sticker or the controller's label. Answer with JSON only, with "
    "exactly these keys: part, serial, legible. part is \"battery\" for the battery's sticker, "
    "\"controller\" for the controller's label, or \"none\" for anything else. For the battery, serial is "
    "the serial number printed under the barcode, which often starts EMIN or ABADAASHA; never the number "
    "after \"Patent No\". For the controller, serial is the number after \"S/N\" on its label; never the "
    "model or part code. Copy the serial exactly as printed, with no spaces added. legible is true only "
    "when every character of the serial is clearly readable; otherwise false, and serial is your best "
    "reading or null. When part is \"none\", serial is null and legible is false."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
# What a serial may be made of. An explicit ASCII class: a serial is never
# Indic, and anything else the model writes is not a serial.
_SERIAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/-]{3,39}$")


class SerialReadError(Exception):
    """A stable code only: a provider message can echo the request."""


class OpenRouterSerialReader:
    provider = "openrouter"

    def __init__(self, transport: Any, model: str = OPENROUTER_SERIAL_MODEL, zdr: bool = True) -> None:
        self.model = model
        self.zdr = zdr
        self._transport = transport

    def read(self, data: bytes, mime: str) -> Dict[str, Any]:
        """{"part": "battery" | "controller" | "none", "serial": str or None,
        "legible": bool}."""
        from .llm import from_openai_response
        from .openrouter import CHAT_PATH, OpenRouterError

        if len(data) > INLINE_LIMIT:
            raise SerialReadError("too_large")
        url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        }
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        try:
            response = from_openai_response(self._transport.post(CHAT_PATH, body, timeout=TIMEOUT_SECONDS))
        except OpenRouterError as exc:
            raise SerialReadError(exc.code) from None
        return parse(response.text or "")


def parse(text: str) -> Dict[str, Any]:
    """The model's answer, made safe: an unknown part is "none", a serial
    that does not look like one is None, and only a real true is legible."""
    try:
        answer = json.loads(_FENCE.sub("", text.strip()))
    except ValueError:
        raise SerialReadError("bad_json") from None
    if not isinstance(answer, dict):
        raise SerialReadError("bad_json")
    part = answer.get("part") if answer.get("part") in PARTS else "none"
    serial = answer.get("serial")
    serial = serial.strip() if isinstance(serial, str) else None
    if part == "none" or not serial or not _SERIAL.match(serial):
        serial = None
    return {"part": part, "serial": serial, "legible": serial is not None and answer.get("legible") is True}


def reading_doc(conversation_id: str, user_key: Optional[str], frame_number: str, media_key: str,
                reading: Mapping[str, Any], model: str, read_at: str) -> Dict[str, Any]:
    """One photo's reading as the store keeps it: `_id` is the photo's S3
    key, so the same photo read twice keeps one reading."""
    return {
        "_id": media_key, "conversation_id": conversation_id, "user_key": user_key,
        "frame_number": frame_number, "part": reading["part"], "serial": reading["serial"],
        "legible": bool(reading["legible"]), "media_key": media_key, "model": model, "read_at": read_at,
        "confirmed": False, "source": "ocr",
    }


def photos_to_read(attachments: Sequence[Any], state: Any) -> List[Tuple[str, str]]:
    """(S3 key, type) for each stored photo in the message, when the bike the
    conversation is about was asked for its serial photos (by the serial ask
    or the melt ask); [] otherwise. An inline photo that could not be stored
    has no key and is not read."""
    frame = getattr(state, "selected_frame", None) if state is not None else None
    if not frame or frame not in list(state.serials_asked_frames) + list(state.melt_asked_frames):
        return []
    found = []
    for item in attachments:
        url = getattr(item, "url", "") or ""
        mime = getattr(item, "mime_type", None) or ""
        if getattr(item, "kind", None) == "image" and url.startswith("s3://") and mime.startswith("image/"):
            found.append((url[len("s3://"):], mime))  # "s3://" + the key, no bucket (api.py)
    return found


def read_photos(reader: Any, photos: Sequence[Tuple[str, str]], load: Callable[[str], bytes], store: Any,
                *, conversation_id: str, user_key: Optional[str], frame_number: str, emit: Callable[..., None],
                now: Optional[Callable[[], datetime]] = None) -> int:
    """Reads each photo and keeps each reading of a battery or controller
    label; returns how many were kept. Never raises: a photo that cannot be
    loaded, read or kept is logged by its code and skipped. The serial itself
    is never logged."""
    clock = now or (lambda: datetime.now(timezone.utc))
    kept = 0
    for key, mime in photos:
        try:
            reading = reader.read(load(key), mime)
        except SerialReadError as exc:
            emit("serial_read_failed", conversation_id, error=str(exc))
            continue
        except Exception as exc:
            emit("serial_read_failed", conversation_id, error=type(exc).__name__)
            continue
        if reading["part"] == "none":
            emit("serial_read", conversation_id, part="none", legible=False)
            continue
        doc = reading_doc(conversation_id, user_key, frame_number, key, reading,
                          getattr(reader, "model", ""), clock().isoformat())
        try:
            store.add_serial_reading(doc)
        except Exception as exc:
            emit("serial_read_failed", conversation_id, error="store:" + type(exc).__name__)
            continue
        kept += 1
        emit("serial_read", conversation_id, part=reading["part"], legible=doc["legible"])
    return kept


def serial_reader_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OpenRouterSerialReader]:
    """OpenRouter when the serial ask is on and its key is set, else None."""
    from .openrouter import API_KEY_ENV, OpenRouterTransport

    env = os.environ if environ is None else environ
    key = (env.get(API_KEY_ENV) or "").strip()
    if env.get(SWITCH_ENV) != "on" or not key:
        return None
    transport = OpenRouterTransport(
        api_key=key,
        base_url=env.get("EMOTORAD_OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
        timeout=TIMEOUT_SECONDS,
    )
    return OpenRouterSerialReader(transport, zdr=env.get("EMOTORAD_OPENROUTER_ZDR", "1") == "1")
