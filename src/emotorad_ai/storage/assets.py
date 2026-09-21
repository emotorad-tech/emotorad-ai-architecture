"""Upload a guide photo, clip or document to the media bucket, with its
derivatives, and build the `id:`/media-list YAML to paste into a knowledge
record.

This is the one implementation; `scripts/upload_asset.py` (a CLI, run with a
human's own AWS credentials — staff loading is a terminal task) and the
playground's admin upload form both delegate to `upload_asset` here.
"""

from __future__ import annotations

from typing import Any, Dict

from . import keys
from .derivatives import image_w900_webp, video_poster_jpg


def upload_asset(store: Any, data: bytes, mime: str, programme: str, category: str, kind: str, slug: str) -> Dict[str, str]:
    key = keys.asset_key(programme, category, kind, slug, mime)
    store.put_bytes(key, data, mime)
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
