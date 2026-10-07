"""Reading serials and the warranty seal off a customer's photo (the person's rules, 7 October 2026).

The serial ask (serial_ask.py) and the melt ask ask for photos of the bike's
frame number sticker and, for a battery issue, the battery's serial sticker,
the controller's label and the battery's warranty seal. Each photo the
customer sends on that bike after the ask is read here by Gemini and the
reading kept in the conversation store's `serial_readings`, one per photo,
with the bike's frame number and the photo's S3 key (never a link).

Gemini is shown EMotorad's reference pictures first (REFERENCE_KEYS: where
each label is, and an intact and a torn seal), labelled as ours, and answers
in JSON only: which part the photo shows, the serial, whether it could read
it, and for the seal whether it is intact or torn. A photo of none of them
is not kept. A serial is kept only when it looks like one (letters, digits,
dashes and slashes); for the battery that is the number under the barcode,
never the "Patent No" one the sticker also carries; for the controller the
S/N, never the model or part code; for the frame, the frame number.

A reading is a claim, not a fact: `confirmed` is False until the customer
confirms it or types it (serial_confirm.py). The seal's reading is never put
to the customer and never told to them: it is kept for support.

It starts when the message arrives and runs beside the turn, never holding
the reply: a reading done by the end of the turn is confirmed in that reply,
a later one in the next. A reading that fails is logged by its code and
lost, as a photo check's is. Switched on with the serial ask
(EMOTORAD_SERIAL_ASK, exactly "on") and the OpenRouter key; api.py decides
once, at startup.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .serial_ask import NO_BIKE, SWITCH_ENV

OPENROUTER_SERIAL_MODEL = "google/gemini-3.8-flash"
TIMEOUT_SECONDS = 20
# Uploaded photos are capped at 10 MB (storage/keys.py); a backstop.
INLINE_LIMIT = 12 * 1024 * 1024

PARTS = ("battery", "controller", "frame", "warranty_seal")
# The parts with a serial; the seal has none.
SERIAL_PARTS = ("battery", "controller", "frame")
SEALS = ("intact", "torn", "unclear")

# EMotorad's reference pictures, shown before the customer's photo
# (evidence_check.References reads them, as the 900 px WebP copies).
REFERENCE_KEYS = {"serial": ("battery_serial_label", "battery_serial_label_doodle", "controller_serial_label",
                             "frame_number_sticker", "battery_warranty_seal_intact", "battery_warranty_seal_torn")}
REFERENCES_INTRO = (
    "The pictures after this line are EMotorad's own reference pictures, not the customer's: where each label "
    "is on the bike, and what an intact and a torn battery warranty seal look like. Use them only to recognise "
    "the part and judge the seal. Never read a serial from them."
)
CUSTOMER_PHOTO_LINE = "The customer's photo:"

PROMPT = (
    "You are reading a photo a customer sent to an electric cycle company's support chat. It should show one "
    "of: the battery's serial number sticker, the controller's label, the bike's frame number sticker, or the "
    "battery's warranty seal. Answer with JSON only, with exactly these keys: part, serial, legible, seal. "
    "part is \"battery\" for the battery's sticker, \"controller\" for the controller's label, \"frame\" for "
    "the frame number sticker, \"warranty_seal\" for the battery's warranty seal, or \"none\" for anything "
    "else. For the battery, serial is the serial number printed under the barcode, which often starts EMIN or "
    "ABADAASHA; never the number after \"Patent No\". For the controller, serial is the number after \"S/N\" "
    "on its label; never the model or part code. For the frame, serial is the frame number on the sticker, a "
    "letter or letters followed by digits. Copy the serial exactly as printed, with no spaces added. legible "
    "is true only when every character of the serial is clearly readable; otherwise false, and serial is your "
    "best reading or null. For the warranty seal, serial is null, legible is false, and seal is \"intact\" "
    "when the seal is whole and flat, \"torn\" when it is broken, cut or peeled, or \"unclear\" when you "
    "cannot tell; compare it with the reference pictures. For every other part, seal is null. When part is "
    "\"none\", serial and seal are null and legible is false."
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

    def read(self, data: bytes, mime: str, references: Sequence[Tuple[bytes, str, str]] = ()) -> Dict[str, Any]:
        """{"part": one of PARTS or "none", "serial": str or None, "legible":
        bool, "seal": one of SEALS or None}. `references` (bytes, mime,
        caption) go first, under REFERENCES_INTRO."""
        from .llm import from_openai_response
        from .openrouter import CHAT_PATH, OpenRouterError

        if len(data) > INLINE_LIMIT:
            raise SerialReadError("too_large")
        content: List[Dict[str, Any]] = [{"type": "text", "text": PROMPT}]
        if references:
            content.append({"type": "text", "text": REFERENCES_INTRO})
            for ref_data, ref_mime, caption in references:
                content.append({"type": "text", "text": caption})
                content.append({"type": "image_url", "image_url": {
                    "url": "data:%s;base64,%s" % (ref_mime, base64.b64encode(ref_data).decode("ascii"))}})
            content.append({"type": "text", "text": CUSTOMER_PHOTO_LINE})
        content.append({"type": "image_url", "image_url": {
            "url": "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))}})
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
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


def looks_like_serial(text: Any) -> bool:
    """Whether `text` could be a serial: letters, digits, dashes and slashes,
    4 to 40 of them, starting with a letter or digit."""
    return isinstance(text, str) and bool(_SERIAL.match(text))


def parse(text: str) -> Dict[str, Any]:
    """The model's answer, made safe: an unknown part is "none", a serial
    that does not look like one (or on the seal) is None, only a real true
    is legible, and the seal is one of SEALS on the seal only."""
    try:
        answer = json.loads(_FENCE.sub("", text.strip()))
    except ValueError:
        raise SerialReadError("bad_json") from None
    if not isinstance(answer, dict):
        raise SerialReadError("bad_json")
    part = answer.get("part") if answer.get("part") in PARTS else "none"
    serial = answer.get("serial")
    serial = serial.strip() if isinstance(serial, str) else None
    if part not in SERIAL_PARTS or not looks_like_serial(serial):
        serial = None
    seal = None
    if part == "warranty_seal":
        seal = answer.get("seal") if answer.get("seal") in SEALS else "unclear"
    return {"part": part, "serial": serial, "legible": serial is not None and answer.get("legible") is True,
            "seal": seal}


def reading_doc(conversation_id: str, user_key: Optional[str], frame_number: str, media_key: str,
                reading: Mapping[str, Any], model: str, read_at: str) -> Dict[str, Any]:
    """One photo's reading as the store keeps it: `_id` is the photo's S3
    key, so the same photo read twice keeps one reading."""
    return {
        "_id": media_key, "conversation_id": conversation_id, "user_key": user_key,
        "frame_number": frame_number, "part": reading["part"], "serial": reading["serial"],
        "legible": bool(reading["legible"]), "seal": reading.get("seal"), "media_key": media_key, "model": model,
        "read_at": read_at, "confirmed": False, "source": "ocr",
    }


def photos_to_read(attachments: Sequence[Any], state: Any) -> List[Tuple[str, str]]:
    """(S3 key, type) for each stored photo in the message, when the bike the
    conversation is about (or, with none chosen, the conversation) was asked
    for its serial photos by the serial ask or the melt ask; [] otherwise.
    An inline photo that could not be stored has no key and is not read."""
    if state is None:
        return []
    frame = getattr(state, "selected_frame", None) or NO_BIKE
    if frame not in list(state.serials_asked_frames) + list(state.melt_asked_frames):
        return []
    found = []
    for item in attachments:
        url = getattr(item, "url", "") or ""
        mime = getattr(item, "mime_type", None) or ""
        if getattr(item, "kind", None) == "image" and url.startswith("s3://") and mime.startswith("image/"):
            found.append((url[len("s3://"):], mime))  # "s3://" + the key, no bucket (api.py)
    return found


def read_photos(reader: Any, photos: Sequence[Tuple[str, str]], load: Callable[[str], bytes], store: Any,
                *, conversation_id: str, user_key: Optional[str], frame_number: Optional[str],
                emit: Callable[..., None], now: Optional[Callable[[], datetime]] = None,
                references: Any = None) -> int:
    """Reads each photo and keeps each reading of a label or the seal;
    returns how many were kept. Never raises: a photo that cannot be loaded,
    read or kept is logged by its code and skipped, and reference pictures
    that cannot be read are left out and named. The serial itself is never
    logged. `references` is an evidence_check.References over REFERENCE_KEYS."""
    clock = now or (lambda: datetime.now(timezone.utc))
    shown: Sequence[Tuple[bytes, str, str]] = ()
    if references is not None:
        try:
            shown, missing = references.for_component("serial")
        except Exception as exc:
            shown, missing = (), [type(exc).__name__]
        if missing:
            emit("serial_read_references_missing", conversation_id, keys=list(missing))
    kept = 0
    for key, mime in photos:
        try:
            reading = reader.read(load(key), mime, **({"references": shown} if shown else {}))
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
        emit("serial_read", conversation_id, part=reading["part"], legible=doc["legible"], seal=doc["seal"])
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
