"""A safety look at every photo (spec 2026-10-01, photo safety, revised).

The safety gate scans what a customer types and what a video's description
says. A photo was seen only by the model, so a photo of a burning pack was
handled by whatever the model chose to write (staging, 2026-10-01).

The model answers yes or no per live hazard, in JSON, and the text the gate
scans is written here, only when a hazard is present. It is never asked to
describe the photo in prose: a description that lists hazards to say they are
absent trips the gate, which is the mistake the video prompt made and fixed on
22 September. Damage that has already happened (dents, cracks, punctures,
scorch marks, melting) is not a live hazard: the agents assess it, and the
damage flows ask for exactly those photos. The model still gets the photo.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, List, Mapping, Optional, Sequence

OPENROUTER_PHOTO_MODEL = "google/gemini-3.8-flash"
TIMEOUT_SECONDS = 15
# Uploaded photos are capped at 10 MB (storage/keys.py), inline ones at 4 MB
# (attachments.py); this is a backstop for whatever else calls the checker.
INLINE_LIMIT = 12 * 1024 * 1024

# In the order the description lists them. Each word is one the safety gate's
# patterns match.
LIVE_HAZARDS = ("smoke", "flames", "swelling", "leaking", "sparks")
_WORDS = {"leaking": "leaking fluid"}

PROMPT = (
    "You are checking a photo a customer sent to an electric cycle company's support chat, for live "
    "safety hazards only. Answer with JSON only, with exactly these keys, each true or false: "
    "smoke, flames, swelling, leaking, sparks. Set a key to true only when it is clearly happening "
    "in the photo now: smoke or vapour coming from the bike, battery or charger; open flames; a "
    "battery pack that is swollen or bulging; fluid leaking from the battery; sparks. Damage that has "
    "already happened (dents, cracks, punctures, scorch marks, melted plastic) is not a live hazard: "
    "answer false for it. When unsure, answer false."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class PhotoCheckError(Exception):
    """A stable code only: a provider message can echo the request."""


class OpenRouterPhotoChecker:
    provider = "openrouter"

    def __init__(self, transport: Any, model: str = OPENROUTER_PHOTO_MODEL, zdr: bool = True) -> None:
        self.model = model
        self.zdr = zdr
        self._transport = transport

    def check(self, data: bytes, mime: str) -> List[str]:
        """The live hazards in the photo, in LIVE_HAZARDS order; [] for none."""
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
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        }
        if self.zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        try:
            response = from_openai_response(self._transport.post(CHAT_PATH, body, timeout=TIMEOUT_SECONDS))
        except OpenRouterError as exc:
            raise PhotoCheckError(exc.code) from None
        return _hazards(response.text or "")


def _hazards(text: str) -> List[str]:
    try:
        answer = json.loads(_FENCE.sub("", text.strip()))
    except ValueError:
        raise PhotoCheckError("bad_json") from None
    if not isinstance(answer, dict):
        raise PhotoCheckError("bad_json")
    # Only a real true counts: "yes", 1 or a missing key is not a hazard.
    return [hazard for hazard in LIVE_HAZARDS if answer.get(hazard) is True]


def describe(hazards: Sequence[str]) -> Optional[str]:
    """The text the safety gate scans, or None when there is no live hazard,
    so a harmless photo cannot trip anything."""
    if not hazards:
        return None
    words = [_WORDS.get(hazard, hazard) for hazard in hazards]
    listed = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return "The customer's photo shows %s." % listed


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
