"""A safety look at every photo (spec 2026-10-01, photo safety).

The safety gate scans what a customer types and what a video's description
says. A photo was seen only by the model, so a photo of a burning pack was
handled by whatever the model chose to write (staging, 2026-10-01). This
describes each photo once, on arrival, in words the gate can scan: every
hazard stated, and every absent one written as "no <thing> visible" so the
gate's negation handling reads it as absent. The model still gets the photo.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Mapping, Optional

OPENROUTER_PHOTO_MODEL = "google/gemini-3.8-flash"
TIMEOUT_SECONDS = 15
# Photos are capped at 4 MB on the way in (attachments.py); this is a
# backstop for whatever else calls the checker.
INLINE_LIMIT = 8 * 1024 * 1024

PROMPT = (
    "You are checking a photo a customer sent to an electric cycle company's support chat, for "
    "safety only. In 2 to 4 plain sentences, say what is visible of the bike, battery, charger or "
    "wiring. Then say, for each of these, whether it is visible: smoke, flames, scorch or burn "
    "marks, swelling or bulging, melting, leaking fluid, sparks. Write each one that is not visible "
    "as \"no <thing> visible\", for example \"no smoke visible\". Describe only what is in the photo; "
    "do not guess at causes or give advice."
)


class PhotoCheckError(Exception):
    """A stable code only: a provider message can echo the request."""


class OpenRouterPhotoChecker:
    provider = "openrouter"

    def __init__(self, transport: Any, model: str = OPENROUTER_PHOTO_MODEL, zdr: bool = True) -> None:
        self.model = model
        self.zdr = zdr
        self._transport = transport

    def check(self, data: bytes, mime: str) -> str:
        from .llm import from_openai_response
        from .openrouter import CHAT_PATH, OpenRouterError

        if len(data) > INLINE_LIMIT:
            raise PhotoCheckError("too_large")
        url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            "usage": {"include": True},
        }
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        try:
            response = from_openai_response(self._transport.post(CHAT_PATH, body, timeout=TIMEOUT_SECONDS))
        except OpenRouterError as exc:
            raise PhotoCheckError(exc.code) from None
        text = (response.text or "").strip()
        if not text:
            raise PhotoCheckError("empty")
        return text


def photo_checker_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[OpenRouterPhotoChecker]:
    """OpenRouter when its key is set, else None: offline and local runs keep
    today's behaviour."""
    from .openrouter import API_KEY_ENV, OpenRouterTransport

    env = os.environ if environ is None else environ
    key = (env.get(API_KEY_ENV) or "").strip()
    if not key:
        return None
    transport = OpenRouterTransport(
        api_key=key,
        base_url=env.get("EMOTORAD_OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
        timeout=TIMEOUT_SECONDS,
    )
    return OpenRouterPhotoChecker(transport, zdr=env.get("EMOTORAD_OPENROUTER_ZDR", "1") == "1")
