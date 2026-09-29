"""The permanent record of one photo or video already written to the bucket
(spec docs/superpowers/specs/2026-09-29-customer-media-in-s3-design.md §2).

Built from `bucket` and `key` alone, never from a URL: a presigned link
expires and carries a credential in its query string, so one must never reach
a record kept for ever. The guard here is the whole of that story: every
caller that builds a record goes through it.
"""

from __future__ import annotations

from typing import Any, Dict

KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_DOCUMENT = "document"
KINDS = (KIND_IMAGE, KIND_VIDEO, KIND_DOCUMENT)

SOURCE_INLINE = "inline"
SOURCE_UPLOAD = "upload"
SOURCES = (SOURCE_INLINE, SOURCE_UPLOAD)

# The marks of a presigned URL. Checked case-insensitively: S3 itself treats
# the header/query name case-insensitively, and a caller could easily lower-case it.
_SIGNED_MARKERS = ("?", "x-amz-")


def _check_not_signed(label: str, value: str) -> None:
    if not value:
        raise ValueError("media_record: %s must not be empty" % label)
    lowered = value.lower()
    for marker in _SIGNED_MARKERS:
        if marker in lowered:
            raise ValueError(
                "media_record: %s looks like a presigned URL (found %r), "
                "never the bucket and key alone: %r" % (label, marker, value)
            )


def media_record(
    bucket: str,
    key: str,
    kind: str,
    mime_type: str,
    size_bytes: int,
    conversation_id: str,
    cluster_id: str,
    source: str,
    stored_at: str,
) -> Dict[str, Any]:
    """Shape the permanent record of one object. Talks to neither S3 nor
    Mongo: the object must already be in the bucket before this is called.

    Raises ValueError if `bucket` or `key` is empty or carries `?` or
    `X-Amz-` (case-insensitively, the mark of a presigned URL), or if `kind`
    or `source` is not one of the allowed values.
    """
    _check_not_signed("bucket", bucket)
    _check_not_signed("key", key)
    if kind not in KINDS:
        raise ValueError("media_record: unknown kind %r, expected one of %s" % (kind, KINDS))
    if source not in SOURCES:
        raise ValueError("media_record: unknown source %r, expected one of %s" % (source, SOURCES))
    return {
        "_id": key,
        "bucket": bucket,
        "key": key,
        "uri": "s3://%s/%s" % (bucket, key),
        "kind": kind,
        "mime_type": mime_type,
        "size_bytes": int(size_bytes),
        "conversation_id": conversation_id,
        "cluster_id": cluster_id,
        "source": source,
        "stored_at": stored_at,
    }
