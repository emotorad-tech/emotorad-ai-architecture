# src/emotorad_ai/attachments.py
"""Inbound attachments as Claude content blocks — in production, not only in
the playground.

Three sources, one output: a `data:` URL is decoded here; an `s3://<key>` is
fetched server-side through `fetch` (the instance role); an http(s) image is
passed by URL. No S3 URL of any kind is ever handed to the model, and a fetch
that fails becomes a sentence the model can read rather than an exception the
customer sees. Video follows the playground's rule: stills plus the narration,
and an unreadable clip says so instead of being silently "seen".
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import video
from .contract import Attachment, InboundMessage

UNRETRIEVABLE = "[An attachment could not be retrieved; do not describe it.]"

MAX_MODEL_EDGE = 1600
MAX_MODEL_BYTES = 3 * 1024 * 1024


def fit_for_model(data: bytes, mime: str) -> Tuple[bytes, str]:
    """Images go to the model at most 1600px on the long edge and under 3 MB,
    re-encoded as JPEG when they were larger. Claude sees no more detail above
    ~1600px, and the request limit is on base64 size, which is 4/3 of this."""
    import io

    from PIL import Image

    if len(data) <= MAX_MODEL_BYTES:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if max(image.size) <= MAX_MODEL_EDGE:
                    return data, mime
        except Exception:
            return data, mime
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=85)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return data, mime


def _payload(attachment: Attachment, fetch: Optional[Callable[[str], bytes]]) -> Union[bytes, str, None]:
    """Bytes for data:/s3:// sources, the URL string for http(s), None to drop."""
    url = attachment.url or ""
    if url.startswith("data:"):
        _, _, data = url.partition(",")
        try:
            return base64.b64decode(data)
        except Exception:
            return None
    if url.startswith("s3://"):
        if fetch is None:
            return UNRETRIEVABLE
        try:
            return fetch(url[len("s3://"):])
        except Exception:
            return UNRETRIEVABLE
    if url.startswith(("http://", "https://")):
        return url
    return None


def _video_blocks(data: bytes, mime: str, name: str) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    suffix = ".mp4"
    wav = video.extract_audio(data, suffix)
    transcript = video.transcribe_audio(wav) if wav else None
    if transcript and transcript.get("text"):
        blocks.append(
            {
                "type": "text",
                "text": (
                    "[What the customer says in the video '%s' (transcribed speech, detected "
                    "language %s): \"%s\"  — this is their narration only. It is not a description "
                    "of any sound the bike makes; you cannot hear the bike.]"
                    % (name, transcript.get("language", "unknown"), transcript["text"])
                ),
            }
        )
    frames = video.extract_frames(data, suffix)
    if not frames:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "[The customer sent a video (%s) that could not be read here. Do not describe "
                    "or assess it. Say you could not open it and ask for a photo of the same thing "
                    "instead.]" % name
                ),
            }
        )
        return blocks
    blocks.append(
        {
            "type": "text",
            "text": (
                "[%d still frames sampled evenly from the customer's video '%s', in order. You are "
                "seeing stills, not the video: judge only what is visible in them.]" % (len(frames), name)
            ),
        }
    )
    for frame in frames:
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame}})
    return blocks


def content_blocks(
    attachments: Sequence[Attachment], fetch: Optional[Callable[[str], bytes]] = None
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for attachment in attachments:
        mime = (attachment.mime_type or "").lower()
        if not mime and attachment.url.startswith("data:"):
            mime = attachment.url[len("data:"):].split(";")[0].lower()
        name = attachment.url.rsplit("/", 1)[-1][:80] if attachment.url.startswith("s3://") else "attachment"

        payload = _payload(attachment, fetch)
        if payload is None:
            continue
        if payload == UNRETRIEVABLE:
            blocks.append({"type": "text", "text": UNRETRIEVABLE})
            continue
        if isinstance(payload, str):  # http(s) URL
            if mime.startswith("image/"):
                blocks.append({"type": "image", "source": {"type": "url", "url": payload}})
            continue

        if video.is_video(mime, name):
            blocks.extend(_video_blocks(payload, mime, name))
        elif mime.startswith("image/"):
            fitted, fitted_mime = fit_for_model(payload, mime)
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": fitted_mime, "data": base64.b64encode(fitted).decode()},
                }
            )
        elif mime == "application/pdf":
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(payload).decode()},
                }
            )
    return blocks


def user_content(message: InboundMessage, fetch: Optional[Callable[[str], bytes]] = None) -> Union[str, List[Dict[str, Any]]]:
    """A plain string when there are no attachments — unchanged history shape —
    else blocks with the text last, so the model reads the picture before the
    question about it. The API rejects an empty text block."""
    blocks = content_blocks(message.attachments, fetch)
    if not blocks:
        return message.message_text
    blocks.append({"type": "text", "text": message.message_text or "(attachment)"})
    return blocks
