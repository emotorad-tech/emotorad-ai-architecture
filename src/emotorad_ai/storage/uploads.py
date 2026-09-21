"""The presign → attach handshake.

`begin_*` derives a key, presigns a PUT, and remembers what was promised:
key, content type, size. `claim` is called when a message references the
upload: it HEADs the object and refuses anything that does not match. A client
therefore cannot presign one thing and upload another, and an upload nobody
ever references is an id nobody claims — the bucket lifecycle rule handles the
bytes, and there is no cleanup job to build.

In-memory, like ConversationStore. A pending upload lives for the PUT window
plus an hour, so a customer who uploads and then types for a while can still
attach it; after that the id is gone and the client presigns again.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from . import keys
from .keys import KeyValidationError
from .s3 import PUT_EXPIRY

CLAIM_WINDOW = 3600


class UploadError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Pending:
    upload_id: str
    key: str
    mime: str
    size: int
    kind: str
    tree: str
    expires_at: float


@dataclass(frozen=True)
class Claimed:
    upload_id: str
    key: str
    mime: str
    size: int
    kind: str


class UploadRegistry:
    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._pending: Dict[str, Pending] = {}
        self._lock = threading.Lock()

    # -- begin -------------------------------------------------------------------

    def begin_customer(self, cluster_id: str, conversation_id: str, mime: str, size: int) -> Tuple[Pending, Dict[str, Any]]:
        self._check_type_and_size(mime, size, keys.customer_kind_for(mime))
        upload_id = keys.new_upload_id()
        try:
            key = keys.customer_key(cluster_id, conversation_id, keys.customer_kind_for(mime), upload_id, mime)
        except KeyValidationError as exc:
            raise UploadError(400, str(exc)) from None
        return self._remember(upload_id, key, mime, size, keys.customer_kind_for(mime), "customers")

    def begin_asset(self, programme: str, category: str, kind: str, slug: str, mime: str, size: int) -> Tuple[Pending, Dict[str, Any]]:
        self._check_type_and_size(mime, size, keys.customer_kind_for(mime))
        try:
            key = keys.asset_key(programme, category, kind, slug, mime)
        except KeyValidationError as exc:
            raise UploadError(400, str(exc)) from None
        return self._remember(keys.new_upload_id(), key, mime, size, kind, "assets")

    def _check_type_and_size(self, mime: str, size: int, cap_kind: str) -> None:
        if mime not in keys.MIME_TYPES:
            raise UploadError(415, "unsupported content type %r; allowed: %s" % (mime, ", ".join(keys.MIME_TYPES)))
        if not isinstance(size, int) or size <= 0:
            raise UploadError(400, "size_bytes must be a positive integer")
        cap = keys.SIZE_CAPS[cap_kind]
        if size > cap:
            raise UploadError(413, "%s uploads are capped at %d bytes" % (cap_kind, cap))

    def _remember(self, upload_id: str, key: str, mime: str, size: int, kind: str, tree: str) -> Tuple[Pending, Dict[str, Any]]:
        presign = self._store.presign_put(key, mime, size)
        pending = Pending(upload_id, key, mime, size, kind, tree, self._clock() + PUT_EXPIRY)
        with self._lock:
            self._pending[upload_id] = pending
        return pending, presign

    # -- claim -------------------------------------------------------------------

    def claim(self, upload_id: str) -> Claimed:
        with self._lock:
            pending = self._pending.get(upload_id)
            if pending is not None and self._clock() > pending.expires_at + CLAIM_WINDOW:
                del self._pending[upload_id]
                pending = None
        if pending is None:
            raise UploadError(404, "unknown or expired upload id")

        head = self._store.head(pending.key)
        if head is None:
            raise UploadError(409, "the upload has not completed")
        if head["size"] != pending.size or head["mime"] != pending.mime:
            # Not a claim we can honour; the object stays and the lifecycle rule
            # removes it. The id stays too, so a retry after a correct re-upload
            # to the same signed URL still works.
            raise UploadError(409, "the uploaded object does not match what was presigned")

        with self._lock:
            self._pending.pop(upload_id, None)
        return Claimed(upload_id, pending.key, pending.mime, pending.size, pending.kind)

    def forget(self, upload_id: str) -> None:
        with self._lock:
            self._pending.pop(upload_id, None)
