"""Derivatives made once at upload time. S3 does not resize on request the way
Cloudinary did, so the 900px WebP a chat client wants is produced here, beside
the original, and never enlarged (the Cloudinary lesson: a 480px source scaled
up is blurrier and bigger than the original)."""

from __future__ import annotations

import base64
import io
from typing import Optional

from .. import video

MAX_WIDTH = 900


def image_w900_webp(data: bytes) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image = image.convert("RGB")
    if image.width > MAX_WIDTH:
        image.thumbnail((MAX_WIDTH, MAX_WIDTH * 10))
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=82)
    return buffer.getvalue()


def video_poster_jpg(data: bytes) -> Optional[bytes]:
    frames = video.extract_frames(data, ".mp4", count=1)
    return base64.b64decode(frames[0]) if frames else None
