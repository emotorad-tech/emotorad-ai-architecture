"""A customer's clip, described once by Gemini so Claude can read it.

Claude has no video input. The old answer was eight sampled stills plus a
local Whisper transcript: slow, gated off in the API, and blind to motion,
sound and timing. A video model that watches the whole clip and writes down
what it saw is better evidence at lower cost, so the clip is summarised once,
at ingest (`api._inbound_attachments`), and the text rides on
`Attachment.summary` into the safety gate and the agent turn. Design:
`docs/superpowers/specs/2026-09-22-video-evidence-gemini-design.md`.

Two ways to hand Gemini the bytes. Under `INLINE_LIMIT` they go inline in the
request, which is one round trip. Above it the Files API is used: upload,
poll until the file is ACTIVE, generate, and delete the file from Google in a
`finally` — the clip is customer evidence and the data boundary in the design
(§4) is "one request, then gone".

The SDK is imported only when a real client is built, so tests inject a fake
and never touch `google.genai`; the inline part is sent in the SDK's own
`PartDict` shape (`{"inline_data": {"data", "mime_type"}}`), which is exactly
what `types.Part.from_bytes` produces, so no SDK type is needed to build it.

Errors never carry the SDK's message: a provider exception can echo request
content, and this text ends up in a log line.
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any, Callable, Mapping, Optional

GEMINI_KEY_ENV = "GEMINI_API_KEY"
# Decided by the person on 2026-09-22; do not change without them.
DEFAULT_MODEL = "gemini-3.8-flash"
# Gemini's 20 MB ceiling on an inline request applies to the request as
# sent, and inline bytes travel base64-encoded at 4/3 their size, so the
# raw clip must stay under 15 MB. 14 MiB leaves room for the prompt and the
# JSON around it; above that the Files API is the route.
INLINE_LIMIT = 14 * 1024 * 1024
# The whole summarise call, upload and polling included. A clip is summarised
# inside the customer's request (design §5: no queue), so this is the longest
# their message can stall before the frames fallback runs instead.
TIMEOUT_SECONDS = 90
# How long to wait between Files API state polls. A 100 MB clip takes Google
# a handful of seconds to process; polling faster only burns quota.
POLL_SECONDS = 2.0

# Fixed, in code, so the description Claude reads is the same shape every
# time and cannot be steered by anything in the conversation. It asks for
# observation only: the diagnosis is Claude's job, with the knowledge base
# and the coverage checks behind it, and a video model guessing at causes
# would hand Claude a conclusion it cannot verify.
PROMPT = """You are an after-sales evidence analyst for an Indian electric cycle company. A customer has sent this video with a support request. Watch and listen to the whole clip and write a detailed, factual description of what it contains, for a support specialist who cannot see it.

Describe only what is present and observable. Cover, where present:
- The component and the area of the bike shown (battery pack, charger, charging port, display, motor, controller, brakes, wheels, drivetrain, frame, or other), and how it is mounted or held.
- Any damage or change to the part that you can actually see, described plainly and with its exact position on the part.
- Indicator lights: which ones, their colours, whether steady or blinking, and the blink pattern with timing where you can count it.
- Any text, symbol or error code readable on a display or label, quoted exactly as shown.
- Sounds coming from the bike: describe them plainly (clicking, grinding, whine, beep, silence) and when they occur relative to what is on screen.
- The customer's spoken words, quoted as closely as you can, and the language or languages they speak in.
- Notable moments with timestamps, in order.
- Problems with the recording itself: too dark, out of focus, shaky, too short.

Never list conditions that were not observed, and do not enumerate faults or hazards by name to say they are absent. If the part of interest is not in frame, say only that. Write plain text in short paragraphs or bullet points. Do not diagnose, do not advise, do not guess at causes, and do not speculate about what is outside the frame."""


class VideoSummaryError(Exception):
    """The clip could not be summarised. The message is a class name or a
    fixed word, never provider text, so it is safe to log."""


class GeminiVideoSummariser:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._clock = clock
        self._sleep = sleep
        if client is None:
            # Lazy: the SDK pulls in a lot, and a deployment without a key (or
            # a test) never needs it. `HttpOptions.timeout` is in milliseconds
            # in google-genai 2.x — verified against the installed SDK, not
            # assumed.
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=TIMEOUT_SECONDS * 1000),
            )
        self._client = client

    def summarise(self, data: bytes, mime: str, name: str = "video") -> str:
        """Plain-text description of the clip, or `VideoSummaryError`."""
        deadline = self._clock() + TIMEOUT_SECONDS
        if len(data) <= INLINE_LIMIT:
            part = {"inline_data": {"data": data, "mime_type": mime}}
            return self._generate([part, PROMPT], deadline)
        return self._via_files_api(data, mime, name, deadline)

    def _via_files_api(self, data: bytes, mime: str, name: str, deadline: float) -> str:
        try:
            uploaded = self._client.files.upload(file=io.BytesIO(data), config={"mime_type": mime})
        except Exception as exc:
            raise VideoSummaryError(type(exc).__name__) from None
        try:
            ready = self._wait_until_active(uploaded, deadline)
            return self._generate([ready, PROMPT], deadline)
        finally:
            # Whatever happened above, the customer's clip does not stay on
            # Google's side. A failed delete is reported but must not mask
            # the real outcome of the summary.
            try:
                self._client.files.delete(name=uploaded.name)
            except Exception:
                logging.getLogger(__name__).warning("gemini file delete failed: %s", uploaded.name)

    def _wait_until_active(self, uploaded: Any, deadline: float) -> Any:
        current = uploaded
        while True:
            # `File.state` is a `FileState` enum that subclasses `str`
            # (PROCESSING / ACTIVE / FAILED), so `.value` gives the plain
            # word on the SDK object and a fake can hand back the word itself.
            state = getattr(current, "state", None)
            state = getattr(state, "value", state) or ""
            if state == "ACTIVE":
                return current
            if state == "FAILED":
                raise VideoSummaryError("file processing failed")
            if self._clock() >= deadline:
                raise VideoSummaryError("timeout")
            self._sleep(POLL_SECONDS)
            try:
                current = self._client.files.get(name=uploaded.name)
            except Exception as exc:
                raise VideoSummaryError(type(exc).__name__) from None

    def _generate(self, contents: list, deadline: float) -> str:
        # The client-level timeout is per HTTP call, so upload, each poll and
        # generate could each take the full budget. The deadline is for the
        # whole summary: generate gets only what is left of it, passed as a
        # per-request `http_options` on `GenerateContentConfig` (a field on
        # google-genai 2.24, timeout in milliseconds, checked against the
        # installed SDK). Whole seconds, so a fake clock's arithmetic and a
        # log line both read cleanly.
        remaining = int(deadline - self._clock())
        if remaining <= 0:
            raise VideoSummaryError("timeout")
        config = {"http_options": {"timeout": remaining * 1000}}
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            raise VideoSummaryError(type(exc).__name__) from None
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise VideoSummaryError("empty summary")
        return text


def summariser_from_env(
    environ: Optional[Mapping[str, str]] = None, client: Any = None
) -> Optional[GeminiVideoSummariser]:
    """None when there is no key: the frames path then runs, so nothing
    regresses on a deployment that has not opted in."""
    env = os.environ if environ is None else environ
    key = (env.get(GEMINI_KEY_ENV) or "").strip()
    if not key:
        return None
    return GeminiVideoSummariser(key, client=client)
