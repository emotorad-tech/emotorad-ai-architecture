"""Upload a guide photo, clip or document to the media bucket, with its
derivatives, and build the `id:`/media-list YAML to paste into a knowledge
record.

This is the one implementation of the derivatives. Two callers:

* `scripts/upload_asset.py` (a CLI, run with a human's own AWS credentials)
  has the bytes in hand and calls `upload_asset`: PUT the original, then the
  derivatives.
* the playground's admin upload form never has the bytes. The browser asks
  `POST /uploads` for a presigned PUT and sends the file straight to S3, the
  way the chat page sends a clip; the server then calls `finish_asset`, which
  reads the original back from the bucket and writes only the derivatives.
  The file therefore never passes through nginx, uvicorn or Streamlit, none
  of which were built to carry a 100 MB clip (nginx's default body cap is
  1 MB, which is how this surfaced: photos went up, the first video was 413).
"""

from __future__ import annotations

from typing import Any, Dict

from . import keys
from .derivatives import image_w900_webp, video_poster_jpg


def upload_asset(store: Any, data: bytes, mime: str, programme: str, category: str, kind: str, slug: str) -> Dict[str, str]:
    key = keys.asset_key(programme, category, kind, slug, mime)
    store.put_bytes(key, data, mime)
    return _write_derivatives(store, key, data)


def finish_asset(store: Any, key: str) -> Dict[str, str]:
    """The original is already in the bucket (a presigned PUT from the
    browser, claimed by the caller): read it back and write its derivatives.
    A document has none, and is not fetched at all."""
    data = store.get_bytes(key) if keys.derivative_keys(key) else b""
    return _write_derivatives(store, key, data)


def _write_derivatives(store: Any, key: str, data: bytes) -> Dict[str, str]:
    written = {"id": key[len("assets/"):], "original": key}
    for name, derivative_key in keys.derivative_keys(key).items():
        if name == "w900":
            store.put_bytes(derivative_key, image_w900_webp(data), "image/webp")
            written[name] = derivative_key
        elif name == "poster":
            poster = video_poster_jpg(data)
            if poster:
                store.put_bytes(derivative_key, poster, "image/jpeg")
                written[name] = derivative_key
    return written


def yaml_snippet(asset_id: str, kind: str, caption: str) -> str:
    """The three lines an author pastes into a record's `media:` list."""
    return "  - id: %s\n    kind: %s\n    caption: %s\n" % (asset_id, kind, caption)
